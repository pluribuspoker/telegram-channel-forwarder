"""
Telegram Channel Forwarder
Polls a source channel for new messages and re-posts them (text + media)
to a destination channel. Tracks last-seen message ID in a state file.

Required env vars:
  TELEGRAM_API_ID       - from https://my.telegram.org
  TELEGRAM_API_HASH     - from https://my.telegram.org
  TELEGRAM_SESSION      - Telethon session string
  SOURCE_CHANNEL        - source channel username or ID (e.g. "@somechannel" or -1001234567890)
  SOURCE_TOPIC_ID       - (optional) topic/thread ID to forward from
  DEST_CHANNEL          - destination channel username or ID
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
SOURCE_CHANNEL = os.environ["SOURCE_CHANNEL"]
SOURCE_TOPIC_ID = int(os.environ["SOURCE_TOPIC_ID"]) if os.environ.get("SOURCE_TOPIC_ID") else None
DEST_CHANNEL = os.environ["DEST_CHANNEL"]

# How many messages to look back on first run (safety cap)
FIRST_RUN_LIMIT = 3

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
def parse_channel(raw: str):
    raw = raw.strip()
    try:
        return int(raw)
    except ValueError:
        pass
    return raw if raw.startswith("@") else f"@{raw}"


# ---------------------------------------------------------------------------
# Core logic
# ---------------------------------------------------------------------------
async def main(limit: int = FIRST_RUN_LIMIT):
    client = TelegramClient(StringSession(SESSION), API_ID, API_HASH)
    await client.start()
    print("✓ Connected to Telegram")

    source = parse_channel(SOURCE_CHANNEL)
    dest = parse_channel(DEST_CHANNEL)

    source_entity = await client.get_entity(source)
    dest_entity = await client.get_entity(dest)
    print(f"  Source : {getattr(source_entity, 'title', source)}")
    print(f"  Dest   : {getattr(dest_entity, 'title', dest)}")

    state = load_state()
    last_id = state.get("last_message_id", 0)

    messages = []
    async for msg in client.iter_messages(source_entity, limit=limit, min_id=last_id, reply_to=SOURCE_TOPIC_ID):
        messages.append(msg)

    messages.reverse()

    if not messages:
        print("  No new messages.")
        await client.disconnect()
        return

    print(f"  Found {len(messages)} new message(s)")

    # Group messages by grouped_id so albums are sent together
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
                # Album: download all media and send as one message
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

        state["last_message_id"] = group[-1].id
        save_state(state)

    print(f"  Re-posted {forwarded} message(s)")
    await client.disconnect()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--clear-state", action="store_true", help="Clear saved state before running (re-processes last messages as if first run)")
    parser.add_argument("--limit", type=int, default=FIRST_RUN_LIMIT, help=f"Max messages to fetch per run (default: {FIRST_RUN_LIMIT})")
    args = parser.parse_args()

    if args.clear_state and STATE_FILE.exists():
        STATE_FILE.unlink()
        print("✓ State reset")

    asyncio.run(main(args.limit))
