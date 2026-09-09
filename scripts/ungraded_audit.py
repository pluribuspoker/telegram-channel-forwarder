#!/usr/bin/env python3
"""Nightly ungraded-pick audit — one fresh /investigate agent per stale pick.

``ungraded-audit.timer`` runs this every night at 04:05 ET (after the last
West-coast finals have had hours to settle, before the 06:00 auto-reboot /
unattended-upgrades window). It:

1. scans ``parse_cache.json`` for entries with unresolved legs (no
   WIN/LOSS/PUSH/VOID verdict) whose stale-reference date — the same
   game-not-post horizon ``grade_daemon._stale_reference_date`` uses — is in
   the past, including entries the daemon already retired as ``_failed``
   (except "message deleted": there is nothing left to grade);
2. groups fan-out copies of the same pick (one source forwarded into several
   dest channels — same capper, same unresolved legs, same reference date)
   so ONE agent fixes every copy, per the multi-dest rule;
3. for each group, SEQUENTIALLY, launches one fresh headless ``claude -p``
   agent in ``/home/forwarder/app`` that runs the ``/investigate`` command:
   decide whether the pick should have graded; if yes, fix the root cause in
   code (with the pinned-test conventions), grade the pick, and verify; if it
   is legitimately ungradeable, change nothing and say so. Agents never push
   and never restart services — the runner does both, once, at the end;
4. audits everything: the full stream-json transcript and final report per
   pick under ``logs/ungraded_audit/<date>/``, one JSONL line per scan and
   per agent call in ``logs/ungraded_audit_runs.jsonl`` (cost, usage, turns,
   commits, outcome), attempts/parking in ``logs/ungraded_audit_state.json``;
5. pushes any commits the agents made, restarts ``grade-daemon`` if code
   changed (the tracker timer picks new code up by itself; the listener is
   NEVER auto-restarted — flood-wait caution — only flagged in the DM);
6. DMs the operator through the watchdog bot: one line per pick — outcome,
   what the issue was, what changed. Silent when the scan found nothing
   (watchdog convention: silent-unless-alerting; the scan ledger line is
   still written every night).

Every agent call bills the Claude Code subscription (OAuth token, same as the
interactive session and the god judge). A failed call is logged and reported,
never retried in the same night; a pick gets at most ``--attempt-cap`` nights
(default 2) before it is parked as needs-human. ``--dry-run`` scans and
prints the plan, calls nothing, writes nothing. Exit status is 0 when there
was nothing to do or every failure was a logged agent call, 1 only on an
unexpected exception.

Full detail and recovery levers: docs/ungraded-audit.md.
"""

from __future__ import annotations

import argparse
import fcntl
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import time
import urllib.parse
import urllib.request
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
load_dotenv(ROOT / ".env.local")
load_dotenv(ROOT / ".env")

ET = ZoneInfo("America/New_York")
RESOLVED = ("WIN", "LOSS", "PUSH", "VOID")

CACHE_FILE = ROOT / "parse_cache.json"
STATE_FILE = ROOT / "logs" / "ungraded_audit_state.json"
LOCK_FILE = ROOT / "logs" / ".ungraded_audit.lock"
TRANSCRIPT_ROOT = ROOT / "logs" / "ungraded_audit"
DEFAULT_RUNS_LOG = ROOT / "logs" / "ungraded_audit_runs.jsonl"
TRANSCRIPT_RETENTION_DAYS = 90

DEFAULT_MAX_PICKS = int(os.environ.get("UNGRADED_AUDIT_MAX_PICKS") or 3)
DEFAULT_DAYS_BACK = int(os.environ.get("UNGRADED_AUDIT_DAYS_BACK") or 10)
DEFAULT_ATTEMPT_CAP = int(os.environ.get("UNGRADED_AUDIT_ATTEMPT_CAP") or 2)
AGENT_TIMEOUT = int(os.environ.get("UNGRADED_AUDIT_AGENT_TIMEOUT") or 1500)
BUDGET_MIN = int(os.environ.get("UNGRADED_AUDIT_BUDGET_MIN") or 90)
MAX_TURNS = int(os.environ.get("UNGRADED_AUDIT_MAX_TURNS") or 150)
MODEL = os.environ.get("UNGRADED_AUDIT_MODEL") or "claude-fable-5"
EFFORT = "max"
DEFAULT_CLAUDE_BIN = (
    os.environ.get("UNGRADED_AUDIT_CLAUDE_BIN")
    or "/home/forwarder/.npm-global/bin/claude"
)
GROUP_CAP = 4  # fan-out copies of one pick handled by a single agent

