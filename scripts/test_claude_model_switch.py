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

Since 2026-10-11 it also pins the restart default: a COMMANDED restart
(watchdog /restart, restart_and_ping.sh via `claude_models.py reset-state`)
lands on the machine default model/effort unless its args name one, the
bash/python default constants stay in step, and a typo'd arg aborts instead
of becoming a --model the CLI refuses (which boot-loops the service).

Runs on Windows (the bash half skips without bash).
"""
import asyncio
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

REPO = Path(__file__).resolve().parent.parent
LAUNCHER = REPO / "deploy" / "run_claude_channels.sh"

# The bot refuses to import without a token; it never sends anything here.
os.environ.setdefault("WATCHDOG_BOT_TOKEN", "test-token")
os.environ.setdefault("WATCHDOG_USER_ID", "1")
sys.path.insert(0, str(REPO / "deploy"))
import claude_models as models  # noqa: E402
import claude_watchdog_bot as bot  # noqa: E402


class ResolveModelTest(unittest.TestCase):
    def test_aliases_resolve_to_full_ids(self):
        self.assertEqual(bot.resolve_model("opus"), "claude-opus-5-5")
        self.assertEqual(bot.resolve_model("OPUS"), "claude-opus-5-5")
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
        # The helpers moved to claude_models (shared with reset-state); the
        # bot's imported names follow the module attribute.
        self._saved, models.STATE_FILE = models.STATE_FILE, self.state
        self.addCleanup(self.tmp.cleanup)
        self.addCleanup(lambda: setattr(models, "STATE_FILE", self._saved))

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


OLD_REPLY = """\
> /model claude-opus-5
  |- Set model to Opus 5 and saved as your default for new sessions

> /effort max
  |- Set effort level to max (this session only)
"""
NEW_ECHO = "\n> /model claude-opus-5\n"
NEW_REPLY = NEW_ECHO + "  |- Set model to Opus 5 and saved as your default for new sessions\n"


class PaneConfirmationTest(unittest.TestCase):
    """The pane is never a clean slate — the last switch's reply is still on it."""

    def setUp(self):
        self.frames = []
        self.typed = []
        saved = (bot._pane_capture, bot._pane_type, bot._pane_alive, bot.time.sleep)

        def advance(cmd):
            self.typed.append(cmd)

        self.calls = 0
        bot._pane_capture = lambda lines=500: self._capture()
        bot._pane_type = advance
        bot._pane_alive = lambda: True
        bot.time.sleep = lambda _s: None
        self.addCleanup(lambda: self._restore(saved))

    def _capture(self):
        frame = self.frames[min(self.calls, len(self.frames) - 1)]
        self.calls += 1
        return frame

    @staticmethod
    def _restore(saved):
        bot._pane_capture, bot._pane_type, bot._pane_alive, bot.time.sleep = saved

    def test_stale_reply_is_not_confirmation(self):
        # The exact 2026-09-11 miss: the same switch was issued minutes ago, so
        # "Set model to" is already on screen when the new one is still typing.
        self.frames = [OLD_REPLY]
        ok, _ = bot.pane_command("/model claude-opus-5", "set model to", timeout=0.2)
        self.assertFalse(ok)

    def test_new_reply_after_a_stale_one_confirms(self):
        self.frames = [OLD_REPLY, OLD_REPLY, OLD_REPLY + NEW_REPLY]
        ok, detail = bot.pane_command("/model claude-opus-5", "set model to", timeout=5)
        self.assertTrue(ok)
        # …and reports the new reply, not everything since the old echo.
        self.assertNotIn("/effort", detail)

    def test_queued_command_is_not_confirmed(self):
        # Claude mid-turn echoes the command and answers it later.
        self.frames = [OLD_REPLY, OLD_REPLY + NEW_ECHO]
        ok, _ = bot.pane_command("/model claude-opus-5", "set model to", timeout=0.2)
        self.assertFalse(ok)

    def test_first_ever_switch_confirms(self):
        self.frames = ["> some earlier work\n", "> some earlier work\n" + NEW_REPLY]
        ok, _ = bot.pane_command("/model claude-opus-5", "set model to", timeout=5)
        self.assertTrue(ok)

    def test_dead_pane_is_reported_not_typed_into(self):
        bot._pane_alive = lambda: False
        ok, detail = bot.pane_command("/model claude-opus-5", "set model to", timeout=5)
        self.assertFalse(ok)
        self.assertEqual(self.typed, [])
        self.assertIn("restart", detail)


