"""Pin the CFL schedule parser against the 2026-09 cfl.ca Nuxt redesign.

cfl.ca dropped its server-rendered game cards (quarter tables, `div-game-id-`
anchors) in early September 2026; the schedule now ships as a devalue-encoded
`__NUXT_DATA__` payload. The old parser returned 0 games, which reads as
"game not found" -> CONTEXT_PENDING, so every CFL pick sat unresolved forever
(2026-09-09 nightly audit: Elks ML, posted 2026-09-07, never graded).

Fixture is the real schedule page fetched 2026-09-09 (gzipped, byte-exact).
The payload carries finals but NO quarter scores, so `*_quarters` are empty;
both formatters must tolerate that (full-game bets grade, period bets pend).

Run: python scripts/test_cfl_schedule_parse.py
"""
import gzip
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scores import (  # noqa: E402
    _cfl_event,
    _format_cfl_line_scores,
    _parse_cfl_schedule,
)

FIXTURE = Path(__file__).resolve().parent / "fixtures" / "cfl_schedule_20260909.html.gz"
WIDGET_FIXTURE = Path(__file__).resolve().parent / "fixtures" / "cfl_genius_widget_13419725.html.gz"

failures = []


def check(name: str, cond: bool, detail: str = ""):
    status = "PASS" if cond else "FAIL"
    print(f"  [{status}] {name}" + (f" — {detail}" if detail and not cond else ""))
    if not cond:
        failures.append(name)


def main():
    html = gzip.decompress(FIXTURE.read_bytes()).decode("utf-8")
    games = _parse_cfl_schedule(html)

    # 95 fixtures in the payload; 5 are TBD playoff placeholders (no team ids).
    check("parses the full season", len(games) == 90, f"got {len(games)}")

    elks = [g for g in games
            if g["date"] == "2026-09-07" and g["away_abbr"] == "EDM"]
    check("finds the Elks@Stampeders final", len(elks) == 1, f"got {len(elks)}")
    if elks:
        g = elks[0]
        check("away is Edmonton Elks", g["away_name"] == "Edmonton Elks", g["away_name"])
        check("home is Calgary Stampeders", g["home_name"] == "Calgary Stampeders", g["home_name"])
        check("final score 38-28", (g["away_total"], g["home_total"]) == ("38", "28"),
              f"{g['away_total']}-{g['home_total']}")
        check("marked final", g["final"] is True)
        check("not live", g["live"] is False)
        check("regulation (4 periods)", g["ot"] is False)
        check("quarters absent from payload", g["away_quarters"] == [] and g["home_quarters"] == [])

        ctx = _format_cfl_line_scores(g)
        check("context renders quarterless final",
              "Edmonton Elks 38 at Calgary Stampeders 28 [Final]" in ctx
              and "Edmonton Elks: Final=38" in ctx, ctx)

        ev = _cfl_event(g)
        check("event state is post/completed",
              ev["status"]["type"] == {"state": "post", "completed": True})
        away = ev["competitions"][0]["competitors"][0]
        check("event carries the final score",
              away["homeAway"] == "away" and away["score"] == "38")

    # A future fixture must read as pre/not-completed — mapping "not live"
    # to "post" would grade unplayed games (see _cfl_event docstring).
    future = [g for g in games if g["date"] > "2026-09-09" and not g["final"]]
    check("future games exist", bool(future), "none parsed")
    if future:
        ev = _cfl_event(future[0])
        check("future game is pre/not completed",
              ev["status"]["type"] == {"state": "pre", "completed": False})
        check("future game never final", future[0]["final"] is False)

    # Legacy markup fallback: the old regex path must still be reachable and
    # return [] (not crash) on the new page.
    from scores import _parse_cfl_schedule_markup
    check("legacy parser degrades cleanly", _parse_cfl_schedule_markup(html) == [])

    genius_quarters_checks(games)


def genius_quarters_checks(games):
    """Quarter scores via the Genius gametracker widget (2026-09-09 audit:
    Bombers 1H +3.5 sat UNKNOWN×6 — the payload's quarterless final can't
    settle a period bet). Fixture is the real widget page for WPG@SSK
    2026-09-06, an OT game whose sides spell the OT phase differently
    (away `overtime1`, home `quarter5`)."""
    import asyncio
    import time as _time

    from scores import (
        _cfl_quarters_cache,
        _ensure_cfl_quarters,
        _parse_cfl_genius_state,
        try_early_grade_math,
    )

    html = gzip.decompress(WIDGET_FIXTURE.read_bytes()).decode("utf-8")
    state = _parse_cfl_genius_state(html)
    check("widget state parses", state is not None)
    if not state:
        return
    check("away quarters incl. scoreless OT",
          state["away"] == ["3", "10", "3", "10", "0"], str(state["away"]))
    check("home quarters incl. quarter5-spelled OT",
          state["home"] == ["5", "7", "1", "13", "6"], str(state["home"]))
    check("post-match status", state["match_status"] == "PostMatch",
          str(state["match_status"]))
    check("current phase 5", state["current_phase"] == 5)

    wpg = [g for g in games
           if g["date"] == "2026-09-06" and g["away_abbr"] == "WPG"]
    check("finds WPG@SSK in schedule", len(wpg) == 1, f"got {len(wpg)}")
    if not wpg:
        return
    g = wpg[0]
    check("schedule carries the genius id", g.get("genius_id") == 13419725,
          str(g.get("genius_id")))

    # Enrich through the real helper, offline: pre-seed the fixture's state
    # into the quarters cache so no network fires.
    _cfl_quarters_cache[13419725] = (_time.monotonic(), state)
    asyncio.run(_ensure_cfl_quarters(g))
    check("quarters filled in place", g["away_quarters"] == state["away"]
          and g["home_quarters"] == state["home"])
    check("OT detected from phase count", g["ot"] is True)

    ctx = _format_cfl_line_scores(g)
    check("context renders the halves",
          "Winnipeg Blue Bombers: Q1=3 Q2=10 H1=13" in ctx
          and "H1=12" in ctx and "OT=" in ctx, ctx)
    check("header shows OT final",
          "Winnipeg Blue Bombers 26 at Saskatchewan Roughriders 32 [F (OT)]" in ctx,
          ctx)

    # The audited pick, end to end through the arithmetic path: WPG won the
    # half 13-12, so 1H +3.5 is a WIN the moment the quarters exist.
    pick = {
        "description": "Winnipeg Blue Bombers 1H +3.5 (-115)",
        "bet_type": "spread",
        "period": "1h",
        "teams": ["Winnipeg Blue Bombers"],
        "player": None,
        "line": 3.5,
        "direction": None,
    }
    result = try_early_grade_math("CFL", pick, {"events": [_cfl_event(g)]})
    check("math settles the 1H spread", result is not None
          and result[0] == "WIN", str(result))

    print()
    if failures:
        print(f"{len(failures)} FAILURE(S): {failures}")
        sys.exit(1)
    print("all checks passed")


if __name__ == "__main__":
    main()
