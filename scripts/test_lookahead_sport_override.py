#!/usr/bin/env python3
"""Regression: a lookahead pick keeps its sport, and no correction drops a typed word.

"LSU ML -150 (Saturday)" (TWG, posted Friday 2026-09-18) parsed correctly as
NCAAF "LSU Tigers" — then validate_sport asked only "does LSU play TODAY?",
found no Friday game, shopped other sports for "Tigers", and flipped the pick
to MLB "Detroit Tigers" (a shared nickname passed the club-evidence rule). It
graded against Tigers–White Sox. Both wrong cross-sport overrides on record
were lookahead picks (49ers/Rams → MLB Angels 2026-08-30 is pinned by
test_sport_override_regression.py). Three layers now:

  1. the parsed sport is confirmed by a game anywhere in VALIDATE_WINDOW
     (day before … 3 days after), BEFORE the same-sport fuzzy rescue
  2. a cross-sport flip may not drop a word the capper typed ("LSU")
  3. `dropped_typed_words` — the tracker's backstop over every correction
     layer (sport override, per-pick override, schedule repair)

and the one correct override on record (an Eagles/Rams teaser parsed NCAAF,
flipped to NFL 2026-09-20) still flips.

Fully offline — real (trimmed) ESPN scoreboards, no API calls.

    python scripts/test_lookahead_sport_override.py
"""
import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import scores  # noqa: E402

SLATES = json.loads((Path(__file__).resolve().parent / "fixtures"
                     / "espn_lookahead_override_slates.json").read_text())
FAILS = 0
LSU_TEXT = "TWG\n\nLSU ML -150 (Saturday)\n\n8-1 this month"
EAGLES_TEXT = "FCS\n\n5U (whale) 7 pt teaser: \nEagles PK / Rams +0.5\n\n1U Bucs -8"


async def _fixture_fetch(sport, date_str, *a, **kw):
    return {"events": SLATES.get(f"{sport}_{date_str.replace('-', '')}", [])}


def check(label, got, want):
    global FAILS
    ok = got == want
    print(f"{'✓' if ok else '✗'} {label}: {got}" + ("" if ok else f"  (want {want})"))
    if not ok:
        FAILS += 1


async def main():
    real = scores.fetch_espn
    scores.fetch_espn = _fixture_fetch
    try:
        # 1. The incident, byte-exact parse fields.
        got = await scores.validate_sport(
            "NCAAF", ["LSU Tigers"], "LSU Tigers moneyline -150", "2026-09-18", {},
            typed_text=LSU_TEXT)
        check("LSU (Saturday) posted Friday stays NCAAF — Saturday's game confirms it",
              got, ("NCAAF", ["LSU Tigers"]))

        # 2. Layer 2 alone: hide Saturday's game — the MLB flip is still refused
        #    because it drops the typed "LSU".
        no_sat = {("NCAAF", d): {"events": []} for d in
                  ("2026-09-17", "2026-09-19", "2026-09-20", "2026-09-21")}
        got = await scores.validate_sport(
            "NCAAF", ["LSU Tigers"], "LSU Tigers moneyline -150", "2026-09-18", no_sat,
            typed_text=LSU_TEXT)
        check("no LSU game in window: shared nickname 'Tigers' still can't flip it",
              got, ("NCAAF", ["LSU Tigers"]))
        # …and without the typed text (the old contract) it flips — proving the
        # fixture reproduces the incident.
        got = await scores.validate_sport(
            "NCAAF", ["LSU Tigers"], "LSU Tigers moneyline -150", "2026-09-18", dict(no_sat))
        check("fixture reproduces the old flip when the guards are off",
              got, ("MLB", ["Detroit Tigers"]))

        # 3. The one correct override on record still happens.
        got = await scores.validate_sport(
            "NCAAF", ["Philadelphia Eagles"], "Eagles +7 (7pt teaser leg)", "2026-09-20", {},
            typed_text=EAGLES_TEXT)
        check("Eagles teaser parsed NCAAF still flips to NFL", got, ("NFL", ["Philadelphia Eagles"]))
    finally:
        scores.fetch_espn = real

    # 4. The backstop predicate.
    check("LSU → Detroit drops typed 'lsu'",
          scores.dropped_typed_words(["LSU Tigers"], ["Detroit Tigers"], LSU_TEXT), ["lsu"])
    check("a guessed city the capper never typed is free to change",
          scores.dropped_typed_words(["Arizona Diamondbacks"], ["Maryland Whipsnakes"], "Snakes -1.5"), [])
    check("an unchanged typed nickname is kept",
          scores.dropped_typed_words(["Philadelphia Eagles"], ["Philadelphia Eagles"], EAGLES_TEXT), [])
    check("KIA typed → a rewrite to Detroit is refused",
          scores.dropped_typed_words(["KIA Tigers"], ["Detroit Tigers"], "KIA Tigers ML"), ["kia"])
    check("filler words never count", scores.dropped_typed_words(
        ["Ohio State Buckeyes"], ["Ohio State Buckeyes"], "Ohio State ML over"), [])

    print(f"\n{'ALL PASS' if not FAILS else f'{FAILS} FAILED'}")
    return 1 if FAILS else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
