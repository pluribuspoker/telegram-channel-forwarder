"""Regression test: Bovada free player props (WNBA fixture, never retyped).

Self-contained (no Telegram/API) — run it directly:

    ~/venv/bin/python scripts/test_bovada_props.py

Player O/U props live outside _BOVADA_LINE_GROUPS as one market per player
("Total Points - Ariel Atkins (LAS)"); the "(ABBR)" parenthetical separates
them from team totals and the stat phrase must match EXACTLY (a "Total
Points" prefix rule would also eat "Total Points, Rebounds and Assists" and
"Points Milestones"). The shaper emits the Odds API player-prop outcome shape
so _lookup_prop consumes it unchanged, and fetch_odds_current tries Bovada
BEFORE any paid call (free-first, 2026-08-22).

Fixture: the real Bovada WNBA coupon event Connecticut Sun @ Los Angeles
Sparks captured 2026-08-23T00:26Z (pregame, 11 displayGroups incl. Player
Points/Rebounds, Assists & Threes, Player Combos, Milestones), whole payload,
never retyped. Known values from the capture: Ariel Atkins Total Points 8.5,
Over -110 / Under -120.
"""
import asyncio
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import odds
from odds import (
    _BOVADA_PROP_KEY,
    _bovada_prop_markets,
    _bovada_prop_phrases,
    _lookup_prop,
    fetch_odds_current,
    should_retry_odds,
)

FIXTURE = Path(__file__).resolve().parent / "fixtures" / "bovada_wnba_props.json"

failures = []


def check(label, got, want):
    ok = got == want
    print(f"{'✅' if ok else '❌'} {label}: {got!r}" + ("" if ok else f" (want {want!r})"))
    if not ok:
        failures.append(label)


EVENT = json.load(open(FIXTURE))[0]["events"][0]
# Freeze "now" one hour before the fixture game so the pregame picker accepts
# it forever (the real game starts and finishes; the capture doesn't).
FROZEN = datetime.strptime("", "").min  # placeholder, replaced below
FROZEN = datetime.fromtimestamp(int(EVENT["startTime"]) / 1000, tz=timezone.utc) - timedelta(hours=1)


def test_phrase_map():
    check("WNBA PTS mapped", bool(_bovada_prop_phrases("WNBA", "PTS")), True)
    check("stat normalization (spaces)", _bovada_prop_phrases("NFL", "pass yds"),
          _bovada_prop_phrases("NFL", "PASS_YDS"))
    check("MLB strikeouts mapped", _bovada_prop_phrases("MLB", "K"), ("Total Strikeouts",))
    check("unmapped stat is empty", _bovada_prop_phrases("MLB", "HR"), ())
    check("unmapped sport is empty", _bovada_prop_phrases("UFC", "KO/TKO"), ())


def test_shaper():
    # Headline: Ariel Atkins Total Points 8.5 (from the capture: -110 / -120).
    bk = _bovada_prop_markets(EVENT, _bovada_prop_phrases("WNBA", "PTS"), "Ariel Atkins")
    r = _lookup_prop(bk, "Ariel Atkins", _BOVADA_PROP_KEY, "over", 8.5)
    check("Atkins PTS over 8.5 priced", (r["match_type"], r["adjusted_odds"], r["bookmaker"]),
          ("exact", -110, "bovada"))
    r = _lookup_prop(bk, "Ariel Atkins", _BOVADA_PROP_KEY, "under", 8.5)
    check("Atkins PTS under 8.5 priced", r["adjusted_odds"], -120)
    # Exact-phrase discipline: only the O/U points market for that player —
    # Milestones and combo markets must not leak in.
    check("only Over+Under emitted for one player", len(bk[0]["markets"][0]["outcomes"]), 2)

    # Wrong line refuses (exact-line rule lives in _lookup_prop).
    r = _lookup_prop(bk, "Ariel Atkins", _BOVADA_PROP_KEY, "over", 9.5)
    check("wrong line refuses", (r["match_type"], r["adjusted_odds"]), ("prop_not_found", None))

    # Combo phrase matches its own market exactly, never the PTS one.
    pra_bk = _bovada_prop_markets(EVENT, _bovada_prop_phrases("WNBA", "PTS+REB+AST"), "")
    pra_descs = {o["description"] for o in pra_bk[0]["markets"][0]["outcomes"]} if pra_bk else set()
    check("PRA markets exist in fixture", bool(pra_bk), True)

    # Structural guard: no shaped outcome may carry a TEAM as its player —
    # team totals ("Total Points - Los Angeles Sparks", no parenthetical)
    # must never pass the prop regex.
    all_bk = _bovada_prop_markets(EVENT, _bovada_prop_phrases("WNBA", "PTS"), "")
    names = {o["description"] for o in all_bk[0]["markets"][0]["outcomes"]}
    teams = {"Connecticut Sun", "Los Angeles Sparks"}
    check("no team total leaked into props", names & teams, set())

    # Unknown player finds nothing.
    r = _lookup_prop(_bovada_prop_markets(EVENT, _bovada_prop_phrases("WNBA", "PTS"), "Caitlin Clark"),
                     "Caitlin Clark", _BOVADA_PROP_KEY, "over", 8.5)
    check("unknown player misses", r["adjusted_odds"], None)


