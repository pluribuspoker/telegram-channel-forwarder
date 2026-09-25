#!/usr/bin/env python3
"""Pins the phone-only Pikkit refresh relay (deploy/pikkit_relay.py) and the
watchdog bot's two commands built on it. Pure file state; no Telegram, no SSH.
"""

from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "deploy"))
os.environ.setdefault("WATCHDOG_BOT_TOKEN", "test-token")
os.environ.setdefault("WATCHDOG_USER_ID", "1")

import pikkit_relay as relay  # noqa: E402

T0 = 1_800_000_000.0


class RelayLifecycleTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_start_then_code_then_done(self):
        relay.write_heartbeat(T0 - 10, self.dir)
        started, reply = relay.start_request(T0, self.dir)
        self.assertTrue(started)
        self.assertIn("online", reply)
        self.assertNotIn("OFFLINE", reply)
        req = relay.read_request(self.dir)
        self.assertEqual(req["state"], "requested")

        # a second /pikkit while active does not clobber the request
        again, reply = relay.start_request(T0 + 5, self.dir)
        self.assertFalse(again)
        self.assertIn("Already in progress", reply)

        # the agent claims and reports the SMS; the operator relays the code
        relay.write_request(relay.advance(req, "sms_sent", T0 + 20), self.dir)
        ok, reply = relay.relay_code("58540736", T0 + 60, self.dir)
        self.assertTrue(ok, reply)
        req = relay.read_request(self.dir)
        self.assertEqual(req["state"], "code_relayed")
        self.assertEqual(req["code"], "58540736")

        # second code is refused, then the agent finishes
        ok, reply = relay.relay_code("58540736", T0 + 61, self.dir)
        self.assertFalse(ok)
        relay.write_request(relay.advance(req, "done", T0 + 90), self.dir)
        self.assertFalse(relay.is_active(relay.read_request(self.dir), T0 + 91))
        self.assertIn("done", relay.describe(relay.read_request(self.dir), T0 + 91))

    def test_code_needs_an_active_request_and_digits(self):
        ok, reply = relay.relay_code("58540736", T0, self.dir)
        self.assertFalse(ok)
        self.assertIn("send /pikkit first", reply)
        relay.start_request(T0, self.dir)
        ok, reply = relay.relay_code("abc", T0 + 1, self.dir)
        self.assertFalse(ok)
        self.assertIn("6-8 digits", reply)
        # the desktop hasn't even claimed it yet: a code is premature
        ok, reply = relay.relay_code("12345678", T0 + 2, self.dir)
        self.assertFalse(ok)
        self.assertIn("Not ready", reply)

    def test_stale_request_is_not_active_and_can_be_replaced(self):
        relay.start_request(T0, self.dir)
        self.assertTrue(relay.is_active(relay.read_request(self.dir), T0 + 30))
        self.assertFalse(relay.is_active(relay.read_request(self.dir), T0 + relay.REQUEST_TTL + 1))
        started, _ = relay.start_request(T0 + relay.REQUEST_TTL + 1, self.dir)
        self.assertTrue(started)

    def test_offline_agent_is_flagged_up_front(self):
        started, reply = relay.start_request(T0, self.dir)
        self.assertTrue(started)
        self.assertIn("never seen", reply)
        self.assertIn("desktop must be on", reply)
        relay.write_heartbeat(T0 - 3 * 3600, self.dir)
        online, line = relay.agent_status(T0, self.dir)
        self.assertFalse(online)
        self.assertIn("OFFLINE", line)
        self.assertIn("3h", line)

    def test_cancel(self):
        self.assertEqual(relay.cancel_request(T0, self.dir), "Nothing to cancel.")
        relay.start_request(T0, self.dir)
        self.assertIn("Cancelled", relay.cancel_request(T0 + 1, self.dir))
        self.assertEqual(relay.read_request(self.dir)["state"], "cancelled")
        self.assertFalse(relay.is_active(relay.read_request(self.dir), T0 + 2))

    def test_writes_are_atomic_and_junk_reads_as_none(self):
        (self.dir / relay.REQUEST_FILE).write_text("{not json")
        self.assertIsNone(relay.read_request(self.dir))
        relay.write_request({"state": "requested", "requested_at": T0}, self.dir)
        self.assertFalse((self.dir / (relay.REQUEST_FILE + ".tmp")).exists())
        self.assertEqual(relay.read_request(self.dir)["state"], "requested")


class BotCommandTests(unittest.TestCase):
    """The bot's handlers are thin wrappers; pin the wiring (`/pikkitcode`
    before `/pikkit`, cancel/status arguments) through its helper."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        import claude_watchdog_bot as bot
        self.bot = bot
        self._old = bot.relay.RELAY_DIR
        bot.PIKKIT_RELAY_DIR = self.dir

    def tearDown(self):
        self.tmp.cleanup()

    def test_pikkit_command_routing(self):
        reply, started = self.bot.pikkit_command("")
        self.assertTrue(started)
        self.assertIn("requested", reply.lower())
        reply, started = self.bot.pikkit_command("status")
        self.assertFalse(started)
        self.assertIn("waiting for the desktop", reply)
        reply, started = self.bot.pikkit_command("cancel")
        self.assertFalse(started)
        self.assertIn("Cancelled", reply)
        self.assertEqual(self.bot.pikkit_code_command("12345678"), "No Pikkit login in progress -- send /pikkit first.")

    def test_code_command_strips_formatting(self):
        self.bot.pikkit_command("")
        req = self.bot.relay.read_request(self.dir)
        self.bot.relay.write_request(self.bot.relay.advance(req, "sms_sent", req["requested_at"] + 5), self.dir)
        reply = self.bot.pikkit_code_command(" 5854 0736 ")
        self.assertIn("relayed", reply)
        self.assertEqual(self.bot.relay.read_request(self.dir)["code"], "58540736")


if __name__ == "__main__":
    unittest.main()
