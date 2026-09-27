#!/usr/bin/env python3
"""Custom-emoji (Premium) reactions for @ForwarderClaudeBot, kept warm.

Bots may add a custom reaction only when it is already on the message (or a
channel's admins whitelisted it; groups and DMs have no such setting — tested
2026-09-26). So a reaction is three steps:

  1. seed   — the operator's Premium session adds the custom reaction,
  2. copy   — the bot adds the same one,
  3. unseed — the operator's session restores its own previous reactions.

Only the bot's reaction remains. What the operator sees is their own copy for
the seed → unseed window, and that window is what this is tuned for:

  * the bot's copy goes over the bot's OWN MTProto session straight to DC1
    (Miami — operator, bot and this box's nearest DC; ~39 ms RTT) instead of
    the Bot API, whose Amsterdam server relays to DC1 (~205 ms). Safe next to
    the plugin's getUpdates poller: receive_updates=False never subscribes
    this session to updates (verified on the watchdog bot, then this one);
  * fast path (default): no step waits for an ack. The copy leaves +20 ms
    after the seed; the unseed +36 ms, as invokeAfterMsg(seed) so the server
    never runs it before the seed; an insurance unseed follows; a verify read
    repairs the rest. The unseed must trail the COPY: Telegram loses one of
    two overlapping writes from different accounts (the seed comes back, or
    the bot's reaction drops out of the message's summary). A copy that beats
    a slow seed fails cleanly — the chained unseed still clears it — and is
    retried later; anything else falls back to
  * the safe path: copy COPY_DELAY_MS after the seed, unseed on the copy's
    ack (one window of ~60-80 ms, every write settled before the next);
  * both sessions persistent and pinged every KEEPALIVE_S; the unit sets
    MemorySwapMax=0 so an idle helper is never paged out on this box.

Measured 2026-09-27 on the operator's own messages (Telegram's clock): fast
path median ~30 ms, 87 % in one attempt; safe path ~60-80 ms (was ~210-270 ms
through the Bot API, ~4 s with a per-call login). The lab behind the numbers:
scripts/react_window_lab.py; the story: docs/vps.md. The operator granted
standing use of their session (2026-09-26); the bot's MTProto session is
~/.claude-react-bot.session.

  serve                     run the helper (claude-react-helper.service)
  react --chat --msg ...    client: one reaction through the running helper
  ping                      client: liveness

The Telegram plugin's react tool (deploy/patch_telegram_plugin.py) calls the
socket directly with the inbound message's date: private chats and basic groups
number messages per account, so the bot-side id is mapped to the operator's
side by date (+ text as a tiebreak); supergroup ids are shared.

Client mode is stdlib-only so it starts fast under plain python3.
"""
import argparse
import json
import os
import random
import socket
import sys
import time

SOCK = os.environ.get("REACT_HELPER_SOCK", "/run/claude-react/react.sock")
BOT_ENV = os.path.expanduser("~/.claude/channels/telegram/.env")
BOT_SESSION = os.path.expanduser("~/.claude-react-bot.session")
KEEPALIVE_S = 25          # MTProto liveness ping on both sessions
RATE_MAX = 12             # custom reactions per rolling minute (runaway guard)
PREMIUM_MAX = 3           # reactions_user_max_premium
COPY_DELAY_MS = 10        # copy this long after the seed is SENT (5 ms raced it; 10 ms: 5/5)
INVALID_RETRY_S = (0, 0.03, 0.06, 0.12, 0.25)  # copy retries once the seed is acked
STRATEGY = "fast"         # "fast" (blind, chained unseed) or "safe" (unseed on the copy's ack)
# fast: (copy, unseed) send times in ms after the seed's send, per attempt. The
# unseed trails the copy by 16 ms: arriving <= ~10.4 ms after the copy corrupted
# the reaction state often in the lab (~17 % when together), >= 11 ms ~2 % (slow
# copies; no gap up to 30 ms removed them — the insurance unseed clears those).
# A copy that beats a slow seed fails cleanly (the chained unseed still clears
# the seed), so attempt 2 simply copies later; anything else → the safe path.
FAST_SCHEDULE = ((20, 36), (26, 42))
SKEW_CAP_MS = 6           # fast: bound on the per-connection path compensation
LOST_RECHECK_S = 0.25     # fast: a verify read missing the bot's reaction is re-read after this
FAST_INSURE_MS = 40       # fast: second unseed this long after the first (chained after it):
                          # a copy write landing late can resurrect the seed; this clears it
                          # at ~+50 ms instead of the verify read's ~+150 ms (no-op otherwise)


