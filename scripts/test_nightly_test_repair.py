#!/usr/bin/env python3
"""Offline tests for the nightly test auto-repair (scripts/nightly_test_repair.py).

No claude binary, no git writes, no DMs:

    python3 scripts/test_nightly_test_repair.py

Pins: which run it repairs (the newest NIGHTLY --all run), the per-test
attempt cap → parked (still reported), re-arm when a test passes, the result
contract, the prompt's rules (fix the right side, never delete/skip, never
push), the one-DM result card, and the subscription-only invoker.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scripts import nightly_test_repair as tr  # noqa: E402

fails = 0


def check(label, ok, detail=""):
    global fails
    print(("PASS " if ok else "FAIL ") + label + (f"  ({detail})" if detail and not ok else ""))
    fails += not ok


runs = [
    {"trigger": "nightly", "mode": "all", "head": "aaa", "failed": [], "secs": {"test_a": 1, "test_b": 1}},
    {"trigger": "prepush", "mode": "changed", "head": "bbb", "failed": ["test_a"], "secs": {"test_a": 1}},
    {"trigger": "nightly", "mode": "all", "head": "ccc", "failed": ["test_a"],
     "secs": {"test_a": 1, "test_b": 1}, "tails": {"test_a": "AssertionError: boom"}},
    {"trigger": "manual", "mode": "area", "head": "ddd", "failed": [], "secs": {"test_b": 1}},
]
check("repairs the newest NIGHTLY full run (not prepush/area runs)", tr.last_nightly(runs)["head"] == "ccc")
check("last green head of a test = newest run where it ran and passed",
      tr.last_green_head(runs, "test_a") == "aaa" and tr.last_green_head(runs, "test_b") == "ddd")
check("never green → None", tr.last_green_head(runs, "test_zzz") is None)

state = {}
todo, parked = tr.gate(state, ["test_a"])
check("first failure goes to the agent", todo == ["test_a"] and parked == [])
tr.settle(state, "test_a", "verify_failed", "t1")
tr.settle(state, "test_a", "error", "t2")
todo, parked = tr.gate(state, ["test_a"])
check(f"{tr.ATTEMPT_CAP} failed repairs → parked (and still reported)", todo == [] and parked == ["test_a"])
s2 = {}
tr.settle(s2, "test_x", "needs_human", "t")
check("needs_human parks at once", s2["test_x"]["parked"])
s3 = {}
tr.settle(s3, "test_y", "fixed_test", "t")
check("a fix doesn't park", not s3["test_y"].get("parked"))
tr.forget_passing(state, {"failed": [], "secs": {"test_a": 1}})
check("a test that passes a night is re-armed", "test_a" not in state)
st = {"test_q": {"parked": True}, "_meta": 1}
tr.forget_passing(st, {"failed": ["test_q"], "secs": {"test_q": 1}})
check("…but a still-failing one stays parked", st["test_q"]["parked"] and "_meta" in st)

text = ('done\nTEST_REPAIR_RESULT: [{"test": "test_a", "outcome": "fixed_test", '
        '"cause": "renderer format changed in abc", "action": "updated expectation (def)"}]')
res = tr.parse_results(text, ["test_a", "test_b"])
check("result contract parses per test", res["test_a"]["outcome"] == "fixed_test"
      and "renderer" in res["test_a"]["cause"])
check("an unreported test is 'unreported'", res["test_b"]["outcome"] == "unreported")
check("garbage → all unreported", tr.parse_results("no contract", ["test_a"])["test_a"]["outcome"] == "unreported")

p = tr.build_prompt(runs[2], ["test_a"], {"test_a": ["111 feat: x", "222 fix: y"]},
                    now_et="2026-10-08 02:45 EDT", head="ccc")
check("prompt carries the tail and the commits since green",
      "AssertionError: boom" in p and "111 feat: x" in p)
check("prompt rules: fix the right side, never delete/skip, never push, sandboxed runs, contract",
      all(s in p for s in ("CODE REGRESSION", "STALE TEST", "NEVER delete a test", "NEVER `git push`",
                           "scripts/run_tests.py --only", "TEST_REPAIR_RESULT", tr.PREFIX)))
check("passed before at this same commit → the prompt says so (flaky/date)",
      "same commit" in tr.build_prompt(runs[2], ["test_a"], {"test_a": []}, now_et="x", head="y"))
check("never passed → unknown", "never passed" in tr.build_prompt(runs[2], ["test_a"], {},
                                                                    now_et="x", head="y"))
check("prompt keeps MOE out of scope", "MOE" in p and "needs_human" in p)

card = tr.dm_text({"test_a": {"outcome": "fixed_test", "cause": "c", "action": "a"},
                   "test_b": {"outcome": "needs_human", "cause": "why", "action": "decide"}},
                  ["test_p"], commits=["abc1234 test-repair: x"], verified="ok", pushed=True,
                  meta={"wall_ms": 61000})
check("one DM: per-test result, parked list, commits pushed",
      "✅" in card and "🙋" in card and "test_p" in card and "pushed" in card and "61s" in card, card)

inv = tr.RepairInvoker("claude", oauth_token="t")
cmd, env = inv.command("x"), inv.environment()
check("invoker: repair model/effort, strict MCP, subscription OAuth only",
      cmd[cmd.index("--model") + 1] == tr.MODEL and "--strict-mcp-config" in cmd
      and env.get("CLAUDE_CODE_OAUTH_TOKEN") == "t" and "ANTHROPIC_API_KEY" not in env)

print(f"\n{'OK' if not fails else f'{fails} FAILED'}")
sys.exit(1 if fails else 0)
