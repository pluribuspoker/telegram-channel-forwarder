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
import hashlib
import sqlite3
import sys
import time
import urllib.request

logging.basicConfig(
    level=logging.WARNING,
    format="%(asctime)s [%(name)s] %(message)s",
    datefmt="%H:%M:%S",
)

from dotenv import load_dotenv
from telethon import TelegramClient, events
from telethon.sessions import StringSession
from telethon.tl.types import MessageMediaDocument, MessageMediaPhoto

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
    last_ok_log = 0.0
    while True:
        await asyncio.sleep(60)
        try:
            await asyncio.wait_for(client.get_me(), timeout=15)
        except Exception as e:
            raise RuntimeError(f"Watchdog: connection probe failed ({e})")
        # One ⇌ per hour, not per probe — the per-minute line was pure journal noise
        # (failure is loud: it raises and restarts the service).
        if time.monotonic() - last_ok_log > 3600:
            print("  ⇌")
            last_ok_log = time.monotonic()


_DB_PATH = os.path.join(os.path.dirname(__file__), "picks.db")
_in_flight: set[tuple[int, int, int]] = set()  # {(channel_id, dest_channel, msg_id)} – prevents race between event handler & catch-up
_content_in_flight: set[tuple[int, str]] = set()  # {(dest_channel, text_hash)} – prevents forwarding an identical repost (capper delete-and-repost / double-post)
_CONTENT_DEDUP_WINDOW = 15 * 60  # seconds: suppress a byte-identical repost to the same dest within this window


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
        # Migrate from old schema (channel_id, msg_id) to new (channel_id, dest_channel, msg_id)
        cols = [row[1] for row in conn.execute("PRAGMA table_info(listener_forwarded)").fetchall()]
        if cols and "dest_channel" not in cols:
            conn.execute("DROP TABLE listener_forwarded")
        conn.execute(
            "CREATE TABLE IF NOT EXISTS listener_forwarded"
            " (channel_id INTEGER NOT NULL, dest_channel INTEGER NOT NULL,"
            " msg_id INTEGER NOT NULL, ts REAL NOT NULL,"
            " PRIMARY KEY (channel_id, dest_channel, msg_id))"
        )
        conn.execute(
            "CREATE TABLE IF NOT EXISTS listener_content_seen"
            " (dest_channel INTEGER NOT NULL, text_hash TEXT NOT NULL, ts REAL NOT NULL,"
            " PRIMARY KEY (dest_channel, text_hash))"
        )
        cutoff = datetime.datetime.now(datetime.timezone.utc).timestamp() - 48 * 3600
        conn.execute("DELETE FROM listener_forwarded WHERE ts < ?", (cutoff,))
        conn.execute("DELETE FROM listener_content_seen WHERE ts < ?", (cutoff,))
        conn.commit()
        conn.close()
    except Exception:
        pass


def _forwarded_save(channel_id: int, dest_channel: int, msg_id: int) -> None:
    """Record a forwarded message ID in picks.db."""
    try:
        conn = sqlite3.connect(_DB_PATH)
        conn.execute(
            "INSERT OR IGNORE INTO listener_forwarded (channel_id, dest_channel, msg_id, ts) VALUES (?,?,?,?)",
            (channel_id, dest_channel, msg_id, datetime.datetime.now(datetime.timezone.utc).timestamp()),
        )
        conn.commit()
        conn.close()
    except Exception:
        pass


def _was_forwarded(channel_id: int, dest_channel: int, msg_id: int) -> bool:
    """Check if a message was already forwarded to a specific destination."""
    try:
        conn = sqlite3.connect(_DB_PATH)
        row = conn.execute(
            "SELECT 1 FROM listener_forwarded WHERE channel_id = ? AND dest_channel = ? AND msg_id = ?",
            (channel_id, dest_channel, msg_id),
        ).fetchone()
        conn.close()
        return row is not None
    except Exception:
        return False


