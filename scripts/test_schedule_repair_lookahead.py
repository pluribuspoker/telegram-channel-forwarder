#!/usr/bin/env python3
"""Regression: schedule repair confirms a lookahead pick across VALIDATE_WINDOW.

"1.5U Kentucky +10.5 / 1.5U Georgia +2.5" (WagerStalk, posted Wed 2026-10-07)
parsed correctly as NCAAF Kentucky Wildcats (LSU @ Kentucky, Sat 10/10) — then
verify_picks_on_schedule checked only Wed/Thu, found no Kentucky Wildcats, and
"repaired" the leg from the token "Kentucky" to Thursday's Western Kentucky
Hilltoppers (WKU -2.5): odds found no +10.5 on that game (alt_line_gap_9.5pts)
and it would have graded the wrong game. Georgia (@ Alabama, Sat) was flagged
"unverified" for the same reason.

Confirmation now spans VALIDATE_WINDOW (the window validate_sport uses); the
repair pass still sees only the near-day slate, so a team genuinely absent all
window is still repaired.

Fully offline — real (trimmed) ESPN scoreboards, no API calls.

    python scripts/test_schedule_repair_lookahead.py
"""
import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import scores  # noqa: E402

SLATES = json.loads((Path(__file__).resolve().parent / "fixtures"
                     / "espn_schedule_repair_lookahead.json").read_text())
FAILS = 0
TEXT = "WagerStalk 💎\n\n1.5U Kentucky +10.5 \n\n1.5U Georgia +2.5"


async def _fixture_fetch(sport, date_str, *a, **kw):
    return {"events": SLATES.get(f"{sport}_{date_str.replace('-', '')}", [])}


def check(label, got, want):
    global FAILS
    ok = got == want
    print(f"{'✓' if ok else '✗'} {label}: {got}" + ("" if ok else f"  (want {want})"))
    if not ok:
        FAILS += 1


def picks():
    return [
        {"description": "Kentucky Wildcats +10.5", "sport": None, "bet_type": "spread",
         "teams": ["Kentucky Wildcats"], "line": 10.5},
        {"description": "Georgia +2.5", "sport": None, "bet_type": "spread",
         "teams": ["Georgia Bulldogs"], "line": 2.5},
    ]


async def main():
    real = scores.fetch_espn
    scores.fetch_espn = _fixture_fetch
    try:
        res = await scores.verify_picks_on_schedule(
            picks(), "NCAAF", TEXT, "2026-10-07", {})
        check("Kentucky (Sat) posted Wed is confirmed, not rebound to WKU (Thu)",
              (res[0]["status"], res[0]["teams"]), ("confirmed", ["Kentucky Wildcats"]))
        check("Georgia (Sat) posted Wed is confirmed, not unverified",
              (res[1]["status"], res[1]["teams"]), ("confirmed", ["Georgia Bulldogs"]))

        # A team on no slate all window is still repaired from the near-day slate.
        bad = [{"description": "Kentucky State Thorobreds +10.5", "teams": ["Kentucky State Thorobreds"]}]
        res = await scores.verify_picks_on_schedule(bad, "NCAAF", TEXT, "2026-10-07", {})
        check("team absent all window still repairs from the near-day slate",
              (res[0]["status"], res[0]["teams"]), ("corrected", ["Western Kentucky Hilltoppers"]))

        # Fixture reproduces the incident when the window is collapsed to the old 2 days.
        old = scores.VALIDATE_WINDOW
        scores.VALIDATE_WINDOW = (0, 1)
        try:
            res = await scores.verify_picks_on_schedule(
                picks(), "NCAAF", TEXT, "2026-10-07", {})
        finally:
            scores.VALIDATE_WINDOW = old
        check("old 2-day window reproduces the WKU rebind",
              (res[0]["status"], res[0]["teams"]), ("corrected", ["Western Kentucky Hilltoppers"]))
    finally:
        scores.fetch_espn = real
    print("PASS" if not FAILS else f"FAIL ({FAILS})")
    sys.exit(1 if FAILS else 0)


if __name__ == "__main__":
    asyncio.run(main())
