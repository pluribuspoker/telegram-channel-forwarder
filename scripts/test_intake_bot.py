#!/usr/bin/env python3
"""Unit tests for the native NFL game browser."""

from __future__ import annotations

import unittest
from unittest.mock import AsyncMock
from datetime import datetime, timedelta, timezone

from telethon.errors import MessageNotModifiedError

from intake_bot import (
    SUGGESTION_HEADERS,
    build_lean_row,
    build_suggestion_row,
    command_keyboard,
    edit_callback,
    game_browser,
    game_detail,
    implied_score,
    implied_score_tldr,
    market_buttons,
    market_side_summary,
    page_games,
    period_market_summary,
    select_games,
    selected_market_context,
    side_buttons,
    team_emoji,
)
from nfl_lines import (
    LEAN_HEADERS,
    LATEST_AWAY_COLUMN,
    LATEST_HOME_COLUMN,
    LATEST_TOTALS_COLUMN,
    OPENING_AWAY_COLUMN,
    OPENING_HOME_COLUMN,
    OPENING_TOTALS_COLUMN,
)


NOW = datetime(2026, 8, 4, 12, tzinfo=timezone.utc)


def _game(event_id: str, days: int, away: str = "Miami Dolphins") -> dict:
    packed_away = "3.5,-105,165|nodata,nodata,nodata|nodata,nodata,nodata"
    packed_home = "-3.5,-115,-190|nodata,nodata,nodata|nodata,nodata,nodata"
    packed_totals = "40.5,-110,-110|nodata,nodata,nodata|nodata,nodata,nodata"
    return {
        "event_id": event_id,
        "commence_time_utc": (NOW + timedelta(days=days)).isoformat(),
        "away_team": away,
        "home_team": "Las Vegas Raiders",
        "bookmaker": "BetOnline.ag",
        OPENING_AWAY_COLUMN: packed_away,
        OPENING_HOME_COLUMN: packed_home,
        OPENING_TOTALS_COLUMN: packed_totals,
        LATEST_AWAY_COLUMN: packed_away,
        LATEST_HOME_COLUMN: packed_home,
        LATEST_TOTALS_COLUMN: packed_totals,
    }


