#!/usr/bin/env python3
"""Offline tests for the Trent auto-repair runner (scripts/trent_repair.py).

Covers the pure decision logic only — gates/cooldown/cap/park, verify
classification, the agent result contract, prompt content, the invoker's
model/effort swap, and state round-tripping. No network, no claude binary,
no DMs (nothing here touches send paths).

    python scripts/test_trent_repair.py
"""
import json
import os
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scripts import trent_repair as tr  # noqa: E402

failures = []


def check(label: str, ok: bool, detail: str = ""):
    print(f"{'PASS' if ok else 'FAIL'}  {label}" + (f"  ({detail})" if detail and not ok else ""))
    if not ok:
        failures.append(label)


NOW = datetime(2026, 9, 25, 3, 0, tzinfo=timezone.utc)

# 1. Gates.
os.environ.pop("TRENT_REPAIR_DISABLED", None)
check("empty state runs", tr.gate({}, NOW) == ("run", "ok"))

os.environ["TRENT_REPAIR_DISABLED"] = "1"
check("kill switch skips", tr.gate({}, NOW)[0] == "skip")
os.environ.pop("TRENT_REPAIR_DISABLED", None)

recent = {"last_spawn_at": (NOW - timedelta(hours=1)).isoformat(timespec="seconds")}
check("cooldown skips", tr.gate(recent, NOW)[0] == "skip")
check("--force bypasses cooldown", tr.gate(recent, NOW, force=True)[0] == "run")
old = {"last_spawn_at": (NOW - timedelta(hours=7)).isoformat(timespec="seconds")}
check("expired cooldown runs", tr.gate(old, NOW)[0] == "run")
check("garbage timestamp runs (fail-open)",
      tr.gate({"last_spawn_at": "not-a-date"}, NOW)[0] == "run")

parked = {"parked": True, "parked_reason": "attempt cap (2)"}
check("fresh park asks for the capped DM", tr.gate(parked, NOW)[0] == "capped")
check("park with DM sent skips silently",
      tr.gate({**parked, "capped_dm_sent": True}, NOW)[0] == "skip")
check("--force does not bypass the park",
      tr.gate({**parked, "capped_dm_sent": True}, NOW, force=True)[0] == "skip")

# 2. Spawn accounting + settle.
st: dict = {}
tr.record_spawn(st, NOW)
check("spawn bumps attempts", st["attempts"] == 1 and st["last_spawn_at"])
tr.settle(st, "fixed_needs_verify")
check("non-terminal outcome under cap does not park", not st.get("parked"))
tr.record_spawn(st, NOW)
tr.settle(st, "error")
check("attempt cap parks", st["parked"] and "attempt cap" in st["parked_reason"])
tr.rearm(st)
check("rearm resets", st["attempts"] == 0 and not st["parked"])
tr.record_spawn(st, NOW)
tr.settle(st, "needs_human")
check("needs_human parks immediately", st["parked"] and st["parked_reason"] == "needs_human")
tr.rearm(st)
tr.record_spawn(st, NOW)
tr.settle(st, "fixed_verified")
check("verified success resets the streak",
      st["attempts"] == 0 and not st["parked"])

# 3. Verify classification.
check("exit 0 clean = pass", tr.classify_verify(0, "Done: 0 picks sent") == "pass")
check("rate-limited exit 0 = inconclusive",
      tr.classify_verify(0, "  Rate-limited by Twitter, skipping this run") == "inconclusive")
check("nonzero = fail", tr.classify_verify(1, "FATAL [bootstrap]: ...") == "fail")
check("timeout sentinel = fail", tr.classify_verify(124, "") == "fail")

# 4. Result contract.
good = 'blah blah\nTRENT_REPAIR_RESULT: {"outcome": "fixed_verified", "issue": "X moved sign.o", "action": "extended scan"}'
r = tr.parse_repair_result(good)
check("valid contract parses", r["outcome"] == "fixed_verified" and r["issue"] == "X moved sign.o")
two = good + '\nTRENT_REPAIR_RESULT: {"outcome": "needs_human", "issue": "second", "action": ""}'
check("last contract line wins", tr.parse_repair_result(two)["outcome"] == "needs_human")
check("unknown outcome degrades to unparsed",
      tr.parse_repair_result('TRENT_REPAIR_RESULT: {"outcome": "nope"}')["outcome"] == "unparsed")
check("missing contract degrades to unparsed, tail kept",
      tr.parse_repair_result("agent rambled")["outcome"] == "unparsed")
check("empty result degrades", tr.parse_repair_result("")["outcome"] == "unparsed")

# 5. Prompt content.
p = tr.build_prompt(now_et="2026-09-24 22:30 EDT",
                    fatal_line="FATAL [bootstrap]: XClIdBootstrapError: ...",
                    log_tail="line1\nline2", journal_tail="j1", head="abc123def456")
check("prompt is an /investigate invocation", p.startswith("/investigate TRENT AUTO-REPAIR"))
for needle in ("FATAL [bootstrap]", "NEVER `git push`", "trent-repair:",
               "TRENT_REPAIR_RESULT", "docs/trent.md", "diagnose_failure",
               "set_env_local.py", "abc123def456"):
    check(f"prompt carries {needle!r}", needle in p)
check("prompt forbids service restarts", "telegram-forwarder OR trent-monitor" in p)

# 6. Invoker command: model/effort swapped, audit flags preserved.
inv = tr.RepairInvoker("/bin/claude", oauth_token="tok", timeout=5)
cmd = inv.command("hi")
check("model swapped", cmd[cmd.index("--model") + 1] == tr.REPAIR_MODEL)
check("effort swapped", cmd[cmd.index("--effort") + 1] == tr.REPAIR_EFFORT)
for flag in ("--strict-mcp-config", "--no-session-persistence",
             "--dangerously-skip-permissions", "--output-format"):
    check(f"audit flag {flag} preserved", flag in cmd)
check("env stays from-scratch with hook standdown",
      inv.environment().get("NIGHTLY_AUDIT") == "1"
      and "ANTHROPIC_API_KEY" not in inv.environment())

# 7. DM card shape.
card = tr.dm_card("fixed_verified", {"issue": "a<b", "action": "c&d"},
                  commits=["abc123 trent-repair: fix"], verify="pre=fail, post=pass",
                  meta={"wall_ms": 61000, "num_turns": 42})
check("card headline + expandable blockquote",
      card.startswith("🛠 <b>Trent auto-repair</b>") and "<blockquote expandable>" in card)
check("card escapes agent text", "a&lt;b" in card and "c&amp;d" in card)
check("card carries verify + commit", "post=pass" in card and "abc123" in card)

# 8. State round-trip preserves unknown keys (forward compat).
with tempfile.TemporaryDirectory() as td:
    tr.STATE_FILE = Path(td) / "state.json"
    tr.save_state({"attempts": 1, "future_key": {"x": 1}})
    loaded = tr.load_state()
    check("state round-trip keeps unknown keys",
          loaded.get("future_key") == {"x": 1} and loaded.get("attempts") == 1)
    tr.STATE_FILE.write_text("{corrupt", encoding="utf-8")
    check("corrupt state loads as empty", tr.load_state() == {})

print()
if failures:
    print(f"{len(failures)} FAILED: {failures}")
    sys.exit(1)
print("all checks passed")
