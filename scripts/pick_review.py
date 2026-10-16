#!/usr/bin/env python3
"""Pick review — Claude reviews EVERY pick, and an agent repairs what it flags.

Operator 2026-10-15: "audit every single pick, both for odds and failing to
parse and failing to grade … at time of send and overnight … have the
reviewer spawn an agent if needed and report to me what its outcome was. No
restrictions on what the agent can do."

Two halves, two units:

- **Review pass** (`pick-review.timer`, every 3 min → this script, no args):
  scans parse_cache.json + picks.db for review questions not yet asked and
  asks each ONE subscription Claude call (pick_reviewer.py, Opus 5.5 medium,
  no tools):
    parse    every freshly parsed source message (fan-out copies = one call)
    grade    every newly resolved leg, with the ESPN final as evidence
    noparse  every message the parser returned no picks for (picks.db)
    nightly  from 05:45 ET, yesterday's messages end to end, in batches —
             incl. legs still unresolved after their game
  A question is keyed by its stage + source + a signature of what was
  reviewed, so a changed parse/verdict (e.g. after an agent's fix) is
  reviewed again and an unchanged one never is. Flags queue in the state
  file and start pick-review-agent.service.
- **Agent pass** (`--agent`, pick-review-agent.service): ONE headless
  `claude -p "/investigate …"` agent (Opus 5.5 high, subscription, the
  odds-watch/hc-repair chassis) per batch of flagged messages, with full
  authority: it judges, repairs every copy + the live post, fixes the class
  in code, may push and restart services. Every outcome — false alarms
  included — is DM'd to the operator as a card. Runner backstops: push any
  unpushed commit, restart grade-daemon on a .py change, start grade-daemon
  if left stopped, revert a failed agent's uncommitted edits.

Guards: PICK_REVIEW_DISABLED=1 (nothing runs), PICK_REVIEW_AGENT_DISABLED=1
(flags DM'd raw, no agent); one agent at a time (flock) and none while the
nightly audit / odds watch / hc-repair holds its lock (1 GB box); a message
re-flagged after PICK_REVIEW_ATTEMPT_CAP (2) agent runs is DM'd raw instead;
PICK_REVIEW_DAILY_CAP (12) agents per rolling 24 h as a runaway breaker.
State logs/pick_review_state.json, every review in logs/pick_review.jsonl,
agent ledger logs/pick_review_runs.jsonl + transcripts logs/pick_review/.
Manual: --dry-run (list the questions; --call also asks Claude, writes
nothing), --nightly-now, --rearm KEY. Test: scripts/test_pick_review.py.
"""

import argparse
import fcntl
import hashlib
import html
import json
import os
import re
import sqlite3
import subprocess
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scripts import ungraded_audit as ua  # noqa: E402  (loads .env/.env.local)
import pick_reviewer as pr  # noqa: E402

ROOT = ua.ROOT
ET = ZoneInfo("America/New_York")
CACHE_FILE = ROOT / "parse_cache.json"
DB_FILE = ROOT / "picks.db"
STATE_FILE = ROOT / "logs" / "pick_review_state.json"
STATE_LOCK = ROOT / "logs" / ".pick_review_state.lock"
AGENT_LOCK = ROOT / "logs" / ".pick_review_agent.lock"
REVIEW_LOG = ROOT / "logs" / "pick_review.jsonl"
RUNS_LOG = ROOT / "logs" / "pick_review_runs.jsonl"
OUT_DIR = ROOT / "logs" / "pick_review"
OTHER_AGENT_LOCKS = [ROOT / "logs" / n for n in
                     (".ungraded_audit.lock", ".odds_watch.lock", ".hc_repair.lock")]

MODEL = os.environ.get("PICK_REVIEW_AGENT_MODEL") or "claude-opus-5-5"
EFFORT = os.environ.get("PICK_REVIEW_AGENT_EFFORT") or "high"
AGENT_TIMEOUT = int(os.environ.get("PICK_REVIEW_AGENT_TIMEOUT") or 2400)
ATTEMPT_CAP = int(os.environ.get("PICK_REVIEW_ATTEMPT_CAP") or 2)
DAILY_CAP = int(os.environ.get("PICK_REVIEW_DAILY_CAP") or 12)
MAX_MESSAGES = int(os.environ.get("PICK_REVIEW_MAX_MESSAGES") or 4)
MAX_CALLS = int(os.environ.get("PICK_REVIEW_MAX_CALLS") or 40)
PARSE_DAYS = 1      # parse/noparse questions: today + yesterday (ET)
GRADE_DAYS = 3      # grade questions: messages up to 3 days old
NIGHTLY_AT = (5, 45)
NIGHTLY_BATCH = 5
FAIL_CAP = 3        # a question whose review keeps erroring is dropped
MAX_ROUNDS = 3

