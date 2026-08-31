#!/usr/bin/env python3
"""Unit tests for the native NFL game browser."""

from __future__ import annotations

import unittest
from unittest.mock import AsyncMock, patch
from datetime import datetime, timedelta, timezone

from gspread.exceptions import WorksheetNotFound
from telethon.errors import MessageNotModifiedError

import intake_bot
from intake_bot import (
    CELEBRITY_HEADERS,
    TEAM_ABBREVIATIONS as DEFAULT_WIN_TEAMS,
    SUGGESTION_HEADERS,
    append_celebrity_picks,
    build_celebrity_rows,
    build_lean_row,
    build_suggestion_row,
    build_win_prediction_row,
    celebrity_screen,
    celebrity_user_id,
    command_keyboard,
    edit_callback,
    game_browser,
    game_detail,
    implied_score,
    implied_score_tldr,
    load_celebrity_roster,
    market_buttons,
    market_side_summary,
    page_games,
    parse_celebrity_names,
    period_market_summary,
    select_games,
    selected_market_context,
    side_buttons,
    snapshot_lean_submission,
    team_emoji,
    win_prediction_browser,
    win_prediction_confirmation,
    win_prediction_team_detail,
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


def _win_totals() -> list[dict]:
    current = [
        {
            "season": 2026,
            "team": team,
            "team_abbreviation": abbreviation,
            "bookmaker": "BetOnline",
            "win_total": 10.5 if abbreviation == "SEA" else 8.5,
            "over_price": "",
            "under_price": "",
            "captured_at_utc": "2026-08-10T03:34:34+00:00",
            "captured_at_et": "2026-08-09T23:34:34-04:00",
            "source": "user_paste",
        }
        for team, abbreviation in {
            "Arizona Cardinals": "ARI",
            "Los Angeles Rams": "LAR",
            "San Francisco 49ers": "SF",
            "Seattle Seahawks": "SEA",
        }.items()
    ]
    other_teams = [
        team
        for team, abbreviation in DEFAULT_WIN_TEAMS.items()
        if abbreviation not in {"ARI", "LAR", "SF", "SEA"}
    ]
    current.extend(
        {
            "season": 2026,
            "team": team,
            "team_abbreviation": DEFAULT_WIN_TEAMS[team],
            "bookmaker": "BetOnline",
            "win_total": 8.5,
            "over_price": "",
            "under_price": "",
            "captured_at_utc": "2026-08-10T03:34:34+00:00",
            "captured_at_et": "2026-08-09T23:34:34-04:00",
            "source": "user_paste",
        }
        for team in other_teams
    )
    return current


def _history() -> list[dict]:
    division_teams = {
        2023: [
            ("San Francisco 49ers", "SF", 1, 12, 5, 6),
            ("Los Angeles Rams", "LAR", 2, 10, 7, 10),
            ("Seattle Seahawks", "SEA", 3, 9, 8, 10),
            ("Arizona Cardinals", "ARI", 4, 4, 13, 8),
        ],
        2024: [
            ("Los Angeles Rams", "LAR", 1, 10, 7, 12),
            ("Seattle Seahawks", "SEA", 2, 10, 7, 14),
            ("San Francisco 49ers", "SF", 3, 6, 11, 12),
            ("Arizona Cardinals", "ARI", 4, 8, 9, 3),
        ],
        2025: [
            ("Seattle Seahawks", "SEA", 1, 14, 3, None),
            ("Los Angeles Rams", "LAR", 2, 12, 5, None),
            ("San Francisco 49ers", "SF", 3, 12, 5, None),
            ("Arizona Cardinals", "ARI", 4, 3, 14, None),
        ],
    }
    rows = []
    for season, teams in division_teams.items():
        for team, abbreviation, rank, wins, losses, next_wins in teams:
            rows.append(
                {
                    "season": season,
                    "team": team,
                    "team_abbreviation": abbreviation,
                    "conference": "National Football Conference",
                    "division": "NFC West",
                    "division_rank": rank,
                    "wins": wins,
                    "losses": losses,
                    "ties": 0,
                    "playoff_team": rank <= 2,
                    "next_season": season + 1 if next_wins is not None else "",
                    "next_season_wins": (
                        next_wins if next_wins is not None else ""
                    ),
                    "next_season_division_rank": "",
                    "next_season_playoff_team": "",
                    "win_change": "",
                }
            )
    other_wins = {
        2023: [15, 15, 13, 12, 10, 10, 7],
        2024: [12, 12, 11, 9, 8, 8, 6],
    }
    for season, wins_list in other_wins.items():
        for index, next_wins in enumerate(wins_list, start=1):
            team = f"Other Team {season}-{index}"
            rows.extend(
                [
                    {
                        "season": season,
                        "team": team,
                        "team_abbreviation": f"O{index}",
                        "conference": "AFC",
                        "division": f"Other Division {index}",
                        "division_rank": 1,
                        "wins": 11,
                        "losses": 6,
                        "ties": 0,
                        "playoff_team": True,
                        "next_season": season + 1,
                        "next_season_wins": next_wins,
                        "next_season_division_rank": "",
                        "next_season_playoff_team": "",
                        "win_change": next_wins - 11,
                    },
                    {
                        "season": season + 1,
                        "team": team,
                        "team_abbreviation": f"O{index}",
                        "conference": "AFC",
                        "division": f"Other Division {index}",
                        "division_rank": 1,
                        "wins": next_wins,
                        "losses": 17 - next_wins,
                        "ties": 0,
                        "playoff_team": True,
                        "next_season": "",
                        "next_season_wins": "",
                        "next_season_division_rank": "",
                        "next_season_playoff_team": "",
                        "win_change": "",
                    },
                ]
            )
    return rows


class GameSelectionTest(unittest.TestCase):
    def test_command_keyboard_is_persistent(self):
        keyboard = command_keyboard()

        self.assertTrue(keyboard.persistent)
        self.assertFalse(keyboard.single_use)
        self.assertEqual(
            keyboard.rows[0].buttons[0].text, "/guess_nfl_game"
        )
        self.assertEqual(
            keyboard.rows[0].buttons[1].text, "/predict_nfl_wins"
        )
        self.assertEqual(keyboard.rows[1].buttons[0].text, "/suggest")

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

    def test_submission_snapshot_rejects_incomplete_matching_state(self):
        status, submission = snapshot_lean_submission(
            {
                "game": _game("miami", 1),
                "prompt_msg_id": 456,
            },
            reply_to_msg_id=456,
        )

        self.assertEqual(status, "invalid")
        self.assertIsNone(submission)

    def test_submission_snapshot_survives_navigation_mutation(self):
        state = {
            "game": _game("miami", 1),
            "period": "game",
            "market": "spread",
            "side": "away",
            "prompt_msg_id": 456,
        }

        status, submission = snapshot_lean_submission(
            state,
            reply_to_msg_id=456,
        )
        state.pop("period")
        state["game"]["away_team"] = "Changed Team"

        self.assertEqual(status, "ready")
        self.assertEqual(submission["period"], "game")
        self.assertEqual(
            submission["game"]["away_team"], "Miami Dolphins"
        )

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

    def test_win_browser_sorts_unmarked_teams_first(self):
        predictions = [
            {
                "revision_id": "revision-1",
                "submitted_at_utc": NOW.isoformat(),
                "telegram_user_id": 123,
                "team": "Arizona Cardinals",
                "predicted_wins": 6,
            }
        ]

        text, buttons = win_prediction_browser(
            _win_totals(), predictions, user_id=123
        )
        labels = [button.text for row in buttons for button in row]

        self.assertIn("Progress: 1/32 teams", text)
        self.assertEqual(labels[:3], ["ATL", "BAL", "BUF"])
        self.assertEqual(labels[-1], "ARI · 6")

    def test_seattle_win_detail_matches_approved_exchange(self):
        text, buttons = win_prediction_team_detail(
            _win_totals(),
            _history(),
            [],
            user_id=123,
            abbreviation="SEA",
        )

        self.assertIn("BetOnline total: 10.5 wins", text)
        self.assertNotIn("Your previous prediction", text)
        self.assertIn("1. (SEA) 14–3", text)
        self.assertIn("2. LAR 12–5", text)
        self.assertIn("SF finished 1st", text)
        self.assertIn("Their 2024 record: 6–11.", text)
        self.assertIn("Average 2024 wins: 11.71", text)
        self.assertIn("(15, 15, 13, 12, 10, 10, 7)", text)
        self.assertIn("LAR finished 1st", text)
        self.assertIn("Their 2025 record: 12–5.", text)
        self.assertIn("Average 2025 wins: 9.43", text)
        self.assertIn("(12, 12, 11, 9, 8, 8, 6)", text)
        win_buttons = [
            int(button.text)
            for row in buttons[:-1]
            for button in row
        ]
        self.assertEqual(win_buttons, list(range(18)))

    def test_previous_prediction_is_only_shown_when_present(self):
        text, _ = win_prediction_team_detail(
            _win_totals(),
            _history(),
            [
                {
                    "revision_id": "revision-1",
                    "submitted_at_utc": NOW.isoformat(),
                    "telegram_user_id": 123,
                    "team": "Seattle Seahawks",
                    "predicted_wins": 11,
                }
            ],
            user_id=123,
            abbreviation="SEA",
        )

        self.assertIn("Your previous prediction: 11 wins", text)

    def test_win_confirmation_and_row_capture_market_context(self):
        text, buttons = win_prediction_confirmation(
            _win_totals(),
            abbreviation="SEA",
            predicted_wins=11,
        )
        market = next(
            row for row in _win_totals() if row["team_abbreviation"] == "SEA"
        )
        prior = next(
            row
            for row in _history()
            if row["season"] == 2025 and row["team_abbreviation"] == "SEA"
        )
        row = build_win_prediction_row(
            submitted_at=NOW,
            user_id=123,
            username="guesser",
            first_name="NFL",
            last_name="Fan",
            team="Seattle Seahawks",
            predicted_wins=11,
            market=market,
            prior=prior,
        )

        self.assertIn("Difference: +0.5 wins", text)
        self.assertEqual(buttons[0][0].data, b"winsave:SEA:11")
        self.assertEqual(row["telegram_display_name"], "NFL Fan")
        self.assertEqual(row["market_win_total"], 10.5)
        self.assertEqual(row["prior_division_rank"], 1)


class _FakeWorksheet:
    def __init__(self, values):
        self._values = [list(row) for row in values]
        self.appended: list[list] = []

    def row_values(self, index):
        return list(self._values[index - 1]) if len(self._values) >= index else []

    def get_all_values(self):
        return [list(row) for row in self._values]

    def update(self, data):
        self._values = [list(row) for row in data] + self._values[1:]

    def append_rows(self, rows, value_input_option="RAW"):
        for row in rows:
            self.appended.append(list(row))
            self._values.append(list(row))


class _FakeSpreadsheet:
    def __init__(self, worksheet):
        self._worksheet = worksheet

    def worksheet(self, title):
        if self._worksheet is None:
            raise WorksheetNotFound("missing")
        return self._worksheet

    def add_worksheet(self, title, rows, cols):
        self._worksheet = _FakeWorksheet([[""] * cols])
        return self._worksheet


class _FakeClient:
    def __init__(self, spreadsheet):
        self._spreadsheet = spreadsheet

    def open_by_key(self, key):
        return self._spreadsheet


def _patch_gspread(worksheet):
    return (
        patch.dict(
            intake_bot.os.environ,
            {"GOOGLE_CREDENTIALS": "creds", "NFL_INTAKE_SHEET_ID": "sheet"},
        ),
        patch.object(
            intake_bot,
            "get_gspread_client",
            return_value=_FakeClient(_FakeSpreadsheet(worksheet)),
        ),
    )


class CelebrityPickTest(unittest.TestCase):
    def test_parse_names_dedupes_case_insensitively_and_keeps_first_form(self):
        names = parse_celebrity_names("LeBron, Drake\n lebron ,  ")

        self.assertEqual(names, ["LeBron", "Drake"])

    def test_parse_names_caps_count_and_length(self):
        names = parse_celebrity_names(", ".join(f"Person{i}" for i in range(20)))
        self.assertEqual(len(names), intake_bot.MAX_CELEBRITY_NAMES)

        long = parse_celebrity_names("x" * 200)[0]
        self.assertEqual(len(long), intake_bot.MAX_CELEBRITY_NAME_LEN)

    def test_build_rows_one_per_name_carrying_submission_context(self):
        submission = {header: "" for header in CELEBRITY_HEADERS[:-1]}
        submission["submission_id"] = "telegram:1:2"
        submission["away_team"] = "Miami Dolphins"

        rows = build_celebrity_rows(submission=submission, names=["LeBron", "Drake"])

        self.assertEqual([r["celebrity_name"] for r in rows], ["LeBron", "Drake"])
        self.assertTrue(all(list(r) == CELEBRITY_HEADERS for r in rows))
        self.assertTrue(all(r["away_team"] == "Miami Dolphins" for r in rows))

    def test_screen_save_label_and_selected_marks(self):
        text, buttons = celebrity_screen(
            saved_summary="✅ Guess saved.",
            roster=["LeBron", "Drake"],
            selected=["Drake"],
        )

        self.assertIn("Whose read does this reflect?", text)
        toggles = [b for row in buttons for b in row if b.data.startswith(b"celeb:tog")]
        self.assertEqual(toggles[0].text, "LeBron")
        self.assertEqual(toggles[1].text, "✅ Drake")
        self.assertEqual(buttons[-1][0].text, "✅ Save (1 selected)")
        self.assertEqual(buttons[-1][0].data, b"celeb:save")

    def test_celebrity_user_id_is_stable_negative_and_distinct(self):
        # Same person (any case/spacing) -> one id; negative so it can never
        # collide with a real positive Telegram user id; different people differ.
        a = celebrity_user_id("LeBron James")
        self.assertEqual(a, celebrity_user_id("  lebron   JAMES "))
        self.assertLess(a, 0)
        self.assertNotEqual(a, celebrity_user_id("Taylor Swift"))

    def test_screen_save_label_when_none_selected(self):
        _, buttons = celebrity_screen(
            saved_summary="ok", roster=["LeBron"], selected=[]
        )

        self.assertEqual(buttons[-1][0].text, "Save — no celebrity info")

    def test_roster_orders_by_frequency_then_recency(self):
        worksheet = _FakeWorksheet(
            [
                CELEBRITY_HEADERS,
                *[["telegram:1:1"] + [""] * 13 + ["Drake"] for _ in range(1)],
                *[["telegram:1:2"] + [""] * 13 + ["LeBron"] for _ in range(3)],
                ["telegram:1:3"] + [""] * 13 + ["Adele"],
            ]
        )
        env_patch, client_patch = _patch_gspread(worksheet)
        with env_patch, client_patch:
            roster = load_celebrity_roster()

        # LeBron (3) first; Drake and Adele each once, Adele more recent.
        self.assertEqual(roster, ["LeBron", "Adele", "Drake"])

    def test_roster_empty_when_tab_missing(self):
        env_patch, client_patch = _patch_gspread(None)
        with env_patch, client_patch:
            self.assertEqual(load_celebrity_roster(), [])

    def test_append_skips_existing_submission_name_pairs(self):
        worksheet = _FakeWorksheet(
            [CELEBRITY_HEADERS, ["telegram:1:2"] + [""] * 13 + ["LeBron"]]
        )
        rows = build_celebrity_rows(
            submission={h: "" for h in CELEBRITY_HEADERS[:-1]}
            | {"submission_id": "telegram:1:2"},
            names=["LeBron", "Drake"],
        )
        env_patch, client_patch = _patch_gspread(worksheet)
        with env_patch, client_patch:
            written = append_celebrity_picks(rows)

        self.assertEqual(written, 1)
        self.assertEqual(len(worksheet.appended), 1)
        self.assertEqual(worksheet.appended[0][-1], "Drake")


class CallbackEditTest(unittest.IsolatedAsyncioTestCase):
    async def test_identical_edit_is_still_acknowledged(self):
        event = AsyncMock()
        event.edit.side_effect = MessageNotModifiedError(request=None)

        await edit_callback(event, "same", [])

        event.answer.assert_awaited_once()


if __name__ == "__main__":
    unittest.main()
