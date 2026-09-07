#!/usr/bin/env python3
"""Tests for the headless God Expert judge runner against a stubbed claude."""

from __future__ import annotations

import contextlib
import io
import json
import os
import shutil
import sys
import tempfile
import unittest
from datetime import timedelta
from pathlib import Path
from unittest.mock import patch

from moe import approved_opinions, load_expert, opinion_output_sha256
from moe_ak import _parse_time
from moe_god import (
    aggregator_policy,
    build_aggregator_input,
    build_judge_request,
    load_registry,
)
from nfl_lines import LATEST_HOME_COLUMN
from scripts.god_judge_runner import (
    RESPONSE_INSTRUCTION,
    ClaudeHeadlessInvoker,
    JudgeCallError,
    main,
    row_committee_key,
    run_once,
)
from scripts.test_moe_god import (
    EVENT_ID,
    KICKOFF,
    MemoryStore,
    _committee,
    _game,
)

# A stand-in for the claude binary: records argv, env, cwd and stdin next to
# itself, then answers with a print-mode JSON envelope. A "mode" file beside
# it selects the failure to simulate.
STUB_SOURCE = '''#!{python}
import json, os, sys

here = os.path.dirname(os.path.abspath(__file__))
stdin_text = sys.stdin.read()
mode_path = os.path.join(here, "mode")
mode = open(mode_path).read().strip() if os.path.exists(mode_path) else "valid"
record = {{
    "argv": sys.argv,
    "env": dict(os.environ),
    "cwd": os.getcwd(),
    "cwd_entries": sorted(os.listdir(os.getcwd())),
    "stdin": stdin_text,
    "mode": mode,
}}
with open(os.path.join(here, "calls.jsonl"), "a", encoding="utf-8") as handle:
    handle.write(json.dumps(record) + "\\n")
if mode == "crash":
    sys.stderr.write("stub crashed\\n")
    sys.exit(3)
request = json.loads(stdin_text[: stdin_text.rindex("}}") + 1])
labels = [voice["label"] for voice in request["voices"]]
if mode == "invalid":
    response = {{"home_win_probability": 0.5}}
else:
    response = {{
        "home_win_probability": 0.61,
        "expected_home_margin": 3.0,
        "projected_total": 45.0,
        "key_reasons": [
            {{"voice": labels[0], "text": "Broad cohort, no single-game claims."}},
            {{"voice": "pool", "text": "The pool already sits close to the market."}},
        ],
        "counterpoints": [{{"voice": labels[1], "text": "Leans on one meeting."}}],
        "discarded_considerations": [],
    }}
envelope = {{
    "type": "result",
    "subtype": "success",
    "is_error": mode == "is_error",
    "duration_ms": 1,
    "duration_api_ms": 1,
    "num_turns": 1,
    "total_cost_usd": 0,
    "usage": {{"input_tokens": 10, "output_tokens": 5}},
    "session_id": "stub-session",
    "result": "" if mode == "empty" else json.dumps(response),
}}
print(json.dumps(envelope))
'''


def _slate(count: int) -> tuple[list[dict], list[dict]]:
    """``count`` games on consecutive days, each with a full committee."""
    games: list[dict] = []
    rows: list[dict] = []
    for index in range(count):
        event_id = f"slate-{index}"
        kickoff = f"2026-09-{13 + index:02d}T17:00:00+00:00"
        games.append(_game(event_id=event_id, kickoff=kickoff))
        for base in _committee():
            row = dict(base)
            row.update(
                {
                    "opinion_id": f"{base['opinion_id']}-{event_id}",
                    "event_id": event_id,
                    "commence_time_utc": kickoff,
                }
            )
            digest = opinion_output_sha256(row)
            row["output_sha256"] = digest
            row["approved_output_sha256"] = (
                digest if row["review_status"] == "approved" else ""
            )
            rows.append(row)
    return games, rows