def _content_sig(group) -> str | None:
    """Normalized-text signature for a message group, or None if there is no text.

    Used to suppress an identical repost (capper deletes a pick and re-posts it, or
    double-taps send). Whitespace-collapsed + lowercased so trivial edits still match.
    Returns None for media-only posts (no text) so distinct images are never deduped.
    """
    text = " ".join((m.text or "").strip() for m in group).strip()
    if not text:
        return None
    norm = " ".join(text.split()).lower()
    return hashlib.sha1(norm.encode("utf-8")).hexdigest()


def _scoped_sig(mapping, sig: str | None) -> str | None:
    """Content-dedup key, scoped by the mapping's source topic.

    CICL and CILT are different cappers in one source group sharing dest channels: a
    byte-identical pick posted in both topics within the window must forward twice.
    Unscoped, the second copy was suppressed permanently — the probe advances
    last_seen past it on the same cycle that declines it, so nothing ever retried.
    Delete-and-repost (the case this dedup exists for) happens within one topic, so
    scoping loses nothing.
    """
    if not sig:
        return None
    return f"{mapping.get('source_topic_id') or 0}:{sig}"


def _content_recent(dest_channel: int, text_hash: str) -> bool:
    """True if identical content was forwarded to this dest within the dedup window."""
    try:
        conn = sqlite3.connect(_DB_PATH)
        cutoff = datetime.datetime.now(datetime.timezone.utc).timestamp() - _CONTENT_DEDUP_WINDOW
        row = conn.execute(
            "SELECT 1 FROM listener_content_seen WHERE dest_channel = ? AND text_hash = ? AND ts > ?",
            (dest_channel, text_hash, cutoff),
        ).fetchone()
        conn.close()
        return row is not None
    except Exception:
        return False


def _content_save(dest_channel: int, text_hash: str) -> None:
    """Record forwarded content so an identical repost within the window is suppressed."""
    try:
        conn = sqlite3.connect(_DB_PATH)
        conn.execute(
            "INSERT OR REPLACE INTO listener_content_seen (dest_channel, text_hash, ts) VALUES (?,?,?)",
            (dest_channel, text_hash, datetime.datetime.now(datetime.timezone.utc).timestamp()),
        )
        conn.commit()
        conn.close()
    except Exception:
        pass


# ── Media relay cache ────────────────────────────────────────────────────────
# A photo pick fanned out to N dests used to be downloaded from the source and
# re-uploaded N times (once per dest, sequentially — the slowest part of the
# fan-out chain). The first successful send now caches the SENT message's media,
# and the other dests send that by file reference: 1 download + 1 upload total.
# Keyed by sender client too, since a file reference only works for the account
# that produced it (bot vs send_as_user user session).
_MEDIA_RELAY_TTL = 15 * 60
_media_relay: dict[tuple[int, int, int], tuple[float, object]] = {}  # (src_ch, first_msg_id, sender_id) → (ts, media | [media])


def _relay_get(key):
    v = _media_relay.get(key)
    if not v:
        return None
    ts, media = v
    if time.time() - ts > _MEDIA_RELAY_TTL:
        _media_relay.pop(key, None)
        return None
    return media


def _relay_put(key, media) -> None:
    now = time.time()
    for k in [k for k, (ts, _) in _media_relay.items() if now - ts > _MEDIA_RELAY_TTL]:
        _media_relay.pop(k, None)
    _media_relay[key] = (now, media)


def _relayable_media(group) -> bool:
    """Only photo/document media is re-sendable by reference; a webpage preview is not."""
    return any(isinstance(m.media, (MessageMediaDocument, MessageMediaPhoto)) for m in group)


_trigger_lock = asyncio.Lock()
# Work queue of (dest_channel, message_id) pairs this listener just created and still owes a
# quick tracker pass. One source pick fanned out to N dest channels enqueues N entries.
_pending_targets: set[tuple[int, int]] = set()
_pending_full_sweep = False   # fallback when a send gave us no usable message id

_QUICK_LOG      = os.path.join(os.path.dirname(os.path.abspath(__file__)), "logs", "tracker_quick.log")
_QUICK_LOG_MAX  = 5 * 1024 * 1024


