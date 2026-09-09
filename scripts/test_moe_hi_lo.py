#!/usr/bin/env python3
"""Tests for the NFL Hi Lo Expert."""

from __future__ import annotations

import csv
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from moe import generate_opinion, load_expert
from moe_hi_lo import build_hi_lo_input
from nfl_lines import (
    LATEST_AWAY_COLUMN,
    LATEST_HOME_COLUMN,
    LATEST_TOTALS_COLUMN,
)


def _game(
    event_id: str,
    *,
    home_spread: float,
    total: float,
) -> dict:
    away_spread = -home_spread
    return {
        "event_id": event_id,
        "season": 2026,
        "season_type": "regular",
        "week": 1,
        "commence_time_utc": "2026-09-10T00:20:00+00:00",
        "away_team": f"{event_id} Away",
        "home_team": f"{event_id} Home",
        LATEST_AWAY_COLUMN: (
            f"{away_spread},-110,nodata|nodata,nodata,nodata|"
            "nodata,nodata,nodata"
        ),
        LATEST_HOME_COLUMN: (
            f"{home_spread},-110,nodata|nodata,nodata,nodata|"
            "nodata,nodata,nodata"
        ),
        LATEST_TOTALS_COLUMN: (
            f"{total},-110,-110|nodata,nodata,nodata|"
            "nodata,nodata,nodata"
        ),
    }


def _history_file(path: Path) -> None:
    headers = [
        "season",
        "week",
        "away_team",
        "home_team",
        "away_score",
        "home_score",
        "home_spread",
        "total",
        "away_moneyline",
        "home_moneyline",
    ]
    rows = [
        [2024, 1, "A", "B", 20, 24, -7, 40, 250, -300],
        [2024, 1, "C", "D", 17, 20, -3, 50, 130, -150],
        [2025, 1, "E", "F", 17, 21, 7, 39, -300, 250],
        [2025, 1, "G", "H", 20, 27, -4, 47, 170, -190],
    ]
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(headers)
        writer.writerows(rows)


class MemoryStore:
    def __init__(self) -> None:
        self.rows: list[dict] = []

    def append(self, row: dict) -> None:
        self.rows.append(dict(row))

    def list(self, event_id: str | None = None) -> list[dict]:
        return list(self.rows)


