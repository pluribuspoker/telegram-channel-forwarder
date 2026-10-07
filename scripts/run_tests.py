#!/usr/bin/env python3
"""Run every offline test (scripts/test_*.py) and time it — the repo's CI.

Nothing ran the tests before 2026-10-07, so four of them sat broken for 1 day
to 6 months, each only ever run by whoever next touched its subsystem. Now:

- **Before every push** (CLAUDE.md rule; the PreToolUse hint
  .claude/hooks/test_before_push_hint.py reminds on `git push` when the
  ledger has no passing run of HEAD): `python3 scripts/run_tests.py`.
- **Nightly** (test-sweep.timer → run_test_sweep.sh → `--trigger nightly
  --notify`): DMs the operator via the watchdog bot only on a failure, a slow
  suite/test, or (Sundays) the weekly CI-time trend line.

How it runs: a scratch clone of HEAD (`git clone --shared`, removed after)
plus an overlay of this checkout's uncommitted tracked changes and untracked
.py files, so it tests what you are about to push and never touches live
state (the MOE tests must not run in ~/app — they import fcntl-locked stores).
parse_cache.json is copied in for the replay tests. Tests run ONE AT A TIME
(the VPS has ~1 GB RAM), each the way it is written: a unittest.TestCase
file via `python -m unittest scripts.<name>`, anything else as a script; exit
0 = pass. SKIP lists the tests that spend money or need a live session.

CI time is tracked in logs/test_runs.jsonl (one line per run: trigger, head,
total wall time, per-test seconds, failures). `--report` prints the trend and
the slowest tests. Budgets: TEST_SUITE_BUDGET_S (default 300 s for the whole
run) and TEST_BUDGET_S (30 s per test); a test also counts as SLOWER when it
takes > 2x its median over the last runs and > 5 s more. Keep new tests
offline and fast — a slow one shows up in the nightly DM.

    python3 scripts/run_tests.py                 # full suite, ~3 min
    python3 scripts/run_tests.py --only test_odds_watch,test_moe
    python3 scripts/run_tests.py --report        # CI-time trend, no run
"""

import argparse
import json
import os
import re
import shutil
import statistics
import subprocess
import sys
import tempfile
import time
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
LEDGER = ROOT / "logs" / "test_runs.jsonl"
PYTHON = os.environ.get("TEST_PYTHON") or (
    "/home/forwarder/venv/bin/python" if Path("/home/forwarder/venv/bin/python").exists()
    else sys.executable)
TEST_TIMEOUT = int(os.environ.get("TEST_TIMEOUT_S") or 300)
SUITE_BUDGET = float(os.environ.get("TEST_SUITE_BUDGET_S") or 300)
TEST_BUDGET = float(os.environ.get("TEST_BUDGET_S") or 30)
SLOW_FACTOR, SLOW_MIN_DELTA, HISTORY_RUNS = 2.0, 5.0, 7

# Not offline: each spends money or needs a live account. Run them by hand.
SKIP = {
    "test_parlay_veto": "real Claude calls on the subscription (~$0.02/run)",
    "test_claude_sub": "real subscription CLI call",
}
# Copied into the clone for tests that replay real data (read-only use).
DATA_FILES = ("parse_cache.json",)


def discover(only: set[str] | None) -> list[str]:
    names = sorted(p.stem for p in (ROOT / "scripts").glob("test_*.py"))
    return [n for n in names if not only or n in only]


def is_unittest(path: Path) -> bool:
    return bool(re.search(r"unittest\.TestCase", path.read_text(encoding="utf-8", errors="replace")))


def git(*args: str, cwd: Path = ROOT) -> str:
    return subprocess.run(["git", "-C", str(cwd), *args], check=True,
                          capture_output=True, text=True).stdout


def make_clone() -> Path:
    """HEAD + this checkout's uncommitted changes, in a throwaway dir."""
    tmp = Path(tempfile.mkdtemp(prefix="run_tests_"))
    clone = tmp / "app"
    git("clone", "-q", "--shared", str(ROOT), str(clone))
    git("checkout", "-q", git("rev-parse", "HEAD").strip(), cwd=clone)
    changed = [l for l in git("diff", "--name-only", "HEAD").splitlines() if l]
    untracked = [l for l in git("ls-files", "--others", "--exclude-standard").splitlines()
                 if l.endswith(".py")]
    for rel in changed + untracked:
        src, dst = ROOT / rel, clone / rel
        if src.exists():
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dst)
        elif dst.exists():
            dst.unlink()   # deleted in the working tree
    for rel in DATA_FILES:
        if (ROOT / rel).exists():
            shutil.copy2(ROOT / rel, clone / rel)
    return clone


