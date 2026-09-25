#!/usr/bin/env python3
"""Trent watcher auto-repair — one headless /investigate agent per real outage.

Modeled on the nightly ungraded audit (scripts/ungraded_audit.py, whose
machinery this imports): when trent-monitor fails BOTH runner attempts,
run_trent_watcher.sh fire-and-forgets `sudo -n systemctl start --no-block
trent-repair.service`, which runs this. Every guard lives HERE, so a manual
`systemctl start trent-repair` behaves identically to the trigger.

Flow: gate checks → pre-verify (--dry-run of the watcher; a pass means the
outage healed itself → tiny DM, no agent, no cooldown burned) → spawn ONE
headless `claude -p "/investigate …"` (TRENT_REPAIR_MODEL, default Opus 5.5
at high effort — operator's pick 2026-09-24; subscription-billed via
CLAUDE_CODE_OAUTH_TOKEN, never ANTHROPIC_API_KEY) → post-verify --dry-run →
push the agent's commits (`trent-repair:` prefix) → ONE watchdog-bot DM card
(HTML, expandable blockquote) with root cause / fix / verification.

The agent and this runner NEVER restart/stop telegram-forwarder and never
start trent-monitor — the 15-min timer IS the deploy path for a repaired
watcher (oneshot; next tick runs the new code). The verify passes call
scripts/trent_watcher.py --dry-run directly (traceless by design), with
TRENT_FINAL_ATTEMPT=0 so a still-broken watcher can't re-fire the DOWN DM,
and never through run_trent_watcher.sh, so a failed verify can't re-trigger
this service (no recursion).

Guards (state in logs/trent_repair_state.json, unknown keys preserved):
- kill switch TRENT_REPAIR_DISABLED=1 → exit 0 silently
- flock logs/.trent_repair.lock — one repair at a time
- cooldown TRENT_REPAIR_COOLDOWN_HOURS (6) between agent spawns
- TRENT_REPAIR_ATTEMPT_CAP (2) agent runs per outage streak → parked with
  ONE ⚠️ DM; a verified pass or --rearm resets the streak
- needs_human parks immediately (same idea as the audit's terminal park)

Auditable: logs/trent_repair_runs.jsonl (one record per invocation) +
per-run transcript/result under logs/trent_repair/<stamp>/.

Manual: --dry-run (gates + prompt, spawns nothing, writes nothing),
--force (bypass cooldown, not the cap), --rearm (unpark + reset attempts),
--no-push (leave agent commits local).
"""

import argparse
import fcntl
import html
import json
import os
import re
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scripts import ungraded_audit as ua  # noqa: E402  (invoker, git, DM, ledger)

ROOT = ua.ROOT
STATE_FILE = ROOT / "logs" / "trent_repair_state.json"
LOCK_FILE = ROOT / "logs" / ".trent_repair.lock"
RUNS_LOG = ROOT / "logs" / "trent_repair_runs.jsonl"
OUT_DIR = ROOT / "logs" / "trent_repair"
WATCHER_LOG = Path("/tmp/trent_watcher_last_run.log")

REPAIR_MODEL = os.environ.get("TRENT_REPAIR_MODEL") or "claude-opus-5-5"
REPAIR_EFFORT = os.environ.get("TRENT_REPAIR_EFFORT") or "high"
AGENT_TIMEOUT = int(os.environ.get("TRENT_REPAIR_AGENT_TIMEOUT") or 1500)
COOLDOWN_HOURS = float(os.environ.get("TRENT_REPAIR_COOLDOWN_HOURS") or 6)
ATTEMPT_CAP = int(os.environ.get("TRENT_REPAIR_ATTEMPT_CAP") or 2)
PYTHON = os.environ.get("TRENT_REPAIR_PYTHON") or "/home/forwarder/venv/bin/python"

OUTCOMES = (
    "fixed_verified",      # code fixed AND the post-verify fetch passed
    "fixed_needs_verify",  # fixed but unverifiable right now (e.g. rate limit)
    "transient_no_change", # nothing wrong by the time the agent looked
    "needs_human",         # cookies/product decision/ambiguity — parked
    "no_issue",            # watcher healthy; trigger was stale
)
PARK_OUTCOMES = ("needs_human",)

