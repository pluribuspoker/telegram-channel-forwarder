"""Regression test: Bovada free first-TD-scorer props (real capture, never retyped).

Self-contained (no Telegram/API) — run it directly:

    ~/venv/bin/python scripts/test_bovada_scorer_props.py

Trent's "JOSH DOWNS +1000" first-TD bet (-1004394797084:138, 2026-10-04)
parsed prop_stat=FTTD and missed as prop_stat_unsupported — the fifth such
post, each with a different stat spelling. Bovada's "TD Scorer Props" group
lists "First Touchdown Scorer" as one yes-only outcome per player
("Josh Downs (IND)" +900), beside the team-scoped "First Touchdown Scorer -
Indianapolis Colts" (+500, a different bet that must never match).

Fixture: the Bovada NFL coupon event Indianapolis Colts @ Washington
Commanders (London) captured 2026-10-04T12:24Z, trimmed to its Game Lines +
TD Scorer Props groups.
"""
import asyncio
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import odds
from odds import _bovada_scorer_market, _bovada_scorer_markets, fetch_odds_current

FIXTURE = Path(__file__).resolve().parent / "fixtures" / "bovada_nfl_first_td_20261004.json"
EVENT = json.load(open(FIXTURE))
FROZEN = datetime.fromtimestamp(int(EVENT["startTime"]) / 1000, tz=timezone.utc) - timedelta(hours=1)

failures = []


def check(label, got, want):
    ok = got == want
    print(f"{'✅' if ok else '❌'} {label}: {got!r}" + ("" if ok else f" (want {want!r})"))
    if not ok:
        failures.append(label)


DOWNS = {"description": "Josh Downs First Touchdown Scorer (+1000) - London game",
         "bet_type": "prop", "period": "game", "teams": ["Indianapolis Colts"],
         "player": "Josh Downs", "prop_stat": "FTTD", "line": None, "direction": None}


def test_recognizer():
    # Every spelling seen in the cache (Trent 109/112/121/124/138).
    for stat in ("FTTD", "FIRST TD SCORER", "1st Touchdown", "1st TD Scorer", "First Touchdown Scorer"):
        check(f"recognizes {stat!r}",
              _bovada_scorer_market("NFL", {**DOWNS, "prop_stat": stat, "description": "x"}),
              "First Touchdown Scorer")
    check("anytime TD is not first TD",
          _bovada_scorer_market("NFL", {**DOWNS, "prop_stat": "TDS", "description": "Josh Downs anytime TD"}), None)
    check("no player → None (team to score first is another market)",
          _bovada_scorer_market("NFL", {**DOWNS, "player": None}), None)
    check("1H period → None", _bovada_scorer_market("NFL", {**DOWNS, "period": "1h"}), None)
    check("'No' side → None", _bovada_scorer_market("NFL", {**DOWNS, "direction": "no"}), None)
    check("unmapped sport → None", _bovada_scorer_market("NCAAF", DOWNS), None)
    check("first-half wording isn't first TD",
          _bovada_scorer_market("NFL", {**DOWNS, "prop_stat": "TDS",
                                        "description": "Josh Downs first half touchdown"}), None)


def test_shaper():
    bk = _bovada_scorer_markets(EVENT, "First Touchdown Scorer", "Josh Downs")
    outs = bk[0]["markets"][0]["outcomes"] if bk else []
    check("Downs first TD = +900 (not the team-scoped +500)", [o["price"] for o in outs], [900])
    check("unknown player → []", _bovada_scorer_markets(EVENT, "First Touchdown Scorer", "Caitlin Clark"), [])


def test_flow():
    paid = []

    async def _events(sport, conn):
        return []

    async def _bov_events(sport):
        return [EVENT]

    async def _paid(*a, **k):
        paid.append(a)
        return []

    orig_prop = odds._fetch_bovada_prop

    async def _prop_frozen(*a, **k):
        k.setdefault("now", FROZEN)
        return await orig_prop(*a, **k)

    saved = (odds._fetch_current_event_list_all, odds._fetch_bovada_events,
             odds._fetch_current_bookmakers, odds._fetch_bovada_prop)
    odds._fetch_current_event_list_all = _events
    odds._fetch_bovada_events = _bov_events
    odds._fetch_current_bookmakers = _paid
    odds._fetch_bovada_prop = _prop_frozen
    try:
        r = asyncio.run(fetch_odds_current("NFL", DOWNS, db_path=":memory:", free_only=True))
        check("F1: priced free via Bovada", (r.match_type, r.odds, r.bookmaker), ("exact", 900, "bovada"))
        r = asyncio.run(fetch_odds_current("NFL", {**DOWNS, "teams": []}, db_path=":memory:", free_only=True))
        check("F2: player scan without teams", r.odds, 900)
        r = asyncio.run(fetch_odds_current("NFL", {**DOWNS, "player": "Caitlin Clark"},
                                           db_path=":memory:", free_only=True))
        check("F3: unlisted player is a retryable miss", r.match_type, "prop_not_found")
        check("no paid calls", paid, [])
    finally:
        (odds._fetch_current_event_list_all, odds._fetch_bovada_events,
         odds._fetch_current_bookmakers, odds._fetch_bovada_prop) = saved


def main():
    test_recognizer()
    test_shaper()
    test_flow()
    print()
    if failures:
        print(f"❌ {len(failures)} failure(s): {failures}")
        sys.exit(1)
    print("✅ all bovada scorer prop checks passed")


if __name__ == "__main__":
    main()
