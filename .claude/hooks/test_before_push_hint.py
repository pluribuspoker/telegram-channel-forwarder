#!/usr/bin/env python3
"""PreToolUse hook (repo-level, every clone): run the tests before `git push`.

Operator rule (2026-10-07): every Claude session runs the offline suite before
pushing, so a test broken by a change is fixed on the spot — four had rotted
for 1 day to 6 months because nothing ran them. The suite is
`python3 scripts/run_tests.py` (~3.5 min); each run lands in
logs/test_runs.jsonl.

On a `git push` this looks for a passing run of HEAD in that ledger (a run of
an earlier commit still counts when everything since touched only docs /
*.md). None → an FYI naming the command; a failing run of HEAD → the failing
tests. Advisory only — the operator rejected blocking hooks (2026-09-28), and
a push of an urgent fix must stay possible; say so when you push without a
green run. Any error → silent. Runs under python3 or python (Windows).
Test: scripts/test_test_before_push_hint.py.
"""
import json
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
LEDGER = ROOT / "logs" / "test_runs.jsonl"
PUSH = re.compile(r"(?:^|[;&|(]\s*|\s)git\s+(?:-C\s+\S+\s+)?push\b")
DOC_ONLY = re.compile(r"(?:^docs/|\.md$)")


def git(*args: str) -> str:
    return subprocess.run(["git", "-C", str(ROOT), *args], capture_output=True,
                          text=True, timeout=10, check=True).stdout


def runs() -> list[dict]:
    try:
        return [json.loads(l) for l in LEDGER.read_text(encoding="utf-8").splitlines() if l.strip()]
    except (OSError, ValueError):
        return []


def covered(head: str, run: dict) -> bool:
    """A full green run of `head`, or of an ancestor with only docs since."""
    if not run.get("ok") or run.get("n", 0) < 10 or run.get("dirty"):
        return False
    if run.get("head") == head:
        return True
    try:
        git("merge-base", "--is-ancestor", run["head"], head)
        changed = [l for l in git("diff", "--name-only", run["head"], head).splitlines() if l]
    except (subprocess.SubprocessError, KeyError, OSError):
        return False
    return all(DOC_ONLY.search(f) for f in changed)


def note(cmd: str, ledger: list[dict], head: str) -> str | None:
    if not PUSH.search(cmd):
        return None
    if any(covered(head, r) for r in ledger[-20:]):
        return None
    mine = [r for r in ledger if r.get("head") == head]
    if mine and not mine[-1].get("ok"):
        return (f"FYI (tests): the last test run of HEAD {head[:7]} FAILED: "
                f"{', '.join(mine[-1].get('failed') or [])}. Fix them (or the test, if the "
                "change was intended) before pushing — operator rule 2026-10-07. If this push "
                "must go out anyway, say so to the operator.")
    return (f"FYI (tests): no passing `python3 scripts/run_tests.py` run of HEAD {head[:7]} — "
            "run it before pushing (~3.5 min, offline, in a scratch clone; operator rule "
            "2026-10-07) and fix anything it breaks on the spot. If this push must go out "
            "anyway, say so to the operator.")


def main() -> None:
    try:
        inp = json.load(sys.stdin)
        if inp.get("tool_name") != "Bash":
            return
        cmd = str((inp.get("tool_input") or {}).get("command") or "")
        if not PUSH.search(cmd):
            return
        msg = note(cmd, runs(), git("rev-parse", "HEAD").strip())
        if msg:
            print(json.dumps({"hookSpecificOutput": {
                "hookEventName": "PreToolUse", "additionalContext": msg}}))
    except Exception:
        pass


if __name__ == "__main__":
    main()
