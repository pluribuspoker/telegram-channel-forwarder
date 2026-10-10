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
EVENTS_FIXTURE = Path(__file__).resolve().parent / "fixtures" / "thescore_cfl_events_20261010.json.gz"
LINES_FIXTURE = Path(__file__).resolve().parent / "fixtures" / "thescore_cfl_linescores_34952.json.gz"

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

    thescore_checks()

    print()
    if failures:
        print(f"{len(failures)} FAILURE(S): {failures}")
        sys.exit(1)
    print("all checks passed")


def thescore_checks():
    """theScore fallback (2026-10-10 audit: BC Lions 1H -7.5 pended forever).

    That night BOTH cfl.ca sources died at once: the site's data layer froze
    mid-slate (the late OTT@BC game sat "NotStarted" 0-0 hours after its
    final) and the Genius widget redeployed with no SSR state (the earlier
    EDM@HAM final had no quarters, capping Hamilton 1H +4.5 as ungradeable).
    Fixtures are the real theScore payloads fetched that morning, byte-exact:
    the events window and the OTT@BC box's line_scores."""
    import asyncio
    import copy
    import json
    import time as _time

    import scores as _scores
    from scores import (
        _cfl_quarters_cache,
        _ensure_cfl_quarters,
        _match_thescore_cfl_event,
        _merge_thescore_cfl_status,
        _parse_thescore_cfl_line_scores,
        _thescore_cfl_lines_cache,
        try_early_grade_math,
    )

    events = json.loads(gzip.decompress(EVENTS_FIXTURE.read_bytes()))
    rows = json.loads(gzip.decompress(LINES_FIXTURE.read_bytes()))
    check("events fixture holds the slate", len(events) == 3, f"got {len(events)}")

    # The OTT@BC game exactly as the stale schedule payload parsed it.
    stale = {
        "date": "2026-10-10", "away_abbr": "OTT", "home_abbr": "BC",
        "away_name": "Ottawa Redblacks", "home_name": "BC Lions",
        "away_quarters": [], "home_quarters": [],
        "away_total": "0", "home_total": "0",
        "final": False, "ot": False, "live": False,
        "current_period": None, "genius_id": 13419744,
    }
    bc_event = _match_thescore_cfl_event(stale, events)
    check("matches the OTT@BC event by abbr pair", bc_event is not None
          and bc_event.get("id") == 138576, str(bc_event and bc_event.get("id")))

    _merge_thescore_cfl_status([stale], events)
    check("stale NotStarted upgrades to final", stale["final"] is True
          and stale["live"] is False)
    check("final score taken from theScore",
          (stale["away_total"], stale["home_total"]) == ("20", "41"),
          f"{stale['away_total']}-{stale['home_total']}")
    check("regulation final", stale["ot"] is False)

    # A pre_game event must not upgrade, and an unknown status never acts
    # (postponed/limbo keeps failing closed as pending).
    pre = {"date": "2026-10-10", "away_abbr": "CGY", "home_abbr": "WPG",
           "away_name": "Calgary Stampeders", "home_name": "Winnipeg Blue Bombers",
           "away_quarters": [], "home_quarters": [], "away_total": "0",
           "home_total": "0", "final": False, "ot": False, "live": False,
           "current_period": None, "genius_id": 13419745}
    _merge_thescore_cfl_status([pre], events)
    check("pre_game event leaves the game pending", pre["final"] is False
          and pre["live"] is False and pre["away_total"] == "0")
    weird = copy.deepcopy(events)
    for e in weird:
        if e.get("id") == 138576:
            e["event_status"] = "postponed"
    limbo = dict(stale, final=False, away_total="0", home_total="0")
    _merge_thescore_cfl_status([limbo], weird)
    check("unknown status never upgrades", limbo["final"] is False)

    # Line scores: side mapping by team uri, quarter order by segment.
    state = _parse_thescore_cfl_line_scores(bc_event, rows)
    check("line scores parse", state is not None, str(state))
    if not state:
        return
    check("away quarters (OTT)", state["away"] == ["6", "6", "6", "2"], str(state["away"]))
    check("home quarters (BC)", state["home"] == ["14", "7", "7", "13"], str(state["home"]))
    gappy = [r for r in rows
             if not (r.get("segment") == 2 and r.get("team") == "/cfl/teams/320")]
    check("a segment gap fails closed",
          _parse_thescore_cfl_line_scores(bc_event, gappy) is None)

    # Enrich through the real helper, offline: genius cache pre-seeded with its
    # live failure (the SSR-less shell parses to None), theScore caches with
    # the fixtures — the fallback must fill the quarters.
    _cfl_quarters_cache[13419744] = (_time.monotonic(), None)
    _scores._thescore_cfl_events_cache = (_time.monotonic(), events)
    _thescore_cfl_lines_cache[34952] = (_time.monotonic(), state)
    asyncio.run(_ensure_cfl_quarters(stale))
    check("quarters filled via theScore", stale["away_quarters"] == state["away"]
          and stale["home_quarters"] == state["home"])

    ctx = _format_cfl_line_scores(stale)
    check("context renders the halves",
          "BC Lions: Q1=14 Q2=7 H1=21" in ctx
          and "Ottawa Redblacks: Q1=6 Q2=6 H1=12" in ctx, ctx)

    # The audited pick, end to end through the arithmetic path: BC won the
    # half 21-12, so 1H -7.5 is a WIN the moment status + quarters exist.
    pick = {
        "description": "BC Lions 1H -7.5",
        "bet_type": "spread",
        "period": "1h",
        "teams": ["BC Lions"],
        "player": None,
        "line": -7.5,
        "direction": None,
    }
    result = try_early_grade_math("CFL", pick, {"events": [_cfl_event(stale)]})
    check("math settles the audited 1H spread", result is not None
          and result[0] == "WIN", str(result))


if __name__ == "__main__":
    main()