class Stub:
    """Patches the network seams of the prop path; counts paid calls."""

    def __init__(self, paid_bk=None):
        self.paid_calls = 0
        commence = datetime.fromtimestamp(int(EVENT["startTime"]) / 1000, tz=timezone.utc)
        self.commence = commence.strftime("%Y-%m-%dT%H:%M:%SZ")
        self.event = {"id": "ev1", "sport_key": "basketball_wnba",
                      "home_team": "Los Angeles Sparks", "away_team": "Connecticut Sun",
                      "commence_time": self.commence}
        self.paid_bk = paid_bk or []

    def install(self):
        async def _events(sport, conn):
            return [self.event]

        async def _bov_events(sport):
            return [EVENT]

        async def _paid(sport_key, event_id, markets, conn, *, live=False):
            self.paid_calls += 1
            return self.paid_bk

        orig_prop = odds._fetch_bovada_prop

        async def _prop_frozen(*a, **k):
            k.setdefault("now", FROZEN)
            return await orig_prop(*a, **k)

        self.saved = (odds._fetch_current_event_list_all, odds._fetch_bovada_events,
                      odds._fetch_current_bookmakers, odds._fetch_bovada_prop,
                      odds._event_already_started)
        odds._fetch_current_event_list_all = _events
        odds._fetch_bovada_events = _bov_events
        odds._fetch_current_bookmakers = _paid
        odds._fetch_bovada_prop = _prop_frozen
        odds._event_already_started = lambda event_list, event_id: False

    def restore(self):
        (odds._fetch_current_event_list_all, odds._fetch_bovada_events,
         odds._fetch_current_bookmakers, odds._fetch_bovada_prop,
         odds._event_already_started) = self.saved


def run(pick, stub, **kw):
    stub.install()
    try:
        return asyncio.run(fetch_odds_current("WNBA", pick, db_path=":memory:", **kw))
    finally:
        stub.restore()


def test_flow():
    atkins = {"bet_type": "prop", "player": "Ariel Atkins", "prop_stat": "PTS",
              "direction": "over", "line": 8.5, "teams": ["Los Angeles Sparks"],
              "description": "Ariel Atkins over 8.5 points"}

    # F1: Bovada prices the prop (teams path) → no paid spend, dates recorded.
    stub = Stub()
    r = run(atkins, stub)
    check("F1: Bovada priced via teams path", (r.odds, r.bookmaker), (-110, "bovada"))
    check("F1: no paid calls", stub.paid_calls, 0)
    check("F1: commence recorded for retries", r.commence_time, stub.commence)

    # F2: no teams — the player scan finds the game.
    stub = Stub()
    r = run({**atkins, "teams": []}, stub)
    check("F2: Bovada priced via player scan", (r.odds, r.bookmaker), (-110, "bovada"))
    check("F2: no paid calls", stub.paid_calls, 0)

    # F3: free_only + mapped stat + player not listed yet → retryable miss.
    stub = Stub()
    r = run({**atkins, "player": "Caitlin Clark"}, stub, free_only=True)
    check("F3: mapped-but-missing is prop_not_found", r.match_type, "prop_not_found")
    check("F3: free_only spends nothing", stub.paid_calls, 0)
    check("F3: prop_not_found is retryable pre-start",
          should_retry_odds({"odds": None, "match_type": "prop_not_found",
                             "commence_time": (datetime.now(timezone.utc) + timedelta(hours=3)).strftime("%Y-%m-%dT%H:%M:%SZ")}),
          True)

    # F4: free_only + unmapped stat → final (never priceable free).
    stub = Stub()
    r = run({**atkins, "prop_stat": "BLK"}, stub, free_only=True)
    check("F4: unmapped stat is final", r.match_type, "player_prop_unavailable")
    check("F4: player_prop_unavailable never retries",
          should_retry_odds({"odds": None, "match_type": "player_prop_unavailable",
                             "commence_time": (datetime.now(timezone.utc) + timedelta(hours=3)).strftime("%Y-%m-%dT%H:%M:%SZ")}),
          False)

    # F5: Bovada misses, paid API is last resort and prices it.
    stub = Stub(paid_bk=[{"key": "stub", "title": "Stub",
                          "markets": [{"key": "player_points", "outcomes": [
                              {"name": "Over", "description": "Caitlin Clark",
                               "point": 8.5, "price": -115}]}]}])
    r = run({**atkins, "player": "Caitlin Clark"}, stub)
    check("F5: paid last resort priced", (r.odds, r.bookmaker), (-115, "stub"))
    check("F5: exactly one paid call", stub.paid_calls, 1)


def main():
    test_phrase_map()
    test_shaper()
    test_flow()
    print()
    if failures:
        print(f"❌ {len(failures)} failure(s): {failures}")
        sys.exit(1)
    print("✅ all bovada prop checks passed")


if __name__ == "__main__":
    main()
