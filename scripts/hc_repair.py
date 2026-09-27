#!/usr/bin/env python3
"""Health-check auto-repair — one headless /investigate agent per real outage.

hc-repair.timer (every 5 min) polls the healthchecks.io API and, when a check
has been DOWN for HC_REPAIR_MIN_DOWN_MINUTES (10), spawns ONE headless
`claude -p "/investigate …"` agent (Opus 5.5 high, subscription-billed) that
root-causes and fixes it, then DMs the operator a card via the watchdog bot.
Chassis = scripts/trent_repair.py (invoker, git, DM, ledger from the ungraded
audit); every guard lives HERE so a manual run behaves like the timer.

Targets: one per down check — except the heartbeat groups (VPS services /
VPS jobs), which fan out to one target per failing UNIT parsed from the
latest /fail body, so a second unit failing inside an already-down group
still gets its own agent. Every eligible target of one pass goes to the SAME
agent (outages cluster; two agents would fight over one root cause).

The 10-min debounce is the second false-positive layer (the first is each
check's own grace + the heartbeat's settle window): a flap never reaches an
agent. Checks with their own repair rung are excluded (HC_REPAIR_EXCLUDE,
default the Trent pair — trent-repair already owns that outage).

Guards (state logs/hc_repair_state.json, per target):
- kill switch HC_REPAIR_DISABLED=1 → exit 0 silently
- flock logs/.hc_repair.lock — one agent at a time (a pass during a run skips)
- cooldown HC_REPAIR_COOLDOWN_HOURS (6) per target between spawns
- HC_REPAIR_ATTEMPT_CAP (2) agent runs per outage streak → parked with ONE ⚠️
  DM; the target coming back up resets the streak (cooldown kept); --rearm
- needs_human parks immediately
- HC_REPAIR_DAILY_CAP (6) agent spawns per rolling 24h across all targets

The agent never restarts telegram-forwarder or claude-channels, never runs a
job whose run posts/spends/spawns agents just to turn a check green, and
never fakes a ping. The runner pushes its `hc-repair:` commits once.
Exit 0 unless the runner itself crashes — an agent failure is reported on the
card, not via the exit code (the heartbeat watches this unit's result, and a
failed agent must not page as a second outage).

Auditable: logs/hc_repair_runs.jsonl + logs/hc_repair/<stamp>/ transcripts.
Manual: --dry-run (targets + prompt; spawns/writes nothing), --force (bypass
cooldown + debounce, not the park), --rearm [KEY] (unpark one/all), --no-push.
"""

import argparse
import fcntl
import html
import json
import os
import re
import subprocess
import sys
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scripts import ungraded_audit as ua  # noqa: E402  (loads .env/.env.local)

ROOT = ua.ROOT
STATE_FILE = ROOT / "logs" / "hc_repair_state.json"
LOCK_FILE = ROOT / "logs" / ".hc_repair.lock"
RUNS_LOG = ROOT / "logs" / "hc_repair_runs.jsonl"
OUT_DIR = ROOT / "logs" / "hc_repair"
API = "https://healthchecks.io/api/v3"

REPAIR_MODEL = os.environ.get("HC_REPAIR_MODEL") or "claude-opus-5-5"
REPAIR_EFFORT = os.environ.get("HC_REPAIR_EFFORT") or "high"
AGENT_TIMEOUT = int(os.environ.get("HC_REPAIR_AGENT_TIMEOUT") or 1500)
COOLDOWN_HOURS = float(os.environ.get("HC_REPAIR_COOLDOWN_HOURS") or 6)
ATTEMPT_CAP = int(os.environ.get("HC_REPAIR_ATTEMPT_CAP") or 2)
DAILY_CAP = int(os.environ.get("HC_REPAIR_DAILY_CAP") or 6)
MIN_DOWN = timedelta(minutes=float(os.environ.get("HC_REPAIR_MIN_DOWN_MINUTES") or 10))
EXCLUDE = {n.strip() for n in (os.environ.get("HC_REPAIR_EXCLUDE")
                               or "Trent monitor,Trent repair").split(",") if n.strip()}

