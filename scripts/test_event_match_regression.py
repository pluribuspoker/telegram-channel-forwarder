#!/usr/bin/env python3
"""Regression: a pick must never be priced off a different game.

Pins the NFL-preseason failure of 2026-08-06. The Odds API files preseason under
its own sport key, so "Panthers / Cardinals UNDER 35.5" found no event in
americanfootball_nfl — and _find_event_id did not fail closed. It scored every
event sharing a team name and returned the nearest one, Bears @ Panthers in Week
1 (total 46.5), whose alternate Under 35.5 is +320. The real preseason total was
34, i.e. about -125. Trent's "CARDINALS ML +100" bound to Cardinals @ Chargers
five weeks out and printed +455.

Fully offline — fixtures only, no API calls, no DB.

    python scripts/test_event_match_regression.py
"""
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from odds import _find_event_id, _snapshot_time, _sport_key_candidates  # noqa: E402

PRESEASON = [
    {"id": "pre_car_ari", "sport_key": "americanfootball_nfl_preseason",
     "commence_time": "2026-08-07T00:00:00Z",
     "home_team": "Arizona Cardinals", "away_team": "Carolina Panthers"},
]
REGULAR = [
    {"id": "reg_chi_car", "sport_key": "americanfootball_nfl",
     "commence_time": "2026-09-13T17:00:00Z",
     "home_team": "Carolina Panthers", "away_team": "Chicago Bears"},
    {"id": "reg_ari_lac", "sport_key": "americanfootball_nfl",
     "commence_time": "2026-09-13T20:25:00Z",
     "home_team": "Los Angeles Chargers", "away_team": "Arizona Cardinals"},
    {"id": "reg_sea_ari", "sport_key": "americanfootball_nfl",
     "commence_time": "2026-09-20T20:25:00Z",
     "home_team": "Arizona Cardinals", "away_team": "Seattle Seahawks"},
]
# A fight card posted five days out — routine for UFC, and the case a naive
# "reject events far from the pick date" guard would break.
MMA = [
    {"id": "mma_goff_miller", "sport_key": "mma_mixed_martial_arts",
     "commence_time": "2026-08-09T00:00:00Z",
     "home_team": "Ty Miller", "away_team": "Billy Goff"},
    {"id": "mma_later", "sport_key": "mma_mixed_martial_arts",
     "commence_time": "2026-08-16T02:00:00Z",
     "home_team": "Islam Makhachev", "away_team": "Ian Garry"},
]

AUG6 = _snapshot_time("2026-08-06")
failures = []


def check(label, got, want):
    ok = got == want
    print(f"[{'PASS' if ok else 'FAIL'}] {label}" + ("" if ok else f"\n        got {got!r}, want {want!r}"))
    if not ok:
        failures.append(label)


# 1. The merged list resolves the preseason game exactly.
check("preseason total picks its own game, not Week 1",
      _find_event_id(REGULAR + PRESEASON, ["Carolina Panthers", "Arizona Cardinals"], as_of=AUG6),
      "pre_car_ari")

check("preseason moneyline picks its own game, not Week 1",
      _find_event_id(REGULAR + PRESEASON, ["Arizona Cardinals"], as_of=AUG6),
      "pre_car_ari")

# 2. Without the preseason list, a two-team pick fails closed rather than
#    pricing a game that holds only one of them.
check("regular-season-only list yields no game, not the wrong game",
      _find_event_id(REGULAR, ["Carolina Panthers", "Arizona Cardinals"], as_of=AUG6),
      None)

# 3. ...but only when the missing team is demonstrably on the schedule. A name
#    variant _team_matches cannot resolve must still fall through to the partial
#    match, or this drops odds we get right today.
check("unresolvable second name still allows the partial match",
      _find_event_id(REGULAR, ["Carolina Panthers", "Bantam Rugby Club"], as_of=AUG6),
      "reg_chi_car")

# 4. Scoring is relative to the pick's date, not to now. Once the preseason game
#    kicks off, "prefer not-yet-started" would otherwise hand the pick to Week 1.
check("already-kicked-off game still wins for its own date",
      _find_event_id(REGULAR + PRESEASON, ["Arizona Cardinals"],
                     as_of=datetime(2026, 8, 7, 2, 0, tzinfo=timezone.utc)),
      "pre_car_ari")

# 5. A card five days after the post still matches — no date window.
check("fight card 5 days out still matches",
      _find_event_id(MMA, ["Ty Miller"], as_of=_snapshot_time("2026-08-04")),
      "mma_goff_miller")

# 6. Both NFL keys are consulted; other sports are untouched.
check("NFL consults preseason key",
      _sport_key_candidates("NFL"),
      ["americanfootball_nfl", "americanfootball_nfl_preseason"])
check("MLB consults spring training",
      _sport_key_candidates("MLB"),
      ["baseball_mlb", "baseball_mlb_preseason"])
check("a sport with no second phase is untouched",
      _sport_key_candidates("CFL"), ["americanfootball_cfl"])
check("unknown sport yields no keys", _sport_key_candidates("Cricket"), [])

print(f"\n{10 - len(failures)}/10 passed")
sys.exit(1 if failures else 0)
