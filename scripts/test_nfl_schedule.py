#!/usr/bin/env python3
"""Tests for the complete ESPN NFL schedule."""

from __future__ import annotations

import unittest
from datetime import datetime, timedelta, timezone

from nfl_schedule import parse_schedule_event, validate_schedule


def _event(
    event_id: int,
    *,
    week: int,
    away: str,
    home: str,
) -> dict:
    kickoff = datetime(2026, 9, 6, tzinfo=timezone.utc) + timedelta(
        days=event_id
    )
    return {
        "id": str(event_id),
        "date": kickoff.isoformat(),
        "season": {"year": 2026, "type": 2},
        "week": {"number": week},
        "status": {"type": {"name": "STATUS_SCHEDULED"}},
        "competitions": [
            {
                "neutralSite": False,
                "competitors": [
                    {
                        "homeAway": "away",
                        "team": {"displayName": away},
                    },
                    {
                        "homeAway": "home",
                        "team": {"displayName": home},
                    },
                ],
            }
        ],
    }


class NflScheduleTest(unittest.TestCase):
    def test_parses_authoritative_week(self) -> None:
        row = parse_schedule_event(
            _event(1, week=12, away="Away", home="Home")
        )

        self.assertEqual(row["week"], 12)
        self.assertEqual(row["season_type"], "regular")
        self.assertEqual(row["away_team"], "Away")
        self.assertEqual(row["home_team"], "Home")

    def test_validation_requires_weeks_one_through_eighteen(self) -> None:
        rows = [
            {
                "event_id": str(index),
                "season": 2026,
                "week": 1,
                "away_team": f"Team {index % 32}",
                "home_team": f"Team {(index + 1) % 32}",
            }
            for index in range(272)
        ]

        with self.assertRaisesRegex(ValueError, "cover 1-18"):
            validate_schedule(rows, season=2026)


if __name__ == "__main__":
    unittest.main()
