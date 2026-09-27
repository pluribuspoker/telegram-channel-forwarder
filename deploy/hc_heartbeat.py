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
import urllib.parse
import urllib.request
from pathlib import Path

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
    out = subprocess.run(
        ["systemctl", "show", unit, *(f"-p{p}" for p in props)],
        capture_output=True, text=True).stdout
    return dict(line.split("=", 1) for line in out.splitlines() if "=" in line)


def covered(unit: str) -> bool:
    key = COVERED.get(unit)
    return bool(key and os.environ.get(key))


def check_services() -> tuple[list[str], int]:
    failing, n = [], 0
    for unit in enabled_units("service"):
        p = show(unit, "FragmentPath", "Type", "ActiveState", "SubState")
        if not p.get("FragmentPath", "").startswith(UNIT_DIR):
            continue
        if p.get("Type") == "oneshot" or covered(unit):
            continue
        n += 1
        if p.get("ActiveState") != "active":
            failing.append(f"{unit}: {p.get('ActiveState')}/{p.get('SubState')}")
    return failing, n


def check_jobs() -> tuple[list[str], int]:
    failing, n = [], 0
    for kind in ("timer", "path"):
        for unit in enabled_units(kind):
            p = show(unit, "FragmentPath", "ActiveState", "Unit")
            if not p.get("FragmentPath", "").startswith(UNIT_DIR) or covered(unit):
                continue
            n += 1
            if p.get("ActiveState") != "active":
                failing.append(f"{unit}: {p.get('ActiveState')} (not scheduled)")
                continue
            if kind == "path":
                continue
            svc = show(p.get("Unit", ""), "Result", "ExecMainStatus")
            if svc.get("Result", "success") != "success":
                failing.append(f"{p.get('Unit')}: last run {svc.get('Result')} "
                               f"(status {svc.get('ExecMainStatus')})")
    return failing, n


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
    results = {"services": check_services(), "jobs": check_jobs()}
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
        STATE.parent.mkdir(parents=True, exist_ok=True)
        STATE.write_text(json.dumps(state, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
