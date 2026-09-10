#!/usr/bin/env python3
"""Tests for the daily grading run's digest (scripts/moe_grade.py).

Unix only, like the rest of the God Expert suite: importing the script pulls
in ``moe`` (fcntl). Run on the VPS scratch clone via scripts/godbuild_test.sh.

The digest is phone-width prose (the aligned-columns era wrapped mid-number
inside a mobile <pre> bubble, operator-reported 2026-09-10): per game a
score header, the closing spread/total the picks were graded against, and
one line per expert spelling out the side and line of each grade plus its
actual bet legs; then the season scoreboard.
"""

from __future__ import annotations

import unittest

from moe_god import GRADES_TAB, MEAN_OF_ARMS_ID
from nfl_lines import (
    AWAY_SNAPSHOT_COLUMN,
    HOME_SNAPSHOT_COLUMN,
    TOTALS_SNAPSHOT_COLUMN,
)
from scripts.moe_grade import (
    NOTIFY_MAX_CHARS,
    NOTIFY_MAX_GAMES,
    _record_line,
    notification_text,
)

AWAY = "New England Patriots"
HOME = "Seattle Seahawks"
EVENT = "401"
KICKOFF = "2026-09-10T00:20:00+00:00"
NODATA = "nodata,nodata,nodata"


def _record(
    resolved: int,
    brier: float | None,
    *,
    legs: dict | None = None,
    clv_mean: float | None = None,
    clv_legs: int = 0,
) -> dict:
    return {
        "resolved": resolved,
        "brier": brier,
        "ats": {"w": 1, "l": 0, "p": 0},
        "ou": {"w": 0, "l": 1, "p": 0},
        "legs": legs or {"w": 0, "l": 0, "p": 0},
        "clv_points_mean": clv_mean,
        "clv_legs": clv_legs,
    }


def _row(
    opinion_id: str,
    expert_id: str,
    *,
    away: str = AWAY,
    home: str = HOME,
    final: str = "10-13",
    event_id: str = EVENT,
    week: int | str = 1,
    brier: float | str = 0.1296,
    ats: str = "P",
    ou: str = "W",
    side: tuple | None = None,   # (selection, line, result, clv)
    total: tuple | None = None,  # (selection, line, result, clv)
) -> dict:
    side = side or ("", "", "", "")
    total = total or ("", "", "", "")
    return {
        "opinion_id": opinion_id,
        "expert_id": expert_id,
        "event_id": event_id,
        "week": week,
        "away_team": away,
        "home_team": home,
        "final": final,
        "home_win_probability": 0.6,
        "brier": brier,
        "ats_at_close": ats,
        "ou_at_close": ou,
        "side_selection": side[0],
        "side_line": side[1],
        "side_result": side[2],
        "side_clv_points": side[3],
        "total_selection": total[0],
        "total_line": total[1],
        "total_result": total[2],
        "total_clv_points": total[3],
    }


def _opinion(
    opinion_id: str,
    expert_id: str,
    *,
    event_id: str = EVENT,
    winner: str = HOME,
    away_score: int = 17,
    home_score: int = 24,
    generated_at: str = "2026-09-08T00:00:00+00:00",
    side_pick_json: str = "",
    total_pick_json: str = "",
) -> dict:
    return {
        "opinion_id": opinion_id,
        "expert_id": expert_id,
        "event_id": event_id,
        "predicted_winner": winner,
        "predicted_away_score": away_score,
        "predicted_home_score": home_score,
        "generated_at_utc": generated_at,
        "commence_time_utc": KICKOFF,
        "side_pick_json": side_pick_json,
        "total_pick_json": total_pick_json,
    }


