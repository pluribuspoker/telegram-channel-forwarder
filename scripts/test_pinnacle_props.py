"""Regression test: NFL completions props price free (Bovada phrase + Pinnacle specials).

Self-contained (no network) — run it directly:

    ~/venv/bin/python scripts/test_pinnacle_props.py

The incident (2026-10-08, WagerStalk "Jalon Daniels O 17.5 completions"):
prop_stat "COMP" had no key anywhere, so the leg stored
prop_stat_unsupported(COMP) — and Bovada's only completions line was 18.5,
while Pinnacle's guest feed quoted the capper's exact 17.5 (-127 / +105) as a
"Player Props" special. Fixtures are the real payloads captured ~50 min before
TB@DAL kickoff (never retyped): Pinnacle league 889 trimmed to three specials
(Jalon Daniels completions + pass attempts, Jayden Daniels completions — the
near-namesake guard), Bovada's TB@DAL event trimmed to "Passing Props".
"""
import asyncio
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import odds
from odds import (
    _BOVADA_PROP_KEY,
    _bovada_prop_markets,
    _bovada_prop_phrases,
    _lookup_prop,
    _pinnacle_prop_phrases,
    _pinnacle_prop_price,
    fetch_odds_current,
)

FIX = Path(__file__).resolve().parent / "fixtures"
PIN = json.load(open(FIX / "pinnacle_nfl_props_tb_dal_20261008.json"))
BOV = json.load(open(FIX / "bovada_nfl_comp_tb_dal_20261008.json"))
PRE = datetime(2026, 10, 8, 23, 30, tzinfo=timezone.utc)     # 45 min pregame
LIVE = datetime(2026, 10, 9, 1, 0, tzinfo=timezone.utc)
TB = ["Tampa Bay Buccaneers"]

failures: list[str] = []


def check(name, got, want):
    ok = got == want
    print(f"{'✅' if ok else '❌'} {name}: {got!r}" + ("" if ok else f" (want {want!r})"))
    if not ok:
        failures.append(name)


def pin(player, stat, direction, line, teams=TB, now=PRE):
    return _pinnacle_prop_price(PIN["matchups"], PIN["markets"], teams, player,
                                _pinnacle_prop_phrases("NFL", stat), direction, line, now)


def test_phrases():
    for spelling in ("COMP", "comp", "Completions", "pass comp", "PASSING_COMPLETIONS", "CMP"):
        check(f"Bovada {spelling!r}", _bovada_prop_phrases("NFL", spelling), ("Total Completions",))
        check(f"Pinnacle {spelling!r}", _pinnacle_prop_phrases("NFL", spelling), ("Total Pass Completions",))
    check("Pinnacle props NFL-only (NCAAF unmapped)", _pinnacle_prop_phrases("NCAAF", "COMP"), ())


def test_bovada_main_line_only():
    bk = _bovada_prop_markets(BOV, _bovada_prop_phrases("NFL", "COMP"), "Jalon Daniels")
    check("Bovada 18.5 prices exact", _lookup_prop(bk, "Jalon Daniels", _BOVADA_PROP_KEY, "over", 18.5)["adjusted_odds"], 105)
    check("Bovada has no 17.5", _lookup_prop(bk, "Jalon Daniels", _BOVADA_PROP_KEY, "over", 17.5)["adjusted_odds"], None)


