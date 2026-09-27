#!/usr/bin/env python3
"""Reaction-window lab for claude-react-helper (docs/vps.md "Custom-emoji reactions").

Replays the helper's seed → copy → unseed schedules against Telegram and times
every step on Telegram's own clock: rpc_result / pong msg_ids are
unixtime * 2^32 with sub-ms resolution, and a per-connection ping gives the
local→server offset, so arrivals and results of the operator's and the bot's
requests land on one timeline.

Targets are OLD messages in the operator↔bot DM, which is silent: the bot's
reactions don't notify (the operator's reaction notifications are
contacts-only and the bot isn't a contact); `cleanup` clears what the lab
left and marks the chat's reactions read.

  run --sched fast [--copy-at LO HI] [--gap LO HI]   blind copy + chained unseed
  run --sched safe                                   unseed on the copy's ack
  report FILE...                                     outcomes by gap, windows
  cleanup                                            clear lab reactions, mark read

Terms (ms, server clock): x = copy arrival - seed result; z = unseed arrival -
copy arrival; W = unseed result - seed result (the operator's seed window).

RATE: ~400 operator sendReaction calls in ~35 min drew a 7-minute FLOOD_WAIT
(2026-09-27) — and a flood wait blocks the live helper too. Defaults keep to
~1 cycle / 20 s at 2 operator calls each; the run stops at the first flood.
Run with the venv python on the VPS (needs .env.local's TELEGRAM_SESSION and
the helper's bot session).
"""
import argparse
import asyncio
import collections
import json
import os
import random
import statistics
import sys
import time
from datetime import timedelta

from dotenv import load_dotenv
from telethon import TelegramClient, errors, functions, types
from telethon.network.connection.connection import Connection
from telethon.network.requeststate import RequestState
from telethon.sessions import StringSession
from telethon.tl.core.rpcresult import RpcResult
from telethon.tl.types import Pong

load_dotenv("/home/forwarder/app/.env.local")
load_dotenv("/home/forwarder/app/.env")
BOT_SESSION = os.path.expanduser("~/.claude-react-bot.session")
OPERATOR = 5911202683    # the operator's user id = the bot-side chat id of the DM
EMOJIS = [5368562433981947135, 5370955972011366737, 5393414085419212814]
# Operator-authored DM messages from 2026-09-20/21 (bot-side ids): production-like
# targets — the seed goes on the operator's own message, the copy notifies nobody.
DEFAULT_MSGS = [1001, 1003, 1005, 1007, 1010, 1014, 1017, 1019, 1021, 1023]
now = time.perf_counter

# ------------------------------------------------------------------ timing probes

_BYDATA = {}                 # id(encrypted packet) -> (packet, probe, msg_ids)
_orig_conn_send = Connection._send


def _conn_send(self, data):
    _orig_conn_send(self, data)
    t = now()
    entry = _BYDATA.pop(id(data), None)
    if entry is not None and entry[0] is data:
        for mid in entry[2]:
            entry[1].sent[mid] = t


Connection._send = _conn_send