# Checks whose /fail body lists failing units one per line ("unit: detail").
HEARTBEAT_KEYS = ("HEARTBEAT_SERVICES_HEALTHCHECK_URL", "HEARTBEAT_JOBS_HEALTHCHECK_URL")

# env key -> where the pinged job lives, for checks NOT in the heartbeat's
# COVERED unit map (cron jobs have no unit; hc_run.sh wraps them).
KEY_HINTS = {
    "SAUCE_DAILY_HEALTHCHECK_URL": "forwarder cron 06:00 ET → run_sauce_daily.sh "
                                   "(log /tmp/sauce_daily_cron.log, docs/sauce.md)",
    "ROOT_BACKUP_HEALTHCHECK_URL": "root cron 06:00 → /root/backup.sh wrapped by "
                                   "deploy/hc_run.sh (sudo -n crontab -l -u root)",
    "TRENT_REPAIR_HEALTHCHECK_URL": "trent-repair.service (scripts/trent_repair.py)",
}

OUTCOMES = (
    "fixed_verified",      # fixed AND the job/service demonstrably works now
    "fixed_needs_verify",  # fixed; proof comes with the job's next real run
    "false_alarm",         # the check misfired (grace/schedule) — check tuned
    "transient_no_change", # a blip that has already healed; nothing to change
    "needs_human",         # secrets, paid API, product decision — parked
    "no_issue",            # target already healthy when the agent looked
)
PARK_OUTCOMES = ("needs_human",)

BADGE = {
    "fixed_verified": ("✅", "fixed + verified"),
    "fixed_needs_verify": ("🔧", "fixed — verify on next run"),
    "false_alarm": ("🙈", "false alarm — check tuned"),
    "transient_no_change": ("👌", "transient, no change"),
    "needs_human": ("🙋", "NEEDS HUMAN"),
    "no_issue": ("👌", "already healthy"),
    "unparsed": ("⚠️", "ran, report unparsed"),
    "error": ("❌", "agent failed"),
    "timeout": ("⏱", "agent timed out"),
}


class RepairInvoker(ua.HeadlessInvoker):
    """The audit's invoker (OAuth-only env, hook standdown, strict MCP,
    stream-json transcript, killpg on timeout) with our model/effort."""

    def command(self, prompt: str) -> list[str]:
        cmd = super().command(prompt)
        cmd[cmd.index("--model") + 1] = REPAIR_MODEL
        cmd[cmd.index("--effort") + 1] = REPAIR_EFFORT
        return cmd


# ─── pure logic (tested offline) ─────────────────────────────────────────────

def _ts(value) -> datetime | None:
    try:
        return datetime.fromisoformat(str(value))
    except (TypeError, ValueError):
        return None


def down_since(flips: list[dict]) -> datetime | None:
    """Timestamp of the newest up→down flip (the API lists newest first, but
    don't rely on it)."""
    downs = [_ts(f.get("timestamp")) for f in flips if not f.get("up")]
    downs = [d for d in downs if d]
    return max(downs) if downs else None


def failing_units(body: str) -> list[str]:
    """Units named by a heartbeat /fail body (`unit: detail` per line)."""
    units = []
    for line in (body or "").splitlines():
        head = line.split(":", 1)[0].strip()
        if re.fullmatch(r"[\w@.\-]+\.(service|timer|path)", head) and head not in units:
            units.append(head)
    return units


def env_key_for(check: dict, env: dict[str, str]) -> str:
    uuid = check.get("uuid") or ""
    for key, value in sorted(env.items()):
        if key.endswith("_HEALTHCHECK_URL") and uuid and uuid in value:
            return key
    return ""