class HelperError(Exception):
    pass


# --------------------------------------------------------------------- client

def _call(req: dict, timeout: float = 10) -> dict:
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.settimeout(timeout)
    try:
        s.connect(SOCK)
        s.sendall(json.dumps(req).encode() + b"\n")
        buf = b""
        while b"\n" not in buf:
            chunk = s.recv(65536)
            if not chunk:
                break
            buf += chunk
    finally:
        s.close()
    return json.loads(buf.split(b"\n", 1)[0] or b'{"ok": false, "error": "empty reply"}')


def _unix_ts(ts: str | None) -> int | None:
    if not ts:
        return None
    if ts.isdigit():
        return int(ts)
    from datetime import datetime
    return int(datetime.fromisoformat(ts.replace("Z", "+00:00")).timestamp())


def client_react(args) -> int:
    resp = _call({
        "op": "react",
        "chat_id": args.chat,
        "message_id": args.msg,
        "custom_emoji_id": str(args.emoji_id),
        "date": _unix_ts(args.ts),
        "text": args.text,
        **({"strategy": args.strategy} if args.strategy else {}),
        **({"copy_at_ms": args.copy_at_ms} if args.copy_at_ms is not None else {}),
        **({"unseed_at_ms": args.unseed_at_ms} if args.unseed_at_ms is not None else {}),
        **({"copy_delay_ms": args.copy_delay_ms} if args.copy_delay_ms is not None else {}),
    })
    print(json.dumps(resp))
    return 0 if resp.get("ok") else 1


def client_ping(_args) -> int:
    resp = _call({"op": "ping"}, timeout=5)
    print(json.dumps(resp))
    return 0 if resp.get("ok") else 1


# --------------------------------------------------------------------- server

