#!/usr/bin/env python3
"""Offline tests for scripts/audit_mark.py (operator verdict from an audit
card): the pure verdict writer, and main()'s registry/state/idempotency flow
against temp files (tracker_cache paths monkeypatched — no real cache, no
network, no systemctl involved anywhere).

Run: ~/venv/bin/python -m unittest scripts.test_audit_mark -v
"""

import json
import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path

import tracker_cache
from scripts.audit_mark import apply_verdict, main

RECENT = (date.today() - timedelta(days=2)).isoformat()


def entry(*, verdicts=None, failed=False, odds=None, n_picks=2):
    e = {
        "parsed": {"picks": [{"description": f"leg {i}", "sport": "CFL"}
                             for i in range(n_picks)],
                   "sport": "MLB"},
        "leg_verdicts": verdicts or {},
        "odds_by_pick": odds or {},
        "msg_date": RECENT,
    }
    if failed:
        e["_failed"] = True
        e["_failed_reason"] = "stale unresolved"
    return e


class ApplyVerdict(unittest.TestCase):
    def test_marks_unresolved_only_and_clears_failed(self):
        cache = {"-1002:10": entry(
            verdicts={"0": {"verdict": "LOSS", "game_date": RECENT}},
            failed=True,
            odds={"1": {"game_date": "2026-09-05"}})}
        counts = apply_verdict(cache, ["-1002:10"], "WIN")
        self.assertEqual({"legs": 1, "copies": 1, "skipped": 1}, counts)
        e = cache["-1002:10"]
        self.assertNotIn("_failed", e)
        self.assertNotIn("_failed_reason", e)
        lv = e["leg_verdicts"]
        self.assertEqual("LOSS", lv["0"]["verdict"])  # settled leg untouched
        self.assertEqual(
            {"verdict": "WIN", "calc": "operator mark via nightly-audit card",
             "sport": "CFL", "game_date": "2026-09-05", "broadcasted": False},
            lv["1"])

    def test_void_and_attempt_dicts(self):
        cache = {"-1002:10": entry(verdicts={
            "0": {"verdict": "VOID"},
            "1": {"attempts": 3},  # verdict-less UNKNOWN-cap dict = unresolved
        })}
        counts = apply_verdict(cache, ["-1002:10"], "PUSH")
        self.assertEqual({"legs": 1, "copies": 1, "skipped": 1}, counts)
        self.assertEqual("VOID", cache["-1002:10"]["leg_verdicts"]["0"]["verdict"])
        self.assertEqual("PUSH", cache["-1002:10"]["leg_verdicts"]["1"]["verdict"])

    def test_fanout_copies_and_missing_key(self):
        cache = {"-1002:10": entry(n_picks=1), "-1004:99": entry(n_picks=1)}
        counts = apply_verdict(cache, ["-1002:10", "-1004:99", "-1009:1"],
                               "LOSS")
        self.assertEqual({"legs": 2, "copies": 2, "skipped": 0}, counts)


class MainFlow(unittest.TestCase):
    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        td = Path(self._td.name)
        self.cache_path = td / "parse_cache.json"
        self.cards_path = td / "cards.json"
        self.state_path = td / "state.json"
        self._orig = (tracker_cache._PENDING_CACHE_PATH,
                      tracker_cache._PENDING_LOCK_PATH)
        tracker_cache._PENDING_CACHE_PATH = str(self.cache_path)
        tracker_cache._PENDING_LOCK_PATH = str(self.cache_path) + ".lock"

    def tearDown(self):
        (tracker_cache._PENDING_CACHE_PATH,
         tracker_cache._PENDING_LOCK_PATH) = self._orig
        self._td.cleanup()

    def _write(self, cache, card_keys):
        self.cache_path.write_text(json.dumps(cache))
        self.cards_path.write_text(json.dumps({"abc123def0": {
            "run_date": RECENT, "keys": card_keys, "capper": "Cap",
            "desc": "Elks ML", "outcome": "needs_human",
            "html": "x", "keyboard": {}, "marked": None}}))

    def _run(self, *argv):
        return main(list(argv) + ["--cards-file", str(self.cards_path),
                                  "--state-file", str(self.state_path)])

    def test_apply_park_and_idempotency(self):
        self._write({"-1002:10": entry(failed=True)}, ["-1002:10"])
        self.assertEqual(0, self._run("abc123def0", "WIN"))
        cache = json.loads(self.cache_path.read_text())
        self.assertEqual("WIN", cache["-1002:10"]["leg_verdicts"]["0"]["verdict"])
        self.assertNotIn("_failed", cache["-1002:10"])
        state = json.loads(self.state_path.read_text())
        self.assertTrue(state["-1002:10"]["parked"])
        self.assertEqual("operator_win", state["-1002:10"]["last_outcome"])
        card = json.loads(self.cards_path.read_text())["abc123def0"]
        self.assertEqual("WIN", card["marked"]["verdict"])
        # second tap: refused, cache untouched
        self.assertEqual(3, self._run("abc123def0", "LOSS"))
        cache = json.loads(self.cache_path.read_text())
        self.assertEqual("WIN", cache["-1002:10"]["leg_verdicts"]["0"]["verdict"])

    def test_unknown_card(self):
        self._write({}, [])
        self.assertEqual(2, self._run("ffffffffff", "WIN"))

    def test_eviction_horizon_refused(self):
        old = (date.today() - timedelta(days=20)).isoformat()
        e = entry()
        e["msg_date"] = old
        self._write({"-1002:10": e}, ["-1002:10"])
        self.assertEqual(2, self._run("abc123def0", "WIN"))
        cache = json.loads(self.cache_path.read_text())
        self.assertEqual({}, cache["-1002:10"]["leg_verdicts"])
        self.assertIsNone(
            json.loads(self.cards_path.read_text())["abc123def0"]["marked"])


if __name__ == "__main__":
    unittest.main()
