#!/usr/bin/env python3
"""Apply an operator verdict (WIN/LOSS/PUSH) from a nightly-audit card.

Invoked by the watchdog bot (deploy/claude_watchdog_bot.py) when the operator
taps a verdict button on an audit card, or manually:

    ~/venv/bin/python scripts/audit_mark.py <card_id> WIN|LOSS|PUSH

Resolves the card in logs/ungraded_audit_cards.json, writes the verdict into
every still-unresolved leg of every fan-out copy through the second-writer-safe
cache API (tracker_cache._load_pending_cache/_save_pending_cache — no daemon
stop needed), clearing `_failed` so the normal pipeline picks the entry back
up: grade-daemon emojis + broadcasts within ~10 s (the tracker's 5-min pass
covers send_as_user channels). Then parks every key in
logs/ungraded_audit_state.json so the nightly audit never retries the pick.

Never overwrites an already-settled leg (WIN/LOSS/PUSH/VOID); a second tap is
a no-op. Refuses picks older than the cache eviction horizon — a fully
resolved entry past ``_EVICT_AFTER_DAYS`` would be evicted before the daemon
could broadcast it, silently losing the verdict.

Exit: 0 applied (or nothing left to mark), 2 card not found / too old,
3 already marked, 1 unexpected error.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

CARDS_FILE = ROOT / "logs" / "ungraded_audit_cards.json"
STATE_FILE = ROOT / "logs" / "ungraded_audit_state.json"

SETTLED = ("WIN", "LOSS", "PUSH", "VOID")
# Stay safely inside tracker_cache._EVICT_AFTER_DAYS (14): a marked entry must
# survive long enough for the daemon to emoji + broadcast it.
MAX_AGE_DAYS = 12
CALC_NOTE = "operator mark via nightly-audit card"


def _leg_game_date(entry: dict, i: int, msg_date: str) -> str:
    odds = (entry.get("odds_by_pick") or {}).get(str(i))
    if isinstance(odds, dict) and odds.get("game_date"):
        return str(odds["game_date"])[:10]
    return msg_date


def newest_reference(entry: dict) -> str:
    """Newest date the entry would carry once marked — the eviction clock."""
    msg_date = str(entry.get("msg_date") or "")[:10]
    dates = [msg_date]
    for v in (entry.get("leg_verdicts") or {}).values():
        if isinstance(v, dict) and v.get("game_date"):
            dates.append(str(v["game_date"])[:10])
    picks = (entry.get("parsed") or {}).get("picks") or []
    for i in range(len(picks)):
        dates.append(_leg_game_date(entry, i, msg_date))
    return max(d for d in dates if d) if any(dates) else ""


def apply_verdict(cache: dict, keys: list[str], verdict: str) -> dict:
    """Write `verdict` into every unresolved leg of every copy, clearing the
    daemon's `_failed` retirement. Mutates `cache`; never touches a settled
    leg. Returns counts."""
    legs = skipped = copies = 0
    for key in keys:
        entry = cache.get(key)
        if not isinstance(entry, dict) or "parsed" not in entry:
            continue
        picks = (entry.get("parsed") or {}).get("picks") or []
        lv = entry.get("leg_verdicts") or {}
        msg_date = str(entry.get("msg_date") or "")[:10]
        sport_default = (entry.get("parsed") or {}).get("sport") or ""
        wrote = False
        for i, pick in enumerate(picks):
            cur = lv.get(str(i))
            if isinstance(cur, dict) and cur.get("verdict") in SETTLED:
                skipped += 1
                continue
            lv[str(i)] = {
                "verdict": verdict, "calc": CALC_NOTE,
                "sport": pick.get("sport") or sport_default,
                "game_date": _leg_game_date(entry, i, msg_date),
                "broadcasted": False,
            }
            legs += 1
            wrote = True
        if wrote:
            entry["leg_verdicts"] = lv
            entry.pop("_failed", None)
            entry.pop("_failed_reason", None)
            copies += 1
    return {"legs": legs, "copies": copies, "skipped": skipped}


def _load_json(path: Path) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def _save_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n",
                   encoding="utf-8")
    os.replace(tmp, path)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("card_id")
    ap.add_argument("verdict", choices=["WIN", "LOSS", "PUSH"])
    ap.add_argument("--cards-file", default=str(CARDS_FILE))
    ap.add_argument("--state-file", default=str(STATE_FILE))
    ap.add_argument("--today", default="", help="override 'today' (tests)")
    a = ap.parse_args(argv)

    cards_path, state_path = Path(a.cards_file), Path(a.state_file)
    cards = _load_json(cards_path)
    card = cards.get(a.card_id)
    if not isinstance(card, dict):
        print(f"❓ card {a.card_id} not found in the registry — nothing done")
        return 2
    if card.get("marked"):
        m = card["marked"]
        print(f"↩️ already marked {m.get('verdict')} at {m.get('at')} — "
              "not touching it again")
        return 3

    keys = [k for k in (card.get("keys") or []) if isinstance(k, str)]
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    today = date.fromisoformat(a.today) if a.today else date.today()
    counts = {"legs": 0, "copies": 0, "skipped": 0}

    if keys:
        from tracker_cache import _load_pending_cache, _save_pending_cache

        cache = _load_pending_cache()
        floor = (today - timedelta(days=MAX_AGE_DAYS)).isoformat()
        for key in keys:
            entry = cache.get(key)
            if isinstance(entry, dict) and "parsed" in entry:
                ref = newest_reference(entry)
                if ref and ref < floor:
                    print(f"⛔ {key} is from {ref} — past the cache eviction "
                          "horizon, a mark would vanish before broadcasting. "
                          "Use the follow-up prompt instead.")
                    return 2
        counts = apply_verdict(cache, keys, a.verdict)
        if counts["legs"]:
            _save_pending_cache(cache)

    # Park every copy — the nightly audit never touches this pick again.
    state = _load_json(state_path)
    for key in keys:
        st = state.get(key) or {}
        st["last_run"] = now
        st["last_outcome"] = f"operator_{a.verdict.lower()}"
        st["parked"] = True
        st["parked_reason"] = f"operator marked {a.verdict}"
        state[key] = st
    if keys:
        _save_json(state_path, state)

    card["marked"] = {"verdict": a.verdict, "at": now}
    cards[a.card_id] = card
    _save_json(cards_path, cards)

    if counts["legs"]:
        extra = (f", {counts['skipped']} already-settled leg(s) untouched"
                 if counts["skipped"] else "")
        print(f"✅ {a.verdict} → {counts['legs']} leg(s) across "
              f"{counts['copies']} cop{'ies' if counts['copies'] != 1 else 'y'}"
              f"{extra}. Emoji + broadcast follow via grade-daemon (~10 s; "
              "tracker covers send_as_user channels). Audit parked "
              f"{len(keys)} key(s).")
    else:
        print(f"✅ nothing left to mark — every leg already settled "
              f"(or entry gone). Card closed, {len(keys)} key(s) parked.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