def serve(_args) -> int:
    import asyncio
    import collections
    import urllib.error
    import urllib.request
    from datetime import datetime, timezone

    from dotenv import load_dotenv
    from telethon import TelegramClient, errors, functions, types
    from telethon.network.requeststate import RequestState
    from telethon.sessions import StringSession
    from telethon.tl.core.rpcresult import RpcResult

    # systemd injects .env/.env.local; dotenv covers manual runs (never overrides).
    load_dotenv("/home/forwarder/app/.env.local")
    load_dotenv("/home/forwarder/app/.env")
    api_id, api_hash = int(os.environ["TELEGRAM_API_ID"]), os.environ["TELEGRAM_API_HASH"]

    def bot_token() -> str:
        for line in open(BOT_ENV, encoding="utf-8"):
            if line.startswith("TELEGRAM_BOT_TOKEN="):
                return line.split("=", 1)[1].strip()
        raise SystemExit("no TELEGRAM_BOT_TOKEN in " + BOT_ENV)

    def ms_since(t: float) -> int:
        return round((time.perf_counter() - t) * 1000)

    def rkey(r) -> tuple:
        if isinstance(r, types.ReactionCustomEmoji):
            return ("custom", r.document_id)
        return (type(r).__name__, getattr(r, "emoticon", None))

    def own_reactions(msg) -> list:
        """The operator's own reactions (chosen_order is set only on theirs)."""
        results = msg.reactions.results if msg.reactions else []
        return [r.reaction for r in sorted(
            (r for r in results if r.chosen_order is not None), key=lambda r: r.chosen_order)]

    def others_have(msg, doc_id: int) -> bool:
        """Someone other than the operator (here: the bot) shows this custom reaction."""
        return any(isinstance(r.reaction, types.ReactionCustomEmoji)
                   and r.reaction.document_id == doc_id
                   and r.count - (r.chosen_order is not None) >= 1
                   for r in (msg.reactions.results if msg.reactions else []))

    async def flush() -> None:
        """Let Telethon's sender + connection loops hand queued packets to the socket."""
        for _ in range(6):
            await asyncio.sleep(0)

    async def until(t: float) -> None:
        """Sleep to perf_counter() == t, yielding (a busy spin would stall the send loops)."""
        while (d := t - time.perf_counter()) > 0.0015:
            await asyncio.sleep(d - 0.0012)
        while time.perf_counter() < t:
            await asyncio.sleep(0)

    class Helper:
        def __init__(self) -> None:
            self.tg = TelegramClient(StringSession(os.environ["TELEGRAM_SESSION"]),
                                     api_id, api_hash, receive_updates=False)
            try:
                bot_sess = open(BOT_SESSION, encoding="utf-8").read().strip()
            except FileNotFoundError:
                bot_sess = ""
            self.bot = TelegramClient(StringSession(bot_sess), api_id, api_hash,
                                      receive_updates=False)
            self.token = bot_token()
            self.lock = asyncio.Lock()
            self.recent: collections.deque[float] = collections.deque()
            self.peers: dict[int, object] = {}
            self.bot_peers: dict[int, object] = {}
            # rpc_result msg_id per request msg_id: Telegram's clock (unixtime * 2^32,
            # sub-ms) for when each result left — the server-side window.
            self.srv_ids: collections.OrderedDict[int, int] = collections.OrderedDict()
            self.raw_ok = False
            self.rtts = {"op": collections.deque(maxlen=12), "bot": collections.deque(maxlen=12)}

        def hook_results(self) -> bool:
            """Record server msg ids of rpc results. The fast path needs Telethon
            internals (RequestState, the sender's queue); any drift → safe path."""
            try:
                sender = self.tg._sender
                orig = sender._handlers[RpcResult.CONSTRUCTOR_ID]
                assert callable(sender._send_queue.append) and hasattr(sender, "_user_connected")
                RequestState(functions.help.GetNearestDcRequest(), after=None)

                async def rpc(message):
                    self.srv_ids[message.obj.req_msg_id] = message.msg_id
                    while len(self.srv_ids) > 64:
                        self.srv_ids.popitem(last=False)
                    return await orig(message)
                sender._handlers[RpcResult.CONSTRUCTOR_ID] = rpc
                return True
            except Exception as e:  # noqa: BLE001
                print(f"fast path disabled — Telethon internals changed? {type(e).__name__}: {e}")
                return False

        def raw(self, client, request, after=None, stamps=None, name=None):
            """Queue a request without Telethon's call wrapper: exact send order,
            and `after` → invokeAfterMsg (the server runs it after that request).
            stamps[name] gets the local time its result arrived."""
            sender = client._sender
            if not sender._user_connected:
                raise ConnectionError("not connected")
            st = RequestState(request, after=after)
            if stamps is not None:
                st.future.add_done_callback(
                    lambda _f: stamps.__setitem__(name, time.perf_counter()))
            sender._send_queue.append(st)
            return st

        async def outcome(self, st):
            """(result, None) or (None, exception) — never raises, never cancels."""
            try:
                return await asyncio.wait_for(asyncio.shield(st.future), 10), None
            except Exception as e:  # noqa: BLE001
                return None, e

        async def start(self) -> None:
            await self.tg.connect()
            if not await self.tg.is_user_authorized():
                raise SystemExit("operator session is not authorized")
            me = await self.tg.get_me()
            self.me_id = me.id
            if not me.premium:
                print("warning: operator account is not Premium — seeding custom reactions will fail")
            await self.bot.connect()
            bme = await self.bot.get_me() if await self.bot.is_user_authorized() else None
            if bme is None or str(bme.id) != self.token.split(":", 1)[0]:
                # First run, or the plugin now runs a different bot.
                await self.bot.sign_in(bot_token=self.token)
                fd = os.open(BOT_SESSION, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
                os.write(fd, self.bot.session.save().encode())
                os.close(fd)
                bme = await self.bot.get_me()
                print("bot: new MTProto authorization saved")
            self.bot_peer = await self.tg.get_input_entity(bme.username)
            self.raw_ok = self.hook_results()
            if self.raw_ok:
                for _ in range(4):
                    await self.ping_rtts()
            print(f"ready: operator {self.me_id} (dc{self.tg.session.dc_id}), "
                  f"bot @{bme.username} (dc{self.bot.session.dc_id}), "
                  f"strategy {STRATEGY if self.raw_ok else 'safe'}, "
                  f"rtt op {min(self.rtts['op'], default=0) * 1000:.1f} / "
                  f"bot {min(self.rtts['bot'], default=0) * 1000:.1f} ms")

        async def ping_rtts(self) -> None:
            """One MTProto ping per session; keeps the recent round trips."""
            for name, client in (("op", self.tg), ("bot", self.bot)):
                done: dict[str, float] = {}
                t = time.perf_counter()
                st = self.raw(client, functions.PingRequest(ping_id=random.getrandbits(63)),
                              None, done, "pong")
                _, err = await self.outcome(st)
                if err is None and "pong" in done:
                    self.rtts[name].append(done["pong"] - t)

        def skew_ms(self) -> float:
            """How much later a packet reaches Telegram over the bot's connection
            than over the operator's (half the min-RTT difference; each TCP
            connection gets its own path — 32-40 ms RTT seen from this box).
            The fast path sends the copy this much earlier, so copy and unseed
            land exactly FAST_SCHEDULE apart whatever paths the sessions got."""
            if not (self.rtts["op"] and self.rtts["bot"]):
                return 0.0
            skew = (min(self.rtts["bot"]) - min(self.rtts["op"])) / 2 * 1000
            return max(-SKEW_CAP_MS, min(SKEW_CAP_MS, skew))

        async def keepalive(self) -> None:
            while True:
                await asyncio.sleep(KEEPALIVE_S)
                try:
                    if self.raw_ok:
                        await self.ping_rtts()   # liveness + the path skew
                    else:
                        await self.tg(functions.help.GetNearestDcRequest())
                        await self.bot(functions.users.GetUsersRequest([types.InputUserSelf()]))
                except Exception as e:  # noqa: BLE001 — logged, next tick retries
                    print(f"keepalive: {type(e).__name__}: {e}")

        async def peer_for(self, chat_id: int):
            if chat_id not in self.peers:
                self.peers[chat_id] = await self.tg.get_input_entity(chat_id)
            return self.peers[chat_id]

        async def bot_peer_for(self, chat_id: int):
            if chat_id not in self.bot_peers:
                self.bot_peers[chat_id] = await self.bot.get_input_entity(chat_id)
            return self.bot_peers[chat_id]

        async def locate(self, chat_id: int, bot_msg: int, date, text):
            """(peer, raw operator-side Message) for the bot-side message id."""
            if str(chat_id).startswith("-100"):
                peer = await self.peer_for(chat_id)
                return peer, await self.fetch(peer, chat_id, bot_msg)
            if chat_id > 0:
                if chat_id != self.me_id:
                    raise HelperError("private chat is not the operator's chat with the bot")
                peer, want_out = self.bot_peer, True
            else:
                peer, want_out = await self.peer_for(chat_id), None
            if date is None:
                raise HelperError("date required to map a per-account message id")
            # Messages sent at or before `date`, newest first — any age, one call.
            hist = await self.tg(functions.messages.GetHistoryRequest(
                peer=peer, offset_id=0,
                offset_date=datetime.fromtimestamp(int(date) + 1, tz=timezone.utc),
                add_offset=0, limit=10, max_id=0, min_id=0, hash=0))
            cands = [m for m in hist.messages
                     if isinstance(m, types.Message)
                     and int(m.date.timestamp()) == int(date)
                     and (want_out is None or m.out == want_out)]
            if len(cands) > 1 and text is not None:
                cands = [m for m in cands if (m.message or "") == text] or cands
            if len(cands) != 1:
                raise HelperError(f"cannot map message {bot_msg} ({len(cands)} candidates at {date})")
            return peer, cands[0]

        async def fetch(self, peer, chat_id: int, msg_id: int):
            if str(chat_id).startswith("-100"):
                res = await self.tg(functions.channels.GetMessagesRequest(
                    channel=peer, id=[types.InputMessageID(msg_id)]))
            else:
                res = await self.tg(functions.messages.GetMessagesRequest(
                    id=[types.InputMessageID(msg_id)]))
            msgs = [m for m in res.messages if isinstance(m, types.Message)]
            if not msgs:
                raise HelperError(f"message {msg_id} not found")
            return msgs[0]

        async def copy(self, chat_id: int, bpeer, bot_msg: int, doc_id: int) -> str:
            """The bot's reaction: 'ok' or 'invalid' (not on the message yet)."""
            try:
                await self.bot(functions.messages.SendReactionRequest(
                    peer=bpeer, msg_id=bot_msg,
                    reaction=[types.ReactionCustomEmoji(document_id=doc_id)]))
            except errors.MessageNotModifiedError:
                pass  # the bot already has exactly this reaction
            except errors.ReactionInvalidError:
                return "invalid"
            except (ConnectionError, OSError, asyncio.TimeoutError) as e:
                print(f"copy: MTProto {type(e).__name__} — Bot API fallback")
                return await asyncio.to_thread(self.copy_botapi, chat_id, bot_msg, doc_id)
            return "ok"

        def copy_botapi(self, chat_id: int, bot_msg: int, doc_id: int) -> str:
            body = json.dumps({"chat_id": chat_id, "message_id": bot_msg, "reaction": [
                {"type": "custom_emoji", "custom_emoji_id": str(doc_id)}]}).encode()
            req = urllib.request.Request(
                f"https://api.telegram.org/bot{self.token}/setMessageReaction",
                data=body, headers={"Content-Type": "application/json"})
            try:
                with urllib.request.urlopen(req, timeout=10) as r:
                    res = json.load(r)
            except urllib.error.HTTPError as e:
                res = json.load(e)
            if res.get("ok"):
                return "ok"
            if "REACTION_INVALID" in str(res.get("description")):
                return "invalid"
            raise HelperError(f"Bot API: {res.get('description')}")

        async def unseed(self, peer, msg_id: int, mine: list) -> None:
            for delay in (0, 0.3, 1.0):
                await asyncio.sleep(delay)
                try:
                    await self.tg(functions.messages.SendReactionRequest(
                        peer=peer, msg_id=msg_id, big=False, add_to_recent=False,
                        reaction=mine))
                    return
                except errors.MessageNotModifiedError:
                    return
                except Exception as e:  # noqa: BLE001
                    print(f"unseed retry: {type(e).__name__}: {e}")
            raise HelperError("could not remove the operator's seed reaction")

        async def seeded_copy(self, peer, msg, mine: list, chat_id: int, bpeer, bot_msg: int,
                              doc_id: int, copy_delay, timing: dict) -> str:
            """Safe path: seed → copy → unseed on the copy's ack. Returns the copy's
            final 'ok'/'invalid'."""
            custom = types.ReactionCustomEmoji(document_id=doc_id)
            t = time.perf_counter()
            seed = asyncio.ensure_future(self.tg(functions.messages.SendReactionRequest(
                peer=peer, msg_id=msg.id, big=False, add_to_recent=False,
                reaction=(mine + [custom])[-PREMIUM_MAX:])))
            first = None
            if copy_delay is not None:
                await asyncio.sleep(copy_delay / 1000)
                first = asyncio.ensure_future(self.copy(chat_id, bpeer, bot_msg, doc_id))
            try:
                await seed
            except Exception:
                if first is not None:
                    await asyncio.gather(first, return_exceptions=True)
                raise
            seeded_at = time.perf_counter()
            timing["seed"] = ms_since(t)
            try:
                result = await first if first is not None else await self.copy(
                    chat_id, bpeer, bot_msg, doc_id)
                tries = 1
                for pause in INVALID_RETRY_S:
                    if result != "invalid":
                        break
                    await asyncio.sleep(pause)
                    result = await self.copy(chat_id, bpeer, bot_msg, doc_id)
                    tries += 1
                timing["copy"] = ms_since(t)
                timing["copy_tries"] = tries
                return result
            finally:
                await self.unseed(peer, msg.id, mine)
                # seed ack → unseed ack: the operator's copy is visible ~this long.
                timing["window"] = ms_since(seeded_at)

        async def fast_copy(self, peer, msg, mine: list, bpeer, bot_msg: int, doc_id: int,
                            copy_at: float, unseed_at: float, timing: dict) -> str:
            """Fast path: seed, then the copy at copy_at ms and the unseed at
            unseed_at ms — both blind, no ack awaited. The unseed rides
            invokeAfterMsg(seed), so the server never runs it before the seed, and
            it is timed to land after the copy is DONE server-side: an unseed that
            overlaps the copy can lose either write (lab 2026-09-27, docs/vps.md).
            Returns the copy's 'ok'/'invalid'/'error'; the caller verifies."""
            custom = types.ReactionCustomEmoji(document_id=doc_id)
            react = functions.messages.SendReactionRequest
            done: dict[str, float] = {}
            skew = self.skew_ms()
            timing["skew"] = round(skew, 1)
            t = time.perf_counter()
            seed = self.raw(self.tg, react(peer=peer, msg_id=msg.id, big=False, add_to_recent=False,
                                           reaction=(mine + [custom])[-PREMIUM_MAX:]), None, done, "seed")
            await flush()
            await until(t + max(0.0, copy_at - skew) / 1000)
            try:
                copy = self.raw(self.bot, react(peer=bpeer, msg_id=bot_msg, reaction=[custom]),
                                None, done, "copy")
            except ConnectionError:
                copy = None              # still unseed promptly; the caller falls back
            await flush()
            await until(t + unseed_at / 1000)
            limit = time.perf_counter() + 0.5
            while seed.msg_id is None and time.perf_counter() < limit:
                await asyncio.sleep(0)   # the chain needs the seed's msg_id (set when packed)
            if seed.msg_id is None:      # sender stalled: never send an unchained unseed early
                await self.outcome(seed)
            unseed = self.raw(self.tg, react(peer=peer, msg_id=msg.id, big=False,
                                             add_to_recent=False, reaction=mine), seed, done, "unseed")
            await flush()
            await until(t + (unseed_at + FAST_INSURE_MS) / 1000)
            insure = self.raw(self.tg, react(peer=peer, msg_id=msg.id, big=False,
                                             add_to_recent=False, reaction=mine), unseed, done, "insure")
            await flush()
            (_, se), (_, ue), (_, ie) = await asyncio.gather(
                self.outcome(seed), self.outcome(unseed), self.outcome(insure))
            if ie is None:
                timing["insured"] = 1    # it changed something: a resurrected seed was cleared
            ce = (await self.outcome(copy))[1] if copy is not None else ConnectionError("bot offline")
            if "seed" in done:
                timing["seed"] = round((done["seed"] - t) * 1000)
            if "copy" in done:
                timing["copy"] = round((done["copy"] - t) * 1000)
            if "seed" in done and "unseed" in done:
                timing["window"] = round((done["unseed"] - done["seed"]) * 1000)
            s_id, u_id = self.srv_ids.get(seed.msg_id), self.srv_ids.get(unseed.msg_id)
            if s_id and u_id:
                # Telegram's own clock: seed result → unseed result.
                timing["window_srv"] = round((u_id - s_id) / 2**32 * 1000, 1)
            if se is not None:
                if not isinstance(se, errors.RPCError):     # timed out: it may still land
                    await self.unseed(peer, msg.id, mine)
                raise HelperError(f"seed failed: {type(se).__name__}: {se}")
            if ue is not None and not isinstance(ue, errors.MessageNotModifiedError):
                print(f"fast unseed: {type(ue).__name__}: {ue} — plain retry")
                await self.unseed(peer, msg.id, mine)
            if ce is None or isinstance(ce, errors.MessageNotModifiedError):
                return "ok"
            if isinstance(ce, errors.ReactionInvalidError):
                return "invalid"
            print(f"fast copy: {type(ce).__name__}: {ce}")
            return "error"

        async def clear_bot(self, bpeer, bot_msg: int) -> None:
            """Drop the bot's reaction row: re-adding an identical reaction is a
            server no-op, so a lost write is repaired by clear + redo."""
            try:
                await self.bot(functions.messages.SendReactionRequest(
                    peer=bpeer, msg_id=bot_msg, reaction=[]))
            except errors.MessageNotModifiedError:
                pass

        async def react(self, req: dict) -> dict:
            now = time.monotonic()
            while self.recent and now - self.recent[0] > 60:
                self.recent.popleft()
            if len(self.recent) >= RATE_MAX:
                raise HelperError(f"rate guard: {RATE_MAX} custom reactions in the last minute")
            self.recent.append(now)

            chat_id, bot_msg = int(req["chat_id"]), int(req["message_id"])
            doc_id = int(req["custom_emoji_id"])
            strategy = req.get("strategy", STRATEGY)
            timing: dict[str, object] = {}
            t0 = t = time.perf_counter()
            (peer, msg), bpeer = await asyncio.gather(
                self.locate(chat_id, bot_msg, req.get("date"), req.get("text")),
                self.bot_peer_for(chat_id))
            timing["locate"] = ms_since(t)

            results = msg.reactions.results if msg.reactions else []
            present = any(isinstance(r.reaction, types.ReactionCustomEmoji)
                          and r.reaction.document_id == doc_id for r in results)
            mine = own_reactions(msg)
            if present:
                result = await self.copy(chat_id, bpeer, bot_msg, doc_id)
            else:
                result = None
                if strategy == "fast" and self.raw_ok:
                    schedule = list(FAST_SCHEDULE)
                    if "copy_at_ms" in req or "unseed_at_ms" in req:
                        schedule[0] = (float(req.get("copy_at_ms", schedule[0][0])),
                                       float(req.get("unseed_at_ms", schedule[0][1])))
                    attempts = timing["fast"] = []
                    for copy_at, unseed_at in schedule:
                        step: dict[str, object] = {}
                        attempts.append(step)
                        try:
                            result = await self.fast_copy(peer, msg, mine, bpeer, bot_msg, doc_id,
                                                          copy_at, unseed_at, step)
                        except (ConnectionError, OSError) as e:
                            print(f"fast path: {type(e).__name__}: {e} — safe path")
                            result = None
                            break
                        step["copy_result"] = result
                        if result != "invalid":
                            break
                        # Clean miss: the copy beat a slow seed and the chained unseed
                        # already cleared it — nothing to repair, just copy later.
                    if result is not None:
                        t = time.perf_counter()
                        after = await self.fetch(peer, chat_id, msg.id)
                        timing["verify"] = ms_since(t)
                        if [rkey(r) for r in own_reactions(after)] != [rkey(r) for r in mine]:
                            print("verify: a seed reaction survived the fast path — unseeding again")
                            await self.unseed(peer, msg.id, mine)
                            timing["reunseed"] = 1
                        if result == "ok" and not others_have(after, doc_id):
                            # Reads can lag a fresh write by 100 ms+: confirm before
                            # repairing, or a good reaction gets cleared and redone.
                            await asyncio.sleep(LOST_RECHECK_S)
                            after = await self.fetch(peer, chat_id, msg.id)
                            timing["recheck"] = 1
                            if not others_have(after, doc_id):
                                result = "lost"      # acked, yet not on the message
                                print(f"verify: bot reaction missing after recheck — "
                                      f"{after.reactions.to_dict() if after.reactions else None}")
                                await self.clear_bot(bpeer, bot_msg)
                        if result == "ok":
                            timing["path"] = "fast"
                            timing["window_srv"] = attempts[-1].get("window_srv")
                        else:
                            timing["fallback"] = result
                            result = None
                if result is None:
                    timing["path"] = "safe" if "fast" not in timing else "fast+safe"
                    result = await self.seeded_copy(peer, msg, mine, chat_id, bpeer, bot_msg, doc_id,
                                                    req.get("copy_delay_ms", COPY_DELAY_MS), timing)
                    # Verify: no seed reaction may survive (an unseed can overtake its seed).
                    t = time.perf_counter()
                    if [rkey(r) for r in own_reactions(await self.fetch(peer, chat_id, msg.id))] \
                            != [rkey(r) for r in mine]:
                        print("verify: a seed reaction survived — unseeding again")
                        await self.unseed(peer, msg.id, mine)
                        timing["reunseed"] = 1
                    timing["verify"] = ms_since(t)
            timing["total"] = ms_since(t0)
            if result != "ok":
                raise HelperError(f"bot reaction failed: {result} {timing}")
            return {"ok": True, "ms": timing}

        async def handle(self, reader, writer) -> None:
            try:
                line = await asyncio.wait_for(reader.readline(), timeout=5)
                req = json.loads(line or b"{}")
                if req.get("op") == "ping":
                    resp = {"ok": True}
                elif req.get("op") == "react":
                    async with self.lock:
                        try:
                            resp = await self.react(req)
                        except HelperError as e:
                            resp = {"ok": False, "error": str(e)}
                        except Exception as e:  # noqa: BLE001 — surface, keep serving
                            resp = {"ok": False, "error": f"{type(e).__name__}: {e}"}
                    print(f"react chat={req.get('chat_id')} msg={req.get('message_id')} "
                          f"emoji={req.get('custom_emoji_id')} -> {json.dumps(resp)}")
                else:
                    resp = {"ok": False, "error": "unknown op"}
                writer.write(json.dumps(resp).encode() + b"\n")
                await writer.drain()
            except Exception as e:  # noqa: BLE001 — a bad client never kills the helper
                print(f"handle: {type(e).__name__}: {e}")
            finally:
                writer.close()

    async def main() -> None:
        helper = Helper()
        await helper.start()
        if os.path.exists(SOCK):
            os.unlink(SOCK)
        server = await asyncio.start_unix_server(helper.handle, path=SOCK)
        os.chmod(SOCK, 0o600)
        asyncio.create_task(helper.keepalive())
        async with server:
            await server.serve_forever()

    asyncio.run(main())
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("serve")
    sub.add_parser("ping")
    r = sub.add_parser("react")
    r.add_argument("--chat", type=int, required=True, help="bot-side chat id")
    r.add_argument("--msg", type=int, required=True, help="bot-side message id")
    r.add_argument("--emoji-id", type=int, required=True, help="custom emoji document id")
    r.add_argument("--ts", help="message date (ISO UTC or unix) — needed outside supergroups")
    r.add_argument("--text", help="message text, tiebreak for same-second messages")
    r.add_argument("--strategy", choices=["fast", "safe"], help="override STRATEGY")
    r.add_argument("--copy-at-ms", type=float, help="fast: first attempt's copy time (FAST_SCHEDULE[0][0])")
    r.add_argument("--unseed-at-ms", type=float, help="fast: first attempt's unseed time (FAST_SCHEDULE[0][1])")
    r.add_argument("--copy-delay-ms", type=int, help="safe: override COPY_DELAY_MS")
    args = ap.parse_args()
    return {"serve": serve, "ping": client_ping, "react": client_react}[args.cmd](args)


if __name__ == "__main__":
    sys.exit(main())
