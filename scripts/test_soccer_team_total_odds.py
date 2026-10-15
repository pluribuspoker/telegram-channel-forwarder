"""Regression test: soccer team totals price free from Pinnacle's league feed.

Self-contained (no network) — run it directly:

    ~/venv/bin/python scripts/test_soccer_team_total_odds.py

The incident (2026-10-15, James "Benfica TT o2.5"): every soccer team total
ended team_total_unavailable — ESPN's DraftKings block is ML/handicap/total
only and Bovada has no soccer. Pinnacle's league feed lists team totals (main
line + alternate ladder) per side. The fixture is the real Champions League
(league 2627) payload captured 2026-10-15, trimmed to two games, plus ESPN's
real event for AEK Athens at Manchester City (uid league 775).
"""
import asyncio
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import odds

FIX = json.load(open(Path(__file__).resolve().parent / "fixtures"
                     / "pinnacle_soccer_team_total_mci_aek_20261015.json"))
NOW = datetime(2026, 10, 15, 19, 10, tzinfo=timezone.utc)
fails = 0


def check(name, got, want):
    global fails
    ok = got == want
    fails += not ok
    print(f"{'PASS' if ok else 'FAIL'} {name}: got {got!r} want {want!r}")


def pick(team, line, direction="over", period="game"):
    return {"description": f"{team} team total {direction} {line}", "bet_type": "team_total",
            "teams": [team], "period": period, "line": line, "direction": direction,
            "player": None, "prop_stat": None}


def run(p, state="pre", uid=None, date=None, raw=None, now=NOW):
    ev = json.loads(json.dumps(FIX["espn_event"]))
    ev["status"]["type"]["state"] = state
    if uid:
        ev["uid"] = uid
    if date:
        ev["date"] = date

    async def bind(teams, d, desc="", ref=None, **kw):
        return ev, "bound", d

    async def league_raw(league):
        return raw if raw is not None else (
            (FIX["matchups"], FIX["markets"]) if league == FIX["league"] else ([], []))

    real_now = odds.datetime
    odds.soccer_bind, odds._fetch_pinnacle_league_raw = bind, league_raw

    class _Clock(real_now):
        @classmethod
        def now(cls, tz=None):
            return now if tz is None or tz == timezone.utc else now.astimezone(tz)

    odds.datetime = _Clock
    try:
        r = asyncio.run(odds._soccer_espn_odds(p, p["teams"], today="2026-10-20"))
    finally:
        odds.datetime = real_now
    return r.match_type, r.odds, r.bookmaker


# Main line, alternate line, and the away side's own total.
check("home main 3.5 over", run(pick("Manchester City", 3.5)), ("exact", -102, "pinnacle"))
check("home alt 2.5 over", run(pick("Manchester City", 2.5)), ("exact", -288, "pinnacle"))
check("home alt 4.5 under", run(pick("Manchester City", 4.5, "under")), ("exact", -328, "pinnacle"))
check("away 0.5 over", run(pick("AEK Athens", 0.5)), ("exact", 170, "pinnacle"))
# Soccer = exact line only: no estimate off a neighbouring line.
check("off-ladder line is a miss", run(pick("Manchester City", 5.5))[1], None)
# Periods Pinnacle maps nothing for in soccer fail closed (no full-game price on a 1H bet).
check("1H team total not priced", run(pick("Manchester City", 1.5, period="1h"))[1], None)
# Started game: no live source, no Pinnacle call.
check("started game", run(pick("Manchester City", 2.5), state="in")[:2], ("game_in_progress", None))
# Unmapped ESPN league → ESPN's own verdict, never a guess.
check("unmapped league", run(pick("Manchester City", 2.5), uid="s:600~l:99999~e:1")[:2],
      ("team_total_unavailable", None))
# The club's Pinnacle game must be the ESPN-bound one (kickoff within 3 h).
check("other-day game refused", run(pick("Manchester City", 2.5), date="2026-10-25T14:00Z")[:2],
      ("no_game", None))
# Mapped league, game not listed yet → retryable no_game.
check("not listed yet", run(pick("Manchester City", 2.5), raw=([], []))[:2], ("no_game", None))

sys.exit(1 if fails else 0)
