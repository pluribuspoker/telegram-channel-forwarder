#!/usr/bin/env python3
"""
stop_context.py — one journal line saying WHY a unit stopped, WHO asked, and
what memory looked like at that moment.

Wired as `ExecStopPost=` on long-running units, so `journalctl -u <unit>` shows
the cause right next to the stop. It exists because a stop otherwise leaves only
"Stopping …" in the journal: on 2026-09-30, attributing three Claude-session
restarts meant cross-reading apt history, sudo lines and timer bursts by hand.

  stop-context: result=exit-code exit=exited/1 requester=none (self-exit)
    mem avail=301MB swap=939/2047MB top: telegram-intake 690MB · claude-channels 501MB · …

* result/exit come from systemd ($SERVICE_RESULT, $EXIT_CODE, $EXIT_STATUS).
  `watchdog` = missed WATCHDOG pings, `oom-kill` = the kernel killed it.
* requester: a `systemctl stop|restart|kill <unit>` blocks on the stop job,
  so it is still alive while this runs — its ancestry (sudo < bash < sshd …)
  names who asked. Nothing found + a failure result = the unit exited by itself.
* top: resident + swapped memory per systemd unit (from /proc/*/cgroup).

Stdlib only, never raises, never exits non-zero (a failing ExecStopPost would
mark the unit failed). Also imported by mem_watchdog.py for its periodic line.

Usage:
  stop_context.py --stop <unit>   # ExecStopPost
  stop_context.py                 # memory line only
"""

import os
import sys
from collections import defaultdict
from pathlib import Path

_STOP_VERBS = {"stop", "restart", "try-restart", "reload-or-restart",
               "try-reload-or-restart", "kill", "isolate"}


def _read(path: str) -> str:
    try:
        return Path(path).read_text(errors="replace")
    except OSError:
        return ""


def _cmdline(pid: str) -> list[str]:
    raw = _read(f"/proc/{pid}/cmdline")
    return [a for a in raw.split("\0") if a]


def _ppid(pid: str) -> str:
    for line in _read(f"/proc/{pid}/status").splitlines():
        if line.startswith("PPid:"):
            return line.split()[1]
    return ""


def _pids() -> list[str]:
    return [p for p in os.listdir("/proc") if p.isdigit()]


def _unit_of(pid: str) -> str:
    """Last cgroup component: 'telegram-intake.service', 'session-12.scope', …"""
    for line in _read(f"/proc/{pid}/cgroup").splitlines():
        path = line.rsplit(":", 1)[-1].strip()
        if path:
            return path.rstrip("/").rsplit("/", 1)[-1] or "-"
    return "-"


def requester(unit: str) -> str:
    """Ancestry chains of live `systemctl <stop-verb> … <unit>` processes."""
    base = unit.removesuffix(".service")
    me = str(os.getpid())
    chains = []
    for pid in _pids():
        if pid == me:
            continue
        args = _cmdline(pid)
        if not args or os.path.basename(args[0]) != "systemctl":
            continue
        rest = args[1:]
        if not (_STOP_VERBS & set(rest)) or not any(
            a in (unit, base) for a in rest
        ):
            continue
        hops, p = [], pid
        for _ in range(7):
            cmd = " ".join(_cmdline(p))[:90]
            if not cmd:
                break
            hops.append(cmd)
            p = _ppid(p)
            if not p or p in ("0", "1"):
                break
        chains.append(" < ".join(hops))
    return " | ".join(chains)


def mem_line(top_n: int = 5) -> str:
    mi = {}
    for line in _read("/proc/meminfo").splitlines():
        k, _, v = line.partition(":")
        if v.strip():
            mi[k] = int(v.split()[0]) // 1024
    swap_used = mi.get("SwapTotal", 0) - mi.get("SwapFree", 0)
    per_unit: dict[str, int] = defaultdict(int)
    for pid in _pids():
        kb = 0
        for line in _read(f"/proc/{pid}/status").splitlines():
            if line.startswith(("VmRSS:", "VmSwap:")):
                kb += int(line.split()[1])
        if kb:
            per_unit[_unit_of(pid).removesuffix(".service")] += kb
    top = sorted(per_unit.items(), key=lambda kv: -kv[1])[:top_n]
    tops = " · ".join(f"{u} {kb // 1024}MB" for u, kb in top)
    return (f"mem avail={mi.get('MemAvailable', '?')}MB "
            f"swap={swap_used}/{mi.get('SwapTotal', '?')}MB top: {tops}")


def stop_line(unit: str) -> str:
    result = os.environ.get("SERVICE_RESULT", "?")
    code = os.environ.get("EXIT_CODE", "?")
    status = os.environ.get("EXIT_STATUS", "?")
    who = requester(unit)
    if not who:
        who = "none (self-exit)" if result != "success" else "none found"
    return (f"stop-context: result={result} exit={code}/{status} "
            f"requester={who} {mem_line()}")


def main() -> None:
    try:
        if len(sys.argv) >= 3 and sys.argv[1] == "--stop":
            print(stop_line(sys.argv[2]), flush=True)
        else:
            print(mem_line(), flush=True)
    except Exception as e:  # never fail the unit over a log line
        print(f"stop-context: error {e!r}", flush=True)


if __name__ == "__main__":
    main()
