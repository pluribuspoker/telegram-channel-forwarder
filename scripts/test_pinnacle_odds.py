"""Regression test: Pinnacle free source + "a quoted line beats an estimate".

Self-contained (no network) — run it directly:

    ~/venv/bin/python scripts/test_pinnacle_odds.py

The incident (2026-09-27, Zilla "Broncos +3.5"): ESPN serves DraftKings' ONE
main line (DEN +1.5 -120), so the +3.5 pick was priced by _adjust_for_gap's
linear half-point model — [-173] — and the free-source chain stopped there,
because any price counted as found. Pinnacle quoted +3.5 at -190 and Bovada
-200 (the model is blind to NFL key numbers: +3 → +3.5 alone is ~30 cents).
Fixtures are the real payloads captured ~2 min before kickoff (never retyped):
Pinnacle league 889 (the game, a margin-band child and a futures special) and
Bovada's LAR @ DEN event trimmed to its line groups.
"""
import asyncio
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import odds
from odds import (
    _better_free_result,
    _bovada_bookmakers,
    _pinnacle_bookmakers,
    fetch_odds_current,
    lookup_pick_odds,
)

FIX = Path(__file__).resolve().parent / "fixtures"
PIN_NFL = json.load(open(FIX / "pinnacle_nfl_lar_den_20260927.json"))
PIN_NHL = json.load(open(FIX / "pinnacle_nhl_bos_nyr_20260929.json"))
BOV_NFL = json.load(open(FIX / "bovada_nfl_lar_den_20260927.json"))[0]["events"][0]
_ET = ZoneInfo("America/New_York")

GAME_ID = 1636875757
PICK = {"bet_type": "spread", "teams": ["Denver Broncos"], "line": 3.5,
        "period": "game", "description": "Denver Broncos +3.5"}
# ESPN's DraftKings main line at pricing time (pickcenter: DEN +1.5 -120 / LAR -1.5 +100).
ESPN_BK = [{"key": "espn_draftkings", "markets": [{"key": "spreads", "outcomes": [
    {"name": "Denver Broncos", "price": -120, "point": 1.5},
    {"name": "Los Angeles Rams", "price": 100, "point": -1.5}]}]}]

failures = []


def check(label, got, want):
    ok = got == want
    print(f"{'✅' if ok else '❌'} {label}: {got!r}" + ("" if ok else f" (want {want!r})"))
    if not ok:
        failures.append(label)


def _game():
    return next(m for m in PIN_NFL["matchups"] if m["id"] == GAME_ID)


def _keys(bk):
    return {m["key"] for m in bk[0]["markets"]} if bk else set()


def test_shaper():
    bk = _pinnacle_bookmakers(_game(), PIN_NFL["markets"], "NFL")
    check("bookmaker key", bk[0]["key"], "pinnacle")
    check("market keys (game + observed 1H/1Q)", _keys(bk),
          {"h2h", "spreads", "totals", "team_totals",
           "h2h_h1", "spreads_h1", "totals_h1", "team_totals_h1",
           "h2h_q1", "spreads_q1", "totals_q1", "team_totals_q1"})
    r = lookup_pick_odds("NFL", PICK, bk)
    check("Broncos +3.5 quoted exactly", (r["match_type"], r["adjusted_odds"], r["bookmaker"]),
          ("exact", -190, "pinnacle"))
    r = lookup_pick_odds("NFL", {**PICK, "line": 1.5, "description": "Denver Broncos +1.5"}, bk)
    check("alternate +1.5 folds into spreads", r["adjusted_odds"], -120)
    r = lookup_pick_odds("NFL", {"bet_type": "moneyline", "teams": ["Los Angeles Rams"],
                                 "period": "game", "description": "Rams ML"}, bk)
    check("moneyline side by alignment", r["adjusted_odds"] is not None, True)
    r = lookup_pick_odds("NFL", {"bet_type": "total", "teams": [], "line": 22.5, "direction": "over",
                                 "period": "1h", "description": "1H o22.5"}, bk)
    check("1H total = period 1", (r["match_type"], r["adjusted_odds"]), ("exact", -104))
    r = lookup_pick_odds("NFL", {"bet_type": "team_total", "teams": ["Denver Broncos"], "line": 3.5,
                                 "direction": "over", "period": "1q", "description": "DEN 1Q TT o3.5"}, bk)
    check("1Q team total carries the team", r["adjusted_odds"] is not None, True)
    # Hockey periods are unmapped (2-way draw-refund period MLs): game only.
    nhl = PIN_NHL["matchups"][0]
    nbk = _pinnacle_bookmakers(nhl, PIN_NHL["markets"], "NHL")
    check("NHL: game markets only", {k for k in _keys(nbk) if k[-3:-1] in ("_p", "_h", "_q")}, set())
    check("NHL: game lines present", {"h2h", "spreads", "totals"} <= _keys(nbk), True)