class RunnerHarness:
    """A stub claude binary, a memory store, captured DMs, a runs log."""

    def __init__(self) -> None:
        self.root = Path(tempfile.mkdtemp(prefix="god-judge-test-"))
        self.stub = self.root / "claude"
        self.stub.write_text(
            STUB_SOURCE.format(python=sys.executable), encoding="utf-8"
        )
        self.stub.chmod(0o755)
        self.work_root = self.root / "work"
        self.work_root.mkdir()
        self.runs_log = self.root / "logs" / "runs.jsonl"
        self.store = MemoryStore()
        self.notifications: list[str] = []
        self.invoker = ClaudeHeadlessInvoker(
            self.stub, oauth_token="test-oauth-token"
        )
        self.registry = load_registry()
        self.policy = aggregator_policy(self.registry)

    def set_mode(self, mode: str) -> None:
        (self.root / "mode").write_text(mode, encoding="utf-8")

    def calls(self) -> list[dict]:
        path = self.root / "calls.jsonl"
        if not path.exists():
            return []
        return [
            json.loads(line)
            for line in path.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]

    def runs(self) -> list[dict]:
        if not self.runs_log.exists():
            return []
        return [
            json.loads(line)
            for line in self.runs_log.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]

    async def run(self, games: list[dict], rows: list[dict], **kwargs) -> dict:
        kwargs.setdefault("now", _parse_time(KICKOFF) - timedelta(days=2))
        self.output = io.StringIO()
        with contextlib.redirect_stdout(self.output):
            return await run_once(
                games=games,
                # The store holds what earlier passes persisted, like the sheet.
                opinion_rows=[*rows, *self.store.rows],
                finals=[],
                snapshots=[],
                store=self.store,
                registry=self.registry,
                policy=self.policy,
                claude_invoker=self.invoker,
                notify=self.notifications.append,
                work_root=self.work_root,
                runs_log_path=self.runs_log,
                **kwargs,
            )

    def cleanup(self) -> None:
        shutil.rmtree(self.root, ignore_errors=True)


class RunnerTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.harness = RunnerHarness()
        self.addCleanup(self.harness.cleanup)
        # The runner's own environment holds these; the claude call must not.
        secrets = patch.dict(
            os.environ,
            {
                "GOOGLE_CREDENTIALS": '{"type": "service_account"}',
                "NFL_INTAKE_SHEET_ID": "sheet-id",
                "ANTHROPIC_API_KEY": "sk-ant-test",
            },
        )
        secrets.start()
        self.addCleanup(secrets.stop)

    def _payload(self, game: dict | None = None, rows: list[dict] | None = None) -> dict:
        return build_aggregator_input(
            game or _game(),
            approved_opinions=approved_opinions(rows or _committee()),
            finals=[],
            snapshots=[],
            registry=self.harness.registry,
            policy=self.harness.policy,
        )

    async def test_first_pass_persists_both_arms_through_the_stub(self) -> None:
        summary = await self.harness.run([_game()], _committee())

        rows = self.harness.store.rows
        self.assertEqual([row["expert_id"] for row in rows], ["god_rules", "god_judge"])
        rules, judge = rows
        self.assertEqual(rules["generation_backend"], "deterministic")
        self.assertEqual(rules["generation_status"], "valid")
        self.assertEqual(judge["model"], "claude-fable-5-1")
        self.assertEqual(judge["generation_backend"], "claude_headless")
        self.assertEqual(judge["generation_effort"], "max")
        self.assertEqual(judge["generation_status"], "valid")
        self.assertEqual(judge["review_status"], "pending")
        payload = self._payload()
        request = build_judge_request(payload)
        judge_input = json.loads(judge["input_json"])
        self.assertEqual(judge_input["committee_key"], payload["committee_key"])
        self.assertEqual(judge_input["aggregator_input_sha256"], rules["input_sha256"])
        self.assertEqual(json.loads(rules["input_json"])["committee_key"], payload["committee_key"])
        self.assertEqual(len(summary["attempted"]), 1)
        self.assertEqual(summary["attempted"][0]["judge_opinion_id"], judge["opinion_id"])

        # What the stub saw: the registered prompt, the request, an empty
        # cwd inside the temp dir, and an environment without secrets.
        calls = self.harness.calls()
        self.assertEqual(len(calls), 1)
        call = calls[0]
        argv = call["argv"]
        self.assertEqual(argv[argv.index("--system-prompt") + 1], load_expert("god_judge")["prompt_text"])
        for flag in ("-p", "--bare", "--strict-mcp-config", "--no-session-persistence"):
            self.assertIn(flag, argv)
        self.assertEqual(argv[argv.index("--model") + 1], "claude-fable-5-1")
        self.assertEqual(argv[argv.index("--effort") + 1], "max")
        self.assertEqual(argv[argv.index("--tools") + 1], "")
        self.assertEqual(argv[argv.index("--output-format") + 1], "json")
        self.assertTrue(call["stdin"].startswith(json.dumps(request, indent=2, sort_keys=True)))
        self.assertTrue(call["stdin"].rstrip().endswith(RESPONSE_INSTRUCTION))
        self.assertEqual(call["cwd_entries"], [])
        self.assertEqual(Path(call["cwd"]).resolve().parent.parent, self.harness.work_root.resolve())
        env = call["env"]
        for secret in ("GOOGLE_CREDENTIALS", "NFL_INTAKE_SHEET_ID", "ANTHROPIC_API_KEY"):
            self.assertNotIn(secret, env)
        self.assertEqual(env["CLAUDE_CODE_OAUTH_TOKEN"], "test-oauth-token")
        self.assertEqual(env["CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC"], "1")
        self.assertEqual(env["TERM"], "dumb")
        self.assertEqual(env["LANG"], "C.UTF-8")
        self.assertEqual(list(self.harness.work_root.iterdir()), [])

        # The DM names both rows and gives the review commands.
        self.assertEqual(len(self.harness.notifications), 1)
        message = self.harness.notifications[0]
        self.assertTrue(message.startswith("pickbot: new God Expert rows pending for New England Patriots @ Seattle Seahawks"))
        self.assertIn(f"rules {rules['opinion_id']}, judge {judge['opinion_id']}", message)
        for row in rows:
            self.assertIn(
                f"python scripts/review_moe_opinion.py --opinion-id {row['opinion_id']} --status approved --reviewed-by <you>",
                message,
            )

        # One usage line per call.
        runs = self.harness.runs()
        self.assertEqual(len(runs), 1)
        self.assertEqual(runs[0]["kind"], "claude_call")
        self.assertEqual(runs[0]["status"], "ok")
        self.assertEqual(runs[0]["event_id"], EVENT_ID)
        self.assertEqual(runs[0]["committee_key"], payload["committee_key"])
        self.assertEqual(runs[0]["duration_ms"], 1)
        self.assertEqual(runs[0]["num_turns"], 1)
        self.assertEqual(runs[0]["total_cost_usd"], 0)
        self.assertEqual(runs[0]["usage"]["output_tokens"], 5)
        self.assertEqual(runs[0]["exit_code"], 0)

    async def test_unchanged_committee_is_not_rerun(self) -> None:
        await self.harness.run([_game()], _committee())
        summary = await self.harness.run([_game()], _committee())

        self.assertEqual(len(self.harness.store.rows), 2)
        self.assertEqual(len(self.harness.calls()), 1)
        self.assertEqual(len(self.harness.notifications), 1)
        self.assertEqual(summary["attempted"], [])
        self.assertIn("valid judge row already carries", summary["skipped"][0]["reason"])

    async def test_price_move_makes_a_new_committee_key(self) -> None:
        await self.harness.run([_game()], _committee())
        moved = _game()
        moved[LATEST_HOME_COLUMN] = "-3.5,-105,-181|-2.5,-115,-155|-0.5,115,-140"
        self.assertNotEqual(self._payload(moved)["committee_key"], self._payload()["committee_key"])

        summary = await self.harness.run([moved], _committee())

        self.assertEqual(len(summary["attempted"]), 1)
        self.assertEqual(
            [row["expert_id"] for row in self.harness.store.rows],
            ["god_rules", "god_judge", "god_rules", "god_judge"],
        )
        self.assertEqual(len(self.harness.calls()), 2)
        keys = {json.loads(row["input_json"])["committee_key"] for row in self.harness.store.rows}
        self.assertEqual(len(keys), 2)

    async def test_kickoff_cutoff_skips_the_game(self) -> None:
        summary = await self.harness.run(
            [_game()], _committee(), now=_parse_time(KICKOFF) - timedelta(hours=1, minutes=59)
        )

        self.assertEqual(self.harness.store.rows, [])
        self.assertEqual(self.harness.calls(), [])
        self.assertEqual(self.harness.notifications, [])
        self.assertIn("kickoff cutoff", summary["skipped"][0]["reason"])

    async def test_incomplete_committee_is_skipped(self) -> None:
        rows = [row for row in _committee() if row["expert_id"] != "win_total"]

        summary = await self.harness.run([_game()], rows)

        self.assertEqual(self.harness.store.rows, [])
        self.assertEqual(self.harness.calls(), [])
        self.assertIn("win_total", summary["skipped"][0]["reason"])

    async def test_max_games_caps_the_slate_earliest_first(self) -> None:
        games, rows = _slate(3)

        summary = await self.harness.run(list(reversed(games)), rows, max_games=2)

        self.assertEqual([item["event_id"] for item in summary["attempted"]], ["slate-0", "slate-1"])
        self.assertEqual(len(self.harness.calls()), 2)
        self.assertEqual(len(self.harness.store.rows), 4)
        summary = await self.harness.run(games, rows, max_games=2)
        self.assertEqual([item["event_id"] for item in summary["attempted"]], ["slate-2"])
        self.assertEqual(len(self.harness.store.rows), 6)

    async def test_invalid_response_persists_an_audit_row(self) -> None:
        self.harness.set_mode("invalid")

        summary = await self.harness.run([_game()], _committee())

        statuses = [(row["expert_id"], row["generation_status"]) for row in self.harness.store.rows]
        self.assertEqual(statuses, [("god_rules", "valid"), ("god_judge", "invalid")])
        self.assertEqual(self.harness.store.rows[1]["review_status"], "not_applicable")
        self.assertEqual(self.harness.store.rows[1]["generation_backend"], "claude_headless")
        self.assertEqual(summary["failed"][0]["stage"], "judge_validation")
        self.assertEqual(self.harness.notifications, [])
        self.assertEqual(list(self.harness.work_root.iterdir()), [])
        self.assertEqual(self.harness.runs()[0]["status"], "ok")

    async def test_two_invalid_rows_stop_further_attempts(self) -> None:
        self.harness.set_mode("invalid")
        await self.harness.run([_game()], _committee())
        await self.harness.run([_game()], _committee())
        self.assertEqual(len(self.harness.calls()), 2)
        self.assertEqual(
            [row["expert_id"] for row in self.harness.store.rows],
            ["god_rules", "god_judge", "god_judge"],
        )

        summary = await self.harness.run([_game()], _committee())

        self.assertEqual(len(self.harness.calls()), 2)
        self.assertEqual(summary["attempted"], [])
        self.assertEqual(len(summary["stalled"]), 1)
        self.assertIn("failed validation 2 times", summary["skipped"][0]["reason"])
        self.assertEqual(len(self.harness.notifications), 1)
        self.assertIn("failed validation twice", self.harness.notifications[0])
        self.assertIn("New England Patriots @ Seattle Seahawks", self.harness.notifications[0])

    async def test_failed_claude_call_is_logged_and_dmed_without_a_row(self) -> None:
        self.harness.set_mode("crash")

        summary = await self.harness.run([_game()], _committee())

        self.assertEqual([row["expert_id"] for row in self.harness.store.rows], ["god_rules"])
        self.assertEqual(summary["failed"][0]["stage"], "claude")
        self.assertIn("exited 3", summary["failed"][0]["error"])
        self.assertEqual(len(self.harness.notifications), 1)
        self.assertTrue(self.harness.notifications[0].startswith("pickbot: God Expert judge call failed"))
        run = self.harness.runs()[0]
        self.assertEqual(run["status"], "error")
        self.assertEqual(run["exit_code"], 3)
        self.assertIn("stub crashed", run["stderr_tail"])
        self.assertEqual(list(self.harness.work_root.iterdir()), [])
        # The next pass reuses the rules row and tries the judge again.
        self.harness.set_mode("valid")
        summary = await self.harness.run([_game()], _committee())
        self.assertEqual([row["expert_id"] for row in self.harness.store.rows], ["god_rules", "god_judge"])
        self.assertNotIn("rules_opinion_id", summary["attempted"][0])
        self.assertIn("rules existing, judge", self.harness.notifications[1])

    async def test_empty_and_error_envelopes_are_call_failures(self) -> None:
        stdin = json.dumps({"voices": [{"label": "Voice A"}, {"label": "Voice B"}]})
        for mode, expected in (("empty", "empty result"), ("is_error", "reported an error")):
            with self.subTest(mode=mode):
                self.harness.set_mode(mode)
                with self.assertRaises(JudgeCallError) as caught:
                    self.harness.invoker("prompt", stdin + "\n", str(self.harness.work_root))
                self.assertIn(expected, str(caught.exception))
                self.assertEqual(self.harness.invoker.last_call["exit_code"], 0)

    async def test_dry_run_touches_nothing(self) -> None:
        summary = await self.harness.run([_game()], _committee(), dry_run=True)

        self.assertEqual(self.harness.store.rows, [])
        self.assertEqual(self.harness.calls(), [])
        self.assertEqual(self.harness.notifications, [])
        self.assertEqual(len(summary["attempted"]), 1)
        self.assertTrue(summary["attempted"][0]["dry_run"])
        self.assertEqual(list(self.harness.work_root.iterdir()), [])
        self.assertFalse(self.harness.runs_log.exists())

    def test_row_committee_key_reads_persisted_input(self) -> None:
        payload = self._payload()
        self.assertEqual(row_committee_key({"input_json": json.dumps(payload)}), payload["committee_key"])
        self.assertEqual(
            row_committee_key({"input_json": json.dumps(build_judge_request(payload))}),
            payload["committee_key"],
        )
        for value in ("", "not json", json.dumps({"input_profile": "aggregator"}), json.dumps([1])):
            with self.subTest(value=value):
                self.assertIsNone(row_committee_key({"input_json": value}))

    def test_invoker_command_and_environment(self) -> None:
        invoker = ClaudeHeadlessInvoker("/opt/claude", oauth_token="token", isolation="safe-mode", path="/usr/bin", home="/home/x")
        command = invoker.command("system")
        self.assertEqual(command[:3], ["/opt/claude", "-p", "--safe-mode"])
        self.assertNotIn("--bare", command)
        self.assertEqual(
            invoker.environment(),
            {
                "PATH": "/usr/bin",
                "HOME": "/home/x",
                "TERM": "dumb",
                "LANG": "C.UTF-8",
                "CLAUDE_CODE_OAUTH_TOKEN": "token",
                "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1",
            },
        )
        with self.assertRaises(ValueError):
            ClaudeHeadlessInvoker("/opt/claude", oauth_token="")
        with self.assertRaises(ValueError):
            ClaudeHeadlessInvoker("/opt/claude", oauth_token="token", isolation="none")

    def test_main_refuses_to_run_without_the_oauth_token(self) -> None:
        with patch.dict(os.environ, {"CLAUDE_CODE_OAUTH_TOKEN": ""}):
            with contextlib.redirect_stderr(io.StringIO()) as stderr:
                with self.assertRaises(SystemExit) as caught:
                    main(["--max-games", "1"])
        self.assertEqual(caught.exception.code, 2)
        self.assertIn("CLAUDE_CODE_OAUTH_TOKEN", stderr.getvalue())


if __name__ == "__main__":
    unittest.main()
