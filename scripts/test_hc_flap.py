#!/usr/bin/env python3
"""Offline tests for health-check flap damping (deploy/hc_flap.py) and its two
users: the heartbeat's job check (deploy/hc_heartbeat.py, systemctl mocked)
and the job runners' ping wiring (run_*.sh executed against a stub curl and a
stub job in a temp app dir). The heartbeat replay is the 2026-09-27 morning of
nfl-lines-fetcher, where the NFL Guesser sheet stalled reads ~30 min of every
hour. No network, no systemctl, nothing pinged.

    python3 scripts/test_hc_flap.py
"""
import importlib.util
import os
import shutil
import subprocess
import sys
import tempfile
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "deploy"))

import hc_flap  # noqa: E402

spec = importlib.util.spec_from_file_location("hc_heartbeat", ROOT / "deploy" / "hc_heartbeat.py")
hb = importlib.util.module_from_spec(spec)
spec.loader.exec_module(hb)
sys.path.insert(0, str(ROOT))
from scripts import hc_repair as hr  # noqa: E402

failures = []


def check(label: str, ok: bool, detail: str = ""):
    print(f"{'PASS' if ok else 'FAIL'}  {label}" + (f"  ({detail})" if detail and not ok else ""))
    if not ok:
        failures.append(label)


def ts(hm: str) -> float:
    return datetime.strptime(f"2026-09-27 {hm}", "%Y-%m-%d %H:%M:%S").timestamp()


M = 60

# 1. The rule.
t0 = ts("05:00:00")
check("no failures → not held", hc_flap.hold_reason([], t0) == "")
check("a lone failure clears on the next good run", hc_flap.hold_reason([t0 - 20 * M], t0) == "")
held = hc_flap.hold_reason([t0 - 50 * M, t0 - 20 * M], t0)
check("2 failures in 3h → held", held.startswith("flapping — 2 failed runs in 3h"), held)
check("hold names the clear time (last failure + 1h)", "held DOWN until 05:40" in held, held)
check("hold ends 60 min after the last failure",
      hc_flap.hold_reason([t0 - 90 * M, t0 - 60 * M], t0) == "")
check("failures older than 3h don't count",
      hc_flap.hold_reason([t0 - 200 * M, t0 - 10 * M], t0) == "")
check("recent() prunes and sorts", hc_flap.recent([t0 - 10, t0 - 4 * 3600, t0 - 20], t0)
      == [t0 - 20, t0 - 10])

# 2. Runner state machine.
st, out = hc_flap.step({}, "start", t0)
st, out = hc_flap.step(st, "ok", t0 + 60)
check("clean run → UP", out == "" and st["running_since"] is None and st["fails"] == [])
st, _ = hc_flap.step(st, "start", t0 + 900)
st, out = hc_flap.step(st, "fail", t0 + 960)
st, _ = hc_flap.step(st, "start", t0 + 1800)
st, out = hc_flap.step(st, "ok", t0 + 1860)
check("one failure then a good run → UP", out == "", out)
st, _ = hc_flap.step(st, "start", t0 + 2700)          # this run gets killed…
st, out = hc_flap.step(st, "start", t0 + 3600)        # …noticed at the next start
check("a run that never reported counts as a failure", len(st["fails"]) == 2 and "never reported" in out)
st, out = hc_flap.step(st, "ok", t0 + 3660)
check("second failure → next good run held DOWN", out.startswith("flapping"), out)
st, out = hc_flap.step(st, "ok", t0 + 3600 + 3601)
check("an hour clean → UP again", out == "", out)

# 3. CLI (temp state dir; bad names refused).
with tempfile.TemporaryDirectory() as tmp:
    real = hc_flap.STATE_DIR
    hc_flap.STATE_DIR = Path(tmp) / "hc_flap"
    try:
        check("CLI refuses a path-like name", hc_flap.main(["ok", "../x"]) == 2)
        check("CLI refuses an unknown action", hc_flap.main(["done", "x"]) == 2)
        for a in ("start", "fail", "start", "fail", "start"):
            hc_flap.main([a, "job-a"])
        check("CLI persists per name", len(hc_flap.load("job-a")["fails"]) == 2
              and hc_flap.load("job-b") == {})
    finally:
        hc_flap.STATE_DIR = real

# 4. Heartbeat: the 2026-09-27 nfl-lines-fetcher morning, pass by pass.
# (start, end, result) per run; the timer fires :09/:39, a stalled run is
# killed at +10 min (Result=timeout).
RUNS = [("01:39:02", "01:49:02", "timeout"), ("02:09:02", "02:09:12", "success"),
        ("02:39:03", "02:43:00", "success"), ("03:09:03", "03:10:07", "success"),
        ("03:39:03", "03:48:48", "success"), ("04:09:03", "04:13:33", "success"),
        ("04:39:08", "04:49:08", "timeout"), ("05:09:08", "05:19:09", "timeout"),
        ("05:39:09", "05:49:09", "timeout"), ("06:09:11", "06:09:15", "success"),
        ("06:39:11", "06:49:11", "timeout"), ("07:09:14", "07:09:18", "success"),
        ("07:39:14", "07:39:18", "success"), ("08:09:14", "08:09:18", "success")]
