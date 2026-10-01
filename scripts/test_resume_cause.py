"""The resume ping's cause line (deploy/hooks/telegram_resume_notify.py classify).

Silent when the operator asked for the restart (watchdog bot, chat self-restart);
otherwise one short label built from the stop-context line + launcher reasons.

    python3 scripts/test_resume_cause.py
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "deploy", "hooks"))
from telegram_resume_notify import classify  # noqa: E402

START = "claude-channels: starting model=claude-opus-5-5 effort=high (from x)"


def stop(result="success", exit_="killed/TERM", req="none found"):
    return (f"stop-context: result={result} exit={exit_} requester={req} "
            "mem avail=268MB swap=1015/2047MB top: telegram-intake 674MB")


def run(*middle, uptime=10**6, upgrades=()):
    return classify([START, *middle, START], uptime, list(upgrades))


WATCHDOG = ("systemctl restart claude-channels.service < sudo -n systemctl restart "
            "claude-channels.service < /bin/sh -c sudo -n systemctl restart claude-c "
            "< /home/forwarder/venv/bin/python /home/forwarder/app/deploy/claude_watchdog_bot.py")
SELF = ("systemctl restart claude-channels.service < /bin/bash "
        "/home/forwarder/app/deploy/restart_and_ping.sh 5911202683 1355")
APT = ("systemctl restart claude-channels.service < /usr/bin/perl /usr/sbin/needrestart "
       "< /bin/sh -c /usr/sbin/needrestart < /usr/bin/python3 /usr/bin/unattended-upgrade")
HC = ("systemctl restart claude-channels.service < sudo -n systemctl restart claude-channels "
      "< claude -p /investigate hc < /usr/bin/python3 /home/forwarder/app/scripts/hc_repair.py")
SSH = ("systemctl restart claude-channels.service < sudo -n systemctl restart claude-channels "
       "< bash -c cd ~/app && sudo -n systemctl < su - forwarder -c ... < sshd: root@notty")


class ResumeCauseTests(unittest.TestCase):
    def test_operator_restarts_are_silent(self):
        self.assertIsNone(run(stop(req=WATCHDOG)))
        self.assertIsNone(run(stop(req=SELF)))

    def test_ubuntu_update_names_packages(self):
        self.assertEqual(run(stop(req=APT), upgrades=["libevent-core-2.1-7t64", "openssl"]),
                         "Ubuntu auto-update (libevent-core-2.1-7t64, openssl)")
        self.assertEqual(run(stop(req=APT)), "Ubuntu auto-update")
        self.assertTrue(run(stop(req=APT), upgrades=list("abcd")).endswith("(a, b, c …)"))

    def test_agent_beats_generic_claude_session(self):
        self.assertEqual(run(stop(req=HC)), "restarted by the hc-repair agent")

    def test_ssh(self):
        self.assertEqual(run(stop(req=SSH)), "manual restart over SSH")

    def test_unknown_requester_shows_first_real_hop(self):
        req = "systemctl restart claude-channels.service < /usr/bin/python3 /opt/thing/run.py"
        self.assertEqual(run(stop(req=req)), "restarted by: /usr/bin/python3 /opt/thing/run.py")

    def test_crash_with_launcher_reason(self):
        self.assertEqual(
            run("claude-channels: bun/telegram plugin gone, exiting for restart",
                stop("exit-code", "exited/1", "none (self-exit)")),
            "crashed: bun/telegram plugin gone · RAM 268MB free")
        self.assertEqual(
            run("Failed to invoke barrier: Connection timed out",
                stop("exit-code", "exited/1", "none (self-exit)")),
            "crashed: watchdog ping timed out · RAM 268MB free")

    def test_watchdog_and_oom(self):
        self.assertTrue(run(stop("watchdog", "killed/ABRT", "none (self-exit)")).startswith("hung"))
        self.assertTrue(run(stop("oom-kill", "killed/KILL", "none (self-exit)")).startswith("OOM"))

    def test_reason_from_an_earlier_restart_is_not_reused(self):
        lines = ["claude-channels: claude process gone, exiting for restart",
                 stop("exit-code", "exited/1", "none (self-exit)"), START,
                 stop(req=APT), START]
        self.assertEqual(classify(lines, 10**6, []), "Ubuntu auto-update")

    def test_no_stop_line(self):
        self.assertIsNone(run())
        self.assertEqual(classify([START], 120, []), "VPS rebooted")
        self.assertIsNone(classify([], 120, []))


if __name__ == "__main__":
    unittest.main()
