"""Regression test: a partial team match in an earlier league can't answer for the real game.

Self-contained (ESPN is stubbed with real fixture payloads) — run directly:

    ~/venv/bin/python scripts/test_soccer_partial_match.py

James Bets "England / Spain BTTS" (2026-09-26, Nations League, FT England 2-3
Spain) sat ⏳ on both fan-out copies for hours: fetch_soccer_context walked
SOCCER_LEAGUES in order and returned on the FIRST league with any match —
"England" matches "New England Revolution", MLS (usa.1) precedes uefa.nations,
and that MLS game hadn't kicked off, so every pass answered PENDING. Had the
MLS game finished first, the BTTS would have graded the wrong match.

  1. both leagues live: the full (England AND Spain) completed match wins
  2. the same with the partial hit COMPLETED: still the full match (no
     wrong-game grade)
  3. full match pending + partial completed: PENDING (never the partial)
  4. single-team pick keeps the old league-order behaviour
  5. find_event_ids ranks full matches first (the daemon's score-header
     lookup `_find_event_for_pick` takes ids[0] off the merged scoreboard)
  6. build_context plumb: the exact cached parse gets the Nations League
     final with BTTS stats context
"""

import asyncio
import copy
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import httpx

import scores
from ai import build_context

FIXTURES = Path(__file__).resolve().parent / "fixtures"
MLS = json.loads((FIXTURES / "espn_usa_1_scoreboard_20260926.json").read_text())
NATIONS = json.loads((FIXTURES / "espn_uefa_nations_scoreboard_20260926.json").read_text())

# The exact parsed pick from parse_cache.json -1002486251914:3901
PICK = {
    "description": "England vs Spain - Both Teams To Score (BTTS)",
    "sport": None,
    "bet_type": "prop",
    "is_parlay_leg": False,
    "period": "game",
    "teams": ["England", "Spain"],
    "player": None,
    "prop_stat": "BTTS",
    "line": None,
    "direction": "over",
}


def _with_completed(sb, completed):
    sb = copy.deepcopy(sb)
    for e in sb["events"]:
        e["status"]["type"]["completed"] = completed
        e["status"]["type"]["state"] = "post" if completed else "pre"
    return sb


class _Resp:
    def __init__(self, data):
        self._data = data

    def raise_for_status(self):
        pass

    def json(self):
        return self._data


class _StubClient:
    mls = MLS
    nations = NATIONS
    only_date = "20260926"

    def __init__(self, *a, **kw):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def get(self, url, params=None, timeout=None):
        if url.endswith("/summary"):
            return _Resp({})
        if (params or {}).get("dates") != self.only_date:
            return _Resp({"events": []})
        if "usa.1/scoreboard" in url:
            return _Resp(type(self).mls)
        if "uefa.nations/scoreboard" in url:
            return _Resp(type(self).nations)
        return _Resp({"events": []})


def main() -> int:
    failures = []

    def check(name, cond, detail=""):
        status = "PASS" if cond else "FAIL"
        print(f"  [{status}] {name}" + (f" — {detail}" if detail and not cond else ""))
        if not cond:
            failures.append(name)

    real_client = httpx.AsyncClient
    httpx.AsyncClient = _StubClient
    try:
        teams = PICK["teams"]
        run = lambda t=teams: asyncio.run(scores.fetch_soccer_context(t, "2026-09-26"))

        # Fixture sanity: the live state that caused the incident
        check("fixture: MLS partial hit is unstarted",
              not MLS["events"][0]["status"]["type"]["completed"])
        check("fixture: Nations League game is final",
              NATIONS["events"][0]["status"]["type"]["completed"])

        # 1. The incident
        ctx, d = run()
        check("full completed match beats earlier-league partial pending",
              ctx not in ("PENDING", "") and "Spain" in ctx and "Revolution" not in ctx, ctx)
        check("game_date is the match date", d == "2026-09-26", d)

        # 2. Partial hit completed too — must still bind the real match
        _StubClient.mls = _with_completed(MLS, True)
        ctx, _ = run()
        check("completed partial never outranks the full match",
              "Spain" in ctx and "Revolution" not in ctx, ctx)

        # 3. Full match pending, partial completed → PENDING, not the wrong game
        _StubClient.nations = _with_completed(NATIONS, False)
        ctx, _ = run()
        check("pending full match → PENDING, not the completed partial",
              ctx == "PENDING", ctx)
        _StubClient.mls, _StubClient.nations = MLS, NATIONS

        # 4. Single-team pick: any hit is a full match; completed still wins
        ctx, _ = run(["Spain"])
        check("single-team pick finds its game", "Spain" in ctx, ctx)

        # 5. Ranked find_event_ids / _find_event_for_pick on a merged scoreboard
        merged = {"events": MLS["events"] + NATIONS["events"]}
        ids = scores.find_event_ids(merged["events"], teams)
        check("find_event_ids ranks the full match first",
              ids and ids[0] == NATIONS["events"][0]["id"], str(ids))
        ev = scores._find_event_for_pick(merged, teams)
        check("_find_event_for_pick binds the Nations League game",
              ev and ev["id"] == NATIONS["events"][0]["id"],
              ev and ev.get("name"))
        ids1 = scores.find_event_ids(merged["events"], ["England"])
        check("single-term ties keep scoreboard order",
              ids1 == [MLS["events"][0]["id"], NATIONS["events"][0]["id"]], str(ids1))

        # 6. Through build_context with the real cached parse
        ctx6, _ = asyncio.run(build_context("Soccer", "2026-09-26", PICK, None, {}))
        check("build_context grades off the Nations League final",
              "Spain" in ctx6 and "England" in ctx6 and "Revolution" not in ctx6, ctx6)
    finally:
        httpx.AsyncClient = real_client

    print()
    if failures:
        print(f"FAILED: {len(failures)}")
        return 1
    print("All checks passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