def build_targets(checks: list[dict], *, env: dict[str, str],
                  details: dict[str, dict], covered: dict[str, str]) -> list[dict]:
    """Down checks → repair targets. `details[uuid]` = {"since", "body", "pings"}
    fetched for down checks only; `covered` = hc_heartbeat.COVERED."""
    unit_for_key = {k: u for u, k in covered.items()}
    targets = []
    for c in checks:
        if c.get("status") != "down" or c.get("name") in EXCLUDE:
            continue
        uuid = c.get("uuid") or ""
        d = details.get(uuid, {})
        key = env_key_for(c, env)
        base = {"check": c.get("name", ""), "uuid": uuid, "env_key": key,
                "desc": c.get("desc", ""), "since": d.get("since"),
                "body": d.get("body", ""), "pings": d.get("pings", [])}
        if key in HEARTBEAT_KEYS:
            units = failing_units(d.get("body", ""))
            if units:
                for unit in units:
                    targets.append({**base, "key": f"{uuid}:{unit}", "unit": unit,
                                    "hint": ""})
                continue
            # Missed pings (no /fail body): the VPS or the heartbeat itself.
            targets.append({**base, "key": uuid, "unit": "hc-heartbeat.timer",
                            "hint": "no failing units listed — the heartbeat itself stopped pinging"})
            continue
        targets.append({**base, "key": uuid, "unit": unit_for_key.get(key, ""),
                        "hint": KEY_HINTS.get(key, "")})
    return targets


def observe(state: dict, targets: list[dict], now: datetime) -> None:
    """Stamp first_seen on new targets; a target no longer down ends its
    streak (attempts/park reset, cooldown kept); entries with nothing left
    to remember are dropped."""
    live = {t["key"] for t in targets}
    for t in targets:
        entry = state.setdefault(t["key"], {})
        entry["check"] = t["check"]
        if t.get("unit"):
            entry["unit"] = t["unit"]
        entry.setdefault("first_seen", now.isoformat(timespec="seconds"))
    for key in list(state):
        if key.startswith("_") or key in live:
            continue
        entry = state[key]
        for f in ("first_seen", "attempts", "parked", "parked_reason", "capped_dm_sent"):
            entry.pop(f, None)
        last = _ts(entry.get("last_spawn_at"))
        if not last or now - last > timedelta(hours=COOLDOWN_HOURS):
            del state[key]


def gate(entry: dict, target: dict, now: datetime, *, force: bool = False) -> tuple[str, str]:
    """(action, reason): action "run" | "skip" | "capped"."""
    if entry.get("parked"):
        if entry.get("capped_dm_sent"):
            return "skip", f"parked ({entry.get('parked_reason')})"
        return "capped", str(entry.get("parked_reason") or "parked")
    if not force:
        first, flip = _ts(entry.get("first_seen")), _ts(target.get("since"))
        # Unit targets: when THIS unit first showed; whole checks: the flip.
        if ":" in target["key"]:
            start = first or flip
        else:
            start = min([s for s in (first, flip) if s], default=None)
        if start and now - start < MIN_DOWN:
            return "skip", f"debounce (down {now - start} < {MIN_DOWN})"
        last = _ts(entry.get("last_spawn_at"))
        if last and now - last < timedelta(hours=COOLDOWN_HOURS):
            return "skip", f"cooldown ({now - last} < {COOLDOWN_HOURS}h)"
    return "run", "ok"


def daily_spawns(state: dict, now: datetime) -> int:
    recent = [s for s in state.get("_spawns", [])
              if (t := _ts(s)) and now - t < timedelta(hours=24)]
    state["_spawns"] = recent
    return len(recent)


def record_spawn(state: dict, keys: list[str], now: datetime) -> None:
    stamp = now.isoformat(timespec="seconds")
    state.setdefault("_spawns", []).append(stamp)
    for key in keys:
        entry = state.setdefault(key, {})
        entry["last_spawn_at"] = stamp
        entry["attempts"] = int(entry.get("attempts") or 0) + 1


def settle(entry: dict, outcome: str) -> None:
    entry["last_outcome"] = outcome
    if outcome in PARK_OUTCOMES:
        entry.update(parked=True, parked_reason=outcome, capped_dm_sent=False)
    elif int(entry.get("attempts") or 0) >= ATTEMPT_CAP and outcome not in (
            "fixed_verified", "no_issue"):
        entry.update(parked=True, parked_reason=f"attempt cap ({entry['attempts']})",
                     capped_dm_sent=False)