def test_pinnacle_special():
    check("Jalon o17.5 = -127", pin("Jalon Daniels", "COMP", "over", 17.5), (-127, "2026-10-09T00:15:00Z"))
    check("Jalon u17.5 = +105", pin("Jalon Daniels", "COMP", "under", 17.5)[0], 105)
    check("no teams still prices", pin("Jalon Daniels", "COMP", "over", 17.5, teams=[])[0], -127)
    check("other line = no price", pin("Jalon Daniels", "COMP", "over", 18.5), None)
    check("wrong game (Kansas) = no price", pin("Jalon Daniels", "COMP", "over", 17.5, teams=["Kansas Jayhawks"]), None)
    check("namesake Jayden ≠ Jalon", pin("Jayden Daniels", "COMP", "over", 17.5, teams=[]), None)
    check("Jayden's own line", pin("Jayden Daniels", "COMP", "over", 18.5, teams=[])[0], 101)
    check("stat must match (pass attempts ≠ completions)", pin("Jalon Daniels", "COMP", "over", 28.5), None)
    check("pass attempts own phrase", pin("Jalon Daniels", "PASS_ATT", "over", 28.5)[0], -118)
    check("post-kickoff = no price", pin("Jalon Daniels", "COMP", "over", 17.5, now=LIVE), None)


def test_flow():
    """fetch_odds_current: Bovada misses 17.5 → Pinnacle exact, zero paid calls."""
    paid = {"n": 0}

    async def _events(sport, conn):
        return [{"id": "ev1", "sport_key": "americanfootball_nfl", "home_team": "Dallas Cowboys",
                 "away_team": "Tampa Bay Buccaneers", "commence_time": "2026-10-09T00:15:00Z"}]

    async def _bov_events(sport):
        return [BOV]

    async def _pin_raw(league):
        return PIN["matchups"], PIN["markets"]

    async def _paid(*a, **k):
        paid["n"] += 1
        return []

    orig_prop, orig_pin = odds._fetch_bovada_prop, odds._pinnacle_prop_odds

    async def _prop_frozen(*a, **k):
        k.setdefault("now", PRE)
        return await orig_prop(*a, **k)

    async def _pin_frozen(*a, **k):
        k.setdefault("now", PRE)
        return await orig_pin(*a, **k)

    saved = (odds._fetch_current_event_list_all, odds._fetch_bovada_events, odds._fetch_pinnacle_league_raw,
             odds._fetch_current_bookmakers, odds._fetch_bovada_prop, odds._pinnacle_prop_odds,
             odds._event_already_started)
    odds._fetch_current_event_list_all = _events
    odds._fetch_bovada_events = _bov_events
    odds._fetch_pinnacle_league_raw = _pin_raw
    odds._fetch_current_bookmakers = _paid
    odds._fetch_bovada_prop = _prop_frozen
    odds._pinnacle_prop_odds = _pin_frozen
    odds._event_already_started = lambda event_list, event_id: False
    pick = {"bet_type": "prop", "player": "Jalon Daniels", "prop_stat": "COMP", "direction": "over",
            "line": 17.5, "period": "game", "teams": TB,
            "description": "Jalon Daniels over 17.5 completions"}
    try:
        r = asyncio.run(fetch_odds_current("NFL", pick, db_path=":memory:"))
        check("flow: Pinnacle exact", (r.match_type, r.odds, r.bookmaker), ("exact", -127, "pinnacle"))
        check("flow: commence recorded", r.commence_time, "2026-10-09T00:15:00Z")
        r = asyncio.run(fetch_odds_current("NFL", {**pick, "line": 18.5}, db_path=":memory:"))
        check("flow: Bovada still first on its line", (r.odds, r.bookmaker), (105, "bovada"))
        r = asyncio.run(fetch_odds_current("NFL", {**pick, "line": 16.5}, db_path=":memory:", free_only=True))
        check("flow: unlisted line = retryable miss", r.match_type, "prop_not_found")
        check("flow: no paid calls", paid["n"], 0)
    finally:
        (odds._fetch_current_event_list_all, odds._fetch_bovada_events, odds._fetch_pinnacle_league_raw,
         odds._fetch_current_bookmakers, odds._fetch_bovada_prop, odds._pinnacle_prop_odds,
         odds._event_already_started) = saved


def main():
    test_phrases()
    test_bovada_main_line_only()
    test_pinnacle_special()
    test_flow()
    print()
    if failures:
        print(f"❌ {len(failures)} failure(s): {failures}")
        sys.exit(1)
    print("✅ all pinnacle prop checks passed")


if __name__ == "__main__":
    main()