def _snapshot(captured_at: str, *, away: str, home: str, totals: str) -> dict:
    return {
        "captured_at": captured_at,
        "event_id": EVENT,
        "commence_time_utc": KICKOFF,
        "away_team": AWAY,
        "home_team": HOME,
        "bookmaker": "BetOnline.ag",
        AWAY_SNAPSHOT_COLUMN: away,
        HOME_SNAPSHOT_COLUMN: home,
        TOTALS_SNAPSHOT_COLUMN: totals,
    }


# Closing: Seahawks -3 · total 44.5.
SNAPSHOTS = [
    _snapshot(
        "2026-09-09T23:50:00+00:00",
        away=f"3,-112,150|{NODATA}|{NODATA}",
        home=f"-3,-108,-171|{NODATA}|{NODATA}",
        totals=f"44.5,-110,-110|{NODATA}|{NODATA}",
    ),
]


class GameBlockTests(unittest.TestCase):
    def setUp(self) -> None:
        self.scoreboard = {
            "resolved_games": 1,
            "graded_opinions": 4,
            "by_expert": {
                "schedule": _record(1, 0.1296),
                "god_rules": _record(
                    2,
                    0.1530,
                    legs={"w": 0, "l": 0, "p": 1},
                    clv_mean=0.0,
                    clv_legs=1,
                ),
            },
        }
        self.opinions = [
            _opinion("s1", "schedule"),
            _opinion(
                "a1",
                "ak",
                away_score=20,
                home_score=24,
                generated_at="2026-09-09T23:00:00+00:00",
                side_pick_json='{"selection": "Seattle Seahawks", "line": -3.0}',
                total_pick_json='{"selection": "PASS"}',
            ),
            _opinion(
                "g1",
                "god_rules",
                away_score=21,
                home_score=24,
                generated_at="2026-09-09T00:00:00+00:00",
                side_pick_json='{"selection": "PASS"}',
                total_pick_json='{"selection": "PASS"}',
            ),
            _opinion(
                "g2",
                "god_rules",
                away_score=20,
                home_score=23,
                generated_at="2026-09-09T22:00:00+00:00",
                side_pick_json='{"selection": "PASS"}',
                total_pick_json='{"selection": "PASS"}',
            ),
        ]
        self.new_rows = [
            _row("s1", "schedule"),
            _row("a1", "ak", brier=0.1444, side=(HOME, -3.0, "P", 0.0)),
            _row("g1", "god_rules", brier=0.1545, ou="L"),
            _row("g2", "god_rules", brier=0.1545, ou="W"),
            _row("mean:g2:j", MEAN_OF_ARMS_ID, brier=0.1534),
        ]

    def _text(self, **overrides) -> str:
        kwargs = dict(
            season=2026,
            scoreboard=self.scoreboard,
            new_rows=self.new_rows,
            finals=[],
            opinions=self.opinions,
            snapshots=SNAPSHOTS,
        )
        kwargs.update(overrides)
        return notification_text(**kwargs)

    def test_header_carries_the_ledger_delta(self) -> None:
        lines = self._text().split("\n")
        self.assertEqual(lines[0], "pickbot: MOE grades · season 2026")
        self.assertEqual(lines[1], "1 resolved game · 4 graded opinions")
        self.assertEqual(
            lines[2], f"+4 opinion rows, +1 mean-of-arms → {GRADES_TAB}"
        )

    def test_game_block_shows_final_close_and_graded_sides(self) -> None:
        text = self._text()
        self.assertIn("🏈 Patriots 10 @ Seahawks 13 · Week 1", text)
        # The exact lines the picks were graded against.
        self.assertIn("close: Seahawks -3 · total 44.5", text)
        # A voice: projected score, ATS side at the closing spread, O/U lean
        # at the closing total, per-game Brier.
        self.assertIn("schedule 17-24: Seahawks -3 ♻️ · U 44.5 ✅ · B 0.1296", text)

    def test_bet_legs_render_with_line_result_and_clv(self) -> None:
        text = self._text()
        self.assertIn("ak 20-24: Seahawks -3 ♻️ · U 44.5 ✅ · B 0.1444", text)
        self.assertIn("↳ bet: Seahawks -3 ♻️ (clv +0.0)", text)
        # Declared picks that graded no leg are an explicit PASS.
        self.assertIn("↳ bet: PASS", text)

    def test_only_the_latest_row_per_expert_shows(self) -> None:
        text = self._text()
        games_section = text.split("season so far:")[0]
        # g2 (22:00) outranks g1 (00:00): its projection and O/U lean render.
        self.assertIn("god_rules 20-23:", games_section)
        self.assertNotIn("god_rules 21-24:", games_section)
        self.assertEqual(games_section.count("god_rules"), 1)

    def test_mean_of_arms_renders_side_less_and_last(self) -> None:
        text = self._text()
        games_section = text.split("season so far:")[0]
        self.assertIn("mean_of_arms: ats ♻️ · o/u ✅ · B 0.1534", games_section)
        self.assertLess(
            games_section.index("schedule 17-24"),
            games_section.index("mean_of_arms:"),
        )

    def test_without_snapshots_or_opinions_the_block_degrades(self) -> None:
        text = self._text(opinions=[], snapshots=[])
        self.assertIn("close: unavailable", text)
        # No sides or lines to spell out, but the results still show.
        self.assertIn("schedule: ats ♻️ · o/u ✅ · B 0.1296", text)

    def test_season_lines_are_compact_and_skip_empty_sections(self) -> None:
        text = self._text()
        self.assertIn("season so far:", text)
        self.assertIn("schedule · n1 · B 0.1296 · ats 1-0-0 · ou 0-1-0", text)
        # No legs and no clv → neither section appears on the line.
        self.assertNotIn("schedule · n1 · B 0.1296 · ats 1-0-0 · ou 0-1-0 · legs", text)
        self.assertIn(
            "god_rules · n2 · B 0.1530 · ats 1-0-0 · ou 0-1-0 · legs 0-0-1 "
            "· clv +0.00/1",
            text,
        )

    def test_record_line_omits_brier_when_unresolved(self) -> None:
        self.assertEqual(
            _record_line("god_judge", _record(0, None)),
            "god_judge · n0 · ats 1-0-0 · ou 0-1-0",
        )