def _quick_log(header: str, body: str) -> None:
    """Append a quick-run's full output to logs/tracker_quick.log (size-rotated).

    The quick run used to write to DEVNULL, so it left no trace anywhere and 'did the fast
    path fire for message X?' was unanswerable after the fact.
    """
    try:
        os.makedirs(os.path.dirname(_QUICK_LOG), exist_ok=True)
        if os.path.exists(_QUICK_LOG) and os.path.getsize(_QUICK_LOG) > _QUICK_LOG_MAX:
            os.replace(_QUICK_LOG, _QUICK_LOG + ".1")
        with open(_QUICK_LOG, "a", encoding="utf-8", errors="replace") as f:
            f.write(f"\n{'='*70}\n{header}\n{'='*70}\n{body}")
    except Exception as e:
        print(f"[trigger] could not write {_QUICK_LOG}: {e}")


async def _trigger_tracker_soon():
    """Fire a quick tracker run ~3s after a pick is forwarded to get odds into the message fast.

    Targeted: we already know exactly which (channel, message_id) pairs we just created, so the
    tracker is handed that list rather than blind-scanning every graded channel by date. That
    removes the race outright — previously whether a pick got its odds depended on where the
    by-date sweep happened to be when the message landed, so the same pick fanned out to two
    channels could get odds in 18s in one and wait 4m14s in the other.

    Work-queue semantics: forwards enqueue targets, the runner drains the queue each pass. A
    forward landing mid-run simply re-arms the queue and is picked up by the next pass, so
    nothing is dropped and no retry/coalesce heuristic is needed.
    """
    global _pending_full_sweep
    if _trigger_lock.locked():
        return  # queue is armed; the in-flight runner will drain it
    async with _trigger_lock:
        failures = 0
        while True:
            await asyncio.sleep(3)   # let Telegram settle; also batches a fan-out into one pass
            batch, full = sorted(_pending_targets), _pending_full_sweep
            _pending_targets.clear()
            _pending_full_sweep = False
            if not batch and not full:
                return

            argv = [sys.executable, "tracker.py", "--live"]
            if full:
                argv += ["--days", "0.1"]
                label = "full-sweep"
            else:
                # `--target=` (not a separate argv item): channel ids are negative, so argparse
                # would otherwise read the value as another option.
                argv += [f"--target={ch}:{mid}" for ch, mid in batch]
                label = " ".join(f"{ch}:{mid}" for ch, mid in batch)

            started = time.monotonic()
            try:
                # Tag this run's Claude spend so the ledger separates it from the
                # timer-driven tracker — both are tracker.py, but this one's output
                # never reaches journald, which is how its spend went uncounted.
                quick_env = {**os.environ, "CLAUDE_SPEND_SOURCE": "tracker-fastpath"}
                proc = await asyncio.create_subprocess_exec(
                    *argv, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
                    env=quick_env,
                )
                out, _ = await proc.communicate()
                rc = proc.returncode
            except Exception as e:
                print(f"[trigger] quick-run {label} failed to spawn: {e}")
                return
            took = time.monotonic() - started
            body = (out or b"").decode("utf-8", "replace")
            _quick_log(f"{datetime.datetime.now().isoformat(timespec='seconds')}  "
                       f"{label}  rc={rc}  {took:.1f}s", body)
            # One line per run into the journal so `journalctl -u telegram-forwarder | grep <msg>`
            # answers "did the fast path fire for this message?" without opening the log file.
            print(f"[trigger] quick-run {label} rc={rc} in {took:.1f}s")

            if rc != 0:
                for line in body.strip().splitlines()[-15:]:
                    print(f"[trigger]   | {line}")
                failures += 1
                if failures > 1:
                    print("[trigger] quick-run failed twice, leaving it to the 5-min timer")
                    return
                # Requeue so the retry targets the same messages rather than guessing.
                _pending_targets.update(batch)
                _pending_full_sweep = _pending_full_sweep or full
                await asyncio.sleep(5)
            else:
                failures = 0


