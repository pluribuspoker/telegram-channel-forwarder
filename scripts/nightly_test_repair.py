#!/usr/bin/env python3
"""Nightly test auto-repair — one headless agent fixes what the sweep found red.

Operator ask (2026-10-07): "for any failures have an agent repair it and just
tell me the result". run_test_sweep.sh runs this after `run_tests.py
--trigger nightly` exits 1. Modeled on scripts/trent_repair.py (the audit's
HeadlessInvoker: OAuth-only subscription billing, NIGHTLY_AUDIT=1 hook
standdown, --strict-mcp-config, stream-json transcript, killpg on timeout).

Flow: the newest nightly ledger run (logs/test_runs.jsonl) → its failing
tests (+ output tails, + the commits since each last passed) → gates → ONE
`claude -p "/investigate …"` agent (TEST_REPAIR_MODEL, Opus 5.5 high) that
decides per test: the CODE regressed (fix the code) or an intended change made
the TEST stale (update the test) — never deleting/skipping a test to go green
→ commits `test-repair:` → the RUNNER verifies itself (`run_tests.py --only
<failed>` + the default changed-files run, both sandboxed) → green: push once,
restart grade-daemon on a non-test .py change (never telegram-forwarder);
red: the agent's commits are undone (`git reset --keep` when they are the only
new commits, else `git revert`) and nothing is pushed → ONE watchdog DM with
the result per test.

Guards (logs/test_repair_state.json, per test): kill switch
TEST_REPAIR_DISABLED=1 (failures then DM plainly); flock; TEST_REPAIR_ATTEMPT_CAP
(2) nights per test → parked (still listed in the DM as parked, so a red test
never goes silent); a test that passes a night is forgotten (re-armed).
`--dry-run` prints the prompt; `--rearm [TEST]`; `--no-push`.
Ledger logs/test_repair_runs.jsonl, transcripts logs/test_repair/<stamp>/.
Test: scripts/test_nightly_test_repair.py. (Not named test_*.py: run_tests.py would run it as a test.)
"""

import argparse
import fcntl
import html
import json
import os
import re
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scripts import ungraded_audit as ua  # noqa: E402  (invoker, git, DM, ledger)

ROOT = ua.ROOT
TEST_LEDGER = ROOT / "logs" / "test_runs.jsonl"
STATE_FILE = ROOT / "logs" / "test_repair_state.json"
LOCK_FILE = ROOT / "logs" / ".test_repair.lock"
RUNS_LOG = ROOT / "logs" / "test_repair_runs.jsonl"
OUT_DIR = ROOT / "logs" / "test_repair"
PYTHON = os.environ.get("TEST_REPAIR_PYTHON") or "/home/forwarder/venv/bin/python"

MODEL = os.environ.get("TEST_REPAIR_MODEL") or "claude-opus-5-5"
EFFORT = os.environ.get("TEST_REPAIR_EFFORT") or "high"
AGENT_TIMEOUT = int(os.environ.get("TEST_REPAIR_AGENT_TIMEOUT") or 1500)
ATTEMPT_CAP = int(os.environ.get("TEST_REPAIR_ATTEMPT_CAP") or 2)
PREFIX = "test-repair:"

OUTCOMES = ("fixed_code", "fixed_test", "flaky", "needs_human")
BADGE = {"fixed_code": "✅", "fixed_test": "✅", "flaky": "👌", "needs_human": "🙋",
         "unreported": "⚠️", "error": "❌", "timeout": "⏱", "parked": "🅿️",
         "verify_failed": "❌"}
LABEL = {"fixed_code": "fixed (code regression)", "fixed_test": "fixed (stale test)",
         "flaky": "flaky — passes now, nothing changed", "needs_human": "needs you",
         "unreported": "agent didn't report", "error": "agent failed",
         "timeout": "agent timed out", "parked": "parked (2 failed repairs)",
         "verify_failed": "fix didn't verify — undone, nothing pushed"}

