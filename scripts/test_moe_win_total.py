#!/usr/bin/env python3
"""Tests for the NFL Win Total Expert."""

from __future__ import annotations

import json
import unittest
from types import SimpleNamespace

from moe import generate_opinion, load_expert
from moe_win_total import build_win_total_input


class MemoryStore:
    def __init__(self) -> None:
        self.rows: list[dict] = []

    def append(self, row: dict) -> None:
        self.rows.append(dict(row))

    def list(self, event_id: str | None = None) -> list[dict]:
        if event_id is None:
            return list(self.rows)
        return [
            row
            for row in self.rows
            if str(row.get("event_id")) == str(event_id)
        ]


def _game() -> dict:
    return {
        "event_id": "seahawks-patriots",
        "season": 2026,
        "week": 1,
        "commence_time_utc": "2026-09-10T00:20:00+00:00",
        "away_team": "New England Patriots",
        "home_team": "Seattle Seahawks",
    }


def _win_totals() -> list[dict]:
    return [
        {
            "season": 2026,
            "team": "New England Patriots",
            "bookmaker": "BetOnline",
            "win_total": 9.5,
            "captured_at_utc": "2026-08-10T03:34:34+00:00",
            "captured_at_et": "2026-08-09T23:34:34-04:00",
        },
        {
            "season": 2026,
            "team": "Seattle Seahawks",
            "bookmaker": "BetOnline",
            "win_total": 10.5,
            "captured_at_utc": "2026-08-10T03:34:34+00:00",
            "captured_at_et": "2026-08-09T23:34:34-04:00",
        },
    ]


def _prediction(
    revision_id: str,
    user_id: int,
    name: str,
    team: str,
    wins: int,
) -> dict:
    return {
        "revision_id": revision_id,
        "submitted_at_utc": f"2026-08-10T0{wins % 10}:00:00+00:00",
        "telegram_user_id": user_id,
        "telegram_display_name": name,
        "telegram_username": "",
        "season": 2026,
        "team": team,
        "predicted_wins": wins,
    }


def _predictions() -> list[dict]:
    return [
        _prediction(
            "ak-ne",
            2,
            "A K",
            "New England Patriots",
            11,
        ),
        _prediction("ak-sea", 2, "A K", "Seattle Seahawks", 12),
        _prediction(
            "cee-ne",
            1,
            "Cee",
            "New England Patriots",
            12,
        ),
        _prediction("cee-sea", 1, "Cee", "Seattle Seahawks", 11),
    ]


def _team_history() -> list[dict]:
    return [
        {"season": 2025, "team": "New England Patriots", "wins": 14},
        {"season": 2025, "team": "Seattle Seahawks", "wins": 14},
        {"season": 2024, "team": "Historical Away", "wins": 14},
        {"season": 2024, "team": "Historical Home", "wins": 14},
        {"season": 2024, "team": "Band Away", "wins": 9},
        {"season": 2024, "team": "Band Home", "wins": 11},
    ]


def _game_history() -> list[dict]:
    return [
        {
            "season": 2025,
            "away_team": "Historical Away",
            "home_team": "Historical Home",
            "away_score": 24,
            "home_score": 20,
        },
        {
            "season": 2025,
            "away_team": "Band Away",
            "home_team": "Band Home",
            "away_score": 17,
            "home_score": 24,
        }
    ]


def _leans() -> list[dict]:
    return [
        {
            "submission_id": "ak-old",
            "submitted_at_utc": "2026-09-01T00:00:00+00:00",
            "event_id": "seahawks-patriots",
            "telegram_user_id": 2,
            "celebrity_name": "",
            "market": "moneyline",
            "period": "game",
            "side": "New England Patriots",
        },
        {
            "submission_id": "ak-latest",
            "submitted_at_utc": "2026-09-02T00:00:00+00:00",
            "event_id": "seahawks-patriots",
            "telegram_user_id": 2,
            "celebrity_name": "",
            "market": "moneyline",
            "period": "game",
            "side": "Seattle Seahawks",
        },
        {
            "submission_id": "cee",
            "submitted_at_utc": "2026-09-02T00:00:00+00:00",
            "event_id": "seahawks-patriots",
            "telegram_user_id": 1,
            "celebrity_name": "",
            "market": "spread",
            "period": "game",
            "side": "New England Patriots",
        },
    ]