OUTCOMES = ("fixed", "repaired", "false_alarm", "already_handled", "needs_human")
BADGE = {"fixed": "✅", "repaired": "🔧", "false_alarm": "👌", "already_handled": "☑️",
         "needs_human": "🙋", "unparsed": "⚠️", "error": "❌", "timeout": "⏱"}
STAGE_TITLE = {"parse": "parse", "grade": "verdict", "noparse": "no picks parsed",
               "nightly": "overnight review"}


class ReviewInvoker(ua.HeadlessInvoker):
    """The audit's invoker (OAuth-only env, hook standdown, strict MCP,
    stream-json transcript, killpg on timeout) with our model/effort."""

    def command(self, prompt: str) -> list[str]:
        cmd = super().command(prompt)
        cmd[cmd.index("--model") + 1] = MODEL
        cmd[cmd.index("--effort") + 1] = EFFORT
        return cmd


# ─── pure logic (tested offline) ─────────────────────────────────────────────

def sig(obj: Any) -> str:
    return hashlib.sha1(json.dumps(obj, sort_keys=True, default=str).encode()).hexdigest()[:16]


def source_of(key: str, entry: dict) -> str:
    return str(entry.get("_source_key") or key)


def groups_by_source(cache: dict, floor: str) -> dict[str, list[str]]:
    """source → copy keys (primary = first) for live parsed entries with
    msg_date ≥ floor."""
    out: dict[str, list[str]] = {}
    for key, entry in cache.items():
        if (not isinstance(entry, dict) or entry.get("_dupe")
                or entry.get("_failed_reason") or not pr.picks_of(entry)):
            continue
        if str(entry.get("msg_date") or "") < floor:
            continue
        out.setdefault(source_of(key, entry), []).append(key)
    for keys in out.values():
        keys.sort(key=lambda k: (not (cache[k].get("leg_verdicts")), k))
    return out


def parse_questions(cache: dict, state: dict, today: date) -> list[dict]:
    floor = (today - timedelta(days=PARSE_DAYS)).isoformat()
    out = []
    for src, keys in groups_by_source(cache, floor).items():
        entry = cache[keys[0]]
        s = sig(pr.picks_of(entry))
        qid = f"parse:{src}"
        if _asked(state, qid, s):
            continue
        out.append({"stage": "parse", "qid": qid, "sig": s, "source": src, "keys": keys,
                    "capper": entry.get("capper_name") or "", "legs": []})
    return out


def grade_questions(cache: dict, state: dict, today: date) -> list[dict]:
    floor = (today - timedelta(days=GRADE_DAYS)).isoformat()
    out = []
    for src, keys in groups_by_source(cache, floor).items():
        entry = cache[keys[0]]
        legs, sigs = [], {}
        for i in range(len(pr.picks_of(entry))):
            v = pr.verdict_view(entry, i)
            if v["verdict"] is None:
                continue
            s = sig([v["verdict"], v.get("calc")])
            if not _asked(state, f"grade:{src}:{i}", s):
                legs.append(i)
                sigs[i] = s
        if legs:
            out.append({"stage": "grade", "qid": f"grade:{src}", "sig": sigs, "source": src,
                        "keys": keys, "capper": entry.get("capper_name") or "", "legs": legs})
    return out


def noparse_questions(rows: list[tuple], state: dict) -> list[dict]:
    """rows: (channel_id, message_id, date, capper, text) from picks.db.
    Copies of one text (fan-out) are one question."""
    by_text: dict[str, dict] = {}
    for ch, mid, d, capper, text in rows:
        s = sig((text or "").strip())
        q = by_text.setdefault(s, {"stage": "noparse", "qid": f"noparse:{s}", "sig": s,
                                   "source": f"{ch}:{mid}", "keys": [], "capper": capper or "",
                                   "legs": [], "text": text or "", "date": d})
        q["keys"].append(f"{ch}:{mid}")
    return [q for q in by_text.values() if not _asked(state, q["qid"], q["sig"])]


