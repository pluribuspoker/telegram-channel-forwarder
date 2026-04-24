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
from tracker_cache import _load_pending_cache, _save_pending_cache

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
_in_flight: set[tuple[int, int]] = set()  # {(channel_id, msg_id)} – prevents race between event handler & catch-up


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
        return {(r[0], r[1] or 0): r[2] for r in rows}
    except Exception:
        return {}


def _probe_db_save(channel_id: int, topic_id, msg_id: int) -> None:
    """Persist a last-seen message ID to picks.db."""
    try:
        conn = sqlite3.connect(_DB_PATH)
        conn.execute(
            "INSERT OR REPLACE INTO listener_probe_state (channel_id, topic_id, last_msg_id) VALUES (?,?,?)",
            (channel_id, topic_id or 0, msg_id),
        )
        conn.commit()
        conn.close()
    except Exception:
        pass


def _reply_chain_init() -> None:
    """Create the reply_chains table if needed."""
    try:
        conn = sqlite3.connect(_DB_PATH)
        conn.execute(
            "CREATE TABLE IF NOT EXISTS reply_chains"
            " (dest_channel INTEGER NOT NULL, capper_key TEXT NOT NULL,"
            " last_msg_id INTEGER NOT NULL, PRIMARY KEY (dest_channel, capper_key))"
        )
        conn.commit()
        conn.close()
    except Exception:
        pass


def _reply_chain_get(dest_channel: int, capper_key: str) -> int | None:
    """Return the last forwarded message ID for this capper in the dest channel."""
    try:
        conn = sqlite3.connect(_DB_PATH)
        row = conn.execute(
            "SELECT last_msg_id FROM reply_chains WHERE dest_channel = ? AND capper_key = ?",
            (dest_channel, capper_key),
        ).fetchone()
        conn.close()
        return row[0] if row else None
    except Exception:
        return None


def _reply_chain_save(dest_channel: int, capper_key: str, msg_id: int) -> None:
    """Update the last forwarded message ID for this capper."""
    try:
        conn = sqlite3.connect(_DB_PATH)
        conn.execute(
            "INSERT OR REPLACE INTO reply_chains (dest_channel, capper_key, last_msg_id) VALUES (?,?,?)",
            (dest_channel, capper_key, msg_id),
        )
        conn.commit()
        conn.close()
    except Exception:
        pass


def _extract_capper_key(text: str, cappers: list[str]) -> str | None:
    """Match first line against capper prefixes. Returns None if no match."""
    first_line = next((l.strip() for l in text.splitlines() if l.strip()), "")
    fl_lower = first_line.lower()
    for capper in cappers:
        if fl_lower.startswith(capper.lower()):
            return capper.lower()
    return None


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


async def _forward_group(group, mapping, client, sender, dest_entity, use_test, catchup=False):
    """Shared forwarding logic: filter → enrich → log → send → record → trigger tracker."""
    ch_id = group[0].peer_id.channel_id

    # Claim messages to prevent duplicate forwarding between event handler and catch-up.
    # asyncio is single-threaded, so check-and-add is atomic between await points.
    keys = [(ch_id, m.id) for m in group]
    if any(k in _in_flight for k in keys):
        return False
    for k in keys:
        _in_flight.add(k)

    try:
        sent_by_id = mapping.get("_sent_by_user_id")
        if sent_by_id and group[0].sender_id != sent_by_id:
            return False
        if not use_test and not passes_filter(group, mapping):
            log_group(group, sent=False)
            return False
        caption, odds = await enrich_caption(group, mapping, client)
        log_group(group, sent=True, ocr_odds=odds if mapping.get("ocr_odds") else None, catchup=catchup)
        # Reply-chain: reply to the most recent forwarded message from the same capper
        reply_to = None
        chain_cappers = mapping.get("reply_chain_cappers")
        dest_ch = mapping.get("test_dest_channel") if use_test else mapping.get("dest_channel")
        capper_key = None
        if chain_cappers and dest_ch:
            msg_text = group[0].text or ""
            capper_key = _extract_capper_key(msg_text, chain_cappers)
            reply_to = _reply_chain_get(dest_ch, capper_key)
        try:
            sent = await send_group(client, group, dest_entity, sender=sender, caption_override=caption, text_only=bool(odds), reply_to=reply_to)
        except Exception:
            if reply_to:
                # Reply target may have been deleted — retry without reply
                sent = await send_group(client, group, dest_entity, sender=sender, caption_override=caption, text_only=bool(odds))
            else:
                raise
        for m in group:
            _forwarded_save(ch_id, m.id)
        # Seed parse cache so the tracker knows this message was forwarded by us
        if sent and sent is not True:
            sent_ids = [s.id for s in sent] if isinstance(sent, list) else [sent.id]
            cache = _load_pending_cache()
            for sid in sent_ids:
                cache[f"{dest_ch}:{sid}"] = {"_forwarded": True, "mapping_id": mapping.get("id", "")}
            _save_pending_cache(cache)
            # Update reply chain with the newest sent message
            if capper_key and dest_ch:
                _reply_chain_save(dest_ch, capper_key, sent_ids[-1])
        if not use_test:
            asyncio.create_task(_trigger_tracker_soon())
        return True
    finally:
        for k in keys:
            _in_flight.discard(k)


