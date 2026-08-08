"""Regression test: totals are settled by arithmetic, not by asking a model to add.

Self-contained (no network) — run it directly:

    ~/venv/bin/python scripts/test_total_grade_math.py

Built on the real box score that exposed the bug: Golden State Valkyries at
Dallas Wings, 2026-08-07. Quarters DAL [21,15,22,18] / GSV [26,18,30,20], so
the first half is 36 + 44 = **80** against a line of 79.5 — the under loses by
a half point. It was graded WIN, because "Dallas Wings 1H under 79.5" is
`bet_type=total` (the team name identifies the *game*) yet was scored as
Dallas's own 36. A wrong total produces a clean verdict exactly like a right
one, so nothing downstream could flag it.

Two independent defects had to line up, and both are pinned here:
  1. `PERIOD_MAP` had no WNBA entry, so `_extract_period_scores` returned None
     and the arithmetic path was dead for this sport in every state.
  2. The arithmetic only ran mid-game, so a settled total was always Claude's.

The guards fail open on purpose — anything unusual (postponed, soccer extra
time, an unmapped period) returns None and still gets the normal context+Claude
path. Those are pinned too, since a guard that silently swallowed a pick would
be a worse bug than the one being fixed.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scores import try_early_grade_math, PERIOD_MAP


def event(away_name, away_q, home_name, home_q, *, state="post", completed=True):
    def competitor(name, quarters, side):
        return {
            "homeAway": side,
            "team": {"displayName": name},
            "score": str(sum(quarters)),
            "linescores": [{"value": float(q), "displayValue": str(q)} for q in quarters],
        }
    return {
        "events": [{
            "id": "401001",
            "status": {"type": {"state": state, "completed": completed}},
            "competitions": [{
                "competitors": [
                    competitor(away_name, away_q, "away"),
                    competitor(home_name, home_q, "home"),
                ]
            }],
        }]
    }


# The real game.
WNBA = event("Golden State Valkyries", [26, 18, 30, 20], "Dallas Wings", [21, 15, 22, 18])
WNBA_LIVE = event("Golden State Valkyries", [26, 18, 30, 20], "Dallas Wings", [21, 15, 22, 18],
                  state="in")
WNBA_PPD = event("Golden State Valkyries", [26, 18, 30, 20], "Dallas Wings", [21, 15, 22, 18],
                 completed=False)


# Texas Rangers beat Seattle 5-4 — the -1 run line case (margin exactly 1).
MLB54 = event("Texas Rangers", [1, 0, 2, 1, 1, 0, 0, 0, 0], "Seattle Mariners",
              [0, 2, 0, 1, 0, 1, 0, 0, 0])
MLB54_LIVE = event("Texas Rangers", [1, 0, 2, 1, 1, 0, 0, 0, 0], "Seattle Mariners",
                   [0, 2, 0, 1, 0, 1, 0, 0, 0], state="in")
# A tied side bet refunds (NFL can tie).
TIED = event("Golden State Valkyries", [7, 7, 7, 7], "Dallas Wings", [7, 7, 7, 7])


def P(**kw):
    base = {"bet_type": "total", "teams": ["Dallas Wings"], "player": "",
            "line": 79.5, "direction": "under", "period": "1h", "sport": "WNBA"}
    base.update(kw)
    return base


def S(**kw):
    """A side bet (spread/moneyline): no direction, period defaults to the game."""
    base = {"bet_type": "moneyline", "teams": ["Texas Rangers"], "player": "",
            "line": None, "direction": None, "period": "game", "sport": "MLB",
            "description": ""}
    base.update(kw)
    return base


CASES = [
    # ── the reported pick: 36+44=80 vs 79.5 → the under LOSES ─────────────────
    ("WNBA 1H total under 79.5 (reported)", "WNBA", P(), WNBA, ("LOSS", "80")),
    ("WNBA 1H total over 79.5", "WNBA", P(direction="over"), WNBA, ("WIN", "80")),
    # The same words as a team_total really are Dallas's 36 — opposite verdict.
    # This divergence is the whole bug: one field decides which number is used.
    ("WNBA 1H TEAM total under 79.5", "WNBA", P(bet_type="team_total"), WNBA, ("WIN", "36")),
    # Exact landing = PUSH. The mid-game path only knew ">", so it could never
    # produce this, and a push mis-booked as a loss costs a full unit.
    ("total lands exactly on the line", "WNBA", P(line=80.0), WNBA, ("PUSH", "push")),
    ("team_total lands exactly on the line", "WNBA",
     P(bet_type="team_total", line=36.0), WNBA, ("PUSH", "push")),
    # Full-game total: 76+94 = 170.
    ("WNBA game total under 171.5", "WNBA",
     P(period="game", line=171.5), WNBA, ("WIN", "170")),
    ("WNBA game total over 171.5", "WNBA",
     P(period="game", line=171.5, direction="over"), WNBA, ("LOSS", "170")),
    # Under wins at final — unreachable before, since mid-game only settled ">".
    ("WNBA 1H total under 99.5", "WNBA", P(line=99.5), WNBA, ("WIN", "80")),

    # ── mid-game behaviour must not change ────────────────────────────────────
    # Value already past the line: settled (monotone — it can only grow).
    ("mid-game, already over the line", "WNBA",
     P(period="game", line=100.5, direction="over"), WNBA_LIVE, ("WIN", "170")),
    ("mid-game, under not yet decided", "WNBA",
     P(period="game", line=200.5), WNBA_LIVE, None),

    # ── guards fail open → None → normal context+Claude path ──────────────────
    ("postponed/suspended is not a result", "WNBA", P(), WNBA_PPD, None),
    ("soccer left to Claude (extra-time rule)", "Soccer",
     P(sport="Soccer", period="game", line=2.5, teams=["Dallas Wings"]), WNBA, None),
    # UFC events hold many bouts and _extract_period_scores reads competitions[0],
    # so without the allowlist a round total would add two unrelated bout scores.
    ("UFC left to Claude (bouts are not a scoreline)", "UFC",
     P(sport="UFC", period="game", line=2.5), WNBA, None),
    ("unlisted sport falls through", "Tennis",
     P(sport="Tennis", period="game", line=2.5), WNBA, None),
    ("unmapped period falls through", "WNBA", P(period="3q_bogus"), WNBA, None),
    ("no scoreboard falls through", "WNBA", P(), None, None),
    ("team_total naming nobody in the game", "WNBA",
     P(bet_type="team_total", teams=["Chicago Sky"]), WNBA, None),
    # Bet types the scoreline can't settle stay with Claude.
    ("player props are not this function's job", "WNBA",
     P(bet_type="prop", player="Paige Bueckers"), WNBA, None),
    ("double_chance is not this function's job", "WNBA",
     P(bet_type="double_chance"), WNBA, None),
    ("missing direction falls through", "WNBA", P(direction=None), WNBA, None),
    ("missing line falls through", "WNBA", P(line=None), WNBA, None),

    # ── spread / moneyline: final only, and PUSH is real ──────────────────────
    # The second real defect: Rangers -1 run line, won 5-4. Margin is exactly 1,
    # so a -1 line refunds. It was graded LOSS.
    ("spread -1 won by exactly 1 = PUSH", "MLB",
     S(bet_type="spread", line=-1, teams=["Texas Rangers"]), MLB54, ("PUSH", "push")),
    ("spread -1.5 won by 1 = LOSS", "MLB",
     S(bet_type="spread", line=-1.5, teams=["Texas Rangers"]), MLB54, ("LOSS", "-0.5")),
    # Seattle lost 4-5, so +1.5 covers by half a point.
    ("spread +1.5 losing by 1 = WIN", "MLB",
     S(bet_type="spread", line=1.5, teams=["Seattle Mariners"]), MLB54, ("WIN", "+0.5")),
    ("moneyline winner", "MLB",
     S(bet_type="moneyline", teams=["Texas Rangers"]), MLB54, ("WIN", "5 vs 4")),
    ("moneyline loser", "MLB",
     S(bet_type="moneyline", teams=["Seattle Mariners"]), MLB54, ("LOSS", "4 vs 5")),
    ("moneyline tie = PUSH", "NFL",
     S(bet_type="moneyline", teams=["Dallas Wings"], sport="NFL"), TIED, ("PUSH", "push")),
    ("period spread uses period scores", "WNBA",
     S(bet_type="spread", line=-3.5, teams=["Dallas Wings"], period="1h", sport="WNBA"),
     WNBA, ("LOSS", "1H")),

    # A lead is not monotone, so a side bet must never settle before final.
    ("spread mid-game never settles", "MLB",
     S(bet_type="spread", line=-1.5, teams=["Texas Rangers"]), MLB54_LIVE, None),
    ("moneyline mid-game never settles", "MLB",
     S(bet_type="moneyline", teams=["Texas Rangers"]), MLB54_LIVE, None),

    # Rules the scoreline cannot express → fall through to Claude.
    ("regulation ML (OT counts against) falls through", "NHL",
     S(bet_type="moneyline", teams=["Texas Rangers"], sport="NHL",
       description="Rangers regulation ML 3-way"), MLB54, None),
    ("to-advance falls through", "MLB",
     S(bet_type="moneyline", teams=["Texas Rangers"],
       description="Texas Rangers to advance"), MLB54, None),
    ("spread with no line falls through", "MLB",
     S(bet_type="spread", line=None, teams=["Texas Rangers"]), MLB54, None),
    ("side bet naming nobody in the game", "MLB",
     S(bet_type="moneyline", teams=["Chicago Cubs"]), MLB54, None),
    ("UFC side bet stays with Claude", "UFC",
     S(bet_type="moneyline", teams=["Texas Rangers"], sport="UFC"), MLB54, None),
    ("soccer side bet stays with Claude (3-way)", "Soccer",
     S(bet_type="moneyline", teams=["Texas Rangers"], sport="Soccer"), MLB54, None),
]


def _cfl_game(**kw):
    base = {
        "date": "2026-08-07", "away_abbr": "TOR", "home_abbr": "CGY",
        "away_name": "Toronto Argonauts", "home_name": "Calgary Stampeders",
        "away_quarters": [7, 3, 7, 0], "home_quarters": [0, 10, 7, 7],
        "away_total": 17, "home_total": 24,
        "final": True, "live": False, "current_period": 4,
    }
    base.update(kw)
    return base


def cfl_checks() -> int:
    """CFL is scraped, not on the ESPN scoreboard, so it needs its own shaping.

    The state mapping is the load-bearing part. `live` and `final` are
    independent flags and an unplayed game is neither, so the old
    "in if live else post" would present a game that hasn't kicked off as a
    finished 0-0 — invisible while only build_early_context consumed it (it
    ignores state), but the arithmetic path reads it and would grade a game
    before it happened.
    """
    from scores import _cfl_event
    failures = 0

    def check(label, cond):
        nonlocal failures
        if not cond:
            failures += 1
        print(f"[{'PASS' if cond else 'FAIL'}] {label}")

    fin = _cfl_event(_cfl_game())
    check("CFL final -> state=post, completed=True",
          fin["status"]["type"] == {"state": "post", "completed": True})

    live = _cfl_event(_cfl_game(final=False, live=True))
    check("CFL live -> state=in, completed=False",
          live["status"]["type"] == {"state": "in", "completed": False})

    pre = _cfl_event(_cfl_game(final=False, live=False, away_quarters=[], home_quarters=[],
                               away_total=0, home_total=0))
    check("CFL not started -> state=pre (never graded as a 0-0 final)",
          pre["status"]["type"]["state"] == "pre")

    # An unplayed game must not settle a total, whatever the line.
    sb_pre = {"events": [pre]}
    got = try_early_grade_math("CFL", {
        "bet_type": "total", "teams": ["Toronto Argonauts"], "player": "",
        "line": 40.5, "direction": "under", "period": "game", "sport": "CFL",
    }, sb_pre)
    check("CFL unplayed game declines instead of grading 0-0 under", got is None)

    # Final CFL game: 17 + 24 = 41.
    sb_fin = {"events": [fin]}
    for label, pick, want in [
        ("CFL total under 40.5 -> LOSS (41)",
         {"bet_type": "total", "line": 40.5, "direction": "under"}, "LOSS"),
        ("CFL total over 40.5 -> WIN (41)",
         {"bet_type": "total", "line": 40.5, "direction": "over"}, "WIN"),
        ("CFL 1H total: 10+10=20 vs 19.5 -> WIN over",
         {"bet_type": "total", "line": 19.5, "direction": "over", "period": "1h"}, "WIN"),
        ("CFL spread +7.5 -> WIN (lost by 7)",
         {"bet_type": "spread", "line": 7.5}, "WIN"),
        ("CFL spread -7 lost by exactly 7 -> PUSH",
         {"bet_type": "spread", "line": 7.0}, "PUSH"),
        ("CFL moneyline on the loser -> LOSS",
         {"bet_type": "moneyline"}, "LOSS"),
    ]:
        p = {"teams": ["Toronto Argonauts"], "player": "", "period": "game",
             "sport": "CFL", "line": None, "direction": None, "description": ""}
        p.update(pick)
        got = try_early_grade_math("CFL", p, sb_fin)
        check(label, got is not None and got[0] == want)

    return failures


def main() -> int:
    failures = cfl_checks()

    # PERIOD_MAP coverage — the gap that made the arithmetic dead for WNBA.
    for sport in ("WNBA", "Lacrosse", "NBA", "CFL"):
        ok = ("WNBA" if sport == "WNBA" else sport, "1h") in PERIOD_MAP and (sport, "1h") in PERIOD_MAP
        if not ok:
            failures += 1
        print(f"[{'PASS' if ok else 'FAIL'}] PERIOD_MAP covers {sport} 1h")

    for label, sport, pick, sb, expected in CASES:
        got = try_early_grade_math(sport, pick, sb)
        if expected is None:
            ok = got is None
        else:
            want_verdict, want_in_calc = expected
            ok = got is not None and got[0] == want_verdict and want_in_calc in got[1]
        if not ok:
            failures += 1
        print(f"[{'PASS' if ok else 'FAIL'}] {label}")
        if not ok:
            print(f"   expected: {expected}")
            print(f"   got:      {got}")

    total = len(CASES) + 4 + 10
    print(f"\n{total - failures}/{total} passed")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
