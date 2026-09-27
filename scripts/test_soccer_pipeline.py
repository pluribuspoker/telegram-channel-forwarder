"""Regression test: the soccer grading pipeline — one source, strict binding, 90' math.

Self-contained (ESPN stubbed with real payloads captured 2026-09-26) — run directly:

    ~/venv/bin/python scripts/test_soccer_pipeline.py

Soccer kept breaking one patch at a time: a league missing from the 26-league
allowlist graded UNKNOWN (Allsvenskan, Nations League, a Conference League code
that 404'd), the first-hit name scan bound "England" to "New England
Revolution" and held a Nations League BTTS at ⏳ for hours (2026-09-26), and
every verdict was Claude reading a score as text. Now:

  A. source      — `soccer/all` in ONE call; league fan-out only when it fails,
                   is empty or may be truncated; per-date TTL cache
  B. binding     — both teams on one event, exact > word match, betting leagues
                   > long tail (the feed carries WSL clubs under identical
                   names), women's picks prefer women's leagues, ties AMBIGUOUS
  C. math        — 3-way ML (draw = loss), DNB, double chance, BTTS yes/no,
                   totals, team totals, whole/half handicaps, 1H/2H — all on
                   the 90' score (AET/PEN count P1+P2); refusals fall to Claude
  D. stored ids  — a binding is graded by id, a repaired parse re-binds, and
                   a pregame binding costs zero HTTP calls
  E. tracker     — bind once + one ambiguous flag; stuck-final tripwire once
  F. cache       — _pending_entry keeps the bindings; the save merge restores
                   bindings a concurrent writer's stale copy would drop
"""

import asyncio
import copy
import json
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import httpx

import scores
import tracker
import tracker_cache
from ai import build_context

FIXTURES = Path(__file__).resolve().parent / "fixtures"
SLATES = json.loads((FIXTURES / "espn_soccer_all_slates.json").read_text())
SUMMARIES = json.loads((FIXTURES / "espn_soccer_summaries.json").read_text())
EVENTS = {e["id"]: e for slate in SLATES.values() for e in slate}

# The exact parsed pick from parse_cache.json -1002486251914:3901
BTTS = {
    "description": "England vs Spain - Both Teams To Score (BTTS)",
    "sport": None, "bet_type": "prop", "is_parlay_leg": False, "period": "game",
    "teams": ["England", "Spain"], "player": None, "prop_stat": "BTTS",
    "line": None, "direction": "over",
}


def ev(name):
    return next(e for e in EVENTS.values() if e["name"] == name)


def with_status(event, completed, state, period=2, name=None):
    e = copy.deepcopy(event)
    t = e["status"]["type"]
    t["completed"], t["state"] = completed, state
    if name:
        t["name"] = name
    e["status"]["period"] = period
    return e


class _Resp:
    def __init__(self, data, status=200):
        self._data, self.status_code = data, status

    def raise_for_status(self):
        if self.status_code >= 400:
            raise httpx.HTTPStatusError("stub", request=None, response=None)

    def json(self):
        return self._data


class _Stub:
    """Serves `all` by date from `slates`, the fan-out leagues from `leagues`."""
    slates: dict = {}
    leagues: dict = {}
    all_status = 200
    calls: list = []

    def __init__(self, *a, **kw):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def get(self, url, params=None, timeout=None):
        type(self).calls.append(url)
        params = params or {}
        if url.endswith("/all/summary"):
            return _Resp(SUMMARIES.get(params.get("event"), {}))
        date = params.get("dates", "")
        iso = f"{date[:4]}-{date[4:6]}-{date[6:]}"
        if "/all/scoreboard" in url:
            if type(self).all_status != 200:
                return _Resp({}, type(self).all_status)
            return _Resp({"events": type(self).slates.get(iso, [])})
        if type(self).all_status != 200 and not type(self).leagues:
            return _Resp({}, type(self).all_status)  # ESPN wholly down
        for lg, by_date in type(self).leagues.items():
            if f"/{lg}/scoreboard" in url:
                return _Resp({"events": by_date.get(iso, [])})
        return _Resp({"events": []})