def nightly_questions(cache: dict, day: date) -> list[dict]:
    """Every source message of `day` (ET), batched NIGHTLY_BATCH per call."""
    srcs = [(src, keys) for src, keys in groups_by_source(cache, day.isoformat()).items()
            if str(cache[keys[0]].get("msg_date")) == day.isoformat()]
    srcs.sort()
    out = []
    for n in range(0, len(srcs), NIGHTLY_BATCH):
        chunk = srcs[n:n + NIGHTLY_BATCH]
        out.append({"stage": "nightly", "qid": f"nightly:{day}:{n // NIGHTLY_BATCH}",
                    "sig": day.isoformat(), "batch": [
                        {"source": s, "keys": k, "capper": cache[k[0]].get("capper_name") or ""}
                        for s, k in chunk]})
    return out


def _asked(state: dict, qid: str, s: str) -> bool:
    r = (state.get("reviewed") or {}).get(qid) or {}
    return r.get("sig") == s or (r.get("failed_sig") == s and int(r.get("fails") or 0) >= FAIL_CAP)


def _qids(q: dict) -> list[tuple[str, str]]:
    """(review id, signature) pairs a question settles — a grade question
    settles each of its legs separately."""
    if q["stage"] == "grade":
        return [(f"grade:{q['source']}:{i}", s) for i, s in q["sig"].items()]
    return [(q["qid"], q["sig"])]


def mark_asked(state: dict, q: dict, now: datetime, nflags: int) -> None:
    rv = state.setdefault("reviewed", {})
    for qid, s in _qids(q):
        rv[qid] = {"sig": s, "at": now.isoformat(timespec="seconds"), "flags": nflags}


def mark_failed(state: dict, q: dict) -> None:
    """Count a failed review; FAIL_CAP failures on the same signature drop it."""
    rv = state.setdefault("reviewed", {})
    for qid, s in _qids(q):
        r = rv.setdefault(qid, {})
        if r.get("failed_sig") != s:
            r.update(failed_sig=s, fails=0)
        r["fails"] = int(r.get("fails") or 0) + 1


def flags_to_pending(q: dict, flags: list[dict], now: datetime) -> list[dict]:
    """One pending item per flagged SOURCE message (nightly batches fan out
    by the `message` key each flag names)."""
    stamp = now.isoformat(timespec="seconds")
    if q["stage"] != "nightly":
        return [{"id": f"{q['stage']}:{q['source']}:{stamp}", "stage": q["stage"],
                 "source": q["source"], "keys": q["keys"], "capper": q["capper"],
                 "flags": flags, "at": stamp, **({"text": q["text"]} if q.get("text") else {})}]
    out: dict[str, dict] = {}
    for f in flags:
        msg = f.get("message")
        hit = next((b for b in q["batch"] if msg == b["source"] or msg in b["keys"]), None)
        if hit is None:
            continue
        item = out.setdefault(hit["source"], {
            "id": f"nightly:{hit['source']}:{stamp}", "stage": "nightly",
            "source": hit["source"], "keys": hit["keys"], "capper": hit["capper"],
            "flags": [], "at": stamp})
        item["flags"].append({k: v for k, v in f.items() if k != "message"})
    return list(out.values())


def queue(state: dict, items: list[dict]) -> tuple[list[dict], list[dict]]:
    """Append to state['pending'] unless that (stage, source) already used its
    agent attempts → those come back as `capped` (DM'd raw). A second flag on
    an already-pending message merges into it."""
    pending = state.setdefault("pending", [])
    attempts = state.setdefault("attempts", {})
    added, capped = [], []
    for it in items:
        ak = f"{it['stage']}:{it['source']}"
        if int(attempts.get(ak) or 0) >= ATTEMPT_CAP:
            capped.append(it)
            continue
        same = next((p for p in pending if p["source"] == it["source"]), None)
        if same:
            same["flags"] += [dict(f, stage=it["stage"]) for f in it["flags"]]
            continue
        it["flags"] = [dict(f, stage=it["stage"]) for f in it["flags"]]
        pending.append(it)
        added.append(it)
    return added, capped


def daily_spawns(state: dict, now: datetime) -> int:
    def ts(s):
        try:
            return datetime.fromisoformat(s)
        except (TypeError, ValueError):
            return None
    recent = [s for s in state.get("_spawns", []) if (t := ts(s)) and now - t < timedelta(hours=24)]
    state["_spawns"] = recent
    return len(recent)


_RESULT_RE = re.compile(r"PICK_REVIEW_RESULT:\s*(\[.*?\])\s*$", re.MULTILINE | re.DOTALL)