class SwitchModelTest(unittest.TestCase):
    """Only a switch the CLI actually took may be written to the state file."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        saved = (models.STATE_FILE, bot.pane_command, bot.settings_model)
        models.STATE_FILE = Path(self.tmp.name) / "state.env"
        self.addCleanup(lambda: self._restore(saved))

    @staticmethod
    def _restore(saved):
        models.STATE_FILE, bot.pane_command, bot.settings_model = saved

    def test_confirmed_switch_persists(self):
        bot.pane_command = lambda *a, **k: (True, "Set model to Opus 5")
        bot.settings_model = lambda: "claude-opus-5"
        ok, _ = bot.switch_model("claude-opus-5")
        self.assertTrue(ok)
        self.assertEqual(bot.read_state()["CLAUDE_CHANNELS_MODEL"], "claude-opus-5")

    def test_unconfirmed_switch_persists_nothing(self):
        # A restart must not land on a model the CLI never agreed to.
        bot.pane_command = lambda *a, **k: (False, "pane busy")
        bot.settings_model = lambda: "claude-fable-5"
        ok, _ = bot.switch_model("claude-opus-5")
        self.assertFalse(ok)
        self.assertEqual(bot.read_state(), {})

    def test_late_acceptance_counts(self):
        # Queued mid-turn, run after the pane timeout: settings.json moved, and
        # the CLI writes it only for a model it accepted.
        models = iter(["claude-fable-5", "claude-opus-5"])
        bot.pane_command = lambda *a, **k: (False, "pane busy")
        bot.settings_model = lambda: next(models)
        ok, _ = bot.switch_model("claude-opus-5")
        self.assertTrue(ok)
        self.assertEqual(bot.read_state()["CLAUDE_CHANNELS_MODEL"], "claude-opus-5")

    def test_already_on_target_is_not_mistaken_for_a_switch(self):
        bot.pane_command = lambda *a, **k: (False, "pane busy")
        bot.settings_model = lambda: "claude-opus-5"
        ok, _ = bot.switch_model("claude-opus-5")
        self.assertFalse(ok)

    def test_effort_persists_even_unconfirmed(self):
        # Five closed values that cannot break a start, and the CLI keeps effort
        # session-only — the file is the only thing that carries it over.
        bot.pane_command = lambda *a, **k: (False, "pane busy")
        ok, _ = bot.switch_effort("high")
        self.assertFalse(ok)
        self.assertEqual(bot.read_state()["CLAUDE_CHANNELS_EFFORT"], "high")


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
            saved, models.STATE_FILE = models.STATE_FILE, state
            try:
                bot.write_state(CLAUDE_CHANNELS_MODEL="claude-haiku-4-5-20251001",
                                CLAUDE_CHANNELS_EFFORT="low")
            finally:
                models.STATE_FILE = saved
            got = self.resolve(state.read_text())
        self.assertEqual((got["model"], got["effort"]), ("claude-haiku-4-5-20251001", "low"))

    def test_missing_file_falls_back(self):
        got = self.resolve(None)
        self.assertEqual((got["model"], got["effort"], got["source"]),
                         (models.DEFAULT_MODEL, models.DEFAULT_EFFORT, "defaults"))

    def test_junk_never_reaches_the_command_line(self):
        for bad in ("claude-opus-5; rm -rf /", "$(id)", "-p", "--bare", ""):
            got = self.resolve(f"CLAUDE_CHANNELS_MODEL={bad}\n")
            self.assertEqual(got["model"], models.DEFAULT_MODEL, bad)

    def test_bad_effort_falls_back_but_keeps_the_model(self):
        got = self.resolve("CLAUDE_CHANNELS_MODEL=claude-sonnet-5\nCLAUDE_CHANNELS_EFFORT=turbo\n")
        self.assertEqual((got["model"], got["effort"]), ("claude-sonnet-5", models.DEFAULT_EFFORT))

    def test_one_million_context_suffix_survives(self):
        got = self.resolve("CLAUDE_CHANNELS_MODEL=claude-fable-5[1m]\n")
        self.assertEqual(got["model"], "claude-fable-5[1m]")

    def test_last_assignment_wins(self):
        got = self.resolve("CLAUDE_CHANNELS_MODEL=claude-sonnet-5\nCLAUDE_CHANNELS_MODEL=claude-opus-5\n")
        self.assertEqual(got["model"], "claude-opus-5")


class DefaultTwinsTest(unittest.TestCase):
    """One machine default, two languages — the launcher's bash fallback and
    claude_models' reset target must be the same values."""

    def test_launcher_defaults_match_claude_models(self):
        text = LAUNCHER.read_text()
        self.assertEqual(
            re.search(r'^DEFAULT_MODEL="([^"]*)"', text, re.M).group(1),
            models.DEFAULT_MODEL)
        self.assertEqual(
            re.search(r'^DEFAULT_EFFORT="([^"]*)"', text, re.M).group(1),
            models.DEFAULT_EFFORT)

    def test_default_survives_both_allowlists(self):
        # A default either half refuses would turn every reset into a fallback.
        self.assertEqual(bot.resolve_model(models.DEFAULT_MODEL), models.DEFAULT_MODEL)
        self.assertIn(models.DEFAULT_EFFORT, models.EFFORT_LEVELS)