_RESULT_RE = re.compile(r"HC_REPAIR_RESULT:\s*(\[.*?\])\s*$", re.MULTILINE | re.DOTALL)


def parse_results(result_text: str, targets: list[dict]) -> dict[str, dict]:
    """key -> {outcome, issue, action}. The agent reports per target label
    (`check` or `check / unit`); a target it didn't report is `unparsed`."""
    reports: dict[str, dict] = {}
    for raw in reversed(_RESULT_RE.findall(result_text or "")):
        try:
            items = json.loads(raw)
        except json.JSONDecodeError:
            continue
        if isinstance(items, list):
            for it in items:
                if isinstance(it, dict) and it.get("outcome") in OUTCOMES:
                    reports.setdefault(str(it.get("target") or ""), it)
            break
    tail = re.sub(r"\s+", " ", (result_text or "").strip())[-200:]
    out = {}
    for t in targets:
        it = reports.get(label(t))
        if it is None and len(targets) == 1 and len(reports) == 1:
            it = next(iter(reports.values()))
        if it is None:
            out[t["key"]] = {"outcome": "unparsed", "issue": tail, "action": ""}
        else:
            out[t["key"]] = {"outcome": it["outcome"],
                             "issue": str(it.get("issue") or "").strip()[:400],
                             "action": str(it.get("action") or "").strip()[:400]}
    return out


def label(t: dict) -> str:
    return f"{t['check']} / {t['unit']}" if ":" in t["key"] else t["check"]


def build_prompt(targets: list[dict], *, now_et: str, head: str,
                 journals: dict[str, str]) -> str:
    lines = [
        f"/investigate HEALTH-CHECK AUTO-REPAIR {now_et}: healthchecks.io reports "
        f"{len(targets)} target(s) DOWN for 10+ minutes (the operator was already "
        "paged). Find the root cause of each and fix it; report precisely if a "
        "fix needs a human. Outages cluster — look for one shared cause first.",
        "",
        "## Down targets",
    ]
    for t in targets:
        lines.append(f"### {label(t)}")
        lines.append(f"- check: {t['check']} (uuid {t['uuid']}; env key "
                     f"{t['env_key'] or 'unknown'}) — {t['desc'] or 'no description'}")
        lines.append(f"- down since: {t.get('since') or 'unknown'}")
        if t.get("unit"):
            lines.append(f"- unit: {t['unit']}")
        if t.get("hint"):
            lines.append(f"- where it runs: {t['hint']}")
        if t.get("pings"):
            lines.append("- recent pings (newest first): " + "; ".join(
                f"{p.get('type')} {p.get('date', '')[:19]}" for p in t["pings"][:6]))
        if t.get("body"):
            lines += ["- latest failure body:", "```", t["body"].strip()[-1500:], "```"]
        j = journals.get(t.get("unit") or "")
        if j:
            lines += [f"- journal tail ({t['unit']}):", "```", j.strip(), "```"]
        lines.append("")
    lines += [
        f"- repo HEAD at spawn: {head}",
        "",
        "## Constraints — these OVERRIDE the standard /investigate workflow where they conflict",
        "- You are a headless auto-repair agent on the VPS (as forwarder, in "
        "/home/forwarder/app, passwordless `sudo -n`); no human is available. "
        "Work directly in this repo — NO git worktree, NO SSH.",
        "- Read CLAUDE.md's Health checks rules and docs/vps.md (Healthchecks "
        "section) first, then the failing job's own doc/runner. Follow "
        "/investigate's service/job-failure workflow.",
        "- NEVER `git push` (the runner pushes once after you). NEVER restart/"
        "stop telegram-forwarder (Telegram flood waits) or claude-channels "
        "(the operator's live session) or hc-repair — if one of those needs a "
        "restart, report needs_human with the exact command. Restarting any "
        "OTHER unit you fixed is fine; `sudo -n systemctl reset-failed <unit>` "
        "is fine after a verified fix.",
        "- Verify with the real entry point. You MAY start a job manually to "
        "prove a fix only if its run is side-effect-safe; NEVER re-run a job "
        "that posts to Telegram channels/DMs, spawns agents, or spends paid "
        "API credit just to turn a check green (ungraded-audit, god-judge, "
        "sauce-daily, moe-grade --notify, trent-monitor, pikkit fetches — "
        "their bounds exist because runs got sessions revoked). Report "
        "fixed_needs_verify instead; the next real run pings the check.",
        "- NEVER send a fake success ping and never pause/delete a check or "
        "change its alert channels. If the check itself misfired (grace or "
        "schedule tighter than the job's real run times), you may tune "
        "grace/schedule through the API (`HEALTHCHECKS_API_KEY` in .env.local, "
        "POST " + API + "/checks/<uuid>) with the run-time evidence — outcome "
        "false_alarm. A heartbeat false positive is a bug in "
        "deploy/hc_heartbeat.py — fix it there.",
        "- Never hand-edit .env/.env.local (scripts/set_env_local.py is the only "
        "writer). Secrets you can't obtain (dead cookies/tokens, Turnstile "
        "logins) → needs_human with exactly what the operator must do.",
        "- Commit any fix locally: stage ONLY files you changed (never `git add "
        "-A`; the tree holds unrelated WIP), message prefixed `hc-repair:`. "
        "Unit/hook changes: edit deploy/ first, then install (CLAUDE.md Infra "
        "sync). Run the pinned tests you touched. Free sources only.",
        "- Budget ~20 minutes; the runner kills you at 25. Do not message the "
        "operator; the runner sends the card. Add an /investigate lesson ONLY "
        "for a novel debugging technique.",
        "",
        "## Result contract (the runner parses this for the operator's DM)",
        "End your FINAL message with exactly one line listing EVERY target above:",
        'HC_REPAIR_RESULT: [{"target": "<target heading exactly>", "outcome": '
        '"<fixed_verified|fixed_needs_verify|false_alarm|transient_no_change|'
        'needs_human|no_issue>", "issue": "<root cause, one sentence>", '
        '"action": "<what you changed / what the operator must do, one sentence>"}]',
    ]
    return "\n".join(lines)


