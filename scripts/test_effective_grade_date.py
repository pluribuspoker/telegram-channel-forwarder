"""Regression test: a lookahead pick's odds-bound game date is eventually trusted.

Self-contained (no Telegram/API) — run it directly:

    ~/venv/bin/python scripts/test_effective_grade_date.py

`eff_date` picks the date a leg is graded against. The old inline form (daemon
+ tracker, duplicated) trusted the odds-bound `game_date` only within ±2 days
of the post date — a comparison between two constants, so the window never
"opens" as time passes. A Week-1 NFL total posted 12 days before kickoff
("49ers vs Rams u48.5", 2026-08-30 → game 2026-09-10) therefore graded against
the post date's empty NFL slate forever: CONTEXT_SKIP → UNKNOWN, never
persisted, silent every daemon cycle, until the nightly audit caught it the
morning after the opener.

`effective_grade_date` (common.py, shared so the loops can't diverge) keeps
the ±2 trust (the 2026-05-02 consecutive-day-series fix) and adds: a farther
FUTURE date is trusted once it has actually arrived. The "has arrived" gate is
load-bearing, not conservatism — pre-game, a mis-bound far date must lose to
the post-date slate so a leg the series fix can settle still settles on the
right game first. A far PAST date stays distrusted (bets precede games; that
binding is wrong).
"""
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from common import effective_grade_date

CASES = [
    # (odds_gd, msg_date, today, expected, label)
    (None, "2026-08-30", date(2026, 9, 11), "2026-08-30", "no odds date -> post date"),
    ("2026-08-30", "2026-08-30", date(2026, 8, 30), "2026-08-30", "same date -> post date"),
    # ±2 window: the consecutive-day-series fix, unchanged, independent of today
    ("2026-08-31", "2026-08-30", date(2026, 8, 30), "2026-08-31", "+1 day trusted (night game, UTC drift)"),
    ("2026-09-01", "2026-08-30", date(2026, 8, 30), "2026-09-01", "+2 days trusted"),
    ("2026-08-28", "2026-08-30", date(2026, 8, 30), "2026-08-28", "-2 days trusted"),
    # far future, game date not yet reached: post date wins (series guard)
    ("2026-09-10", "2026-08-30", date(2026, 8, 30), "2026-08-30", "lookahead pre-game -> post date"),
    ("2026-09-10", "2026-08-30", date(2026, 9, 9), "2026-08-30", "lookahead eve of game -> post date"),
    # far future, game date arrived: the incident — odds date must win now
    ("2026-09-10", "2026-08-30", date(2026, 9, 10), "2026-09-10", "lookahead on game day -> odds date"),
    ("2026-09-10", "2026-08-30", date(2026, 9, 11), "2026-09-10", "lookahead post-final -> odds date"),
    # far past: never trusted, at any today
    ("2026-08-20", "2026-08-30", date(2026, 9, 11), "2026-08-30", "far-past binding -> post date"),
    # unparseable input falls back to the post date instead of raising
    ("not-a-date", "2026-08-30", date(2026, 9, 11), "2026-08-30", "garbage odds date -> post date"),
]


def main() -> int:
    failed = 0
    for odds_gd, msg_date, today, expected, label in CASES:
        got = effective_grade_date(odds_gd, msg_date, today=today)
        ok = got == expected
        failed += 0 if ok else 1
        print(f"{'PASS' if ok else 'FAIL'}  {label}: "
              f"odds_gd={odds_gd} msg_date={msg_date} today={today} -> {got}"
              + ("" if ok else f" (expected {expected})"))
    print(f"\n{len(CASES) - failed}/{len(CASES)} passed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
