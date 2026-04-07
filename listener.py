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
import sqlite3
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
load_dotenv(".env.local", override=True)  # VPS-specific overrides (never synced)

API_ID = int(os.environ["TELEGRAM_API_ID"])
API_HASH = os.environ["TELEGRAM_API_HASH"]
SESSION = os.environ["TELEGRAM_SESSION"]
BOT_TOKEN = os.environ["BOT_TOKEN"]
BOT_SESSION = os.environ.get("BOT_SESSION", "")
MAPPINGS = json.loads(os.environ["MAPPINGS_CONFIG"])


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
        await asyncio.sleep(240)


async def connection_watchdog(client):
    """Probe Telegram every 60s with a real round-trip. Raises on failure to trigger restart."""
    await asyncio.sleep(60)  # let startup settle
    while True:
        await asyncio.sleep(60)
        try:
            await asyncio.wait_for(client.get_me(), timeout=15)
            print("  ⇌")
        except Exception as e:
            raise RuntimeError(f"Watchdog: connection probe failed ({e})")


_DB_PATH = os.path.join(os.path.dirname(__file__), "picks.db")


def _probe_db_load() -> dict:
    """Load last-seen message IDs from picks.db. Returns {(channel_id, topic_id): msg_id}."""
    try:
        conn = sqlite3.connect(_DB_PATH)
        conn.execute(
            "CREATE TABLE IF NOT EXISTS listener_probe_state"
            " (channel_id INTEGER NOT NULL, topic_id INTEGER, last_msg_id INTEGER NOT NULL,"
            " PRIMARY KEY (channel_id, topic_id))"
        )
        conn.commit()
        rows = conn.execute("SELECT channel_id, topic_id, last_msg_id FROM listener_probe_state").fetchall()
        conn.close()
        return {(r[0], r[1]): r[2] for r in rows}
    except Exception:
        return {}


def _probe_db_save(channel_id: int, topic_id, msg_id: int) -> None:
    """Persist a last-seen message ID to picks.db."""
    try:
        conn = sqlite3.connect(_DB_PATH)
        conn.execute(
            "INSERT OR REPLACE INTO listener_probe_state (channel_id, topic_id, last_msg_id) VALUES (?,?,?)",
            (channel_id, topic_id, msg_id),
        )
        conn.commit()
        conn.close()
    except Exception:
        pass


def _forwarded_init() -> None:
    """Create the listener_forwarded table if needed and prune entries older than 48h."""
    try:
        conn = sqlite3.connect(_DB_PATH)
        conn.execute(
            "CREATE TABLE IF NOT EXISTS listener_forwarded"
            " (channel_id INTEGER NOT NULL, msg_id INTEGER NOT NULL, ts REAL NOT NULL,"
            " PRIMARY KEY (channel_id, msg_id))"
        )
        cutoff = datetime.datetime.now(datetime.timezone.utc).timestamp() - 48 * 3600
        conn.execute("DELETE FROM listener_forwarded WHERE ts < ?", (cutoff,))
        conn.commit()
        conn.close()
    except Exception:
        pass


def _forwarded_save(channel_id: int, msg_id: int) -> None:
    """Record a forwarded message ID in picks.db."""
    try:
        conn = sqlite3.connect(_DB_PATH)
        conn.execute(
            "INSERT OR IGNORE INTO listener_forwarded (channel_id, msg_id, ts) VALUES (?,?,?)",
            (channel_id, msg_id, datetime.datetime.now(datetime.timezone.utc).timestamp()),
        )
        conn.commit()
        conn.close()
    except Exception:
        pass


def _was_forwarded(channel_id: int, msg_id: int) -> bool:
    """Check if a message was already forwarded."""
    try:
        conn = sqlite3.connect(_DB_PATH)
        row = conn.execute(
            "SELECT 1 FROM listener_forwarded WHERE channel_id = ? AND msg_id = ?",
            (channel_id, msg_id),
        ).fetchone()
        conn.close()
        return row is not None
    except Exception:
        return False


