"""parse_cache eviction covers the classes that were immortal (2026-10-10).

Before this, 281 of 548 live entries (51%) could never evict: _failed markers were
kept forever, listener seeds that never parsed (media-only posts) had no leg
verdicts to age on, and a VOID leg (voided moot parlay legs) made all_resolved
False permanently. The parsed ones were also refetched from Telegram by every
tracker pass's stale catch-up — needs_stale_fetch now filters that BEFORE the
fetch. Entries carry no creation time, so the sweep stamps _evict_ts on first
sight and ages against it; _pending_entry must preserve the stamp or rebuilds
reset the clock.

Run:  ~/venv/bin/python scripts/test_cache_eviction.py
"""
import os
import sys
from datetime import datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tracker_cache import (  # noqa: E402
    _evict_stale,
    _pending_entry,
    needs_stale_fetch,
)


def iso_days_ago(days):
    return (datetime.now() - timedelta(days=days)).isoformat(timespec="seconds")


def resolved_leg(verdict, game_date):
    return {"verdict": verdict, "calc": "x", "game_date": game_date, "broadcasted": True}


def main() -> int:
    failures = 0

    def check(label, ok):
        nonlocal failures
        print(f"  {'PASS' if ok else 'FAIL'}  {label}")
        failures += not ok

    cache = {
        "c:1": {"_failed": True, "text_hash": "aa"},                               # fresh failure → stamp + keep
        "c:2": {"_failed": True, "text_hash": "bb", "_evict_ts": iso_days_ago(8)},  # old failure → evict
        "c:3": {"_failed": True, "_failed_reason": "message deleted", "parsed": {"picks": [{}]},
                "_evict_ts": iso_days_ago(8)},                                      # old retired → evict
        "c:4": {"_failed": True, "text_hash": "cc", "_evict_ts": iso_days_ago(2)},  # young failure → keep
        "c:5": {"_forwarded": True, "mapping_id": "a", "_source_key": "s:1"},       # fresh seed → stamp + keep
        "c:6": {"_forwarded": True, "_evict_ts": iso_days_ago(4)},                  # old seed → evict
        "c:7": {"parsed": {"picks": [{}, {}]},                                      # VOID-settled, old → evict
                "leg_verdicts": {"0": resolved_leg("LOSS", iso_days_ago(20)[:10]),
                                 "1": resolved_leg("VOID", iso_days_ago(20)[:10])}},
        "c:8": {"parsed": {"picks": [{}]},                                          # resolved, young → keep
                "leg_verdicts": {"0": resolved_leg("WIN", iso_days_ago(2)[:10])}},
        "c:9": {"parsed": {"picks": [{}]}, "leg_verdicts": {}},                     # open, no verdicts → keep
        "c:10": {"parsed": {"picks": [{}]},                                         # unresolved leg → keep
                 "leg_verdicts": {"0": {"unknown_attempts": 2}}},
        "c:11": "corrupt",                                                          # non-dict → evict
        "c:12": {"_dupe": True, "primary_id": 8},                                   # primary alive → keep
        "c:13": {"_dupe": True, "primary_id": 999},                                 # primary gone → evict
    }
    _evict_stale(cache)

    check("old _failed marker evicted", "c:2" not in cache)
    check("old retired entry evicted", "c:3" not in cache)
    check("young _failed kept", "c:4" in cache)
    check("fresh _failed stamped and kept", "c:1" in cache and cache["c:1"].get("_evict_ts"))
    check("old seed evicted", "c:6" not in cache)
    check("fresh seed stamped and kept", "c:5" in cache and cache["c:5"].get("_evict_ts"))
    check("VOID counts as resolved — settled parlay evicts", "c:7" not in cache)
    check("young resolved kept", "c:8" in cache)
    check("open entries kept and never stamped",
          "c:9" in cache and "_evict_ts" not in cache["c:9"]
          and "c:10" in cache and "_evict_ts" not in cache["c:10"])
    check("corrupt entry evicted", "c:11" not in cache)
    check("dupe with live primary kept", "c:12" in cache)
    check("dupe whose primary is gone evicted", "c:13" not in cache)

    # A second sweep must not re-stamp (ages against the first sight).
    ts = cache["c:1"]["_evict_ts"]
    _evict_stale(cache)
    check("stamp is stable across sweeps", cache["c:1"]["_evict_ts"] == ts)

    # _pending_entry preserves the stamp across rebuilds (else the clock resets).
    rebuilt = _pending_entry("cap", {"picks": []}, {}, {"_evict_ts": "2026-10-01T00:00:00",
                                                        "_failed": True})
    check("_pending_entry preserves _evict_ts", rebuilt.get("_evict_ts") == "2026-10-01T00:00:00")

    # needs_stale_fetch: skip terminal/fully-broadcast entries BEFORE the fetch.
    check("retired entry is not refetched",
          not needs_stale_fetch({"_failed": True, "_failed_reason": "x",
                                 "parsed": {"picks": [{}]}}))
    check("parse-failure without reason IS refetched (text_hash re-check needs text)",
          needs_stale_fetch({"_failed": True, "parsed": {"picks": [{}]}}))
    check("fully-broadcast entry is not refetched",
          not needs_stale_fetch({"parsed": {"picks": [{}, {}]},
                                 "leg_verdicts": {"0": {"verdict": "WIN", "broadcasted": True},
                                                  "1": {"verdict": "VOID", "broadcasted": True}}}))
    check("partially-broadcast entry IS refetched",
          needs_stale_fetch({"parsed": {"picks": [{}, {}]},
                             "leg_verdicts": {"0": {"verdict": "WIN", "broadcasted": True}}}))
    check("no-picks / missing-verdict entries ARE refetched",
          needs_stale_fetch({"parsed": {"picks": []}})
          and needs_stale_fetch({"parsed": {"picks": [{}]}}))

    print("FAILURES:", failures)
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