class GameSelectionTest(unittest.TestCase):
    def test_command_keyboard_is_persistent(self):
        keyboard = command_keyboard()

        self.assertTrue(keyboard.persistent)
        self.assertFalse(keyboard.single_use)
        self.assertEqual(
            keyboard.rows[0].buttons[0].text, "/guess_nfl_game"
        )
        self.assertEqual(keyboard.rows[0].buttons[1].text, "/suggest")

    def test_default_window_includes_only_next_ten_days(self):
        records = [_game("a", 1), _game("b", 10), _game("c", 11)]

        selected = select_games(records, days=10, now=NOW)

        self.assertEqual([game["event_id"] for game in selected], ["a", "b"])

    def test_pagination_clamps_page_and_limits_rows(self):
        games = [_game(str(index), 1) for index in range(14)]

        page, index, count = page_games(games, 99)

        self.assertEqual(index, 2)
        self.assertEqual(count, 3)
        self.assertEqual(len(page), 2)

    def test_browser_has_window_controls_and_game_callbacks(self):
        text, buttons = game_browser(
            [_game("miami", 1)], days=10, page=0, now=NOW
        )

        self.assertIn("next 10 days", text)
        callback_values = [
            button.data.decode()
            for row in buttons
            for button in row
        ]
        self.assertIn("game:10:0:miami", callback_values)
        self.assertIn("games:30:0", callback_values)
        self.assertIn("games:365:0", callback_values)

    def test_detail_includes_miami_and_all_periods(self):
        text, buttons = game_detail(_game("miami", 1), days=10, page=0)

        self.assertIn("Miami Dolphins", text)
        self.assertEqual(text.count("Miami Dolphins"), 1)
        self.assertIn("🐬", text)
        self.assertIn("Full game", text)
        self.assertIn("First half", text)
        self.assertIn("First quarter", text)
        self.assertIn("No BetOnline data yet", text)
        self.assertIn("<u>Total</u>: 40.5", text)
        self.assertNotIn("<u>Total</u>: +40.5", text)
        self.assertIn("Opening\n\n<u>Spread</u>", text)
        self.assertIn(
            "<u>Total</u>: 40.5 (O -110 / U -110)\n\nLatest\n<u>Spread</u>",
            text,
        )
        self.assertNotIn("Latest\n\n<u>Spread</u>", text)
        self.assertIn("<u>Moneyline</u>", text)
        self.assertEqual(
            [button.data.decode() for button in buttons[0]],
            ["period:game", "period:first_half", "period:first_quarter"],
        )

    def test_detail_uses_sheet_team_emoji_mapping(self):
        text, _ = game_detail(
            _game("miami", 1),
            days=10,
            page=0,
            team_emojis={
                "Miami Dolphins": "MIA",
                "Las Vegas Raiders": "LV",
            },
        )

        self.assertIn("MIA Miami Dolphins @ LV Las Vegas Raiders", text)
        self.assertIn("Final: MIA 18.5 · LV 22", text)
        self.assertNotIn("🐬", text)

    def test_missing_sheet_team_emoji_uses_football(self):
        self.assertEqual(team_emoji("Miami Dolphins", {}), "🏈")

    def test_side_buttons_follow_market(self):
        total_buttons = side_buttons(
            "total", "Miami Dolphins", "Las Vegas Raiders"
        )
        spread_buttons = side_buttons(
            "spread", "Miami Dolphins", "Las Vegas Raiders"
        )

        self.assertEqual(
            [button.data.decode() for button in total_buttons[0]],
            ["side:over", "side:under"],
        )
        self.assertEqual(
            [button.text for button in spread_buttons[0]],
            ["Miami Dolphins", "Las Vegas Raiders"],
        )
        self.assertEqual(
            total_buttons[1][0].data.decode(), "back:markets"
        )

    def test_market_buttons_include_back_to_periods(self):
        buttons = market_buttons()

        self.assertEqual(
            [button.data.decode() for button in buttons[0]],
            ["market:spread", "market:moneyline", "market:total"],
        )
        self.assertEqual(buttons[1][0].data.decode(), "back:game")

    def test_period_summary_shows_all_three_markets(self):
        text = period_market_summary(
            _game("miami", 1),
            period="game",
        )

        self.assertIn("<b>Full game</b>", text)
        self.assertIn("<u>Spread</u>", text)
        self.assertIn("<u>Moneyline</u>", text)
        self.assertIn("<u>Total</u>", text)
        self.assertIn("Choose a market:", text)

    def test_market_summary_shows_both_sides_before_selection(self):
        spread_text = market_side_summary(
            _game("miami", 1),
            period="game",
            market="spread",
        )
        total_text = market_side_summary(
            _game("miami", 1),
            period="game",
            market="total",
        )

        self.assertIn("Opening\n🐬 +3.5 (-105) · ☠️ -3.5 (-115)", spread_text)
        self.assertIn("Latest\n🐬 +3.5 (-105) · ☠️ -3.5 (-115)", spread_text)
        self.assertIn("Opening\nOver 40.5 (-110) · Under 40.5 (-110)", total_text)
        self.assertIn("Choose a side:", total_text)

    def test_selected_market_context_uses_requested_period_and_side(self):
        context = selected_market_context(
            _game("miami", 1),
            period="game",
            market="spread",
            side="away",
        )

        self.assertEqual(context["opening_line"], 3.5)
        self.assertEqual(context["opening_price"], -105)
        self.assertEqual(context["latest_line"], 3.5)
        self.assertEqual(context["latest_price"], -105)

    def test_lean_row_is_compact_and_duplicate_key_is_deterministic(self):
        row = build_lean_row(
            submitted_at=NOW,
            user_id=123,
            username="guesser",
            first_name="NFL",
            last_name="Fan",
            message_id=789,
            game=_game("miami", 1),
            period="game",
            market="total",
            side="over",
            lean_text="Over, but only at 40.5 or better.",
        )

        self.assertEqual(list(row), LEAN_HEADERS)
        self.assertEqual(row["submission_id"], "telegram:123:789")
        self.assertEqual(row["period"], "game")
        self.assertEqual(row["market"], "total")
        self.assertEqual(row["side"], "Over")
        self.assertEqual(row["opening_selected_line"], 40.5)
        self.assertEqual(row["opening_selected_price"], -110)
        self.assertEqual(row["lean_text"], "Over, but only at 40.5 or better.")

    def test_implied_score_uses_latest_total_and_spread(self):
        score = implied_score(
            {"total": 40.5, "home_spread": -3.5}
        )

        self.assertEqual(score, (18.5, 22.0))

    def test_tldr_shows_nodata_for_missing_periods(self):
        latest = {
            "game": {"total": 40.5, "home_spread": -3.5},
            "first_half": {"total": None, "home_spread": None},
            "first_quarter": {"total": None, "home_spread": None},
        }

        text = implied_score_tldr(
            latest, "Miami Dolphins", "Las Vegas Raiders"
        )

        self.assertIn("Q1: 🐬 nodata · ☠️ nodata", text)
        self.assertIn("Final: 🐬 18.5 · ☠️ 22", text)

    def test_suggestion_row_captures_when_and_who(self):
        row = build_suggestion_row(
            submitted_at=NOW,
            user_id=123,
            username="guesser",
            first_name="NFL",
            last_name="Fan",
            message_id=456,
            suggestion="Add confidence levels.",
        )

        self.assertEqual(list(row), SUGGESTION_HEADERS)
        self.assertEqual(row["submitted_at_utc"], NOW.isoformat())
        self.assertEqual(row["telegram_user_id"], 123)
        self.assertEqual(row["telegram_username"], "guesser")
        self.assertEqual(row["telegram_first_name"], "NFL")
        self.assertEqual(row["telegram_last_name"], "Fan")
        self.assertEqual(row["telegram_message_id"], 456)
        self.assertEqual(row["suggestion"], "Add confidence levels.")


class CallbackEditTest(unittest.IsolatedAsyncioTestCase):
    async def test_identical_edit_is_still_acknowledged(self):
        event = AsyncMock()
        event.edit.side_effect = MessageNotModifiedError(request=None)

        await edit_callback(event, "same", [])

        event.answer.assert_awaited_once()


if __name__ == "__main__":
    unittest.main()