def dm_card(targets: list[dict], reports: dict[str, dict], *, commits: list[str],
            status_after: dict[str, str], meta: dict, pushed: bool | None) -> str:
    esc = html.escape
    blocks = []
    for t in targets:
        r = reports.get(t["key"], {})
        emoji, lbl = BADGE.get(r.get("outcome", ""), ("❓", r.get("outcome", "?")))
        inner = []
        if r.get("issue"):
            inner.append(f"<b>Issue:</b> {esc(r['issue'])}")
        if r.get("action"):
            inner.append(f"<b>Action:</b> {esc(r['action'])}")
        inner.append(f"<b>Check now:</b> {esc(status_after.get(t['uuid'], '?'))}")
        blocks.append(f"{emoji} <b>{esc(label(t))}</b> — {esc(lbl)}\n"
                      f"<blockquote expandable>{chr(10).join(inner)}</blockquote>")
    foot = ["<b>Commits:</b> " + (esc("; ".join(commits)) if commits else "none")]
    if pushed is False:
        foot.append("⚠️ PUSH FAILED")
    bits = [f"{REPAIR_MODEL}/{REPAIR_EFFORT}"]
    if meta.get("wall_ms"):
        bits.append(f"{int(meta['wall_ms'] / 1000)}s")
    if meta.get("num_turns"):
        bits.append(f"{meta['num_turns']} turns")
    foot.append(esc(" · ".join(str(b) for b in bits)))
    return "🛠 <b>Health-check auto-repair</b>\n" + "\n".join(blocks) + "\n" + "\n".join(foot)


# ─── I/O ─────────────────────────────────────────────────────────────────────

def api_get(path: str, *, raw: bool = False):
    req = urllib.request.Request(API + path, headers={
        "X-Api-Key": os.environ.get("HEALTHCHECKS_API_KEY", "")})
    with urllib.request.urlopen(req, timeout=20) as resp:
        data = resp.read().decode("utf-8", "replace")
    return data if raw else json.loads(data)


