#!/usr/bin/env python3
"""Flap damping for healthchecks.io checks (docs/vps.md "Healthchecks").

healthchecks.io alerts on every status change, so a job failing on and off
paged DOWN and UP with nearly every run: on 2026-09-27 the NFL Guesser sheet
stalled its reads for ~30 min of every hour, and four checks sent ~40 alerts
in six hours. One rule, shared by the heartbeat (VPS jobs) and the job runners
of dedicated checks:

  * a failure still reports at once (the first page is never delayed), and a
    lone failure still clears on the next good run;
  * once a job has failed FLAP_FAILS+ times within FLAP_WINDOW, a good run
    keeps it DOWN until HOLD has passed with no new failure — one DOWN/UP
    pair per episode instead of one per run.

A hold only ever keeps a DOWN check down, never pushes an UP one down: a
runner's held run pings /log, and /log on an UP check starves it of success
pings until healthchecks.io's period+grace pages "missed" (seeding the history
into UP checks did exactly that, 2026-09-27) — so `ok` holds only while this
runner's last status ping was a /fail, and the heartbeat only keeps listing a
unit it listed on the previous pass.

Runners call the CLI (state: logs/hc_flap/<name>.json):
    hc_flap.py start <name>   before /start; prints a notice when the previous
                              run never reported (killed at TimeoutStartSec) —
                              it counts as a failure, and the runner pings
                              /fail with the notice
    hc_flap.py fail <name>    before /fail
    hc_flap.py ok <name>      prints why the check must stay DOWN (the runner
                              then pings /log, which leaves the status alone)
                              or nothing (ping success as usual)
Every error fails open: the runner then pings exactly as it did before.
"""
from __future__ import annotations

import json
import os
import re
import sys
import time
from datetime import datetime
from pathlib import Path

FLAP_FAILS = 2           # failures within FLAP_WINDOW that make a job "flapping"
FLAP_WINDOW = 3 * 3600   # s
HOLD = 3600              # s without a new failure before a flapping job reads UP
KILLED_WINDOW = 86400    # s an unreported run still counts as killed at the next start

STATE_DIR = Path(__file__).resolve().parent.parent / "logs" / "hc_flap"


def recent(fails: list[float], now: float) -> list[float]:
    """The failure times still inside FLAP_WINDOW, oldest first."""
    return sorted(t for t in fails if now - t < FLAP_WINDOW)


def clock(ts: float) -> str:
    return datetime.fromtimestamp(ts).strftime("%H:%M")


def hold_reason(fails: list[float], now: float) -> str:
    """'' when a good run may report UP, else why the check stays DOWN."""
    r = recent(fails, now)
    if len(r) < FLAP_FAILS or now - r[-1] >= HOLD:
        return ""
    return (f"flapping — {len(r)} failed runs in {FLAP_WINDOW // 3600}h, last "
            f"{clock(r[-1])}; held DOWN until {clock(r[-1] + HOLD)} unless it fails again")


def _path(name: str) -> Path:
    return STATE_DIR / f"{name}.json"


def load(name: str) -> dict:
    try:
        return json.loads(_path(name).read_text())
    except (OSError, ValueError):
        return {}


def save(name: str, state: dict) -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    tmp = _path(name).with_suffix(".tmp")
    tmp.write_text(json.dumps(state))
    os.replace(tmp, _path(name))


def step(state: dict, action: str, now: float) -> tuple[dict, str]:
    """Apply one runner event; returns (new state, text to print). `down` =
    the last status ping this runner sent was a /fail."""
    fails = recent(state.get("fails", []), now)
    down = bool(state.get("down"))
    out = ""
    if action == "start":
        began = state.get("running_since")
        if began and now - began < KILLED_WINDOW:
            fails.append(now)
            down = True
            out = (f"the previous run (started {clock(began)}) never reported — "
                   "killed at its TimeoutStartSec? Counted as a failed run.")
        running = now
    elif action == "fail":
        fails.append(now)
        down, running = True, None
    else:  # ok
        running = None
        out = hold_reason(fails, now) if down else ""
        down = bool(out)
    return {"fails": fails, "running_since": running, "down": down}, out


def main(argv: list[str]) -> int:
    if (len(argv) != 2 or argv[0] not in ("start", "fail", "ok")
            or not re.fullmatch(r"[\w.-]+", argv[1])):
        print("usage: hc_flap.py start|fail|ok <name>", file=sys.stderr)
        return 2
    action, name = argv
    state, out = step(load(name), action, time.time())
    save(name, state)
    if out:
        print(out)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
