"""Regression test: a pick whose message line already states the capper's price.

Self-contained (no Telegram/API) — run it directly:

    ~/venv/bin/python scripts/test_source_priced_odds.py

Guards the bug where "Blue Jays TT over 3.5 -145" (capper's own price on the
pick line) had the fetched [-138] stranded on an unrelated blockquote line: the
pick line was correctly declined for already carrying a price, but the pick then
counted as *unmatched* and fell through to the single-pick "best content line"
fallback.

Also guards the complementary half of that bug: the declined pick line does not
end the search, so a LATER line matching the same pick took the tag and the
`src_declined` resolution below was never reached. Both real cases are here —
a write-up repeating the team name ("The Roughriders should be very motivated")
and an angle record containing the bet number ("9-3 off 2 wins" for an over 9).
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

# The declined pick line must END the search. These two are real messages whose
# write-up / angle record matched the pick AFTER its own priced line was declined.
CFL_PICK = {
    "description": "Saskatchewan Roughriders 1Q -0.5",
    "bet_type": "spread", "is_parlay_leg": False, "period": "1q",
    "teams": ["Saskatchewan Roughriders"], "player": None, "prop_stat": None,
    "line": -0.5, "direction": None,
}
# The write-up names the Roughriders again, so the team-name pass matches it.
CFL_TEXT = (
    "Andrew\n\nCFL MAX PLAY‼️ \n\n"
    "• Roughriders 1Q -0.5 (-125) / (5u) \n\n"
    "<blockquote>5U CFL: 8-1</blockquote>\n\n"
    "<blockquote>The Roughriders should be very motivated in this spot at "
    "home.</blockquote>"
)
TOTAL_PICK = {
    "description": "Padres vs Diamondbacks Over 9",
    "bet_type": "total", "is_parlay_leg": False, "period": "game",
    "teams": ["San Diego Padres", "Arizona Diamondbacks"], "player": None,
    "prop_stat": None, "line": 9, "direction": "over",
}
# No line here repeats a team name — "9-3 off 2 wins" matches on the bet number.
TOTAL_TEXT = (
    "Cblez\n\nPadres / Dbacks over 9 -105 (3U)❌\n\n"
    "<blockquote>8-2 L10 \n9-3 off 2 wins\n17-7 MLB\n"
    "6-1 3U picks record</blockquote>"
)

CASES = [
    # (label, text, picks, fetched odds, expected output)
    ("within threshold: leave the message alone",
     WITH_ANGLE, [PICK], -138, WITH_ANGLE),
    ("write-up repeats the team name: no tag on the analysis blockquote",
     CFL_TEXT, [CFL_PICK], -132, CFL_TEXT),
    ("write-up repeats the team name, beyond threshold: 'now' on the pick line",
     CFL_TEXT, [CFL_PICK], -190,
     CFL_TEXT.replace("(-125) / (5u) ", "(-125) / (5u) [-190 now]")),
    ("angle record holds the bet number: no tag on the angle line",
     TOTAL_TEXT, [TOTAL_PICK], -105, TOTAL_TEXT),
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
