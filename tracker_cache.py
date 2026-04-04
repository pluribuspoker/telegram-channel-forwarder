import json
import os
import re

_PENDING_CACHE_PATH = os.path.join(os.path.dirname(__file__), "parse_cache.json")


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
    return entry


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


def _save_pending_cache(cache: dict) -> None:
    with open(_PENDING_CACHE_PATH, "w") as f:
        json.dump(cache, f)