def parse_results(result_text: str, items: list[dict]) -> dict[str, dict]:
    reports: dict[str, dict] = {}
    for raw in reversed(_RESULT_RE.findall(result_text or "")):
        try:
            got = json.loads(raw)
        except json.JSONDecodeError:
            continue
        if isinstance(got, list):
            for it in got:
                if isinstance(it, dict) and it.get("outcome") in OUTCOMES:
                    reports.setdefault(str(it.get("message") or ""), it)
            break
    tail = re.sub(r"\s+", " ", (result_text or "").strip())[-200:]
    out = {}
    for it in items:
        r = reports.get(it["source"]) or next((reports[k] for k in it["keys"] if k in reports), None)
        if r is None and len(items) == 1 and len(reports) == 1:
            r = next(iter(reports.values()))
        out[it["source"]] = ({"outcome": "unparsed", "issue": tail, "action": ""} if r is None else
                             {"outcome": r["outcome"],
                              "issue": str(r.get("issue") or "").strip()[:500],
                              "action": str(r.get("action") or "").strip()[:500]})
    return out


def _tme(key: str) -> str:
    return ua._tme_link(key)


def _strip(s: str) -> str:
    return re.sub(r"<[^>]+>", "", s or "")


def build_prompt(items: list[dict], cache: dict, *, now_et: str, head: str) -> str:
    lines = [
        f"/investigate PICK REVIEW {now_et}: a Claude reviewer that reads EVERY "
        f"pick flagged {len(items)} forwarded message(s) as possibly wrong — a "
        "parse, a verdict, a missed pick, or a leg that never graded. The "
        "reviewer is a single no-tools call and CAN be wrong: for EACH message, "
        "first JUDGE the flag against ground truth, then fix what is really "
        "broken.",
        "",
        "## Flagged messages",
    ]
    for it in items:
        lines.append(f"### {it['source']}  ({it['capper'] or 'unknown capper'})")
        for key in it["keys"]:
            e = cache.get(key) or {}
            lines.append(f"- copy {key} {_tme(key)} msg_date {e.get('msg_date')}"
                         + (" has_media (a bet slip is ground truth)" if e.get("has_media") else ""))
        for f in it["flags"]:
            leg = "message" if f.get("leg") == "message" else f"leg {f.get('leg')}"
            lines.append(f"- FLAG [{STAGE_TITLE.get(f.get('stage') or it['stage'])}] {leg}: "
                         f"{f['problem']}")
        first = cache.get(it["keys"][0]) or {}
        if pr.picks_of(first):
            lines += ["- displayed text (first copy):", "```", _strip(first.get("html_text"))[:1500].strip(),
                      "```"]
            rows = [pr.leg_view(first, i, p) | pr.verdict_view(first, i)
                    for i, p in enumerate(pr.picks_of(first))]
            lines += ["- parse + binding + price + verdict per leg (first copy):", "```",
                      json.dumps(rows, indent=1, default=str)[:3000], "```"]
        elif it.get("text"):
            lines += ["- the post (the parser found NO picks; picks.db grades row "
                      "calc='no picks extracted'/'parse failed', cache entry `_failed` "
                      "with this text_hash):", "```", it["text"][:1500], "```"]
        lines.append("")
    lines += [
        f"- repo HEAD at spawn: {head}",
        "",
        "## How to work",
        "- Read CLAUDE.md and the subsystem doc (docs/tracker.md for parse/"
        "grading, docs/odds.md for prices, docs/ungraded-audit.md for "
        "unresolved legs) before changing anything. Check ground truth: the "
        "live Telegram post, the bet slip image, ESPN/the free odds sources.",
        "- Another system may already own it: odds-watch (logs/odds_watch_state.json, "
        "held prices), the nightly ungraded audit (logs/ungraded_audit_state.json), "
        "grader anomalies (logs/grader_anomalies.jsonl, hc-repair). If one already "
        "fixed it, report `already_handled`.",
        "- FALSE ALARM is a normal outcome — report `false_alarm` with why, change nothing.",
        "- When it IS wrong: repair EVERY fan-out copy (cache entry + the live post + "
        "a wrong broadcast) through the real pipeline (targeted tracker "
        "`--target=<key>`, scripts/audit_mark.py for a verdict), fix the CLASS in "
        "code with a pinned test (a deterministic backstop, never prompt text "
        "alone for grading), sweep parse_cache.json for other live instances, and "
        "add the class rule to CLAUDE.md/docs. A missed pick (no picks parsed): "
        "make the pipeline parse it — clear the `_failed` marker and run the "
        "targeted tracker so it posts, prices and grades like any pick.",
        "",
        "## Authority — the operator gave you NO restrictions",
        "- You are a headless agent on the VPS (as forwarder, in /home/forwarder/app), "
        "no human available. Edit any file, run anything, commit, push, restart "
        "services. Work directly in this repo — no worktree.",
        "- Before editing parse_cache.json: `sudo -n systemctl stop grade-daemon`, "
        "use the locked load/save (tracker_cache), start it again after.",
        "- To push: `python3 scripts/run_tests.py` green first, then `git push`; restart "
        "grade-daemon for a .py change. telegram-forwarder restarts cost a Telegram "
        "flood wait — only for a listener.py change, once. Do NOT restart "
        "claude-channels (the operator's live chat session).",
        "- Commit as soon as a test passes (uncommitted edits are reverted on a "
        "timeout): stage only your files (never `git add -A`; the tree holds "
        "unrelated WIP), message prefixed `pick-review:`.",
        "- Subscription only: no code calling the Claude API (ANTHROPIC_API_KEY); "
        "never spend paid Odds API credit.",
        f"- Budget ~{AGENT_TIMEOUT // 60 - 5} minutes; the runner kills you at "
        f"{AGENT_TIMEOUT // 60}. Judge every message before fixing any. Don't "
        "message the operator — the runner sends your result as a card. Add an "
        "/investigate lesson only for a novel technique.",
        "",
        "## Result contract (the runner parses this)",
        "End your FINAL message with exactly one line listing EVERY message above:",
        'PICK_REVIEW_RESULT: [{"message": "<the ### heading key exactly>", '
        '"outcome": "<fixed|repaired|false_alarm|already_handled|needs_human>", '
        '"issue": "<what was wrong, or why the flag was wrong — one sentence>", '
        '"action": "<what you changed / what the operator must decide — one sentence>"}]',
        "(fixed = repaired AND the class fixed in code; repaired = live artifacts "
        "repaired, no code change needed.)",
    ]
    return "\n".join(lines)


