#!/usr/bin/env python3
"""Offline tests for the heartbeat's parked-service rule (deploy/hc_heartbeat.py):
a service stopped cleanly (inactive, Result=success) passes silently for
PARK_GRACE, then the heartbeat starts it itself and DMs; a failed start, a
second stop inside AUTOSTART_WINDOW, or a crashed unit fails the check. The
replay is 2026-10-04: an audit agent stopped grade-daemon 04:27:50→04:31:40 and
the 04:31 pass paged DOWN→UP. systemctl mocked; nothing pinged or started.

    python3 scripts/test_hc_heartbeat_parked.py
"""
import importlib.util
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "deploy"))
spec = importlib.util.spec_from_file_location("hc_heartbeat", ROOT / "deploy" / "hc_heartbeat.py")
hb = importlib.util.module_from_spec(spec)
spec.loader.exec_module(hb)

failures = []
UNIT = "grade-daemon.service"
T0 = 1_790_000_000.0  # the clean stop


def check(label, ok, detail=""):
    print(f"{'PASS' if ok else 'FAIL'}  {label}" + (f"  ({detail})" if detail and not ok else ""))
    if not ok:
        failures.append(label)


def run(now, *, active="inactive", result="success", start_ok=True, autostarts=None):
    """One check_services pass at `now`; returns (failing, notes, started, autostarts)."""
    started = []
    unit = {"FragmentPath": "/etc/systemd/system/" + UNIT, "Type": "notify",
            "ActiveState": active, "SubState": "dead" if active == "inactive" else "failed",
            "NRestarts": "0", "Result": result, "InactiveEnterTimestamp": f"@{int(T0)}"}
    hb.enabled_units = lambda kind: [UNIT]
    hb.show = lambda u, *p: unit
    hb.covered = lambda u: False
    hb.settle = lambda units: set(units) if unit["ActiveState"] != "active" else set()
    hb.stop_requester = lambda u: "systemctl stop grade-daemon < claude -p /investigate"

    def start(u):
        started.append(u)
        if start_ok:
            unit["ActiveState"] = "active"
        return start_ok
    hb.start_unit = start
    hb.time.time = lambda: now
    autostarts = {} if autostarts is None else autostarts
    notes = []
    failing, n = hb.check_services({}, autostarts, notes)
    return failing, notes, started, autostarts


# the incident: the pass ~4 min into the stop must not page
f, notes, started, _ = run(T0 + 230)
check("clean stop 4 min ago → parked, not failing", f == [] and not started and not notes, str(f))
f, notes, started, _ = run(T0 + hb.PARK_GRACE - 1)
check("just inside the grace → still parked", f == [] and not started)

# nobody came back: the heartbeat starts it and DMs
f, notes, started, auto = run(T0 + hb.PARK_GRACE + 60)
check("past the grace → auto-started", started == [UNIT] and f == [], str((f, started)))
check("auto-start DMs who stopped it", len(notes) == 1 and "🔧 started grade-daemon" in notes[0]
      and "claude -p /investigate" in notes[0], str(notes))
check("auto-start remembered", UNIT in auto)

# stopped again within the window → fail (hc-repair investigates), no second start
f, notes, started, _ = run(T0 + hb.PARK_GRACE + 60, autostarts={UNIT: T0 - 600})
check("second stop inside the window → failing, not restarted again",
      len(f) == 1 and "again" in f[0] and not started, str(f))
f, notes, started, _ = run(T0 + hb.PARK_GRACE + 60, autostarts={UNIT: T0 - hb.AUTOSTART_WINDOW - 1})
check("an old auto-start ages out → starts again", started == [UNIT] and f == [])

f, notes, started, _ = run(T0 + hb.PARK_GRACE + 60, start_ok=False)
check("auto-start fails → failing", len(f) == 1 and "auto-start failed" in f[0] and not notes, str(f))

# real failures still page at once
f, _, started, _ = run(T0 + 60, active="failed", result="exit-code")
check("crashed unit (failed) → failing immediately", f == [f"{UNIT}: failed/failed"] and not started, str(f))
f, _, started, _ = run(T0 + 60, result="watchdog")
check("inactive after a watchdog kill → failing immediately", len(f) == 1 and not started, str(f))

print(f"\n{'ALL PASS' if not failures else f'{len(failures)} FAILED'}")
sys.exit(1 if failures else 0)
