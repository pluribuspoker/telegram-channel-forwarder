"""Regression test: the Bovada free fallback for CFL odds.

Self-contained (no Telegram/API) — run it directly:

    ~/venv/bin/python scripts/test_bovada_fallback.py

The Odds API quota ran out on 2026-08-08 and every CFL pick after that
recorded no_game: the free /events endpoint still matched the game (so
game_date was set) while the market call 401'd into an empty bookmakers list,
and the ESPN fallback is dead for CFL because ESPN's CFL scoreboard is
deliberately empty. Bovada's public coupon JSON is the free second source.

The fixture is the real Bovada v2 payload for Winnipeg @ Edmonton captured
2026-08-22T01:28:33Z — 87 seconds before kickoff — trimmed to whole
displayGroups (Game Lines, Alternate Lines, and Passing Props to pin the
prop-leak guard), never retyped. The headline case is the real pick that
exposed the gap (-1002486251914:3643): "Blue Bombers 1H +3.5 (-110)", which
must price as exact_alt -105 through alternate_spreads_h1.
"""
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from odds import (
    _bovada_american,
    _bovada_bookmakers,
    _bovada_minimal_event,
    _bovada_pick_event,
    _bovada_team,
    _find_event_id,
    lookup_pick_odds,
)

FIXTURE = Path(__file__).resolve().parent / "fixtures" / "bovada_cfl_pregame.json"

failures = []


def check(label, got, want):
    ok = got == want
    print(f"{'✅' if ok else '❌'} {label}: {got!r}" + ("" if ok else f" (want {want!r})"))
    if not ok:
        failures.append(label)


def lookup(pick, bookmakers):
    r = lookup_pick_odds("CFL", pick, bookmakers)
    return r.get("match_type"), r.get("adjusted_odds"), r.get("bookmaker")