_RESULT_RE = re.compile(r"TEST_REPAIR_RESULT:\s*(\[.*?\])\s*$", re.MULTILINE | re.DOTALL)


class RepairInvoker(ua.HeadlessInvoker):
    def command(self, prompt: str) -> list[str]:
        cmd = super().command(prompt)
        cmd[cmd.index("--model") + 1] = MODEL
        cmd[cmd.index("--effort") + 1] = EFFORT
        return cmd


# ─── pure logic (tested offline) ─────────────────────────────────────────────

def last_nightly(runs: list[dict]) -> dict | None:
    return next((r for r in reversed(runs) if r.get("trigger") == "nightly"
                 and r.get("mode", "all") == "all"), None)


def last_green_head(runs: list[dict], test: str) -> str | None:
    """The newest commit at which `test` passed (it ran and the run didn't list it)."""
    for r in reversed(runs):
        if test in (r.get("secs") or {}) and test not in (r.get("failed") or []) \
                and not r.get("dirty"):
            return r.get("head")
    return None


def gate(state: dict, failed: list[str]) -> tuple[list[str], list[str]]:
    """(tests to hand the agent, parked tests)."""
    run, parked = [], []
    for t in failed:
        st = state.get(t) or {}
        (parked if st.get("parked") or int(st.get("attempts") or 0) >= ATTEMPT_CAP
         else run).append(t)
    return run, parked


def forget_passing(state: dict, run: dict) -> None:
    """A test that ran green tonight is re-armed (its streak is over)."""
    failed = set(run.get("failed") or [])
    for t in list(state):
        if not t.startswith("_") and t in (run.get("secs") or {}) and t not in failed:
            state.pop(t)


def settle(state: dict, test: str, outcome: str, now: str) -> None:
    st = state.setdefault(test, {})
    st["attempts"] = int(st.get("attempts") or 0) + 1
    st["last_outcome"], st["last_at"] = outcome, now
    if outcome == "needs_human" or st["attempts"] >= ATTEMPT_CAP and outcome not in (
            "fixed_code", "fixed_test", "flaky"):
        st["parked"] = True


def parse_results(text: str, tests: list[str]) -> dict[str, dict]:
    reports: dict[str, dict] = {}
    for raw in reversed(_RESULT_RE.findall(text or "")):
        try:
            items = json.loads(raw)
        except json.JSONDecodeError:
            continue
        if isinstance(items, list):
            for it in items:
                if isinstance(it, dict) and it.get("outcome") in OUTCOMES:
                    reports.setdefault(str(it.get("test") or ""), it)
            break
    out = {}
    for t in tests:
        it = reports.get(t) or reports.get(t.removeprefix("test_"))
        out[t] = ({"outcome": it["outcome"], "cause": str(it.get("cause") or "")[:300],
                   "action": str(it.get("action") or "")[:300]} if it else
                  {"outcome": "unreported", "cause": "", "action": ""})
    return out


