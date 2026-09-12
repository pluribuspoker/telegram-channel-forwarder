#!/usr/bin/env python3
"""Tests for the two-phase shadow Pikkit Expert."""

from __future__ import annotations

import json
import sys
import unittest
from types import SimpleNamespace
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from moe_god import aggregator_policy, load_registry, select_voice_rows
from moe import generate_opinion
from moe_pikkit import (
    FINAL_PHASE,
    INITIAL_PHASE,
    build_historical_calibration,
    build_pikkit_input,
    normalize_pikkit_opinion,
    opinion_phase,
)
from nfl_pikkit import CaptureTask, build_snapshot_row, snapshot_identity


def game() -> dict:
    return {
        "event_id": "nfl-1",
        "season": 2026,
        "week": 1,
        "status": "upcoming",
        "commence_time_utc": "2026-09-13T17:00:00+00:00",
        "away_team": "Chicago Bears",
        "home_team": "Carolina Panthers",
    }


def event() -> dict:
    return {
        "event_id": "pikkit-1",
        "away_full": "Chicago Bears",
        "home_full": "Carolina Panthers",
        "start_time": "2026-09-13T17:00:00Z",
        "status": "not_started",
    }


def split_values(home_bet: float = 0.4, home_handle: float = 0.3) -> dict:
    return {
        "num_picks": 10000,
        "total_wagered": 500000.0,
        "moneyline": {
            "home": {
                "bet_pct": home_bet,
                "handle_pct": home_handle,
                "label": "CAR",
                "bets": 4000,
            },
            "away": {
                "bet_pct": 1 - home_bet,
                "handle_pct": 1 - home_handle,
                "label": "CHI",
                "bets": 6000,
            },
        },
        "spread": {
            "home": {
                "bet_pct": 0.55,
                "handle_pct": 0.48,
                "label": "CAR",
                "bets": 2200,
            },
            "away": {
                "bet_pct": 0.45,
                "handle_pct": 0.52,
                "label": "CHI",
                "bets": 1800,
            },
        },
        "total": {
            "over": {
                "bet_pct": 0.6,
                "handle_pct": 0.5,
                "label": "OVER",
                "bets": 1800,
            },
            "under": {
                "bet_pct": 0.4,
                "handle_pct": 0.5,
                "label": "UNDER",
                "bets": 1200,
            },
        },
    }


def snapshot(
    kind: str,
    scheduled: datetime,
    captured: datetime,
    values: dict | None = None,
) -> dict:
    task = CaptureTask(
        game=game(),
        capture_kind=kind,
        scheduled_for_utc=scheduled,
        snapshot_id=snapshot_identity("nfl-1", kind, scheduled),
    )
    return build_snapshot_row(task, event(), values or split_values(), captured)


def line(captured: str) -> dict:
    return {
        "captured_at": captured,
        "event_id": "nfl-1",
        "commence_time_utc": game()["commence_time_utc"],
        "commence_time_et": "",
        "away_team": "Chicago Bears",
        "home_team": "Carolina Panthers",
        "bookmaker": "BetOnline.ag",
        "away_game_spread_spreadprice_moneyline__h1_spread_spreadprice_moneyline__q1_spread_spreadprice_moneyline": "3,-110,130|nodata,nodata,nodata|nodata,nodata,nodata",
        "home_game_spread_spreadprice_moneyline__h1_spread_spreadprice_moneyline__q1_spread_spreadprice_moneyline": "-3,-110,-150|nodata,nodata,nodata|nodata,nodata,nodata",
        "totals_game_total_overprice_underprice__h1_total_overprice_underprice__q1_total_overprice_underprice": "44.5,-105,-115|nodata,nodata,nodata|nodata,nodata,nodata",
        "api_requests_used": "",
        "api_requests_remaining": "",
    }