class HtmlDigestTests(GameBlockTests):
    """html=True: the same content, visually chunked for the Scores topic."""

    def test_html_bolds_headers_and_quotes_the_game_block(self) -> None:
        text = self._text(html=True)
        self.assertTrue(text.startswith("<b>pickbot: MOE grades · season 2026</b>"))
        self.assertIn("<b>🏈 Patriots 10 @ Seahawks 13 · Week 1</b>", text)
        # The close line stays outside the quote; the expert lines live in
        # ONE blockquote per game, each expert bolded through its colon.
        self.assertIn(
            "close: Seahawks -3 · total 44.5\n<blockquote><b>ak 20-24:</b> "
            "Seahawks -3 ♻️ · U 44.5 ✅ · B 0.1444",
            text,
        )
        self.assertIn("↳ bet: Seahawks -3 ♻️ (clv +0.0)", text)
        self.assertIn("<b>mean_of_arms:</b> ats ♻️ · o/u ✅ · B 0.1534</blockquote>", text)

    def test_html_season_board_is_an_expandable_quote(self) -> None:
        text = self._text(html=True)
        self.assertIn("<b>season so far</b>", text)
        self.assertIn(
            "<blockquote expandable><b>god_rules</b> · n2 · B 0.1530", text
        )
        self.assertTrue(text.endswith("</blockquote>"))

    def test_html_degraded_games_keep_only_the_bold_header(self) -> None:
        rows = []
        for game in range(8):
            for i in range(25):
                rows.append(
                    _row(
                        f"o{game}-{i}",
                        f"expert_{i:02d}",
                        away=f"Away Team{game}",
                        home=f"Home Team{game}",
                        final="10-13",
                        event_id=f"ev{game}",
                    )
                )
        board = {
            "resolved_games": 8,
            "graded_opinions": len(rows),
            "by_expert": {f"expert_{i:02d}": _record(1, 0.25) for i in range(25)},
        }
        text = notification_text(
            season=2026, scoreboard=board, new_rows=rows, html=True
        )
        self.assertLessEqual(len(text), NOTIFY_MAX_CHARS)
        self.assertIn("<b>🏈 Team7 10 @ Team7 13 · Week 1</b>", text)
        # Balanced tags — a degraded game contributes no quote at all.
        self.assertEqual(text.count("<blockquote"), text.count("</blockquote>"))

    def test_html_last_resort_is_the_tag_free_plain_render(self) -> None:
        board = {
            "resolved_games": 1,
            "graded_opinions": 120,
            "by_expert": {f"expert_{i:03d}": _record(1, 0.25) for i in range(120)},
        }
        text = notification_text(
            season=2026,
            scoreboard=board,
            new_rows=[_row("o1", "schedule")],
            html=True,
        )
        self.assertEqual(len(text), NOTIFY_MAX_CHARS)
        self.assertTrue(text.endswith("…"))
        self.assertNotIn("<b>", text)
        self.assertNotIn("<blockquote", text)


