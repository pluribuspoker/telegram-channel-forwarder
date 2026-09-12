#!/usr/bin/env python3
"""Tests for the idempotent two-phase Pikkit opinion runner."""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from moe import generate_opinion
from moe_pikkit import INITIAL_PHASE, build_pikkit_input
from scripts.pikkit_opinion_runner import build_parser, run_once
from scripts.test_moe_pikkit import (
    MemoryStore,
    game,
    initial_response,
    line,
    snapshot,
)


class PikkitOpinionRunnerTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.first = snapshot(
            "baseline",
            datetime(2026, 9, 11, 12, tzinfo=timezone.utc),
            datetime(2026, 9, 11, 17, tzinfo=timezone.utc),
        )
        self.final = snapshot(
            "final_t_minus_2h",
            datetime(2026, 9, 13, 15, tzinfo=timezone.utc),
            datetime(2026, 9, 13, 15, tzinfo=timezone.utc),
        )
        self.lines = [
            line("2026-09-11T16:00:00+00:00"),
            line("2026-09-13T14:55:00+00:00"),
        ]

    async def _initial_row(self):
        payload = build_pikkit_input(
            game(),
            phase=INITIAL_PHASE,
            snapshot_rows=[self.first],
            line_rows=self.lines,
        )
        store = MemoryStore()

        async def create_fn(**_kwargs):
            return SimpleNamespace(
                content=[SimpleNamespace(text=json.dumps(initial_response()))]
            )

        return await generate_opinion(
            expert_id="pikkit",
            game=game(),
            history=[],
            input_payload=payload,
            store=store,
            model="claude-opus-4-8",
            create_fn=create_fn,
            generation_backend="claude_headless",
            generation_effort="max",
        )

    async def _run(self, snapshots, opinions, now):
        with tempfile.TemporaryDirectory() as temp:
            return await run_once(
                games=[game()],
                snapshot_rows=snapshots,
                line_rows=self.lines,
                opinion_rows=opinions,
                finals=[],
                store=MemoryStore(),
                now=now,
                invoker=None,
                dry_run=True,
                max_opinions=3,
                runs_log=Path(temp) / "runs.jsonl",
                work_root=temp,
            )

    async def test_initial_is_due_once_first_snapshot_exists(self):
        summary = await self._run(
            [self.first],
            [],
            datetime(2026, 9, 11, 17, 5, tzinfo=timezone.utc),
        )
        self.assertEqual(
            [(item["phase"], item["dry_run"]) for item in summary["generated"]],
            [("initial", True)],
        )

    async def test_final_is_due_only_after_valid_initial(self):
        initial = await self._initial_row()
        summary = await self._run(
            [self.first, self.final],
            [initial],
            datetime(2026, 9, 13, 15, 5, tzinfo=timezone.utc),
        )
        self.assertEqual(
            [item["phase"] for item in summary["generated"]],
            ["final_t_minus_2h"],
        )

    async def test_valid_initial_dedupes_the_phase(self):
        initial = await self._initial_row()
        summary = await self._run(
            [self.first],
            [initial],
            datetime(2026, 9, 12, tzinfo=timezone.utc),
        )
        self.assertEqual(summary["generated"], [])

    async def test_final_stops_retrying_inside_one_hour(self):
        initial = await self._initial_row()
        summary = await self._run(
            [self.first, self.final],
            [initial],
            datetime(2026, 9, 13, 16, 30, tzinfo=timezone.utc),
        )
        self.assertEqual(summary["generated"], [])
        self.assertIn("one-hour", summary["skipped"][0]["reason"])

    async def test_two_invalid_rows_stall_the_phase_identity(self):
        invalid = {
            "event_id": game()["event_id"],
            "expert_id": "pikkit",
            "generation_status": "invalid",
            "review_status": "not_applicable",
            "calibration_summary_json": json.dumps(
                {
                    "generation_phase": "initial",
                    "selected_snapshot_id": self.first["snapshot_id"],
                    "prior_opinion_id": "",
                }
            ),
        }
        summary = await self._run(
            [self.first],
            [dict(invalid), dict(invalid)],
            datetime(2026, 9, 12, tzinfo=timezone.utc),
        )
        self.assertEqual(summary["generated"], [])
        self.assertEqual(summary["stalled"][0]["phase"], "initial")

    def test_cli_accepts_explicit_season_and_week_scope(self):
        args = build_parser().parse_args(
            ["--dry-run", "--season", "2026", "--week", "1"]
        )
        self.assertEqual((args.season, args.week), (2026, 1))


if __name__ == "__main__":
    unittest.main()
