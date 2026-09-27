#!/usr/bin/env python3
"""VPS heartbeat → healthchecks.io (hc-heartbeat.timer, every 5 min).

Baseline coverage for every systemd unit that has no dedicated health check,
DISCOVERED from /etc/systemd/system so a new service is covered the day it is
enabled (docs/vps.md "Healthchecks"):

  * VPS services (HEARTBEAT_SERVICES_HEALTHCHECK_URL): every enabled
    long-running service (Type != oneshot) must be active.
  * VPS jobs (HEARTBEAT_JOBS_HEALTHCHECK_URL): every enabled timer must be
    active and its unit's last run must have succeeded; every enabled .path
    unit must be active.

Units with their own check are skipped via COVERED — but only while that
check's URL is actually set, so an unset key never silently drops coverage.
Each pass pings success, or /fail with the failing units as the body. If the
VPS (or this timer) dies, both checks miss their pings and alert on their own.

A unit caught mid-transition (a restart is ~1 s of deactivating/activating)
is re-polled for SETTLE_SECONDS and fails only if it never comes back — one
sample landing inside an operator restart paged "DOWN" then "UP" 5 min later
(2026-09-26). So the grace can't hide a crash loop, a service whose systemd
NRestarts grew by FLAP_RESTARTS+ since the last pass fails as flapping.

A job that fails on and off would page DOWN/UP with every run, so jobs are
flap-damped (deploy/hc_flap.py): once a timer's unit has failed twice within
3 h, a good run keeps it listed ("flapping — …held DOWN until HH:MM") until an
hour passes with no new failure (a hold only keeps a unit this heartbeat
already listed — it never pages one). Failures are keyed by the failed run's
InactiveEnterTimestamp, so the four passes that see one failed run count it
once — and a job stays failing while its next run is still in progress (a
start resets Result to success; that alone used to flip the check UP).

healthchecks.io alerts only on a status CHANGE, so a second unit failing while
a group is already down would be masked — that one case is DMed through the
watchdog bot ("also failing now"). A first failure is the check's alert only.
State: logs/hc_heartbeat_state.json. `--dry-run` prints, pings nothing.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
import urllib.parse
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))  # hc_repair loads us by path
import hc_flap  # noqa: E402

APP = Path(__file__).resolve().parent.parent
UNIT_DIR = "/etc/systemd/system/"
STATE = APP / "logs" / "hc_heartbeat_state.json"

# unit -> env key of the dedicated check that covers it
COVERED = {
    "telegram-forwarder.service": "LISTENER_HEALTHCHECK_URL",
    "telegram-tracker.timer": "TRACKER_HEALTHCHECK_URL",
    "ungraded-audit.timer": "UNGRADED_AUDIT_HEALTHCHECK_URL",
    "god-judge.timer": "GOD_JUDGE_HEALTHCHECK_URL",
    "moe-grade.timer": "MOE_GRADE_HEALTHCHECK_URL",
    "pikkit-opinions.timer": "PIKKIT_OPINION_HEALTHCHECK_URL",
    "nfl-pikkit-snapshots.timer": "PIKKIT_SNAPSHOT_HEALTHCHECK_URL",
    "sauce-watch.timer": "SAUCE_WATCH_HEALTHCHECK_URL",
    "trent-monitor.timer": "TRENT_HEALTHCHECK_URL",
    "hc-heartbeat.timer": "HEARTBEAT_JOBS_HEALTHCHECK_URL",  # its own ping IS the check
}

SETTLE_SECONDS = 60   # restarts finish in seconds; a real outage outlasts this
SETTLE_POLL = 5
FLAP_RESTARTS = 3     # automatic restarts between two passes (5 min) = crash loop
KEEP_LAST_FAIL = 7 * 86400  # s a job's newest failure is remembered (weekly jobs)

GROUPS = {
    "services": ("HEARTBEAT_SERVICES_HEALTHCHECK_URL", "VPS services"),
    "jobs": ("HEARTBEAT_JOBS_HEALTHCHECK_URL", "VPS jobs"),
}


def load_env() -> None:
    for name in (".env", ".env.local"):  # .env.local wins, same as systemd
        f = APP / name
        if not f.exists():
            continue
        for line in f.read_text().splitlines():
            m = line.strip()
            if m and not m.startswith("#") and "=" in m:
                k, v = m.split("=", 1)
                os.environ[k.strip()] = v.strip().strip("'\"")


def enabled_units(kind: str) -> list[str]:
    out = subprocess.run(
        ["systemctl", "list-unit-files", f"--type={kind}", "--state=enabled",
         "--no-legend", "--plain"],
        capture_output=True, text=True, check=True).stdout
    return [line.split()[0] for line in out.splitlines() if line.strip()]


def show(unit: str, *props: str) -> dict[str, str]:
    # unix timestamps ("@1790507358") so the job failure times parse exactly
    out = subprocess.run(
        ["systemctl", "show", "--timestamp=unix", unit, *(f"-p{p}" for p in props)],
        capture_output=True, text=True).stdout
    return dict(line.split("=", 1) for line in out.splitlines() if "=" in line)


def _unix(value: str) -> float | None:
    try:
        return float(value.lstrip("@")) if value.startswith("@") else None
    except ValueError:
        return None


def covered(unit: str) -> bool:
    key = COVERED.get(unit)
    return bool(key and os.environ.get(key))


def settle(units: list[str]) -> set[str]:
    """Re-poll not-active units; return those still not active after SETTLE_SECONDS."""
    pending = set(units)
    deadline = time.monotonic() + SETTLE_SECONDS
    while pending and time.monotonic() < deadline:
        time.sleep(SETTLE_POLL)
        pending = {u for u in pending if show(u, "ActiveState").get("ActiveState") != "active"}
    return pending


def check_services(restarts: dict[str, int]) -> tuple[list[str], int]:
    """`restarts` (unit -> NRestarts at the last pass) is updated in place."""
    down, flapping, n = [], [], 0
    for unit in enabled_units("service"):
        p = show(unit, "FragmentPath", "Type", "ActiveState", "NRestarts")
        if not p.get("FragmentPath", "").startswith(UNIT_DIR):
            continue
        if p.get("Type") == "oneshot" or covered(unit):
            continue
        n += 1
        if p.get("ActiveState") != "active":
            down.append(unit)
        try:
            cur = int(p.get("NRestarts", "0"))
        except ValueError:
            cur = 0
        prev = restarts.get(unit)
        restarts[unit] = cur
        # NRestarts resets on a manual start, so a drop is never a flap
        if prev is not None and cur - prev >= FLAP_RESTARTS:
            flapping.append(f"{unit}: flapping ({cur - prev} automatic restarts in 5 min)")
    still_down = settle(down)
    failing = []
    for unit in sorted(still_down):
        p = show(unit, "ActiveState", "SubState")
        failing.append(f"{unit}: {p.get('ActiveState')}/{p.get('SubState')}")
    return failing + [f for f in flapping if f.split(":")[0] not in still_down], n


def check_jobs(job_fails: dict[str, list[float]],
               listed: set[str] = frozenset()) -> tuple[list[str], int]:
    """`job_fails` (service -> recent failed-run times) is updated in place;
    `listed` = units the previous pass reported — a hold only keeps those."""
    failing, held, down, n = [], [], [], 0
    now = time.time()
    for kind in ("timer", "path"):
        for unit in enabled_units(kind):
            p = show(unit, "FragmentPath", "ActiveState", "Unit")
            if not p.get("FragmentPath", "").startswith(UNIT_DIR) or covered(unit):
                continue
            n += 1
            if p.get("ActiveState") != "active":
                down.append(unit)
                continue
            if kind == "path":
                continue
            job = p.get("Unit", "")
            svc = show(job, "Result", "ExecMainStatus", "ActiveState",
                       "InactiveEnterTimestamp")
            # the failed run's end time: failure identity AND, while the next
            # run is in progress, proof that the last finished run failed
            ended = _unix(svc.get("InactiveEnterTimestamp", ""))
            fails = job_fails.get(job, [])
            if svc.get("Result", "success") != "success":
                at = ended or now
                if at not in fails:
                    fails.append(at)
                failing.append(f"{job}: last run {svc.get('Result')} "
                               f"(status {svc.get('ExecMainStatus')})")
            elif svc.get("ActiveState") not in ("inactive", "failed") and ended in fails:
                # a new run resets Result to success at START — that used to
                # read as recovered and flip the check UP before the run ended
                failing.append(f"{job}: last run failed at {hc_flap.clock(ended)}, re-running")
            elif job in listed and (reason := hc_flap.hold_reason(fails, now)):
                held.append(f"{job}: last run ok, {reason}")
            job_fails[job] = fails
    for job in list(job_fails):
        fails = sorted(job_fails[job])
        # past the flap window keep only the newest failure (a daily job's
        # next run must still read as re-running it), for a week at most
        job_fails[job] = hc_flap.recent(fails, now) or [
            t for t in fails[-1:] if now - t < KEEP_LAST_FAIL]
        if not job_fails[job]:
            del job_fails[job]
    for unit in sorted(settle(down)):
        failing.append(f"{unit}: {show(unit, 'ActiveState').get('ActiveState')} (not scheduled)")
    return failing + held, n


def ping(url: str, failing: list[str], n: int) -> None:
    body = ("\n".join(failing) if failing else f"all {n} ok").encode()
    target = url + ("/fail" if failing else "")
    for _ in range(3):
        try:
            with urllib.request.urlopen(
                    urllib.request.Request(target, data=body), timeout=10):
                return
        except Exception as exc:
            err = exc
    print(f"ping failed: {err}", file=sys.stderr)


def send_dm(text: str) -> None:
    token = os.environ.get("WATCHDOG_BOT_TOKEN", "")
    uid = os.environ.get("WATCHDOG_USER_ID", "")
    if not token or not uid:
        print("WATCHDOG_BOT_TOKEN / WATCHDOG_USER_ID not set", file=sys.stderr)
        return
    data = urllib.parse.urlencode({"chat_id": uid, "text": text}).encode()
    try:
        urllib.request.urlopen(urllib.request.Request(
            f"https://api.telegram.org/bot{token}/sendMessage", data=data),
            timeout=20).close()
    except Exception as exc:
        print(f"DM failed: {exc}", file=sys.stderr)


def main(argv: list[str]) -> int:
    dry = "--dry-run" in argv
    load_env()
    try:
        state = json.loads(STATE.read_text())
    except (OSError, ValueError):
        state = {}
    restarts = state.get("restarts", {})
    job_fails = state.get("job_fails", {})
    listed = {f.split(":")[0] for f in state.get("jobs", [])}
    results = {"services": check_services(restarts),
               "jobs": check_jobs(job_fails, listed)}
    for group, (failing, n) in results.items():
        key, label = GROUPS[group]
        print(f"{label}: {n} checked, {len(failing)} failing"
              + "".join(f"\n  ✗ {f}" for f in failing))
        if dry:
            continue
        prev = state.get(group, [])
        prev_units = {f.split(":")[0] for f in prev}
        new = [f for f in failing if f.split(":")[0] not in prev_units]
        if prev and new:
            send_dm(f"🔴 {label} — also failing now:\n" + "\n".join(new)
                    + "\n\n(healthchecks.io already shows this group down, "
                      "so it won't alert again)")
        url = os.environ.get(key)
        if url:
            ping(url, failing, n)
        else:
            print(f"{key} not set — no ping", file=sys.stderr)
        state[group] = failing
    if not dry:
        state["restarts"] = restarts
        state["job_fails"] = job_fails
        STATE.parent.mkdir(parents=True, exist_ok=True)
        STATE.write_text(json.dumps(state, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
