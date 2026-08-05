#!/usr/bin/env python3
"""Unit tests for BetOnline NFL line parsing and snapshot decisions."""

from __future__ import annotations

import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from nfl_lines import (
    AWAY_SNAPSHOT_COLUMN,
    HOME_SNAPSHOT_COLUMN,
    LATEST_AWAY_COLUMN,
    OPENING_AWAY_COLUMN,
    OPENING_HOME_COLUMN,
    OPENING_TOTALS_COLUMN,
    TOTALS_SNAPSHOT_COLUMN,
    deduplicate_games,
    indexed_records,
    new_game_row,
    parse_game,
    poll_interval_for_game,
    preserve_unchecked_periods,
    scheduled_poll_plan,
    should_append_snapshot,
    snapshot_row,
    update_game_row,
)


def _payload() -> dict:
    return {
        "id": "game-1",
        "sport_key": "americanfootball_nfl_preseason",
        "commence_time": "2026-08-10T17:00:00Z",
        "home_team": "Buffalo Bills",
        "away_team": "Miami Dolphins",
        "bookmakers": [
            {
                "key": "betonlineag",
                "title": "BetOnline.ag",
                "markets": [
                    {
                        "key": "h2h",
                        "outcomes": [
                            {"name": "Miami Dolphins", "price": 130},
                            {"name": "Buffalo Bills", "price": -150},
                        ],
                    },
                    {
                        "key": "spreads",
                        "outcomes": [
                            {
                                "name": "Miami Dolphins",
                                "point": 3.0,
                                "price": -110,
                            },
                            {
                                "name": "Buffalo Bills",
                                "point": -3.0,
                                "price": -110,
                            },
                        ],
                    },
                    {
                        "key": "totals",
                        "outcomes": [
                            {"name": "Over", "point": 44.5, "price": -105},
                            {"name": "Under", "point": 44.5, "price": -115},
                        ],
                    },
                    {
                        "key": "h2h_h1",
                        "outcomes": [
                            {"name": "Miami Dolphins", "price": 115},
                            {"name": "Buffalo Bills", "price": -135},
                        ],
                    },
                    {
                        "key": "spreads_h1",
                        "outcomes": [
                            {
                                "name": "Miami Dolphins",
                                "point": 1.5,
                                "price": -105,
                            },
                            {
                                "name": "Buffalo Bills",
                                "point": -1.5,
                                "price": -115,
                            },
                        ],
                    },
                    {
                        "key": "totals_h1",
                        "outcomes": [
                            {"name": "Over", "point": 21.5, "price": -110},
                            {"name": "Under", "point": 21.5, "price": -110},
                        ],
                    },
                    {
                        "key": "h2h_q1",
                        "outcomes": [
                            {"name": "Miami Dolphins", "price": 105},
                            {"name": "Buffalo Bills", "price": -125},
                        ],
                    },
                    {
                        "key": "spreads_q1",
                        "outcomes": [
                            {
                                "name": "Miami Dolphins",
                                "point": 0.5,
                                "price": -110,
                            },
                            {
                                "name": "Buffalo Bills",
                                "point": -0.5,
                                "price": -110,
                            },
                        ],
                    },
                    {
                        "key": "totals_q1",
                        "outcomes": [
                            {"name": "Over", "point": 10.5, "price": -105},
                            {"name": "Under", "point": 10.5, "price": -115},
                        ],
                    },
                ],
            }
        ],
    }


def _game(captured_at: datetime | None = None):
    return parse_game(
        _payload(),
        season_type="preseason",
        captured_at=captured_at or datetime(2026, 8, 4, tzinfo=timezone.utc),
        requests_used="3",
        requests_remaining="497",
    )