BADGE = {
    "fixed_verified": ("✅", "fixed + verified"),
    "fixed_needs_verify": ("🔧", "fixed — verify next tick"),
    "transient_no_change": ("👌", "transient, no change"),
    "needs_human": ("🙋", "NEEDS HUMAN"),
    "no_issue": ("👌", "healthy at pre-check"),
    "unparsed": ("⚠️", "ran, report unparsed"),
    "error": ("❌", "agent failed"),
    "timeout": ("⏱", "agent timed out"),
    "capped": ("⚠️", "attempt cap — parked"),
    "precheck_pass": ("👌", "healed before repair ran"),
}


class RepairInvoker(ua.HeadlessInvoker):
    """The audit's invoker with the repair model/effort swapped in.

    Everything else (from-scratch env, OAuth-only billing, NIGHTLY_AUDIT=1
    hook standdown, --strict-mcp-config, stream-json transcript, killpg on
    timeout) is inherited unchanged.
    """

    def command(self, prompt: str) -> list[str]:
        cmd = super().command(prompt)
        cmd[cmd.index("--model") + 1] = REPAIR_MODEL
        cmd[cmd.index("--effort") + 1] = REPAIR_EFFORT
        return cmd


# ─── pure logic (tested offline) ─────────────────────────────────────────────

def gate(state: dict, now: datetime, *, force: bool = False) -> tuple[str, str]:
    """Decide whether this invocation may spawn an agent.

    Returns (action, reason): action "run" | "skip" | "capped".
    "capped" means the cap was JUST hit by the previous run and the parked DM
    may need sending (caller checks capped_dm_sent).
    """
    if os.environ.get("TRENT_REPAIR_DISABLED") == "1":
        return "skip", "TRENT_REPAIR_DISABLED=1"
    if state.get("parked"):
        if state.get("capped_dm_sent"):
            return "skip", f"parked ({state.get('parked_reason')})"
        return "capped", str(state.get("parked_reason") or "parked")
    last = state.get("last_spawn_at")
    if last and not force:
        try:
            age = now - datetime.fromisoformat(last)
            if age < timedelta(hours=COOLDOWN_HOURS):
                return "skip", f"cooldown ({age} < {COOLDOWN_HOURS}h since last spawn)"
        except ValueError:
            pass
    return "run", "ok"


def record_spawn(state: dict, now: datetime) -> None:
    state["last_spawn_at"] = now.isoformat(timespec="seconds")
    state["attempts"] = int(state.get("attempts") or 0) + 1


def settle(state: dict, outcome: str) -> None:
    """Apply an agent outcome to the state: reset on verified success, park on
    terminal outcomes or the attempt cap."""
    state["last_outcome"] = outcome
    if outcome == "fixed_verified":
        state["attempts"] = 0
        state["parked"] = False
        state["parked_reason"] = ""
        state["capped_dm_sent"] = False
        return
    if outcome in PARK_OUTCOMES:
        state["parked"] = True
        state["parked_reason"] = outcome
        state["capped_dm_sent"] = False
        return
    if int(state.get("attempts") or 0) >= ATTEMPT_CAP:
        state["parked"] = True
        state["parked_reason"] = f"attempt cap ({state['attempts']})"
        state["capped_dm_sent"] = False


def rearm(state: dict) -> None:
    state["attempts"] = 0
    state["parked"] = False
    state["parked_reason"] = ""
    state["capped_dm_sent"] = False


def classify_verify(exit_code: int, output: str) -> str:
    """One --dry-run of the watcher: 'pass' | 'fail' | 'inconclusive'.

    The watcher's rate-limit path prints "Rate-limited by Twitter" and exits 0
    without proving anything — that's inconclusive, never a pass (the repair
    right after an outage often runs inside X's UserTweets cooldown)."""
    if exit_code != 0:
        return "fail"
    if "Rate-limited by Twitter" in (output or ""):
        return "inconclusive"
    return "pass"


_RESULT_RE = re.compile(r"TRENT_REPAIR_RESULT:\s*(\{.*?\})\s*$", re.MULTILINE | re.DOTALL)


