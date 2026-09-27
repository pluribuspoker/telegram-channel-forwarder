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
    (Miami — operator, bot and this box's nearest DC; ~48 ms) instead of the
    Bot API, whose Amsterdam server relays to DC1 (~205 ms). Safe next to the
    plugin's getUpdates poller: receive_updates=False never subscribes this
    session to updates (verified on the watchdog bot, then this one);
  * the copy is fired COPY_DELAY_MS after the seed is sent, not after its ack
    (a copy that beats a slow seed retries at the seed's ack — one continuous
    window, never a second flash);
  * the unseed goes out on the copy's ack, then a verify read re-unseeds if a
    seed reaction survived (seen once when an unseed overtook its seed);
  * both sessions persistent and pinged every KEEPALIVE_S; the unit sets
    MemorySwapMax=0 so an idle helper is never paged out on this box.

Measured 2026-09-26 (DM): window ~60 ms (was ~210-270 ms through the Bot API,
~4 s with a per-call login). The operator granted standing use of their
session (2026-09-26); the bot's MTProto session is ~/.claude-react-bot.session.

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
import socket
import sys
import time

SOCK = "/run/claude-react/react.sock"
BOT_ENV = os.path.expanduser("~/.claude/channels/telegram/.env")
BOT_SESSION = os.path.expanduser("~/.claude-react-bot.session")
KEEPALIVE_S = 25          # MTProto liveness ping on both sessions
RATE_MAX = 12             # custom reactions per rolling minute (runaway guard)
PREMIUM_MAX = 3           # reactions_user_max_premium
COPY_DELAY_MS = 10        # copy this long after the seed is SENT (5 ms raced it; 10 ms: 5/5)
INVALID_RETRY_S = (0, 0.03, 0.06, 0.12, 0.25)  # copy retries once the seed is acked


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
        **({"copy_delay_ms": args.copy_delay_ms} if args.copy_delay_ms is not None else {}),
        **({"unseed_after_ms": args.unseed_after_ms} if args.unseed_after_ms is not None else {}),
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
    from telethon.sessions import StringSession

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
            print(f"ready: operator {self.me_id} (dc{self.tg.session.dc_id}), "
                  f"bot @{bme.username} (dc{self.bot.session.dc_id})")

        async def keepalive(self) -> None:
            while True:
                await asyncio.sleep(KEEPALIVE_S)
                try:
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
                              doc_id: int, copy_delay, unseed_after, timing: dict) -> str:
            """seed → copy → unseed; returns the copy's final 'ok'/'invalid'."""
            custom = types.ReactionCustomEmoji(document_id=doc_id)
            t = time.perf_counter()
            seed = asyncio.ensure_future(self.tg(functions.messages.SendReactionRequest(
                peer=peer, msg_id=msg.id, big=False, add_to_recent=False,
                reaction=(mine + [custom])[-PREMIUM_MAX:])))
            first = None
            if copy_delay is not None:
                await asyncio.sleep(copy_delay / 1000)
                copy_sent = time.perf_counter()
                first = asyncio.ensure_future(self.copy(chat_id, bpeer, bot_msg, doc_id))
            try:
                await seed
            except Exception:
                if first is not None:
                    await asyncio.gather(first, return_exceptions=True)
                raise
            seeded_at = time.perf_counter()
            timing["seed"] = ms_since(t)
            unseeded = False
            try:
                if first is not None and unseed_after is not None:
                    # Experiment: speculative unseed (never before the seed's ack,
                    # or it can overtake the seed). A copy that then comes back
                    # 'invalid' is re-done the safe way by the caller.
                    wait = copy_sent + unseed_after / 1000 - time.perf_counter()
                    if wait > 0:
                        await asyncio.sleep(wait)
                    await self.unseed(peer, msg.id, mine)
                    unseeded = True
                    timing["window"] = ms_since(seeded_at)
                    result = await first
                    timing["copy"] = ms_since(t)
                    return result
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
                if not unseeded:
                    await self.unseed(peer, msg.id, mine)
                    # seed ack → unseed ack: the operator's copy is visible ~this long.
                    timing["window"] = ms_since(seeded_at)

        async def react(self, req: dict) -> dict:
            now = time.monotonic()
            while self.recent and now - self.recent[0] > 60:
                self.recent.popleft()
            if len(self.recent) >= RATE_MAX:
                raise HelperError(f"rate guard: {RATE_MAX} custom reactions in the last minute")
            self.recent.append(now)

            chat_id, bot_msg = int(req["chat_id"]), int(req["message_id"])
            doc_id = int(req["custom_emoji_id"])
            copy_delay = req.get("copy_delay_ms", COPY_DELAY_MS)
            unseed_after = req.get("unseed_after_ms")
            timing: dict[str, int] = {}
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
                result = await self.seeded_copy(peer, msg, mine, chat_id, bpeer, bot_msg,
                                                doc_id, copy_delay, unseed_after, timing)
                if result == "invalid" and unseed_after is not None:
                    timing["fallback"] = 1
                    result = await self.seeded_copy(peer, msg, mine, chat_id, bpeer, bot_msg,
                                                    doc_id, None, None, timing)
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
                raise HelperError(f"bot reaction failed: REACTION_INVALID {timing}")
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
    r.add_argument("--copy-delay-ms", type=int, help="override COPY_DELAY_MS (latency experiments)")
    r.add_argument("--unseed-after-ms", type=int,
                   help="experiment: unseed this long after the copy is sent, without waiting for its ack")
    args = ap.parse_args()
    return {"serve": serve, "ping": client_ping, "react": client_react}[args.cmd](args)


if __name__ == "__main__":
    sys.exit(main())
