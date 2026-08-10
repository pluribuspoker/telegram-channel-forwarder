#!/usr/bin/env python3
"""Unit tests for NFL win prediction backend data."""

from __future__ import annotations

import sys
import unittest
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from nfl_win_predictions import (
    TEAM_ABBREVIATIONS,
    build_latest_prediction_rows,
    build_rank_benchmarks,
    build_team_history,
    build_win_total_rows,
    parse_standings,
    validate_prediction_revision,
)


def _standings_payload(season: int) -> dict:
    teams = list(TEAM_ABBREVIATIONS)
    divisions = []
    for division_index in range(8):
        entries = []
        for rank, team in enumerate(
            teams[division_index * 4 : division_index * 4 + 4],
            start=1,
        ):
            wins = 13 - rank - (season % 2)
            entries.append(
                {
                    "team": {"displayName": team},
                    "stats": [
                        {"name": "wins", "value": wins},
                        {"name": "losses", "value": 17 - wins},
                        {"name": "ties", "value": 0},
                        {"name": "playoffSeed", "value": rank},
                    ],
                }
            )
        divisions.append(
            {
                "name": f"Division {division_index + 1}",
                "standings": {"entries": entries},
            }
        )
    return {
        "children": [
            {"name": "AFC", "children": divisions[:4]},
            {"name": "NFC", "children": divisions[4:]},
        ]
    }


class WinPredictionBackendTest(unittest.TestCase):
    def test_parse_and_build_two_transition_cohorts(self) -> None:
        standings = {
            season: parse_standings(_standings_payload(season), season)
            for season in (2023, 2024, 2025)
        }

        history = build_team_history(standings)
        benchmarks = build_rank_benchmarks(history)

        self.assertEqual(len(history), 96)
        self.assertEqual(len(benchmarks), 4)
        self.assertEqual(
            {row["sample_size"] for row in benchmarks}, {16}
        )
        self.assertEqual(
            {row["cohorts"] for row in benchmarks},
            {"2023->2024,2024->2025"},
        )

    def test_division_rank_uses_playoff_seed_not_payload_order(self) -> None:
        payload = _standings_payload(2025)
        entries = payload["children"][0]["children"][0]["standings"][
            "entries"
        ]
        entries[:] = [entries[1], entries[2], entries[3], entries[0]]

        rows = parse_standings(payload, 2025)
        division = [
            row for row in rows if row["division"] == "Division 1"
        ]

        self.assertEqual(
            [row["division_rank"] for row in division], [1, 2, 3, 4]
        )
        self.assertEqual(
            [row["team"] for row in division],
            list(TEAM_ABBREVIATIONS)[:4],
        )

    def test_win_total_seed_requires_all_32_teams(self) -> None:
        totals = {
            team: 8.5 for team in TEAM_ABBREVIATIONS
        }

        rows = build_win_total_rows(
            totals,
            season=2026,
            captured_at=datetime.fromisoformat(
                "2026-08-09T23:34:34-04:00"
            ),
        )

        self.assertEqual(len(rows), 32)
        self.assertEqual(rows[0]["team_abbreviation"], "ARI")
        self.assertEqual(rows[0]["captured_at_et"][-6:], "-04:00")

    def test_revision_requires_integer_prediction_for_every_team(self) -> None:
        rows = [
            {"team": team, "predicted_wins": 9}
            for team in TEAM_ABBREVIATIONS
        ]

        validate_prediction_revision(rows)
        rows[0]["predicted_wins"] = 9.5

        with self.assertRaisesRegex(ValueError, "whole numbers"):
            validate_prediction_revision(rows)

    def test_latest_queue_sorts_unmarked_then_abbreviation(self) -> None:
        totals = [
            {
                "season": 2026,
                "team": team,
                "team_abbreviation": abbreviation,
                "win_total": 8.5,
                "captured_at_et": "2026-08-09T23:34:34-04:00",
            }
            for team, abbreviation in TEAM_ABBREVIATIONS.items()
        ]
        predictions = [
            {
                "revision_id": "revision-1",
                "submitted_at_utc": "2026-08-10T01:00:00+00:00",
                "submitted_at_et": "2026-08-09T21:00:00-04:00",
                "telegram_user_id": "123",
                "telegram_username": "guesser",
                "telegram_display_name": "NFL Guesser",
                "season": 2026,
                "team": "Arizona Cardinals",
                "predicted_wins": 6,
            }
        ]

        latest = build_latest_prediction_rows(predictions, totals)

        self.assertEqual(len(latest), 32)
        self.assertEqual(latest[0]["team_abbreviation"], "ATL")
        self.assertEqual(latest[-1]["team_abbreviation"], "ARI")
        self.assertEqual(latest[-1]["guess_count"], 1)
        self.assertEqual(latest[-1]["predicted_wins"], 6)


if __name__ == "__main__":
    unittest.main()
