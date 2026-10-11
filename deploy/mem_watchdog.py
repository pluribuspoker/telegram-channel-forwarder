#!/usr/bin/env python3
"""
mem_watchdog.py — proactive VPS memory monitor.

Runs every ~10 min via the mem-watchdog systemd timer. Stays SILENT unless a
real problem trips a threshold, then DMs the operator through the watchdog bot
so they never have to log into the VPS to know memory is in trouble:

  🔴 OOM-kill  — the kernel killed a process (genuinely out of RAM). Act now;
                 a user-facing function may be down.
  🟡 Swap sat  — swap heavily used for a sustained window. Not an outage yet,
                 but the box is chronically over budget — plan the 2GB upgrade.

Design notes:
  * Reuses WATCHDOG_BOT_TOKEN / WATCHDOG_USER_ID from .env (same bot as
    claude_watchdog_bot.py). Stdlib only — no extra deps, tiny footprint.
  * State (swap history, last OOM scan time, alert debounce) persists to
    ~/.mem_watchdog_state.json, outside the repo.
  * OOM scan reads the kernel log via `sudo -n journalctl -k` since the last
    run, so each event is reported exactly once.

Usage:
  python deploy/mem_watchdog.py          # one monitoring cycle (timer calls this)
  python deploy/mem_watchdog.py --test   # send a liveness DM and exit
"""

import json
import os
import subprocess
import sys
import time
import urllib.parse
import urllib.request
from pathlib import Path

APP = Path(__file__).resolve().parent.parent
STATE = Path.home() / ".mem_watchdog_state.json"

# ── thresholds ──────────────────────────────────────────────────────────────
SWAP_ALERT_MB = 1024              # swap used above this ...
SWAP_SUSTAINED_SECS = 40 * 60     # ... continuously for this long -> 🟡 alert
SWAP_MIN_SAMPLES = 4              # and across at least this many samples
OOM_DEBOUNCE_SECS = 60 * 60       # at most one 🔴 alert per hour (legacy DM path)
SWAP_DEBOUNCE_SECS = 24 * 60 * 60  # at most one 🟡 alert per day (legacy DM path)
HISTORY_KEEP = 12                  # ~2h of 10-min samples
# Agent-first (2026-10-11): an OOM is an EVENT, not a state — keep the check
# failing this long so hc-repair's 10-min debounce always sees it and spawns
# the agent whose card is the operator's one DM.
OOM_FAIL_HOLD_SECS = 30 * 60


def load_env() -> None:
    # .env.local first so its values win (it holds the healthcheck URL).
    for name in (".env.local", ".env"):
        f = APP / name
        if not f.exists():
            continue
        for line in f.read_text().splitlines():
            m = line.strip()
            if m and not m.startswith("#") and "=" in m:
                k, v = m.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip().strip("'\""))


def meminfo() -> dict:
    d = {}
    for line in Path("/proc/meminfo").read_text().splitlines():
        parts = line.split()
        if len(parts) >= 2:
            d[parts[0].rstrip(":")] = int(parts[1])  # values in kB
    swap_total = d.get("SwapTotal", 0)
    swap_free = d.get("SwapFree", 0)
    return {
        "mem_total_mb": d.get("MemTotal", 0) // 1024,
        "mem_avail_mb": d.get("MemAvailable", 0) // 1024,
        "swap_total_mb": swap_total // 1024,
        "swap_used_mb": (swap_total - swap_free) // 1024,
    }


def load_state() -> dict:
    if STATE.exists():
        try:
            return json.loads(STATE.read_text())
        except Exception:
            pass
    return {}


def save_state(s: dict) -> None:
    tmp = STATE.with_suffix(".tmp")
    tmp.write_text(json.dumps(s))
    os.replace(tmp, STATE)


def send(text: str) -> bool:
    token = os.environ.get("WATCHDOG_BOT_TOKEN", "")
    uid = os.environ.get("WATCHDOG_USER_ID", "")
    if not token or not uid:
        print("WATCHDOG_BOT_TOKEN / WATCHDOG_USER_ID not set", file=sys.stderr)
        return False
    data = urllib.parse.urlencode({"chat_id": uid, "text": text}).encode()
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    try:
        with urllib.request.urlopen(urllib.request.Request(url, data=data), timeout=20) as r:
            return r.status == 200
    except Exception as e:
        print(f"send failed: {e}", file=sys.stderr)
        return False


def ping_check(suffix: str = "", body: str = "") -> bool:
    """Agent-first signal: the dedicated check carries the alarm to hc-repair
    (/fail = incident with the diagnosis as body; bare = clean run). URL unset
    (desktop clones) = False — the caller falls back to the direct DM."""
    url = os.environ.get("MEM_WATCHDOG_HEALTHCHECK_URL", "")
    if not url:
        return False
    try:
        req = urllib.request.Request(url + suffix, data=body.encode() if body else None)
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.status == 200
    except Exception as e:  # noqa: BLE001 - the check's own missed-ping alarm covers us
        print(f"ping failed: {e}", file=sys.stderr)
        return False


