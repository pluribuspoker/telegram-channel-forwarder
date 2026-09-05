#!/usr/bin/env python3
"""Unit tests for completed NFL game history."""

from __future__ import annotations

import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from nfl_game_history import (
    _as_bool,
    build_game_history,
    fetch_regular_season_events,
    summarize_game_history,
)


def _standings() -> list[dict]:
    rows = []
    for conference in ("AFC", "NFC"):
        for division_index in range(4):
            division = f"{conference} Division {division_index + 1}"
            for team_index in range(4):
                rows.append(
                    {
                        "team": (
                            f"{conference}-{division_index + 1}-"
                            f"{team_index + 1}"
                        ),
                        "conference": conference,
                        "division": division,
                    }
                )
    return rows


def _event(
    event_id: int,
    *,
    season: int,
    week: int,
    away: str,
    home: str,
    away_score: int = 17,
    home_score: int = 24,
    neutral: bool = False,
    period: int = 4,
    kickoff: datetime | None = None,
) -> dict:
    kickoff = kickoff or (
        datetime(season, 9, 1, tzinfo=timezone.utc)
        + timedelta(days=event_id)
    )
    competitor = lambda team, home_away, score: {
        "homeAway": home_away,
        "team": {"displayName": team},
        "score": str(score),
    }
    return {
        "id": str(event_id),
        "date": kickoff.isoformat(),
        "season": {"year": season, "type": 2},
        "week": {"number": week},
        "status": {
            "period": period,
            "type": {"completed": True},
        },
        "competitions": [
            {
                "neutralSite": neutral,
                "status": {"period": period},
                "competitors": [
                    competitor(home, "home", home_score),
                    competitor(away, "away", away_score),
                ],
            }
        ],
    }


class GameHistoryTest(unittest.TestCase):
    def test_classifies_matchups_and_numbers_divisional_meetings(self) -> None:
        standings = _standings()
        events = [
            _event(
                20,
                season=2025,
                week=1,
                away="AFC-1-1",
                home="AFC-1-2",
                kickoff=datetime(2025, 9, 1, tzinfo=timezone.utc),
            ),
            _event(
                10,
                season=2025,
                week=10,
                away="AFC-1-2",
                home="AFC-1-1",
                away_score=20,
                home_score=20,
                period=5,
                kickoff=datetime(2025, 11, 1, tzinfo=timezone.utc),
            ),
            _event(
                3,
                season=2025,
                week=2,
                away="AFC-2-1",
                home="AFC-1-1",
            ),
            _event(
                4,
                season=2025,
                week=3,
                away="NFC-1-1",
                home="AFC-1-1",
                neutral=True,
            ),
        ]

        rows = build_game_history(
            {2025: events},
            {2025: standings},
            validate=False,
        )

        by_id = {row["event_id"]: row for row in rows}
        self.assertEqual(by_id["20"]["division_meeting_number"], 1)
        self.assertEqual(by_id["10"]["division_meeting_number"], 2)
        self.assertEqual(by_id["20"]["tags"], "divisional_game_1,week_1")
        self.assertEqual(by_id["10"]["home_result"], "T")
        self.assertTrue(by_id["10"]["overtime"])
        self.assertEqual(by_id["3"]["matchup_type"], "conference")
        self.assertEqual(by_id["3"]["tags"], "conference_game")
        self.assertEqual(by_id["4"]["matchup_type"], "non_conference")
        self.assertEqual(
            by_id["4"]["tags"],
            "non_conference_game,neutral_site",
        )

    def test_summary_reports_week_one_divisional_home_record(self) -> None:
        rows = [
            {
                "season": 2025,
                "week": 1,
                "same_division": True,
                "matchup_type": "division",
                "home_result": result,
            }
            for result in ("W", "W", "L", "T")
        ]

        summary = summarize_game_history(rows)[0]

        self.assertEqual(summary["week_1_division_games"], 4)
        self.assertEqual(summary["week_1_division_home_record"], "2-1-1")

    def test_current_partial_season_allows_first_division_meeting(self) -> None:
        standings = _standings()
        rows = build_game_history(
            {
                2026: [
                    _event(
                        20,
                        season=2026,
                        week=3,
                        away="AFC-1-1",
                        home="AFC-1-2",
                    )
                ]
            },
            {2026: standings},
            validate=False,
            require_complete_divisional_pairs=False,
        )

        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["division_meeting_number"], 1)

    def test_fetches_two_calendar_years_and_filters_season(self) -> None:
        def response(request: httpx.Request) -> httpx.Response:
            year = int(request.url.params["dates"])
            events = [
                {
                    "id": f"{year}-regular",
                    "date": f"{year}-09-01T00:00Z",
                    "season": {"year": 2025, "type": 2},
                    "status": {"type": {"completed": True}},
                },
                {
                    "id": f"{year}-postseason",
                    "date": f"{year}-09-02T00:00Z",
                    "season": {"year": 2025, "type": 3},
                    "status": {"type": {"completed": True}},
                },
                {
                    "id": f"{year}-incomplete",
                    "date": f"{year}-09-03T00:00Z",
                    "season": {"year": 2025, "type": 2},
                    "status": {"type": {"completed": False}},
                },
            ]
            return httpx.Response(200, json={"events": events})

        with httpx.Client(transport=httpx.MockTransport(response)) as client:
            events = fetch_regular_season_events(
                2025,
                client=client,
                expected_games=2,
            )

        self.assertEqual(
            [event["id"] for event in events],
            ["2025-regular", "2026-regular"],
        )

    def test_sheet_boolean_strings_are_normalized(self) -> None:
        self.assertTrue(_as_bool("TRUE"))
        self.assertFalse(_as_bool("FALSE"))
        self.assertTrue(_as_bool(True))
        self.assertFalse(_as_bool(False))
        with self.assertRaisesRegex(ValueError, "Invalid boolean"):
            _as_bool("yes")


if __name__ == "__main__":
    unittest.main()
