"""Regression test: soccer period bets get half scores in their grade context.

Self-contained (ESPN is stubbed with real fixture payloads) — run directly:

    ~/venv/bin/python scripts/test_soccer_halves_context.py

James Bets "Bayern / Bodo over 1.5 1H" (2026-09-10, UCL): the game finished
5-0 with every goal in the second half, but both fan-out copies sat UNKNOWN —
fetch_soccer_context builds its context from the league scoreboards, whose
`linescores` arrays ESPN ships EMPTY for soccer, so the grader saw only
"FT 5-0" and (correctly) refused to grade a first-half total, every attempt
until the ungradeable cap parked it. The half split lives only in the match
summary endpoint. Now build_context requests the summary's line scores
whenever the pick's period isn't "game":

  1. include_linescores=True  -> context carries P1/P2 half scores
  2. include_linescores=False -> byte-identical old context and NO summary
     fetch (the widened helper must not change old callers' contract or
     per-cycle fetch volume)
  3. build_context plumb: a real parsed 1h pick reaches the summary halves,
     the same pick as a full-game total does not
"""

import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import httpx

import scores
from ai import build_context

FIXTURES = Path(__file__).resolve().parent / "fixtures"
SCOREBOARD = json.loads((FIXTURES / "espn_ucl_scoreboard_20260910.json").read_text())
SUMMARY = json.loads((FIXTURES / "espn_ucl_summary_bayern_bodo_20260910.json").read_text())

# The exact parsed pick from parse_cache.json -1002486251914:3801
PICK_1H = {
    "description": "Bayern Munich vs Bodo/Glimt over 1.5 goals first half",
    "sport": None,
    "bet_type": "total",
    "is_parlay_leg": False,
    "period": "1h",
    "teams": ["Bayern Munich", "Bodo/Glimt"],
    "player": None,
    "prop_stat": None,
    "line": 1.5,
    "direction": "over",
}


class _Resp:
    def __init__(self, data):
        self._data = data

    def raise_for_status(self):
        pass

    def json(self):
        return self._data


class _StubClient:
    summary_gets = 0

    def __init__(self, *a, **kw):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def get(self, url, params=None, timeout=None):
        if url.endswith("/summary"):
            type(self).summary_gets += 1
            return _Resp(SUMMARY)
        if "uefa.champions/scoreboard" in url:
            return _Resp(SCOREBOARD)
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
        teams = PICK_1H["teams"]

        # 1. Period bet: halves present
        _StubClient.summary_gets = 0
        ctx, game_date = asyncio.run(
            scores.fetch_soccer_context(teams, "2026-09-10", include_linescores=True))
        check("1h context has P1/P2 halves", "P1=0" in ctx and "P2=5" in ctx, ctx)
        check("game_date preserved", game_date == "2026-09-10", game_date)
        check("summary fetched once", _StubClient.summary_gets == 1,
              str(_StubClient.summary_gets))

        # 2. Old contract: no flag -> no summary fetch, no halves
        _StubClient.summary_gets = 0
        ctx_plain, _ = asyncio.run(scores.fetch_soccer_context(teams, "2026-09-10"))
        check("full-game context has no halves", "P1=" not in ctx_plain, ctx_plain)
        check("no summary fetch without flag", _StubClient.summary_gets == 0,
              str(_StubClient.summary_gets))
        check("final score still present", "5" in ctx_plain and "Bayern" in ctx_plain,
              ctx_plain)

        # 3. build_context plumbs the period through
        _StubClient.summary_gets = 0
        ctx3, _ = asyncio.run(build_context("Soccer", "2026-09-10", PICK_1H, None, {}))
        check("build_context 1h pick sees halves", "P1=0" in ctx3, ctx3)

        _StubClient.summary_gets = 0
        full_game = {**PICK_1H, "period": "game"}
        ctx4, _ = asyncio.run(build_context("Soccer", "2026-09-10", full_game, None, {}))
        check("build_context full-game pick skips summary",
              _StubClient.summary_gets == 0 and "P1=" not in ctx4, ctx4)
    finally:
        httpx.AsyncClient = real_client

    print()
    if failures:
        print(f"FAILED: {len(failures)}: {failures}")
        return 1
    print("All soccer halves context tests passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