class HiLoInputTests(unittest.TestCase):
    def test_builds_tie_aware_week_and_historical_extrema(self) -> None:
        games = [
            _game("target", home_spread=7, total=40),
            _game("other", home_spread=-3, total=50),
            _game("spread-tie", home_spread=-7, total=45),
            _game("fourth", home_spread=-2, total=47),
        ]
        with tempfile.TemporaryDirectory() as directory:
            history = Path(directory) / "history.csv"
            _history_file(history)
            payload = build_hi_lo_input(
                games[0],
                games,
                historical_path=history,
            )

        by_category = {
            item["category"]: item
            for item in payload["current_game_outliers"]
        }
        self.assertEqual(
            set(by_category),
            {"largest_spread", "lowest_total"},
        )
        self.assertEqual(by_category["largest_spread"]["tie_count"], 2)
        self.assertEqual(
            by_category["largest_spread"]["selected_side"],
            "target Home",
        )
        self.assertEqual(by_category["lowest_total"]["distance_to_next"], 5)
        self.assertEqual(
            payload["season_extrema"]["game.lowest_total"]["value"],
            40,
        )
        spread = payload["historical_weekly_extrema"]["largest_spread"]
        self.assertEqual(spread["weeks"], 2)
        self.assertEqual(spread["observations"], 2)
        self.assertEqual(spread["record"], "2-0-0")
        under = payload["historical_weekly_extrema"]["lowest_total"]
        self.assertEqual(under["record"], "1-1-0")

    def test_period_extreme_is_retained_as_explicit_no_signal(self) -> None:
        target = _game("target", home_spread=-3, total=45)
        other = _game("other", home_spread=-3, total=45)
        third = _game("third", home_spread=-2, total=46)
        fourth = _game("fourth", home_spread=-4, total=44)
        target[LATEST_TOTALS_COLUMN] = (
            "45,-110,-110|18.5,-110,-110|nodata,nodata,nodata"
        )
        other[LATEST_TOTALS_COLUMN] = (
            "45,-110,-110|21.5,-110,-110|nodata,nodata,nodata"
        )
        third[LATEST_TOTALS_COLUMN] = (
            "46,-110,-110|20.5,-110,-110|nodata,nodata,nodata"
        )
        fourth[LATEST_TOTALS_COLUMN] = (
            "44,-110,-110|22.5,-110,-110|nodata,nodata,nodata"
        )
        with tempfile.TemporaryDirectory() as directory:
            history = Path(directory) / "history.csv"
            _history_file(history)
            payload = build_hi_lo_input(
                target,
                [target, other, third, fourth],
                historical_path=history,
            )

        period = next(
            item
            for item in payload["current_game_outliers"]
            if item["period"] == "first_half"
            and item["category"] == "lowest_total"
        )
        self.assertEqual(period["value"], 18.5)
        self.assertEqual(
            payload["historical_weekly_extrema"][
                "first_half"
            ]["status"],
            "unavailable",
        )

    def test_sparse_period_board_cannot_create_an_outlier(self) -> None:
        games = [
            _game("target", home_spread=-3, total=45),
            _game("other", home_spread=-7, total=40),
            _game("third", home_spread=-2, total=50),
            _game("fourth", home_spread=-4, total=47),
            _game("fifth", home_spread=-5, total=46),
            _game("sixth", home_spread=-6, total=44),
        ]
        games[0][LATEST_TOTALS_COLUMN] = (
            "45,-110,-110|18.5,-110,-110|nodata,nodata,nodata"
        )
        with tempfile.TemporaryDirectory() as directory:
            history = Path(directory) / "history.csv"
            _history_file(history)
            payload = build_hi_lo_input(
                games[0],
                games,
                historical_path=history,
            )

        self.assertEqual(payload["current_game_outliers"], [])
        period = next(
            item
            for item in payload["weekly_market_positions"]
            if item["period"] == "first_half"
        )
        self.assertFalse(period["board_eligible"])
        self.assertFalse(period["is_weekly_extreme"])

    def test_does_not_mix_preseason_and_regular_week_one(self) -> None:
        regular = [
            _game("target", home_spread=-7, total=40),
            _game("other", home_spread=-3, total=45),
            _game("third", home_spread=-2, total=50),
            _game("fourth", home_spread=-4, total=47),
        ]
        preseason = _game("preseason", home_spread=-14, total=35)
        preseason["season_type"] = "preseason"
        with tempfile.TemporaryDirectory() as directory:
            history = Path(directory) / "history.csv"
            _history_file(history)
            payload = build_hi_lo_input(
                regular[0],
                [*regular, preseason],
                historical_path=history,
            )

        spread = next(
            item
            for item in payload["current_game_outliers"]
            if item["category"] == "largest_spread"
        )
        self.assertEqual(spread["value"], 7)
        self.assertEqual(spread["week_games"], 4)
        self.assertEqual(payload["game"]["season_type"], "regular")

    def test_non_outlier_game_keeps_current_market_positions(self) -> None:
        games = [
            _game("low", home_spread=-1, total=40),
            _game("target", home_spread=-3, total=45),
            _game("high", home_spread=-7, total=50),
            _game("middle", home_spread=-4, total=47),
        ]
        with tempfile.TemporaryDirectory() as directory:
            history = Path(directory) / "history.csv"
            _history_file(history)
            payload = build_hi_lo_input(
                games[1],
                games,
                historical_path=history,
            )

        self.assertEqual(payload["current_game_outliers"], [])
        self.assertFalse(
            payload["weekly_outlier_status"]["has_eligible_outlier"]
        )
        self.assertEqual(
            payload["weekly_outlier_status"]["statement"],
            "This game has no eligible weekly market extreme.",
        )
        full_game = [
            item
            for item in payload["weekly_market_positions"]
            if item["period"] == "game"
        ]
        self.assertTrue(full_game)
        self.assertTrue(all(item["rank"] > 1 for item in full_game))