def initial_response() -> dict:
    return {
        "home_win_probability": 0.6,
        "expected_home_margin": 3.5,
        "projected_total": 45,
        "confidence_stars": 2,
        "thesis": "The market baseline remains primary while handle is concentrated away.",
        "supporting_factors": [
            "BetOnline implies the home side near 58%, while away moneyline handle is 70%."
        ],
        "counterarguments": [
            "The splits have no direct historical calibration yet."
        ],
        "no_signal_factors": ["No movement exists at the first snapshot."],
        "discarded_considerations": [
            "Estimated sportsbook exposure is not actual BetOnline liability."
        ],
        "movement_watch": [
            {
                "id": "ml-home-handle",
                "market": "moneyline",
                "side": "home",
                "metric": "handle_pct",
                "expected_movement": "rise",
                "threshold": "at least 5 percentage points",
                "if_observed": "Strengthen the home adjustment.",
                "if_not_observed": "Keep the market baseline dominant.",
                "invalidation": "Moneyline splits become unavailable.",
            }
        ],
    }


class PikkitInputTests(unittest.TestCase):
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
            split_values(0.5, 0.4),
        )
        self.lines = [
            line("2026-09-11T16:00:00+00:00"),
            line("2026-09-13T14:55:00+00:00"),
        ]

    def test_initial_input_uses_first_snapshot_and_market_baseline(self):
        payload = build_pikkit_input(
            game(),
            phase=INITIAL_PHASE,
            snapshot_rows=[self.first, self.final],
            line_rows=self.lines,
        )

        self.assertEqual(payload["generation_phase"], INITIAL_PHASE)
        self.assertIsNone(payload["movement"])
        self.assertAlmostEqual(
            payload["selected_snapshot"]["market_baseline"][
                "home_win_probability"
            ],
            0.579832,
        )

    def test_final_input_links_initial_watch_and_all_prior_snapshots(self):
        initial = normalize_pikkit_opinion(
            initial_response(),
            build_pikkit_input(
                game(),
                phase=INITIAL_PHASE,
                snapshot_rows=[self.first],
                line_rows=self.lines,
            ),
        )
        initial_row = {
            **initial,
            "opinion_id": "initial-id",
            "event_id": "nfl-1",
        }

        payload = build_pikkit_input(
            game(),
            phase=FINAL_PHASE,
            snapshot_rows=[self.first, self.final],
            line_rows=self.lines,
            initial_opinion=initial_row,
        )

        self.assertEqual(payload["initial_opinion"]["opinion_id"], "initial-id")
        self.assertEqual(len(payload["snapshot_ids"]), 2)
        self.assertIsNotNone(payload["movement"])

    def test_historical_calibration_is_time_safe_and_deterministic(self):
        final_row = {
            "kickoff_utc": game()["commence_time_utc"],
            "away_team": game()["away_team"],
            "home_team": game()["home_team"],
            "away_score": 20,
            "home_score": 27,
        }
        calibration = build_historical_calibration(
            snapshot_rows=[self.first, self.final],
            line_rows=self.lines,
            opinion_rows=[],
            finals=[final_row],
            as_of=datetime(2026, 9, 14, tzinfo=timezone.utc),
        )

        self.assertEqual(calibration["resolved_games"], 1)
        self.assertEqual(calibration["status"], "available")
        self.assertEqual(
            calibration["markets"]["moneyline"]["sportsbook_preferred_rate"],
            1.0,
        )
        self.assertEqual(
            calibration["markets"]["spread"]["sportsbook_preferred_rate"],
            1.0,
        )
        self.assertEqual(
            calibration["markets"]["total"]["sportsbook_preferred_rate"],
            0.0,
        )
        before_final = build_historical_calibration(
            snapshot_rows=[self.first, self.final],
            line_rows=self.lines,
            opinion_rows=[],
            finals=[final_row],
            as_of=datetime(2026, 9, 13, 16, tzinfo=timezone.utc),
        )
        self.assertEqual(before_final["resolved_games"], 0)