async def _trigger_tracker_soon():
    """Fire a quick tracker run ~3s after a pick is forwarded to get odds into the message fast."""
    await asyncio.sleep(3)
    for attempt in range(2):
        try:
            proc = await asyncio.create_subprocess_exec(
                sys.executable, "tracker.py", "--live", "--days", "0.1",
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
            await proc.wait()
            if proc.returncode != 0 and attempt == 0:
                print(f"[trigger] tracker quick-run exited {proc.returncode}, retrying in 5s")
                await asyncio.sleep(5)
                continue
        except Exception as e:
            print(f"[trigger] tracker quick-run failed: {e}")
            return
        if attempt == 0:
            await asyncio.sleep(5)  # second pass: catches "message not yet in window" edge case


async def _forward_group(group, mapping, client, bot, bot_dest_entity, use_test, catchup=False):
    """Shared forwarding logic: filter → enrich → log → send → record → trigger tracker."""
    if not use_test and not passes_filter(group, mapping):
        log_group(group, sent=False)
        return False
    caption, odds = await enrich_caption(group, mapping, client)
    log_group(group, sent=True, ocr_odds=odds if mapping.get("ocr_odds") else None, catchup=catchup)
    await send_group(client, group, bot_dest_entity, sender=bot, caption_override=caption, text_only=bool(odds))
    ch_id = group[0].peer_id.channel_id
    for m in group:
        _forwarded_save(ch_id, m.id)
    if not use_test:
        asyncio.create_task(_trigger_tracker_soon())
    return True


async def channel_probe(client, bot, channels, use_test):
    """Every 5 min, log the latest message in each source channel. Forward any missed messages."""
    await asyncio.sleep(60)
    last_seen: dict = _probe_db_load()
    while True:
        await asyncio.sleep(300)
        for source_entity, bot_dest_entity, src_label, _, topic_id, mapping in channels:
            try:
                probe_key = (source_entity.id, topic_id)
                kwargs = {"reply_to": topic_id} if topic_id else {}
                min_id = last_seen.get(probe_key, 0)
                msgs = await client.get_messages(source_entity, min_id=min_id, limit=50, **kwargs)
                if not msgs:
                    print(f"\033[2m  ⊙ {src_label}: no new msg\033[0m")
                    continue

                # Update last_seen to the newest message
                newest = max(msgs, key=lambda m: m.id)
                last_seen[probe_key] = newest.id
                _probe_db_save(source_entity.id, topic_id, newest.id)

                # Log probe status
                age = datetime.datetime.now(datetime.timezone.utc) - newest.date
                preview = (newest.text or "[media]").replace("\n", " ")[:28]
                print(f"  ⊙ {src_label}: new msg ({age.seconds//60}m ago) {preview!r}")

                # Catch-up forwarding DISABLED — was causing duplicate forwards
                # when the event handler already handled the message.
                # TODO: fix dedup before re-enabling

            except Exception as e:
                print(f"  ⊙ {src_label}: probe failed ({str(e)[:40]})")


async def main():
    client = TelegramClient(StringSession(SESSION), API_ID, API_HASH)
    await client.start()
    print("✓ Connected to Telegram (user)")

    bot = TelegramClient(StringSession(BOT_SESSION), API_ID, API_HASH)
    await bot.start(bot_token=BOT_TOKEN)
    print("✓ Connected to Telegram (bot)")

    use_test = "--test" in sys.argv
    _SEP = "  " + "─" * 55

    # ── Resolve channels ──────────────────────────────────────────────────────
    registered = set()
    channels = []  # (source_entity, bot_dest_entity, src_label, dst_label, topic_id, mapping)

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
            continue
        registered.add(pair)

        src_label = getattr(source_entity, 'title', source)
        if topic_id:
            src_label += f" #{topic_id}"
        dst_label = getattr(dest_entity, 'title', dest_entity)
        channels.append((source_entity, bot_dest_entity, src_label, dst_label, topic_id, mapping))

    # ── Init forwarded tracking table ───────────────────────────────────────────
    _forwarded_init()

    # ── Print startup block ───────────────────────────────────────────────────
    print(f"\n{_SEP}")
    print(f"  Mode: {'TEST' if use_test else 'REAL'}  |  {len(channels)} channel mapping(s)")
    src_w = max((len(c[2]) for c in channels), default=0)
    for _, _, src_lbl, dst_lbl, _, _ in channels:
        print(f"  Listening:  {src_lbl:<{src_w}}  →  {dst_lbl}")
    print(f"{_SEP}\n")

    # ── Register event handlers ───────────────────────────────────────────────
    for source_entity, bot_dest_entity, _, _, topic_id, mapping in channels:

        def _topic_ok(msg, topic_id=topic_id):
            """Return True if the message belongs to the configured topic (or no topic filter)."""
            if not topic_id:
                return True
            reply_to = msg.reply_to
            if not reply_to:
                return False
            msg_topic = getattr(reply_to, "reply_to_top_id", None) or getattr(reply_to, "reply_to_msg_id", None)
            return msg_topic == topic_id

        @client.on(events.NewMessage(chats=source_entity))
        async def handler(event, bot_dest_entity=bot_dest_entity, mapping=mapping, _topic_ok=_topic_ok):
            msg = event.message
            if msg.grouped_id:
                return  # handled by album_handler below
            if not _topic_ok(msg):
                return
            try:
                await _forward_group([msg], mapping, client, bot, bot_dest_entity, use_test)
            except Exception as e:
                print(f"  ✗ Failed on message {msg.id}: {e}", file=sys.stderr)

        @client.on(events.Album(chats=source_entity))
        async def album_handler(event, bot_dest_entity=bot_dest_entity, mapping=mapping, _topic_ok=_topic_ok):
            group = sorted(event.messages, key=lambda m: m.id)
            if not _topic_ok(group[0]):
                return
            try:
                await _forward_group(group, mapping, client, bot, bot_dest_entity, use_test)
            except Exception as e:
                print(f"  ✗ Album send failed: {e}", file=sys.stderr)

    asyncio.create_task(heartbeat())
    asyncio.create_task(channel_probe(client, bot, channels, use_test))
    watchdog = asyncio.create_task(connection_watchdog(client))
    try:
        await asyncio.gather(client.run_until_disconnected(), watchdog)
    finally:
        watchdog.cancel()
        await bot.disconnect()


if __name__ == "__main__":
    while True:
        try:
            asyncio.run(main())
        except Exception as e:
            print(f"  ✗ Crashed: {e} — restarting in 5 seconds...", file=sys.stderr)
            import time
            time.sleep(5)
