#!/usr/bin/env python3
"""Custom-emoji (Premium) reactions for @ForwarderClaudeBot, kept warm.

Bots may add a custom reaction only when it is already on the message (or a
channel's admins whitelisted it; groups and DMs have no such setting — tested
2026-09-26). So a reaction is three steps:

  1. seed   — the operator's Premium session adds the custom reaction,
  2. copy   — the bot adds the same one (Bot API setMessageReaction),
  3. unseed — the operator's session restores its own previous reactions.

Only the bot's reaction remains. The operator's copy is visible for the seed →
unseed window, so everything on that path is pre-connected: one persistent
MTProto connection (receive_updates=False — no update traffic), one keep-alive
HTTPS connection to the Bot API, both kept warm by a keepalive loop; the unit
sets MemorySwapMax=0 so an idle helper is never paged out on this box. The
operator granted standing use of their session (2026-09-26).

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
KEEPALIVE_S = 25          # Bot API idle keep-alive + MTProto liveness probe
RATE_MAX = 12             # custom reactions per rolling minute (runaway guard)
PREMIUM_MAX = 3           # reactions_user_max_premium
INVALID_RETRY_S = (0.05, 0.1, 0.2, 0.4)  # seed → bot visibility lag
BOT_DELAY_MS = 0          # see react(); 0 won 3/3 vs sequential (window ~210 vs ~257 ms)


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
        **({"bot_delay_ms": args.bot_delay_ms} if args.bot_delay_ms is not None else {}),
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

    import httpx
    from dotenv import load_dotenv
    from telethon import TelegramClient, functions, types
    from telethon.sessions import StringSession

    # systemd injects .env/.env.local; dotenv covers manual runs (never overrides).
    load_dotenv("/home/forwarder/app/.env.local")
    load_dotenv("/home/forwarder/app/.env")

    def bot_token() -> str:
        for line in open(BOT_ENV, encoding="utf-8"):
            if line.startswith("TELEGRAM_BOT_TOKEN="):
                return line.split("=", 1)[1].strip()
        raise SystemExit("no TELEGRAM_BOT_TOKEN in " + BOT_ENV)

    def ms_since(t: float) -> int:
        return round((time.perf_counter() - t) * 1000)

    class Helper:
        def __init__(self) -> None:
            self.tg = TelegramClient(
                StringSession(os.environ["TELEGRAM_SESSION"]),
                int(os.environ["TELEGRAM_API_ID"]),
                os.environ["TELEGRAM_API_HASH"],
                receive_updates=False,
            )
            self.token = bot_token()
            self.http = httpx.AsyncClient(
                timeout=10,
                limits=httpx.Limits(max_connections=2, max_keepalive_connections=2,
                                    keepalive_expiry=KEEPALIVE_S * 4),
            )
            self.lock = asyncio.Lock()
            self.recent: collections.deque[float] = collections.deque()
            self.peers: dict[int, object] = {}

        async def bot(self, method: str, payload: dict) -> dict:
            url = f"https://api.telegram.org/bot{self.token}/{method}"
            try:
                r = await self.http.post(url, json=payload)
            except httpx.TransportError:
                # A keep-alive connection the server already closed — one retry.
                r = await self.http.post(url, json=payload)
            return r.json()

        async def start(self) -> None:
            await self.tg.connect()
            if not await self.tg.is_user_authorized():
                raise SystemExit("operator session is not authorized")
            me = await self.tg.get_me()
            self.me_id = me.id
            if not me.premium:
                print("warning: operator account is not Premium — seeding custom reactions will fail")
            info = await self.bot("getMe", {})
            if not info.get("ok"):
                raise SystemExit(f"bot getMe failed: {info}")
            self.bot_peer = await self.tg.get_input_entity(info["result"]["username"])
            print(f"ready: operator {self.me_id}, bot @{info['result']['username']}")

        async def keepalive(self) -> None:
            while True:
                await asyncio.sleep(KEEPALIVE_S)
                try:
                    await self.bot("getMe", {})
                    await self.tg(functions.help.GetNearestDcRequest())
                except Exception as e:  # noqa: BLE001 — logged, next tick retries
                    print(f"keepalive: {type(e).__name__}: {e}")

        async def peer_for(self, chat_id: int):
            if chat_id not in self.peers:
                self.peers[chat_id] = await self.tg.get_input_entity(chat_id)
            return self.peers[chat_id]

        async def locate(self, chat_id: int, bot_msg: int, date, text):
            """(peer, raw operator-side Message) for the bot-side message id."""
            if str(chat_id).startswith("-100"):
                peer = await self.peer_for(chat_id)
                res = await self.tg(functions.channels.GetMessagesRequest(
                    channel=peer, id=[types.InputMessageID(bot_msg)]))
                msgs = [m for m in res.messages if isinstance(m, types.Message)]
                if not msgs:
                    raise HelperError(f"message {bot_msg} not found")
                return peer, msgs[0]
            if chat_id > 0:
                if chat_id != self.me_id:
                    raise HelperError("private chat is not the operator's chat with the bot")
                peer, want_out = self.bot_peer, True
            else:
                peer, want_out = await self.peer_for(chat_id), None
            if date is None:
                raise HelperError("date required to map a per-account message id")
            hist = await self.tg(functions.messages.GetHistoryRequest(
                peer=peer, offset_id=0, offset_date=None, add_offset=0,
                limit=20, max_id=0, min_id=0, hash=0))
            cands = [m for m in hist.messages
                     if isinstance(m, types.Message)
                     and int(m.date.timestamp()) == int(date)
                     and (want_out is None or m.out == want_out)]
            if len(cands) > 1 and text is not None:
                cands = [m for m in cands if (m.message or "") == text] or cands
            if len(cands) != 1:
                raise HelperError(f"cannot map message {bot_msg} ({len(cands)} candidates at {date})")
            return peer, cands[0]

        async def react(self, req: dict) -> dict:
            now = time.monotonic()
            while self.recent and now - self.recent[0] > 60:
                self.recent.popleft()
            if len(self.recent) >= RATE_MAX:
                raise HelperError(f"rate guard: {RATE_MAX} custom reactions in the last minute")
            self.recent.append(now)

            chat_id, bot_msg = int(req["chat_id"]), int(req["message_id"])
            doc_id = int(req["custom_emoji_id"])
            timing: dict[str, int] = {}
            t0 = t = time.perf_counter()
            peer, msg = await self.locate(chat_id, bot_msg, req.get("date"), req.get("text"))
            timing["locate"] = ms_since(t)

            results = msg.reactions.results if msg.reactions else []
            present = any(isinstance(r.reaction, types.ReactionCustomEmoji)
                          and r.reaction.document_id == doc_id for r in results)
            mine = [r.reaction for r in sorted(
                (r for r in results if r.chosen_order is not None),
                key=lambda r: r.chosen_order)]
            custom = types.ReactionCustomEmoji(document_id=doc_id)
            payload = {"chat_id": chat_id, "message_id": bot_msg,
                       "reaction": [{"type": "custom_emoji", "custom_emoji_id": str(doc_id)}]}

            if present:
                t = time.perf_counter()
                res = await self.bot("setMessageReaction", payload)
                timing["bot"] = ms_since(t)
            else:
                # bot_delay_ms: fire the bot call that long after the seed is
                # SENT instead of after its ack — overlaps the seed round trip
                # with the (slower) Bot API call; a copy that lands before the
                # seed is visible to it retries immediately. None = sequential.
                delay = req.get("bot_delay_ms", BOT_DELAY_MS)
                t = time.perf_counter()
                seed = asyncio.ensure_future(self.tg(functions.messages.SendReactionRequest(
                    peer=peer, msg_id=msg.id, big=False, add_to_recent=False,
                    reaction=(mine + [custom])[-PREMIUM_MAX:])))
                early = None
                if delay is not None:
                    await asyncio.sleep(delay / 1000)
                    early = asyncio.ensure_future(self.bot("setMessageReaction", payload))
                try:
                    await seed
                except Exception:
                    if early:
                        await asyncio.gather(early, return_exceptions=True)
                    raise
                seeded_at = time.perf_counter()
                timing["seed"] = ms_since(t)
                try:
                    res = await early if early else await self.bot("setMessageReaction", payload)
                    tries = 1
                    for pause in ((0,) if early else ()) + INVALID_RETRY_S:
                        if res.get("ok") or "REACTION_INVALID" not in str(res.get("description")):
                            break
                        await asyncio.sleep(pause)
                        res = await self.bot("setMessageReaction", payload)
                        tries += 1
                    timing["bot"] = ms_since(t)
                    timing["bot_tries"] = tries
                finally:
                    t = time.perf_counter()
                    await self.unseed(peer, msg.id, mine)
                    timing["unseed"] = ms_since(t)
                    # seed ack → unseed ack: the operator's copy is visible ~this long.
                    timing["window"] = ms_since(seeded_at)
            timing["total"] = ms_since(t0)
            if not res.get("ok"):
                raise HelperError(f"bot reaction failed: {res.get('description')} {timing}")
            return {"ok": True, "ms": timing}

        async def unseed(self, peer, msg_id: int, mine: list) -> None:
            for delay in (0, 0.3, 1.0):
                await asyncio.sleep(delay)
                try:
                    await self.tg(functions.messages.SendReactionRequest(
                        peer=peer, msg_id=msg_id, big=False, add_to_recent=False,
                        reaction=mine))
                    return
                except Exception as e:  # noqa: BLE001
                    print(f"unseed retry: {type(e).__name__}: {e}")
            raise HelperError("could not remove the operator's seed reaction")

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
    r.add_argument("--bot-delay-ms", type=int, help="override BOT_DELAY_MS (latency experiments)")
    args = ap.parse_args()
    return {"serve": serve, "ping": client_ping, "react": client_react}[args.cmd](args)


if __name__ == "__main__":
    sys.exit(main())