def _head(it: dict) -> str:
    esc = html.escape
    link = _tme(it["keys"][0]) if it.get("keys") else ""
    name = esc(it.get("capper") or it["source"])
    return f"<a href=\"{esc(link)}\">{name}</a>" if link else name


def dm_card(items: list[dict], reports: dict[str, dict], *, commits: list[str],
            notes: list[str], meta: dict) -> str:
    """One card per agent run — every outcome is reported (operator ask)."""
    esc = html.escape
    blocks = []
    for it in items:
        r = reports[it["source"]]
        stages = sorted({STAGE_TITLE.get(f.get("stage") or it["stage"], "") for f in it["flags"]})
        inner = [f"<b>Flag ({esc(', '.join(stages))}):</b> "
                 + esc(" · ".join(f["problem"] for f in it["flags"])[:600])]
        if r.get("issue"):
            inner.append(f"<b>Finding:</b> {esc(r['issue'])}")
        if r.get("action"):
            inner.append(f"<b>Action:</b> {esc(r['action'])}")
        blocks.append(f"{BADGE.get(r['outcome'], '❓')} {_head(it)} — "
                      f"{esc(r['outcome'].replace('_', ' '))}\n"
                      f"<blockquote expandable>{chr(10).join(inner)}</blockquote>")
    foot = ["<b>Commits:</b> " + (esc("; ".join(commits)) if commits else "none")]
    foot += [esc(n) for n in notes]
    bits = [f"{MODEL}/{EFFORT}"]
    if meta.get("wall_ms"):
        bits.append(f"{int(meta['wall_ms'] / 60000)} min")
    foot.append(esc(" · ".join(bits)))
    return "🔍 <b>Pick review</b>\n" + "\n".join(blocks) + "\n" + "\n".join(foot)


def raw_card(items: list[dict], why: str) -> str:
    """Flags no agent will take (attempt cap / daily cap / agent disabled)."""
    esc = html.escape
    blocks = [f"🚩 {_head(it)}\n<blockquote expandable>"
              + esc(" · ".join(f["problem"] for f in it["flags"])[:700]) + "</blockquote>"
              for it in items]
    return f"🔍 <b>Pick review</b> — flagged, no agent ({esc(why)})\n" + "\n".join(blocks)


# ─── I/O ─────────────────────────────────────────────────────────────────────

def load_json(path: Path) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