class PikkitNormalizationTests(unittest.TestCase):
    def setUp(self):
        self.first = snapshot(
            "baseline",
            datetime(2026, 9, 11, 12, tzinfo=timezone.utc),
            datetime(2026, 9, 11, 17, tzinfo=timezone.utc),
        )
        self.lines = [line("2026-09-11T16:00:00+00:00")]
        self.payload = build_pikkit_input(
            game(),
            phase=INITIAL_PHASE,
            snapshot_rows=[self.first],
            line_rows=self.lines,
        )

    def test_initial_forces_both_legs_to_pass(self):
        opinion = normalize_pikkit_opinion(initial_response(), self.payload)

        self.assertEqual(
            json.loads(opinion["side_pick_json"])["selection"], "PASS"
        )
        self.assertEqual(
            json.loads(opinion["total_pick_json"])["selection"], "PASS"
        )
        self.assertEqual(
            json.loads(opinion["calibration_summary_json"])["generation_phase"],
            INITIAL_PHASE,
        )

    def test_final_requires_result_for_every_initial_watch(self):
        initial = normalize_pikkit_opinion(initial_response(), self.payload)
        initial_row = {
            **initial,
            "opinion_id": "initial-id",
            "event_id": "nfl-1",
        }
        final = snapshot(
            "final_t_minus_2h",
            datetime(2026, 9, 13, 15, tzinfo=timezone.utc),
            datetime(2026, 9, 13, 15, tzinfo=timezone.utc),
        )
        final_payload = build_pikkit_input(
            game(),
            phase=FINAL_PHASE,
            snapshot_rows=[self.first, final],
            line_rows=[*self.lines, line("2026-09-13T14:55:00+00:00")],
            initial_opinion=initial_row,
        )
        response = {
            **initial_response(),
            "watch_results": [],
            "side_selection": "Chicago Bears",
            "total_selection": "Under",
        }

        with self.assertRaisesRegex(ValueError, "account for every"):
            normalize_pikkit_opinion(response, final_payload)

    def test_phase_can_be_recovered_from_persisted_summary(self):
        opinion = normalize_pikkit_opinion(initial_response(), self.payload)

        self.assertEqual(opinion_phase(opinion), INITIAL_PHASE)


class ShadowSelectionTests(unittest.TestCase):
    def test_shadow_expert_is_not_selected_for_god(self):
        registry = load_registry()
        row = {
            "event_id": "nfl-1",
            "expert_id": "pikkit",
            "review_status": "approved",
            "generation_status": "valid",
            "model": "claude-opus-4-8",
            "generated_at_utc": "2026-09-13T15:01:00+00:00",
            "opinion_id": "pikkit-id",
        }

        selected = select_voice_rows(
            [row],
            event_id="nfl-1",
            registry=registry,
            policy=aggregator_policy(registry),
        )

        self.assertEqual(selected, [])


class MemoryStore:
    def __init__(self):
        self.rows = []

    def append(self, row):
        self.rows.append(dict(row))

    def list(self, event_id=None):
        if event_id is None:
            return list(self.rows)
        return [
            row for row in self.rows if str(row.get("event_id")) == str(event_id)
        ]

    def review(self, *_args, **_kwargs):
        raise AssertionError("Automatic validation approval should not call review")


class GenerationRailTests(unittest.IsolatedAsyncioTestCase):
    async def test_initial_opinion_runs_through_standard_rail(self):
        first = snapshot(
            "baseline",
            datetime(2026, 9, 11, 12, tzinfo=timezone.utc),
            datetime(2026, 9, 11, 17, tzinfo=timezone.utc),
        )
        payload = build_pikkit_input(
            game(),
            phase=INITIAL_PHASE,
            snapshot_rows=[first],
            line_rows=[line("2026-09-11T16:00:00+00:00")],
        )
        store = MemoryStore()

        async def create_fn(**_kwargs):
            return SimpleNamespace(
                content=[SimpleNamespace(text=json.dumps(initial_response()))]
            )

        row = await generate_opinion(
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

        self.assertEqual(row["generation_status"], "valid")
        self.assertEqual(row["review_status"], "approved")
        self.assertEqual(row["reviewed_by"], "validation")
        self.assertEqual(opinion_phase(row), INITIAL_PHASE)
        self.assertEqual(len(store.rows), 1)


if __name__ == "__main__":
    unittest.main()
