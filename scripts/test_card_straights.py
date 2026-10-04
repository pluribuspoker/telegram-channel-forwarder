"""Regression test: a one-pick-per-line card with no ticket wording is straights.

Self-contained (no Telegram/API) — run it directly:

    ~/venv/bin/python scripts/test_card_straights.py

Trent posted "CFB MORTAL MEGAS:" with five sides one per line and "5-0 CARD"
(-1004394797084:136, 2026-10-03). The parse made all five parlay legs:
_insert_odds stamped a combined [+2309], Michigan's loss settled the "ticket"
and the other four legs were voided instead of graded.
_demote_unworded_card_parlay (ai.py) re-reads the shape as straight bets.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from ai import _demote_unworded_card_parlay


def leg(team, line):
    return {"description": f"{team} {line}", "bet_type": "spread",
            "is_parlay_leg": True, "period": "game", "teams": [team],
            "player": None, "prop_stat": None, "line": line, "direction": None}


INCIDENT_LEGS = [("Alabama Crimson Tide", -5.5), ("Michigan Wolverines", -5.5),
                 ("South Carolina Gamecocks", -2.5), ("Florida Gators", -5.5),
                 ("Tennessee Volunteers", -6.5)]
# Byte-exact raw_text of the source post before any tag/emoji was stamped.
INCIDENT_TEXT = ("◼️ Trent\n\nCFB MORTAL MEGAS: ☢️🐳💣\n\nBAMA -5.5\nMICHIGAN -5.5\n"
                 "SOUTH CAROLINA -2.5\nFLORIDA -5.5\nTENNEESEE -6.5\n\n"
                 "NO DOGS NEEDED. 5-0 CARD. \n\nLETS SWEAT. 🫡🔒\n\n🔗 View on X")
TWO = [("Los Angeles Lakers", -3.5), ("Boston Celtics", -2.5)]

CASES = [
    # (name, legs, text, has_image, expected is_parlay_leg after)
    ("the incident: 5-line card, no ticket wording", INCIDENT_LEGS, INCIDENT_TEXT, False, False),
    ("the incident with the stamped tags/emoji still demotes",
     INCIDENT_LEGS, INCIDENT_TEXT.replace("-2.5\n", "-2.5 [+2309]\n").replace(
         "FLORIDA -5.5", "FLORIDA -5.5❌"), False, False),
    ("a record line '(Record: 4-7)' is not a bet line",
     TWO, "LAKERS -3.5\nCELTICS -2.5\n\n(Record: 4-7)", False, False),
    ("'parlay' wording keeps the parlay", TWO, "2 team parlay\nLAKERS -3.5\nCELTICS -2.5", False, True),
    ("'legs' wording keeps the parlay", TWO, "legs:\nLAKERS -3.5\nCELTICS -2.5", False, True),
    ("'teaser' wording keeps the parlay", TWO, "6pt teaser\nLAKERS -3.5\nCELTICS -2.5", False, True),
    ("a combined price on its own line keeps the parlay",
     TWO, "LAKERS -3.5\nCELTICS -2.5\n+264", False, True),
    ("two selections on one line keep the parlay", TWO, "LAKERS -3.5 & CELTICS -2.5", False, True),
    ("an attached image (slip) is left to the slip rule", TWO, "LAKERS -3.5\nCELTICS -2.5", True, True),
    ("blockquoted angle records don't count as bet lines",
     TWO, "LAKERS -3.5\nCELTICS -2.5\n> 35-13 ATS, +12.5u", False, False),
]


def run():
    failed = 0
    for name, legs, text, has_image, want in CASES:
        parsed = {"picks": [leg(t, l) for t, l in legs]}
        _demote_unworded_card_parlay(parsed, text, has_image)
        got = [p["is_parlay_leg"] for p in parsed["picks"]]
        ok = got == [want] * len(legs)
        failed += not ok
        print(f"{'PASS' if ok else 'FAIL'}  {name}" + ("" if ok else f"  got={got}"))

    # A mixed message (straight + parlay legs) is the model's call — untouched.
    parsed = {"picks": [leg("Los Angeles Lakers", -3.5),
                        dict(leg("Boston Celtics", -2.5), is_parlay_leg=False)]}
    _demote_unworded_card_parlay(parsed, "LAKERS -3.5\nCELTICS -2.5", False)
    ok = [p["is_parlay_leg"] for p in parsed["picks"]] == [True, False]
    failed += not ok
    print(f"{'PASS' if ok else 'FAIL'}  mixed straight/parlay message untouched")

    print(f"\n{len(CASES) + 1 - failed}/{len(CASES) + 1} passed")
    return failed


if __name__ == "__main__":
    sys.exit(1 if run() else 0)