class WinTotalInputTest(unittest.TestCase):
    def test_builds_market_consensus_and_consistency_evidence(self) -> None:
        payload = build_win_total_input(
            _game(),
            _game_history(),
            _win_totals(),
            _predictions(),
            _team_history(),
            _leans(),
        )

        self.assertEqual(payload["input_profile"], "win_total")
        self.assertEqual(
            payload["market"]["higher_total_side"], "Seattle Seahawks"
        )
        self.assertEqual(payload["market"]["home_minus_away"], 1.0)
        self.assertEqual(
            payload["consensus"]["home_team_higher_count"], 1
        )
        self.assertEqual(
            payload["consensus"]["away_team_higher_count"], 1
        )
        self.assertEqual(
            payload["forecasters"]["forecaster_01"]["display_name"], "A K"
        )
        self.assertEqual(
            payload["forecasters"]["forecaster_01"]["latest_game_pick"][
                "consistency_with_season_picks"
            ],
            "consistent",
        )
        self.assertEqual(
            payload["forecasters"]["forecaster_02"]["latest_game_pick"][
                "consistency_with_season_picks"
            ],
            "consistent",
        )
        exact = payload["historical_analogs"]["exact_prior_win_pair"]
        self.assertEqual(exact["games"], 1)
        self.assertEqual(exact["wins"], 0)
        self.assertEqual(exact["losses"], 1)
        self.assertEqual(
            payload["historical_analogs"]["method"],
            "prior_season_wins_only",
        )
        market_band = payload["historical_analogs"][
            "bookmaker_projection_band_matchup"
        ]
        self.assertEqual(
            market_band["away_allowed_prior_wins"], [8, 9, 10, 11]
        )
        self.assertEqual(
            market_band["home_allowed_prior_wins"], [9, 10, 11, 12]
        )
        self.assertEqual(market_band["games"], 1)
        self.assertEqual(market_band["wins"], 1)

    def test_latest_game_pick_revision_is_used(self) -> None:
        payload = build_win_total_input(
            _game(),
            _game_history(),
            _win_totals(),
            _predictions(),
            _team_history(),
            _leans(),
        )

        ak = payload["forecasters"]["forecaster_01"]
        self.assertEqual(
            ak["latest_game_pick"]["side"], "Seattle Seahawks"
        )
        self.assertNotIn("ak-latest", json.dumps(payload))

    def test_opposing_latest_game_pick_is_marked_inconsistent(self) -> None:
        leans = [
            row
            for row in _leans()
            if row["submission_id"] != "ak-latest"
        ]
        payload = build_win_total_input(
            _game(),
            _game_history(),
            _win_totals(),
            _predictions(),
            _team_history(),
            leans,
        )

        ak = payload["forecasters"]["forecaster_01"]
        self.assertEqual(
            ak["latest_game_pick"]["consistency_with_season_picks"],
            "inconsistent",
        )


