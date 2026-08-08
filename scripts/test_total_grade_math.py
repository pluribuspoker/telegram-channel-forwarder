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


def P(**kw):
    base = {"bet_type": "total", "teams": ["Dallas Wings"], "player": "",
            "line": 79.5, "direction": "under", "period": "1h", "sport": "WNBA"}
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
    ("non-total bet types are not this function's job", "WNBA",
     P(bet_type="moneyline"), WNBA, None),
    ("missing direction falls through", "WNBA", P(direction=None), WNBA, None),
    ("missing line falls through", "WNBA", P(line=None), WNBA, None),
]


def main() -> int:
    failures = 0

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

    total = len(CASES) + 4
    print(f"\n{total - failures}/{total} passed")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