UNIT = "nfl-lines-fetcher.service"


def unit_at(now: float, runs=RUNS) -> dict:
    """systemctl show for the job at wall time `now`."""
    last_end, result, state = None, "success", "inactive"
    for start, end, res in runs:
        s, e = ts(start), ts(end)
        if now < s:
            break
        if now < e:                       # in progress: Result reset at start
            state, result = "activating", "success"
            break
        last_end, result = e, res
        state = "inactive" if res == "success" else "failed"
    return {"Result": result, "ExecMainStatus": "0" if result == "success" else "15",
            "ActiveState": state, "InactiveEnterTimestamp": f"@{int(last_end)}" if last_end else ""}


def fake_env(now: float, runs=RUNS):
    def show(unit, *props):
        if unit.endswith(".timer"):
            return {"FragmentPath": "/etc/systemd/system/" + unit, "ActiveState": "active",
                    "Unit": UNIT}
        return unit_at(now, runs)
    hb.show = show
    hb.enabled_units = lambda kind: ["nfl-lines-fetcher.timer"] if kind == "timer" else []
    hb.settle = lambda units: set(units)
    hb.covered = lambda unit: False
    hb.time.time = lambda: now


real_time = hb.time.time


def replay(runs, first: str, last: str, *, old_rule=False) -> tuple[list[str], dict]:
    """Heartbeat passes every 5 min; returns the group's status per pass."""
    job_fails: dict = {}
    statuses, bodies = [], {}
    t, end = ts(first), ts(last)
    while t <= end:
        fake_env(t, runs)
        if old_rule:  # the pre-damping check: last run Result only
            down = unit_at(t, runs)["Result"] != "success"
        else:
            failing, _ = hb.check_jobs(job_fails)
            down = bool(failing)
            bodies[datetime.fromtimestamp(t).strftime("%H:%M")] = failing
        statuses.append("DOWN" if down else "UP")
        t += 5 * M
    return statuses, bodies


def flips(statuses: list[str]) -> int:
    return sum(1 for a, b in zip(statuses, statuses[1:]) if a != b) + (statuses[0] == "DOWN")


try:
    old, _ = replay(RUNS, "01:40:00", "08:15:00", old_rule=True)
    new, bodies = replay(RUNS, "01:40:00", "08:15:00")
    check("old rule flapped (5 DOWN pages)", old.count("DOWN") and
          sum(1 for a, b in zip(["UP"] + old, old) if a == "UP" and b == "DOWN") == 5,
          "".join(s[0] for s in old))
    new_downs = sum(1 for a, b in zip(["UP"] + new, new) if a == "UP" and b == "DOWN")
    check("damped: one DOWN page per episode (2 episodes: 01:49, 04:49)", new_downs == 2,
          "".join(s[0] for s in new))
    check("damped: DOWN held continuously 04:50 → 07:45",
          all(s == "DOWN" for s in new[38:74]), "".join(s[0] for s in new))
    check("damped: UP once an hour passes clean (07:50)",
          bodies.get("07:50") == [] and bodies.get("07:45"), str(bodies.get("07:45")))
    check("re-run in progress after a failure stays failing (05:15)",
          any("re-running" in b for b in bodies["05:15"]), str(bodies["05:15"]))
    check("held line is parseable by hc_repair (unit first)",
          hr.failing_units("\n".join(bodies["06:15"])) == [UNIT], str(bodies["06:15"]))
    check("held line explains itself", "last run ok, flapping" in bodies["06:15"][0]
          and "held DOWN until 06:49" in bodies["06:15"][0], bodies["06:15"][0])
    check("first episode: a lone failure clears on the next good run (02:10)",
          bodies["02:10"] == [], str(bodies["02:10"]))
    # one failed run seen by four passes counts once
    jf: dict = {}
    for hm in ("04:50:00", "04:55:00", "05:00:00", "05:05:00"):
        fake_env(ts(hm))
        hb.check_jobs(jf)
    check("a failed run seen by several passes counts once", jf.get(UNIT) == [ts("04:49:08")],
          str(jf))
    # a slow job: yesterday's failure must still read failing while today's run
    # is in progress (past the 3h window only the newest failure is kept)
    ended = ts("05:00:30")
    jf = {UNIT: [ended]}

    def slow(state: str, end: float):
        hb.show = lambda unit, *p: (
            {"FragmentPath": "/etc/systemd/system/" + unit, "ActiveState": "active", "Unit": UNIT}
            if unit.endswith(".timer") else
            {"Result": "success", "ExecMainStatus": "0", "ActiveState": state,
             "InactiveEnterTimestamp": f"@{int(end)}"})

    slow("activating", ended)
    hb.time.time = lambda: ended + 86400
    failing, _ = hb.check_jobs(jf)
    check("a day-old failure reads failing while the next run is in progress",
          len(failing) == 1 and "re-running" in failing[0], str(failing))
    check("…remembered past the 3h window", jf.get(UNIT) == [ended], str(jf))
    slow("inactive", ended + 86400 + 60)
    failing, _ = hb.check_jobs(jf)
    check("…and the good run clears it (one failure never holds)", failing == [], str(failing))
    hb.time.time = lambda: ended + 8 * 86400
    hb.check_jobs(jf)
    check("a failure older than a week is forgotten", UNIT not in jf, str(jf))
