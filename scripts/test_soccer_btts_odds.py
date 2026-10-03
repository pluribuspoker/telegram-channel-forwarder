"""Regression test: soccer BTTS prices free from Pinnacle's league feed.

Self-contained (no network) — run it directly:

    ~/venv/bin/python scripts/test_soccer_btts_odds.py

The incident (2026-10-03, James Bets "Croatia / England BTTS"): every soccer
BTTS pick ended prop_stat_unsupported(BTTS) — ESPN's DraftKings block is
ML/handicap/total only and Bovada has no soccer. Pinnacle's guest API lists
"Both Teams To Score?" as a Yes/No special child of the game in the league
feed (Yes -161 / No +137 here). The fixture is the real league-200719 payload
captured 8 min before kickoff, trimmed to this game + one other (never
retyped), plus ESPN's event (uid league 2395 = Nations League).
"""
import asyncio
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import odds

FIX = json.load(open(Path(__file__).resolve().parent / "fixtures"
                     / "pinnacle_soccer_btts_cro_eng_20261003.json"))
NOW = datetime(2026, 10, 3, 15, 52, 32, tzinfo=timezone.utc)
TEAMS = ["Croatia", "England"]
fails = 0


def check(name, got, want):
    global fails
    ok = got == want
    fails += not ok
    print(f"{'PASS' if ok else 'FAIL'} {name}: got {got!r} want {want!r}")


def pick(desc, period="game", direction=None):
    return {"description": desc, "bet_type": "prop", "prop_stat": "BTTS", "teams": TEAMS,
            "period": period, "line": None, "direction": direction, "player": None}


def run(p, state="pre", uid=None, raw=None):
    ev = json.loads(json.dumps(FIX["espn_event"]))
    ev["status"]["type"]["state"] = state
    if uid:
        ev["uid"] = uid

    async def bind(teams, date, desc="", ref=None, **kw):
        return ev, "bound", date

    async def league_raw(league):
        return raw if raw is not None else (
            (FIX["matchups"], FIX["markets"]) if league == FIX["league"] else ([], []))

    odds.soccer_bind, odds._fetch_pinnacle_league_raw = bind, league_raw
    return asyncio.run(odds._soccer_btts_odds(p, TEAMS, today="2026-10-03", now=NOW))


r = run(pick("Croatia vs England — Both Teams To Score (Yes)"))
check("BTTS yes price", (r.odds, r.bookmaker, r.match_type), (-161, "pinnacle", "pinnacle_btts"))
check("BTTS yes game_date/commence", (r.game_date, r.commence_time), ("2026-10-03", "2026-10-03T16:00Z"))
check("BTTS no", run(pick("Croatia vs England — Both Teams To Score (No)")).odds, 137)
check("BTTS no via direction", run(pick("Croatia vs England BTTS", direction="no")).odds, 137)
check("BTTS 1H yes (period-1 market)", run(pick("Croatia vs England — 1H BTTS (Yes)", "1h")).odds, 301)
check("unmapped period → no price", run(pick("x BTTS", "2h")).match_type, "no_game")
check("started game → game_in_progress", run(pick("x BTTS"), state="in").match_type, "game_in_progress")
check("unmapped ESPN league → None", run(pick("x BTTS"), uid="s:600~l:99999~e:1"), None)
check("league not listing it yet → retryable no_game",
      run(pick("x BTTS"), raw=([m for m in FIX["matchups"] if m["type"] == "matchup"], [])).match_type,
      "no_game")
# The other league game must never be read as this one.
other = next(m for m in FIX["matchups"] if m["type"] == "matchup" and m["id"] != 1636866914)
names = [p["name"] for p in other["participants"]]
check("other game's teams don't bind Croatia's special",
      odds._pinnacle_btts_price(FIX["matchups"], FIX["markets"], names, "game", False, NOW), None)
check("pregame only: at kickoff no price",
      odds._pinnacle_btts_price(FIX["matchups"], FIX["markets"], TEAMS, "game", False,
                                datetime(2026, 10, 3, 16, 0, tzinfo=timezone.utc)), None)

print(f"\n{'ALL PASS' if not fails else f'{fails} FAILED'}")
sys.exit(1 if fails else 0)
