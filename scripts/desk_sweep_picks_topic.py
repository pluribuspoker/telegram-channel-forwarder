#!/usr/bin/env python3
"""
desk_sweep_picks_topic.py — one-off (rerunnable) cleanup of the desk group's
🏈 Picks topic, for the 2026-10-11 bet-cards-only change: the topic keeps
ONLY the live 🔔 bet cards (`moe_desk_state.json` `announced` entries) and
messages humans sent; every other intake-bot message — retired picks cards
(including tracked ones the bot cannot delete itself), loud-era alert
replies, bet cards pruned before grading existed — is deleted.

Runs entirely over the operator's Telethon session: bots cannot read
history, and the Bot API refuses to delete the bot's own messages once they
are 48 hours old ("message can't be deleted") even with can_delete_messages
— the group owner's session has no such wall. After the sweep, the sync's
own retry delete of a tracked picks card answers "not found", which
`BotApi.delete` treats as done, so the state entry unsticks itself.

Dry-run by default; pass --delete to act.

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

load_dotenv()
load_dotenv(".env.local", override=True)


def live_betcard_ids(state_path: str) -> set[int]:
    """The message ids of the live 🔔 bet cards — the only bot messages the
    Picks topic keeps. Tracked `picks:*` cards are deletion targets: the
    sync already wants them gone and only fails on Telegram's 48h wall."""
    try:
        state = json.loads(open(state_path, encoding="utf-8").read())
    except (OSError, ValueError):
        print(f"cannot read {state_path}", file=sys.stderr)
        raise SystemExit(1)
    return {
        int(entry["message_id"])
        for entry in (state.get("announced") or {}).values()
        if isinstance(entry, dict) and entry.get("message_id")
    }


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
    state_path = os.getenv("MOE_DESK_STATE_PATH", "moe_desk_state.json")

    async with TelegramClient(StringSession(session), api_id, api_hash) as client:
        candidates: list[tuple[int, str, str]] = []
        async for msg in client.iter_messages(chat_id, reply_to=picks_topic):
            if msg.id == picks_topic or isinstance(msg, MessageService):
                continue  # the topic's root/service messages
            sender = msg.from_id.user_id if isinstance(msg.from_id, PeerUser) else None
            if sender != bot_user_id:
                continue  # humans keep their messages
            head = (msg.raw_text or "").splitlines()[0][:80] if msg.raw_text else "<no text>"
            candidates.append((msg.id, str(msg.date), head))
        # Re-read the keep set AFTER enumerating so a bet card the sync
        # posted mid-sweep is never swept with the clutter.
        keep = live_betcard_ids(state_path)
        print(f"keeping {len(keep)} live bet card(s): {sorted(keep)}")
        candidates = [item for item in candidates if item[0] not in keep]
        if not candidates:
            print("nothing to delete — the topic is already clean")
            return
        for message_id, date, head in sorted(candidates):
            print(f"{'DELETE' if delete else 'would delete'} {message_id} ({date}) {head}")
        if not delete:
            print(f"\n{len(candidates)} message(s); rerun with --delete to remove them")
            return
        await client.delete_messages(chat_id, [item[0] for item in candidates])
        print(f"deleted {len(candidates)}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--delete", action="store_true",
                        help="actually delete (default: dry-run preview)")
    args = parser.parse_args()
    asyncio.run(sweep(args.delete))
