#!/usr/bin/env python3
"""
desk_sweep_picks_topic.py — one-off (rerunnable) cleanup of the desk group's
🏈 Picks topic, for the 2026-10-11 bet-cards-only change: the topic keeps
ONLY the live 🔔 bet cards (`moe_desk_state.json` `announced` entries) and
messages humans sent; every other intake-bot message — retired picks cards
the state no longer tracks, loud-era alert replies, bet cards pruned before
grading existed — is deleted.

Enumerates the topic over the operator's Telethon session (bots cannot read
history), deletes via the Bot API as the intake bot (group admin). Dry-run
by default; pass --delete to act.

Usage (on the VPS):
  ~/venv/bin/python scripts/desk_sweep_picks_topic.py            # preview
  ~/venv/bin/python scripts/desk_sweep_picks_topic.py --delete
"""

import argparse
import asyncio
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from dotenv import load_dotenv
from telethon import TelegramClient
from telethon.sessions import StringSession
from telethon.tl.types import MessageService, PeerUser

from moe_desk import BotApi, DeskApiError

load_dotenv()
load_dotenv(".env.local", override=True)


def kept_message_ids(state_path: str, picks_topic: int) -> set[int]:
    """Every message id the desk state still points at in the Picks topic:
    the live bet cards, plus any tracked card that targets the topic (none
    after the migration, but the sweep must never race the sync)."""
    try:
        state = json.loads(open(state_path, encoding="utf-8").read())
    except (OSError, ValueError):
        print(f"cannot read {state_path}", file=sys.stderr)
        raise SystemExit(1)
    keep: set[int] = set()
    for entry in (state.get("announced") or {}).values():
        if isinstance(entry, dict) and entry.get("message_id"):
            keep.add(int(entry["message_id"]))
    for entry in (state.get("cards") or {}).values():
        if not isinstance(entry, dict) or entry.get("topic") != picks_topic:
            continue
        if entry.get("message_id"):
            keep.add(int(entry["message_id"]))
        for message_id in entry.get("obsolete_message_ids") or []:
            keep.add(int(message_id))
    return keep


async def sweep(delete: bool) -> None:
    api_id = int(os.getenv("TELEGRAM_API_ID", "0"))
    api_hash = os.getenv("TELEGRAM_API_HASH", "")
    session = os.getenv("TELEGRAM_SESSION", "")
    bot_token = os.getenv("INTAKE_BOT_TOKEN", "")
    chat_id = int(os.getenv("MOE_DESK_CHAT_ID", "0"))
    picks_topic = int(os.getenv("MOE_DESK_PICKS_TOPIC", "0"))
    if not all((api_id, api_hash, session, bot_token, chat_id, picks_topic)):
        print("missing env (TELEGRAM_API_ID/HASH/SESSION, INTAKE_BOT_TOKEN, "
              "MOE_DESK_CHAT_ID, MOE_DESK_PICKS_TOPIC)", file=sys.stderr)
        raise SystemExit(1)
    bot_user_id = int(bot_token.partition(":")[0])
    keep = kept_message_ids(
        os.getenv("MOE_DESK_STATE_PATH", "moe_desk_state.json"), picks_topic
    )
    print(f"keeping {len(keep)} tracked message(s): {sorted(keep)}")

    candidates: list[tuple[int, str, str]] = []
    async with TelegramClient(StringSession(session), api_id, api_hash) as client:
        async for msg in client.iter_messages(chat_id, reply_to=picks_topic):
            if msg.id == picks_topic or isinstance(msg, MessageService):
                continue  # the topic's root/service messages
            sender = msg.from_id.user_id if isinstance(msg.from_id, PeerUser) else None
            if sender != bot_user_id:
                continue  # humans keep their messages
            if msg.id in keep:
                continue
            head = (msg.raw_text or "").splitlines()[0][:80] if msg.raw_text else "<no text>"
            candidates.append((msg.id, str(msg.date), head))

    if not candidates:
        print("nothing to delete — the topic is already clean")
        return
    for message_id, date, head in sorted(candidates):
        print(f"{'DELETE' if delete else 'would delete'} {message_id} ({date}) {head}")
    if not delete:
        print(f"\n{len(candidates)} message(s); rerun with --delete to remove them")
        return
    api = BotApi(bot_token)
    failed = 0
    for message_id, _, _ in sorted(candidates):
        try:
            if not api.delete(str(chat_id), message_id):
                failed += 1
                print(f"  delete failed for {message_id}", file=sys.stderr)
        except DeskApiError as exc:
            failed += 1
            print(f"  delete failed for {message_id}: {exc}", file=sys.stderr)
    print(f"deleted {len(candidates) - failed}/{len(candidates)}")
    if failed:
        raise SystemExit(1)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--delete", action="store_true",
                        help="actually delete (default: dry-run preview)")
    args = parser.parse_args()
    asyncio.run(sweep(args.delete))
