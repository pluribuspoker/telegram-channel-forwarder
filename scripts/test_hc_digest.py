#!/usr/bin/env python3
"""Offline tests for the daily alerting digest (scripts/hc_digest.py).

build_digest renders only the sections with content (blips, agent runs,
parked, open episodes, grader anomalies, X bootstrap wobble) and returns
None for a clean 24h — a clean day sends nothing, and the check's success
ping is the liveness proof.
"""
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scripts import hc_digest as hd  # noqa: E402

failures = []


def check(label: str, ok: bool, detail: str = ""):
    print(f"{'PASS' if ok else 'FAIL'}  {label}" + (f"  ({detail})" if detail and not ok else ""))
    if not ok:
        failures.append(label)


NOW = datetime(2026, 10, 12, 12, 30, tzinfo=timezone.utc)
iso = lambda dt: dt.isoformat(timespec="seconds")  # noqa: E731

check("clean 24h renders no digest",
      hd.build_digest(NOW, {}, [], [], {}) is None)

state = {
    "_blip_log": [
        {"ts": iso(NOW - timedelta(hours=2)), "check": "Sauce watch", "unit": ""},
        {"ts": iso(NOW - timedelta(hours=1)), "check": "Sauce watch", "unit": ""},
        {"ts": iso(NOW - timedelta(hours=30)), "check": "Old blip", "unit": ""},
    ],
    "judge-key": {"check": "God judge", "parked": True,
                  "parked_reason": "needs_human"},
    "open-key": {"check": "MOE grade",
                 "first_seen": iso(NOW - timedelta(minutes=40))},
}
runs = [{"ts": iso(NOW - timedelta(hours=3)), "total_cost_usd": 1.25,
         "commits": ["abc fix"], "reports": {"Sauce watch": {"outcome": "fixed_verified"}}},
        {"ts": iso(NOW - timedelta(hours=40)), "total_cost_usd": 9.0,
         "commits": [], "reports": {"Old": {"outcome": "no_issue"}}}]
anoms = [{"id": "a1", "status": "pending", "text": "possible wrong contest: X", "ts": 1.0},
         {"id": "a2", "status": "resolved", "resolved_ts": time.time()}]
trent = {"degraded_events": [iso(NOW - timedelta(hours=h)) for h in (1, 2, 3, 4)]}

text = hd.build_digest(NOW, state, runs, anoms, trent)
check("digest renders", text is not None and text.startswith("🗞"))
check("blips: in-window grouped with count, stale dropped",
      "Sauce watch ×2" in text and "Old blip" not in text, text)
check("agent runs: outcome + commits + notional cost",
      "Sauce watch=fixed_verified" in text and "1 commit(s)" in text
      and "$1.25" in text and "no_issue" not in text, text)
check("parked section carries the rearm hint",
      "God judge (needs_human)" in text and "--rearm" in text, text)
check("open episodes show down-since", "MOE grade (down since" in text, text)
check("anomalies: pending detail + resolved count",
      "1 pending, 1 resolved/24h" in text and "[a1]" in text, text)
check("trent wobble appears at 4+ events/24h",
      "X bootstrap wobble" in text and "4 degraded" in text, text)

trent3 = {"degraded_events": trent["degraded_events"][:3]}
t2 = hd.build_digest(NOW, {"_blip_log": state["_blip_log"][:1]}, [], [], trent3)
check("3 wobble events stay out of the digest",
      t2 is not None and "wobble" not in t2, t2)

check("no URL → ping is a no-op (no crash)", hd.ping() is None)

print(f"\n{'ALL PASS' if not failures else f'{len(failures)} FAILED'}")
sys.exit(1 if failures else 0)
