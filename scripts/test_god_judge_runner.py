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
    ENSEMBLE_RULE,
    aggregator_policy,
    build_aggregator_input,
    build_judge_request,
    canonical_json,
    load_registry,
    sha256_text,
)
from nfl_lines import LATEST_HOME_COLUMN
from scripts.god_judge_runner import (
    RESPONSE_INSTRUCTION,
    ClaudeHeadlessInvoker,
    JudgeCallError,
    build_parser,
    committee_experts,
    human_input_experts,
    latest_human_input,
    main,
    pending_message,
    row_committee_key,
    run_once,
    stale_human_voices,
)
from scripts.test_moe_god import (
    EVENT_ID,
    HOME,
    KICKOFF,
    MemoryStore,
    _game,
    _opinion,
)
from scripts.test_moe_god import _committee as _voice_committee


def _committee() -> list[dict]:
    """The God tests' committee plus the rating voice (``rating_elo``,
    enabled since WP7): a complete committee needs an approved row for
    every enabled non-aggregator expert, so the runner's slates carry one."""
    return _voice_committee() + [
        _opinion(
            "rating_elo",
            model="deterministic",
            probability=0.6,
            margin=3,
            away_score=21,
            home_score=24,
            stars=2,
        )
    ]

# A stand-in for the claude binary: records argv, env, cwd and stdin next to
# itself, then answers with a print-mode JSON envelope. A "mode" file beside
# it selects the behavior to simulate, one mode per call (comma-separated,
# cycling by the call index) when a trigger makes several calls.
STUB_SOURCE = '''#!{python}
import json, os, sys

here = os.path.dirname(os.path.abspath(__file__))
stdin_text = sys.stdin.read()
mode_path = os.path.join(here, "mode")
modes = open(mode_path).read().strip().split(",") if os.path.exists(mode_path) else ["valid"]
calls_path = os.path.join(here, "calls.jsonl")
call_index = sum(1 for _line in open(calls_path, encoding="utf-8")) if os.path.exists(calls_path) else 0
mode = modes[call_index % len(modes)].strip() or "valid"
record = {{
    "argv": sys.argv,
    "env": dict(os.environ),
    "cwd": os.getcwd(),
    "cwd_entries": sorted(os.listdir(os.getcwd())),
    "stdin": stdin_text,
    "mode": mode,
    "call_index": call_index + 1,
}}
with open(calls_path, "a", encoding="utf-8") as handle:
    handle.write(json.dumps(record) + "\\n")
if mode == "crash":
    sys.stderr.write("stub crashed\\n")
    sys.exit(3)
request = json.loads(stdin_text[: stdin_text.rindex("}}") + 1])
labels = [voice["label"] for voice in request["voices"]]
# "vary" answers a different estimate per call, so an ensemble has a mean.
variants = [(0.58, 2.0, 44.0), (0.64, 4.0, 46.0), (0.61, 3.0, 45.0)]
if mode == "invalid":
    response = {{"home_win_probability": 0.5}}
else:
    probability, margin, total = variants[call_index % 3] if mode == "vary" else (0.61, 3.0, 45.0)
    response = {{
        "home_win_probability": probability,
        "expected_home_margin": margin,
        "projected_total": total,
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


class CommitteeConfigurationTests(unittest.TestCase):
    def test_cee_is_optional_but_available_to_the_aggregator(self) -> None:
        registry = load_registry()

        self.assertNotIn("cee", committee_experts(registry))
        self.assertTrue(registry["experts"]["cee"]["enabled"])
        self.assertTrue(registry["experts"]["cee"]["committee_optional"])


class _HarnessCase(unittest.IsolatedAsyncioTestCase):
    """A stub claude, a memory store, and an environment holding secrets
    the call must not see; the test classes below add the cases."""

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


class RunnerTests(_HarnessCase):
    """The single-sample path (--samples 1, the default)."""

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
        self.assertEqual(rules["review_status"], "approved")
        self.assertEqual(judge["review_status"], "approved")
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
        for flag in ("-p", "--safe-mode", "--strict-mcp-config", "--no-session-persistence"):
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

        # The DM names both automatically approved rows.
        self.assertEqual(len(self.harness.notifications), 1)
        message = self.harness.notifications[0]
        self.assertTrue(message.startswith("pickbot: new God Expert rows approved for New England Patriots @ Seattle Seahawks"))
        self.assertIn(f"rules {rules['opinion_id']}, judge {judge['opinion_id']}", message)
        self.assertNotIn("review_moe_opinion.py", message)

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

    async def test_rejected_judge_row_does_not_block_a_fresh_run(self) -> None:
        await self.harness.run([_game()], _committee())
        for row in self.harness.store.rows:
            if row["expert_id"] == "god_judge":
                row["review_status"] = "rejected"
        summary = await self.harness.run([_game()], _committee())

        # Rejection is the reviewer asking for a fresh judge run; the rules
        # arm on the same committee is still deduped.
        self.assertEqual(len(summary["attempted"]), 1)
        self.assertEqual(len(self.harness.calls()), 2)
        by_expert = {}
        for row in self.harness.store.rows:
            by_expert.setdefault(row["expert_id"], []).append(row)
        self.assertEqual(len(by_expert["god_judge"]), 2)
        self.assertEqual(len(by_expert["god_rules"]), 1)
        self.assertEqual(by_expert["god_judge"][-1]["review_status"], "approved")

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

    async def test_pending_dm_can_be_silenced_for_the_desk_group(self) -> None:
        summary = await self.harness.run([_game()], _committee(), pending_dm=False)
        self.assertEqual(len(summary["attempted"]), 1)
        self.assertEqual(summary["failed"], [])
        self.assertEqual(self.harness.notifications, [])

    async def test_kickoff_cutoff_skips_the_game(self) -> None:
        summary = await self.harness.run(
            [_game()], _committee(), now=_parse_time(KICKOFF) - timedelta(minutes=59)
        )

        self.assertEqual(self.harness.store.rows, [])
        self.assertEqual(self.harness.calls(), [])
        self.assertEqual(self.harness.notifications, [])
        self.assertIn("kickoff cutoff", summary["skipped"][0]["reason"])

    async def test_game_between_one_and_two_hours_out_is_judged(self) -> None:
        # The cutoff moved from two hours to one (late-window refresh): a
        # game 90 minutes out now gets a judge pass.
        summary = await self.harness.run(
            [_game()], _committee(), now=_parse_time(KICKOFF) - timedelta(minutes=90)
        )

        self.assertEqual(len(summary["attempted"]), 1)
        self.assertEqual(
            [row["expert_id"] for row in self.harness.store.rows],
            ["god_rules", "god_judge"],
        )

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


class EnsembleRunnerTests(_HarnessCase):
    """--samples N >= 2 (WP9): sampled audit rows and one mean judge row per trigger."""

    def _statuses(self) -> list[tuple[str, str]]:
        return [(row["expert_id"], row["generation_status"]) for row in self.harness.store.rows]

    async def test_three_samples_form_one_judge_row(self) -> None:
        self.harness.set_mode("vary")
        summary = await self.harness.run([_game()], _committee(), samples=3)

        self.assertEqual(
            self._statuses(),
            [("god_rules", "valid"), ("god_judge", "sample"), ("god_judge", "sample"), ("god_judge", "sample"), ("god_judge", "valid")],
        )
        rows = self.harness.store.rows
        samples, judge = rows[1:4], rows[4]
        payload = self._payload()
        request = build_judge_request(payload)
        request_sha256 = sha256_text(canonical_json(request))
        for index, row in enumerate(samples):
            self.assertEqual(row["review_status"], "not_applicable")
            self.assertEqual(row["generation_backend"], "claude_headless")
            self.assertEqual(row["model"], "claude-fable-5-1")
            self.assertEqual(row["generation_effort"], "max")
            self.assertEqual(row["generation_error"], "")
            self.assertEqual(row["input_json"], canonical_json(request))
            self.assertEqual(row["input_sha256"], request_sha256)
            self.assertEqual(row["output_sha256"], opinion_output_sha256(row))
            self.assertEqual(row["home_win_probability"], [0.58, 0.64, 0.61][index])
            self.assertEqual(json.loads(row["raw_response"])["expected_home_margin"], [2.0, 4.0, 3.0][index])
        sample_ids = [row["opinion_id"] for row in samples]
        self.assertEqual(len(set(sample_ids)), 3)
        self.assertEqual((judge["generation_status"], judge["review_status"]), ("valid", "approved"))
        self.assertEqual((judge["home_win_probability"], judge["expected_home_margin"]), (0.61, 3.0))
        self.assertEqual(judge["predicted_away_score"] + judge["predicted_home_score"], 45)
        self.assertEqual(judge["input_json"], canonical_json(request))
        self.assertEqual(judge["input_sha256"], request_sha256)
        raw = json.loads(judge["raw_response"])
        self.assertEqual(
            raw["ensemble"],
            {
                "size": 3,
                "valid": 3,
                "samples": sample_ids,
                "estimates": [[0.58, 2.0, 44.0], [0.64, 4.0, 46.0], [0.61, 3.0, 45.0]],
                "reasons_from": sample_ids[2],
                "rule": ENSEMBLE_RULE,
            },
        )
        self.assertEqual(raw["key_reasons"], json.loads(samples[2]["raw_response"])["key_reasons"])
        self.assertEqual(json.loads(judge["calibration_summary_json"])["ensemble"], raw["ensemble"])
        self.assertIn("mean of 3 of 3 samples", judge["full_opinion"])

        self.assertEqual(len(self.harness.calls()), 3)
        runs = self.harness.runs()
        self.assertEqual([(run["sample"], run["samples"], run["status"]) for run in runs], [(1, 3, "ok"), (2, 3, "ok"), (3, 3, "ok")])
        self.assertTrue(all(run["committee_key"] == payload["committee_key"] for run in runs))
        attempt = summary["attempted"][0]
        self.assertEqual(
            (attempt["samples"], attempt["valid_samples"], attempt["sample_opinion_ids"], attempt["judge_opinion_id"]),
            (3, 3, sample_ids, judge["opinion_id"]),
        )
        self.assertEqual(summary["failed"], [])
        self.assertEqual(len(self.harness.notifications), 1)
        message = self.harness.notifications[0]
        self.assertIn(f"judge {judge['opinion_id']} (mean of 3 of 3 samples)", message)
        self.assertNotIn("sample failures", message)
        for sample_id in sample_ids:
            self.assertNotIn(sample_id, message)
        self.assertNotIn("review_moe_opinion.py", message)
        self.assertEqual(list(self.harness.work_root.iterdir()), [])

        # The next pass finds the valid judge row on the same committee.
        summary = await self.harness.run([_game()], _committee(), samples=3)
        self.assertEqual(summary["attempted"], [])
        self.assertIn("valid judge row already carries", summary["skipped"][0]["reason"])
        self.assertEqual(len(self.harness.calls()), 3)
        self.assertEqual(len(self.harness.store.rows), 5)

    async def test_one_invalid_sample_leaves_a_mean_of_two(self) -> None:
        self.harness.set_mode("vary,invalid,vary")
        summary = await self.harness.run([_game()], _committee(), samples=3)

        self.assertEqual(
            self._statuses(),
            [("god_rules", "valid"), ("god_judge", "sample"), ("god_judge", "sample"), ("god_judge", "sample"), ("god_judge", "valid")],
        )
        rows = self.harness.store.rows
        failed = rows[2]
        self.assertEqual(failed["review_status"], "not_applicable")
        self.assertTrue(failed["generation_error"].startswith("ValueError: "))
        self.assertEqual(failed["raw_response"], json.dumps({"home_win_probability": 0.5}))
        self.assertEqual(failed["output_sha256"], "")
        judge = rows[4]
        self.assertEqual((judge["home_win_probability"], judge["expected_home_margin"]), (0.595, 2.5))
        ensemble = json.loads(judge["calibration_summary_json"])["ensemble"]
        self.assertEqual((ensemble["size"], ensemble["valid"]), (3, 2))
        self.assertEqual(ensemble["samples"], [rows[1]["opinion_id"], rows[3]["opinion_id"]])
        self.assertEqual(ensemble["estimates"], [[0.58, 2.0, 44.0], [0.61, 3.0, 45.0]])
        self.assertEqual(ensemble["reasons_from"], rows[1]["opinion_id"])  # equidistant: call order
        attempt = summary["attempted"][0]
        self.assertEqual(attempt["valid_samples"], 2)
        self.assertEqual(attempt["sample_opinion_ids"], [row["opinion_id"] for row in rows[1:4]])
        self.assertEqual(summary["failed"], [])
        message = self.harness.notifications[0]
        self.assertIn("(mean of 2 of 3 samples)", message)
        self.assertIn("sample failures: sample 2/3 invalid: ", message)
        self.assertIn("mean of 2 of 3 samples", judge["full_opinion"])

    async def test_all_samples_invalid_persists_one_invalid_judge_row(self) -> None:
        self.harness.set_mode("invalid")
        summary = await self.harness.run([_game()], _committee(), samples=3)

        self.assertEqual(
            self._statuses(),
            [("god_rules", "valid"), ("god_judge", "sample"), ("god_judge", "sample"), ("god_judge", "sample"), ("god_judge", "invalid")],
        )
        invalid = self.harness.store.rows[4]
        self.assertEqual(invalid["review_status"], "not_applicable")
        self.assertEqual(invalid["raw_response"], json.dumps({"home_win_probability": 0.5}))
        self.assertEqual(summary["failed"][0]["stage"], "judge_validation")
        attempt = summary["attempted"][0]
        self.assertEqual((attempt["valid_samples"], len(attempt["sample_opinion_ids"])), (0, 3))
        self.assertEqual(self.harness.notifications, [])
        self.assertEqual(len(self.harness.calls()), 3)
        # A second trigger stalls the committee exactly as two single-sample triggers would.
        await self.harness.run([_game()], _committee(), samples=3)
        self.assertEqual(len(self.harness.calls()), 6)
        self.assertEqual([status for _expert, status in self._statuses()].count("invalid"), 2)
        summary = await self.harness.run([_game()], _committee(), samples=3)
        self.assertEqual(len(self.harness.calls()), 6)
        self.assertEqual(summary["attempted"], [])
        self.assertEqual(len(summary["stalled"]), 1)
        self.assertIn("failed validation 2 times", summary["skipped"][0]["reason"])
        self.assertEqual(len(self.harness.notifications), 1)
        self.assertIn("failed validation twice", self.harness.notifications[0])

    async def test_crashed_call_among_three_leaves_a_mean_of_two(self) -> None:
        self.harness.set_mode("vary,crash,vary")
        summary = await self.harness.run([_game()], _committee(), samples=3)

        self.assertEqual(
            self._statuses(),
            [("god_rules", "valid"), ("god_judge", "sample"), ("god_judge", "sample"), ("god_judge", "valid")],
        )
        runs = self.harness.runs()
        self.assertEqual([(run["sample"], run["status"]) for run in runs], [(1, "ok"), (2, "error"), (3, "ok")])
        self.assertEqual(runs[1]["exit_code"], 3)
        judge = self.harness.store.rows[3]
        ensemble = json.loads(judge["calibration_summary_json"])["ensemble"]
        self.assertEqual((ensemble["size"], ensemble["valid"]), (3, 2))
        self.assertEqual(summary["attempted"][0]["valid_samples"], 2)
        self.assertEqual(summary["failed"], [])
        message = self.harness.notifications[0]
        self.assertIn("(mean of 2 of 3 samples)", message)
        self.assertIn("sample failures: call 2/3: JudgeCallError: claude exited 3", message)

    async def test_every_call_crashing_is_one_failure_without_a_row(self) -> None:
        self.harness.set_mode("crash")
        summary = await self.harness.run([_game()], _committee(), samples=3)

        self.assertEqual(self._statuses(), [("god_rules", "valid")])
        self.assertEqual(len(self.harness.calls()), 3)
        self.assertEqual(summary["failed"][0]["stage"], "claude")
        self.assertIn("call 1/3: JudgeCallError", summary["failed"][0]["error"])
        self.assertIn("call 3/3: JudgeCallError", summary["failed"][0]["error"])
        self.assertEqual(len(self.harness.notifications), 1)
        self.assertTrue(self.harness.notifications[0].startswith("pickbot: God Expert judge call failed"))
        self.assertEqual([run["status"] for run in self.harness.runs()], ["error"] * 3)

    async def test_single_sample_persists_no_sample_rows(self) -> None:
        summary = await self.harness.run([_game()], _committee())

        self.assertEqual(self._statuses(), [("god_rules", "valid"), ("god_judge", "valid")])
        judge = self.harness.store.rows[1]
        self.assertNotIn("ensemble", json.loads(judge["raw_response"]))
        self.assertIsNone(json.loads(judge["calibration_summary_json"])["ensemble"])
        run = self.harness.runs()[0]
        self.assertEqual((run["sample"], run["samples"]), (1, 1))
        attempt = summary["attempted"][0]
        self.assertEqual((attempt["samples"], attempt["valid_samples"], attempt["sample_opinion_ids"]), (1, 0, []))
        self.assertNotIn("samples", self.harness.notifications[0])
        with self.assertRaises(ValueError):
            await self.harness.run([_game()], _committee(), samples=0)

    def test_samples_argument_and_environment(self) -> None:
        with patch.dict(os.environ, {"GOD_JUDGE_SAMPLES": "3"}):
            self.assertEqual(build_parser().parse_args([]).samples, 3)
        with patch.dict(os.environ, {"GOD_JUDGE_SAMPLES": ""}):
            self.assertEqual(build_parser().parse_args([]).samples, 1)
            self.assertEqual(build_parser().parse_args(["--samples", "2"]).samples, 2)
        for bad in ("0", "6"):
            with self.subTest(samples=bad):
                with patch.dict(os.environ, {"CLAUDE_CODE_OAUTH_TOKEN": "token"}):
                    with contextlib.redirect_stderr(io.StringIO()) as stderr:
                        with self.assertRaises(SystemExit) as caught:
                            main(["--samples", bad])
                self.assertEqual(caught.exception.code, 2)
                self.assertIn("--samples", stderr.getvalue())


def _rehash(row: dict) -> dict:
    digest = opinion_output_sha256(row)
    row["output_sha256"] = digest
    row["approved_output_sha256"] = (
        digest if row["review_status"] == "approved" else ""
    )
    return row


def _voice_row(
    expert_id: str,
    *,
    generated_at: str = "2026-09-05T02:00:00+00:00",
    opinion_id: str | None = None,
) -> dict:
    """An approved, hash-bound optional-voice row (celebrity-shaped)."""
    return _rehash(
        _opinion(
            expert_id,
            model="claude-opus-4-8",
            probability=0.6,
            margin=3,
            away_score=20,
            home_score=24,
            stars=2,
            generated_at=generated_at,
            opinion_id=opinion_id,
            side_leg={"selection": "PASS", "line": None, "confidence_stars": 1},
            total_leg={
                "selection": "Under",
                "line": 44.5,
                "confidence_stars": 1,
            },
        )
    )


def _refresh_data(**overrides: object) -> dict:
    """Human-submission tabs where every voice is fresh unless overridden."""
    data: dict = {
        "leans": [],
        "celebrity_picks": [],
        "win_predictions": [],
        "celebrity_grades": [],
        "ak_user_id": "111",
        "cee_user_id": "222",
    }
    data.update(overrides)
    return data


def _ak_lean(submitted_at: str, *, status: str = "parsed") -> dict:
    return {
        "telegram_user_id": "111",
        "event_id": EVENT_ID,
        "period": "game",
        "market": "spread",
        "side": HOME,
        "submitted_at_utc": submitted_at,
        "prediction_parse_status": status,
        "submission_id": f"ak-{submitted_at}",
    }


def _cee_lean(submitted_at: str, *, market: str = "moneyline") -> dict:
    return {
        "telegram_user_id": "222",
        "event_id": EVENT_ID,
        "period": "game",
        "market": market,
        "side": HOME,
        "submitted_at_utc": submitted_at,
        "submission_id": f"cee-{submitted_at}",
    }


def _celebrity_pick(submitted_at: str) -> dict:
    return {"event_id": EVENT_ID, "submitted_at_utc": submitted_at}


class StaleHumanVoiceTests(unittest.TestCase):
    """The staleness helpers mirror each builder's own source filter."""

    def setUp(self) -> None:
        self.registry = load_registry()
        self.game = _game()

    def test_registry_marks_the_three_human_input_voices(self) -> None:
        self.assertEqual(
            human_input_experts(self.registry), ["ak", "cee", "celebrity"]
        )

    def test_ak_uses_the_newest_parsed_game_projection(self) -> None:
        data = _refresh_data(
            leans=[
                _ak_lean("2026-09-06T00:00:00+00:00"),
                _ak_lean("2026-09-07T00:00:00+00:00"),
            ]
        )
        newest = latest_human_input("ak", self.game, data)
        self.assertEqual(newest, _parse_time("2026-09-07T00:00:00+00:00"))

    def test_ak_unparsed_newest_projection_is_not_fresh_input(self) -> None:
        # build_ak_input would refuse it, so a regen cannot succeed.
        data = _refresh_data(
            leans=[
                _ak_lean("2026-09-06T00:00:00+00:00"),
                _ak_lean("2026-09-07T00:00:00+00:00", status="failed"),
            ]
        )
        self.assertIsNone(latest_human_input("ak", self.game, data))

    def test_cee_requires_a_moneyline_and_counts_her_spread(self) -> None:
        spread_only = _refresh_data(
            leans=[_cee_lean("2026-09-06T00:00:00+00:00", market="spread")]
        )
        self.assertIsNone(latest_human_input("cee", self.game, spread_only))
        both = _refresh_data(
            leans=[
                _cee_lean("2026-09-06T00:00:00+00:00"),
                _cee_lean("2026-09-07T00:00:00+00:00", market="spread"),
            ]
        )
        self.assertEqual(
            latest_human_input("cee", self.game, both),
            _parse_time("2026-09-07T00:00:00+00:00"),
        )

    def test_celebrity_counts_only_pre_kickoff_picks(self) -> None:
        data = _refresh_data(
            celebrity_picks=[
                _celebrity_pick("2026-09-06T00:00:00+00:00"),
                _celebrity_pick("2026-09-11T00:00:00+00:00"),  # post-kickoff
            ]
        )
        self.assertEqual(
            latest_human_input("celebrity", self.game, data),
            _parse_time("2026-09-06T00:00:00+00:00"),
        )

    def test_stale_when_input_postdates_the_selected_row(self) -> None:
        selected = [
            (
                "ak",
                {},
                {"generated_at_utc": "2026-09-05T02:00:00+00:00"},
                "default_model",
            )
        ]
        stale = stale_human_voices(
            self.game,
            selected=selected,
            registry=self.registry,
            refresh_data=_refresh_data(
                leans=[_ak_lean("2026-09-06T00:00:00+00:00")]
            ),
        )
        self.assertEqual([item[0] for item in stale], ["ak"])
        fresh = stale_human_voices(
            self.game,
            selected=selected,
            registry=self.registry,
            refresh_data=_refresh_data(
                leans=[_ak_lean("2026-09-04T00:00:00+00:00")]
            ),
        )
        self.assertEqual(fresh, [])

    def test_missing_voice_with_input_is_stale(self) -> None:
        stale = stale_human_voices(
            self.game,
            selected=[],
            registry=self.registry,
            refresh_data=_refresh_data(
                celebrity_picks=[_celebrity_pick("2026-09-06T00:00:00+00:00")]
            ),
        )
        self.assertEqual(stale, [("celebrity", "no approved row yet")])

    def test_no_input_never_refreshes(self) -> None:
        self.assertEqual(
            stale_human_voices(
                self.game,
                selected=[],
                registry=self.registry,
                refresh_data=_refresh_data(),
            ),
            [],
        )


class LateWindowRefreshTests(_HarnessCase):
    """The refresh step inside REFRESH_WINDOW, against a fake refresher."""

    def _fake_refresher(
        self,
        *,
        persist: dict[str, dict] | None = None,
        results: dict[str, dict] | None = None,
    ) -> tuple:
        calls: list[tuple[str, str]] = []
        store = self.harness.store

        async def refresh(game: dict, expert_id: str) -> dict:
            calls.append((str(game["event_id"]), expert_id))
            row = (persist or {}).get(expert_id)
            if row is not None:
                store.append(row)
                return {
                    "status": "approved",
                    "opinion_id": str(row["opinion_id"]),
                }
            return (results or {}).get(
                expert_id, {"status": "failed", "error": "boom"}
            )

        return refresh, calls

    async def test_stale_voice_refreshes_and_the_judge_sees_it(self) -> None:
        stale_ak = next(
            row for row in _committee() if row["expert_id"] == "ak"
        )
        fresh_ak = _rehash(
            {
                **stale_ak,
                "opinion_id": "ak-refreshed",
                "generated_at_utc": "2026-09-09T00:00:00+00:00",
            }
        )
        refresh, calls = self._fake_refresher(persist={"ak": fresh_ak})
        summary = await self.harness.run(
            [_game()],
            _committee(),
            now=_parse_time(KICKOFF) - timedelta(minutes=90),
            refresh_data=_refresh_data(
                leans=[_ak_lean("2026-09-06T00:00:00+00:00")]
            ),
            refresh_voice=refresh,
        )

        self.assertEqual(calls, [(EVENT_ID, "ak")])
        self.assertEqual(len(summary["refreshed"]), 1)
        self.assertEqual(summary["refreshed"][0]["status"], "approved")
        self.assertEqual(len(summary["attempted"]), 1)
        rules = next(
            row
            for row in self.harness.store.rows
            if row["expert_id"] == "god_rules"
        )
        # The judged committee carries the refreshed voice, not the stale
        # one (the rules row persists the full input; the judge row only
        # the masked request).
        self.assertIn("ak-refreshed", rules["input_json"])
        self.assertNotIn(stale_ak["opinion_id"], rules["input_json"])
        self.assertIn("refreshed voices: ak", self.harness.notifications[-1])

    async def test_missing_required_voice_heals_the_committee(self) -> None:
        rows = [row for row in _committee() if row["expert_id"] != "ak"]
        fresh_ak = _voice_row(
            "ak",
            generated_at="2026-09-09T00:00:00+00:00",
            opinion_id="ak-healed",
        )
        refresh, calls = self._fake_refresher(persist={"ak": fresh_ak})
        summary = await self.harness.run(
            [_game()],
            rows,
            now=_parse_time(KICKOFF) - timedelta(minutes=90),
            refresh_data=_refresh_data(
                leans=[_ak_lean("2026-09-06T00:00:00+00:00")]
            ),
            refresh_voice=refresh,
        )

        self.assertEqual(calls, [(EVENT_ID, "ak")])
        self.assertEqual(len(summary["attempted"]), 1)
        self.assertEqual(summary["skipped"], [])

    async def test_optional_voice_joins_via_refresh(self) -> None:
        fresh_celebrity = _voice_row(
            "celebrity",
            generated_at="2026-09-09T00:00:00+00:00",
            opinion_id="celebrity-joined",
        )
        refresh, calls = self._fake_refresher(
            persist={"celebrity": fresh_celebrity}
        )
        summary = await self.harness.run(
            [_game()],
            _committee(),
            now=_parse_time(KICKOFF) - timedelta(minutes=90),
            refresh_data=_refresh_data(
                celebrity_picks=[_celebrity_pick("2026-09-06T00:00:00+00:00")]
            ),
            refresh_voice=refresh,
        )

        self.assertEqual(calls, [(EVENT_ID, "celebrity")])
        self.assertEqual(len(summary["attempted"]), 1)
        rules = next(
            row
            for row in self.harness.store.rows
            if row["expert_id"] == "god_rules"
        )
        self.assertIn("celebrity-joined", rules["input_json"])

    async def test_refresh_failure_still_judges_on_the_old_committee(
        self,
    ) -> None:
        refresh, calls = self._fake_refresher(
            results={"ak": {"status": "failed", "error": "boom"}}
        )
        stale_ak = next(
            row for row in _committee() if row["expert_id"] == "ak"
        )
        summary = await self.harness.run(
            [_game()],
            _committee(),
            now=_parse_time(KICKOFF) - timedelta(minutes=90),
            refresh_data=_refresh_data(
                leans=[_ak_lean("2026-09-06T00:00:00+00:00")]
            ),
            refresh_voice=refresh,
        )

        self.assertEqual(calls, [(EVENT_ID, "ak")])
        self.assertEqual(len(summary["attempted"]), 1)
        rules = next(
            row
            for row in self.harness.store.rows
            if row["expert_id"] == "god_rules"
        )
        self.assertIn(stale_ak["opinion_id"], rules["input_json"])
        self.assertTrue(
            any(
                "voice refresh failed" in message and "ak: boom" in message
                for message in self.harness.notifications
            )
        )

    async def test_outside_the_window_nothing_refreshes(self) -> None:
        refresh, calls = self._fake_refresher()
        summary = await self.harness.run(
            [_game()],
            _committee(),
            now=_parse_time(KICKOFF) - timedelta(hours=3),
            refresh_data=_refresh_data(
                leans=[_ak_lean("2026-09-06T00:00:00+00:00")]
            ),
            refresh_voice=refresh,
        )

        self.assertEqual(calls, [])
        self.assertEqual(summary["refreshed"], [])
        self.assertEqual(len(summary["attempted"]), 1)

    async def test_fresh_voices_are_left_alone(self) -> None:
        refresh, calls = self._fake_refresher()
        summary = await self.harness.run(
            [_game()],
            _committee(),
            now=_parse_time(KICKOFF) - timedelta(minutes=90),
            refresh_data=_refresh_data(
                leans=[_ak_lean("2026-09-04T00:00:00+00:00")]
            ),
            refresh_voice=refresh,
        )

        self.assertEqual(calls, [])
        self.assertEqual(summary["refreshed"], [])
        self.assertEqual(len(summary["attempted"]), 1)

    async def test_refresh_budget_caps_the_pass(self) -> None:
        refresh, calls = self._fake_refresher(
            results={
                "ak": {"status": "failed", "error": "boom"},
                "celebrity": {"status": "failed", "error": "boom"},
            }
        )
        summary = await self.harness.run(
            [_game()],
            _committee(),
            now=_parse_time(KICKOFF) - timedelta(minutes=90),
            refresh_data=_refresh_data(
                leans=[_ak_lean("2026-09-06T00:00:00+00:00")],
                celebrity_picks=[
                    _celebrity_pick("2026-09-06T00:00:00+00:00")
                ],
            ),
            refresh_voice=refresh,
            max_refreshes=1,
        )

        self.assertEqual(len(calls), 1)
        self.assertEqual(len(summary["refreshed"]), 1)

    async def test_dry_run_only_reports_the_refresh(self) -> None:
        summary = await self.harness.run(
            [_game()],
            _committee(),
            now=_parse_time(KICKOFF) - timedelta(minutes=90),
            refresh_data=_refresh_data(
                leans=[_ak_lean("2026-09-06T00:00:00+00:00")]
            ),
            dry_run=True,
        )

        self.assertEqual(len(summary["refreshed"]), 1)
        self.assertTrue(summary["refreshed"][0]["dry_run"])
        self.assertEqual(self.harness.store.rows, [])
        self.assertIn(
            "would refresh ak", self.harness.output.getvalue()
        )

    def test_pending_message_names_refreshed_voices(self) -> None:
        message = pending_message(
            _game(),
            rules_id="r1",
            judge_id="j1",
            refreshed=["ak", "celebrity"],
        )
        self.assertIn("refreshed voices: ak, celebrity", message)


if __name__ == "__main__":
    unittest.main()