def parse_repair_result(result_text: str) -> dict[str, str]:
    """Last TRENT_REPAIR_RESULT line of the agent's final message; an
    unparseable report degrades to outcome=unparsed with the tail as issue."""
    for raw in reversed(_RESULT_RE.findall(result_text or "")):
        try:
            obj = json.loads(raw)
        except json.JSONDecodeError:
            continue
        if isinstance(obj, dict) and obj.get("outcome") in OUTCOMES:
            return {
                "outcome": str(obj["outcome"]),
                "issue": str(obj.get("issue") or "").strip()[:400],
                "action": str(obj.get("action") or "").strip()[:400],
            }
    tail = re.sub(r"\s+", " ", (result_text or "").strip())[-200:]
    return {"outcome": "unparsed", "issue": tail, "action": ""}


def build_prompt(*, now_et: str, fatal_line: str, log_tail: str,
                 journal_tail: str, head: str) -> str:
    """The whole -p prompt: facts, mission, constraint overrides, contract."""
    lines = [
        f"/investigate TRENT AUTO-REPAIR {now_et}: trent-monitor.service "
        "(the @BookitWithTrent X watcher, docs/trent.md) just FAILED BOTH "
        "runner attempts — no picks are being forwarded. Find the root cause "
        "and fix it if it is a code fix; report precisely if it is not.",
        "",
        "## Facts",
        f"- repo HEAD at spawn: {head}",
        f"- FATAL line: {fatal_line or '(none captured — read the log tail)'}",
        "- last run log tail (/tmp/trent_watcher_last_run.log):",
        "```",
        log_tail.strip() or "(empty)",
        "```",
        "- recent journal (journalctl -u trent-monitor):",
        "```",
        journal_tail.strip() or "(empty)",
        "```",
        "",
        "## Constraints — these OVERRIDE the standard /investigate workflow where they conflict",
        "- You are a headless auto-repair agent on the VPS (as forwarder, in "
        "/home/forwarder/app); no human is available. Work directly in this "
        "repo — NO git worktree, NO SSH.",
        "- NEVER `git push` (this runner pushes once after you finish). NEVER "
        "restart/stop/start telegram-forwarder OR trent-monitor — trent-monitor "
        "is an oneshot 15-min timer, so a committed fix deploys itself on the "
        "next tick, and this runner verifies with a --dry-run.",
        "- Read docs/trent.md and the scripts/x_client.py module docstring "
        "FIRST. The most likely failure family is X changing its web build "
        "again (XClIdGen / transaction-id bootstrap): the 2026-09-24 incident "
        "(commit 0211a9b) is the worked example — probe what X serves now, "
        "snapshot raw payloads under logs/trent_repair/ before they perish, "
        "extend the scan/regexes in scripts/x_client.py, and pin byte-exact "
        "fixtures in scripts/test_xclid_scripts_parse.py. `diagnose_failure()` "
        "in x_client.py already separates bootstrap breakage from cookie "
        "death — trust its kind over any 'bad cookies' guess.",
        "- If the cause is genuinely dead cookies or anything needing a human "
        "(new secrets, paid API, product decision), change nothing, report "
        "needs_human, and say exactly what the operator must do. Never edit "
        ".env/.env.local by hand (scripts/set_env_local.py is the only "
        "writer, and only for values you can actually obtain).",
        "- X request discipline: cookieless page/CDN fetches are fine; do NOT "
        "hammer authenticated endpoints — at most ONE authenticated "
        "verification fetch (`~/venv/bin/python scripts/trent_watcher.py "
        "--dry-run`), and never loosen any fetch bounds (Pikkit revocations "
        "2026-09-11/24 were exactly that).",
        "- Commit any fix locally: stage ONLY files you changed (never `git "
        "add -A`), commit message prefixed `trent-repair:`. Run the pinned "
        "tests you touched (scripts/test_xclid_scripts_parse.py runs offline).",
        "- Free sources only; no new paid API calls.",
        "",
        "## Result contract (the runner parses this for the operator's DM)",
        "End your FINAL message with exactly one line:",
        'TRENT_REPAIR_RESULT: {"outcome": "<fixed_verified|fixed_needs_verify|'
        'transient_no_change|needs_human|no_issue>", "issue": "<root cause, '
        'one sentence>", "action": "<what you changed / what the operator '
        'must do, one sentence>"}',
    ]
    return "\n".join(lines)


# ─── context capture ─────────────────────────────────────────────────────────

