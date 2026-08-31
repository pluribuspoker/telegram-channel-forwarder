#!/usr/bin/env python3
"""Regression: validate_sport must not rebind a pick across sports on city words.

Pins the 2026-08-30 failure: "49ers vs Rams u48.5" (NFL Week-1 lookahead, parsed
correctly as NFL) found no NFL game that day — the game was 2026-09-11Z — so
validate_sport shopped alternative sports. Its fragment matching split the
description into ["San","Francisco","49ers","Los","Angeles","Rams",...] and the
city words matched the Angels' MLB game as displayName substrings. The override
flipped the leg to sport=MLB / teams=["Los Angeles Angels"], the math grader
scored the Angels game (5+2=7 vs 48.5 → WIN), and the results channel broadcast
"✅ Los Angeles Angels U48.5" for a football game 12 days out.

A cross-sport override now requires a bet-text token that names the club itself
(nickname / short name / abbreviation). The fixture is the real (trimmed) ESPN
MLB scoreboard for 2026-08-30.

Fully offline — fixtures only, no API calls, no DB.

    python scripts/test_sport_override_regression.py
"""
import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import scores  # noqa: E402

FIXTURE = Path(__file__).resolve().parent / "fixtures" / "espn_mlb_scoreboard_20260830.json"
MLB_SB = json.loads(FIXTURE.read_text())
DATE = "2026-08-30"

FAILS = 0


async def _no_network(sport, date_str, *a, **kw):
    # Any sport/date the test didn't pre-fill has no games that day (matches
    # reality for 2026-08-30: NFL/NBA/NHL were all dark) — and stays offline.
    return {"events": []}


def check(label: str, got, want) -> None:
    global FAILS
    ok = got == want
    print(f"{'✓' if ok else '✗'} {label}: {got}" + ("" if ok else f"  (want {want})"))
    if not ok:
        FAILS += 1


async def main() -> None:
    scores.fetch_espn = _no_network

    # 1. The incident, byte-exact parse fields: a correctly-parsed NFL leg with
    #    no NFL game on the post date must come back untouched — the MLB slate's
    #    city-word overlap ("San", "Los", "Angeles") is not evidence.
    cache = {("MLB", DATE): MLB_SB}
    got = await scores.validate_sport(
        "NFL",
        ["San Francisco 49ers", "Los Angeles Rams"],
        "San Francisco 49ers vs Los Angeles Rams under 48.5",
        DATE,
        cache,
    )
    check("NFL lookahead survives city-word overlap",
          got, ("NFL", ["San Francisco 49ers", "Los Angeles Rams"]))

    # 2. A cross-sport override with club-level evidence still fires: "Tigers"
    #    names the club, and Detroit Tigers are on the MLB slate.
    cache = {("MLB", DATE): MLB_SB}
    got = await scores.validate_sport(
        "NHL", ["Detroit Tigers"], "Tigers ML tonight", DATE, cache,
    )
    check("nickname-evidenced override still corrects the sport",
          got, ("MLB", ["Detroit Tigers"]))

    # 3. The same-sport fuzzy rescue is untouched: "KIA Tigers" (wrong league
    #    prefix, right nickname) still lands on the MLB club playing that day.
    cache = {("MLB", DATE): MLB_SB}
    got = await scores.validate_sport(
        "MLB", ["KIA Tigers"], "KIA Tigers moneyline", DATE, cache,
    )
    check("same-sport fragment rescue unchanged",
          got, ("MLB", ["Detroit Tigers"]))

    print(f"\n{'PASS' if FAILS == 0 else f'FAIL ({FAILS})'}")
    sys.exit(1 if FAILS else 0)


if __name__ == "__main__":
    asyncio.run(main())
