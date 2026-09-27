"""Regression test: bind-once event binding for every ESPN team sport + free soccer odds.

Self-contained (ESPN stubbed with real payloads captured 2026-09-26) — run directly:

    ~/venv/bin/python scripts/test_event_binding.py

Grading used to re-match team names against whatever date looked right on
every pass, with first-hit fallbacks to the previous/next day — so a series
(same teams on consecutive days) could grade the wrong game, and a Monday
night leg looked for on Sunday found nothing. Now the tracker binds each
team-market leg to ONE ESPN event at post time (`espn_bind` → `rank_bind`,
stored in entry["espn_events"]) and every pass grades that id:

  1. series: the post time picks the game — before game 1 → game 1; posted
     during game 1 (a live bet) → game 1; the next morning → game 2
  2. the odds match's commence_time anchors it when known
  3. lookahead: a Sunday-dated MNF leg binds Monday's game (window +3 days)
  4. different teams tying ("Los Angeles ML": Dodgers AND Angels) → ambiguous
  5. bound_scoreboard: pregame = zero fetches; after kickoff a one-event
     slate; build_context(bound_event=True) waits on an unfinished bound game
     instead of grading the same teams' finished game from another day
  6. props aren't bound (their box-score search needs the whole slate)
  7. soccer odds: DraftKings from ESPN's all-competitions feed, exact lines
     only; extract_espn_bookmaker survives an empty odds slot and reads the
     away handicap LINE (not its odds) from the open line
"""

import asyncio
import copy
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import httpx

import scores
import odds
from ai import build_context, CONTEXT_PENDING

FIXTURES = Path(__file__).resolve().parent / "fixtures"
SLATES = json.loads((FIXTURES / "espn_binding_slates.json").read_text())
SOCCER = json.loads((FIXTURES / "espn_soccer_all_slates.json").read_text())


def by_name(slate, name):
    return next(e for e in SLATES[slate] if e["name"] == name)


G1 = by_name("MLB_20260918", "Detroit Tigers at Chicago White Sox")   # 23:40Z 9/18
G2 = by_name("MLB_20260919", "Detroit Tigers at Chicago White Sox")   # 18:10Z 9/19
RAMS = SLATES["NFL_20260921"][0]


def ts(s):
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


calls = []


async def fake_fetch_espn(sport, date):
    calls.append((sport, date))
    key = f"{sport}_{date.replace('-', '')}"
    return {"events": copy.deepcopy(SLATES.get(key, []))}