class Probe:
    """Socket-write time per msg_id and (local time, server msg_id) per result.
    Must be attached BEFORE connect() so every packed batch is tagged."""

    def __init__(self, client):
        self.client, self.sent, self.recv, self._pending = client, {}, {}, None
        sender = client._sender
        packer, mstate, probe = sender._send_queue, sender._state, self
        orig_get, orig_enc = packer.get, mstate.encrypt_message_data

        async def get():
            batch, data = await orig_get()
            if batch:
                probe._pending = [s.msg_id for s in batch] + list(
                    {s.container_id for s in batch if s.container_id})
            return batch, data

        def enc(data):
            out = orig_enc(data)
            if probe._pending is not None:
                _BYDATA[id(out)] = (out, probe, probe._pending)
                probe._pending = None
            return out
        packer.get, mstate.encrypt_message_data = get, enc

        for ctor, key in ((RpcResult.CONSTRUCTOR_ID, lambda m: m.obj.req_msg_id),
                          (Pong.CONSTRUCTOR_ID, lambda m: m.obj.msg_id)):
            orig = sender._handlers[ctor]

            async def handler(message, orig=orig, key=key):
                probe.recv[key(message)] = (now(), message.msg_id)
                return await orig(message)
            sender._handlers[ctor] = handler

    def send(self, request, after=None):
        st = RequestState(request, after=after)
        self.client._sender._send_queue.append(st)
        return st

    async def result(self, st):
        """{ok, v, sent, recv, smid} — never raises."""
        try:
            val = await asyncio.wait_for(asyncio.shield(st.future), 10)
            ok, v = True, type(val).__name__
        except errors.RPCError as e:
            ok, v = False, type(e).__name__
        except asyncio.TimeoutError:
            ok, v = False, "timeout"
        rt = self.recv.get(st.msg_id, (None, None))
        return {"ok": ok, "v": v, "sent": self.sent.get(st.msg_id), "recv": rt[0], "smid": rt[1]}

    async def calib(self, n=2):
        """Min-RTT ping: {rtt, off} with off = server clock - local clock (s)."""
        best = None
        for _ in range(n):
            r = await self.result(self.send(functions.PingRequest(ping_id=random.getrandbits(63))))
            if r["sent"] is None or r["recv"] is None:
                continue
            rtt = r["recv"] - r["sent"]
            if best is None or rtt < best["rtt"]:
                best = {"rtt": rtt, "off": r["smid"] / 2**32 - (r["sent"] + r["recv"]) / 2}
        return best


async def flush():
    for _ in range(6):
        await asyncio.sleep(0)


async def until(t):
    """Sleep to perf_counter() == t, yielding: a busy spin stalls the send loops."""
    while (d := t - now()) > 0.0015:
        await asyncio.sleep(d - 0.0012)
    while now() < t:
        await asyncio.sleep(0)


# ------------------------------------------------------------------ plumbing

async def connect():
    api_id, api_hash = int(os.environ["TELEGRAM_API_ID"]), os.environ["TELEGRAM_API_HASH"]
    op = TelegramClient(StringSession(os.environ["TELEGRAM_SESSION"]), api_id, api_hash,
                        receive_updates=False)
    bot = TelegramClient(StringSession(open(BOT_SESSION).read().strip()), api_id, api_hash,
                         receive_updates=False)
    P, B = Probe(op), Probe(bot)
    await op.connect()
    await bot.connect()
    if not (await op.is_user_authorized() and await bot.is_user_authorized()):
        raise SystemExit("a session is not authorized")
    return op, bot, P, B


async def targets(op, bot, bot_ids):
    """[(op_peer, op_msg_id, bot_peer, bot_msg_id)]: per-account ids mapped by date+text."""
    op_peer = await op.get_input_entity((await bot.get_me()).username)
    bot_peer = await bot.get_input_entity(OPERATOR)
    res = await bot(functions.messages.GetMessagesRequest(
        id=[types.InputMessageID(i) for i in bot_ids]))
    out = []
    for m in res.messages:
        if not isinstance(m, types.Message):
            continue
        hist = await op(functions.messages.GetHistoryRequest(
            peer=op_peer, offset_id=0, offset_date=m.date + timedelta(seconds=1),
            add_offset=0, limit=10, max_id=0, min_id=0, hash=0))
        c = [h for h in hist.messages if isinstance(h, types.Message) and h.out != m.out
             and h.date == m.date and (h.message or "") == (m.message or "")]
        if len(c) == 1:
            out.append((op_peer, c[0].id, bot_peer, m.id))
    return out


async def state(op, peer, msg_id):
    m = (await op(functions.messages.GetMessagesRequest(id=[types.InputMessageID(msg_id)]))).messages[0]
    mine, others = [], {}
    for r in (m.reactions.results if m.reactions else []):
        key = getattr(r.reaction, "document_id", None) or getattr(r.reaction, "emoticon", None)
        if r.chosen_order is not None:
            mine.append(key)
            if r.count > 1:
                others[key] = r.count - 1
        else:
            others[key] = r.count
    return mine, others


