import copy
import json
import os
import re
import time
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone

try:
    import fcntl            # POSIX only. Local Windows dev runs one process, so no lock needed.
except ImportError:         # pragma: no cover
    fcntl = None

_PENDING_CACHE_PATH = os.path.join(os.path.dirname(__file__), "parse_cache.json")
# Lock a SIDECAR file, never parse_cache.json itself. flock attaches to an *inode*, and every
# save os.replace()s a brand-new inode into place — so a lock taken on the data file would end
# up guarding an orphaned inode while every process believed it was protected.
_PENDING_LOCK_PATH = _PENDING_CACHE_PATH + ".lock"
_LOCK_TIMEOUT = 10.0
_EVICT_AFTER_DAYS = 14


class _PendingCache(dict):
    """The pending cache plus a snapshot of how it looked when this process loaded it.

    Every writer (listener, tracker, grade daemon) loads the whole dict, edits a few entries and
    writes the whole dict back. Without a snapshot the last writer wins for the ENTIRE file and
    silently erases whatever the others did in between — that is how the listener's `_forwarded`
    seed for a freshly forwarded message used to vanish. The snapshot lets a save distinguish
    "I changed this key" from "I merely read it", so it can merge instead of clobber.
    """
    __slots__ = ("_snapshot",)


def _norm_desc(d: str) -> str:
    """Normalise a pick description for duplicate comparison."""
    d = d.lower().strip()
    d = re.sub(r'\([+-]?\d+\)', '', d)               # strip parenthesized odds e.g. (-170)
    d = re.sub(r'(?<=\s)[+-]?\d{3,}(?=\s|$)', '', d) # strip bare American odds e.g. -170, +110
    d = re.sub(r'\d+(\.\d+)?u\b', '', d)              # strip units e.g. 1.5u
    return re.sub(r'\s+', ' ', d).strip()


def _pending_entry(capper: str, parsed: dict, leg_verdicts: dict, existing: dict, odds_by_pick: dict | None = None) -> dict:
    """Build a pending-cache entry, preserving linked_message_ids and odds from the existing entry."""
    entry = {
        "capper_name":        capper,
        "parsed":             parsed,
        "leg_verdicts":       leg_verdicts,
        "linked_message_ids": existing.get("linked_message_ids", []),
        # Preserve fetched odds — once set, never overwritten with None
        "odds_by_pick":       odds_by_pick if odds_by_pick is not None else existing.get("odds_by_pick", {}),
    }
    if existing.get("_unknown_notified"):
        entry["_unknown_notified"] = True
    if existing.get("_forwarded"):
        entry["_forwarded"] = True
    if existing.get("mapping_id"):
        entry["mapping_id"] = existing["mapping_id"]
    if existing.get("_source_key"):
        entry["_source_key"] = existing["_source_key"]
    # Preserve html_text + has_media for grade daemon (Bot API edits without Telethon)
    if existing.get("html_text") is not None:
        entry["html_text"] = existing["html_text"]
    if existing.get("has_media") is not None:
        entry["has_media"] = existing["has_media"]
    if existing.get("reply_to_id") is not None:
        entry["reply_to_id"] = existing["reply_to_id"]
    return entry


def _find_mirror_entry(
    pending_cache: dict,
    source_key: str,
    exclude_key: str,
) -> dict | None:
    """Find a sibling cache entry from the same source message that already has parsed data.

    Used to share parse results and odds across destinations when the same source
    message is forwarded to multiple channels.
    """
    for key, entry in pending_cache.items():
        if key == exclude_key:
            continue
        if isinstance(entry, dict) and entry.get("_source_key") == source_key and "parsed" in entry:
            return entry
    return None


def _find_duplicate_cache_key(
    pending_cache: dict,
    channel_id: int,
    capper_name: str,
    new_picks: list[dict],
    exclude_key: str | None = None,
) -> str | None:
    """Return the cache key of a pending entry that matches this capper+picks, else None."""
    norm_new = sorted(_norm_desc(p.get("description", "")) for p in new_picks)
    capper_lower = capper_name.lower()
    for key, entry in pending_cache.items():
        if key == exclude_key:
            continue
        if int(key.split(':')[0]) != channel_id:
            continue
        if entry.get("capper_name", "").lower() != capper_lower:
            continue
        # Skip fully-resolved entries — a new message matching a completed pick
        # is a new game, not a duplicate (e.g. same team ML on different days).
        leg_verdicts = entry.get("leg_verdicts", {})
        if leg_verdicts and all(
            isinstance(v, dict) and v.get("verdict") in ("WIN", "LOSS", "PUSH")
            for v in leg_verdicts.values()
        ):
            continue
        existing_picks = entry.get("parsed", {}).get("picks", [])
        if not existing_picks:
            continue
        norm_existing = sorted(_norm_desc(p.get("description", "")) for p in existing_picks)
        if norm_existing == norm_new:
            return key
    return None


def _read_raw() -> dict:
    """Plain read of whatever is on disk right now. No snapshot, no lock.

    Reads need no lock: saves swap the file in with os.replace(), so a reader either sees the
    whole old file or the whole new one, never a torn one.
    """
    try:
        with open(_PENDING_CACHE_PATH) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def _load_pending_cache() -> _PendingCache:
    data = _read_raw()
    cache = _PendingCache(data)
    cache._snapshot = copy.deepcopy(data)
    return cache


