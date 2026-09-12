#!/usr/bin/env python3
"""Pins the credit-limit alarm: what counts as blocked, and when it is worth saying.

The failure this watches for went unseen for fourteen hours on 2026-09-11, so
the alarm itself has to be trustworthy in both directions. Two ways it could
fail and be worse than nothing:

  * crying wolf — a 429 burst, a 5xx, or a dropped connection paged as an
    outage, or the same alert repeated until it gets muted; then the real one
    is invisible too, which is exactly what happened to the odds quota alert.
  * going quiet — a network blip between two blocked probes clearing the
    recorded state, so the next pass reads as "unchanged" and never alerts.

Runs on Windows: nothing here touches the network or Telegram.
"""
import os
import sys
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
os.environ.setdefault("WATCHDOG_BOT_TOKEN", "test-token")
os.environ.setdefault("WATCHDOG_USER_ID", "1")
sys.path.insert(0, str(REPO / "deploy"))
import claude_limit_watchdog as lw  # noqa: E402


class ClassifyTest(unittest.TestCase):
    def test_a_served_request_is_ok(self):
        self.assertEqual(lw.classify(200, "")[0], "ok")

    def test_a_refusal_is_blocked_and_keeps_the_reason(self):
        cond, detail = lw.classify(429, "You've reached your Fable limit.")
        self.assertEqual(cond, "blocked")
        self.assertIn("Fable limit", detail)

    def test_credentials_are_the_other_watchdogs_alarm(self):
        # Two alarms for one fault is how both end up muted.
        for status in (401, 403):
            self.assertEqual(lw.classify(status, "invalid")[0], "auth")

    def test_outages_and_overloads_are_not_an_account_problem(self):
        for status in (500, 502, 529):
            self.assertEqual(lw.classify(status, "overloaded")[0], "unknown")

    def test_an_unexpected_status_never_reads_as_blocked(self):
        self.assertEqual(lw.classify(404, "model not found")[0], "unknown")


class DecideTest(unittest.TestCase):
    """The alerting rule: state changes are news, repetition is not."""

    def setUp(self):
        self.blocked = ("blocked", "claude-fable-5")
        self.addCleanup(setattr, lw, "REMIND_SECS", lw.REMIND_SECS)

    def test_first_block_alerts(self):
        alert, state = lw.decide(*self.blocked, {}, False, 1000.0)
        self.assertTrue(alert)
        self.assertEqual(state["condition"], "blocked")
        self.assertEqual(state["alert_at"], 1000.0)

    def test_the_same_block_does_not_alert_again(self):
        _, state = lw.decide(*self.blocked, {}, False, 1000.0)
        alert, _ = lw.decide(*self.blocked, state, False, 9999.0)
        self.assertFalse(alert)

    def test_a_different_model_going_out_is_news(self):
        # Switched to Opus after Fable ran out, and Opus is out too: the
        # operator's next tap depends on knowing that.
        _, state = lw.decide(*self.blocked, {}, False, 1000.0)
        alert, _ = lw.decide("blocked", "claude-opus-5", state, False, 1100.0)
        self.assertTrue(alert)

    def test_recovery_is_announced_once(self):
        _, state = lw.decide(*self.blocked, {}, False, 1000.0)
        alert, state = lw.decide("ok", "claude-opus-5", state, False, 1100.0)
        self.assertTrue(alert)
        alert, _ = lw.decide("ok", "claude-opus-5", state, False, 1200.0)
        self.assertFalse(alert)

    def test_a_healthy_run_from_a_clean_slate_says_nothing(self):
        alert, _ = lw.decide("ok", "claude-opus-5", {}, False, 1000.0)
        self.assertFalse(alert)

    def test_a_blip_between_two_blocks_neither_alerts_nor_clears(self):
        # A 5xx or a dropped connection must leave the recorded block intact,
        # or the next pass reads as a change and re-alerts - and a cleared
        # state would also make a still-blocked session look like news later.
        _, state = lw.decide(*self.blocked, {}, False, 1000.0)
        alert, after_blip = lw.decide("unknown", "claude-fable-5", state, False, 1100.0)
        self.assertFalse(alert)
        self.assertIs(after_blip, state)
        alert, _ = lw.decide(*self.blocked, after_blip, False, 1200.0)
        self.assertFalse(alert)

    def test_auth_failures_leave_the_state_alone(self):
        alert, state = lw.decide("auth", "claude-opus-5", {}, False, 1000.0)
        self.assertFalse(alert)
        self.assertEqual(state, {})

    def test_force_says_it_regardless(self):
        _, state = lw.decide("ok", "claude-opus-5", {}, False, 1000.0)
        self.assertTrue(lw.decide("ok", "claude-opus-5", state, True, 1100.0)[0])

    def test_reminders_are_off_unless_asked_for(self):
        _, state = lw.decide(*self.blocked, {}, False, 1000.0)
        self.assertFalse(lw.decide(*self.blocked, state, False, 10**9)[0])
        lw.REMIND_SECS = 3600
        self.assertTrue(lw.decide(*self.blocked, state, False, 1000.0 + 3601)[0])
        self.assertFalse(lw.decide(*self.blocked, state, False, 1000.0 + 60)[0])


class ComposeTest(unittest.TestCase):
    """The message has to be actionable from a phone, mid-outage."""

    def test_the_alert_names_a_model_that_actually_works(self):
        msg = lw.compose("blocked", "claude-fable-5", "You've reached your Fable limit.",
                         ["opus", "sonnet"])
        self.assertIn("claude-fable-5 (fable)", msg)
        self.assertIn("Fable limit", msg)
        self.assertIn("/model opus", msg)

    def test_an_account_wide_outage_does_not_suggest_a_pointless_tap(self):
        msg = lw.compose("blocked", "claude-opus-5", "refused", [])
        self.assertNotIn("/model", msg)
        self.assertIn("account-wide", msg)

    def test_recovery_names_the_model_that_came_back(self):
        msg = lw.compose("ok", "claude-opus-5", "serving", [])
        self.assertIn("serving again", msg)
        self.assertIn("claude-opus-5 (opus)", msg)


class SharedTableTest(unittest.TestCase):
    """The watchdog may only offer models the bot can actually switch to."""

    def test_alternatives_come_from_the_bot_s_own_table(self):
        import claude_models as cm
        probed = []
        saved = lw.probe
        lw.probe = lambda _t, model: (probed.append(model), ("ok", ""))[1]
        self.addCleanup(lambda: setattr(lw, "probe", saved))
        usable = lw.alternatives("token", "claude-fable-5")
        self.assertEqual(set(probed), set(cm.MODEL_CHOICES.values()) - {"claude-fable-5"})
        self.assertNotIn("fable", usable)
        for alias in usable:
            self.assertIn(alias, cm.MODEL_CHOICES)


if __name__ == "__main__":
    unittest.main()