def reset(slates=None, leagues=None, all_status=200):
    scores.clear_soccer_cache()
    _Stub.slates, _Stub.leagues, _Stub.all_status = slates or {}, leagues or {}, all_status
    _Stub.calls = []


class _Audit:
    def __init__(self):
        self.sent = []

    async def warn(self, text):
        self.sent.append(text)


def main() -> int:
    failures = []

    def check(name, cond, detail=""):
        print(f"  [{'PASS' if cond else 'FAIL'}] {name}" + (f" — {detail}" if detail and not cond else ""))
        if not cond:
            failures.append(name)

    run = asyncio.run
    real = httpx.AsyncClient
    httpx.AsyncClient = _Stub
    try:
        slate26 = SLATES["2026-09-26"]
        ne, eng_spa = ev("New England Revolution at Real Salt Lake"), ev("Spain at England")

        print("A. source")
        reset({"2026-09-26": slate26})
        sb = run(scores.fetch_soccer_scoreboard("2026-09-26"))
        check("all-scoreboard served in one call", len(_Stub.calls) == 1 and len(sb["events"]) == len(slate26),
              str(_Stub.calls))
        run(scores.fetch_soccer_scoreboard("2026-09-26"))
        check("second read within TTL hits the cache", len(_Stub.calls) == 1, str(len(_Stub.calls)))
        reset(leagues={"uefa.nations": {"2026-09-26": [eng_spa]}}, all_status=503)
        sb = run(scores.fetch_soccer_scoreboard("2026-09-26"))
        check("all-scoreboard down → league fan-out", sb and [e["id"] for e in sb["events"]] == [eng_spa["id"]],
              str(sb))
        check("fan-out includes the Conference League's real code",
              any("/uefa.europa.conf/" in u for u in _Stub.calls))
        reset({"2026-09-26": [eng_spa] * scores.SOCCER_ALL_LIMIT},
              leagues={"uefa.nations": {"2026-09-26": [eng_spa]}})
        sb = run(scores.fetch_soccer_scoreboard("2026-09-26"))
        check("possibly-truncated all response → fan-out (deduped)", len(sb["events"]) == 1, str(len(sb["events"])))
        reset(all_status=503)
        check("ESPN wholly down → None (distinguishable from an empty slate)",
              run(scores.fetch_soccer_scoreboard("2026-09-26")) is None)

        print("B. binding")
        e, st = scores.bind_soccer_event(slate26, ["England", "Spain"])
        check("the incident: England/Spain binds the Nations League game", st == "bound" and e["id"] == eng_spa["id"],
              f"{st} {e and e['name']}")
        done_ne = with_status(ne, True, "post", name="STATUS_FULL_TIME")
        e, st = scores.bind_soccer_event([done_ne, eng_spa], ["England", "Spain"])
        check("a finished partial never outranks the full match", e["id"] == eng_spa["id"], e["name"])
        e, st = scores.bind_soccer_event([ne, eng_spa], ["England"])
        check("single term: exact 'England' beats 'New England Revolution'",
              st == "bound" and e["id"] == eng_spa["id"], f"{st} {e and e['name']}")
        lfc_wsl = ev("Everton at Liverpool")
        lfc_epl = ev("Liverpool at AFC Bournemouth")
        lfc_uru = ev("Liverpool at Central Español Fútbol Club")
        e, st = scores.bind_soccer_event([lfc_wsl, lfc_epl], ["Liverpool"], "Liverpool ML")
        check("EPL Liverpool beats the WSL's identical 'Liverpool'", st == "bound" and e["id"] == lfc_epl["id"],
              f"{st} {e and e['name']}")
        e, st = scores.bind_soccer_event([lfc_wsl, lfc_epl], ["Liverpool"], "Liverpool Women ML")
        check("a women's pick binds the WSL game", st == "bound" and e["id"] == lfc_wsl["id"],
              f"{st} {e and e['name']}")
        e, st = scores.bind_soccer_event(SLATES["2026-09-27_liverpool"], ["Liverpool"], "Liverpool ML")
        check("two long-tail 'Liverpool's (WSL + Montevideo) → ambiguous, never guessed", st == "ambiguous", st)
        e, st = scores.bind_soccer_event(slate26, ["Real Madrid", "Barcelona"])
        check("no match → none", st == "none" and e is None, st)

        print("C. math")
        s_es = SUMMARIES["401861066"]

        def g(pick, event=eng_spa, summ=s_es):
            r = scores.soccer_grade_math({"period": "game", **pick}, event, summ)
            return r[0] if r else None

        check("BTTS yes on England 2-3 Spain → WIN", g(BTTS) == "WIN")
        check("BTTS no → LOSS", g({**BTTS, "description": "England vs Spain BTTS No"}) == "LOSS")
        check("BTTS with a price in the text still grades", g({**BTTS, "description": "England/Spain BTTS +100"}) == "WIN")
        check("BTTS combo refused (Claude)", g({**BTTS, "description": "England/Spain BTTS & Over 2.5"}) is None)
        tot = {"bet_type": "total", "teams": ["England", "Spain"], "description": "England/Spain o4.5"}
        check("total over 4.5 (5 goals) → WIN", g({**tot, "line": 4.5, "direction": "over"}) == "WIN")
        check("total under 4.5 → LOSS", g({**tot, "line": 4.5, "direction": "under"}) == "LOSS")
        check("total 5 exact → PUSH", g({**tot, "line": 5, "direction": "over"}) == "PUSH")
        check("quarter line 4.75 refused (split stake)", g({**tot, "line": 4.75, "direction": "over"}) is None)
        check("corners total refused", g({**tot, "line": 9.5, "direction": "over",
                                          "description": "England/Spain over 9.5 corners"}) is None)
        check("1H total o2.5 (2-1 at half) → WIN",
              g({**tot, "period": "1h", "line": 2.5, "direction": "over"}) == "WIN")
        check("2H total u2.5 (0-2 after half) → WIN",
              g({**tot, "period": "2h", "line": 2.5, "direction": "under"}) == "WIN")
        ml = {"bet_type": "moneyline", "description": "Spain ML"}
        check("Spain ML → WIN", g({**ml, "teams": ["Spain"]}) == "WIN")
        check("England ML → LOSS", g({**ml, "teams": ["England"], "description": "England ML"}) == "LOSS")
        check("1H England ML (2-1) → WIN", g({**ml, "teams": ["England"], "period": "1h"}) == "WIN")
        check("spread Spain -1 (won by 1) → PUSH",
              g({"bet_type": "spread", "teams": ["Spain"], "line": -1, "description": "Spain -1"}) == "PUSH")
        check("spread England +1.5 → WIN",
              g({"bet_type": "spread", "teams": ["England"], "line": 1.5, "description": "England +1.5"}) == "WIN")
        check("team total England o1.5 → WIN",
              g({"bet_type": "team_total", "teams": ["England"], "line": 1.5, "direction": "over",
                 "description": "England TT o1.5"}) == "WIN")
        check("double chance England or draw → LOSS",
              g({"bet_type": "double_chance", "teams": ["England"], "description": "England or Draw"}) == "LOSS")
        check("'to advance' refused", g({**ml, "teams": ["Spain"], "description": "Spain to advance"}) is None)
        # AET: Argentina 3-1 Switzerland after extra time, 1-1 at 90'
        arg = ev("Switzerland at Argentina")
        s_arg = SUMMARIES["760513"]
        check("AET: Argentina ML on a 1-1 90' → LOSS (recorded LOSS 2026-07-11)",
              g({**ml, "teams": ["Argentina"], "description": "Argentina moneyline"}, arg, s_arg) == "LOSS")
        check("AET: Argentina DNB → PUSH",
              g({"bet_type": "draw_no_bet", "teams": ["Argentina"], "description": "Argentina DNB"}, arg, s_arg) == "PUSH")
        check("AET: Argentina double chance → WIN",
              g({"bet_type": "double_chance", "teams": ["Argentina"],
                 "description": "Argentina double chance"}, arg, s_arg) == "WIN")
        cv = ev("Cape Verde at Argentina")
        check("AET: o2.5 on 1-1 at 90' (3-2 after ET) → LOSS",
              g({**tot, "teams": ["Argentina"], "line": 2.5, "direction": "over",
                 "description": "Argentina game over 2.5 goals"}, cv, SUMMARIES[cv["id"]]) == "LOSS")
        pen = ev("East Fife at Inverness Caledonian Thistle")
        s_pen = SUMMARIES["401874208"]
        check("PEN (no ET): shootout ignored — Inverness ML on 1-1 → LOSS",
              g({**ml, "teams": ["Inverness Caledonian Thistle"], "description": "Inverness ML"}, pen, s_pen) == "LOSS")
        check("PEN: BTTS yes on 1-1 → WIN", g({**BTTS, "teams": ["East Fife", "Inverness"]}, pen, s_pen) == "WIN")
        live_2h = with_status(eng_spa, False, "in", period=2, name="STATUS_SECOND_HALF")
        check("live 2nd half: 1H bet settles", g({**tot, "period": "1h", "line": 2.5, "direction": "over"},
                                                  live_2h) == "WIN")
        check("live 2nd half: full-game bet waits (VAR can undo a goal)", g(BTTS, live_2h) is None)
        bad = copy.deepcopy(s_es)
        bad["header"]["competitions"][0]["competitors"][0]["linescores"] = [{"displayValue": "1"}]
        check("line scores that don't add up to the score → refused", g(BTTS, eng_spa, bad) is None)
        check("no summary → refused", g(BTTS, eng_spa, None) is None)

        print("D. stored binding + context")
        reset({"2026-09-26": slate26})
        r = run(scores.try_soccer_grade_math(BTTS, "2026-09-26"))
        check("try_soccer_grade_math: incident pick grades WIN", r and r[0] == "WIN" and r[2] == "2026-09-26", str(r))
        rec = scores.binding_record(eng_spa, "2026-09-26", "bound", BTTS["teams"])
        reset({"2026-09-26": [ne, eng_spa]})
        e, st, d = run(scores.soccer_bind(["England"], "2026-09-26", bound={**rec, "teams": ["England"]}))
        check("stored id is authoritative", e and e["id"] == eng_spa["id"], str(st))
        e, st, d = run(scores.soccer_bind(["Portugal"], "2026-09-26", bound=rec))
        check("repaired parse (teams changed) ignores the old binding", st == "none", st)
        future = {**rec, "kickoff": (datetime.now(timezone.utc) + timedelta(hours=3)).strftime("%Y-%m-%dT%H:%MZ")}
        reset({"2026-09-26": slate26})
        ctx, _ = run(scores.fetch_soccer_context(BTTS["teams"], "2026-09-26", bound=future))
        check("pregame binding → PENDING with zero HTTP calls", ctx == "PENDING" and not _Stub.calls,
              f"{ctx!r} {len(_Stub.calls)}")
        check("pregame binding → math skipped with zero HTTP calls",
              run(scores.try_soccer_grade_math(BTTS, "2026-09-26", future)) is None and not _Stub.calls)
        ctx, _ = run(scores.fetch_soccer_context(BTTS["teams"], "2026-09-26",
                                                 bound={**rec, "status": "ambiguous", "id": ""}))
        check("ambiguous binding → no context (UNKNOWN path, never a guess)", ctx == "", repr(ctx))
        reset(all_status=503)
        ctx, _ = run(scores.fetch_soccer_context(BTTS["teams"], "2026-09-26"))
        check("ESPN fully down → PENDING, never UNKNOWN", ctx == "PENDING", repr(ctx))
        reset({"2026-07-11": SLATES["2026-07-11_aet"]})
        ctx, _ = run(scores.fetch_soccer_context(["Argentina"], "2026-07-11"))
        check("AET context carries halves, 90' and ET — never quarter labels",
              "1H=1 2H=0 90'=1 ET1=0 ET2=2" in ctx and "Q1" not in ctx, ctx)
        reset({"2026-09-26": slate26})
        ctx, _ = run(build_context("Soccer", "2026-09-26", BTTS, None, {}, soccer_bound=rec))
        check("build_context plumbs the stored binding", "Spain 3 at England 2" in ctx, ctx)

        print("E. tracker")
        reset({"2026-09-26": slate26})
        audit, se = _Audit(), {}
        r1 = run(tracker._bind_leg(BTTS, "Soccer", "2026-09-26", se, 0, audit, "James Bets", "k:1"))
        check("binds once and stores id/kickoff/teams", r1 and r1["id"] == eng_spa["id"]
              and se["0"]["teams"] == BTTS["teams"] and r1["kickoff"], str(r1))
        n = len(_Stub.calls)
        run(tracker._bind_leg(BTTS, "Soccer", "2026-09-26", se, 0, audit, "James Bets", "k:1"))
        check("second pass reuses the binding (no fetch)", len(_Stub.calls) == n)
        reset({"2026-09-27": SLATES["2026-09-27_liverpool"]})
        audit, se = _Audit(), {}
        lfc = {"description": "Liverpool ML", "bet_type": "moneyline", "teams": ["Liverpool"]}
        run(tracker._bind_leg(lfc, "Soccer", "2026-09-27", se, 0, audit, "X", "k:2"))
        run(tracker._bind_leg(lfc, "Soccer", "2026-09-27", se, 0, audit, "X", "k:2"))
        check("ambiguous → stored + exactly one audit flag",
              se["0"]["status"] == "ambiguous" and len(audit.sent) == 1, str(audit.sent))
        reset({"2026-09-26": slate26})
        audit = _Audit()
        early = {**rec, "kickoff": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%MZ")}
        run(tracker._stuck_tripwire(early, BTTS, "Soccer", audit, "J", "k:1", 0))
        check("tripwire quiet before kickoff + 2h45m", not audit.sent)
        stuck = dict(rec)  # kickoff 2026-09-26T18:45Z, final
        run(tracker._stuck_tripwire(stuck, BTTS, "Soccer", audit, "J", "k:1", 0))
        run(tracker._stuck_tripwire(stuck, BTTS, "Soccer", audit, "J", "k:1", 0))
        check("tripwire fires once for a final game with an ungraded pick",
              len(audit.sent) == 1 and stuck.get("stuck_warned"), str(audit.sent))
        audit = _Audit()
        reset({"2026-09-26": [ne]})
        run(tracker._stuck_tripwire(
            scores.binding_record(ne, "2026-09-26", "bound", ["New England Revolution"]),
            {"teams": ["New England Revolution"], "description": "NE ML"}, "Soccer", audit, "J", "k:3", 0))
        check("tripwire quiet while the game isn't final", not audit.sent)

        print("F. cache")
        existing = {"espn_events": {"0": rec}, "soccer_events": {"0": rec}, "parsed": {}}
        entry = tracker_cache._pending_entry("J", {}, {}, existing)
        check("_pending_entry keeps espn_events (+ the legacy soccer_events)",
              entry.get("espn_events") == {"0": rec} and entry.get("soccer_events") == {"0": rec})
        check("tracker reads the legacy key", tracker._leg_bindings({"soccer_events": {"0": rec}}) == {"0": rec})
        mem = {"k": {"leg_verdicts": {}}}
        disk = {"k": {"espn_events": {"0": {**rec, "stuck_warned": True}}}}
        tracker_cache._merge_leg_bindings(mem, disk)
        check("save merge restores a binding our stale copy lacked",
              mem["k"].get("espn_events", {}).get("0", {}).get("id") == eng_spa["id"])
        mem = {"k": {"espn_events": {"0": dict(rec)}}}
        tracker_cache._merge_leg_bindings(mem, disk)
        check("save merge ORs the one-shot stuck flag", mem["k"]["espn_events"]["0"].get("stuck_warned"))
    finally:
        httpx.AsyncClient = real
        scores.clear_soccer_cache()

    print()
    if failures:
        print(f"FAILED: {len(failures)}")
        return 1
    print("All soccer pipeline checks passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
