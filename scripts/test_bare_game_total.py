"""Regression test: bare "TEAM over/under N" mis-parsed as team_total → game total.

Self-contained (no Telegram/API) — run it directly:

    ~/venv/bin/python scripts/test_bare_game_total.py

Insider posted "Chiefs over 42.5 5U" (2026-09-14, Broncos at Chiefs — the game
total market WAS 42.5). It parsed as a Chiefs TEAM total: the only market with
a 42.5 team-total line was a deep FanDuel alternate, so the pick displayed
[+3500] for a -110 bet — priced through the PAID odds path, because team totals
aren't on the free sources — and graded on the Chiefs' score alone. The ❌
survived by luck (41 combined and 31 team-only both lose the over).

_fix_bare_game_total is the deterministic backstop behind the prompt rule. The
shapes it must NOT touch are pinned here too: explicit "team total"/"TT"
wording, lines below the sport's game-total floor, period-scoped team totals,
prop-stat team markets (corners), and sports with no floor (Soccer).
"""
import copy
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from ai import _fix_bare_game_total


def P(desc, sport=None, line=None, period="game", bet_type="team_total",
      direction="over", prop_stat=None):
    return {"description": desc, "sport": sport, "bet_type": bet_type,
            "is_parlay_leg": False, "period": period, "teams": ["X"],
            "player": None, "prop_stat": prop_stat, "line": line,
            "direction": direction}


CASES = [
    # (name, top_sport, picks, text, expected bet_types, expected descriptions)
    (
        "the incident: bare Chiefs over 42.5 (NFL) → game total",
        "NFL",
        [P("Kansas City Chiefs team total over 42.5", line=42.5)],
        "Insider\n\nChiefs over 42.5 5U\n\n> 4-0 NFL totals run\n> also on a hot run",
        ["total"],
        ["Kansas City Chiefs game total over 42.5"],
    ),
    (
        "explicit 'team total' wording stays team_total even above the floor",
        "NFL",
        [P("Kansas City Chiefs team total over 42.5", line=42.5)],
        "Chiefs team total over 42.5",
        ["team_total"],
        ["Kansas City Chiefs team total over 42.5"],
    ),
    (
        "'TT' marker stays team_total",
        "NFL",
        [P("Kansas City Chiefs team total over 42.5", line=42.5)],
        "Chiefs TT o42.5",
        ["team_total"],
        ["Kansas City Chiefs team total over 42.5"],
    ),
    (
        "below the floor: a real NFL team total is left alone",
        "NFL",
        [P("Baltimore Ravens team total over 24.5", line=24.5)],
        "Ravens over 24.5",
        ["team_total"],
        ["Baltimore Ravens team total over 24.5"],
    ),
    (
        "period-scoped team total is never flipped (1H ranges differ)",
        "NBA",
        [P("Suns 1st half team total over 56.5", line=56.5, period="1h")],
        "Suns 1H over 56.5",
        ["team_total"],
        ["Suns 1st half team total over 56.5"],
    ),
    (
        "Soccer has no floor: corners/goals shapes untouched",
        "Soccer",
        [P("Brazil team total over 4.5 corners", line=4.5, prop_stat="CORNERS")],
        "Brazil over 4.5 corners",
        ["team_total"],
        ["Brazil team total over 4.5 corners"],
    ),
    (
        "MLB bare 'Yankees over 9.5' → game total",
        "MLB",
        [P("New York Yankees team total over 9.5", line=9.5)],
        "Yankees over 9.5",
        ["total"],
        ["New York Yankees game total over 9.5"],
    ),
    (
        "description without 'team total' words gets the game-total marker",
        "NBA",
        [P("Boston Celtics over 214", line=214.0)],
        "Celtics over 214",
        ["total"],
        ["Boston Celtics game total over 214"],
    ),
    (
        "sweep sibling: Mercury over 174 (WNBA) would have flipped",
        "WNBA",
        [P("Phoenix Mercury team total over 174", line=174.0)],
        "Mercury over 174 -110",
        ["total"],
        ["Phoenix Mercury game total over 174"],
    ),
    (
        "pick-level sport wins over the top-level sport",
        "NFL",
        [P("Boston Celtics team total over 214", sport="NBA", line=214.0)],
        "Celtics over 214",
        ["total"],
        ["Boston Celtics game total over 214"],
    ),
    (
        "no line → untouched",
        "NFL",
        [P("Kansas City Chiefs team total over", line=None)],
        "Chiefs over",
        ["team_total"],
        ["Kansas City Chiefs team total over"],
    ),
]


def main() -> int:
    failures = 0
    for name, sport, picks, text, want_types, want_descs in CASES:
        parsed = {"sport": sport, "picks": copy.deepcopy(picks)}
        _fix_bare_game_total(parsed, text)
        got_types = [p["bet_type"] for p in parsed["picks"]]
        got_descs = [p["description"] for p in parsed["picks"]]
        ok = got_types == want_types and got_descs == want_descs
        # idempotent: a second pass over the corrected parse changes nothing
        again = copy.deepcopy(parsed)
        _fix_bare_game_total(again, text)
        if again != parsed:
            ok = False
            print(f"FAIL (not idempotent): {name}")
        if not ok:
            failures += 1
            print(f"FAIL: {name}")
            print(f"  want: {want_types} {want_descs}")
            print(f"  got:  {got_types} {got_descs}")
        else:
            print(f"ok: {name}")
    if failures:
        print(f"\n{failures} failure(s)")
        return 1
    print(f"\nall {len(CASES)} cases pass")
    return 0


if __name__ == "__main__":
    sys.exit(main())
