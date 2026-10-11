#!/usr/bin/env python3
"""Grader anomaly queue (agent-first, 2026-10-11).

The tracker queues actionable parse/binding anomalies here instead of posting
to the retired Telegram audit channel (audit.warn(kind="anomaly") →
logs/grader_anomalies.jsonl). While any entry is pending, every tracker pass
pings the "Grader anomalies" check /fail, hc-repair spawns the agent whose
card is the operator's one DM, the agent fixes the pick and resolves the
entry here, and the next pass pings the check up.

    ~/venv/bin/python scripts/grader_anomalies.py              # list pending
    ~/venv/bin/python scripts/grader_anomalies.py --all        # list everything
    ~/venv/bin/python scripts/grader_anomalies.py --resolve ID # or: --resolve all
"""
from __future__ import annotations

import argparse
import sys
import time
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from audit import load_anomalies, save_anomalies  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--all", action="store_true", help="include resolved entries")
    ap.add_argument("--resolve", metavar="ID", help="mark one entry (or 'all') resolved")
    args = ap.parse_args()

    rows = load_anomalies()
    if args.resolve:
        hit = 0
        for r in rows:
            if r.get("status") == "pending" and args.resolve in ("all", r.get("id")):
                r["status"] = "resolved"
                r["resolved_ts"] = time.time()
                hit += 1
        if not hit:
            print(f"no pending entry matches {args.resolve!r}")
            return 1
        save_anomalies(rows)
        print(f"resolved {hit} entr{'y' if hit == 1 else 'ies'} — the next tracker "
              "pass pings the check up")
        return 0

    shown = 0
    for r in rows:
        if not args.all and r.get("status") != "pending":
            continue
        stamp = datetime.fromtimestamp(float(r.get("ts") or 0)).strftime("%m-%d %H:%M")
        print(f"[{r.get('id')}] {r.get('status', '?'):8} {stamp}  {r.get('text', '')}")
        shown += 1
    if not shown:
        print("no pending anomalies" if not args.all else "queue empty")
    return 0


if __name__ == "__main__":
    sys.exit(main())