finally:
    hb.time.time = real_time

# 5. hc_repair: the failure body survives a re-run in progress and held /log pings.
P = lambda t, n: {"type": t, "n": n}  # noqa: E731
check("latest_fail: fail newest", hr.latest_fail([P("fail", 3), P("start", 2)])["n"] == 3)
check("latest_fail: re-run started after the fail",
      hr.latest_fail([P("start", 4), P("fail", 3)])["n"] == 3)
check("latest_fail: held /log after the fail",
      hr.latest_fail([P("log", 6), P("start", 5), P("fail", 4)])["n"] == 4)
check("latest_fail: a success after the fail = no body",
      hr.latest_fail([P("start", 5), P("success", 4), P("fail", 3)]) is None)
check("latest_fail: empty", hr.latest_fail([]) is None)

# 6. Runner wiring: each run_*.sh against a stub job + stub curl.
RUNNERS = {"run_god_judge.sh": ("GOD_JUDGE_HEALTHCHECK_URL", "god-judge"),
           "run_nfl_pikkit_snapshots.sh": ("PIKKIT_SNAPSHOT_HEALTHCHECK_URL", "nfl-pikkit-snapshots"),
           "run_pikkit_opinions.sh": ("PIKKIT_OPINION_HEALTHCHECK_URL", "pikkit-opinions")}
for runner, (key, name) in RUNNERS.items():
    with tempfile.TemporaryDirectory() as tmp:
        app = Path(tmp)
        (app / "deploy").mkdir()
        (app / "bin").mkdir()
        shutil.copy(ROOT / "deploy" / "hc_flap.py", app / "deploy" / "hc_flap.py")
        (app / "bin" / "curl").write_text(
            '#!/bin/bash\nfor a in "$@"; do case "$a" in https://*) echo "$a" >> "$PINGS";; esac; done\n')
        (app / "bin" / "job").write_text('#!/bin/bash\necho "job ran"\nexit "${JOB_STATUS:-0}"\n')
        for f in ("curl", "job"):
            os.chmod(app / "bin" / f, 0o755)
        src = (ROOT / runner).read_text()
        src = (src.replace('APP_DIR="/home/forwarder/app"', f'APP_DIR="{app}"')
                  .replace('PYTHON="/home/forwarder/venv/bin/python"', f'PYTHON="{app}/bin/job"')
                  .replace('LOGFILE="/tmp/', f'LOGFILE="{app}/'))
        check(f"{runner}: test harness rewired paths", "/home/forwarder" not in src)
        (app / runner).write_text(src)
        pings = app / "pings.log"
        env = {**os.environ, "PATH": f"{app}/bin:{os.environ['PATH']}", "PINGS": str(pings),
               key: "https://hc.test/uuid"}

        def run(status: int) -> list[str]:
            pings.write_text("")
            subprocess.run(["bash", str(app / runner)], env={**env, "JOB_STATUS": str(status)},
                           capture_output=True, text=True)
            return [line.rsplit("/uuid", 1)[1] for line in pings.read_text().split()]

        seq = [run(0), run(1), run(0), run(1), run(0)]
        check(f"{runner}: clean run pings start+success", seq[0] == ["/start", ""], str(seq[0]))
        check(f"{runner}: failure pings /fail", seq[1] == ["/start", "/fail"], str(seq[1]))
        check(f"{runner}: lone failure then good run → success", seq[2] == ["/start", ""], str(seq[2]))
        check(f"{runner}: second failure pings /fail", seq[3] == ["/start", "/fail"], str(seq[3]))
        check(f"{runner}: flapping good run → /log (stays DOWN)", seq[4] == ["/start", "/log"],
              str(seq[4]))
        check(f"{runner}: state under logs/hc_flap/{name}.json",
              (app / "logs" / "hc_flap" / f"{name}.json").exists())
        (app / "deploy" / "hc_flap.py").write_text("raise SystemExit(1)\n")
        check(f"{runner}: a broken helper fails open (plain success)", run(0) == ["/start", ""])

print(f"\n{'ALL PASS' if not failures else f'{len(failures)} FAILED'}")
sys.exit(1 if failures else 0)