OUTCOMES = (
    "graded",             # verdict now persisted + message/broadcast repaired
    "fixed_needs_verify", # code fixed; normal flow should grade it shortly
    "legit_ungraded",     # correctly ungraded (postponed, not a pick, ...)
    "needs_human",        # product decision / paid API / ambiguity — parked
    "no_issue",           # already resolved by the time the agent looked
)
PARK_OUTCOMES = ("legit_ungraded", "needs_human", "no_issue")


# ─── scan ────────────────────────────────────────────────────────────────────

def _stale_reference_date(leg_verdicts: dict, odds_by_pick: dict, msg_date: str) -> str:
    """Mirror of grade_daemon._stale_reference_date (kept import-free: pulling
    grade_daemon in would drag telethon into a job that must stay light).
    Ages off the GAME, not the post; a leg with no known game contributes
    nothing and falls back to the post date — exactly the leg worth auditing."""
    dates = [d for d in [msg_date] if d]
    for src in (leg_verdicts, odds_by_pick):
        for v in (src or {}).values():
            if isinstance(v, dict):
                gd = v.get("game_date")
                if isinstance(gd, str) and len(gd) == 10:
                    dates.append(gd)
    return max(dates) if dates else msg_date


def _unresolved_indices(picks: list, leg_verdicts: dict) -> list[int]:
    """Same predicate the daemon greps for pending legs, VOID counted as
    settled and a lost parlay's pending siblings treated as moot."""
    parlay_lost = any(p.get("is_parlay_leg") for p in picks) and any(
        (leg_verdicts.get(str(i)) or {}).get("verdict") == "LOSS"
        for i in range(len(picks))
    )
    if parlay_lost:
        return []
    return [
        i for i in range(len(picks))
        if (leg_verdicts.get(str(i)) or {}).get("verdict") not in RESOLVED
    ]


def _leg_facts(entry: dict, indices: list[int]) -> list[dict[str, Any]]:
    picks = (entry.get("parsed") or {}).get("picks") or []
    lv = entry.get("leg_verdicts") or {}
    odds = entry.get("odds_by_pick") or {}
    facts = []
    for i in indices:
        pick = picks[i] if i < len(picks) else {}
        leg = lv.get(str(i)) or {}
        facts.append({
            "idx": i,
            "description": pick.get("description") or "",
            "bet_type": pick.get("bet_type") or "",
            "sport": pick.get("sport") or (entry.get("parsed") or {}).get("sport") or "",
            "period": pick.get("period") or "",
            "game_date": leg.get("game_date")
            or (odds.get(str(i)) or {}).get("game_date") or "",
            "unknown_attempts": leg.get("unknown_attempts") or 0,
        })
    return facts


def _fingerprint(capper: str, ref: str, legs: list[dict[str, Any]]) -> tuple:
    descs = tuple(sorted(re.sub(r"\s+", " ", l["description"].strip().lower())
                         for l in legs))
    return (capper.strip().lower(), ref, descs)


def scan(
    cache: dict,
    state: dict,
    *,
    today_et: date,
    days_back: int = DEFAULT_DAYS_BACK,
    attempt_cap: int = DEFAULT_ATTEMPT_CAP,
) -> list[dict[str, Any]]:
    """Ungraded-pick groups worth an agent tonight, newest reference first.

    A group is every cache key that carries the same pick (fan-out copies:
    same capper, same unresolved descriptions, same reference date); the
    whole group is skipped when any copy is parked or attempt-capped, since
    one verdict covers them all.
    """
    floor = (today_et - timedelta(days=days_back)).isoformat()
    yesterday = (today_et - timedelta(days=1)).isoformat()
    groups: dict[tuple, dict[str, Any]] = {}
    for key, entry in cache.items():
        if not isinstance(entry, dict) or "parsed" not in entry:
            continue
        if entry.get("_dupe"):
            continue
        picks = (entry.get("parsed") or {}).get("picks") or []
        if not picks:
            continue
        lv = entry.get("leg_verdicts") or {}
        unresolved = _unresolved_indices(picks, lv)
        if not unresolved:
            continue
        reason = str(entry.get("_failed_reason") or "")
        if entry.get("_failed") and reason.startswith("message deleted"):
            continue  # nothing left to grade or repair
        msg_date = str(entry.get("msg_date") or "")[:10]
        ref = _stale_reference_date(lv, entry.get("odds_by_pick") or {}, msg_date)
        if not ref or ref > yesterday:
            continue  # game not over yet — the normal flow still owns it
        if ref < floor:
            continue  # written-off backlog stays written off
        capper = str(entry.get("capper_name") or "")
        legs = _leg_facts(entry, unresolved)
        member = {
            "key": key,
            "msg_date": msg_date,
            "failed": bool(entry.get("_failed")),
            "failed_reason": reason,
            "legs": legs,
            "resolved": [
                {
                    "idx": i,
                    "description": (picks[i].get("description") or "")[:60],
                    "verdict": (lv.get(str(i)) or {}).get("verdict"),
                }
                for i in range(len(picks))
                if (lv.get(str(i)) or {}).get("verdict") in RESOLVED
            ],
        }
        fp = _fingerprint(capper, ref, legs)
        group = groups.setdefault(fp, {
            "capper": capper, "ref_date": ref, "members": [],
        })
        if len(group["members"]) < GROUP_CAP:
            group["members"].append(member)

    out = []
    for group in groups.values():
        group["members"].sort(key=lambda m: m["key"])
        keys = [m["key"] for m in group["members"]]
        st = [state.get(k) or {} for k in keys]
        if any(s.get("parked") for s in st):
            continue
        if max((s.get("attempts") or 0) for s in st) >= attempt_cap:
            continue
        group["keys"] = keys
        group["attempts"] = max((s.get("attempts") or 0) for s in st)
        out.append(group)
    out.sort(key=lambda g: (g["ref_date"], g["keys"][0]), reverse=True)
    return out


