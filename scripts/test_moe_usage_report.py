#!/usr/bin/env python3
"""Tests for the MOE usage look-back summarizers."""

from __future__ import annotations

import unittest
from datetime import datetime, timezone

from scripts.moe_usage_report import summarize_opinions, summarize_runs


def _run(kind: str, day: str, *, expert: str = "", out: int = 100) -> dict:
    record = {
        "kind": kind,
        "logged_at_utc": f"{day}T12:00:00+00:00",
        "status": "ok",
        "usage": {
            "input_tokens": 10,
            "output_tokens": out,
            "cache_read_input_tokens": 5,
            "cache_creation_input_tokens": 7,
        },
        "total_cost_usd": 1.5,
    }
    if expert:
        record["expert_id"] = expert
    return record


class SummarizeRunsTests(unittest.TestCase):
    def test_groups_judge_and_refresh_calls(self) -> None:
        runs = [
            _run("claude_call", "2026-09-09"),
            _run("claude_call", "2026-09-10", out=200),
            _run("voice_refresh_call", "2026-09-10", expert="celebrity"),
        ]
        summary = summarize_runs(runs)

        self.assertEqual(sorted(summary), ["judge", "refresh:celebrity"])
        judge = summary["judge"]
        self.assertEqual(judge["calls"], 2)
        self.assertEqual(judge["output_tokens"], 300)
        self.assertEqual(judge["cost_usd"], 3.0)
        self.assertEqual(
            judge["by_day"], {"2026-09-09": 1, "2026-09-10": 1}
        )
        self.assertEqual(summary["refresh:celebrity"]["calls"], 1)

    def test_since_filters_and_errors_count(self) -> None:
        runs = [
            _run("claude_call", "2026-09-01"),
            {**_run("claude_call", "2026-09-10"), "status": "error"},
        ]
        summary = summarize_runs(
            runs, since=datetime(2026, 9, 5, tzinfo=timezone.utc)
        )

        self.assertEqual(summary["judge"]["calls"], 1)
        self.assertEqual(summary["judge"]["errors"], 1)


class SummarizeOpinionsTests(unittest.TestCase):
    def test_groups_by_expert_backend_model_with_size_proxy(self) -> None:
        rows = [
            {
                "expert_id": "celebrity",
                "generation_backend": "agent_runtime",
                "model": "claude-opus-4-8",
                "generation_status": "valid",
                "generated_at_utc": "2026-09-10T00:00:00+00:00",
                "raw_response": "x" * 50,
            },
            {
                "expert_id": "celebrity",
                "generation_backend": "agent_runtime",
                "model": "claude-opus-4-8",
                "generation_status": "invalid",
                "generated_at_utc": "2026-09-10T01:00:00+00:00",
                "raw_response": "x" * 150,
            },
            {
                "expert_id": "god_judge",
                "generation_backend": "claude_headless",
                "model": "claude-fable-5-1",
                "generation_status": "valid",
                "generated_at_utc": "2026-09-01T00:00:00+00:00",
                "raw_response": "y" * 10,
            },
        ]
        summary = summarize_opinions(rows)

        celeb = summary[("celebrity", "agent_runtime", "claude-opus-4-8")]
        self.assertEqual(celeb["rows"], 2)
        self.assertEqual(celeb["by_status"], {"invalid": 1, "valid": 1})
        self.assertEqual(celeb["response_chars"], 200)
        filtered = summarize_opinions(
            rows, since=datetime(2026, 9, 5, tzinfo=timezone.utc)
        )
        self.assertNotIn(
            ("god_judge", "claude_headless", "claude-fable-5-1"), filtered
        )


if __name__ == "__main__":
    unittest.main()
