"""Regression test: tennis grading must go PENDING until the match is final.

Self-contained (no network/API) — run it directly:

    ~/venv/bin/python scripts/test_tennis_pending.py

2026-09-08 (-1004394797084:86): "Alcaraz ML (-410)" was the US Open QF vs
Ben Shelton, a night session that started 2026-09-09T03:00Z. The tennis
context fetcher has no pending concept, so from 17:56Z it fed the scheduled
match (no winner flag) to claude_grade six times — the full UNKNOWN-attempt
cap — hours before first serve, and the leg went terminal while the match
was still gradeable the next morning. Fix: an unfinished match returns
"PENDING" (mapped to CONTEXT_PENDING like KBO/CFL, which never burns an
attempt), and candidate matches are ranked closest-date-first with the
upcoming match beating the previous day's on a tie, so page order can never
hand back yesterday's final for tomorrow's match.

Fixture: the real ESPN core API competition 182780 (Shelton d. Alcaraz
7-6(?) set scores as served) captured 2026-09-09, trimmed to the fields the
fetcher reads, never retyped. The pregame variant is the same competition
with the winner flags cleared — exactly what the core API served before the
match ended. The tie-break case shifts the fixture's date field only (the
sort under test cares about dates, not payload bytes).

2026-09-04 (-1004427337587:206): "Francis Tiafoe ML 2u" — ESPN spells him
"Frances Tiafoe", so the exact matcher never bound and the pick
context-skipped every cycle until retirement (zero UNKNOWN attempts burned).
Fix: _player_near_match — exact surname + first name within one edit — as a
strictly lower tier than an exact match, refused when two different players
fit (the Wang sisters problem). Second fixture: real competition 182691
(Tiafoe d. Vacherot), captured 2026-09-09.
"""
import asyncio
import copy
import json
import sys
import types
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import ai
import scores

FIXTURE = Path(__file__).resolve().parent / "fixtures" / "tennis_usopen_qf_20260909.json"
FIXTURE_TIAFOE = Path(__file__).resolve().parent / "fixtures" / "tennis_usopen_tiafoe_20260904.json"
SKIP = "__SKIP__"

FINAL_CONTEXT = (
    "Tennis match on 2026-09-09 (ATP):\n"
    "  Ben Shelton: S1=6 S2=6 S3=6 S4=1 S5=7 [WINNER]\n"
    "  Carlos Alcaraz: S1=7 S2=1 S3=3 S4=6 S5=6"
)

TIAFOE_CONTEXT = (
    "Tennis match on 2026-09-04 (ATP):\n"
    "  Valentin Vacherot: S1=4 S2=2 S3=4\n"
    "  Frances Tiafoe: S1=6 S2=6 S3=6 [WINNER]"
)

failures = []


def check(label, got, want):
    ok = got == want
    print(f"{'✅' if ok else '❌'} {label}: {got!r}" + ("" if ok else f" (want {want!r})"))
    if not ok:
        failures.append(label)


class FakeResponse:
    def __init__(self, payload):
        self._payload = payload

    def raise_for_status(self):
        pass

    def json(self):
        return self._payload


class FakeAsyncClient:
    """Routes the three URL shapes fetch_tennis_match_context hits."""

    comps = []
    linescores = {}

    def __init__(self, *a, **k):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def get(self, url, params=None):
        if url.endswith("/linescores"):
            athlete_id = url.rstrip("/linescores").rsplit("/", 1)[-1]
            return FakeResponse(FakeAsyncClient.linescores.get(athlete_id, {"items": []}))
        if "/tennis/atp/scoreboard" in url:
            return FakeResponse({"events": [{"id": "189-2026", "name": "US Open"}]})
        if "/tennis/wta/scoreboard" in url:
            return FakeResponse({"events": []})
        if url.endswith("/competitions"):
            return FakeResponse({"items": FakeAsyncClient.comps, "pageCount": 1})
        raise RuntimeError(f"unrouted URL in test: {url}")


def fetch(player, date):
    return asyncio.run(scores.fetch_tennis_match_context(player, date, SKIP))


