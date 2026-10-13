#!/usr/bin/env python3
"""Offline tests: the grader anomaly queue (operator 2026-10-11).

The Telegram audit channel is retired: audit.record writes only the DB row,
audit.warn prints to the journal, and kind="anomaly" queues the entry in
logs/grader_anomalies.jsonl. anomaly_check_ping /fails the Grader anomalies
check while anything is pending (hc-repair's agent is the notifier) and
pings success when clear; scripts/grader_anomalies.py resolves entries.
"""
import asyncio
import json
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import audit  # noqa: E402

failures = []


def check(label: str, ok: bool, detail: str = ""):
    print(f"{'PASS' if ok else 'FAIL'}  {label}" + (f"  ({detail})" if detail and not ok else ""))
    if not ok:
        failures.append(label)


TMP = Path(tempfile.mkdtemp(prefix="grader_anoms_"))
audit.ANOMALIES_FILE = str(TMP / "grader_anomalies.jsonl")
import scripts.grader_anomalies as cli  # noqa: E402

# The channel plumbing is gone for good.
check("AuditLog has no audit channel attribute",
      not hasattr(audit.AuditLog, "_post_telegram"))
log = audit.AuditLog(db_path=str(TMP / "picks.db"))
check("constructor no longer takes a channel id",
      "audit_channel_id" not in audit.AuditLog.__init__.__code__.co_varnames)

# warn(kind="info") prints, queues nothing.
asyncio.run(log.warn("🔧 <b>parse repaired from schedule</b>: fixed"))
check("info warn queues nothing", audit.load_anomalies() == [])

# warn(kind="anomaly") queues a pending entry with HTML stripped.
asyncio.run(log.warn("⚠️ <b>possible wrong contest</b>: Huskies vs Commanders",
                     kind="anomaly"))
rows = audit.load_anomalies()
check("anomaly warn queues one pending entry",
      len(rows) == 1 and rows[0]["status"] == "pending"
      and rows[0]["text"] == "⚠️ possible wrong contest: Huskies vs Commanders",
      f"{rows}")
asyncio.run(log.warn("⚠️ <b>possible wrong contest</b>: Huskies vs Commanders",
                     kind="anomaly"))
check("identical pending text dedupes", len(audit.load_anomalies()) == 1)

# The pick's cache key rides along → a t.me link for hc-repair's card.
asyncio.run(log.warn("⚠️ <b>pick names no one on the slate</b>: x", kind="anomaly",
                     key="-1002486251914:4321", capper="Wager <b>Stalk</b> 💎"))
row = audit.load_anomalies()[-1]
check("anomaly stores the pick key, link and plain capper",
      row.get("key") == "-1002486251914:4321"
      and row.get("link") == "https://t.me/c/2486251914/4321"
      and row.get("capper") == "Wager Stalk 💎", f"{row}")
check("unparseable key → no link", audit.pick_link_for_key("abc") == "")
audit.save_anomalies(audit.load_anomalies()[:-1])  # the rest expects one entry

# The check ping: /fail with the pending list, success when clear.
pings = []


class _Resp:
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return None


def _fake_urlopen(req, timeout=None):
    pings.append((req.full_url, (req.data or b"").decode()))
    return _Resp()


audit.os.environ["GRADER_ANOMALIES_HEALTHCHECK_URL"] = "https://hc-ping.com/ga-uuid"
saved = audit.urllib.request.urlopen
audit.urllib.request.urlopen = _fake_urlopen
try:
    audit.anomaly_check_ping()
    check("pending anomaly → /fail ping listing it",
          pings and pings[-1][0].endswith("/fail")
          and "possible wrong contest" in pings[-1][1]
          and "grader_anomalies.py --resolve" in pings[-1][1], f"{pings[-1:]}")

    rid = audit.load_anomalies()[0]["id"]
    sys.argv = ["grader_anomalies.py", "--resolve", rid]
    check("CLI resolves by id", cli.main() == 0
          and audit.load_anomalies()[0]["status"] == "resolved")

    pings.clear()
    audit.anomaly_check_ping()
    check("clear queue → success ping",
          pings and not pings[-1][0].endswith("/fail"), f"{pings[-1:]}")

    audit.os.environ.pop("GRADER_ANOMALIES_HEALTHCHECK_URL")
    pings.clear()
    audit.anomaly_check_ping()
    check("no URL → no ping", pings == [])
finally:
    audit.urllib.request.urlopen = saved
    audit.os.environ.pop("GRADER_ANOMALIES_HEALTHCHECK_URL", None)

# Resolve-all + prune: resolved entries age out, pending never do.
asyncio.run(log.warn("⚠️ a", kind="anomaly"))
asyncio.run(log.warn("⚠️ b", kind="anomaly"))
sys.argv = ["grader_anomalies.py", "--resolve", "all"]
check("CLI resolves all pending", cli.main() == 0
      and all(r["status"] == "resolved" for r in audit.load_anomalies()))
rows = audit.load_anomalies()
rows[0]["ts"] = 1.0  # ancient resolved entry
old_pending = {"id": "keepme", "ts": 1.0, "text": "old pending", "status": "pending"}
rows.append(old_pending)
audit.save_anomalies(rows)
kept = audit.load_anomalies()
check("prune drops ancient resolved, keeps ancient pending",
      all(r["ts"] != 1.0 or r["status"] == "pending" for r in kept)
      and any(r["id"] == "keepme" for r in kept), f"{kept}")

# The tracker's six anomaly sites carry the kind; nothing else does.
src = (ROOT / "tracker.py").read_text()
check("tracker queues exactly six anomaly kinds", src.count('kind="anomaly"') == 6)
check("tracker pings the check at end of run", "anomaly_check_ping()" in src)

print(f"\n{'ALL PASS' if not failures else f'{len(failures)} FAILED'}")
sys.exit(1 if failures else 0)