# ─── prompt ──────────────────────────────────────────────────────────────────

def _tme_link(key: str) -> str:
    try:
        ch, msg = key.split(":")
        return f"https://t.me/c/{ch.removeprefix('-100')}/{msg}"
    except ValueError:
        return ""


def build_prompt(group: dict[str, Any], *, today_et: date) -> str:
    """The whole -p prompt: a /investigate invocation whose argument carries
    the facts, the mission, the nightly-run constraint overrides, and the
    machine-readable result contract the runner parses for the DM."""
    lines = []
    first = group["members"][0]
    desc0 = (first["legs"][0]["description"] or "pick")[:80] if first["legs"] else "pick"
    lines.append(
        f"/investigate NIGHTLY UNGRADED AUDIT {today_et.isoformat()}: "
        f"pick by {group['capper'] or 'unknown capper'} — “{desc0}” — still has no "
        f"verdict although its stale-reference date is {group['ref_date']}. "
        "Determine whether it SHOULD have been graded; if yes, fix the root "
        "cause in code AND grade it; if it is legitimately ungradeable, change "
        "nothing and say so."
    )
    lines.append("")
    lines.append("## Facts (from parse_cache.json)")
    if len(group["members"]) > 1:
        lines.append(
            f"- {len(group['members'])} cache entries are fan-out copies of the SAME "
            "pick (one source → several dest channels, independent cache/message/"
            "broadcast per copy). Whatever you conclude or repair must cover EVERY "
            "copy listed below, not just the first."
        )
    for m in group["members"]:
        lines.append(f"- cache key `{m['key']}` ({_tme_link(m['key'])}), posted {m['msg_date']}"
                     + (f", RETIRED by grade-daemon: {m['failed_reason']!r}" if m["failed"] else ""))
        for leg in m["legs"]:
            bits = [f"leg {leg['idx']}: {leg['description']!r}"]
            if leg["bet_type"]:
                bits.append(f"bet_type={leg['bet_type']}")
            if leg["period"]:
                bits.append(f"period={leg['period']}")
            if leg["sport"]:
                bits.append(f"sport={leg['sport']}")
            bits.append(f"game_date={leg['game_date'] or 'UNKNOWN'}")
            if leg["unknown_attempts"]:
                bits.append(f"unknown_attempts={leg['unknown_attempts']} (capped at 6)")
            lines.append("    - unresolved " + ", ".join(bits))
        for r in m["resolved"]:
            lines.append(f"    - resolved leg {r['idx']}: {r['description']!r} → {r['verdict']}")
    lines.append("")
    lines.append("## Constraints — these OVERRIDE the standard /investigate workflow where they conflict")
    lines.append(
        "- You are a headless nightly agent on the VPS (as forwarder, in "
        "/home/forwarder/app); no human is available. Work directly in this "
        "repo — NO git worktree, NO SSH."
    )
    lines.append(
        "- NEVER `git push` and NEVER restart/stop `telegram-forwarder`. The "
        "audit runner pushes and restarts grade-daemon after all agents "
        "finish. If you must edit parse_cache.json entries: `sudo -n "
        "systemctl stop grade-daemon` first, `sudo -n systemctl start "
        "grade-daemon` when done (the runner re-checks it at the end)."
    )
    lines.append(
        "- Commit any code fix locally: stage ONLY the files you changed "
        "(never `git add -A`; the tree may hold unrelated work-in-progress — "
        "leave it untouched), commit message prefixed `nightly-audit:`."
    )
    lines.append(
        "- A code fix must follow the repo's invariants (CLAUDE.md + the "
        "subsystem's docs/*.md — read the doc first) and carry/extend the "
        "pinned test where one exists. Don't fix grading with prompt text. "
        "No new paid API calls; free sources only — if only a paid path "
        "could grade it, report needs_human with the cost math."
    )
    lines.append(
        "- Grade via the real pipeline where possible (targeted tracker run, "
        "`python tracker.py --live --target=<channel>:<msg>`), and verify the "
        "live message/emoji/broadcast state afterwards like the investigate "
        "workflow requires. Do not message the operator; the runner sends "
        "the summary."
    )
    lines.append(
        "- Budget ~20 minutes; the runner kills you at 25. If the root cause "
        "needs a human decision, stop early and report needs_human. Add an "
        "/investigate lesson ONLY for a novel debugging technique, never for "
        "a routine code fix."
    )
    lines.append("")
    lines.append("## Result contract")
    lines.append(
        "End your FINAL message with exactly one line (single line, valid "
        "JSON, no code fence):"
    )
    lines.append(
        'AUDIT_RESULT: {"outcome": "graded|fixed_needs_verify|legit_ungraded|'
        'needs_human|no_issue", "issue": "<one line: what the problem was>", '
        '"action": "<one line: what you changed, or none>"}'
    )
    return "\n".join(lines)


