#!/usr/bin/env python3
"""Regression: a bare place name shared by a pro and a college team resolves
from the schedule around the post.

Pins the 2026-10-09 failure: "Dagger — Washington ML (main play) … other good
cappers on Washington today", posted 00:50Z, ten minutes before Iowa at
Washington, parsed as the Commanders. validate_sport and the schedule check
both confirmed it (the Commanders play Sunday, inside VALIDATE_WINDOW), so it
bound Giants at Washington, priced [-177] and sat pending two days on the wrong
game while the same pick via DAGGER graded ❌ on the Huskies.

Also pins the other direction: absence of the parsed team's game today is
never evidence (NFL picks post days ahead), a named other day or a typed
nickname leaves the parse alone, and an outage changes nothing.

Fully offline — prefilled scoreboard cache + stubbed fetch_espn.

    python scripts/test_location_collision.py
"""
import asyncio
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import scores  # noqa: E402

FAILS = 0


def check(label: str, got, want) -> None:
    global FAILS
    ok = got == want
    print(f"{'✓' if ok else '✗'} {label}: {got}" + ("" if ok else f"  (want {want})"))
    if not ok:
        FAILS += 1


async def _no_network(sport, date_str, *a, **kw):
    return {"events": []}


def _sb(*games: tuple[str, str, str]) -> dict:
    return {"events": [
        {"id": str(i + 1), "date": start,
         "competitions": [{"competitors": [
             {"team": {"displayName": away}}, {"team": {"displayName": home}}]}]}
        for i, (away, home, start) in enumerate(games)
    ]}


# Real kickoffs (ESPN): Iowa at Washington 2026-10-10T01:00Z (Fri 9 PM ET);
# Giants at Commanders 2026-10-11T17:00Z.
HUSKIES = ("Iowa Hawkeyes", "Washington Huskies", "2026-10-10T01:00Z")
COMMANDERS = ("New York Giants", "Washington Commanders", "2026-10-11T17:00Z")


def week_cache() -> dict:
    cache = {}
    for d in ("2026-10-09", "2026-10-10", "2026-10-11", "2026-10-12"):
        cache[("NCAAF", d)] = {"events": []}
        cache[("NFL", d)] = {"events": []}
    # Week-scoped college slate: Friday's game is listed on Saturday's board too.
    cache[("NCAAF", "2026-10-09")] = _sb(HUSKIES)
    cache[("NCAAF", "2026-10-10")] = _sb(HUSKIES)
    cache[("NFL", "2026-10-11")] = _sb(COMMANDERS)
    return cache


# Byte-exact raw_text of -1002486251914:3996 at parse time.
TEXT = ("Dagger \n\nWashington ML (main play)\n\nThere's a bunch of other good "
        "cappers on Washington today (The Pick Don, Midwest Mike)")
POST = datetime(2026, 10, 10, 0, 50, 53, tzinfo=timezone.utc)
CMD = ("NFL", ["Washington Commanders"], "Washington Commanders ML")


async def resolve(text, date_str, post, parse=CMD, cache=None):
    sport, teams, desc = parse
    got = await scores.resolve_location_collision(
        sport, teams, desc, text, date_str,
        week_cache() if cache is None else cache, post_time=post)
    return got[:3], got[3] is not None


async def main() -> None:
    scores.fetch_espn = _no_network
    huskies = (("NCAAF", ["Washington Huskies"], "Washington Huskies ML"), True)
    unchanged = (CMD, False)

    check("incident → Huskies", await resolve(TEXT, "2026-10-09", POST), huskies)
    check("imminent kickoff alone → Huskies",
          await resolve("Dagger \n\nWashington ML (main play)", "2026-10-09", POST), huskies)
    check("'today' hours before kickoff → Huskies",
          await resolve("Washington ML today", "2026-10-09",
                        datetime(2026, 10, 9, 16, 0, tzinfo=timezone.utc)), huskies)

    # Lookahead NFL picks keep the parse: no positive evidence for the Huskies.
    check("Friday afternoon, no day word → kept",
          await resolve("Washington ML", "2026-10-09",
                        datetime(2026, 10, 9, 16, 0, tzinfo=timezone.utc)), unchanged)
    check("Saturday post (Huskies already played) → kept",
          await resolve("Washington ML today", "2026-10-10",
                        datetime(2026, 10, 10, 16, 0, tzinfo=timezone.utc)), unchanged)
    check("names another day → kept",
          await resolve("Washington ML Sunday", "2026-10-09", POST), unchanged)
    check("typed nickname → kept",
          await resolve("Washington Commanders ML", "2026-10-09", POST), unchanged)
    check("Washington State is another school → kept",
          await resolve("Washington State ML", "2026-10-09", POST), unchanged)

    outage = week_cache()
    outage[("NCAAF", "2026-10-09")] = None
    check("post-date outage → kept",
          await resolve(TEXT, "2026-10-09", POST, cache=outage), unchanged)

    # A correct college parse stays college (the DAGGER copy, 3995).
    hp = huskies[0]
    check("correct Huskies parse → kept",
          await resolve("❗️❗️MAIN PLAY❗️❗️\n\nWashington ML -150 2U", "2026-10-09",
                        datetime(2026, 10, 10, 0, 49, 3, tzinfo=timezone.utc), parse=hp),
          (hp, False))
    # And the reverse flip: a Sunday-morning "Washington ML" parsed as the
    # Huskies (who don't play) is the Commanders kicking off in 1.5 h.
    check("Sunday morning Huskies parse → Commanders",
          await resolve("Washington ML", "2026-10-11",
                        datetime(2026, 10, 11, 15, 30, tzinfo=timezone.utc), parse=hp),
          (CMD, True))

    print(f"\n{'PASS' if not FAILS else f'FAIL ({FAILS})'}")
    sys.exit(1 if FAILS else 0)


if __name__ == "__main__":
    asyncio.run(main())