class WinTotalGenerationTest(unittest.IsolatedAsyncioTestCase):
    def _output(self) -> dict:
        return {
            "predicted_winner": "Seattle Seahawks",
            "predicted_away_score": 20,
            "predicted_home_score": 24,
            "home_win_probability": 0.61,
            "expected_home_margin": 3.0,
            "confidence_stars": 3,
            "thesis": {
                "claim": (
                    "BetOnline's 10.5 to 9.5 ordering and the equal "
                    "two-person split produce a modest Seattle lean."
                ),
                "evidence_paths": ["market", "consensus"],
            },
            "supporting_factors": [
                {
                    "claim": (
                        "A K projects Seattle 12 wins to New England 11 and "
                        "the Seattle game pick is consistent."
                    ),
                    "evidence_paths": ["forecasters.forecaster_01"],
                },
                {
                    "claim": (
                        "Both teams enter after 14-win prior-season records."
                    ),
                    "evidence_paths": ["team_context"],
                },
                {
                    "claim": (
                        "Historical preseason bookmaker totals are unavailable, "
                        "so these samples use prior-season wins."
                    ),
                    "evidence_paths": ["historical_analogs"],
                },
                {
                    "claim": (
                        "The bookmaker-band prior-season sample was 1-0 for "
                        "home sides across 1 game."
                    ),
                    "evidence_paths": [
                        "historical_analogs.bookmaker_projection_band_matchup"
                    ],
                },
                {
                    "claim": (
                        "The exact prior-season role sample was 0-1 for "
                        "historical home sides across 1 game."
                    ),
                    "evidence_paths": [
                        "historical_analogs.exact_prior_win_pair"
                    ],
                },
                {
                    "claim": (
                        "Equal prior-season win matchups were 0-1 for home "
                        "sides in the supplied sample."
                    ),
                    "evidence_paths": [
                        "historical_analogs.matching_prior_win_gap"
                    ],
                },
                {
                    "claim": (
                        "The 12_plus and 12_plus prior-season level sample was "
                        "0-1 for home sides."
                    ),
                    "evidence_paths": [
                        "historical_analogs.matching_prior_win_level"
                    ],
                },
            ],
            "counterarguments": [
                {
                    "claim": (
                        "Cee projects New England 12 wins to Seattle 11 and "
                        "the New England game pick is consistent."
                    ),
                    "evidence_paths": ["forecasters.forecaster_02"],
                }
            ],
            "no_signal_factors": [],
            "discarded_considerations": [
                "current game market - discarded because it is unavailable"
            ],
        }

    async def test_generates_cited_win_total_opinion(self) -> None:
        output = self._output()

        async def create_fn(**_kwargs):
            return SimpleNamespace(
                content=[SimpleNamespace(text=json.dumps(output))]
            )

        store = MemoryStore()
        row = await generate_opinion(
            expert_id="win_total",
            game=_game(),
            history=_game_history(),
            leans=_leans(),
            win_totals=_win_totals(),
            win_predictions=_predictions(),
            team_history=_team_history(),
            store=store,
            create_fn=create_fn,
        )

        self.assertEqual(row["generation_status"], "valid")
        self.assertEqual(row["input_profile"], "win_total")
        self.assertEqual(row["expert_version"], 3)
        self.assertIn("A K projects Seattle", row["full_opinion"])
        self.assertEqual(store.rows, [row])

    async def test_rejects_opinion_that_omits_a_forecaster(self) -> None:
        output = self._output()
        output["counterarguments"] = []

        async def create_fn(**_kwargs):
            return SimpleNamespace(
                content=[SimpleNamespace(text=json.dumps(output))]
            )

        store = MemoryStore()
        with self.assertRaisesRegex(ValueError, "every forecaster"):
            await generate_opinion(
                expert_id="win_total",
                game=_game(),
                history=_game_history(),
                leans=_leans(),
                win_totals=_win_totals(),
                win_predictions=_predictions(),
                team_history=_team_history(),
                store=store,
                create_fn=create_fn,
            )
        self.assertEqual(store.rows[0]["generation_status"], "invalid")

    def test_expert_configuration_is_versioned(self) -> None:
        expert = load_expert("win_total")

        self.assertEqual(expert["version"], 3)
        self.assertEqual(expert["prompt_version"], 3)
        self.assertEqual(expert["output_schema_version"], 3)
        self.assertEqual(
            expert["prompt_path"], "moe/prompts/win_total/v3.md"
        )
        self.assertEqual(expert["default_model"], "claude-opus-4-8")
        self.assertEqual(expert["reasoning_effort"], "max")


if __name__ == "__main__":
    unittest.main()
