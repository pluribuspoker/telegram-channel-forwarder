#!/usr/bin/env python3
"""Offline tests for the pick review (pick_reviewer.py + scripts/pick_review.py).

Which questions get asked (each parse/verdict/no-parse once per signature,
fan-out copies as one, a changed parse asked again, a failing review dropped
after FAIL_CAP), the nightly batching, flag → pending → attempt cap, the
reviewer's answer parser, the agent result contract, the every-outcome card,
the subscription-only agent env. No network, no claude binary, no DMs.

    python scripts/test_pick_review.py
"""
import json
import sys
from datetime import date, datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scripts import pick_review as prv  # noqa: E402
import pick_reviewer as pr  # noqa: E402

failures = []


def check(label: str, ok: bool, detail: str = ""):
    print(f"{'PASS' if ok else 'FAIL'}  {label}" + (f"  ({detail})" if detail and not ok else ""))
    if not ok:
        failures.append(label)


NOW = datetime(2026, 10, 15, 22, 0, tzinfo=timezone.utc)
TODAY = date(2026, 10, 15)


def entry(desc="Rams -3.5", *, msg_date="2026-10-15", src=None, verdict=None, calc=None,
          html_text="Trent\n\nRAMS -3.5 ⁠-102"):
    e = {"capper_name": "Trent", "msg_date": msg_date, "html_text": html_text,
         "parsed": {"sport": "NFL", "picks": [{"description": desc, "sport": "NFL",
                                               "bet_type": "spread", "line": -3.5,
                                               "teams": ["Los Angeles Rams"]}]},
         "odds_by_pick": {"0": {"odds": -102, "match_type": "exact",
                                "bookmaker": "espn_draftkings"}},
         "espn_events": {"0": {"id": "1", "date": msg_date, "kickoff": "2026-10-13T00:15Z",
                               "name": "Bills at Rams", "status": "bound"}}}
    if src:
        e["_source_key"] = src
    if verdict:
        e["leg_verdicts"] = {"0": {"verdict": verdict, "calc": calc or "x"}}
    return e


# ─── questions ────────────────────────────────────────────────────────────────

cache = {"-100A:1": entry(src="S:1"), "-100B:7": entry(src="S:1"),
         "-100A:2": entry("Bills ML", msg_date="2026-10-10"),
         "-100A:3": {"_failed": True, "text_hash": "x"},
         "-100A:4": entry("Jets +3", msg_date="2026-10-14", verdict="WIN", calc="Jets 20-17")}
state: dict = {}
qs = prv.parse_questions(cache, state, TODAY)
srcs = sorted(q["source"] for q in qs)
check("parse: fan-out copies are ONE question", srcs == ["-100A:4", "S:1"], srcs)
check("parse: both copies ride along", sorted(next(q for q in qs if q["source"] == "S:1")["keys"])
      == ["-100A:1", "-100B:7"])
check("parse: old message / failed marker not asked",
      not any(q["source"] in ("-100A:2", "-100A:3") for q in qs))
for q in qs:
    prv.mark_asked(state, q, NOW, 0)
check("parse: an asked question is not asked again", prv.parse_questions(cache, state, TODAY) == [])
cache["-100A:1"]["parsed"]["picks"][0]["line"] = -2.5
cache["-100B:7"]["parsed"]["picks"][0]["line"] = -2.5
check("parse: a changed parse is asked again (an agent's fix gets re-reviewed)",
      [q["source"] for q in prv.parse_questions(cache, state, TODAY)] == ["S:1"])

gq = prv.grade_questions(cache, {}, TODAY)
check("grade: only resolved legs", [(q["source"], q["legs"]) for q in gq] == [("-100A:4", [0])], gq)
st2: dict = {}
prv.mark_asked(st2, gq[0], NOW, 0)
check("grade: per-leg signature recorded", "grade:-100A:4:0" in st2["reviewed"])
check("grade: asked once", prv.grade_questions(cache, st2, TODAY) == [])
cache["-100A:4"]["leg_verdicts"]["0"]["verdict"] = "LOSS"
check("grade: a changed verdict is asked again", len(prv.grade_questions(cache, st2, TODAY)) == 1)

st3: dict = {}
cur = prv.grade_questions(cache, st3, TODAY)[0]
for _ in range(prv.FAIL_CAP - 1):
    prv.mark_failed(st3, cur)
check("grade: a failed review is retried", len(prv.grade_questions(cache, st3, TODAY)) == 1)
prv.mark_failed(st3, cur)
check("grade: a review failing FAIL_CAP times is dropped", prv.grade_questions(cache, st3, TODAY) == [])

rows = [(-100, 5, "2026-10-13", "Kings", "Kings NBA Pre Season"),
        (-200, 9, "2026-10-13", "Kings", "Kings NBA Pre Season"),
        (-100, 6, "2026-10-13", "K", "Nothing today")]
nq = prv.noparse_questions(rows, {})
check("noparse: identical fan-out texts are one question",
      sorted(len(q["keys"]) for q in nq) == [1, 2], [q["keys"] for q in nq])

