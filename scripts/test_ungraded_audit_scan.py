#!/usr/bin/env python3
"""Offline tests for the nightly ungraded-pick audit scanner.

Pins the candidate predicate (who gets an agent), the fan-out grouping, the
state/parking transitions, and the AUDIT_RESULT contract parsing. No network,
no claude, no cache file — synthetic entries only.

Run: ~/venv/bin/python -m unittest scripts.test_ungraded_audit_scan -v
"""

import unittest
from datetime import date

from scripts.ungraded_audit import (
    _stale_reference_date,
    _unresolved_indices,
    build_prompt,
    compose_dm,
    parse_audit_result,
    record_attempt,
    scan,
)

TODAY = date(2026, 9, 8)


def entry(*, picks, leg_verdicts=None, msg_date="2026-09-05", capper="Cap",
          failed=False, failed_reason="", odds=None, dupe=False):
    e = {
        "parsed": {"picks": picks, "sport": "MLB"},
        "leg_verdicts": leg_verdicts or {},
        "odds_by_pick": odds or {},
        "msg_date": msg_date,
        "capper_name": capper,
    }
    if failed:
        e["_failed"] = True
        e["_failed_reason"] = failed_reason
    if dupe:
        e["_dupe"] = True
    return e


def pick(desc="Yankees ML", **kw):
    return {"description": desc, "bet_type": kw.get("bet_type", "moneyline"),
            **kw}


class StaleReferenceDate(unittest.TestCase):
    def test_falls_back_to_msg_date(self):
        self.assertEqual(_stale_reference_date({}, {}, "2026-09-01"),
                         "2026-09-01")

    def test_game_date_pushes_horizon(self):
        lv = {"0": {"game_date": "2026-09-06"}}
        odds = {"1": {"game_date": "2026-09-07"}}
        self.assertEqual(_stale_reference_date(lv, odds, "2026-09-01"),
                         "2026-09-07")

    def test_ignores_malformed_dates(self):
        lv = {"0": {"game_date": "soon"}, "1": "not-a-dict"}
        self.assertEqual(_stale_reference_date(lv, {}, "2026-09-01"),
                         "2026-09-01")


class UnresolvedIndices(unittest.TestCase):
    def test_void_counts_as_settled(self):
        picks = [pick(), pick("Sox ML")]
        lv = {"0": {"verdict": "VOID"}, "1": {"verdict": "WIN"}}
        self.assertEqual(_unresolved_indices(picks, lv), [])

    def test_attempt_dict_without_verdict_is_unresolved(self):
        picks = [pick()]
        lv = {"0": {"unknown_attempts": 6, "last_unknown": "2026-09-05T00:00:00"}}
        self.assertEqual(_unresolved_indices(picks, lv), [0])

    def test_lost_parlay_moots_pending_legs(self):
        picks = [pick(is_parlay_leg=True), pick("Cubs ML", is_parlay_leg=True)]
        lv = {"0": {"verdict": "LOSS"}}
        self.assertEqual(_unresolved_indices(picks, lv), [])


class Scan(unittest.TestCase):
    def scan(self, cache, state=None, **kw):
        kw.setdefault("today_et", TODAY)
        return scan(cache, state or {}, **kw)

    def test_selects_unresolved_past_ref(self):
        cache = {"-100:1": entry(picks=[pick()])}
        groups = self.scan(cache)
        self.assertEqual(len(groups), 1)
        self.assertEqual(groups[0]["keys"], ["-100:1"])
        self.assertEqual(groups[0]["ref_date"], "2026-09-05")

    def test_skips_resolved_and_dupes_and_pickless(self):
        cache = {
            "-100:1": entry(picks=[pick()], leg_verdicts={"0": {"verdict": "WIN"}}),
            "-100:2": entry(picks=[pick()], dupe=True),
            "-100:3": entry(picks=[]),
            "-100:4": {"_forwarded": True},
        }
        self.assertEqual(self.scan(cache), [])

    def test_todays_game_still_owned_by_normal_flow(self):
        cache = {
            "-100:1": entry(picks=[pick()], msg_date=TODAY.isoformat()),
            "-100:2": entry(picks=[pick("Angels ML")], msg_date="2026-09-01",
                            odds={"0": {"game_date": TODAY.isoformat()}}),
        }
        self.assertEqual(self.scan(cache), [])

    def test_window_floor_excludes_old_backlog(self):
        cache = {"-100:1": entry(picks=[pick()], msg_date="2026-08-01")}
        self.assertEqual(self.scan(cache, days_back=10), [])
        self.assertEqual(len(self.scan(cache, days_back=60)), 1)

    def test_deleted_message_retiree_excluded_other_failed_kept(self):
        cache = {
            "-100:1": entry(picks=[pick()], failed=True,
                            failed_reason="message deleted"),
            "-100:2": entry(picks=[pick("Mets ML")], failed=True,
                            failed_reason="unresolvable after 4d — Mets ML"),
        }
        groups = self.scan(cache)
        self.assertEqual([g["keys"] for g in groups], [["-100:2"]])
        self.assertTrue(groups[0]["members"][0]["failed"])

    def test_fanout_copies_group_into_one_candidate(self):
        cache = {
            "-1002:10": entry(picks=[pick("Bears -3.5")]),
            "-1004:99": entry(picks=[pick("Bears -3.5")]),
            "-1004:98": entry(picks=[pick("Lions ML")]),
        }
        groups = self.scan(cache)
        self.assertEqual(len(groups), 2)
        bears = next(g for g in groups if len(g["keys"]) == 2)
        self.assertEqual(bears["keys"], ["-1002:10", "-1004:99"])

    def test_parked_or_capped_copy_skips_whole_group(self):
        cache = {
            "-1002:10": entry(picks=[pick("Bears -3.5")]),
            "-1004:99": entry(picks=[pick("Bears -3.5")]),
        }
        self.assertEqual(
            self.scan(cache, {"-1004:99": {"parked": True}}), [])
        self.assertEqual(
            self.scan(cache, {"-1002:10": {"attempts": 2}}, attempt_cap=2), [])
        self.assertEqual(
            len(self.scan(cache, {"-1002:10": {"attempts": 1}}, attempt_cap=2)), 1)

    def test_newest_reference_first(self):
        cache = {
            "-100:1": entry(picks=[pick("old")], msg_date="2026-09-03"),
            "-100:2": entry(picks=[pick("new")], msg_date="2026-09-07"),
        }
        groups = self.scan(cache)
        self.assertEqual([g["ref_date"] for g in groups],
                         ["2026-09-07", "2026-09-03"])