def build_prompt(run: dict, tests: list[str], since: dict[str, list[str]], *,
                 now_et: str, head: str) -> str:
    lines = [
        f"/investigate NIGHTLY TEST REPAIR {now_et}: the nightly offline test sweep "
        f"(scripts/run_tests.py --all, at {run.get('head', '')[:7]}) has {len(tests)} "
        "failing test(s). For EACH, find why it fails and fix the RIGHT side.",
        "",
        "## Failing tests",
    ]
    for t in tests:
        lines += [f"### {t}", "- output tail:", "```",
                  ((run.get("tails") or {}).get(t) or "(none captured)").strip(), "```"]
        commits = since.get(t)
        lines.append("- commits since it last passed: " + (
            "; ".join(commits[:15]) if commits else
            "none — it passed at this same commit before (flaky? date/environment?)"
            if commits is not None else
            "unknown (it never passed in the ledger, logs/test_runs.jsonl)"))
        lines.append("")
    lines += [
        f"- repo HEAD at spawn: {head}",
        "",
        "## How to decide",
        "- Reproduce first: `python3 scripts/run_tests.py --only <name>` (sandboxed "
        "scratch clone — use it, never run a test directly against live state).",
        "- CODE REGRESSION (a commit broke behavior the test rightly pins) → fix the "
        "code, keep the test → `fixed_code`. Read the subsystem's docs/*.md and "
        "CLAUDE.md first; respect every invariant there.",
        "- STALE TEST (an intended change — the commit message/docs say so — moved "
        "the expected output) → update the test's expectation, citing that commit "
        "in a comment → `fixed_test`. Examples from 2026-10-07: a renderer format "
        "change, a new refusal ordering, a date fixture aging out of a window.",
        "- Passes on re-run with nothing changed → `flaky`; say what made it flaky "
        "(time/date dependence, network, ordering) and fix that if it is cheap.",
        "- NEVER delete a test, skip/xfail it, or loosen an assertion just to go "
        "green. Anything needing a product decision, money, credentials, or a "
        "behavior you can't tell is intended → `needs_human`, change nothing for it.",
        "- NFL MOE / God Expert code is out of scope (excluded from CI): if a "
        "failure leads there, `needs_human`.",
        "",
        "## Constraints — these OVERRIDE the standard /investigate workflow where they conflict",
        "- Headless agent on the VPS (as forwarder, in /home/forwarder/app); no human "
        "available. Work directly in this repo — NO git worktree, NO SSH.",
        "- NEVER `git push`, never restart/stop telegram-forwarder or claude-channels. "
        "The runner verifies with its own sandboxed test run, pushes if green "
        "(undoes your commits if not), and restarts grade-daemon on a code change.",
        "- Before committing: `python3 scripts/run_tests.py --only <the failing "
        "tests>` AND `python3 scripts/run_tests.py` (every test your change touches) "
        "must pass. Stage ONLY files you changed (never `git add -A`; the tree holds "
        f"unrelated WIP), one commit per fix, message prefixed `{PREFIX}`.",
        "- No new Claude API (ANTHROPIC_API_KEY) calls in any code; free sources only. "
        "Do not message the operator — the runner sends the DM.",
        f"- Budget ~{AGENT_TIMEOUT // 60 - 5} min; killed at {AGENT_TIMEOUT // 60}. "
        "Commit each fix as soon as its tests pass (uncommitted edits are reverted "
        "on a timeout). Add an /investigate lesson only for a novel technique.",
        "",
        "## Result contract (the runner parses this)",
        "End your FINAL message with exactly one line listing EVERY failing test:",
        'TEST_REPAIR_RESULT: [{"test": "<test_name>", "outcome": "<fixed_code|'
        'fixed_test|flaky|needs_human>", "cause": "<why it failed — one sentence>", '
        '"action": "<what you changed (commit) / what the operator must decide — '
        'one sentence>"}]',
    ]
    return "\n".join(lines)


def dm_text(results: dict[str, dict], parked: list[str], *, commits: list[str],
            verified: str, pushed: bool | None, meta: dict) -> str:
    esc = html.escape
    lines = ["🧪 <b>Nightly test repair</b>"]
    for t, r in results.items():
        o = r["outcome"]
        lines.append(f"{BADGE.get(o, '❓')} <code>{esc(t)}</code> — {esc(LABEL.get(o, o))}")
        inner = [x for x in (r.get("cause") and f"<b>Cause:</b> {esc(r['cause'])}",
                             r.get("action") and f"<b>Action:</b> {esc(r['action'])}") if x]
        if inner:
            lines.append(f"<blockquote expandable>{chr(10).join(inner)}</blockquote>")
    for t in parked:
        lines.append(f"🅿️ <code>{esc(t)}</code> — still failing, parked after "
                     f"{ATTEMPT_CAP} repair tries (re-arm: scripts/nightly_test_repair.py --rearm {esc(t)})")
    if commits:
        lines.append("<b>Commits:</b> " + esc("; ".join(commits))
                     + (" — pushed" if pushed else " — NOT pushed" if pushed is False else ""))
    if verified:
        lines.append(f"<b>Verify:</b> {esc(verified)}")
    bits = [f"{MODEL}/{EFFORT}"] + ([f"{int(meta['wall_ms'] / 1000)}s"] if meta.get("wall_ms") else [])
    lines.append(esc(" · ".join(bits)))
    return "\n".join(lines)