# ─── headless agent ──────────────────────────────────────────────────────────

class AgentCallError(RuntimeError):
    pass


ENVELOPE_FIELDS = (
    "subtype", "duration_ms", "duration_api_ms", "num_turns",
    "total_cost_usd", "usage", "session_id",
)


class HeadlessInvoker:
    """One fresh full-tool ``claude -p`` run; stream-json teed to a file.

    Unlike the god judge (isolated, toolless), this agent needs the whole dev
    setup: cwd = the repo (CLAUDE.md + .claude/commands/investigate.md load
    from there), permissions skipped, all tools. Its environment is still
    built from scratch: CLAUDE_CODE_OAUTH_TOKEN so the run bills the
    subscription, and deliberately NO ANTHROPIC_API_KEY (present in .env —
    the CLI would bill the API with it; the bash commands the agent runs
    don't need it inherited either, every script load_dotenv()s from disk).
    NIGHTLY_AUDIT=1 makes the session hooks (resume-notify DM,
    post-investigate stop gate) stand down. --no-session-persistence keeps
    nightly transcripts out of ~/.claude/projects, where they would poison
    the resume-notify hook's previous-session lookup; the stream file IS the
    durable transcript.
    """

    def __init__(self, claude_bin: str, *, oauth_token: str,
                 timeout: int = AGENT_TIMEOUT) -> None:
        if not oauth_token:
            raise ValueError("CLAUDE_CODE_OAUTH_TOKEN is required")
        self.claude_bin = claude_bin
        self.oauth_token = oauth_token
        self.timeout = timeout
        self.last_call: dict[str, Any] = {}

    def command(self, prompt: str) -> list[str]:
        return [
            self.claude_bin, "-p", prompt,
            "--dangerously-skip-permissions",
            "--model", MODEL,
            "--effort", EFFORT,
            "--max-turns", str(MAX_TURNS),
            "--output-format", "stream-json",
            "--verbose",
            "--no-session-persistence",
        ]

    def environment(self) -> dict[str, str]:
        return {
            "PATH": os.environ.get(
                "PATH",
                "/home/forwarder/.npm-global/bin:/usr/local/bin:/usr/bin:/bin",
            ),
            "HOME": os.environ.get("HOME", "/home/forwarder"),
            "TERM": "dumb",
            "LANG": "C.UTF-8",
            "CLAUDE_CODE_OAUTH_TOKEN": self.oauth_token,
            "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1",
            "NIGHTLY_AUDIT": "1",
        }

    def __call__(self, prompt: str, transcript_path: Path) -> str:
        """Returns the final result text; raises AgentCallError otherwise."""
        started = time.monotonic()
        self.last_call = {"transcript": str(transcript_path)}
        transcript_path.parent.mkdir(parents=True, exist_ok=True)
        with transcript_path.open("wb") as out:
            proc = subprocess.Popen(
                self.command(prompt),
                cwd=str(ROOT),
                env=self.environment(),
                stdin=subprocess.DEVNULL,
                stdout=out,
                stderr=subprocess.PIPE,
                start_new_session=True,  # so a timeout can kill bash children too
            )
            try:
                _, stderr = proc.communicate(timeout=self.timeout)
            except subprocess.TimeoutExpired:
                try:
                    os.killpg(proc.pid, signal.SIGKILL)
                except OSError:
                    pass
                proc.wait(timeout=30)
                self.last_call["wall_ms"] = int((time.monotonic() - started) * 1000)
                raise AgentCallError(f"agent timed out after {self.timeout}s")
        self.last_call["wall_ms"] = int((time.monotonic() - started) * 1000)
        self.last_call["exit_code"] = proc.returncode
        tail = (stderr or b"").decode("utf-8", "replace").strip()[-1000:]
        if tail:
            self.last_call["stderr_tail"] = tail
        envelope = self._result_event(transcript_path)
        if envelope is None:
            raise AgentCallError(
                f"no result event in stream (exit {proc.returncode}): "
                f"{tail or 'no stderr'}"
            )
        for field in ENVELOPE_FIELDS:
            if field in envelope:
                self.last_call[field] = envelope[field]
        result = envelope.get("result")
        if envelope.get("is_error"):
            raise AgentCallError(
                f"agent reported an error: {str(result or envelope.get('subtype'))[:500]}"
            )
        if proc.returncode != 0:
            raise AgentCallError(
                f"claude exited {proc.returncode}: {tail or 'no stderr'}"
            )
        if not isinstance(result, str) or not result.strip():
            raise AgentCallError("agent returned an empty result")
        return result

    @staticmethod
    def _result_event(transcript_path: Path) -> dict | None:
        event = None
        try:
            with transcript_path.open(encoding="utf-8", errors="replace") as fh:
                for line in fh:
                    line = line.strip()
                    if not line.startswith("{"):
                        continue
                    try:
                        obj = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if isinstance(obj, dict) and obj.get("type") == "result":
                        event = obj
        except OSError:
            return None
        return event


