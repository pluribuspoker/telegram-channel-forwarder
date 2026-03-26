"""
Telegram Channel Forwarder
Forwards messages from multiple source channels/topics to destination channels.
Tracks last-seen message ID per mapping in a state file.

Required env vars:
  TELEGRAM_API_ID    - from https://my.telegram.org
  TELEGRAM_API_HASH  - from https://my.telegram.org
  TELEGRAM_SESSION   - Telethon session string
  BOT_TOKEN          - Telegram bot token for sending messages
  MAPPINGS_CONFIG    - JSON array of mapping objects, each with:
                         id                - unique stable slug for state tracking
                         source_channel    - username or numeric ID
                         source_topic_id   - (optional) topic/thread ID
                         dest_channel      - username or numeric ID
                         test_dest_channel - (optional) override used with --test flag
"""

import argparse
import asyncio
import json
import os
import sys
from pathlib import Path

from dotenv import load_dotenv
from telethon import TelegramClient
from telethon.sessions import StringSession

from common import log_group, parse_channel, passes_filter, resolve_dest, send_group

load_dotenv(override=True)

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
API_ID = int(os.environ["TELEGRAM_API_ID"])
API_HASH = os.environ["TELEGRAM_API_HASH"]
SESSION = os.environ["TELEGRAM_SESSION"]
BOT_TOKEN = os.environ["BOT_TOKEN"]
MAPPINGS = json.loads(os.environ["MAPPINGS_CONFIG"])

FIRST_RUN_LIMIT = 50

STATE_FILE = Path(__file__).parent / "state.json"

# ---------------------------------------------------------------------------
# State helpers
# ---------------------------------------------------------------------------
def load_state() -> dict:
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text())
    return {}


def save_state(state: dict):
    STATE_FILE.write_text(json.dumps(state, indent=2))


# ---------------------------------------------------------------------------
# Forward a single mapping
# ---------------------------------------------------------------------------
async def forward_mapping(client, bot, mapping, state, limit, use_test):
    mapping_id = mapping["id"]
    source = parse_channel(mapping["source_channel"])
    topic_id = mapping.get("source_topic_id") or None
    if topic_id:
        topic_id = int(topic_id)

    dest = resolve_dest(mapping, use_test)

    print(f"\n[{mapping_id}]")

    source_entity = await client.get_entity(source)
    dest_entity = await bot.get_entity(dest)
    print(f"  Source : {getattr(source_entity, 'title', source)}")
    print(f"  Dest   : {getattr(dest_entity, 'title', dest)}")

    mapping_state = state.setdefault(mapping_id, {})
    last_id = mapping_state.get("last_message_id", 0)

    messages = []
    async for msg in client.iter_messages(source_entity, limit=limit, min_id=last_id, reply_to=topic_id):
        messages.append(msg)

    messages.reverse()

    if not messages:
        print("  No new messages.")
        return

    print(f"  Found {len(messages)} new message(s)")

    # Group by grouped_id so albums are sent together
    groups = []
    for msg in messages:
        if msg.grouped_id and groups and groups[-1][0].grouped_id == msg.grouped_id:
            groups[-1].append(msg)
        else:
            groups.append([msg])

    forwarded = 0
    for group in groups:
        if not passes_filter(group, mapping):
            log_group(group, sent=False)
            continue
        try:
            sent = await send_group(client, group, dest_entity, sender=bot)
            if not sent:
                continue
            log_group(group, sent=True)
            forwarded += 1
        except Exception as e:
            print(f"  ✗ Failed on message {group[0].id}: {e}", file=sys.stderr)
            break

        mapping_state["last_message_id"] = group[-1].id
        save_state(state)

    print(f"  ─────────────────────────────────────────")
    print(f"  Re-posted {forwarded} / {len(groups)} group(s)")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
async def main(limit, use_test, clear_id):
    state = load_state()

    if clear_id == "all":
        state = {}
        save_state(state)
        print("✓ All state cleared")
    elif clear_id:
        state.pop(clear_id, None)
        save_state(state)
        print(f"✓ State cleared for: {clear_id}")

    client = TelegramClient(StringSession(SESSION), API_ID, API_HASH)
    await client.start()
    print("✓ Connected to Telegram (user)")

    bot = TelegramClient(StringSession(), API_ID, API_HASH)
    await bot.start(bot_token=BOT_TOKEN)
    print("✓ Connected to Telegram (bot)")

    for mapping in MAPPINGS:
        try:
            await forward_mapping(client, bot, mapping, state, limit, use_test)
        except Exception as e:
            print(f"  ✗ Mapping {mapping.get('id')} failed: {e}", file=sys.stderr)

    await client.disconnect()
    await bot.disconnect()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--limit", type=int, default=FIRST_RUN_LIMIT, help=f"Max messages to fetch per mapping per run (default: {FIRST_RUN_LIMIT})")
    parser.add_argument("--test", action="store_true", help="Use test_dest_channel from each mapping instead of dest_channel")
    parser.add_argument("--clear-state", metavar="ID", nargs="?", const="all", help="Clear state for a mapping ID, or 'all' to clear everything")
    args = parser.parse_args()

    asyncio.run(main(args.limit, args.test, args.clear_state))