def fetch_details(check: dict) -> dict:
    uuid = check["uuid"]
    out: dict = {"since": None, "body": "", "pings": []}
    try:
        since = down_since(api_get(f"/checks/{uuid}/flips/").get("flips", []))
        out["since"] = since.isoformat() if since else None
    except Exception as exc:
        print(f"flips {check.get('name')}: {exc}", file=sys.stderr)
    try:
        pings = api_get(f"/checks/{uuid}/pings/").get("pings", [])
        out["pings"] = [{"type": p.get("type"), "date": p.get("date")} for p in pings[:10]]
        fail = next((p for p in pings if p.get("type") == "fail"), None)
        if fail and pings and pings[0].get("n") == fail.get("n"):
            out["body"] = api_get(f"/checks/{uuid}/pings/{fail['n']}/body", raw=True)
    except Exception as exc:
        print(f"pings {check.get('name')}: {exc}", file=sys.stderr)
    return out


def journal_tail(unit: str) -> str:
    if not unit:
        return ""
    svc = re.sub(r"\.(timer|path)$", ".service", unit)
    try:
        return subprocess.run(
            ["journalctl", "-u", svc, "-n", "40", "--no-pager", "-o", "short-iso"],
            capture_output=True, text=True, timeout=30).stdout[-3500:]
    except Exception:
        return ""