_AUDIT_RE = re.compile(r"AUDIT_RESULT:\s*(\{.*?\})\s*$", re.MULTILINE | re.DOTALL)


def parse_audit_result(result_text: str) -> dict[str, str]:
    """Last AUDIT_RESULT line of the agent's final message, validated; an
    unparseable report degrades to outcome=unparsed with the tail as issue."""
    matches = _AUDIT_RE.findall(result_text or "")
    for raw in reversed(matches):
        try:
            obj = json.loads(raw)
        except json.JSONDecodeError:
            continue
        if isinstance(obj, dict) and obj.get("outcome") in OUTCOMES:
            return {
                "outcome": str(obj["outcome"]),
                "issue": str(obj.get("issue") or "").strip()[:300],
                "action": str(obj.get("action") or "").strip()[:300],
            }
    tail = re.sub(r"\s+", " ", (result_text or "").strip())[-200:]
    return {"outcome": "unparsed", "issue": tail, "action": ""}


# ─── state / ledger / git / DM ───────────────────────────────────────────────

def load_state() -> dict:
    try:
        data = json.loads(STATE_FILE.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def save_state(state: dict) -> None:
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = STATE_FILE.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(state, indent=2, sort_keys=True) + "\n",
                   encoding="utf-8")
    os.replace(tmp, STATE_FILE)


def record_attempt(state: dict, keys: list[str], outcome: str,
                   *, attempt_cap: int) -> bool:
    """Bump every copy of the pick; park terminal outcomes and capped
    attempts. Returns True when the group just got parked."""
    parked = False
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    for key in keys:
        st = state.get(key) or {}
        st["attempts"] = (st.get("attempts") or 0) + 1
        st["last_run"] = now
        st["last_outcome"] = outcome
        if outcome in PARK_OUTCOMES:
            st["parked"] = True
            st["parked_reason"] = outcome
            parked = True
        elif st["attempts"] >= attempt_cap:
            st["parked"] = True
            st["parked_reason"] = f"attempt cap ({st['attempts']})"
            parked = True
        state[key] = st
    return parked


def append_runs_log(path: Path, record: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(record, sort_keys=True, ensure_ascii=False) + "\n")


def _git(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git", *args], cwd=str(ROOT), capture_output=True,
                          text=True, timeout=120)


def git_head() -> str:
    return _git("rev-parse", "HEAD").stdout.strip()


def git_dirty_paths() -> set[str]:
    out = _git("status", "--porcelain").stdout
    return {line[3:].strip() for line in out.splitlines() if line.strip()}


def git_commits_between(old: str, new: str) -> list[str]:
    if not old or not new or old == new:
        return []
    out = _git("log", "--oneline", f"{old}..{new}").stdout
    return [line.strip() for line in out.splitlines() if line.strip()]


