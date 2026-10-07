#!/usr/bin/env python3
"""Regression test: the CI runner's sandbox and timing logic (scripts/run_tests.py).

Offline, temp dirs only:

    python3 scripts/test_run_tests.py

Pins the 2026-10-07 lesson — a test that runs `sudo systemctl …` for real is
caught (blocked + marked LIVE), credentials reach no test, unittest files run
via -m unittest — plus the slower-than-usual and budget math the nightly DM
uses.
"""
import os
import shutil
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import run_tests as rt  # noqa: E402

fails = 0


def check(label, ok, detail=""):
    global fails
    print(("PASS " if ok else "FAIL ") + label + (f"  ({detail})" if detail and not ok else ""))
    fails += not ok


tmp = Path(tempfile.mkdtemp())
clone = tmp / "app"
(clone / "scripts").mkdir(parents=True)
(clone / "scripts" / "__init__.py").write_text("")
rt.make_shims(tmp / "shims")
os.environ["FAKE_BOT_TOKEN"] = "123:secret"
os.environ["FAKE_HEALTHCHECK_URL"] = "https://hc-ping.com/x"
os.environ["WATCHDOG_BOT_TOKEN"] = "9:bot"
os.environ["WATCHDOG_USER_ID"] = "42"
env = rt.app_env(tmp / "shims")

check("credentials are blanked for tests",
      env["FAKE_BOT_TOKEN"] == "" and env["FAKE_HEALTHCHECK_URL"] == ""
      and env["WATCHDOG_BOT_TOKEN"] == "", {k: env[k] for k in ("FAKE_BOT_TOKEN",)})
check("non-secret ids survive (modules int() them at import)", env["WATCHDOG_USER_ID"] == "42")
check("the shims come first on PATH", env["PATH"].split(os.pathsep)[0] == str(tmp / "shims"))

(clone / "scripts" / "test_sneaky.py").write_text(
    "import subprocess\nsubprocess.run(['sudo', '-n', 'systemctl', 'start', 'trent-repair.service'])\n")
r = rt.run_one(clone, "test_sneaky", env)
check("a test running sudo systemctl is blocked and marked LIVE",
      r["status"] == "live" and "systemctl start trent-repair" in r["tail"], r)
(clone / "scripts" / "test_claude.py").write_text(
    "import subprocess,sys\nsys.exit(subprocess.run(['claude', '-p', 'hi']).returncode and 0)\n")
check("a test spawning claude is caught even if it swallows the failure",
      rt.run_one(clone, "test_claude", env)["status"] == "live")
(clone / "scripts" / "test_ok.py").write_text("print('fine')\n")
check("a plain passing script passes", rt.run_one(clone, "test_ok", env)["status"] == "pass")
(clone / "scripts" / "test_unit.py").write_text(
    "import unittest\nclass T(unittest.TestCase):\n    def test_a(self):\n        self.assertTrue(True)\n")
check("a unittest.TestCase file runs via -m unittest and passes",
      rt.is_unittest(clone / "scripts" / "test_unit.py")
      and rt.run_one(clone, "test_unit", env)["status"] == "pass")
(clone / "scripts" / "test_bad.py").write_text("raise SystemExit(1)\n")
check("a failing script fails", rt.run_one(clone, "test_bad", env)["status"] == "fail")

hist = [{"ok": True, "secs": {"a": 2.0, "b": 10.0}} for _ in range(5)]
slow = rt.slower_tests({"secs": {"a": 9.0, "b": 14.0}}, hist)
check("slower = >2x the median AND +5 s", [s[0] for s in slow] == ["a"], slow)
check("no history → never 'slower'", rt.slower_tests({"secs": {"a": 99.0}}, []) == [])
check("per-test budget", rt.over_budget({"secs": {"a": rt.TEST_BUDGET + 1, "b": 1}})
      == [("a", rt.TEST_BUDGET + 1)])
msg = rt.nightly_message({"total_s": 100, "n": 2, "head": "abc1234", "secs": {"a": 1, "b": 1}},
                         [{"name": "a", "status": "pass"}, {"name": "b", "status": "pass"}],
                         hist, weekly=False)
check("an all-green, in-budget night sends no DM", msg is None, msg)

shutil.rmtree(tmp, ignore_errors=True)
print(f"\n{'OK' if not fails else f'{fails} FAILED'}")
sys.exit(1 if fails else 0)
