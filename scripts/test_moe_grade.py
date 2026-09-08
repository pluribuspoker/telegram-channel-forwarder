#!/usr/bin/env python3
"""Tests for the daily grading run's operator DM (scripts/moe_grade.py).

Unix only, like the rest of the God Expert suite: importing the script pulls
in ``moe`` (fcntl). Run on the VPS scratch clone via scripts/godbuild_test.sh.
"""

from __future__ import annotations

import unittest

from moe_god import GRADES_TAB, MEAN_OF_ARMS_ID
from scripts.moe_grade import (
    NOTIFY_MAX_CHARS,
    NOTIFY_MAX_GAMES,
    _record_line,
    notification_text,
)


def _record(resolved: int, brier: float | None) -> dict:
    return {
        "resolved": resolved,
        "brier": brier,
        "ats": {"w": 1, "l": 0, "p": 0},
        "ou": {"w": 0, "l": 1, "p": 0},
        "legs": {"w": 0, "l": 0, "p": 0},
        "clv_points_mean": None,
        "clv_legs": 0,
    }


def _row(opinion_id: str, expert_id: str, away: str, home: str, final: str) -> dict:
    return {
        "opinion_id": opinion_id,
        "expert_id": expert_id,
        "away_team": away,
        "home_team": home,
        "final": final,
    }


class NotificationTextTests(unittest.TestCase):
    def setUp(self) -> None:
        self.scoreboard = {
            "resolved_games": 2,
            "graded_opinions": 5,
            "by_expert": {
                "schedule": _record(2, 0.2101),
                "god_rules": _record(0, None),
            },
        }
        self.new_rows = [
            _row("a1", "schedule", "Patriots", "Seahawks", "13-17"),
            _row("a2", "god_rules", "Patriots", "Seahawks", "13-17"),
            _row("a3", "schedule", "49ers", "Rams", "20-24"),
            _row("mean:r:j", MEAN_OF_ARMS_ID, "Patriots", "Seahawks", "13-17"),
        ]

    def test_header_games_and_one_line_per_expert(self) -> None:
        text = notification_text(
            season=2026, scoreboard=self.scoreboard, new_rows=self.new_rows
        )
        lines = text.split("\n")
        self.assertEqual(
            lines[0],
            "pickbot: MOE grades, season 2026: 2 resolved games, 5 graded opinions",
        )
        self.assertEqual(
            lines[1], f"+3 opinion rows, +1 mean-of-arms appended to {GRADES_TAB}"
        )
        # Each final once, in ledger order.
        self.assertEqual(text.count("Patriots @ Seahawks 13-17"), 1)
        self.assertLess(
            text.index("Patriots @ Seahawks 13-17"), text.index("49ers @ Rams 20-24")
        )
        # The scoreboard lines are the terminal's, sorted by expert id; the
        # mean-of-arms row never appears as an expert.
        self.assertIn(_record_line("god_rules", _record(0, None)), lines)
        self.assertIn(_record_line("schedule", _record(2, 0.2101)), lines)
        self.assertLess(text.index("god_rules"), text.index("schedule    "))
        self.assertNotIn(MEAN_OF_ARMS_ID, text)
        self.assertLessEqual(len(text), NOTIFY_MAX_CHARS)

    def test_games_are_capped(self) -> None:
        rows = [
            _row(f"o{i}", "schedule", f"Away{i}", f"Home{i}", f"{i}-{i + 1}")
            for i in range(NOTIFY_MAX_GAMES + 7)
        ]
        board = {
            "resolved_games": len(rows),
            "graded_opinions": len(rows),
            "by_expert": {"schedule": _record(len(rows), 0.25)},
        }
        text = notification_text(season=2026, scoreboard=board, new_rows=rows)
        self.assertIn(f"Away{NOTIFY_MAX_GAMES - 1} @", text)
        self.assertNotIn(f"Away{NOTIFY_MAX_GAMES} @", text)
        self.assertIn("+7 more games", text)
        self.assertFalse(text.endswith("…"))
        self.assertLessEqual(len(text), NOTIFY_MAX_CHARS)

    def test_long_text_is_truncated_under_the_telegram_limit(self) -> None:
        # 80 scoreboard lines of ~80 characters is well past the cap.
        board = {
            "resolved_games": 1,
            "graded_opinions": 80,
            "by_expert": {f"expert_{i:02d}": _record(1, 0.25) for i in range(80)},
        }
        text = notification_text(
            season=2026, scoreboard=board, new_rows=self.new_rows[:1]
        )
        self.assertEqual(len(text), NOTIFY_MAX_CHARS)
        self.assertTrue(text.endswith("…"))
        self.assertTrue(text.startswith("pickbot: MOE grades, season 2026"))


if __name__ == "__main__":
    unittest.main()