def main() -> int:
    failures = []

    def check(name, cond, detail=""):
        print(f"  [{'PASS' if cond else 'FAIL'}] {name}" + (f" — {detail}" if detail and not cond else ""))
        if not cond:
            failures.append(name)

    run = asyncio.run
    real_fetch = scores.fetch_espn
    scores.fetch_espn = fake_fetch_espn
    try:
        tigers = ["Detroit Tigers"]
        print("1. series by post time")
        e, st, d = run(scores.espn_bind("MLB", tigers, "2026-09-18", "Tigers ML", ref=ts("2026-09-18T20:00Z")))
        check("posted before game 1 → game 1", e["id"] == G1["id"] and d == "2026-09-18", f"{e and e['id']} {d}")
        e, st, d = run(scores.espn_bind("MLB", tigers, "2026-09-18", "Tigers ML", ref=ts("2026-09-19T00:30Z")))
        check("posted during game 1 (live bet) → game 1", e["id"] == G1["id"], e and e["id"])
        e, st, d = run(scores.espn_bind("MLB", tigers, "2026-09-18", "Tigers ML", ref=ts("2026-09-19T14:00Z")))
        check("posted the next morning → game 2", e["id"] == G2["id"] and d == "2026-09-19", f"{e and e['id']} {d}")

        print("2. odds anchor")
        e, st, d = run(scores.espn_bind("MLB", tigers, "2026-09-18", "Tigers ML", anchor="2026-09-19T18:10Z"))
        check("commence_time anchors game 2", e["id"] == G2["id"], e and e["id"])

        print("3. lookahead")
        e, st, d = run(scores.espn_bind("NFL", ["Los Angeles Rams"], "2026-09-20", "Rams ML",
                                        ref=ts("2026-09-20T15:00Z")))
        check("Sunday-dated MNF leg binds Monday's game", st == "bound" and e["id"] == RAMS["id"]
              and d == "2026-09-21", f"{st} {e and e['id']} {d}")

        print("4. ambiguity")
        e, st, d = run(scores.espn_bind("MLB", ["Los Angeles"], "2026-09-18", "Los Angeles ML",
                                        ref=ts("2026-09-18T20:00Z")))
        check("'Los Angeles' = Dodgers and Angels → ambiguous", st == "ambiguous", st)
        e, st, d = run(scores.espn_bind("MLB", ["Los Angeles Dodgers", "San Francisco Giants"], "2026-09-18",
                                        "Dodgers ML", ref=ts("2026-09-18T20:00Z")))
        check("both teams named → bound", st == "bound" and "Dodgers" in e["name"], st)

        print("5. grading a bound leg")
        pick = {"bet_type": "moneyline", "teams": tigers, "description": "Tigers ML", "period": "game"}
        rec = scores.binding_record(G2, "2026-09-19", "bound", tigers)
        future = {**rec, "kickoff": "2099-01-01T00:00Z"}
        calls.clear()
        mode, sb, d = run(scores.bound_scoreboard("MLB", pick, future, fake_fetch_espn))
        check("pregame binding → 'pregame', zero fetches", mode == "pregame" and not calls, f"{mode} {calls}")
        mode, sb, d = run(scores.bound_scoreboard("MLB", pick, rec, fake_fetch_espn))
        check("after kickoff → a one-event slate of the bound game on its date",
              mode == "ok" and [e["id"] for e in sb["events"]] == [G2["id"]] and d == "2026-09-19", mode)
        live = copy.deepcopy(sb)
        live["events"][0]["status"]["type"].update(completed=False, state="in")
        ctx, _ = run(build_context("MLB", "2026-09-19", pick, live, {}, msg_date="2026-09-18",
                                   bound_event=True))
        check("unfinished bound game → PENDING (never yesterday's final of the series)",
              ctx == CONTEXT_PENDING, ctx[:80])
        ctx, _ = run(build_context("MLB", "2026-09-19", pick, sb, {}, bound_event=True))
        check("finished bound game → its own score context", "Tigers" in ctx and "White Sox" in ctx, ctx[:80])
        m = scores.try_early_grade_math("MLB", pick, sb)
        check("math grades the bound game", m is not None, str(m))
        other = scores.binding_record(G2, "2026-09-19", "bound", ["Chicago White Sox"])
        mode, _, _ = run(scores.bound_scoreboard("MLB", pick, other, fake_fetch_espn))
        check("binding from a different parse is ignored", mode == "none", mode)

        print("6. what binds")
        check("props aren't bound", not scores.bindable("MLB", {"bet_type": "prop", "teams": tigers}))
        check("UFC isn't bound", not scores.bindable("UFC", {"bet_type": "moneyline", "teams": ["X"]}))
        check("team markets are", scores.bindable("NFL", {"bet_type": "spread", "teams": ["Los Angeles Rams"]}))
    finally:
        scores.fetch_espn = real_fetch

    print("7. odds")
    comp = {"odds": [None], "competitors": []}
    check("empty odds slot → no bookmaker (was a crash)", scores.extract_espn_bookmaker(comp) is None)
    o = {"pointSpread": {"home": {"open": {"line": "-1.5", "odds": "+140"}},
                         "away": {"open": {"line": "+1.5", "odds": "-160"}}}}
    comp = {"odds": [o], "competitors": [
        {"homeAway": "home", "team": {"displayName": "Home FC"}},
        {"homeAway": "away", "team": {"displayName": "Away FC"}}]}
    bk = scores.extract_espn_bookmaker(comp)
    pts = {x["name"]: x["point"] for x in bk["markets"][0]["outcomes"]}
    check("away handicap line read from the open LINE", pts == {"Home FC": -1.5, "Away FC": 1.5}, str(pts))

    class _Resp:
        def __init__(self, data):
            self._d = data

        def raise_for_status(self):
            pass

        def json(self):
            return self._d

    class _Stub:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def get(self, url, params=None, timeout=None):
            d = (params or {}).get("dates", "")
            if "/all/scoreboard" in url and d == "20260927":
                return _Resp({"events": SOCCER["2026-09-27_liverpool"]})
            return _Resp({"events": []})

    real = httpx.AsyncClient
    httpx.AsyncClient = _Stub
    scores.clear_soccer_cache()
    try:
        wsl = next(e for e in SOCCER["2026-09-27_liverpool"] if e["name"] == "Everton at Liverpool")
        pick = {"bet_type": "moneyline", "teams": ["Liverpool"], "description": "Liverpool Women ML",
                "period": "game"}
        r = asyncio.run(odds._soccer_espn_odds(pick, pick["teams"], today="2026-09-27"))
        want = wsl["competitions"][0]["odds"][0]["moneyline"]["home"]["close"]["odds"]
        check("women's club ML priced from DraftKings (free)", r and r.odds == int(want)
              and r.game_date == "2026-09-27" and r.commence_time == wsl["date"], str(r))
        tot = {"bet_type": "total", "teams": ["Liverpool", "Everton"], "line": 3.5, "direction": "over",
               "description": "Liverpool Women v Everton o3.5", "period": "game"}
        r = asyncio.run(odds._soccer_espn_odds(tot, tot["teams"], today="2026-09-27"))
        check("off-main soccer total → alt_line_gap, never a made-up price",
              r and r.odds is None and r.match_type.startswith("alt_line_gap"), str(r))
        amb = {"bet_type": "moneyline", "teams": ["Liverpool"], "description": "Liverpool ML", "period": "game"}
        r = asyncio.run(odds._soccer_espn_odds(amb, amb["teams"], today="2026-09-27"))
        check("ambiguous soccer pick → None (old path, never a guessed price)", r is None, str(r))
    finally:
        httpx.AsyncClient = real
        scores.clear_soccer_cache()

    print()
    if failures:
        print(f"FAILED: {len(failures)}")
        return 1
    print("All event-binding checks passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
