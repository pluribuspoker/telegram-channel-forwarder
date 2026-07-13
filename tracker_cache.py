import json
import os
import re
from datetime import datetime, timedelta, timezone

_PENDING_CACHE_PATH = os.path.join(os.path.dirname(__file__), "parse_cache.json")
_EVICT_AFTER_DAYS = 14


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


def _load_pending_cache() -> dict:
    try:
        with open(_PENDING_CACHE_PATH) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def _merge_broadcasted_flags(cache: dict) -> None:
    """Preserve `broadcasted: True` flags set on disk since `cache` was loaded.

    The tracker loads the whole cache once per run and rewrites it in full,
    while the grade daemon concurrently sets `broadcasted=True` on legs it has
    just broadcast. Without this merge, the tracker's full-dict write clobbers
    those flags, so the daemon re-broadcasts next cycle (duplicate result).

    `broadcasted` is monotonic (a leg broadcasts exactly once, False→True), so
    OR-ing the on-disk flag into the in-memory copy is always safe: it can only
    suppress a duplicate, never drop a legitimate broadcast. Only legs still
    present in memory are touched — a leg the caller intentionally cleared
    (e.g. a forced re-grade) is left cleared.
    """
    disk = _load_pending_cache()
    for key, entry in cache.items():
        if not isinstance(entry, dict):
            continue
        disk_lv = disk.get(key, {}).get("leg_verdicts")
        mem_lv = entry.get("leg_verdicts")
        if not isinstance(disk_lv, dict) or not isinstance(mem_lv, dict):
            continue
        for leg, dv in disk_lv.items():
            mv = mem_lv.get(leg)
            if isinstance(dv, dict) and dv.get("broadcasted") and isinstance(mv, dict):
                mv["broadcasted"] = True


def _save_pending_cache(cache: dict) -> None:
    _evict_stale(cache)
    _merge_broadcasted_flags(cache)
    tmp = _PENDING_CACHE_PATH + ".tmp"
    with open(tmp, "w") as f:
        json.dump(cache, f)
    os.replace(tmp, _PENDING_CACHE_PATH)


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