class AuditResultParsing(unittest.TestCase):
    def test_valid_line(self):
        out = parse_audit_result(
            'Long report...\nAUDIT_RESULT: {"outcome": "graded", '
            '"issue": "FCS game missing", "action": "added groups=90"}')
        self.assertEqual(out["outcome"], "graded")
        self.assertEqual(out["action"], "added groups=90")

    def test_last_line_wins(self):
        out = parse_audit_result(
            'AUDIT_RESULT: {"outcome": "needs_human", "issue": "a", "action": ""}\n'
            'more digging...\n'
            'AUDIT_RESULT: {"outcome": "graded", "issue": "b", "action": "c"}')
        self.assertEqual(out["outcome"], "graded")

    def test_bad_outcome_or_garbage_degrades_to_unparsed(self):
        self.assertEqual(
            parse_audit_result('AUDIT_RESULT: {"outcome": "victory"}')["outcome"],
            "unparsed")
        out = parse_audit_result("I could not finish the analysis")
        self.assertEqual(out["outcome"], "unparsed")
        self.assertIn("could not finish", out["issue"])


class RecordAttempt(unittest.TestCase):
    def test_terminal_outcome_parks_every_copy(self):
        state = {}
        parked = record_attempt(state, ["a", "b"], "legit_ungraded", attempt_cap=2)
        self.assertTrue(parked)
        self.assertTrue(state["a"]["parked"] and state["b"]["parked"])

    def test_retryable_outcome_parks_only_at_cap(self):
        state = {}
        self.assertFalse(
            record_attempt(state, ["a"], "fixed_needs_verify", attempt_cap=2))
        self.assertEqual(state["a"]["attempts"], 1)
        self.assertTrue(
            record_attempt(state, ["a"], "error", attempt_cap=2))
        self.assertIn("attempt cap", state["a"]["parked_reason"])


def dm_result(**kw):
    base = {"capper": "Cap", "desc": "Elks ML (-115)", "ref_date": "2026-09-07",
            "n_keys": 1, "outcome": "graded", "issue": "", "action": "",
            "commits": [], "parked": False}
    base.update(kw)
    return base


class ComposeDm(unittest.TestCase):
    def test_headline_bold_with_detail_in_expandable_quote(self):
        dm = compose_dm([dm_result(
            issue="cfl.ca went SPA & parser found <0> games",
            action="rewrote _parse_cfl_schedule", n_keys=2,
            commits=["abc"], parked=True)], [], run_date="2026-09-09")
        self.assertIn("✅ <b>Cap — Elks ML (-115)</b> (2026-09-07, ×2) "
                      "— graded · 1 commit(s) [parked]", dm)
        self.assertIn("<blockquote expandable>cfl.ca went SPA &amp; parser "
                      "found &lt;0&gt; games\n→ rewrote _parse_cfl_schedule"
                      "</blockquote>", dm)

    def test_no_quote_when_agent_had_nothing_to_say(self):
        dm = compose_dm([dm_result(outcome="no_issue", action="none")],
                        [], run_date="2026-09-09")
        self.assertIn("👌", dm)
        self.assertIn("already resolved", dm)
        self.assertNotIn("<blockquote", dm)

    def test_unknown_outcome_degrades_and_notes_are_escaped(self):
        dm = compose_dm([dm_result(outcome="exploded")],
                        ["pushed 4 commit(s) <fast & loose>"],
                        run_date="2026-09-09")
        self.assertIn("❓", dm)
        self.assertIn("exploded", dm)
        self.assertIn("pushed 4 commit(s) &lt;fast &amp; loose&gt;", dm)

    def test_static_footer_paths_are_gone(self):
        dm = compose_dm([dm_result()], [], run_date="2026-09-09")
        self.assertNotIn("ledger:", dm)
        self.assertNotIn("ungraded_audit_runs.jsonl", dm)


class Prompt(unittest.TestCase):
    def test_prompt_carries_contract_and_fanout_note(self):
        cache = {
            "-1002:10": entry(picks=[pick("Bears -3.5")]),
            "-1004:99": entry(picks=[pick("Bears -3.5")]),
        }
        group = scan(cache, {}, today_et=TODAY)[0]
        prompt = build_prompt(group, today_et=TODAY)
        self.assertTrue(prompt.startswith("/investigate "))
        for needle in ("-1002:10", "-1004:99", "fan-out copies",
                       "AUDIT_RESULT", "NEVER `git push`",
                       "t.me/c/2/10"):
            self.assertIn(needle, prompt)

    def test_single_copy_has_no_fanout_note(self):
        cache = {"-1002:10": entry(picks=[pick()])}
        group = scan(cache, {}, today_et=TODAY)[0]
        self.assertNotIn("fan-out copies", build_prompt(group, today_et=TODAY))


if __name__ == "__main__":
    unittest.main()