def main():
    fx = json.load(open(FIXTURE))
    comp_final = fx["comp_final"]
    comp_pregame = copy.deepcopy(comp_final)
    for c in comp_pregame["competitors"]:
        c["winner"] = False

    scores.httpx = types.SimpleNamespace(AsyncClient=FakeAsyncClient)
    FakeAsyncClient.linescores = fx["linescores"]

    # 1. The incident: pick dated 2026-09-08, match scheduled for the 9th (UTC),
    #    not final yet → PENDING, never a gradeable context.
    FakeAsyncClient.comps = [comp_pregame]
    check("pregame match → PENDING", fetch("Carlos Alcaraz", "2026-09-08"), "PENDING")

    # 2. Same pick after the final → graded context via the ±1 fallback.
    FakeAsyncClient.comps = [comp_final]
    check("final via ±1 fallback", fetch("Carlos Alcaraz", "2026-09-08"), FINAL_CONTEXT)

    # 3. Exact-date pick still grades in the first pass.
    check("final on exact date", fetch("Carlos Alcaraz", "2026-09-09"), FINAL_CONTEXT)

    # 4. Date tie: yesterday's final and tomorrow's unplayed match are both
    #    ±1 — the upcoming match must win (else the pick grades the wrong match).
    comp_prev = copy.deepcopy(comp_final)
    comp_prev["id"] = "888881"
    comp_prev["date"] = "2026-09-07T03:00Z"
    FakeAsyncClient.comps = [comp_prev, comp_pregame]
    check("tie prefers upcoming → PENDING", fetch("Carlos Alcaraz", "2026-09-08"), "PENDING")

    # 5. Player not on the board at all → CONTEXT_SKIP (the attempt cap still
    #    guards genuinely ungradeable names).
    FakeAsyncClient.comps = [comp_final]
    check("unknown player → SKIP", fetch("Novak Djokovic", "2026-09-08"), SKIP)

    # 6. The 2026-09-04 incident: "Francis Tiafoe" must find ESPN's
    #    "Frances Tiafoe" — exact surname, first name one edit off.
    fx2 = json.load(open(FIXTURE_TIAFOE))
    tiafoe_final = fx2["comp_final"]
    FakeAsyncClient.linescores = {**fx["linescores"], **fx2["linescores"]}
    FakeAsyncClient.comps = [tiafoe_final]
    check("near-miss first name grades", fetch("Francis Tiafoe", "2026-09-04"), TIAFOE_CONTEXT)

    # 7. Surname-only picks keep flowing through the exact matcher.
    check("surname-only still exact", fetch("Tiafoe", "2026-09-04"), TIAFOE_CONTEXT)

    # 8. An exact match outranks a same-window near-miss: with both Wang
    #    sisters on the board, "Xinyu Wang" binds Xinyu, never Xiyu.
    wang_a = copy.deepcopy(tiafoe_final)
    wang_a["id"] = "777771"
    wang_a["competitors"][0]["name"] = "Xiyu Wang"
    wang_b = copy.deepcopy(tiafoe_final)
    wang_b["id"] = "777772"
    wang_b["competitors"][0]["name"] = "Xinyu Wang"
    FakeAsyncClient.comps = [wang_a, wang_b]
    got = fetch("Xinyu Wang", "2026-09-04")
    check("exact beats near-miss", "Xinyu Wang" in got and "Xiyu Wang" not in got, True)

    # 9. A spelling one edit from TWO different players is ambiguous → SKIP
    #    (never guess which sister was meant).
    check("ambiguous near-miss → SKIP", fetch("Xnyu Wang", "2026-09-04"), SKIP)

    # 10. build_context maps the sentinel to CONTEXT_PENDING (no attempt burned).
    async def fake_fetch(player, date, skip):
        return "PENDING"

    real = ai.fetch_tennis_match_context
    try:
        ai.fetch_tennis_match_context = fake_fetch
        ctx, gd = asyncio.run(ai.build_context(
            "Tennis", "2026-09-08",
            {"teams": ["Carlos Alcaraz"], "bet_type": "moneyline", "period": "game"},
            None, {}))
        check("build_context → CONTEXT_PENDING", ctx, ai.CONTEXT_PENDING)
        check("build_context keeps date", gd, "2026-09-08")
    finally:
        ai.fetch_tennis_match_context = real

    print()
    if failures:
        print(f"❌ {len(failures)} failure(s): {failures}")
        sys.exit(1)
    print("✅ all tennis pending checks passed")


if __name__ == "__main__":
    main()