class BudgetTests(unittest.TestCase):
    def _board(self, experts: int) -> dict:
        return {
            "resolved_games": 1,
            "graded_opinions": experts,
            "by_expert": {
                f"expert_{i:02d}": _record(1, 0.25) for i in range(experts)
            },
        }

    def test_games_beyond_the_cap_are_counted(self) -> None:
        rows = [
            _row(
                f"o{i}",
                "schedule",
                away=f"Away Team{i}",
                home=f"Home Team{i}",
                final=f"{i}-{i + 1}",
                event_id=f"ev{i}",
                week="",
            )
            for i in range(NOTIFY_MAX_GAMES + 7)
        ]
        text = notification_text(
            season=2026,
            scoreboard=self._board(1),
            new_rows=rows,
        )
        self.assertIn(f"Team{NOTIFY_MAX_GAMES - 1} ", text)
        self.assertNotIn(f"Team{NOTIFY_MAX_GAMES} ", text)
        self.assertIn("+7 more games", text)
        self.assertLessEqual(len(text), NOTIFY_MAX_CHARS)

    def test_long_digest_degrades_trailing_games_to_headers(self) -> None:
        # 8 games × 25 experts of full detail is far past the cap; the head
        # of the message keeps its close line, the tail keeps only the score
        # header, and no game disappears.
        rows = []
        for game in range(8):
            for i in range(25):
                rows.append(
                    _row(
                        f"o{game}-{i}",
                        f"expert_{i:02d}",
                        away=f"Away Team{game}",
                        home=f"Home Team{game}",
                        final="10-13",
                        event_id=f"ev{game}",
                    )
                )
        text = notification_text(
            season=2026,
            scoreboard=self._board(25),
            new_rows=rows,
        )
        self.assertLessEqual(len(text), NOTIFY_MAX_CHARS)
        self.assertIn("🏈 Team0 10 @ Team0 13", text)
        self.assertIn("🏈 Team7 10 @ Team7 13", text)
        self.assertLess(text.count("close:"), 8)
        self.assertFalse(text.endswith("…"))

    def test_hard_truncation_is_the_last_resort(self) -> None:
        # A scoreboard alone past the cap cannot be fixed by degrading game
        # blocks — the tail is cut under Telegram's limit.
        text = notification_text(
            season=2026,
            scoreboard=self._board(120),
            new_rows=[_row("o1", "schedule")],
        )
        self.assertEqual(len(text), NOTIFY_MAX_CHARS)
        self.assertTrue(text.endswith("…"))
        self.assertTrue(text.startswith("pickbot: MOE grades · season 2026"))


if __name__ == "__main__":
    unittest.main()