def test_league_filters():
    """Specials (margin bands, futures) never bind as the game."""
    async def _league(sport):
        kept = [m for m in PIN_NFL["matchups"] if m.get("type") == "matchup"
                and not m.get("parentId") and m.get("units", "Regular") == "Regular"]
        return kept, PIN_NFL["markets"]
    saved = odds._fetch_pinnacle_league
    odds._fetch_pinnacle_league = _league
    try:
        before = datetime(2026, 9, 28, 0, 18, tzinfo=timezone.utc)
        after = datetime(2026, 9, 28, 0, 21, tzinfo=timezone.utc)
        bk, gd, ct = asyncio.run(odds._fetch_pinnacle_bookmakers("NFL", ["Denver Broncos"], now=before))
        check("pregame: bound the game", (bool(bk), gd, ct), (True, "2026-09-27", "2026-09-28T00:20:00Z"))
        bk, gd, _ = asyncio.run(odds._fetch_pinnacle_bookmakers("NFL", ["Denver Broncos"], now=after))
        check("started: refused, date still reported", (bk, gd), ([], "2026-09-27"))
    finally:
        odds._fetch_pinnacle_league = saved
    ids = {m["id"] for m in PIN_NFL["matchups"] if m.get("type") == "matchup" and not m.get("parentId")}
    check("fixture filter keeps only the game", ids, {GAME_ID})


def test_school_collision():
    """Books name colleges bare — a school must not bind its neighbour's game.

    All three were live bindings in the 2026-09-27 A/B: Georgia State and
    South Alabama took Georgia's/Alabama's games on Pinnacle (-2881, -625), and
    production Bovada already priced "Washington State" off Washington @ USC.
    """
    cases = [("Georgia State Panthers", "Georgia", True), ("Georgia State", "Georgia", True),
             ("South Alabama Jaguars", "Alabama", True), ("Washington State Cougars", "Washington", True),
             ("Miami (OH) RedHawks", "Miami", True), ("Texas A&M Aggies", "Texas", True),
             ("Georgia Bulldogs", "Georgia", False), ("Washington Huskies", "Washington", False),
             ("South Alabama", "South Alabama", False), ("Denver Broncos", "Denver Broncos", False)]
    for term, name, want in cases:
        check(f"other school: {term!r} vs {name!r}", odds._names_other_school(term, name), want)
    ev = {"home_team": "Mississippi State", "away_team": "Alabama"}
    check("collision drops the neighbour's game", odds._school_collision(ev, ["South Alabama"], "NCAAF"), True)
    check("fighters exempt (surname vs full name)",
          odds._school_collision({"home_team": "Volkanovski", "away_team": "Evloev"},
                                 ["Alexander Volkanovski"], "UFC"), False)
    check("Bovada poll rank stripped", odds._bovada_team("Alabama (#7)"), "Alabama")
    check("Bovada rank + period tag stripped", odds._bovada_team("USC (#18) - 1H"), "USC")


def test_better_free_result():
    est = {"match_type": "proximity_2.0pts", "adjusted_odds": -173, "api_line": 1.5, "pick_line": 3.5}
    near = {"match_type": "proximity_0.5pts", "adjusted_odds": -185, "api_line": 3.0, "pick_line": 3.5}
    exact = {"match_type": "exact", "adjusted_odds": -190, "api_line": 3.5, "pick_line": 3.5}
    miss = {"match_type": "no_spread_data", "adjusted_odds": None}
    no_game = {"match_type": "no_game", "adjusted_odds": None}
    check("exact beats estimate", _better_free_result(est, exact), True)
    check("nearer estimate beats farther", _better_free_result(est, near), True)
    check("farther estimate loses", _better_free_result(near, est), False)
    check("equal-gap estimate keeps the earlier source", _better_free_result(est, dict(est)), False)
    check("estimate never replaces exact", _better_free_result(exact, near), False)
    check("exact never replaces exact (source order)", _better_free_result(exact, dict(exact)), False)
    check("price beats miss", _better_free_result(miss, est), True)
    check("miss never replaces a price", _better_free_result(est, miss), False)
    check("miss replaces only no_game", (_better_free_result(no_game, miss),
                                         _better_free_result(miss, no_game)), (True, False))