def sreq(peer, msg_id, reaction, user=True):
    if user:
        return functions.messages.SendReactionRequest(peer=peer, msg_id=msg_id, big=False,
                                                      add_to_recent=False, reaction=reaction)
    return functions.messages.SendReactionRequest(peer=peer, msg_id=msg_id, reaction=reaction)


# ------------------------------------------------------------------ run

async def cycle(P, B, tgt, emoji, sched, copy_at, gap):
    op_peer, op_msg, bot_peer, bot_msg = tgt
    custom = [types.ReactionCustomEmoji(document_id=emoji)]
    # production's locate() reads the message right before seeding
    await P.client(functions.messages.GetHistoryRequest(
        peer=op_peer, offset_id=op_msg + 1, offset_date=None, add_offset=0, limit=3,
        max_id=0, min_id=0, hash=0))
    t0 = now()
    seed = P.send(sreq(op_peer, op_msg, custom))
    await flush()
    if sched == "fast":
        await until(t0 + copy_at / 1000)
        copy = B.send(sreq(bot_peer, bot_msg, custom, user=False))
        await flush()
        await until(t0 + (copy_at + gap) / 1000)
        unseed = P.send(sreq(op_peer, op_msg, []), after=seed)
        await flush()
    else:
        await until(t0 + 0.010)
        copy = B.send(sreq(bot_peer, bot_msg, custom, user=False))
        await asyncio.wait([copy.future], timeout=10)
        if copy.future.done() and isinstance(copy.future.exception(), errors.ReactionInvalidError):
            await asyncio.wait([seed.future], timeout=10)
            copy = B.send(sreq(bot_peer, bot_msg, custom, user=False))   # the retry counts
            await asyncio.wait([copy.future], timeout=10)
        unseed = P.send(sreq(op_peer, op_msg, []))
    return t0, {"seed": await P.result(seed), "copy": await B.result(copy),
                "unseed": await P.result(unseed)}


async def run(a):
    op, bot, P, B = await connect()
    tg = await targets(op, bot, a.msgs)
    print(f"{len(tg)} targets mapped", flush=True)
    states = {t[1]: await state(op, t[0], t[1]) for t in tg}
    out = open(a.out, "a")
    for i in range(a.n):
        tgt = tg[i % len(tg)]
        mine, others = states[tgt[1]]
        if mine:                                   # a survivor from a corrupt cycle
            await op(sreq(tgt[0], tgt[1], []))
            mine, others = states[tgt[1]] = await state(op, tgt[0], tgt[1])
        emoji = next(e for e in EMOJIS if e not in set(others) | set(mine))
        copy_at = round(random.uniform(*a.copy_at), 1)
        gap = round(random.uniform(*a.gap), 1)
        cp, cb = await P.calib(), await B.calib()
        t0, res = await cycle(P, B, tgt, emoji, a.sched, copy_at, gap)
        await asyncio.sleep(0.4)                   # reads lag writes; see docs
        mine, others = states[tgt[1]] = await state(op, tgt[0], tgt[1])
        rec = {"i": i, "sched": a.sched, "copy_at": copy_at, "gap": gap, "op_msg": tgt[1],
               "good": not mine and others.get(emoji) == 1,
               "final": {"mine": len(mine), "bot": others.get(emoji, 0)}}
        for k, r in res.items():
            cal = cp if k in ("seed", "unseed") else cb
            rec[k] = {"ok": r["ok"], "v": r["v"],
                      # server clock, ms, relative to t0 on the operator connection's scale
                      "arr": None if r["sent"] is None else round(
                          (r["sent"] + cal["rtt"] / 2 + cal["off"] - cp["off"] - t0) * 1000, 2),
                      "res": None if r["smid"] is None else round(
                          (r["smid"] / 2**32 - cp["off"] - t0) * 1000, 2)}
        print(json.dumps(rec), flush=True)
        out.write(json.dumps(rec) + "\n")
        out.flush()
        if any("Flood" in r["v"] for r in res.values()):
            print("FLOOD_WAIT — stopping", flush=True)
            break
        await asyncio.sleep(a.pause + random.random() * 2)
    await op.disconnect()
    await bot.disconnect()


