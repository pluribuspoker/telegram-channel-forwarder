#!/usr/bin/env python3
"""Build ``data/college_team_names.json``: ESPN college full name → school name.

The broadcast renderer (``audit._short_team``) shortens team names through
exact-name tables only — heuristic stripping mangles "Ohio State" or
"Coastal Carolina". Pro teams keep their nickname ("Broncos"); college teams
keep their SCHOOL ("Appalachian State Mountaineers" → "Appalachian State"),
the way cappers write them. This pulls ESPN's own ``displayName`` →
``location`` pairs for college football and men's/women's basketball, so the
table matches the names the parse and ESPN binding produce.

Rerun when ESPN adds schools (conference realignment, new D1 programs); the
output is sorted and byte-stable.
"""
import json
import os
import sys
import urllib.request

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT = os.path.join(_ROOT, "data", "college_team_names.json")
LEAGUES = (
    "football/college-football",
    "basketball/mens-college-basketball",
    "basketball/womens-college-basketball",
)
URL = "https://site.api.espn.com/apis/site/v2/sports/{}/teams?limit=2000"


def fetch(league: str) -> list[dict]:
    req = urllib.request.Request(URL.format(league), headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=30) as r:
        data = json.load(r)
    return [t["team"] for t in data["sports"][0]["leagues"][0]["teams"]]


def main() -> int:
    sys.path.insert(0, _ROOT)
    from audit import _TEAM_SHORT_NAMES_RAW
    pro = {k.casefold() for k in _TEAM_SHORT_NAMES_RAW}
    table: dict[str, str] = {}
    mascots: set[str] = set()
    schools: set[str] = set()
    conflicts = []
    for league in LEAGUES:
        for t in fetch(league):
            full = " ".join(str(t.get("displayName", "")).split())
            school = " ".join(str(t.get("location", "")).split())
            if school:
                schools.add(school.casefold())
            mascot = " ".join(str(t.get("name", "")).split())
            if mascot and full == f"{school} {mascot}":
                mascots.add(mascot)
            if not full or not school or full == school or full.casefold() in pro:
                continue
            if not full.startswith(school):
                continue  # school must be the name's own prefix — never rename
            prev = table.setdefault(full, school)
            if prev != school:
                conflicts.append((full, prev, school))
    for full, a, b in conflicts:
        print(f"conflict: {full!r}: {a!r} vs {b!r} (kept the first)", file=sys.stderr)
    # A mascot that is also the tail of some school's name ("Green", "State")
    # would cut that school short — never strip it.
    safe = sorted(m for m in mascots
                  if not any(s == m.casefold() or s.endswith(" " + m.casefold())
                             for s in schools))
    dropped = sorted(mascots - set(safe))
    if dropped:
        print(f"unsafe mascots (end a school name): {dropped}", file=sys.stderr)
    with open(OUT, "w", encoding="utf-8") as f:
        json.dump({"names": dict(sorted(table.items())), "mascots": safe},
                  f, ensure_ascii=False, indent=0)
        f.write("\n")
    print(f"{len(table)} names, {len(safe)} mascots → {OUT}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
