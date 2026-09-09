#!/usr/bin/env python3
"""Regression: cross-league nickname collisions resolve via the schedule.

Pins the 2026-09-05 failure: "Liberty +7 [-105]" (Liberty Flames at James
Madison, kickoff 18 minutes after the post) parsed as WNBA "New York Liberty".
There were ZERO WNBA games that day, so grading found no game, returned UNKNOWN
until the attempt cap, and the daemon retired the pick unresolved — while the
actual game landed 13-20, an exact PUSH on +7. Bare "Liberty" is the WNBA
team's nickname AND the NCAAF school's name, so `resolve_nickname_collision`
must arbitrate from the raw token + schedule before validate_sport runs.

Also pins the original "Snakes" entry (Diamondbacks vs Whipsnakes), which
shipped without a test, and the candidate-order trap: the WNBA name's last word
("liberty") is a substring of a correct "Liberty Flames" parse, so the NCAAF
candidate must be listed first or good parses would mis-key `matched`.

Fully offline — prefilled scoreboard cache + stubbed fetch_espn, no API calls.

    python scripts/test_nickname_collision.py
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
    # Any league the test didn't pre-fill is dark that day — and stays offline.
    return {"events": []}


def _sb(*matchups: tuple[str, str], date: str = "") -> dict:
    return {"events": [
        {
            "id": str(i + 1),
            "date": date,
            "competitions": [{"competitors": [
                {"team": {"displayName": away}},
                {"team": {"displayName": home}},
            ]}],
        }
        for i, (away, home) in enumerate(matchups)
    ]}


NCAAF_0905 = _sb(("Liberty Flames", "James Madison Dukes"), date="2026-09-05T16:00Z")
POST_0905 = datetime(2026, 9, 5, 15, 42, 29, tzinfo=timezone.utc)


async def main() -> None:
    scores.fetch_espn = _no_network

    # 1. The incident, byte-exact parse fields: WNBA slate is empty, the Flames
    #    kick off 18 minutes after the post — resolves to NCAAF.
    cache = {("WNBA", "2026-09-05"): {"events": []}, ("NCAAF", "2026-09-05"): NCAAF_0905}
    got = await scores.resolve_nickname_collision(
        "WNBA", ["New York Liberty"], "New York Liberty +7",
        "Breaking Bank\n\nLiberty +7 [-105]\n\n3-0 NCAAF\n8-2 L10",
        "2026-09-05", cache, post_time=POST_0905,
    )
    check("incident resolves to Flames", got,
          ("NCAAF", ["Liberty Flames"], "Liberty Flames +7", None))

    # 2. A legitimate mid-season WNBA Liberty pick stays put (NCAAF is dark).
    cache = {("WNBA", "2026-07-15"): _sb(("New York Liberty", "Las Vegas Aces"),
                                         date="2026-07-15T23:00Z")}
    got = await scores.resolve_nickname_collision(
        "WNBA", ["New York Liberty"], "New York Liberty ML",
        "Liberty ML tonight", "2026-07-15", cache,
    )
    check("in-season WNBA pick unchanged", got,
          ("WNBA", ["New York Liberty"], "New York Liberty ML", None))

    # 3. A correct NCAAF parse must come back untouched — candidate order guard:
    #    "liberty" (WNBA last word) substring-matches "liberty flames", and a
    #    WNBA-first list would mis-key `matched` and mangle the description via
    #    _swap_team_in_desc ("Liberty Flames Flames +7").
    cache = {("WNBA", "2026-09-05"): {"events": []}, ("NCAAF", "2026-09-05"): NCAAF_0905}
    got = await scores.resolve_nickname_collision(
        "NCAAF", ["Liberty Flames"], "Liberty Flames +7",
        "Liberty +7 [-105]", "2026-09-05", cache, post_time=POST_0905,
    )
    check("correct Flames parse unchanged", got,
          ("NCAAF", ["Liberty Flames"], "Liberty Flames +7", None))

    # 4. Both play the same day, post time not decisive → flagged, never guessed.
    cache = {
        ("WNBA", "2026-09-12"): _sb(("New York Liberty", "Indiana Fever"),
                                    date="2026-09-12T23:00Z"),
        ("NCAAF", "2026-09-12"): _sb(("Liberty Flames", "Old Dominion Monarchs"),
                                     date="2026-09-12T22:00Z"),
    }
    got = await scores.resolve_nickname_collision(
        "WNBA", ["New York Liberty"], "New York Liberty +7",
        "Liberty +7", "2026-09-12", cache,
        post_time=datetime(2026, 9, 12, 12, 0, tzinfo=timezone.utc),
    )
    check("same-day double is kept", got[:3],
          ("WNBA", ["New York Liberty"], "New York Liberty +7"))
    check("same-day double warns", bool(got[3]), True)

    # 5. Same-day double WITH a decisive post time (18 min before one kickoff,
    #    7h+ before the other) resolves to the near game.
    cache = {
        ("WNBA", "2026-09-05"): _sb(("New York Liberty", "Indiana Fever"),
                                    date="2026-09-05T23:00Z"),
        ("NCAAF", "2026-09-05"): NCAAF_0905,
    }
    got = await scores.resolve_nickname_collision(
        "WNBA", ["New York Liberty"], "New York Liberty +7",
        "Liberty +7 [-105]", "2026-09-05", cache, post_time=POST_0905,
    )
    check("pregame window breaks the tie", got,
          ("NCAAF", ["Liberty Flames"], "Liberty Flames +7", None))

    # 6. The original "Snakes" incident stays pinned: MLB parse, only the
    #    Whipsnakes play → flips to Lacrosse.
    cache = {
        ("MLB", "2026-06-14"): {"events": []},
        ("Lacrosse", "2026-06-14"): _sb(("Maryland Whipsnakes", "Denver Outlaws"),
                                        date="2026-06-14T22:00Z"),
    }
    got = await scores.resolve_nickname_collision(
        "MLB", ["Arizona Diamondbacks"], "Arizona Diamondbacks -1.5",
        "Snakes -1.5 tonight", "2026-06-14", cache,
    )
    check("snakes resolves to Whipsnakes", got,
          ("Lacrosse", ["Maryland Whipsnakes"], "Maryland Whipsnakes -1.5", None))

    # 7. No collision token in the raw message → untouched, no lookups.
    got = await scores.resolve_nickname_collision(
        "WNBA", ["Los Angeles Sparks"], "Los Angeles Sparks ML",
        "Sparks ML", "2026-07-15", {},
    )
    check("no token untouched", got,
          ("WNBA", ["Los Angeles Sparks"], "Los Angeles Sparks ML", None))

    # 8. Token present but Claude landed outside the candidate set → this map
    #    isn't what's in play, leave it alone.
    got = await scores.resolve_nickname_collision(
        "WNBA", ["Minnesota Lynx"], "Minnesota Lynx -3",
        "Lynx -3 over Liberty", "2026-07-15", {},
    )
    check("non-candidate parse untouched", got,
          ("WNBA", ["Minnesota Lynx"], "Minnesota Lynx -3", None))

    print()
    if FAILS:
        print(f"{FAILS} FAILED")
        sys.exit(1)
    print("all passed")


if __name__ == "__main__":
    asyncio.run(main())