class ParseGameTest(unittest.TestCase):
    def test_parses_all_three_markets(self):
        game = _game()

        self.assertIsNotNone(game)
        self.assertEqual(game.away_team, "Miami Dolphins")
        self.assertEqual(game.away_spread, 3.0)
        self.assertEqual(game.away_moneyline, 130)
        self.assertEqual(game.home_moneyline, -150)
        self.assertEqual(game.total, 44.5)
        self.assertEqual(game.over_price, -105)
        self.assertEqual(game.under_price, -115)
        self.assertEqual(game.first_half_away_spread, 1.5)
        self.assertEqual(game.first_half_away_moneyline, 115)
        self.assertEqual(game.first_half_total, 21.5)
        self.assertEqual(game.first_quarter_away_spread, 0.5)
        self.assertEqual(game.first_quarter_away_moneyline, 105)
        self.assertEqual(game.first_quarter_total, 10.5)
        self.assertEqual(game.season_type, "preseason")

    def test_skips_game_without_betonline(self):
        payload = _payload()
        payload["bookmakers"][0]["key"] = "draftkings"

        game = parse_game(
            payload,
            season_type="preseason",
            captured_at=datetime(2026, 8, 4, tzinfo=timezone.utc),
            requests_used="",
            requests_remaining="",
        )

        self.assertIsNone(game)

    def test_assigns_january_game_to_previous_nfl_season(self):
        payload = _payload()
        payload["commence_time"] = "2027-01-10T18:00:00Z"

        game = parse_game(
            payload,
            season_type="regular",
            captured_at=datetime(2026, 8, 4, tzinfo=timezone.utc),
            requests_used="",
            requests_remaining="",
        )

        self.assertEqual(game.season, 2026)


class GameRowTest(unittest.TestCase):
    def test_update_preserves_opening_and_changes_latest(self):
        opening = _game()
        existing = new_game_row(opening)
        payload = _payload()
        payload["bookmakers"][0]["markets"][1]["outcomes"][0]["point"] = 2.5
        latest = parse_game(
            payload,
            season_type="preseason",
            captured_at=datetime(2026, 8, 4, 1, tzinfo=timezone.utc),
            requests_used="6",
            requests_remaining="494",
        )

        updated = update_game_row(existing, latest)

        self.assertTrue(updated[OPENING_AWAY_COLUMN].startswith("3,"))
        self.assertTrue(updated[LATEST_AWAY_COLUMN].startswith("2.5,"))

    def test_first_available_market_initializes_blank_opening(self):
        opening = _game()
        existing = new_game_row(opening)
        existing[OPENING_AWAY_COLUMN] = (
            "3.0,-110,nodata|1.5,-105,115|0.5,-110,105"
        )
        existing[OPENING_HOME_COLUMN] = (
            "-3.0,-110,nodata|-1.5,-115,-135|-0.5,-110,-125"
        )

        updated = update_game_row(existing, opening)

        self.assertTrue(updated[OPENING_AWAY_COLUMN].startswith("3,-110,130"))
        self.assertTrue(updated[OPENING_HOME_COLUMN].startswith("-3,-110,-150"))

    def test_first_available_period_market_initializes_blank_opening(self):
        opening = _game()
        existing = new_game_row(opening)
        existing[OPENING_TOTALS_COLUMN] = (
            "44.5,-105,-115|nodata,nodata,nodata|10.5,-105,-115"
        )

        updated = update_game_row(existing, opening)

        self.assertIn("|21.5,-110,-110|", updated[OPENING_TOTALS_COLUMN])

    def test_full_game_only_poll_preserves_latest_period_markets(self):
        previous = _game()
        existing = new_game_row(previous)
        payload = _payload()
        payload["bookmakers"][0]["markets"] = [
            market
            for market in payload["bookmakers"][0]["markets"]
            if not market["key"].endswith(("_h1", "_q1"))
        ]
        full_game_only = parse_game(
            payload,
            season_type="preseason",
            captured_at=datetime(2026, 8, 4, 1, tzinfo=timezone.utc),
            requests_used="",
            requests_remaining="",
            period_checked_at=None,
        )

        merged = preserve_unchecked_periods(full_game_only, existing)
        updated = update_game_row(existing, merged)

        self.assertIn("|1.5,-105,115|", updated[LATEST_AWAY_COLUMN])
        self.assertFalse(
            should_append_snapshot(merged, snapshot_row(previous))
        )


class IndexedRecordsTest(unittest.TestCase):
    def test_preserves_physical_row_numbers_across_blank_rows(self):
        values = [
            ["event_id", "away_team"],
            ["game-1", "Miami Dolphins"],
            [],
            ["game-2", "Buffalo Bills"],
        ]

        records = indexed_records(values, "event_id")

        self.assertEqual(records[0][0], 2)
        self.assertEqual(records[1][0], 4)
        self.assertEqual(records[1][1]["event_id"], "game-2")


class DuplicatePreventionTest(unittest.TestCase):
    def test_collapses_duplicate_events_by_event_id(self):
        first = _game()
        duplicate = _game(
            datetime(2026, 8, 4, 1, tzinfo=timezone.utc)
        )

        games = deduplicate_games([first, duplicate])

        self.assertEqual(len(games), 1)
        self.assertEqual(games[0].captured_at, duplicate.captured_at)