def git_changed_files(old: str, new: str) -> list[str]:
    if not old or not new or old == new:
        return []
    out = _git("diff", "--name-only", old, new).stdout
    return [line.strip() for line in out.splitlines() if line.strip()]


def git_revert_paths(paths: set[str]) -> list[str]:
    """Discard a failed agent's uncommitted edits to TRACKED files only —
    never paths that were already dirty before it started, never untracked
    files (those are left in place and reported)."""
    reverted = []
    for path in sorted(paths):
        tracked = _git("ls-files", "--error-unmatch", path).returncode == 0
        if tracked:
            if _git("checkout", "--", path).returncode == 0:
                reverted.append(path)
    return reverted


def send_watchdog_dm(text: str) -> bool:
    """DM the operator through the watchdog bot (same send as god_judge_runner)."""
    token = os.environ.get("WATCHDOG_BOT_TOKEN", "")
    uid = os.environ.get("WATCHDOG_USER_ID", "")
    if not token or not uid:
        print("WATCHDOG_BOT_TOKEN / WATCHDOG_USER_ID not set", file=sys.stderr)
        return False
    data = urllib.parse.urlencode({"chat_id": uid, "text": text}).encode()
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    try:
        with urllib.request.urlopen(
            urllib.request.Request(url, data=data), timeout=20
        ) as response:
            return response.status == 200
    except Exception as exc:
        print(f"send failed: {exc}", file=sys.stderr)
        return False


OUTCOME_LABEL = {
    "graded": "GRADED",
    "fixed_needs_verify": "FIXED (verify tomorrow)",
    "legit_ungraded": "legit ungraded",
    "needs_human": "NEEDS HUMAN",
    "no_issue": "already resolved",
    "unparsed": "ran, report unparsed",
    "error": "agent FAILED",
    "timeout": "agent TIMED OUT",
}


def compose_dm(results: list[dict], notes: list[str], *, run_date: str) -> str:
    lines = [f"pickbot: nightly ungraded audit {run_date} — "
             f"{len(results)} pick(s) examined"]
    for r in results:
        desc = (r.get("desc") or "pick")[:48]
        copies = f", {r['n_keys']} copies" if r.get("n_keys", 1) > 1 else ""
        line = (f"• {r.get('capper') or '?'} — {desc} ({r['ref_date']}{copies}) — "
                f"{OUTCOME_LABEL.get(r['outcome'], r['outcome'])}")
        if r.get("issue"):
            line += f": {r['issue']}"
        if r.get("action") and r["action"].lower() not in ("", "none"):
            line += f" | {r['action']}"
        if r.get("commits"):
            line += f" ({len(r['commits'])} commit(s))"
        if r.get("parked"):
            line += " [parked]"
        lines.append(line)
    lines.extend(notes)
    lines.append("ledger: logs/ungraded_audit_runs.jsonl · transcripts: "
                 f"logs/ungraded_audit/{run_date}/")
    return "\n".join(lines)


def prune_old_transcripts(root: Path, *, today_et: date) -> None:
    if not root.is_dir():
        return
    floor = (today_et - timedelta(days=TRANSCRIPT_RETENTION_DAYS)).isoformat()
    for child in root.iterdir():
        if child.is_dir() and re.fullmatch(r"\d{4}-\d{2}-\d{2}", child.name) \
                and child.name < floor:
            shutil.rmtree(child, ignore_errors=True)


# ─── run ─────────────────────────────────────────────────────────────────────

