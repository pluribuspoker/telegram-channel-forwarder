"""
Telegram Channel Listener
Real-time event-driven forwarder. Keeps a persistent connection to Telegram
and forwards messages instantly as they arrive.

Required env vars (same as forwarder.py):
  TELEGRAM_API_ID    - from https://my.telegram.org
  TELEGRAM_API_HASH  - from https://my.telegram.org
  TELEGRAM_SESSION   - Telethon session string
  MAPPINGS_CONFIG    - JSON array of mapping objects
"""

import asyncio
import datetime
import json
import logging
import os
import sys
import urllib.request

logging.basicConfig(
    level=logging.WARNING,
    format="%(asctime)s [%(name)s] %(message)s",
    datefmt="%H:%M:%S",
)

from dotenv import load_dotenv
from telethon import TelegramClient, events
from telethon.sessions import StringSession

from common import enrich_caption, log_group, parse_channel, passes_filter, resolve_dest, send_group

load_dotenv(override=True)

API_ID = int(os.environ["TELEGRAM_API_ID"])
API_HASH = os.environ["TELEGRAM_API_HASH"]
SESSION = os.environ["TELEGRAM_SESSION"]
BOT_TOKEN = os.environ["BOT_TOKEN"]
MAPPINGS = json.loads(os.environ["MAPPINGS_CONFIG"])

# How long to wait for album messages to arrive before sending as a group
ALBUM_WAIT = 5.0


async def heartbeat():
    """Ping healthchecks.io every 4 minutes to signal the service is alive."""
    url = os.environ.get("LISTENER_HEALTHCHECK_URL")
    if not url:
        return
    while True:
        try:
            urllib.request.urlopen(url, timeout=10)
        except Exception:
            pass
        print(f" ♡ listening")
        await asyncio.sleep(240)


async def main():
    client = TelegramClient(StringSession(SESSION), API_ID, API_HASH)
    await client.start()
    print("✓ Connected to Telegram (user)")

    bot = TelegramClient(StringSession(), API_ID, API_HASH)
    await bot.start(bot_token=BOT_TOKEN)
    print("✓ Connected to Telegram (bot)")

    # album buffer: grouped_id -> (list of messages, flush task)
    album_buffer: dict = {}

    use_test = "--test" in sys.argv
    print(f"  Mode: {'TEST' if use_test else 'REAL'}")

    registered = set()  # track (source_id, dest_id) pairs to avoid duplicate handlers

    for mapping in MAPPINGS:
        source_raw = mapping.get("test_source_channel") if use_test else None
        if not source_raw:
            source_raw = mapping["source_channel"]
        source = parse_channel(source_raw)
        topic_id = int(mapping["source_topic_id"]) if mapping.get("source_topic_id") and not use_test else None

        source_entity = await client.get_entity(source)
        dest_raw = resolve_dest(mapping, use_test)
        dest_entity = await client.get_entity(dest_raw)
        bot_dest_entity = await bot.get_entity(dest_raw)

        pair = (source_entity.id, dest_entity.id)
        if pair in registered:
            print(f"  Skipped duplicate: {mapping['id']}")
            continue
        registered.add(pair)

        source_label = getattr(source_entity, 'title', source)
        if topic_id:
            source_label += f" #{topic_id}"
        print(f"  Listening: {source_label} → {getattr(dest_entity, 'title', dest_entity)}")

        @client.on(events.NewMessage(chats=source_entity))
        async def handler(event, bot_dest_entity=bot_dest_entity, topic_id=topic_id, mapping=mapping):
            msg = event.message

            # Filter by topic if needed
            if topic_id:
                reply_to = msg.reply_to
                if not reply_to:
                    return
                msg_topic = getattr(reply_to, "reply_to_top_id", None) or getattr(reply_to, "reply_to_msg_id", None)
                if msg_topic != topic_id:
                    return

            if msg.grouped_id:
                gid = msg.grouped_id
                if gid not in album_buffer:
                    album_buffer[gid] = []
                album_buffer[gid].append(msg)

                # Cancel existing flush and reset timer
                existing = album_buffer.get(f"{gid}_task")
                if existing:
                    existing.cancel()

                async def flush_album(gid=gid, bot_dest_entity=bot_dest_entity, mapping=mapping):
                    await asyncio.sleep(ALBUM_WAIT)
                    group = sorted(album_buffer.pop(gid, []), key=lambda m: m.id)
                    album_buffer.pop(f"{gid}_task", None)
                    if not group:
                        return
                    if passes_filter(group, mapping):
                        try:
                            caption, odds = await enrich_caption(group, mapping, client)
                            log_group(group, sent=True, ocr_odds=odds if mapping.get("ocr_odds") else None)
                            await send_group(client, group, bot_dest_entity, sender=bot, caption_override=caption, text_only=bool(odds))
                        except Exception as e:
                            print(f"  ✗ Album send failed: {e}", file=sys.stderr)
                    else:
                        log_group(group, sent=False)

                album_buffer[f"{gid}_task"] = asyncio.create_task(flush_album())
            else:
                if not passes_filter([msg], mapping):
                    log_group([msg], sent=False)
                    return
                try:
                    caption, odds = await enrich_caption([msg], mapping, client)
                    log_group([msg], sent=True, ocr_odds=odds if mapping.get("ocr_odds") else None)
                    await send_group(client, [msg], bot_dest_entity, sender=bot, caption_override=caption, text_only=bool(odds))
                except Exception as e:
                    print(f"  ✗ Failed on message {msg.id}: {e}", file=sys.stderr)

    print("\n✓ Listening for new messages (Ctrl+C to stop)...")
    asyncio.create_task(heartbeat())
    try:
        await client.run_until_disconnected()
    finally:
        await bot.disconnect()


if __name__ == "__main__":
    while True:
        try:
            asyncio.run(main())
        except Exception as e:
            print(f"  ✗ Crashed: {e} — restarting in 5 seconds...", file=sys.stderr)
            import time
            time.sleep(5)