class PollScheduleTest(unittest.TestCase):
    NOW = datetime(2026, 8, 4, 12, tzinfo=timezone.utc)

    def test_poll_interval_bands(self):
        cases = [
            (timedelta(days=8), timedelta(days=1)),
            (timedelta(days=2), timedelta(hours=12)),
            (timedelta(hours=10), timedelta(hours=1)),
            (timedelta(hours=3), timedelta(minutes=30)),
        ]

        for until_kickoff, expected in cases:
            commence = (self.NOW + until_kickoff).isoformat()
            self.assertEqual(
                poll_interval_for_game(commence, self.NOW), expected
            )

    def test_period_poll_can_be_due_when_full_game_poll_is_not(self):
        records = [
            {
                "event_id": "game-1",
                "commence_time_utc": (
                    self.NOW + timedelta(hours=10)
                ).isoformat(),
                "last_updated_at": (
                    self.NOW - timedelta(minutes=30)
                ).isoformat(),
                "period_last_checked_at": (
                    self.NOW - timedelta(hours=1)
                ).isoformat(),
            }
        ]

        due, period_ids, known_ids = scheduled_poll_plan(
            records, self.NOW
        )

        self.assertTrue(due)
        self.assertEqual(period_ids, {"game-1"})
        self.assertEqual(known_ids, {"game-1"})

    def test_skips_when_neither_poll_is_due(self):
        records = [
            {
                "event_id": "game-1",
                "commence_time_utc": (
                    self.NOW + timedelta(days=2)
                ).isoformat(),
                "last_updated_at": (
                    self.NOW - timedelta(hours=11)
                ).isoformat(),
                "period_last_checked_at": (
                    self.NOW - timedelta(hours=11)
                ).isoformat(),
            }
        ]

        due, period_ids, _ = scheduled_poll_plan(records, self.NOW)

        self.assertFalse(due)
        self.assertEqual(period_ids, set())


class SnapshotDecisionTest(unittest.TestCase):
    def test_appends_first_snapshot(self):
        self.assertTrue(should_append_snapshot(_game(), None))

    def test_skips_unchanged_snapshot(self):
        first = _game()
        latest = _game(
            datetime(2026, 8, 4, tzinfo=timezone.utc)
            + timedelta(minutes=59)
        )

        self.assertFalse(
            should_append_snapshot(latest, snapshot_row(first))
        )

    def test_skips_unchanged_snapshot_after_one_hour(self):
        first = _game()
        latest = _game(
            datetime(2026, 8, 4, tzinfo=timezone.utc) + timedelta(hours=1)
        )

        self.assertFalse(should_append_snapshot(latest, snapshot_row(first)))

    def test_appends_changed_snapshot_immediately(self):
        first = _game()
        payload = _payload()
        payload["bookmakers"][0]["markets"][1]["outcomes"][0]["point"] = 2.5
        latest = parse_game(
            payload,
            season_type="preseason",
            captured_at=datetime(2026, 8, 4, 0, 5, tzinfo=timezone.utc),
            requests_used="6",
            requests_remaining="494",
        )

        self.assertTrue(
            should_append_snapshot(latest, snapshot_row(first))
        )

    def test_snapshot_compacts_period_markets_into_three_columns(self):
        row = snapshot_row(_game())

        self.assertEqual(
            row[AWAY_SNAPSHOT_COLUMN],
            "3.0,-110,130|1.5,-105,115|0.5,-110,105",
        )
        self.assertEqual(
            row[HOME_SNAPSHOT_COLUMN],
            "-3.0,-110,-150|-1.5,-115,-135|-0.5,-110,-125",
        )
        self.assertEqual(
            row[TOTALS_SNAPSHOT_COLUMN],
            "44.5,-105,-115|21.5,-110,-110|10.5,-105,-115",
        )
        self.assertNotIn("away_spread", row)

    def test_snapshot_uses_nodata_for_missing_markets(self):
        payload = _payload()
        payload["bookmakers"][0]["markets"] = [
            market
            for market in payload["bookmakers"][0]["markets"]
            if not market["key"].endswith(("_h1", "_q1"))
        ]
        game = parse_game(
            payload,
            season_type="preseason",
            captured_at=datetime(2026, 8, 4, tzinfo=timezone.utc),
            requests_used="",
            requests_remaining="",
        )

        row = snapshot_row(game)

        self.assertTrue(
            row[AWAY_SNAPSHOT_COLUMN].endswith(
                "|nodata,nodata,nodata|nodata,nodata,nodata"
            )
        )


if __name__ == "__main__":
    unittest.main()
