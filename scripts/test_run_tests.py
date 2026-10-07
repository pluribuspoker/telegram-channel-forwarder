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

D = rt.PLACEHOLDER
check("credentials are replaced by an inert placeholder for tests",
      env["FAKE_BOT_TOKEN"] == D and env["FAKE_HEALTHCHECK_URL"] == D
      and env["WATCHDOG_BOT_TOKEN"] == D, {k: env[k] for k in ("FAKE_BOT_TOKEN",)})
check("non-secret ids survive (modules int() them at import)",
      env["WATCHDOG_USER_ID"] not in ("", D))
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

moe = [n for n in rt.discover(None) if rt.MOE_TEST.match(n)]
check("MOE family is out of CI (operator 2026-10-07)",
      {"test_moe_god", "test_god_judge_runner", "test_intake_bot", "test_nfl_lines",
       "test_celebrity_grades", "test_pikkit_opinion_runner",
       "test_generate_moe_opinion_cli"} <= set(moe), moe)
check("…but the tracker's Pikkit splits and the watchdog relay stay in",
      not rt.MOE_TEST.match("test_pikkit") and not rt.MOE_TEST.match("test_pikkit_relay")
      and not rt.MOE_TEST.match("test_odds_watch"))

# ── selection: only the tests a change touches ──
import subprocess  # noqa: E402
repo = tmp / "sel"
(repo / "scripts").mkdir(parents=True)
(repo / "docs").mkdir()
(repo / "scripts" / "__init__.py").write_text("")
(repo / "b.py").write_text("X = 1\n")
(repo / "a.py").write_text("import b\n")
(repo / "c.py").write_text("Y = 2\n")
(repo / "run_y.sh").write_text("echo y\n")
(repo / "docs" / "n.md").write_text("doc\n")
(repo / "scripts" / "test_x.py").write_text("from a import *\n")
(repo / "scripts" / "test_y.py").write_text("import subprocess  # runs run_y.sh\n")
(repo / "scripts" / "helper.py").write_text("Z = 3\n")
(repo / "scripts" / "test_z.py").write_text("from scripts import helper\n")
for c in (["init", "-q"], ["add", "."]):
    subprocess.run(["git", "-C", str(repo), *c], check=True)
T = ["test_x", "test_y", "test_z"]
def sel(*changed):
    return rt.select_affected(T, list(changed), root=repo)
check("a transitive import selects the test", list(sel("b.py")[0]) == ["test_x"], sel("b.py"))
check("a file the test names (runner .sh) selects it", list(sel("run_y.sh")[0]) == ["test_y"])
check("`from scripts import helper` resolves", list(sel("scripts/helper.py")[0]) == ["test_z"])
check("editing the test itself selects it", list(sel("scripts/test_y.py")[0]) == ["test_y"])
check("docs select nothing and aren't 'uncovered'", sel("docs/n.md", "README.md") == ({}, []))
check("code no test reaches is reported uncovered", sel("c.py") == ({}, ["c.py"]))
check("--area matches test names and imported module paths",
      rt.area_tests(T, "helper", root=repo) == ["test_z"] and rt.area_tests(T, "x", root=repo) == ["test_x"])

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