# ------------------------------------------------------------------ report

def report(paths):
    rows = []
    for p in paths:
        for line in open(p):
            r = json.loads(line)
            s, c, u = r["seed"], r["copy"], r["unseed"]
            if not s["ok"] or None in (s["res"], c["arr"], c["res"], u["arr"], u["res"]):
                continue
            r["x"], r["z"], r["W"] = c["arr"] - s["res"], u["arr"] - c["arr"], u["res"] - s["res"]
            r["out"] = "good" if r["good"] else ("clean-miss" if not c["ok"] else
                                                 f"CORRUPT(mine={r['final']['mine']},bot={r['final']['bot']})")
            rows.append(r)
    if not rows:
        return print("no rows")
    print(collections.Counter(r["out"] for r in rows), "n =", len(rows))
    print("copy-ok cycles by z (unseed arrival - copy arrival):")
    for lo, hi in ((-99, 11), (11, 15), (15, 20), (20, 30), (30, 999)):
        g = [r for r in rows if lo <= r["z"] < hi and r["copy"]["ok"]]
        if g:
            print(f"  [{lo:>4},{hi:>4}) n={len(g):3} {dict(collections.Counter(r['out'] for r in g))}")
    ws = sorted(r["W"] for r in rows if r["out"] == "good")
    if ws:
        print(f"W (good): median {statistics.median(ws):.1f}  p10 {ws[len(ws) // 10]:.1f}  "
              f"p90 {ws[int(len(ws) * .9)]:.1f}  max {ws[-1]:.1f}")
    for k, name in (("seed", "seed"), ("copy", "copy")):
        xs = sorted(r[k]["res"] - r[k]["arr"] for r in rows)
        print(f"{name} server time: median {statistics.median(xs):.1f}  p90 {xs[int(len(xs) * .9)]:.1f}  "
              f"max {xs[-1]:.1f}")


# ------------------------------------------------------------------ cleanup

async def cleanup(a):
    op, bot, P, B = await connect()
    tg = await targets(op, bot, a.msgs)
    for op_peer, op_msg, bot_peer, bot_msg in tg:
        m = (await op(functions.messages.GetMessagesRequest(id=[types.InputMessageID(op_msg)]))).messages[0]
        rs = m.reactions.results if m.reactions else []
        if not rs:
            continue
        print(f"bot msg {bot_msg}: clearing", [(getattr(r.reaction, "document_id", None)
                                                or getattr(r.reaction, "emoticon", None),
                                                r.count, r.chosen_order) for r in rs])
        for client, peer, mid, user in ((bot, bot_peer, bot_msg, False), (op, op_peer, op_msg, True)):
            if user and not any(r.chosen_order is not None for r in rs):
                continue
            try:
                await client(sreq(peer, mid, [], user=user))
            except errors.RPCError as e:
                print("  ", type(e).__name__)
        await asyncio.sleep(1.5)
    if tg:
        await op(functions.messages.ReadReactionsRequest(peer=tg[0][0]))
    await op.disconnect()
    await bot.disconnect()


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run")
    r.add_argument("--sched", choices=["fast", "safe"], required=True)
    r.add_argument("--copy-at", type=float, nargs=2, default=[16, 16], metavar=("LO", "HI"))
    r.add_argument("--gap", type=float, nargs=2, default=[16, 16], metavar=("LO", "HI"),
                   help="fast: unseed this long after the copy (uniform draw)")
    r.add_argument("--n", type=int, default=30)
    r.add_argument("--msgs", type=int, nargs="+", default=DEFAULT_MSGS, help="bot-side ids")
    r.add_argument("--pause", type=float, default=18)
    r.add_argument("--out", required=True)
    p = sub.add_parser("report")
    p.add_argument("files", nargs="+")
    c = sub.add_parser("cleanup")
    c.add_argument("--msgs", type=int, nargs="+", default=DEFAULT_MSGS)
    a = ap.parse_args()
    if a.cmd == "report":
        return report(a.files)
    asyncio.run(run(a) if a.cmd == "run" else cleanup(a))


if __name__ == "__main__":
    sys.exit(main())