def app_env() -> dict:
    """This process's env + .env + .env.local, as systemd hands them to the
    services (some modules read config at import: trent_watcher needs
    TELEGRAM_API_ID). Passed as variables — the secrets files are never
    copied into the clone. Tests stay offline; the values are only read."""
    env = dict(os.environ)
    code = ("import json,sys; from dotenv import dotenv_values; "
            "print(json.dumps({k: v for f in sys.argv[1:] for k, v in dotenv_values(f).items() "
            "if v is not None}))")
    files = [str(ROOT / f) for f in (".env", ".env.local") if (ROOT / f).exists()]
    if files:
        try:
            p = subprocess.run([PYTHON, "-c", code, *files], capture_output=True,
                               text=True, timeout=30, check=True)
            env.update(json.loads(p.stdout))
        except (subprocess.SubprocessError, json.JSONDecodeError, OSError) as exc:
            print(f"(env files not loaded: {exc})", file=sys.stderr)
    for k in ("MOE_ALLOW_API", "ANTHROPIC_API_KEY"):   # never let a test bill the API
        env.pop(k, None)
    return env


def run_one(clone: Path, name: str, env: dict | None = None) -> dict:
    path = clone / "scripts" / f"{name}.py"
    cmd = ([PYTHON, "-m", "unittest", f"scripts.{name}"] if is_unittest(path)
           else [PYTHON, str(path)])
    t0 = time.monotonic()
    try:
        p = subprocess.run(cmd, cwd=clone, capture_output=True, text=True,
                           timeout=TEST_TIMEOUT, stdin=subprocess.DEVNULL, env=env)
        status = "pass" if p.returncode == 0 else "fail"
        out = (p.stdout + p.stderr)
    except subprocess.TimeoutExpired as exc:
        status = "timeout"
        out = ((exc.stdout or b"").decode(errors="replace") if isinstance(exc.stdout, bytes)
               else (exc.stdout or "")) + f"\n[timed out after {TEST_TIMEOUT}s]"
    return {"name": name, "status": status, "secs": round(time.monotonic() - t0, 2),
            "tail": "\n".join(out.strip().splitlines()[-15:]) if status != "pass" else ""}


def load_ledger() -> list[dict]:
    try:
        return [json.loads(l) for l in LEDGER.read_text(encoding="utf-8").splitlines() if l.strip()]
    except (OSError, json.JSONDecodeError):
        return []


def slower_tests(run: dict, history: list[dict]) -> list[tuple[str, float, float]]:
    """(name, secs now, median before) for tests clearly slower than usual."""
    out = []
    for name, secs in run["secs"].items():
        past = [h["secs"][name] for h in history[-HISTORY_RUNS:]
                if name in h.get("secs", {}) and h.get("ok")]
        if len(past) >= 3:
            med = statistics.median(past)
            if secs > SLOW_FACTOR * med and secs - med > SLOW_MIN_DELTA:
                out.append((name, secs, med))
    return out


def over_budget(run: dict) -> list[tuple[str, float]]:
    return sorted(((n, s) for n, s in run["secs"].items() if s > TEST_BUDGET),
                  key=lambda x: -x[1])


def report(runs: list[dict], n: int) -> str:
    if not runs:
        return "no runs recorded yet (logs/test_runs.jsonl)"
    lines = [f"CI time — last {min(n, len(runs))} of {len(runs)} runs "
             f"(budget {SUITE_BUDGET:.0f}s suite / {TEST_BUDGET:.0f}s per test):"]
    for r in runs[-n:]:
        lines.append(f"  {r['ts'][:16]}  {r['trigger']:<8} {r['head'][:7]}  "
                     f"{r['total_s']:6.1f}s  {r['n']} tests  "
                     + ("ok" if r["ok"] else f"FAILED {','.join(r['failed'])}"))
    last = runs[-1]
    top = sorted(last["secs"].items(), key=lambda x: -x[1])[:8]
    lines.append("slowest in the last run: " + ", ".join(f"{k} {v:.1f}s" for k, v in top))
    totals = [r["total_s"] for r in runs[-n:] if r["n"] >= 10]
    if len(totals) >= 2:
        lines.append(f"suite trend: first {totals[0]:.0f}s → last {totals[-1]:.0f}s, "
                     f"median {statistics.median(totals):.0f}s")
    return "\n".join(lines)


def send_dm(text: str) -> bool:
    token, uid = os.environ.get("WATCHDOG_BOT_TOKEN", ""), os.environ.get("WATCHDOG_USER_ID", "")
    if not token or not uid:
        print("WATCHDOG_BOT_TOKEN / WATCHDOG_USER_ID not set — no DM", file=sys.stderr)
        return False
    data = urllib.parse.urlencode({"chat_id": uid, "text": text[:4000],
                                   "disable_web_page_preview": "true"}).encode()
    try:
        with urllib.request.urlopen(urllib.request.Request(
                f"https://api.telegram.org/bot{token}/sendMessage", data=data), timeout=20) as r:
            return r.status == 200
    except Exception as exc:  # noqa: BLE001 - the run's result stands either way
        print(f"DM failed: {exc}", file=sys.stderr)
        return False