async def _forward_group(group, mapping, client, sender, dest_entity, use_test, catchup=False):
    """Shared forwarding logic: filter → enrich → log → send → record → trigger tracker."""
    global _pending_full_sweep
    ch_id = group[0].peer_id.channel_id
    dest_ch = mapping.get("test_dest_channel") if use_test else mapping.get("dest_channel")

    # Claim messages to prevent duplicate forwarding between event handler and catch-up.
    # asyncio is single-threaded, so check-and-add is atomic between await points.
    keys = [(ch_id, dest_ch, m.id) for m in group]
    if any(k in _in_flight for k in keys):
        return False
    # Check persistent DB — catches late event-handler fires after catch-up already forwarded
    if any(_was_forwarded(ch_id, dest_ch, m.id) for m in group):
        return False
    # Content dedup — a capper deleting a pick and re-posting it (or double-tapping send)
    # produces a NEW message id, so the id-based guards above don't catch it. Suppress a
    # byte-identical repost to the same dest within the window. Skipped in test mode.
    sig = None if use_test else _scoped_sig(mapping, _content_sig(group))
    content_key = (dest_ch, sig) if sig else None
    if content_key and (content_key in _content_in_flight or _content_recent(dest_ch, sig)):
        print(f"  ⊘ duplicate content, skipping msg {group[0].id} → {dest_ch}")
        return False
    for k in keys:
        _in_flight.add(k)
    if content_key:
        _content_in_flight.add(content_key)

    try:
        sent_by_id = mapping.get("_sent_by_user_id")
        if sent_by_id and group[0].sender_id != sent_by_id:
            return False
        if not use_test and not passes_filter(group, mapping):
            log_group(group, sent=False)
            return False
        caption, odds = await enrich_caption(group, mapping, client)
        source_label = mapping.get("source_label")
        text_suffix = f"— {source_label}" if source_label else None
        # source_prefix: a bold header line naming the source (it becomes the post's
        # first line, i.e. the capper_name the tracker/broadcasts read).
        text_prefix = mapping.get("source_prefix") or None
        log_group(group, sent=True, ocr_odds=odds if mapping.get("ocr_odds") else None, catchup=catchup)
        # Reply-chain: reply to the most recent forwarded message from the same capper
        reply_to = None
        chain_cappers = mapping.get("reply_chain_cappers")
        capper_key = None
        if chain_cappers and dest_ch:
            # raw_text: `.text` is the markdown render, so a bolded capper name
            # arrives as `**OG Kelly**` and the prefix match below misses.
            msg_text = group[0].raw_text or ""
            capper_key = _extract_capper_key(msg_text, chain_cappers)
            # Single-capper mapping: default to the only capper even if prefix missing
            if capper_key is None and len(chain_cappers) == 1:
                capper_key = chain_cappers[0].lower()
            reply_to = _reply_chain_get(dest_ch, capper_key)
        relayable = not odds and _relayable_media(group)
        relay_key = (ch_id, group[0].id, id(sender)) if relayable else None

        async def _send(reply_to_arg):
            media_override = _relay_get(relay_key) if relayable else None
            try:
                return await send_group(client, group, dest_entity, sender=sender, caption_override=caption, text_only=bool(odds), reply_to=reply_to_arg, text_suffix=text_suffix, text_prefix=text_prefix, media_override=media_override)
            except Exception:
                if media_override is None:
                    raise
                # Cached file reference went stale or was rejected — drop it and
                # pay the normal download+upload path for this dest.
                _media_relay.pop(relay_key, None)
                return await send_group(client, group, dest_entity, sender=sender, caption_override=caption, text_only=bool(odds), reply_to=reply_to_arg, text_suffix=text_suffix, text_prefix=text_prefix)

        try:
            sent = await _send(reply_to)
        except Exception:
            if reply_to:
                # Reply target may have been deleted — retry without reply
                sent = await _send(None)
            else:
                raise
        # Cache the sent media so the remaining fan-out dests resend by reference.
        if relayable and sent and sent is not True:
            _sent_list = sent if isinstance(sent, list) else [sent]
            _media = [s.media for s in _sent_list
                      if isinstance(getattr(s, "media", None), (MessageMediaDocument, MessageMediaPhoto))]
            if _media:
                _relay_put(relay_key, _media if len(group) > 1 else _media[0])
        for m in group:
            _forwarded_save(ch_id, dest_ch, m.id)
        if content_key:
            _content_save(dest_ch, sig)
        # Seed parse cache so the tracker knows this message was forwarded by us
        if sent and sent is not True:
            sent_ids = [s.id for s in sent] if isinstance(sent, list) else [sent.id]
            cache = _load_pending_cache()
            source_key = f"{ch_id}:{group[0].id}"
            for sid in sent_ids:
                cache[f"{dest_ch}:{sid}"] = {"_forwarded": True, "mapping_id": mapping.get("id", ""), "_source_key": source_key}
            _save_pending_cache(cache)
            # Hand the quick tracker pass exactly what we just created. The same source pick
            # forwarded to N dest channels enqueues N targets, so every copy is processed —
            # no copy's fate depends on where a by-date sweep happened to be.
            _pending_targets.update((dest_ch, sid) for sid in sent_ids)
            # Update reply chain with the newest sent message
            if capper_key and dest_ch:
                _reply_chain_save(dest_ch, capper_key, sent_ids[-1])
        elif sent:
            _pending_full_sweep = True   # sent, but no usable ids — fall back to a date sweep
        if not use_test:
            asyncio.create_task(_trigger_tracker_soon())
        return True
    finally:
        for k in keys:
            _in_flight.discard(k)
        if content_key:
            _content_in_flight.discard(content_key)