def heartbeat_covered() -> dict[str, str]:
    import importlib.util
    spec = importlib.util.spec_from_file_location("hc_heartbeat", ROOT / "deploy" / "hc_heartbeat.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return dict(mod.COVERED)


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


# ─── main ────────────────────────────────────────────────────────────────────

def main() -> int:
    parser = argparse.ArgumentParser(description="Health-check auto-repair agent")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--force", action="store_true",
                        help="bypass debounce + cooldown (not the park or daily cap)")
    parser.add_argument("--rearm", nargs="?", const="*", metavar="KEY",
                        help="unpark one target key (default: all), then exit")
    parser.add_argument("--no-push", action="store_true")
    args = parser.parse_args()

    if os.environ.get("HC_REPAIR_DISABLED") == "1":
        print("skip: HC_REPAIR_DISABLED=1")
        return 0
    if not os.environ.get("HEALTHCHECKS_API_KEY"):
        print("HEALTHCHECKS_API_KEY not set", file=sys.stderr)
        return 1

    now = datetime.now(timezone.utc)
    state = load_state()

    if args.rearm:
        for key, entry in state.items():
            if not key.startswith("_") and args.rearm in ("*", key):
                for f in ("attempts", "parked", "parked_reason", "capped_dm_sent"):
                    entry.pop(f, None)
        save_state(state)
        print(f"re-armed {args.rearm}")
        return 0

    checks = api_get("/checks/").get("checks", [])
    details = {c["uuid"]: fetch_details(c) for c in checks
               if c.get("status") == "down" and c.get("name") not in EXCLUDE}
    targets = build_targets(checks, env=dict(os.environ), details=details,
                            covered=heartbeat_covered())
    if not args.dry_run:
        observe(state, targets, now)

    eligible, capped = [], []
    for t in targets:
        action, reason = gate(state.get(t["key"], {}), t, now, force=args.force)
        print(f"{label(t)}: {action} ({reason})")
        if action == "run":
            eligible.append(t)
        elif action == "capped":
            capped.append((t, reason))
    if not targets:
        print("all checks up")

    for t, reason in capped:
        if args.dry_run:
            continue
        if ua.send_watchdog_dm(
            f"⚠️ Health-check auto-repair PARKED for {label(t)} ({reason}) — still "
            f"down and needs a human. After fixing: ~/venv/bin/python "
            f"scripts/hc_repair.py --rearm '{t['key']}'"
        ):
            state[t["key"]]["capped_dm_sent"] = True

    spawned = daily_spawns(state, now)
    if eligible and spawned >= DAILY_CAP:
        print(f"daily cap reached ({spawned}/{DAILY_CAP} in 24h) — not spawning")
        last_dm = _ts(state.get("_cap_dm_at"))
        if not last_dm or now - last_dm > timedelta(hours=24):
            if not args.dry_run and ua.send_watchdog_dm(
                    f"⚠️ Health-check auto-repair hit its daily cap ({DAILY_CAP} "
                    f"agents/24h) — {', '.join(label(t) for t in eligible)} left for you."):
                state["_cap_dm_at"] = now.isoformat(timespec="seconds")
        eligible = []

    if args.dry_run:
        if eligible:
            print("---- prompt ----")
            print(build_prompt(eligible, now_et=now.astimezone().strftime("%Y-%m-%d %H:%M %Z"),
                               head=ua.git_head()[:12],
                               journals={t["unit"]: journal_tail(t["unit"]) for t in eligible}))
        return 0
    if not eligible:
        save_state(state)
        return 0

    LOCK_FILE.parent.mkdir(parents=True, exist_ok=True)
    with LOCK_FILE.open("w") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            print("an agent run holds the lock; next pass")
            save_state(state)
            return 0

        journals = {t["unit"]: journal_tail(t["unit"]) for t in eligible}
        prompt = build_prompt(eligible, now_et=now.astimezone().strftime("%Y-%m-%d %H:%M %Z"),
                              head=ua.git_head()[:12], journals=journals)
        record_spawn(state, [t["key"] for t in eligible], now)
        save_state(state)

        run_dir = OUT_DIR / now.strftime("%Y%m%d-%H%M%S")
        record: dict = {"ts": now.isoformat(timespec="seconds"),
                        "targets": [label(t) for t in eligible]}
        head0, dirty0 = ua.git_head(), ua.git_dirty_paths()
        invoker = RepairInvoker(
            os.environ.get("HC_REPAIR_CLAUDE_BIN") or ua.DEFAULT_CLAUDE_BIN,
            oauth_token=os.environ.get("CLAUDE_CODE_OAUTH_TOKEN", ""),
            timeout=AGENT_TIMEOUT,
        )
        try:
            result_text = invoker(prompt, run_dir / "agent.jsonl")
            (run_dir / "result.md").write_text(result_text, encoding="utf-8")
            reports = parse_results(result_text, eligible)
        except ua.AgentCallError as exc:
            outcome = "timeout" if "timed out" in str(exc) else "error"
            reports = {t["key"]: {"outcome": outcome, "issue": str(exc)[:400], "action": ""}
                       for t in eligible}
        record["reports"] = {label(t): reports[t["key"]] for t in eligible}
        record.update(invoker.last_call)

        commits = ua.git_commits_between(head0, ua.git_head())
        record["commits"] = commits
        leftover = ua.git_dirty_paths() - dirty0
        if leftover and all(r["outcome"] in ("error", "timeout", "unparsed")
                            for r in reports.values()):
            record["reverted"] = ua.git_revert_paths(leftover)

        pushed = None
        if commits and not args.no_push:
            pushed = ua._git("push").returncode == 0
            record["pushed"] = pushed

        status_after = {}
        try:
            status_after = {c["uuid"]: c.get("status", "?")
                            for c in api_get("/checks/").get("checks", [])}
        except Exception as exc:
            print(f"post-check fetch failed: {exc}", file=sys.stderr)
        record["status_after"] = {label(t): status_after.get(t["uuid"]) for t in eligible}

        state = load_state()  # a pass may have run while the agent worked
        for t in eligible:
            settle(state.setdefault(t["key"], {}), reports[t["key"]]["outcome"])
        save_state(state)
        ua.append_runs_log(RUNS_LOG, record)
        ua.send_watchdog_dm(dm_card(eligible, reports, commits=commits,
                                    status_after=status_after, meta=invoker.last_call,
                                    pushed=pushed), as_html=True)
        print("done: " + "; ".join(f"{label(t)}={reports[t['key']]['outcome']}"
                                   for t in eligible))
    return 0


if __name__ == "__main__":
    sys.exit(main())
