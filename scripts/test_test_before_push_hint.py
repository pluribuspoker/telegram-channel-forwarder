#!/usr/bin/env python3
"""Regression test: the pre-push test hint (.claude/hooks/test_before_push_hint.py).

Offline, a throwaway git repo for the ancestry cases:

    python3 scripts/test_test_before_push_hint.py

Pins: which commands count as a push, that a green full run of HEAD (or of an
ancestor with only docs since) silences it, that a code change since, a dirty
or partial run, or a failing run of HEAD does not — and that it never blocks.
"""
import json
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / ".claude" / "hooks"))

import test_before_push_hint as h  # noqa: E402

fails = 0


def check(label, ok, detail=""):
    global fails
    print(("PASS " if ok else "FAIL ") + label + (f"  ({detail})" if detail and not ok else ""))
    fails += not ok


for cmd, want in [("git push", True), ("cd ~/app && git push -q", True),
                  ("git -C /home/forwarder/app push origin main", True),
                  ("git commit -m 'x' && git push", True),
                  ("git pull", False), ("git status", False), ("echo 'pushes'", False),
                  ("grep -n 'git push' CLAUDE.md", False)]:
    check(f"push detection: {cmd!r}", bool(h.PUSH.search(cmd)) == want)

tmp = Path(tempfile.mkdtemp())
def g(*a):
    return subprocess.run(["git", "-C", str(tmp), *a], check=True, capture_output=True,
                          text=True).stdout.strip()
g("init", "-q"); g("config", "user.email", "t@t"); g("config", "user.name", "t")
(tmp / "a.py").write_text("x=1\n"); g("add", "."); g("commit", "-qm", "code")
c1 = g("rev-parse", "HEAD")
(tmp / "docs").mkdir(); (tmp / "docs" / "n.md").write_text("doc\n"); g("add", "."); g("commit", "-qm", "doc")
c2 = g("rev-parse", "HEAD")
(tmp / "a.py").write_text("x=2\n"); g("add", "."); g("commit", "-qm", "code2")
c3 = g("rev-parse", "HEAD")
h.ROOT = tmp

def run(head, ok=True, n=90, dirty=False, failed=()):
    return {"head": head, "ok": ok, "n": n, "dirty": dirty, "failed": list(failed)}

check("green run of HEAD → silent", h.note("git push", [run(c1)], c1) is None)
check("green run, only docs since → silent", h.note("git push", [run(c1)], c2) is None)
check("green run, code since → FYI", "no passing" in (h.note("git push", [run(c1)], c3) or ""))
check("no runs → FYI", "no passing" in (h.note("git push", [], c1) or ""))
check("failing run of HEAD → names the tests",
      "test_x" in (h.note("git push", [run(c1, ok=False, failed=["test_x"])], c1) or ""))
check("a partial (--only) run doesn't count", h.note("git push", [run(c1, n=2)], c1) is not None)
check("a green changed-files run of HEAD counts, even with 0 tests (docs/config only)",
      h.note("git push", [{**run(c1, n=0), "mode": "changed"}], c1) is None)
check("an --area run doesn't count", h.note("git push", [{**run(c1), "mode": "area"}], c1) is not None)
check("a run of a dirty tree doesn't count", h.note("git push", [run(c1, dirty=True)], c1) is not None)
check("not a push → silent", h.note("git status", [], c1) is None)

p = subprocess.run([sys.executable, str(ROOT / ".claude" / "hooks" / "test_before_push_hint.py")],
                   input=json.dumps({"tool_name": "Bash", "tool_input": {"command": "git push"}}),
                   capture_output=True, text=True)
out = json.loads(p.stdout) if p.stdout.strip() else {}
check("hook process: exit 0, advisory additionalContext only (never a block)",
      p.returncode == 0 and "decision" not in out and "permissionDecision" not in json.dumps(out),
      p.stdout)

shutil.rmtree(tmp, ignore_errors=True)
print(f"\n{'OK' if not fails else f'{fails} FAILED'}")
sys.exit(1 if fails else 0)