def _build_probe_groups(channels):
    """Group resolved mappings by (source channel, topic).

    The probe fetches history once per group per cycle: a source fanned out to N
    dests costs one RPC instead of N identical ones (it was O(mappings) per minute,
    which is why the old loop needed a min_ids snapshot — the first mapping to see
    new messages advanced last_seen under its siblings)."""
    groups: dict[tuple[int, int], list] = {}
    for chan in channels:
        source_entity, topic_id = chan[0], chan[4]
        groups.setdefault((source_entity.id, topic_id or 0), []).append(chan)
    return groups


async def _probe_cycle(client, groups, last_seen, use_test) -> int:
    """One probe pass: fetch each (source, topic) once, catch up every mapping on it.
    Returns the number of quiet groups (no new messages)."""
    quiet = 0
    for (src_id, _topic0), group_chans in groups.items():
        source_entity, _, src_label, _, topic_id, _, _ = group_chans[0]
        try:
            probe_key = (src_id, topic_id or 0)
            kwargs = {"reply_to": topic_id} if topic_id else {}
            if probe_key not in last_seen:
                # New mapping — seed with newest msg so we don't replay history
                seed = await client.get_messages(source_entity, limit=1, **kwargs)
                seed_id = seed[0].id if seed else 0
                last_seen[probe_key] = seed_id
                _probe_db_save(src_id, topic_id, seed_id)
            min_id = last_seen[probe_key]
            msgs = await client.get_messages(source_entity, min_id=min_id, limit=50, **kwargs)
            if not msgs:
                quiet += 1
                continue

            # Update last_seen to the newest message
            newest = max(msgs, key=lambda m: m.id)
            last_seen[probe_key] = newest.id
            _probe_db_save(src_id, topic_id, newest.id)

            # Log probe status
            age = datetime.datetime.now(datetime.timezone.utc) - newest.date
            preview = (newest.text or "[media]").replace("\n", " ")[:28]
            print(f"  ⊙ {src_label}: new msg ({age.seconds//60}m ago) {preview!r}")

            ordered = sorted(msgs, key=lambda m: m.id)
            for chan in group_chans:
                _, sender_dest_entity, _, _, _, mapping, sender_client = chan
                probe_dest = mapping.get("test_dest_channel") if use_test else mapping.get("dest_channel")

                # Catch-up: forward any messages not already handled by event handlers
                # Separate into singles and albums (grouped_id)
                albums: dict[int, list] = {}
                singles = []
                for msg in ordered:
                    if _was_forwarded(src_id, probe_dest, msg.id):
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
    return quiet


