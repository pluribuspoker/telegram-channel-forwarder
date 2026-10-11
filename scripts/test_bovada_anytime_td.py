"""Regression test: Bovada free anytime-TD-scorer props (real capture, never retyped).

Self-contained (no Telegram/API) — run it directly:

    ~/venv/bin/python scripts/test_bovada_anytime_td.py

Dagger's "Trey McBride ATD +110 2U" (-1002486251914:4014, 2026-10-11) parsed
prop_stat=ATD and missed as prop_stat_unsupported — only the first-TD scorer
market was wired. Bovada's "TD Scorer Props" group lists "Anytime Touchdown
Scorer" three times (Game EVEN, First Half +260, 1st Quarter +625 for
McBride — same name, different period) beside "Either Player - Anytime
Touchdown Scorer" (a two-player bet that must never match).

Fixture: the Bovada NFL coupon event Detroit Lions @ Arizona Cardinals
captured 2026-10-11T20:07Z, trimmed to its Game Lines + TD Scorer Props groups.
"""
import asyncio
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import odds
from odds import _bovada_scorer_market, _bovada_scorer_markets, fetch_odds_current

FIXTURE = Path(__file__).resolve().parent / "fixtures" / "bovada_nfl_anytime_td_20261011.json"
EVENT = json.load(open(FIXTURE))
FROZEN = datetime.fromtimestamp(int(EVENT["startTime"]) / 1000, tz=timezone.utc) - timedelta(hours=1)

failures = []


def check(label, got, want):
    ok = got == want
    print(f"{'✅' if ok else '❌'} {label}: {got!r}" + ("" if ok else f" (want {want!r})"))
    if not ok:
        failures.append(label)


# The cached parse, verbatim.
MCBRIDE = {"description": "Trey McBride anytime touchdown scorer +110", "sport": None,
           "bet_type": "prop", "is_parlay_leg": False, "period": "game",
           "teams": ["Arizona Cardinals"], "player": "Trey McBride", "prop_stat": "ATD",
           "line": None, "direction": None}
ATD = "Anytime Touchdown Scorer"


def test_recognizer():
    for stat, desc in (("ATD", "x"), ("ANYTIME_TD", "x"), ("Anytime TD", "x"),
                       ("TDS", "Trey McBride anytime touchdown scorer"),
                       ("TD", "Trey McBride any time TD"), ("ATTS", "x")):
        check(f"recognizes {stat!r}/{desc!r}",
              _bovada_scorer_market("NFL", {**MCBRIDE, "prop_stat": stat, "description": desc}), ATD)
    check("first TD still first TD",
          _bovada_scorer_market("NFL", {**MCBRIDE, "prop_stat": "FTTD", "description": "x"}),
          "First Touchdown Scorer")
    check("line 1.5 (2+ TDs) is not anytime",
          _bovada_scorer_market("NFL", {**MCBRIDE, "line": 1.5, "direction": "over"}), None)
    check("line 0.5 over is anytime",
          _bovada_scorer_market("NFL", {**MCBRIDE, "line": 0.5, "direction": "over"}), ATD)
    check("1H period → None", _bovada_scorer_market("NFL", {**MCBRIDE, "period": "1h"}), None)
    check("'No' side → None", _bovada_scorer_market("NFL", {**MCBRIDE, "direction": "no"}), None)
    check("unmapped sport → None", _bovada_scorer_market("NCAAF", MCBRIDE), None)
    check("receptions prop untouched",
          _bovada_scorer_market("NFL", {**MCBRIDE, "prop_stat": "RECEPTIONS",
                                        "description": "Trey McBride over 6.5 receptions"}), None)


def test_shaper():
    bk = _bovada_scorer_markets(EVENT, ATD, "Trey McBride")
    outs = bk[0]["markets"][0]["outcomes"] if bk else []
    check("McBride anytime TD = EVEN (game only, not 1H +260 / Q1 +625 / either-player)",
          [o["price"] for o in outs], [100])
    check("unknown player → []", _bovada_scorer_markets(EVENT, ATD, "Caitlin Clark"), [])


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
        r = asyncio.run(fetch_odds_current("NFL", MCBRIDE, db_path=":memory:", free_only=True))
        check("F1: priced free via Bovada", (r.match_type, r.odds, r.bookmaker), ("exact", 100, "bovada"))
        r = asyncio.run(fetch_odds_current("NFL", {**MCBRIDE, "teams": []}, db_path=":memory:", free_only=True))
        check("F2: player scan without teams", r.odds, 100)
        r = asyncio.run(fetch_odds_current("NFL", MCBRIDE, db_path=":memory:", free_only=False))
        check("F3: paid-allowed path still prices free", (r.odds, r.bookmaker), (100, "bovada"))
        r = asyncio.run(fetch_odds_current("NFL", {**MCBRIDE, "player": "Caitlin Clark"},
                                           db_path=":memory:", free_only=True))
        check("F4: unlisted player is a retryable miss", r.match_type, "prop_not_found")
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
    print("✅ all bovada anytime TD checks passed")


if __name__ == "__main__":
    main()