def run(args: argparse.Namespace) -> int:
    today_et = datetime.now(ET).date()
    run_date = today_et.isoformat()
    runs_log = Path(args.runs_log)

    try:
        cache = json.loads(CACHE_FILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        print(f"cannot read {CACHE_FILE}: {exc}", file=sys.stderr)
        return 1
    state = load_state()

    if args.target:
        groups = []
        for key in args.target:
            entry = cache.get(key)
            if not isinstance(entry, dict) or "parsed" not in entry:
                print(f"--target {key}: no such cache entry", file=sys.stderr)
                return 1
            picks = (entry.get("parsed") or {}).get("picks") or []
            lv = entry.get("leg_verdicts") or {}
            indices = _unresolved_indices(picks, lv) or list(range(len(picks)))
            msg_date = str(entry.get("msg_date") or "")[:10]
            groups.append({
                "capper": str(entry.get("capper_name") or ""),
                "ref_date": _stale_reference_date(
                    lv, entry.get("odds_by_pick") or {}, msg_date),
                "members": [{
                    "key": key, "msg_date": msg_date,
                    "failed": bool(entry.get("_failed")),
                    "failed_reason": str(entry.get("_failed_reason") or ""),
                    "legs": _leg_facts(entry, indices),
                    "resolved": [],
                }],
                "keys": [key], "attempts": 0,
            })
    else:
        groups = scan(cache, state, today_et=today_et,
                      days_back=args.days_back, attempt_cap=args.attempt_cap)

    append_runs_log(runs_log, {
        "logged_at_utc": datetime.now(timezone.utc).isoformat(),
        "kind": "scan", "run_date": run_date, "dry_run": args.dry_run,
        "groups": len(groups),
        "keys": [g["keys"] for g in groups],
    })
    print(f"scan: {len(groups)} candidate group(s) "
          f"({sum(len(g['keys']) for g in groups)} cache keys)")

    picked = groups[: args.max_picks]
    if args.dry_run:
        for g in groups:
            marker = "RUN " if g in picked else "wait"
            desc = g["members"][0]["legs"][0]["description"][:60] if g["members"][0]["legs"] else ""
            print(f"  [{marker}] {g['capper'] or '?'} — {desc!r} ref {g['ref_date']} "
                  f"keys {g['keys']} attempts {g['attempts']}")
        if picked:
            print("\n--- prompt for first group ---")
            print(build_prompt(picked[0], today_et=today_et))
        return 0

    if not picked:
        print("nothing to audit — clean night")
        prune_old_transcripts(TRANSCRIPT_ROOT, today_et=today_et)
        return 0

    oauth = os.environ.get("CLAUDE_CODE_OAUTH_TOKEN", "")
    invoker = HeadlessInvoker(args.claude_bin, oauth_token=oauth,
                              timeout=args.agent_timeout)
    deadline = time.monotonic() + BUDGET_MIN * 60
    head0 = git_head()
    results: list[dict[str, Any]] = []
    notes: list[str] = []

    for group in picked:
        if time.monotonic() > deadline - args.agent_timeout - 60:
            notes.append(f"⏱ budget exhausted — "
                         f"{len(picked) - len(results)} group(s) postponed")
            break
        desc = (group["members"][0]["legs"][0]["description"]
                if group["members"][0]["legs"] else "")
        record: dict[str, Any] = {
            "logged_at_utc": datetime.now(timezone.utc).isoformat(),
            "kind": "agent", "run_date": run_date,
            "keys": group["keys"], "capper": group["capper"],
            "ref_date": group["ref_date"], "desc": desc,
        }
        safe_key = group["keys"][0].replace(":", "_")
        transcript = TRANSCRIPT_ROOT / run_date / f"{safe_key}.stream.jsonl"
        prompt = build_prompt(group, today_et=today_et)
        pre_head, pre_dirty = git_head(), git_dirty_paths()
        print(f"→ agent for {group['keys']} ({group['capper']!r}, ref {group['ref_date']})")
        started = time.monotonic()
        try:
            result_text: str | None = invoker(prompt, transcript)
            record["status"] = "ok"
        except AgentCallError as exc:
            result_text = None
            record["status"] = "error"
            record["error"] = str(exc)
        record["wall_ms"] = int((time.monotonic() - started) * 1000)
        record.update(invoker.last_call)

        post_head, post_dirty = git_head(), git_dirty_paths()
        record["commits"] = git_commits_between(pre_head, post_head)
        record["changed_files"] = git_changed_files(pre_head, post_head)
        leftover = post_dirty - pre_dirty

        if result_text is not None:
            (transcript.parent / f"{safe_key}.result.md").write_text(
                result_text + "\n", encoding="utf-8")
            audit = parse_audit_result(result_text)
        else:
            audit = {
                "outcome": "timeout" if "timed out" in record.get("error", "")
                else "error",
                "issue": record.get("error", ""), "action": "",
            }
        record.update(audit)

        if audit["outcome"] in ("error", "timeout", "unparsed") and leftover:
            record["reverted"] = git_revert_paths(leftover)
            still = sorted(leftover - set(record["reverted"]))
            if still:
                record["leftover_untracked"] = still
                notes.append(f"⚠ {group['keys'][0]}: failed agent left "
                             f"untracked files: {', '.join(still[:5])}")
        elif leftover:
            record["uncommitted_leftover"] = sorted(leftover)
            notes.append(f"⚠ {group['keys'][0]}: agent left uncommitted "
                         f"changes: {', '.join(sorted(leftover)[:5])}")

        parked = record_attempt(state, group["keys"], audit["outcome"],
                                attempt_cap=args.attempt_cap)
        save_state(state)
        record["parked"] = parked
        append_runs_log(runs_log, record)
        results.append({
            "capper": group["capper"], "desc": desc,
            "ref_date": group["ref_date"], "n_keys": len(group["keys"]),
            "outcome": audit["outcome"], "issue": audit["issue"],
            "action": audit["action"], "commits": record["commits"],
            "parked": parked,
        })
        print(f"  ← {audit['outcome']}: {audit['issue'][:120]}")

    # ── one push, one daemon restart, at the end ────────────────────────────
    head1 = git_head()
    if head1 != head0:
        if args.no_push:
            notes.append(f"{len(git_commits_between(head0, head1))} commit(s) "
                         "NOT pushed (--no-push)")
        else:
            push = _git("push")
            if push.returncode == 0:
                notes.append(f"pushed {len(git_commits_between(head0, head1))} "
                             "commit(s)")
            else:
                notes.append("⚠ git push FAILED: "
                             + (push.stderr or push.stdout).strip()[-200:])
        restart = subprocess.run(
            ["sudo", "-n", "systemctl", "restart", "grade-daemon"],
            capture_output=True, text=True, timeout=120)
        notes.append("grade-daemon restarted" if restart.returncode == 0
                     else f"⚠ grade-daemon restart failed: {restart.stderr.strip()[-200:]}")
        touched = set()
        for r in results:
            for c in git_changed_files(head0, head1):
                touched.add(c)
        if "listener.py" in touched:
            notes.append("⚠ listener.py changed — telegram-forwarder NOT "
                         "auto-restarted, deploy it yourself")
    else:
        active = subprocess.run(
            ["systemctl", "is-active", "--quiet", "grade-daemon"])
        if active.returncode != 0:
            started_ok = subprocess.run(
                ["sudo", "-n", "systemctl", "start", "grade-daemon"],
                capture_output=True, text=True, timeout=120)
            notes.append("⚠ grade-daemon was down — restarted" if started_ok.returncode == 0
                         else "⚠ grade-daemon DOWN and restart failed")

    prune_old_transcripts(TRANSCRIPT_ROOT, today_et=today_et)

    dm = compose_dm(results, notes, run_date=run_date)
    print("---\n" + dm)
    if results or any(n.startswith("⚠") for n in notes):
        if args.no_dm:
            print("(DM suppressed by --no-dm)")
        else:
            send_watchdog_dm(dm)
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--max-picks", type=int, default=DEFAULT_MAX_PICKS,
                        help="pick groups per night (default %(default)s)")
    parser.add_argument("--days-back", type=int, default=DEFAULT_DAYS_BACK,
                        help="reference-date window (default %(default)s)")
    parser.add_argument("--attempt-cap", type=int, default=DEFAULT_ATTEMPT_CAP,
                        help="nights per pick before parking (default %(default)s)")
    parser.add_argument("--agent-timeout", type=int, default=AGENT_TIMEOUT,
                        help="seconds per agent (default %(default)s)")
    parser.add_argument("--claude-bin", default=DEFAULT_CLAUDE_BIN)
    parser.add_argument("--runs-log", default=str(
        Path(os.environ.get("UNGRADED_AUDIT_RUNS_LOG") or DEFAULT_RUNS_LOG)))
    parser.add_argument("--dry-run", action="store_true",
                        help="scan + print plan and first prompt; run nothing")
    parser.add_argument("--target", action="append", default=[],
                        help="cache key to audit regardless of scan/state "
                             "(repeatable)")
    parser.add_argument("--rearm", action="append", default=[],
                        help="clear state for a cache key, then exit")
    parser.add_argument("--list-state", action="store_true")
    parser.add_argument("--no-dm", action="store_true")
    parser.add_argument("--no-push", action="store_true")
    args = parser.parse_args(argv)

    if os.environ.get("UNGRADED_AUDIT_DISABLED"):
        print("UNGRADED_AUDIT_DISABLED is set — exiting")
        return 0
    if args.list_state:
        print(json.dumps(load_state(), indent=2, sort_keys=True))
        return 0
    if args.rearm:
        state = load_state()
        for key in args.rearm:
            if state.pop(key, None) is not None:
                print(f"re-armed {key}")
            else:
                print(f"{key} had no state")
        save_state(state)
        return 0
    if not args.dry_run and not os.environ.get("CLAUDE_CODE_OAUTH_TOKEN"):
        parser.error("CLAUDE_CODE_OAUTH_TOKEN is not set (agents bill the "
                     "subscription through it; --dry-run works without)")

    LOCK_FILE.parent.mkdir(parents=True, exist_ok=True)
    with LOCK_FILE.open("w") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            print("another ungraded-audit run holds the lock — exiting")
            return 0
        return run(args)


if __name__ == "__main__":
    sys.exit(main())