async def channel_probe(client, channels, use_test):
    """Every 60s, poll each source (channel, topic) once; forward anything the event
    handlers missed. Quiet polls log one summary line per hour — the per-mapping
    'no new msg' line every minute was ~90% of this unit's journal."""
    await asyncio.sleep(60)
    last_seen: dict = _probe_db_load()
    groups = _build_probe_groups(channels)
    last_quiet_log = 0.0
    while True:
        await asyncio.sleep(60)
        quiet = await _probe_cycle(client, groups, last_seen, use_test)
        if quiet and time.monotonic() - last_quiet_log > 3600:
            print(f"  ⊙ probe: {quiet}/{len(groups)} source topics quiet this pass")
            last_quiet_log = time.monotonic()


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
        try:
            dest_entity = await client.get_entity(dest_raw)
            bot_dest_entity = await bot.get_entity(dest_raw)
        except Exception as e:
            # Same guard as the source above: one dead dest channel (deleted, bot
            # kicked, never met) must cost only its own mapping — unguarded, it
            # crash-looped main() and took every other mapping down with it.
            mid = mapping.get("id", dest_raw)
            print(f"  ⚠ Skipping mapping '{mid}': cannot resolve dest channel ({e})")
            continue

        # Resolve sent_by_user username → numeric ID
        if mapping.get("sent_by_user"):
            try:
                user_entity = await client.get_entity(mapping["sent_by_user"])
            except Exception as e:
                # Skip rather than forward unfiltered: a failed resolve would
                # otherwise disable the sender filter for this mapping.
                mid = mapping.get("id", dest_raw)
                print(f"  ⚠ Skipping mapping '{mid}': cannot resolve sent_by_user ({e})")
                continue
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
            # One mapping per (source, dest, topic): a second one here would be
            # dead config — its filter/prefix would never run. Say so instead of
            # silently ignoring it.
            print(f"  ⚠ Mapping '{mapping.get('id', '?')}' shares (source, dest, topic) with an "
                  f"earlier mapping and is IGNORED — merge the two into one mapping")
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

    # ── Auth: /access command handler on bot ────────────────────────────
    FIGHT_CLUB_CHANNEL = -1002486251914

    def _check_membership(user_id: int) -> str:
        """Check Fight Club membership via Bot API getChatMember. Returns status string."""
        url = f"https://api.telegram.org/bot{BOT_TOKEN}/getChatMember"
        data = json.dumps({"chat_id": FIGHT_CLUB_CHANNEL, "user_id": user_id}).encode()
        req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"})
        resp = urllib.request.urlopen(req, timeout=10)
        result = json.loads(resp.read().decode())
        return result["result"]["status"]

    async def _handle_auth_request(event):
        if not event.is_private:
            return
        user_id = event.sender_id
        try:
            status = await asyncio.get_event_loop().run_in_executor(None, _check_membership, user_id)
        except Exception:
            await event.respond("Something went wrong checking your membership. Try again later.")
            return
        if status not in ("member", "administrator", "creator"):
            await event.respond("You must be a member of the Fight Club channel to access the dashboard.")
            return
        try:
            from angles.auth import make_token, get_secret, MAGIC_LINK_TTL
            secret = get_secret()
            token = make_token(user_id, MAGIC_LINK_TTL, secret)
        except Exception:
            await event.respond("Auth is not configured on the server. Contact admin.")
            return
        url = f"https://fightclubpicks.cc/auth?token={token}"
        await event.respond(
            f"Here's your access link (valid for 5 minutes):\n\n{url}\n\n"
            "Click it to log in to the Angle Analyzer dashboard."
        )

    @bot.on(events.NewMessage(pattern=r'^/access$', incoming=True, func=lambda e: e.is_private))
    async def handle_access(event):
        await _handle_auth_request(event)

    @bot.on(events.NewMessage(pattern=r'^/start\s+access$', incoming=True, func=lambda e: e.is_private))
    async def handle_start_access(event):
        await _handle_auth_request(event)

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