class Stub:
    def __init__(self, *, pinnacle, bovada, espn=ESPN_BK):
        self.commence = (datetime.now(timezone.utc) + timedelta(hours=2)).strftime("%Y-%m-%dT%H:%M:%SZ")
        self.gd = datetime.fromisoformat(self.commence.replace("Z", "+00:00")).astimezone(_ET).strftime("%Y-%m-%d")
        self.pin, self.bov, self.espn = pinnacle, bovada, espn
        self.calls = {"pinnacle": 0, "bovada": 0, "paid": 0}

    def install(self):
        event = {"id": "ev1", "sport_key": "americanfootball_nfl", "home_team": "Denver Broncos",
                 "away_team": "Los Angeles Rams", "commence_time": self.commence}

        async def _events(sport, conn):
            return [event]

        async def _espn(sport, date):
            return {"stub": True} if self.espn else None

        async def _pin(sport, teams, *, now=None):
            self.calls["pinnacle"] += 1
            return (self.pin, self.gd, self.commence) if self.pin else ([], None, None)

        async def _bov(sport, teams, allow_started=False):
            self.calls["bovada"] += 1
            return (self.bov, self.gd) if self.bov else ([], None)

        async def _paid(*a, **k):
            self.calls["paid"] += 1
            return []

        names = ("_fetch_current_event_list_all", "fetch_espn", "espn_bookmakers_for_teams",
                 "_fetch_pinnacle_bookmakers", "_fetch_bovada_bookmakers", "_fetch_current_bookmakers")
        self.saved = {n: getattr(odds, n) for n in names}
        odds._fetch_current_event_list_all = _events
        odds.fetch_espn = _espn
        odds.espn_bookmakers_for_teams = lambda data, teams: self.espn
        odds._fetch_pinnacle_bookmakers = _pin
        odds._fetch_bovada_bookmakers = _bov
        odds._fetch_current_bookmakers = _paid

    def restore(self):
        for n, f in self.saved.items():
            setattr(odds, n, f)


def run(pick, stub):
    stub.install()
    try:
        return asyncio.run(fetch_odds_current("NFL", pick, db_path=":memory:"))
    finally:
        stub.restore()


def test_source_order():
    pin_bk = _pinnacle_bookmakers(_game(), PIN_NFL["markets"], "NFL")
    bov_bk = _bovada_bookmakers(BOV_NFL, "NFL")

    s = Stub(pinnacle=pin_bk, bovada=bov_bk)
    r = run(PICK, s)
    check("incident: Pinnacle's quote replaces ESPN's estimate",
          (r.match_type, r.odds, r.bookmaker), ("exact", -190, "pinnacle"))
    check("incident: Bovada not needed once quoted", s.calls["bovada"], 0)
    check("incident: no paid call", s.calls["paid"], 0)

    s = Stub(pinnacle=[], bovada=bov_bk)
    r = run(PICK, s)
    check("Pinnacle down: Bovada's quote", (r.match_type, r.odds, r.bookmaker), ("exact", -200, "bovada"))

    s = Stub(pinnacle=[], bovada=[])
    r = run(PICK, s)
    check("no alt quote anywhere: ESPN estimate stands (old behavior)",
          (r.match_type, r.odds, r.bookmaker), ("proximity_2.0pts", -173, "espn_draftkings"))
    check("estimate still never spends a paid call", s.calls["paid"], 0)

    s = Stub(pinnacle=pin_bk, bovada=bov_bk)
    r = run({**PICK, "line": 1.5, "description": "Denver Broncos +1.5"}, s)
    check("main-line pick: ESPN exact stays", (r.match_type, r.odds, r.bookmaker),
          ("exact", -120, "espn_draftkings"))
    check("main-line pick: no extra free fetches", (s.calls["pinnacle"], s.calls["bovada"]), (0, 0))


def test_bovada_parent_fallback():
    """football/nfl answering 200 + {} falls back to the parent feed's NFL block."""
    blocks = [{"path": [{"link": "/football/college-football"}], "events": [{"id": "cfb"}]},
              {"path": [{"link": "/football/nfl"}], "events": [BOV_NFL]}]
    seen = []

    class Resp:
        def __init__(self, data):
            self.data = data

        def raise_for_status(self):
            pass

        def json(self):
            return self.data

    class Client:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def get(self, url, params=None):
            seen.append(url.rsplit("/description/", 1)[1])
            return Resp({} if url.endswith("/football/nfl") else blocks)

    saved = odds.httpx.AsyncClient
    odds.httpx.AsyncClient = Client
    odds._bovada_cache.pop("NFL", None)
    try:
        events = asyncio.run(odds._fetch_bovada_events("NFL"))
    finally:
        odds.httpx.AsyncClient = saved
        odds._bovada_cache.pop("NFL", None)
    check("fallback asked the parent feed", seen, ["football/nfl", "football"])
    check("kept only the NFL block's events", [e.get("id") for e in events], [BOV_NFL["id"]])


def main():
    test_shaper()
    test_league_filters()
    test_school_collision()
    test_better_free_result()
    test_source_order()
    test_bovada_parent_fallback()
    print()
    if failures:
        print(f"❌ {len(failures)} failure(s): {failures}")
        sys.exit(1)
    print("✅ all pinnacle checks passed")


if __name__ == "__main__":
    main()
