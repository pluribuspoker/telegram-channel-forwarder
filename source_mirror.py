"""Mirror a forwarded pick's odds tag + result emoji back onto its SOURCE post.

Opt-in per mapping with `"grade_source": true` (DAGGER, 2026-09-29: the
operator's own channel should show results like its Fight Club Picks copy).
Sources are otherwise read-only — every tag and emoji lands on the dest copy.

Runs at the end of each live tracker pass (it needs Telethon: the source post
belongs to the userbot account, which the Bot API can't edit). The source is
rebuilt from its OWN live text through the same `_insert_odds` →
`_insert_emojis` path the dest copy gets, so a `source_label` suffix or other
dest-side decoration never leaks back. Both inserts are idempotent, so a
re-run over an already-marked post is a no-op.

State lives in `logs/source_mirror_state.json` (source_key → signature of the
odds + verdicts last applied), NOT on the parse-cache entry: `_pending_entry`
rebuilds entries from an allowlist and would drop an unknown key. A source is
fetched only when its signature changes, i.e. about twice per pick (odds, result).
"""

import json
import os
from datetime import date as _date, timedelta

from tracker_format import _insert_emojis, _insert_odds, _user_edit_message, to_bot_html

STATE_PATH = os.path.join(os.path.dirname(__file__), "logs", "source_mirror_state.json")
MIRROR_DAYS = 7          # stop looking at posts older than this
_RESOLVED = ("WIN", "LOSS", "PUSH")


def grade_source_mapping_ids() -> frozenset:
    """Mapping ids flagged `"grade_source": true` in MAPPINGS_CONFIG."""
    return frozenset(
        m["id"] for m in json.loads(os.getenv("MAPPINGS_CONFIG", "[]"))
        if m.get("grade_source") and m.get("id")
    )


def _load_state() -> dict:
    try:
        with open(STATE_PATH, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def _save_state(state: dict) -> None:
    os.makedirs(os.path.dirname(STATE_PATH), exist_ok=True)
    tmp = f"{STATE_PATH}.{os.getpid()}.tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(state, f)
    os.replace(tmp, STATE_PATH)


def _signature(entry: dict, skip_odds: bool) -> str:
    odds = {} if skip_odds else {k: v["odds"] for k, v in entry.get("odds_by_pick", {}).items()
                                 if isinstance(v, dict) and v.get("odds") is not None}
    verdicts = {k: (v or {}).get("verdict") for k, v in entry.get("leg_verdicts", {}).items()
                if isinstance(v, dict) and v.get("verdict") in _RESOLVED}
    return json.dumps([odds, verdicts], sort_keys=True)


def plan_source_syncs(cache: dict, mapping_ids: frozenset, state: dict,
                      skip_odds_channels: set[int] = frozenset(),
                      today: _date | None = None) -> list[dict]:
    """Sources whose dest copy carries odds/verdicts not yet mirrored.

    A dest `_dupe` marker (the same pick was already in the dest, e.g. posted by
    hand) mirrors from its primary entry. One plan per source, even when the
    source fans out to several dests.
    """
    if not mapping_ids:
        return []
    cutoff = ((today or _date.today()) - timedelta(days=MIRROR_DAYS)).isoformat()
    plans: dict[str, dict] = {}
    for key, entry in cache.items():
        if not isinstance(entry, dict) or entry.get("mapping_id") not in mapping_ids:
            continue
        source_key = entry.get("_source_key")
        if not source_key or source_key in plans:
            continue
        channel = key.split(":")[0]
        data = entry
        if entry.get("_dupe"):
            data = cache.get(f"{channel}:{entry.get('primary_id')}")
            if not isinstance(data, dict):
                continue
        if "parsed" not in data or (data.get("_failed") and data.get("_failed_reason")):
            continue
        if (data.get("msg_date") or "9999") < cutoff:
            continue
        skip_odds = int(channel) in skip_odds_channels
        sig = _signature(data, skip_odds)
        if sig == _signature({}, skip_odds) or state.get(source_key) == sig:
            continue          # nothing to show yet, or already mirrored
        plans[source_key] = {"source_key": source_key, "dest_key": key, "entry": data,
                             "skip_odds": skip_odds, "sig": sig}
    return list(plans.values())


def render_source(src_html: str, entry: dict, skip_odds: bool = False) -> str:
    """The source post's text with the dest copy's odds tags + verdict emojis."""
    parsed = entry.get("parsed") or {}
    picks = parsed.get("picks", [])
    sport = parsed.get("sport")
    html = src_html if skip_odds else _insert_odds(src_html, picks, entry.get("odds_by_pick", {}))
    verdicts = []
    for i, pick in enumerate(picks):
        lv = entry.get("leg_verdicts", {}).get(str(i))
        if isinstance(lv, dict) and lv.get("verdict") in _RESOLVED:
            verdicts.append((pick, lv["verdict"], lv.get("calc", ""), lv.get("sport") or sport))
        else:
            verdicts.append((pick, "PENDING", "", pick.get("sport") or sport))
    return _insert_emojis(html, verdicts)


async def sync_source_mirrors(client, cache: dict, skip_odds_channels: set[int] = frozenset()) -> int:
    """Edit every flagged source post that's behind its dest copy. Returns edits made."""
    state = _load_state()
    plans = plan_source_syncs(cache, grade_source_mapping_ids(), state, skip_odds_channels)
    edits = 0
    for p in plans:
        ch, _, mid = p["source_key"].partition(":")
        chat = int(f"-100{ch}")
        try:
            msg = await client.get_messages(chat, ids=int(mid))
        except Exception as exc:
            print(f"  [source mirror] {p['source_key']}: fetch failed — {exc}")
            continue
        if msg is None:
            print(f"  [source mirror] {p['source_key']}: source post gone, dropping")
            state[p["source_key"]] = p["sig"]
            continue
        src_html = to_bot_html(msg.raw_text or "", msg.entities)
        new_html = render_source(src_html, p["entry"], p["skip_odds"])
        if new_html != src_html:
            if not await _user_edit_message(client, chat, msg.id, new_html):
                continue      # retried next run — state not advanced
            edits += 1
            print(f"  [source mirror] {p['source_key']} ← {p['dest_key']}")
        state[p["source_key"]] = p["sig"]
    if plans:
        _save_state(state)
    return edits