@contextmanager
def _cache_lock(timeout: float = _LOCK_TIMEOUT):
    """Hold an exclusive lock across the cache read-modify-write (a few ms).

    Advisory and same-host, which is all we need — every writer is a process on this VPS.
    """
    if fcntl is None:
        yield
        return
    fd = os.open(_PENDING_LOCK_PATH, os.O_CREAT | os.O_RDWR, 0o644)
    try:
        deadline = time.monotonic() + timeout
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except OSError:
                if time.monotonic() >= deadline:
                    # Never wedge grading behind a stuck holder. The merge still re-reads disk,
                    # so an unlocked write is no worse than the behaviour before this lock.
                    print(f"[cache] lock busy >{timeout:.0f}s — writing without it")
                    break
                time.sleep(0.02)
        yield
    finally:
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        except OSError:
            pass
        os.close(fd)


def _merge_onto_disk(cache: dict, disk: dict, snapshot: dict | None) -> dict:
    """Apply only THIS process's changes onto the current on-disk state.

    Starts from `disk`, so entries another process created since we loaded survive instead of
    being erased by our full-dict write. Then, per key:
      - we added or modified it  -> ours wins
      - we only read it          -> disk wins, even if it changed under us
      - we deleted it (eviction) -> honour the delete
    """
    merged = dict(disk)
    if snapshot is None:
        # Caller built the dict itself rather than going through _load_pending_cache(); we can't
        # tell reads from writes, so treat every key as ours (old behaviour) but still keep the
        # keys only disk knows about.
        merged.update(cache)
        return merged
    for key, value in cache.items():
        if key not in snapshot or snapshot[key] != value:
            merged[key] = value
    for key in snapshot:
        if key not in cache:
            merged.pop(key, None)
    return merged


def _merge_broadcasted_flags(cache: dict, disk: dict) -> None:
    """Preserve `broadcasted: True` flags already set on disk.

    `_merge_onto_disk` resolves conflicts a whole key at a time, so when we legitimately rewrite
    a key (say, to record a verdict) our copy of its legs wins — including a stale
    `broadcasted: False` for a leg the daemon broadcast in the meantime, which would make the
    daemon broadcast it again next cycle (duplicate result).

    `broadcasted` is monotonic (a leg broadcasts exactly once, False→True), so OR-ing the
    on-disk flag in is always safe: it can only suppress a duplicate, never drop a legitimate
    broadcast. Only legs still present in `cache` are touched.
    """
    for key, entry in cache.items():
        if not isinstance(entry, dict):
            continue
        disk_entry = disk.get(key)
        if not isinstance(disk_entry, dict):
            continue
        disk_lv = disk_entry.get("leg_verdicts")
        mem_lv = entry.get("leg_verdicts")
        if not isinstance(disk_lv, dict) or not isinstance(mem_lv, dict):
            continue
        for leg, dv in disk_lv.items():
            mv = mem_lv.get(leg)
            if isinstance(dv, dict) and dv.get("broadcasted") and isinstance(mv, dict):
                mv["broadcasted"] = True


def _save_pending_cache(cache: dict) -> None:
    # Evict first, on the caller's dict, so the deletions register as *ours* in the merge below
    # rather than looking like keys we simply never had.
    _evict_stale(cache)
    snapshot = getattr(cache, "_snapshot", None)
    with _cache_lock():
        disk = _read_raw()
        merged = _merge_onto_disk(cache, disk, snapshot)
        _merge_broadcasted_flags(merged, disk)
        # Per-process temp name: a shared "parse_cache.json.tmp" lets two concurrent writers
        # collide, one renaming it away while the other is still using it — which raises
        # FileNotFoundError out of os.replace() and kills that run mid-write. The lock already
        # serialises writers; this keeps the degraded unlocked path safe too.
        tmp = f"{_PENDING_CACHE_PATH}.tmp.{os.getpid()}"
        try:
            with open(tmp, "w") as f:
                json.dump(merged, f)
            os.replace(tmp, _PENDING_CACHE_PATH)
        finally:
            if os.path.exists(tmp):
                os.unlink(tmp)
    # Re-baseline: callers (notably the grade daemon) save several times from one load, and a
    # later save must only assert what changed since this one — otherwise it would re-apply
    # these same keys and clobber another process's newer writes to them.
    if isinstance(cache, _PendingCache):
        cache._snapshot = copy.deepcopy(dict(cache))


def _evict_stale(cache: dict) -> None:
    """Remove entries that are fully resolved and older than _EVICT_AFTER_DAYS.

    Only evicts:
      - Fully resolved entries (all legs WIN/LOSS/PUSH) whose newest game_date is old
      - _dupe / _failed markers whose primary entry has already been evicted
      - Non-dict entries (corrupt)
    """
    stale_keys = []
    for key, entry in cache.items():
        if not isinstance(entry, dict):
            stale_keys.append(key)
            continue
        # _dupe markers: only evict if the primary entry is gone
        if entry.get("_dupe"):
            primary_key = f"{key.split(':')[0]}:{entry.get('primary_id', '')}"
            if primary_key not in cache:
                stale_keys.append(key)
            continue
        # _failed markers: skip — they're cheap and prevent re-notifying audit
        if entry.get("_failed"):
            continue
        leg_verdicts = entry.get("leg_verdicts", {})
        if not leg_verdicts:
            continue
        # Keep entries that still have unresolved legs
        all_resolved = all(
            isinstance(v, dict) and v.get("verdict") in ("WIN", "LOSS", "PUSH")
            for v in leg_verdicts.values()
        )
        if not all_resolved:
            continue
        # Check age: use the most recent game_date among legs
        dates = [v.get("game_date", "") for v in leg_verdicts.values() if isinstance(v, dict)]
        if not dates:
            continue
        try:
            newest = max(datetime.fromisoformat(d) for d in dates if d)
            if datetime.now() - newest > timedelta(days=_EVICT_AFTER_DAYS):
                stale_keys.append(key)
        except (ValueError, TypeError):
            pass
    for key in stale_keys:
        del cache[key]