class RestartArgsTest(unittest.TestCase):
    """A commanded restart lands on the default except the parts its args name."""

    def test_no_args_is_the_machine_default(self):
        self.assertEqual(models.classify_restart_args([]),
                         (models.DEFAULT_MODEL, models.DEFAULT_EFFORT))

    def test_order_free_overrides(self):
        want = ("claude-fable-5", "max")
        self.assertEqual(models.classify_restart_args(["fable", "max"]), want)
        self.assertEqual(models.classify_restart_args(["MAX", "Fable"]), want)

    def test_partial_override_keeps_the_other_default(self):
        self.assertEqual(models.classify_restart_args(["fable"]),
                         ("claude-fable-5", models.DEFAULT_EFFORT))
        self.assertEqual(models.classify_restart_args(["xhigh"]),
                         (models.DEFAULT_MODEL, "xhigh"))

    def test_full_ids_pass_but_bare_typos_refuse(self):
        # resolve_model admits any bare word (it reaches /model behind a
        # confirmation step); here a typo'd token would land unconfirmed on the
        # launcher's command line and boot-loop the service, so only aliases
        # and claude-… ids pass.
        self.assertEqual(models.classify_restart_args(["claude-fable-5[1m]"])[0],
                         "claude-fable-5[1m]")
        for bad in ("fablee", "opsu", "-p", "claude-opus-5;rm", "turbo"):
            with self.assertRaises(ValueError, msg=bad):
                models.classify_restart_args([bad])

    def test_conflicting_args_refuse(self):
        for toks in (["fable", "opus"], ["high", "max"]):
            with self.assertRaises(ValueError):
                models.classify_restart_args(toks)