def scan_oom(since_epoch: float) -> list[str]:
    """Names of processes the kernel OOM-killed since since_epoch (one per kill)."""
    try:
        out = subprocess.run(
            ["sudo", "-n", "journalctl", "-k", "-S", f"@{int(since_epoch)}",
             "-o", "cat", "--no-pager"],
            capture_output=True, text=True, timeout=30,
        ).stdout
    except Exception as e:
        print(f"journalctl failed: {e}", file=sys.stderr)
        return []
    killed = []
    for line in out.splitlines():
        if "Out of memory: Killed process" in line:
            name = "process"
            if "(" in line and ")" in line:
                name = line[line.rfind("(") + 1:line.rfind(")")]
            killed.append(name)
    return killed


def main() -> None:
    load_env()
    now = time.time()

    if "--test" in sys.argv:
        mi = meminfo()
        ok = send(
            "🧪 mem_watchdog is live.\n"
            f"RAM: {mi['mem_avail_mb']}MB free of {mi['mem_total_mb']}MB · "
            f"swap: {mi['swap_used_mb']}/{mi['swap_total_mb']}MB used.\n"
            "You'll only hear from me again if the kernel OOM-kills something "
            "or swap stays saturated — otherwise I stay quiet."
        )
        print("test sent" if ok else "test FAILED")
        return

    st = load_state()
    mi = meminfo()
    last_alert = st.get("last_alert", {})

    # Per-unit RAM+swap history in the journal (`journalctl -u mem-watchdog`),
    # so an investigation can see WHICH service grew, not just that swap did.
    try:
        from stop_context import mem_line
        print(mem_line(), flush=True)
    except Exception as e:
        print(f"mem line failed: {e!r}", file=sys.stderr)

    # ── OOM check (each kernel OOM-kill recorded once) ──
    since = st.get("last_oom_check", now)  # first run: baseline to now, no backfill
    killed = scan_oom(since)
    st["last_oom_check"] = now
    if killed:
        st["oom_incident"] = {"at": now, "names": sorted(set(killed))}

    inc = st.get("oom_incident") or {}
    oom_active = bool(inc.get("names")) and now - float(inc.get("at") or 0) < OOM_FAIL_HOLD_SECS

    # ── sustained swap-pressure check ──
    hist = st.get("swap_history", [])
    hist.append([now, mi["swap_used_mb"]])
    hist = hist[-HISTORY_KEEP:]
    st["swap_history"] = hist
    window = [h for h in hist if now - h[0] <= SWAP_SUSTAINED_SECS + 900]
    sustained = (
        len(window) >= SWAP_MIN_SAMPLES
        and (now - window[0][0]) >= SWAP_SUSTAINED_SECS
        and all(v >= SWAP_ALERT_MB for _, v in window)
    )

    mem_summary = (f"RAM {mi['mem_avail_mb']}MB free of {mi['mem_total_mb']}MB, "
                   f"swap {mi['swap_used_mb']}/{mi['swap_total_mb']}MB used")

    if os.environ.get("MEM_WATCHDOG_HEALTHCHECK_URL", ""):
        # Agent-first: the check carries the alarm; hc-repair's agent + card
        # are the operator's one DM. No direct DMs, no DM debounce.
        alarms = []
        if oom_active:
            ago = int(now - float(inc.get("at") or now))
            alarms.append(f"kernel OOM-killed: {', '.join(inc['names'])} ({ago}s ago)")
        if sustained:
            alarms.append(f"swap sustained over {SWAP_ALERT_MB}MB for ~40min+")
        if alarms:
            ping_check("/fail", "\n".join(alarms) + "\n" + mem_summary)
        else:
            ping_check("", mem_summary)
    else:
        if killed and now - last_alert.get("oom", 0) >= OOM_DEBOUNCE_SECS:
            uniq = ", ".join(sorted(set(killed)))
            send(
                f"🔴 VPS OUT OF MEMORY — kernel OOM-killed: {uniq}\n"
                f"{mem_summary}.\n"
                "Something got terminated — if a user-facing function (Sauce, grading, "
                "this bot) is down, it likely needs a restart. If this recurs, upgrade "
                "to the 2GB droplet."
            )
            last_alert["oom"] = now
        if sustained and now - last_alert.get("swap", 0) >= SWAP_DEBOUNCE_SECS:
            send(
                f"🟡 VPS swap under sustained pressure — {mi['swap_used_mb']}MB of "
                f"{mi['swap_total_mb']}MB swap used for ~40min+, RAM only "
                f"{mi['mem_avail_mb']}MB free.\n"
                "Not an outage, but the box is consistently over its RAM budget. "
                "This is the signal to upgrade to the 2GB droplet."
            )
            last_alert["swap"] = now

    st["last_alert"] = last_alert
    save_state(st)


if __name__ == "__main__":
    main()