def nightly_message(run: dict, results: list[dict], history: list[dict],
                    weekly: bool) -> str | None:
    """None = nothing worth a DM (all green, in budget, nothing slower)."""
    failed = [r for r in results if r["status"] != "pass"]
    slow = slower_tests(run, history)
    budget = over_budget(run)
    suite_over = run["total_s"] > SUITE_BUDGET
    if not (failed or slow or budget or suite_over or weekly):
        return None
    head = "🧪 tests: " + (f"{len(failed)} FAILING" if failed else "all pass") + \
        f" · {run['total_s']:.0f}s for {run['n']} tests ({run['head'][:7]})"
    parts = [head]
    for r in failed:
        parts.append(f"❌ {r['name']} ({r['status']}, {r['secs']:.0f}s)\n" + r["tail"][-600:])
    if suite_over:
        parts.append(f"🐢 suite over budget: {run['total_s']:.0f}s > {SUITE_BUDGET:.0f}s")
    for name, secs, med in slow:
        parts.append(f"🐢 {name} slower: {secs:.1f}s vs median {med:.1f}s")
    if budget:
        parts.append("⏱ over the per-test budget: " + ", ".join(f"{n} {s:.0f}s" for n, s in budget))
    if weekly:
        week = [h for h in history[-14:] if h.get("trigger") == "nightly"] + [run]
        totals = [h["total_s"] for h in week[-7:]]
        parts.append(f"📈 weekly CI time: {', '.join(f'{t:.0f}s' for t in totals)}")
    parts.append("Fix: python3 scripts/run_tests.py --only <name> · trend: --report")
    return "\n\n".join(parts)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--only", help="comma-separated test names (test_x or x)")
    ap.add_argument("--trigger", default="manual", choices=("manual", "prepush", "nightly"))
    ap.add_argument("--notify", action="store_true", help="DM failures/slowdowns (nightly)")
    ap.add_argument("--report", action="store_true", help="print the CI-time trend and exit")
    ap.add_argument("--runs", type=int, default=14)
    ap.add_argument("--no-record", action="store_true", help="don't append to the ledger")
    args = ap.parse_args()

    history = load_ledger()
    if args.report:
        print(report(history, args.runs))
        return 0

    only = None
    if args.only:
        only = {n if n.startswith("test_") else f"test_{n}" for n in args.only.split(",") if n}
    names = discover(only)
    if only and len(names) != len(only):
        print(f"unknown test(s): {sorted(only - set(names))}", file=sys.stderr)
        return 2
    skipped = [n for n in names if n in SKIP]
    names = [n for n in names if n not in SKIP]

    t0 = time.monotonic()
    clone = make_clone()
    env = app_env()
    results = []
    try:
        for name in names:
            r = run_one(clone, name, env)
            results.append(r)
            mark = {"pass": "✓", "fail": "✗", "timeout": "⏱"}[r["status"]]
            print(f"{mark} {r['secs']:6.1f}s  {name}", flush=True)
            if r["status"] != "pass":
                print("    " + r["tail"].replace("\n", "\n    "), flush=True)
    finally:
        shutil.rmtree(clone.parent, ignore_errors=True)
    total = round(time.monotonic() - t0, 1)

    failed = [r["name"] for r in results if r["status"] != "pass"]
    head = git("rev-parse", "HEAD").strip()
    dirty = bool(git("status", "--porcelain", "--untracked-files=no").strip())
    run = {"ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
           "trigger": args.trigger, "head": head, "dirty": dirty, "total_s": total,
           "n": len(results), "ok": not failed, "failed": failed,
           "secs": {r["name"]: r["secs"] for r in results}}
    if not args.no_record:
        LEDGER.parent.mkdir(parents=True, exist_ok=True)
        with LEDGER.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(run) + "\n")

    print(f"\n{len(results) - len(failed)}/{len(results)} passed in {total:.0f}s"
          + (f" — FAILED: {', '.join(failed)}" if failed else "")
          + (f" · skipped (not offline): {', '.join(skipped)}" if skipped else ""))
    for name, secs, med in slower_tests(run, history):
        print(f"🐢 {name}: {secs:.1f}s vs median {med:.1f}s")
    if total > SUITE_BUDGET and not only:
        print(f"🐢 suite took {total:.0f}s — over the {SUITE_BUDGET:.0f}s budget")

    if args.notify:
        msg = nightly_message(run, results, history,
                              weekly=datetime.now().weekday() == 6)
        if msg:
            send_dm(msg)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