def collect_context() -> dict[str, str]:
    log_tail = ""
    try:
        log_tail = "\n".join(
            WATCHER_LOG.read_text(encoding="utf-8", errors="replace").splitlines()[-40:]
        )
    except OSError:
        pass
    fatal = ""
    for line in reversed(log_tail.splitlines()):
        if line.startswith("FATAL"):
            fatal = line.strip()
            break
    journal = ""
    try:
        journal = subprocess.run(
            ["journalctl", "-u", "trent-monitor.service", "-n", "60",
             "--no-pager", "-o", "short"],
            capture_output=True, text=True, timeout=30,
        ).stdout[-4000:]
    except Exception:
        pass
    return {"log_tail": log_tail, "fatal_line": fatal, "journal_tail": journal}


def run_watcher_dry() -> tuple[int, str]:
    env = dict(os.environ)
    env["TRENT_FINAL_ATTEMPT"] = "0"  # a failing verify must not re-DM DOWN
    try:
        proc = subprocess.run(
            [PYTHON, "scripts/trent_watcher.py", "--dry-run"],
            cwd=str(ROOT), env=env, capture_output=True, text=True, timeout=180,
        )
        return proc.returncode, (proc.stdout or "") + (proc.stderr or "")
    except subprocess.TimeoutExpired:
        return 124, "verify dry-run timed out"


# ─── state / DM ──────────────────────────────────────────────────────────────