class ResetStateCliTest(unittest.TestCase):
    """`claude_models.py reset-state` — how the restart scripts write the file."""

    def cli(self, *args, state):
        env = dict(os.environ, CLAUDE_CHANNELS_STATE=str(state))
        return subprocess.run(
            [sys.executable, str(REPO / "deploy" / "claude_models.py"),
             "reset-state", *args],
            capture_output=True, text=True, env=env, timeout=30,
        )

    @staticmethod
    def read(state: Path) -> dict:
        return dict(
            ln.split("=", 1) for ln in state.read_text().splitlines()
            if ln and not ln.startswith("#") and "=" in ln
        )

    def test_no_args_writes_the_default(self):
        with tempfile.TemporaryDirectory() as d:
            state = Path(d) / "state.env"
            out = self.cli(state=state)
            self.assertEqual(out.returncode, 0, out.stderr)
            self.assertEqual(self.read(state), {
                "CLAUDE_CHANNELS_MODEL": models.DEFAULT_MODEL,
                "CLAUDE_CHANNELS_EFFORT": models.DEFAULT_EFFORT,
            })

    def test_stated_override_is_written(self):
        with tempfile.TemporaryDirectory() as d:
            state = Path(d) / "state.env"
            out = self.cli("max", "fable", state=state)
            self.assertEqual(out.returncode, 0, out.stderr)
            got = self.read(state)
            self.assertEqual(got["CLAUDE_CHANNELS_MODEL"], "claude-fable-5")
            self.assertEqual(got["CLAUDE_CHANNELS_EFFORT"], "max")

    def test_typo_aborts_and_writes_nothing(self):
        with tempfile.TemporaryDirectory() as d:
            state = Path(d) / "state.env"
            out = self.cli("opsu", state=state)
            self.assertNotEqual(out.returncode, 0)
            self.assertFalse(state.exists())
            self.assertIn("Unknown arg", out.stderr)


class _Msg:
    def __init__(self, text):
        self.text = text
        self.from_user = SimpleNamespace(id=1)
        self.replies, self.reactions = [], []

    async def reply_text(self, text, **kw):
        self.replies.append(text)

    async def set_reaction(self, emoji):
        self.reactions.append(emoji)


class RestartCommandTest(unittest.TestCase):
    """The watchdog /restart: reset-to-default before the systemctl call, and a
    bad arg aborts with nothing written, nothing restarted."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.state = Path(self.tmp.name) / "state.env"
        self.cmds = []
        # Pin the allowed id too: the sandboxed suite carries the real
        # WATCHDOG_USER_ID in its env, so the import-time setdefault loses.
        saved = (models.STATE_FILE, bot.run, bot._report_restart,
                 bot.ALLOWED_USER_ID)
        models.STATE_FILE = self.state
        bot.ALLOWED_USER_ID = 1
        bot.run = lambda cmd, timeout=30: (self.cmds.append(cmd), "")[1]

        async def report(msg, out, budget=60):
            pass

        bot._report_restart = report
        self.addCleanup(lambda: self._restore(saved))

    @staticmethod
    def _restore(saved):
        (models.STATE_FILE, bot.run, bot._report_restart,
         bot.ALLOWED_USER_ID) = saved

    def send(self, text):
        msg = _Msg(text)
        asyncio.run(bot.handle_message(SimpleNamespace(message=msg), None))
        return msg

    def test_bare_restart_resets_to_the_default(self):
        msg = self.send("/restart")
        self.assertEqual(models.read_state(), {
            "CLAUDE_CHANNELS_MODEL": models.DEFAULT_MODEL,
            "CLAUDE_CHANNELS_EFFORT": models.DEFAULT_EFFORT,
        })
        self.assertTrue(any("systemctl restart" in c for c in self.cmds), self.cmds)
        self.assertEqual(msg.reactions, ["⚡"])

    def test_stated_args_override_the_default(self):
        msg = self.send("/restart max fable")
        got = models.read_state()
        self.assertEqual(got["CLAUDE_CHANNELS_MODEL"], "claude-fable-5")
        self.assertEqual(got["CLAUDE_CHANNELS_EFFORT"], "max")
        self.assertTrue(any("claude-fable-5" in r for r in msg.replies), msg.replies)

    def test_bad_arg_aborts_before_anything_happens(self):
        msg = self.send("/restart opsu")
        self.assertFalse(self.state.exists())
        self.assertEqual(self.cmds, [])
        self.assertEqual(msg.reactions, [])
        self.assertTrue(any("Unknown arg" in r for r in msg.replies), msg.replies)


if __name__ == "__main__":
    unittest.main()
