"""
Telegram Channel Forwarder
Forwards messages from multiple source channels/topics to destination channels.
Tracks last-seen message ID per mapping in a state file.

Required env vars:
  TELEGRAM_API_ID    - from https://my.telegram.org
  TELEGRAM_API_HASH  - from https://my.telegram.org
  TELEGRAM_SESSION   - Telethon session string
  MAPPINGS_CONFIG    - JSON array of mapping objects, each with:
                         id                - unique stable slug for state tracking
                         source_channel    - username or numeric ID
                         source_topic_id   - (optional) topic/thread ID
                         dest_channel      - username or numeric ID
                         test_dest_channel - (optional) override used with --test flag
"""

import argparse
import asyncio
import io
import json
import os
import sys
from pathlib import Path

from dotenv import load_dotenv
from telethon import TelegramClient
from telethon.sessions import StringSession
from telethon.tl.types import MessageMediaPhoto, MessageMediaDocument

load_dotenv()

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
API_ID = int(os.environ["TELEGRAM_API_ID"])
API_HASH = os.environ["TELEGRAM_API_HASH"]
SESSION = os.environ["TELEGRAM_SESSION"]
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
# Resolve a channel reference that might be a username or numeric ID
# ---------------------------------------------------------------------------
def parse_channel(raw):
    raw = str(raw).strip()
    try:
        return int(raw)
    except ValueError:
        pass
    return raw if raw.startswith("@") else f"@{raw}"


# ---------------------------------------------------------------------------
# Forward a single mapping
# ---------------------------------------------------------------------------
async def forward_mapping(client, mapping, state, limit, use_test):
    mapping_id = mapping["id"]
    source = parse_channel(mapping["source_channel"])
    topic_id = mapping.get("source_topic_id") or None
    if topic_id:
        topic_id = int(topic_id)

    dest_raw = mapping.get("test_dest_channel") if use_test else None
    if not dest_raw:
        dest_raw = mapping["dest_channel"]
    dest = parse_channel(dest_raw)

    print(f"\n[{mapping_id}]")

    source_entity = await client.get_entity(source)
    dest_entity = await client.get_entity(dest)
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
        try:
            if len(group) > 1:
                files = []
                caption = ""
                caption_entities = None
                for m in group:
                    data = await client.download_media(m.media, file=bytes)
                    buf = io.BytesIO(data)
                    buf.name = "photo.jpg"
                    files.append(buf)
                    if m.text:
                        caption = m.text
                        caption_entities = m.entities
                await client.send_file(dest_entity, files, caption=caption, formatting_entities=caption_entities)
            else:
                msg = group[0]
                if isinstance(msg.media, MessageMediaPhoto):
                    photo = await client.download_media(msg.media, file=bytes)
                    buf = io.BytesIO(photo)
                    buf.name = "photo.jpg"
                    await client.send_file(dest_entity, buf, caption=msg.text or "", formatting_entities=msg.entities)

                elif isinstance(msg.media, MessageMediaDocument):
                    doc = await client.download_media(msg.media, file=bytes)
                    await client.send_file(dest_entity, doc, caption=msg.text or "", formatting_entities=msg.entities)

                elif msg.text:
                    await client.send_message(dest_entity, msg.text, formatting_entities=msg.entities)

                else:
                    print(f"  Skipped message {msg.id} (unsupported type)")
                    continue

            forwarded += 1

        except Exception as e:
            print(f"  ✗ Failed on message {group[0].id}: {e}", file=sys.stderr)
            break

        mapping_state["last_message_id"] = group[-1].id
        save_state(state)

    print(f"  Re-posted {forwarded} message(s)")


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
    print("✓ Connected to Telegram")

    for mapping in MAPPINGS:
        try:
            await forward_mapping(client, mapping, state, limit, use_test)
        except Exception as e:
            print(f"  ✗ Mapping {mapping.get('id')} failed: {e}", file=sys.stderr)

    await client.disconnect()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--limit", type=int, default=FIRST_RUN_LIMIT, help=f"Max messages to fetch per mapping per run (default: {FIRST_RUN_LIMIT})")
    parser.add_argument("--test", action="store_true", help="Use test_dest_channel from each mapping instead of dest_channel")
    parser.add_argument("--clear-state", metavar="ID", nargs="?", const="all", help="Clear state for a mapping ID, or 'all' to clear everything")
    args = parser.parse_args()

    asyncio.run(main(args.limit, args.test, args.clear_state))