def main():
    event = json.load(open(FIXTURE))[0]["events"][0]
    bk = _bovada_bookmakers(event)

    # ── Shaper: exactly the Odds API market keys the lookups read ─────────────
    keys = sorted(m["key"] for m in bk[0]["markets"])
    check("bookmaker key", bk[0]["key"], "bovada")
    # Alternate lines fold into the main keys — _lookup_spread never reads
    # alternate_spreads_h1 (the Odds API doesn't sell it), so an API-style
    # alternate_* sibling would make Bovada's 1H alt lines invisible.
    check("market keys", keys, sorted([
        "h2h", "spreads", "totals",
        "h2h_h1", "spreads_h1", "totals_h1",
        "h2h_q1", "spreads_q1", "totals_q1",
        "team_totals",
    ]))
    # Player props ("Total Passing Touchdowns - Cody Fajardo") must not leak
    # into totals: every totals outcome is a bare Over/Under.
    for m in bk[0]["markets"]:
        if "totals" in m["key"] and m["key"] != "team_totals":
            bad = [o for o in m["outcomes"] if o["name"] not in ("Over", "Under")]
            check(f"no prop leak in {m['key']}", bad, [])

    # ── The real pick that exposed the gap (parse_cache -1002486251914:3643) ──
    real_pick = {"description": "Winnipeg Blue Bombers 1H +3.5", "sport": None,
                 "bet_type": "spread", "is_parlay_leg": False, "period": "1h",
                 "teams": ["Winnipeg Blue Bombers"], "player": None,
                 "prop_stat": None, "line": 3.5, "direction": None}
    check("real pick 1H +3.5 (alt line)", lookup(real_pick, bk),
          ("exact", -105, "bovada"))

    # ── Every market family at its own period ─────────────────────────────────
    # Synthetic picks get clean descriptions: lookup_pick_odds re-derives the
    # period from the description when period says "game", so reusing the real
    # pick's "1H +3.5" text would silently move the lookup to spreads_h1.
    check("1H spread main 4.0", lookup({**real_pick, "line": 4.0}, bk),
          ("exact", -115, "bovada"))
    check("game spread 6.0",
          lookup({**real_pick, "description": "Winnipeg Blue Bombers +6",
                  "period": "game", "line": 6.0}, bk),
          ("exact", -115, "bovada"))
    check("game total over 58.5",
          lookup({"description": "over 58.5", "bet_type": "total", "period": "game",
                  "teams": ["Winnipeg Blue Bombers"], "line": 58.5,
                  "direction": "over", "player": None, "prop_stat": None}, bk),
          ("exact", -105, "bovada"))
    check("1H total under 28.5",
          lookup({"description": "1H under 28.5", "bet_type": "total", "period": "1h",
                  "teams": ["Winnipeg Blue Bombers"], "line": 28.5,
                  "direction": "under", "player": None, "prop_stat": None}, bk),
          ("exact", -115, "bovada"))
    check("game ML Edmonton",
          lookup({"description": "Edmonton Elks ML", "bet_type": "moneyline",
                  "period": "game", "teams": ["Edmonton Elks"], "line": None,
                  "direction": None, "player": None, "prop_stat": None}, bk),
          ("exact", -270, "bovada"))
    check("1H ML Winnipeg",
          lookup({"description": "Winnipeg 1H ML", "bet_type": "moneyline",
                  "period": "1h", "teams": ["Winnipeg Blue Bombers"], "line": None,
                  "direction": None, "player": None, "prop_stat": None}, bk),
          ("exact", 180, "bovada"))
    check("1Q spread Winnipeg +0.5",
          lookup({**real_pick, "description": "Winnipeg Blue Bombers 1Q +0.5",
                  "period": "1q", "line": 0.5}, bk),
          ("exact", -105, "bovada"))
    # EVEN price → +100 (alternate game total UNDER 57.5 — the over is -130)
    check("alt game total under 57.5 (EVEN)",
          lookup({"description": "under 57.5", "bet_type": "total", "period": "game",
                  "teams": ["Winnipeg Blue Bombers"], "line": 57.5,
                  "direction": "under", "player": None, "prop_stat": None}, bk),
          ("exact", 100, "bovada"))
    # Team total: outcomes carry the team in `description`, Over/Under in `name`
    r = lookup_pick_odds("CFL", {"description": "Edmonton team total over 32.5",
                                 "bet_type": "team_total", "period": "game",
                                 "teams": ["Edmonton Elks"], "line": 32.5,
                                 "direction": "over", "player": None,
                                 "prop_stat": None}, bk)
    check("team total EDM over 32.5", (r.get("adjusted_odds"), r.get("api_line")),
          (-120, 32.5))

    # ── Name and price helpers ────────────────────────────────────────────────
    check("period suffix strip", _bovada_team("Winnipeg Blue Bombers - 1H"),
          "Winnipeg Blue Bombers")
    check("over suffix strip", _bovada_team("Over - 1Q"), "Over")
    check("BC alias", _bovada_team("British Columbia Lions"), "BC Lions")
    check("american +220", _bovada_american("+220"), 220)
    check("american EVEN", _bovada_american("EVEN"), 100)
    check("american None", _bovada_american(None), None)
    check("american junk", _bovada_american("1.909091"), None)

    # ── Event matching and the started-game refusal ───────────────────────────
    minimal = _bovada_minimal_event(event)
    check("minimal event", (minimal["home_team"], minimal["away_team"],
                            minimal["commence_time"]),
          ("Edmonton Elks", "Winnipeg Blue Bombers", "2026-08-22T01:30:00Z"))
    check("event id found", _find_event_id([minimal], ["Winnipeg Blue Bombers"]),
          minimal["id"])

    before = datetime(2026, 8, 22, 1, 0, tzinfo=timezone.utc)
    after = datetime(2026, 8, 22, 2, 0, tzinfo=timezone.utc)
    ev, gd = _bovada_pick_event([event], ["Winnipeg Blue Bombers"],
                                allow_started=False, now=before)
    check("pregame: event served", (ev is not None, gd), (True, "2026-08-21"))
    ev, gd = _bovada_pick_event([event], ["Winnipeg Blue Bombers"],
                                allow_started=False, now=after)
    check("started + pregame caller: refused", (ev, gd), (None, "2026-08-21"))
    ev, _ = _bovada_pick_event([event], ["Winnipeg Blue Bombers"],
                               allow_started=True, now=after)
    check("started + live caller: served", ev is not None, True)

    # ── Suspended markets/outcomes are dropped (synthetic — the pregame
    #    snapshot has none; live coupons do) ─────────────────────────────────
    frozen = {"displayGroups": [{"description": "Game Lines", "markets": [
        {"description": "Point Spread", "status": "S",
         "period": {"abbreviation": "G"},
         "outcomes": [{"description": "Edmonton Elks", "status": "O",
                       "price": {"american": "-105", "handicap": "-6.0"}}]},
        {"description": "Moneyline", "status": "O",
         "period": {"abbreviation": "G"},
         "outcomes": [
             {"description": "Edmonton Elks", "status": "S",
              "price": {"american": "-270"}},
             {"description": "Winnipeg Blue Bombers", "status": "O",
              "price": {"american": "+220"}},
         ]},
    ]}]}
    fbk = _bovada_bookmakers(frozen)
    check("suspended market dropped",
          [m["key"] for m in fbk[0]["markets"]], ["h2h"])
    check("suspended outcome dropped",
          [o["name"] for o in fbk[0]["markets"][0]["outcomes"]],
          ["Winnipeg Blue Bombers"])

    print()
    if failures:
        print(f"❌ {len(failures)} failure(s): {failures}")
        sys.exit(1)
    print("✅ all bovada fallback checks passed")


if __name__ == "__main__":
    main()
