#!/usr/bin/env python3
"""Pins the model escape hatch: watchdog bot /model + the launcher that obeys it.

The failure this guards against is the one that happened on 2026-09-11 — the
session hit "You've reached your Fable limit" and stayed there for fourteen
hours, because the model was hardcoded in run_claude_channels.sh and the only
tool that could reach it was an SSH shell. Two properties keep that from
recurring, and both are tested here:

  * a model the bot writes is the model the launcher starts with, and
  * anything the bot didn't write — junk, a half-written file, a CLI flag
    smuggled in as a model name — falls back to a working default instead of
    landing on the command line or wedging the start.

Runs on Windows (the bash half skips without bash).
"""
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
LAUNCHER = REPO / "deploy" / "run_claude_channels.sh"

# The bot refuses to import without a token; it never sends anything here.
os.environ.setdefault("WATCHDOG_BOT_TOKEN", "test-token")
os.environ.setdefault("WATCHDOG_USER_ID", "1")
sys.path.insert(0, str(REPO / "deploy"))
import claude_watchdog_bot as bot  # noqa: E402


class ResolveModelTest(unittest.TestCase):
    def test_aliases_resolve_to_full_ids(self):
        self.assertEqual(bot.resolve_model("opus"), "claude-opus-5")
        self.assertEqual(bot.resolve_model("OPUS"), "claude-opus-5")
        self.assertEqual(bot.resolve_model(" fable "), "claude-fable-5")

    def test_full_ids_pass_through(self):
        for raw in ("claude-opus-5", "claude-haiku-4-5-20251001", "claude-fable-5[1m]"):
            self.assertEqual(bot.resolve_model(raw), raw)

    def test_junk_is_refused(self):
        # A model name reaches a shell command line in the launcher, and a
        # leading dash would be read as a claude CLI flag, not a model.
        for raw in ("", "   ", "opus; rm -rf /", "$(id)", "a b", "-p",
                    "--dangerously-skip-permissions", "claude-opus-5`id`"):
            self.assertIsNone(bot.resolve_model(raw), raw)


class StateFileTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.state = Path(self.tmp.name) / ".claude-channels.env"
        self._saved, bot.STATE_FILE = bot.STATE_FILE, self.state
        self.addCleanup(self.tmp.cleanup)
        self.addCleanup(lambda: setattr(bot, "STATE_FILE", self._saved))

    def test_roundtrip_and_key_preservation(self):
        bot.write_state(CLAUDE_CHANNELS_MODEL="claude-opus-5")
        bot.write_state(CLAUDE_CHANNELS_EFFORT="max")
        self.assertEqual(
            bot.read_state(),
            {"CLAUDE_CHANNELS_MODEL": "claude-opus-5", "CLAUDE_CHANNELS_EFFORT": "max"},
        )

    def test_write_is_atomic(self):
        bot.write_state(CLAUDE_CHANNELS_MODEL="claude-opus-5")
        # A restart reading a leftover temp file would see a truncated model.
        self.assertEqual([p.name for p in Path(self.tmp.name).iterdir()], [self.state.name])

    def test_comments_and_junk_lines_ignored(self):
        self.state.write_text("# a comment\n\nnot-a-pair\nCLAUDE_CHANNELS_MODEL='claude-opus-5'\n")
        self.assertEqual(bot.read_state(), {"CLAUDE_CHANNELS_MODEL": "claude-opus-5"})

    def test_missing_file_reads_empty(self):
        self.assertEqual(bot.read_state(), {})


@unittest.skipUnless(shutil.which("bash"), "launcher resolution needs bash")
class LauncherResolutionTest(unittest.TestCase):
    """The launcher is the one place model resolution happens (--print-model)."""

    def resolve(self, body=None):
        with tempfile.TemporaryDirectory() as d:
            state = Path(d) / "state.env"
            if body is not None:
                state.write_text(body)
            env = dict(os.environ, CLAUDE_CHANNELS_STATE=state.as_posix())
            out = subprocess.run(
                ["bash", LAUNCHER.as_posix(), "--print-model"],
                capture_output=True, text=True, env=env, timeout=30,
            )
            self.assertEqual(out.returncode, 0, out.stderr)
            return dict(ln.split("=", 1) for ln in out.stdout.splitlines() if "=" in ln)

    def test_state_file_wins(self):
        got = self.resolve("CLAUDE_CHANNELS_MODEL=claude-opus-5\nCLAUDE_CHANNELS_EFFORT=high\n")
        self.assertEqual((got["model"], got["effort"]), ("claude-opus-5", "high"))

    def test_bot_written_file_is_read_back(self):
        # The two halves have to agree on the format, not just on the values.
        with tempfile.TemporaryDirectory() as d:
            state = Path(d) / "state.env"
            saved, bot.STATE_FILE = bot.STATE_FILE, state
            try:
                bot.write_state(CLAUDE_CHANNELS_MODEL="claude-haiku-4-5-20251001",
                                CLAUDE_CHANNELS_EFFORT="low")
            finally:
                bot.STATE_FILE = saved
            got = self.resolve(state.read_text())
        self.assertEqual((got["model"], got["effort"]), ("claude-haiku-4-5-20251001", "low"))

    def test_missing_file_falls_back(self):
        got = self.resolve(None)
        self.assertEqual((got["model"], got["effort"], got["source"]),
                         ("claude-opus-5", "max", "defaults"))

    def test_junk_never_reaches_the_command_line(self):
        for bad in ("claude-opus-5; rm -rf /", "$(id)", "-p", "--bare", ""):
            got = self.resolve(f"CLAUDE_CHANNELS_MODEL={bad}\n")
            self.assertEqual(got["model"], "claude-opus-5", bad)

    def test_bad_effort_falls_back_but_keeps_the_model(self):
        got = self.resolve("CLAUDE_CHANNELS_MODEL=claude-sonnet-5\nCLAUDE_CHANNELS_EFFORT=turbo\n")
        self.assertEqual((got["model"], got["effort"]), ("claude-sonnet-5", "max"))

    def test_one_million_context_suffix_survives(self):
        got = self.resolve("CLAUDE_CHANNELS_MODEL=claude-fable-5[1m]\n")
        self.assertEqual(got["model"], "claude-fable-5[1m]")

    def test_last_assignment_wins(self):
        got = self.resolve("CLAUDE_CHANNELS_MODEL=claude-sonnet-5\nCLAUDE_CHANNELS_MODEL=claude-opus-5\n")
        self.assertEqual(got["model"], "claude-opus-5")


if __name__ == "__main__":
    unittest.main()