async def channel_probe(client, channels, use_test):
    """Every 5 min, log the latest message in each source channel. Forward any missed messages."""
    await asyncio.sleep(60)
    last_seen: dict = _probe_db_load()
    while True:
        await asyncio.sleep(60)
        for source_entity, sender_dest_entity, src_label, _, topic_id, mapping, sender_client in channels:
            try:
                probe_key = (source_entity.id, topic_id or 0)
                kwargs = {"reply_to": topic_id} if topic_id else {}
                if probe_key not in last_seen:
                    # New mapping — seed with newest msg so we don't replay history
                    seed = await client.get_messages(source_entity, limit=1, **kwargs)
                    seed_id = seed[0].id if seed else 0
                    last_seen[probe_key] = seed_id
                    _probe_db_save(source_entity.id, topic_id, seed_id)
                min_id = last_seen[probe_key]
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

                # Catch-up: forward any messages not already handled by event handlers
                # Separate into singles and albums (grouped_id)
                albums: dict[int, list] = {}
                singles = []
                for msg in sorted(msgs, key=lambda m: m.id):
                    if _was_forwarded(source_entity.id, msg.id):
                        continue
                    if msg.grouped_id:
                        albums.setdefault(msg.grouped_id, []).append(msg)
                    else:
                        singles.append(msg)

                for msg in singles:
                    try:
                        await _forward_group([msg], mapping, client, sender_client, sender_dest_entity, use_test, catchup=True)
                    except Exception as e:
                        print(f"  ✗ Catch-up failed msg {msg.id}: {e}", file=sys.stderr)

                for gid, group in albums.items():
                    try:
                        await _forward_group(group, mapping, client, sender_client, sender_dest_entity, use_test, catchup=True)
                    except Exception as e:
                        print(f"  ✗ Catch-up failed album {gid}: {e}", file=sys.stderr)

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
    channels = []  # (source_entity, sender_dest_entity, src_label, dst_label, topic_id, mapping, sender_client)

    for mapping in MAPPINGS:
        source_raw = mapping.get("test_source_channel") if use_test else None
        if not source_raw:
            source_raw = mapping["source_channel"]
        source = parse_channel(source_raw)
        topic_id = int(mapping["source_topic_id"]) if mapping.get("source_topic_id") and not use_test else None

        try:
            source_entity = await client.get_entity(source)
        except Exception as e:
            mid = mapping.get("id", source_raw)
            print(f"  ⚠ Skipping mapping '{mid}': cannot resolve source channel ({e})")
            continue
        dest_raw = resolve_dest(mapping, use_test)
        dest_entity = await client.get_entity(dest_raw)
        bot_dest_entity = await bot.get_entity(dest_raw)

        # Resolve sent_by_user username → numeric ID
        if mapping.get("sent_by_user"):
            user_entity = await client.get_entity(mapping["sent_by_user"])
            mapping["_sent_by_user_id"] = user_entity.id
            print(f"  Resolved sent_by_user '{mapping['sent_by_user']}' → {user_entity.id}")

        # Determine sender client based on send_as_user flag
        if mapping.get("send_as_user"):
            sender_client = client
            sender_dest_entity = dest_entity
        else:
            sender_client = bot
            sender_dest_entity = bot_dest_entity

        pair = (source_entity.id, dest_entity.id, topic_id)
        if pair in registered:
            continue
        registered.add(pair)

        src_label = getattr(source_entity, 'title', source)
        if topic_id:
            try:
                topic_msg = await client.get_messages(source_entity, ids=topic_id)
                topic_name = topic_msg.action.title
            except Exception:
                topic_name = str(topic_id)
            src_label += f" / {topic_name}"
        dst_label = getattr(dest_entity, 'title', dest_entity)
        channels.append((source_entity, sender_dest_entity, src_label, dst_label, topic_id, mapping, sender_client))

    # ── Init DB tables ───────────────────────────────────────────────────────────
    _forwarded_init()
    _reply_chain_init()

    # ── Print startup block ───────────────────────────────────────────────────
    print(f"\n{_SEP}")
    print(f"  Mode: {'TEST' if use_test else 'REAL'}  |  {len(channels)} channel mapping(s)")
    src_w = max((len(c[2]) for c in channels), default=0)
    for _, _, src_lbl, dst_lbl, _, _, _ in channels:
        print(f"  Listening:  {src_lbl:<{src_w}}  →  {dst_lbl}")
    print(f"{_SEP}\n")

    # ── Register event handlers ───────────────────────────────────────────────
    for source_entity, sender_dest_entity, _, _, topic_id, mapping, sender_client in channels:

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
        async def handler(event, sender_dest_entity=sender_dest_entity, mapping=mapping, _topic_ok=_topic_ok, sender_client=sender_client):
            msg = event.message
            if msg.grouped_id:
                return  # handled by album_handler below
            if not _topic_ok(msg):
                return
            try:
                await _forward_group([msg], mapping, client, sender_client, sender_dest_entity, use_test)
            except Exception as e:
                print(f"  ✗ Failed on message {msg.id}: {e}", file=sys.stderr)

        @client.on(events.Album(chats=source_entity))
        async def album_handler(event, sender_dest_entity=sender_dest_entity, mapping=mapping, _topic_ok=_topic_ok, sender_client=sender_client):
            group = sorted(event.messages, key=lambda m: m.id)
            if not _topic_ok(group[0]):
                return
            try:
                await _forward_group(group, mapping, client, sender_client, sender_dest_entity, use_test)
            except Exception as e:
                print(f"  ✗ Album send failed: {e}", file=sys.stderr)

    asyncio.create_task(heartbeat())
    asyncio.create_task(channel_probe(client, channels, use_test))
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