many = {f"-100A:{i}": entry(f"Team{i} ML", msg_date="2026-10-14") for i in range(12)}
many["-100A:99"] = entry("Today ML", msg_date="2026-10-15")
nights = prv.nightly_questions(many, date(2026, 10, 14))
check("nightly: yesterday only, batched", [len(q["batch"]) for q in nights] == [5, 5, 2],
      [len(q["batch"]) for q in nights])

# ─── flags → pending ──────────────────────────────────────────────────────────

st4: dict = {}
q = {"stage": "parse", "qid": "parse:S:1", "sig": "s", "source": "S:1",
     "keys": ["-100A:1", "-100B:7"], "capper": "Trent", "legs": []}
items = prv.flags_to_pending(q, [{"leg": 0, "problem": "line is -2.5 not -3.5"}], NOW)
added, capped = prv.queue(st4, items)
check("queue: a flag becomes one pending message", len(st4["pending"]) == 1 and not capped)
gq2 = dict(q, stage="grade", qid="grade:S:1")
prv.queue(st4, prv.flags_to_pending(gq2, [{"leg": 0, "problem": "wrong verdict"}], NOW))
check("queue: a second flag on a pending message merges",
      len(st4["pending"]) == 1 and len(st4["pending"][0]["flags"]) == 2)
st4["attempts"] = {"parse:S:1": prv.ATTEMPT_CAP}
st4["pending"] = []
_, capped = prv.queue(st4, items)
check("queue: past the attempt cap → raw DM, no agent", len(capped) == 1 and not st4["pending"])

nb = {"stage": "nightly", "qid": "nightly:2026-10-14:0", "sig": "2026-10-14",
      "batch": [{"source": "S:1", "keys": ["-100A:1"], "capper": "Trent"},
                {"source": "S:2", "keys": ["-100A:2"], "capper": "TMS"}]}
nit = prv.flags_to_pending(nb, [{"message": "-100A:2", "leg": 0, "problem": "never graded"},
                                {"message": "bogus", "leg": 0, "problem": "x"}], NOW)
check("nightly: flags fan out to their message, unknown keys dropped",
      [i["source"] for i in nit] == ["S:2"])

# ─── reviewer answer + payload ────────────────────────────────────────────────

check("parse_flags: clean", pr.parse_flags('{"flags": []}', "parse") == [])
check("parse_flags: prose around JSON",
      pr.parse_flags('Sure.\n{"flags": [{"leg": 0, "problem": "wrong team"}]}', "parse")
      == [{"leg": 0, "problem": "wrong team"}])
check("parse_flags: garbage → None (retry)", pr.parse_flags("no idea", "parse") is None)
check("parse_flags: nightly needs a message key",
      pr.parse_flags('{"flags": [{"leg": 0, "problem": "x"}]}', "nightly") == [])
txt = pr.display_text({"html_text": "TMS<br>Twins -1 ⁠-133✅<blockquote>4-1 off 2</blockquote>"})
check("display_text: our price tag stripped, quote marked",
      "-133" not in txt and "[quote] 4-1 off 2 [/quote]" in txt, txt)
pl = json.loads(pr.parse_payload("-100A:1", cache["-100A:1"]))
check("payload: carries bound game + price", pl["picks"][0]["bound_game"] == "Bills at Rams"
      and pl["picks"][0]["price"] == -102)

# ─── agent contract + card ────────────────────────────────────────────────────

it = st4.get("pending") or items
res = ('done\nPICK_REVIEW_RESULT: [{"message": "S:1", "outcome": "false_alarm", '
       '"issue": "the slip says -3.5", "action": "nothing"}]')
rep = prv.parse_results(res, items)
check("result contract parsed", rep["S:1"]["outcome"] == "false_alarm")
check("missing contract → unparsed", prv.parse_results("oops", items)["S:1"]["outcome"] == "unparsed")
card = prv.dm_card(items, rep, commits=[], notes=[], meta={"wall_ms": 120000})
check("card: a false alarm is still reported (operator asked for every outcome)",
      "false alarm" in card and "t.me/c/A/1" in card, card)
prompt = prv.build_prompt(items, cache, now_et="now", head="abc")
check("prompt: full authority, but not the operator's chat session",
      "NO restrictions" in prompt and "claude-channels" in prompt and "PICK_REVIEW_RESULT" in prompt)

inv = prv.ReviewInvoker("claude", oauth_token="tok")
env = inv.environment()
cmd = inv.command("x")
check("agent: subscription only (no ANTHROPIC_API_KEY), strict MCP, our model",
      "ANTHROPIC_API_KEY" not in env and env["CLAUDE_CODE_OAUTH_TOKEN"] == "tok"
      and "--strict-mcp-config" in cmd and cmd[cmd.index("--model") + 1] == prv.MODEL)

print(f"\n{len(failures)} failure(s)" if failures else "\nall passed")
sys.exit(1 if failures else 0)