class HiLoGenerationTests(unittest.IsolatedAsyncioTestCase):
    async def test_valid_outlier_opinion_is_approved(self) -> None:
        games = [
            _game("target", home_spread=7, total=40),
            _game("other", home_spread=-3, total=50),
            _game("third", home_spread=-2, total=45),
            _game("fourth", home_spread=-4, total=47),
        ]
        payload = build_hi_lo_input(games[0], games)
        outlier_text = "; ".join(
            (
                f"{item['period']} {item['category']} value {item['value']}, "
                f"{item['selected_side']}, tie count {item['tie_count']}, "
                f"{item['games_with_market']} games with market"
                + (
                    f", distance to next {item['distance_to_next']}"
                    if item["distance_to_next"] is not None
                    else ""
                )
            )
            for item in payload["current_game_outliers"]
        )
        historical_factors = []
        for category, summary in payload[
            "historical_weekly_extrema"
        ].items():
            if "observations" not in summary:
                continue
            prefix = f"historical_weekly_extrema.{category}"
            historical_factors.append(
                {
                    "claim": (
                        f"{summary['selection']} has {summary['weeks']} "
                        f"weeks, {summary['observations']} observations, "
                        f"record {summary['record']}."
                    ),
                    "evidence_paths": [prefix],
                }
            )
        position_text = "; ".join(
            (
                f"{item['category']} value {item['value']}, "
                f"{item['selected_side']}, rank {item['rank']}, "
                f"{item['games_with_market']} games with market"
            )
            for item in payload["weekly_market_positions"]
            if item["period"] == "game" and item["board_eligible"]
        )
        output = {
            "predicted_winner": "target Away",
            "predicted_away_score": 21,
            "predicted_home_score": 20,
            "home_win_probability": 0.48,
            "expected_home_margin": -1.0,
            "confidence_stars": 2,
            "thesis": {
                "claim": f"{outlier_text}.",
                "evidence_paths": [
                    "current_game_outliers",
                ],
            },
            "supporting_factors": [
                {
                    "claim": (
                        "The target values tie the supplied season extrema."
                    ),
                    "evidence_paths": ["season_extrema"],
                },
                {
                    "claim": payload["weekly_outlier_status"]["statement"],
                    "evidence_paths": ["weekly_outlier_status"],
                },
                *historical_factors,
            ],
            "counterarguments": [
                {
                    "claim": position_text,
                    "evidence_paths": ["weekly_market_positions"],
                }
            ],
            "no_signal_factors": [
                {
                    "claim": (
                        "Team quality and current-week results are "
                        "prohibited inputs."
                    ),
                    "evidence_paths": ["data_limits"],
                }
            ],
            "discarded_considerations": [
                "Injuries and team strength are prohibited."
            ],
        }

        async def create_fn(**_kwargs):
            return SimpleNamespace(
                content=[SimpleNamespace(text=json.dumps(output))]
            )

        store = MemoryStore()
        row = await generate_opinion(
            expert_id="hi_lo",
            game=games[0],
            games=games,
            history=[],
            store=store,
            create_fn=create_fn,
        )

        self.assertEqual(row["generation_status"], "valid")
        self.assertEqual(row["review_status"], "approved")
        self.assertEqual(row["pick_market"], "straight_up")
        self.assertEqual(row["expert_id"], "hi_lo")

    def test_expert_configuration(self) -> None:
        expert = load_expert("hi_lo")

        self.assertEqual(expert["version"], 1)
        self.assertEqual(expert["prompt_version"], 2)
        self.assertEqual(expert["input_profile"], "hi_lo_outliers")
        self.assertEqual(expert["output_schema_version"], 3)
        self.assertTrue(expert["committee_optional"])
        self.assertEqual(expert["markets"], ["side", "total"])


if __name__ == "__main__":
    unittest.main()
