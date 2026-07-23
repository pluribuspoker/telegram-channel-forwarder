"""Regression test: a pick whose message line already states the capper's price.

Self-contained (no Telegram/API) — run it directly:

    ~/venv/bin/python scripts/test_source_priced_odds.py

Guards the bug where "Blue Jays TT over 3.5 -145" (capper's own price on the
pick line) had the fetched [-138] stranded on an unrelated blockquote line: the
pick line was correctly declined for already carrying a price, but the pick then
counted as *unmatched* and fell through to the single-pick "best content line"
fallback.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tracker_format import _insert_odds

PICK = {
    "description": "Toronto Blue Jays Team Total Over 3.5",
    "bet_type": "team_total", "is_parlay_leg": False, "period": "game",
    "teams": ["Toronto Blue Jays"], "player": None, "prop_stat": None,
    "line": 3.5, "direction": "over",
}
WITH_ANGLE = (
    "Laformula exclusive\n\nBlue Jays TT over 3.5 -145\n\n"
    "10-2 MLB team total overs\n26-18 MLB since June"
)
PLAIN = "Laformula exclusive\n\nBlue Jays TT over 3.5 -145"
NO_PRICE = (
    "Laformula exclusive\n\nBlue Jays TT over 3.5\n\n"
    "10-2 MLB team total overs\n26-18 MLB since June"
)

CASES = [
    # (label, text, picks, fetched odds, expected output)
    ("within threshold: leave the message alone",
     WITH_ANGLE, [PICK], -138, WITH_ANGLE),
    ("within threshold, no blockquote to leak onto",
     PLAIN, [PICK], -138, PLAIN),
    ("beyond threshold: 'now' marker on the PICK line, not the angle line",
     WITH_ANGLE, [PICK], -190,
     WITH_ANGLE.replace("3.5 -145", "3.5 -145 [-190 now]")),
    ("no stated price: plain tag still lands on the pick line",
     NO_PRICE, [PICK], -138,
     NO_PRICE.replace("over 3.5", "over 3.5 [-138]")),
]


def main() -> int:
    failures = 0
    for label, text, picks, odds, expected in CASES:
        ob = {"0": {"odds": odds, "bookmaker": "fanduel", "match_type": "exact",
                    "pregame_odds": None, "game_date": "2026-07-23"}}
        got = _insert_odds(text, picks, ob)
        again = _insert_odds(got, picks, ob)   # must be idempotent: the tracker
                                               # re-runs this over the live text
                                               # every cycle
        ok = got == expected
        idem = got == again
        status = "PASS" if (ok and idem) else "FAIL"
        if not (ok and idem):
            failures += 1
        print(f"[{status}] {label}")
        if not ok:
            print("   expected:")
            for l in expected.split("\n"):
                print("     ", repr(l))
            print("   got:")
            for l in got.split("\n"):
                print("     ", repr(l))
        if not idem:
            print("   NOT IDEMPOTENT — second pass:")
            for l in again.split("\n"):
                print("     ", repr(l))

    print(f"\n{len(CASES) - failures}/{len(CASES)} passed")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