def load_state() -> dict:
    try:
        data = json.loads(STATE_FILE.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def save_state(state: dict) -> None:
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = STATE_FILE.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(state, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(tmp, STATE_FILE)


def dm_card(outcome: str, report: dict[str, str], *, commits: list[str],
            verify: str, meta: dict) -> str:
    emoji, label = BADGE.get(outcome, ("❓", outcome))
    esc = html.escape
    inner = []
    if report.get("issue"):
        inner.append(f"<b>Issue:</b> {esc(report['issue'])}")
    if report.get("action"):
        inner.append(f"<b>Action:</b> {esc(report['action'])}")
    inner.append("<b>Commits:</b> " + (esc("; ".join(commits)) if commits else "none"))
    inner.append(f"<b>Verify:</b> {esc(verify)}")
    bits = [f"{REPAIR_MODEL}/{REPAIR_EFFORT}"]
    if meta.get("wall_ms"):
        bits.append(f"{int(meta['wall_ms'] / 1000)}s")
    if meta.get("num_turns"):
        bits.append(f"{meta['num_turns']} turns")
    inner.append(esc(" · ".join(str(b) for b in bits)))
    return (
        f"🛠 <b>Trent auto-repair</b> — {emoji} {esc(label)}\n"
        f"<blockquote expandable>{chr(10).join(inner)}</blockquote>"
    )


# ─── main ────────────────────────────────────────────────────────────────────

def main() -> int:
    parser = argparse.ArgumentParser(description="Trent watcher auto-repair agent")
    parser.add_argument("--dry-run", action="store_true",
                        help="gates + prompt only; spawn nothing, write nothing")
    parser.add_argument("--force", action="store_true", help="bypass the cooldown")
    parser.add_argument("--rearm", action="store_true",
                        help="unpark + reset the attempt streak, then exit")
    parser.add_argument("--no-push", action="store_true",
                        help="leave agent commits local")
    args = parser.parse_args()

    now = datetime.now(timezone.utc)
    state = load_state()

    if args.rearm:
        rearm(state)
        save_state(state)
        print("re-armed")
        return 0

    action, reason = gate(state, now, force=args.force)
    if action == "skip":
        print(f"skip: {reason}")
        return 0
    if action == "capped":
        print(f"parked: {reason} — sending the one ⚠️ DM")
        if not args.dry_run:
            sent = ua.send_watchdog_dm(
                f"⚠️ Trent auto-repair is PARKED ({reason}) — the watcher is "
                f"still failing and needs a human. Re-arm after fixing: "
                f"sudo systemctl start trent-monitor.service to confirm, then "
                f"~/venv/bin/python scripts/trent_repair.py --rearm"
            )
            if sent:
                state["capped_dm_sent"] = True
                save_state(state)
        return 0

    ctx = collect_context()
    stamp = now.strftime("%Y%m%d-%H%M%S")
    run_dir = OUT_DIR / stamp
    prompt = build_prompt(
        now_et=now.astimezone().strftime("%Y-%m-%d %H:%M %Z"),
        fatal_line=ctx["fatal_line"], log_tail=ctx["log_tail"],
        journal_tail=ctx["journal_tail"], head=ua.git_head()[:12],
    )

    if args.dry_run:
        print(f"gate: run ({reason}); would write {run_dir}")
        print("---- prompt ----")
        print(prompt)
        return 0

    # Lock only the spawning path; the cheap gate/rearm paths above stay lock-free.
    LOCK_FILE.parent.mkdir(parents=True, exist_ok=True)
    with LOCK_FILE.open("w") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            print("another trent-repair run holds the lock; exiting")
            return 0

        record = {"ts": now.isoformat(timespec="seconds"), "trigger_fatal": ctx["fatal_line"]}

        # Pre-verify: the outage may have healed between the trigger and now
        # (edge flakiness heals; a deploy landed). A pass costs one authed
        # fetch and saves a whole Opus run — and burns NO cooldown/attempt.
        code, out = run_watcher_dry()
        pre = classify_verify(code, out)
        record["pre_verify"] = pre
        if pre == "pass":
            print("pre-verify passed — outage healed; no agent needed")
            ua.send_watchdog_dm(
                "🛠 Trent auto-repair: pre-check passed — the watcher healed "
                "before the agent ran (transient). No changes made."
            )
            record["outcome"] = "precheck_pass"
            ua.append_runs_log(RUNS_LOG, record)
            return 0

        record_spawn(state, now)
        save_state(state)

        head0 = ua.git_head()
        dirty0 = ua.git_dirty_paths()
        invoker = RepairInvoker(
            os.environ.get("TRENT_REPAIR_CLAUDE_BIN") or ua.DEFAULT_CLAUDE_BIN,
            oauth_token=os.environ.get("CLAUDE_CODE_OAUTH_TOKEN", ""),
            timeout=AGENT_TIMEOUT,
        )
        outcome = "error"
        report: dict[str, str] = {"outcome": "error", "issue": "", "action": ""}
        try:
            result_text = invoker(prompt, run_dir / "agent.jsonl")
            (run_dir / "result.md").write_text(result_text, encoding="utf-8")
            report = parse_repair_result(result_text)
            outcome = report["outcome"]
        except ua.AgentCallError as exc:
            outcome = "timeout" if "timed out" in str(exc) else "error"
            report = {"outcome": outcome, "issue": str(exc)[:400], "action": ""}
        record.update({"outcome": outcome, "issue": report["issue"],
                       "action": report["action"], **invoker.last_call})

        head1 = ua.git_head()
        commits = ua.git_commits_between(head0, head1)
        record["commits"] = commits

        # A failed agent's uncommitted edits to tracked files are reverted
        # (pre-existing dirt untouched) — same policy as the audit runner.
        leftover = ua.git_dirty_paths() - dirty0
        if leftover and outcome in ("error", "timeout", "unparsed"):
            record["reverted"] = ua.git_revert_paths(leftover)

        # Post-verify through the real fetch path (rate limit → inconclusive).
        code, out = run_watcher_dry()
        post = classify_verify(code, out)
        record["post_verify"] = post
        verify_note = f"pre={pre}, post={post}"
        if outcome == "fixed_verified" and post != "pass":
            # Agent's claim didn't survive OUR verify — degrade honestly.
            outcome = "fixed_needs_verify" if post == "inconclusive" else "error"
            record["outcome"] = outcome

        if commits and not args.no_push:
            push = ua._git("push")
            record["pushed"] = push.returncode == 0
            if push.returncode != 0:
                verify_note += "; PUSH FAILED"

        settle(state, outcome)
        save_state(state)
        ua.append_runs_log(RUNS_LOG, record)

        ua.send_watchdog_dm(
            dm_card(outcome, report, commits=commits, verify=verify_note,
                    meta=invoker.last_call),
            as_html=True,
        )
        print(f"done: {outcome} ({verify_note}); commits: {len(commits)}")
        # Non-zero only when the repair itself failed AND nothing landed, so
        # the (optional) healthcheck flags it; parked/skip paths stay 0.
        return 0 if outcome not in ("error", "timeout") else 1


if __name__ == "__main__":
    sys.exit(main())