# ─── I/O ─────────────────────────────────────────────────────────────────────

def load_json(path: Path) -> dict:
    try:
        d = json.loads(path.read_text(encoding="utf-8"))
        return d if isinstance(d, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def save_state(state: dict) -> None:
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = STATE_FILE.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(state, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(tmp, STATE_FILE)


def load_runs() -> list[dict]:
    try:
        return [json.loads(l) for l in TEST_LEDGER.read_text(encoding="utf-8").splitlines()
                if l.strip()]
    except (OSError, json.JSONDecodeError):
        return []


def run_tests(*args: str) -> tuple[int, str]:
    p = subprocess.run([PYTHON, str(ROOT / "scripts" / "run_tests.py"), "--no-record", *args],
                       cwd=ROOT, capture_output=True, text=True, timeout=1800)
    return p.returncode, (p.stdout + p.stderr)


def undo(head0: str, commits: list[str]) -> str:
    """Remove the agent's unverified commits. `reset --keep` only when every
    commit since head0 is ours (never drops another agent's work, keeps
    uncommitted edits); otherwise revert ours one by one."""
    shas = [c.split()[0] for c in commits]
    if all(c.split(" ", 1)[1].startswith(PREFIX) for c in commits):
        r = ua._git("reset", "--keep", head0)
        if r.returncode == 0:
            return f"reset to {head0[:7]}"
    done = []
    for sha in shas:
        if ua._git("log", "-1", "--format=%s", sha).stdout.startswith(PREFIX):
            if ua._git("revert", "--no-edit", sha).returncode == 0:
                done.append(sha)
    return f"reverted {', '.join(done) or 'nothing (revert failed — needs a human)'}"


def main() -> int:
    ap = argparse.ArgumentParser(description="Nightly test auto-repair agent")
    ap.add_argument("--dry-run", action="store_true", help="print the prompt; spawn/write nothing")
    ap.add_argument("--rearm", nargs="?", const="*", help="unpark one test (or all)")
    ap.add_argument("--no-push", action="store_true")
    args = ap.parse_args()

    state = load_json(STATE_FILE)
    if args.rearm:
        for t in ([k for k in state if not k.startswith("_")] if args.rearm == "*" else [args.rearm]):
            state.pop(t, None)
        save_state(state)
        print("re-armed")
        return 0

    runs = load_runs()
    run = last_nightly(runs)
    if not run:
        print("no nightly run in the ledger")
        return 0
    forget_passing(state, run)
    failed = list(run.get("failed") or [])
    if not failed:
        save_state(state)
        print("nightly run is green — nothing to repair")
        return 0
    if os.environ.get("TEST_REPAIR_DISABLED") == "1":
        print("TEST_REPAIR_DISABLED=1 — reporting only")
        if not args.dry_run:
            ua.send_watchdog_dm("🧪 tests FAILING (auto-repair disabled): " + ", ".join(failed))
        return 0

    todo, parked = gate(state, failed)
    since = {}
    for t in todo:
        green = last_green_head(runs, t)
        if green:
            since[t] = ua.git_commits_between(green, run.get("head", ""))
    now = datetime.now(timezone.utc)
    stamp = now.strftime("%Y%m%d-%H%M%S")
    prompt = build_prompt(run, todo, since, now_et=now.astimezone().strftime("%Y-%m-%d %H:%M %Z"),
                          head=ua.git_head()[:12]) if todo else ""
    if args.dry_run:
        print(f"to repair: {todo}; parked: {parked}")
        print(prompt)
        return 0
    if not todo:
        save_state(state)
        ua.send_watchdog_dm(dm_text({}, parked, commits=[], verified="", pushed=None, meta={}),
                            as_html=True)
        return 0

    LOCK_FILE.parent.mkdir(parents=True, exist_ok=True)
    with LOCK_FILE.open("w") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            print("another test-repair run holds the lock; exiting")
            return 0
        record: dict = {"ts": now.isoformat(timespec="seconds"), "tests": todo,
                        "parked": parked, "sweep_head": run.get("head")}
        head0, dirty0 = ua.git_head(), ua.git_dirty_paths()
        invoker = RepairInvoker(os.environ.get("TEST_REPAIR_CLAUDE_BIN") or ua.DEFAULT_CLAUDE_BIN,
                                oauth_token=os.environ.get("CLAUDE_CODE_OAUTH_TOKEN", ""),
                                timeout=AGENT_TIMEOUT)
        try:
            text = invoker(prompt, OUT_DIR / stamp / "agent.jsonl")
            (OUT_DIR / stamp / "result.md").write_text(text, encoding="utf-8")
            results = parse_results(text, todo)
        except ua.AgentCallError as exc:
            kind = "timeout" if "timed out" in str(exc) else "error"
            results = {t: {"outcome": kind, "cause": str(exc)[:300], "action": ""} for t in todo}
        record.update(invoker.last_call)

        leftover = ua.git_dirty_paths() - dirty0
        if leftover:
            record["reverted_uncommitted"] = ua.git_revert_paths(leftover)
        head1 = ua.git_head()
        commits = [c for c in ua.git_commits_between(head0, head1)]
        ours = [c for c in commits if c.split(" ", 1)[1].startswith(PREFIX)]

        # The runner's own verdict — never the agent's word alone.
        rc1, out1 = run_tests("--only", ",".join(todo))
        rc2, out2 = run_tests()       # every test the commits touch, vs origin/main
        ok = rc1 == 0 and rc2 == 0
        tail = lambda o: next((l for l in reversed(o.splitlines()) if "passed" in l), "")  # noqa: E731
        verified = f"failing tests: {tail(out1)} · affected: {tail(out2)}"
        record.update(verify_ok=ok, verify=verified, commits=commits)
        pushed = None
        if ours and not ok:
            record["undo"] = undo(head0, commits)
            verified += f" → {record['undo']}"
            for t, r in results.items():
                if r["outcome"] in ("fixed_code", "fixed_test"):
                    r["outcome"] = "verify_failed"
        elif ours and ok and not args.no_push:
            pushed = ua._git("push").returncode == 0
            record["pushed"] = pushed
            code_changed = [f for f in ua.git_changed_files(head0, head1)
                            if f.endswith(".py") and not f.split("/")[-1].startswith("test_")]
            if pushed and code_changed:
                r = subprocess.run(["sudo", "-n", "systemctl", "restart", "grade-daemon"],
                                   capture_output=True, text=True)
                record["restarted_grade_daemon"] = r.returncode == 0
                if any(f in ("listener.py",) for f in code_changed):
                    verified += " · ⚠ listener.py changed — restart telegram-forwarder yourself"
        elif not ours and ok:
            for r in results.values():
                if r["outcome"] == "unreported":
                    r["outcome"] = "flaky"

        stamp_iso = now.isoformat(timespec="seconds")
        for t, r in results.items():
            settle(state, t, r["outcome"], stamp_iso)
        save_state(state)
        record["results"] = results
        ua.append_runs_log(RUNS_LOG, record)
        ua.send_watchdog_dm(dm_text(results, parked, commits=ours, verified=verified,
                                    pushed=pushed, meta=invoker.last_call), as_html=True)
        print(f"done: {json.dumps({t: r['outcome'] for t, r in results.items()})}; "
              f"verify {'ok' if ok else 'FAILED'}; commits {len(ours)}")
        return 0


if __name__ == "__main__":
    sys.exit(main())