class StateLock:
    """Read-modify-write of the state file under a flock (review pass and
    agent pass both write it)."""

    def __enter__(self) -> dict:
        STATE_LOCK.parent.mkdir(parents=True, exist_ok=True)
        self.fh = STATE_LOCK.open("a")
        fcntl.flock(self.fh, fcntl.LOCK_EX)
        self.state = load_json(STATE_FILE)
        return self.state

    def __exit__(self, exc_type, *_: Any) -> None:
        try:
            if exc_type is None:
                _prune(self.state)
                tmp = STATE_FILE.with_suffix(".json.tmp")
                tmp.write_text(json.dumps(self.state, indent=1, sort_keys=True) + "\n",
                               encoding="utf-8")
                os.replace(tmp, STATE_FILE)
        finally:
            fcntl.flock(self.fh, fcntl.LOCK_UN)
            self.fh.close()


def _prune(state: dict) -> None:
    cutoff = (datetime.now(timezone.utc) - timedelta(days=10)).isoformat()
    rv = state.get("reviewed") or {}
    for k in [k for k, v in rv.items() if str(v.get("at") or "9") < cutoff and "fails" not in v]:
        rv.pop(k)
    nightly = state.get("nightly") or {}
    for k in sorted(nightly)[:-14]:
        nightly.pop(k)


def log_review(record: dict) -> None:
    try:
        REVIEW_LOG.parent.mkdir(parents=True, exist_ok=True)
        with REVIEW_LOG.open("a") as fh:
            fh.write(json.dumps(record, default=str) + "\n")
    except OSError:
        pass


def noparse_rows(now: datetime) -> list[tuple]:
    since = (now - timedelta(hours=36)).isoformat()
    try:
        db = sqlite3.connect(f"file:{DB_FILE}?mode=ro", uri=True, timeout=10)
        rows = db.execute(
            "SELECT channel_id, message_id, date, capper_name, prev_caption FROM grades "
            "WHERE calc IN ('parse failed', 'no picks extracted') AND dry_run = 0 "
            "AND graded_at >= ? ORDER BY id", (since,)).fetchall()
        db.close()
        return rows
    except sqlite3.Error as exc:
        print(f"picks.db read failed: {exc}")
        return []


def lock_held(path: Path) -> bool:
    try:
        with path.open("a") as fh:
            fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
            fcntl.flock(fh, fcntl.LOCK_UN)
        return False
    except BlockingIOError:
        return True
    except OSError:
        return False


def start_agent_unit() -> None:
    try:
        subprocess.Popen(["sudo", "-n", "systemctl", "start", "--no-block",
                          "pick-review-agent.service"],
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except OSError:
        pass


# ─── review pass ─────────────────────────────────────────────────────────────

def payload_for(q: dict, cache: dict, now: datetime) -> str:
    if q["stage"] == "parse":
        return pr.parse_payload(q["keys"][0], cache[q["keys"][0]])
    if q["stage"] == "grade":
        entry = cache[q["keys"][0]]
        return pr.grade_payload(q["keys"][0], entry, q["legs"], pr.evidence_for(entry, q["legs"]))
    if q["stage"] == "noparse":
        return pr.noparse_payload(q["keys"][0], q["capper"], q["text"], q["date"])
    items = []
    for b in q["batch"]:
        entry = cache[b["keys"][0]]
        items.append((b["source"], entry,
                      pr.evidence_for(entry, list(range(len(pr.picks_of(entry)))))))
    return pr.nightly_payload(items, now)


def collect(cache: dict, state: dict, now: datetime, *, nightly_now: bool = False) -> list[dict]:
    today = now.astimezone(ET).date()
    qs = parse_questions(cache, state, today)
    qs += noparse_questions(noparse_rows(now), state)
    qs += grade_questions(cache, state, today)
    yday = today - timedelta(days=1)
    local = now.astimezone(ET)
    due = nightly_now or (local.hour, local.minute) >= NIGHTLY_AT
    if due and yday.isoformat() not in (state.get("nightly") or {}):
        qs += [q for q in nightly_questions(cache, yday) if not _asked(state, q["qid"], q["sig"])]
    return qs


def review_pass(args: argparse.Namespace, reviewer: pr.Reviewer = pr.review) -> int:
    now = datetime.now(timezone.utc)
    cache = load_json(CACHE_FILE)
    with StateLock() as state:
        questions = collect(cache, state, now, nightly_now=args.nightly_now)
    if not questions:
        print("clean: nothing new to review")
        return _maybe_start_agent(args)
    print(f"{len(questions)} question(s): "
          + ", ".join(f"{q['stage']}" for q in questions[:MAX_CALLS]))
    if args.dry_run and not args.call:
        for q in questions[:MAX_CALLS]:
            print(f"\n=== {q['qid']}\n{payload_for(q, cache, now)[:1500]}")
        return 0
    all_capped = []
    for q in questions[:MAX_CALLS]:
        payload = payload_for(q, cache, now)
        flags, meta = reviewer(q["stage"], payload)
        log_review({"ts": now.isoformat(timespec="seconds"), "qid": q["qid"],
                    "stage": q["stage"], "flags": flags, **meta})
        print(f"{q['qid']}: " + ("ERROR " + str(meta.get("error")) if flags is None
                                 else f"{len(flags)} flag(s)") + f" ({meta.get('wall_ms')} ms)")
        if args.dry_run:
            for f in flags or []:
                print(f"   🚩 {f}")
            continue
        with StateLock() as state:
            if flags is None:
                mark_failed(state, q)
                continue
            mark_asked(state, q, now, len(flags))
            if flags:
                _, capped = queue(state, flags_to_pending(q, flags, now))
                all_capped += capped
            if q["stage"] == "nightly":
                day = q["sig"]
                left = [x for x in questions if x["stage"] == "nightly" and x["sig"] == day
                        and not _asked(state, x["qid"], x["sig"])]
                if not left:
                    state.setdefault("nightly", {})[day] = now.isoformat(timespec="seconds")
    if args.dry_run:
        return 0
    if all_capped:
        ua.send_watchdog_dm(raw_card(all_capped, f"already had {ATTEMPT_CAP} agent runs"),
                            as_html=True)
    return _maybe_start_agent(args)


def _maybe_start_agent(args: argparse.Namespace) -> int:
    if args.dry_run:
        return 0
    with StateLock() as state:
        pending = list(state.get("pending") or [])
    if pending and not lock_held(AGENT_LOCK):
        print(f"{len(pending)} flagged message(s) pending → starting the agent")
        start_agent_unit()
    return 0


# ─── agent pass ──────────────────────────────────────────────────────────────

def run_agent(items: list[dict], *, no_push: bool) -> None:
    now = datetime.now(timezone.utc)
    cache = load_json(CACHE_FILE)
    with StateLock() as state:
        state.setdefault("_spawns", []).append(now.isoformat(timespec="seconds"))
        attempts = state.setdefault("attempts", {})
        for it in items:
            ak = f"{it['stage']}:{it['source']}"
            attempts[ak] = int(attempts.get(ak) or 0) + 1
    now_et = now.astimezone(ET).strftime("%Y-%m-%d %H:%M %Z")
    prompt = build_prompt(items, cache, now_et=now_et, head=ua.git_head()[:12])
    run_dir = OUT_DIR / now.strftime("%Y%m%d-%H%M%S")
    record: dict[str, Any] = {"ts": now.isoformat(timespec="seconds"),
                              "items": [{k: it[k] for k in ("stage", "source", "flags")}
                                        for it in items]}
    head0, dirty0 = ua.git_head(), ua.git_dirty_paths()
    invoker = ReviewInvoker(os.environ.get("PICK_REVIEW_CLAUDE_BIN") or ua.DEFAULT_CLAUDE_BIN,
                            oauth_token=os.environ.get("CLAUDE_CODE_OAUTH_TOKEN", ""),
                            timeout=AGENT_TIMEOUT)
    try:
        result_text = invoker(prompt, run_dir / "agent.jsonl")
        (run_dir / "result.md").write_text(result_text, encoding="utf-8")
        reports = parse_results(result_text, items)
    except ua.AgentCallError as exc:
        outcome = "timeout" if "timed out" in str(exc) else "error"
        reports = {it["source"]: {"outcome": outcome, "issue": str(exc)[:400], "action": ""}
                   for it in items}
    (run_dir / "prompt.md").write_text(prompt, encoding="utf-8")
    record["reports"] = reports
    record.update(invoker.last_call)

    notes: list[str] = []
    head1 = ua.git_head()
    commits = ua.git_commits_between(head0, head1)
    record["commits"] = commits
    leftover = ua.git_dirty_paths() - dirty0
    if leftover and all(r["outcome"] in ("error", "timeout", "unparsed") for r in reports.values()):
        record["reverted"] = ua.git_revert_paths(leftover)
        notes.append(f"reverted {len(record['reverted'])} uncommitted file(s)")
    if commits:
        if _ahead_of_upstream():  # the agent may have pushed already
            if no_push:
                notes.append(f"{len(commits)} commit(s) NOT pushed (--no-push)")
            elif ua._git("push").returncode != 0:
                notes.append("⚠ git push FAILED")
        changed = ua.git_changed_files(head0, head1)
        if any(f.endswith(".py") for f in changed):
            r = subprocess.run(["sudo", "-n", "systemctl", "restart", "grade-daemon"],
                               capture_output=True, text=True, timeout=120)
            if r.returncode != 0:
                notes.append("⚠ grade-daemon restart failed")
    if subprocess.run(["systemctl", "is-active", "--quiet", "grade-daemon"]).returncode:
        ok = subprocess.run(["sudo", "-n", "systemctl", "start", "grade-daemon"],
                            capture_output=True, timeout=120).returncode == 0
        notes.append("⚠ grade-daemon was down — restarted" if ok
                     else "⚠ grade-daemon DOWN and restart failed")
    record["notes"] = notes

    with StateLock() as state:
        done = {it["id"] for it in items}
        state["pending"] = [p for p in state.get("pending") or [] if p["id"] not in done]
        judged = state.setdefault("judged", {})
        for it in items:
            judged[it["id"]] = {"outcome": reports[it["source"]]["outcome"],
                                "at": now.isoformat(timespec="seconds")}
        for k in sorted(judged, key=lambda k: judged[k]["at"])[:-300]:
            judged.pop(k)
    ua.append_runs_log(RUNS_LOG, record)
    ua.send_watchdog_dm(dm_card(items, reports, commits=commits, notes=notes,
                                meta=invoker.last_call), as_html=True)
    print("done: " + "; ".join(f"{s}={r['outcome']}" for s, r in reports.items()))


def _ahead_of_upstream() -> bool:
    r = ua._git("rev-list", "--count", "@{u}..HEAD")
    return r.returncode == 0 and r.stdout.strip() not in ("", "0")


def agent_pass(args: argparse.Namespace) -> int:
    if os.environ.get("PICK_REVIEW_AGENT_DISABLED") == "1":
        with StateLock() as state:
            items, state["pending"] = list(state.get("pending") or []), []
        if items:
            ua.send_watchdog_dm(raw_card(items, "PICK_REVIEW_AGENT_DISABLED=1"), as_html=True)
        return 0
    AGENT_LOCK.parent.mkdir(parents=True, exist_ok=True)
    with AGENT_LOCK.open("w") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            print("an agent run holds the lock")
            return 0
        for _ in range(MAX_ROUNDS):
            busy = [p.name for p in OTHER_AGENT_LOCKS if lock_held(p)]
            if busy:
                print(f"another agent is running ({', '.join(busy)}) — next review pass retries")
                return 0
            now = datetime.now(timezone.utc)
            with StateLock() as state:
                pending = list(state.get("pending") or [])
                capped = daily_spawns(state, now) >= DAILY_CAP
                if capped and pending:
                    last = state.get("_cap_dm_at") or ""
                    if last < (now - timedelta(hours=24)).isoformat():
                        state["_cap_dm_at"] = now.isoformat(timespec="seconds")
                        state["pending"] = []
                        dm = pending
                    else:
                        dm = []
            if not pending:
                return 0
            if capped:
                if dm:
                    ua.send_watchdog_dm(raw_card(dm, f"daily cap {DAILY_CAP} agents/24h"),
                                        as_html=True)
                print(f"daily cap reached ({DAILY_CAP})")
                return 0
            batch = sorted(pending, key=lambda p: p["at"])[:MAX_MESSAGES]
            if args.dry_run:
                cache = load_json(CACHE_FILE)
                print(build_prompt(batch, cache, now_et=now.astimezone(ET).strftime(
                    "%Y-%m-%d %H:%M %Z"), head=ua.git_head()[:12]))
                return 0
            run_agent(batch, no_push=args.no_push)
    return 0


def main() -> int:
    p = argparse.ArgumentParser(description="Pick review: Claude reviews every pick")
    p.add_argument("--agent", action="store_true", help="run the agent over pending flags")
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--call", action="store_true", help="with --dry-run: ask Claude too")
    p.add_argument("--nightly-now", action="store_true", help="run yesterday's nightly batch now")
    p.add_argument("--no-push", action="store_true")
    p.add_argument("--rearm", metavar="KEY", help="forget reviews/attempts whose id contains KEY")
    args = p.parse_args()
    if os.environ.get("PICK_REVIEW_DISABLED") == "1":
        print("skip: PICK_REVIEW_DISABLED=1")
        return 0
    if args.rearm:
        with StateLock() as state:
            for bucket in ("reviewed", "attempts"):
                for k in [k for k in (state.get(bucket) or {}) if args.rearm in k]:
                    state[bucket].pop(k)
        print(f"re-armed {args.rearm}")
        return 0
    return agent_pass(args) if args.agent else review_pass(args)


if __name__ == "__main__":
    sys.exit(main())
