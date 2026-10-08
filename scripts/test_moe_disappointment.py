#!/usr/bin/env python3
"""Tests for the NFL Disappointment Expert."""

from __future__ import annotations

import copy
import json
import unittest
from types import SimpleNamespace

from moe import generate_opinion, load_expert
from moe_disappointment import build_disappointment_input
from moe_god import VOICE_LENSES
from nfl_lines import (
    LATEST_AWAY_COLUMN,
    LATEST_HOME_COLUMN,
    LATEST_TOTALS_COLUMN,
)


def _line_row(
    away: str,
    home: str,
    commence: str,
    *,
    home_spread: float,
    total: float,
    captured: str | None = None,
    week: int = 1,
) -> dict:
    return {
        "event_id": f"odds-{away}-{home}-{week}",
        "season": 2026,
        "season_type": "regular",
        "week": week,
        "commence_time_utc": commence,
        "away_team": away,
        "home_team": home,
        "latest_captured_at": captured or commence.replace("T17", "T12"),
        LATEST_AWAY_COLUMN: (
            f"{-home_spread},-110,nodata|nodata,nodata,nodata|"
            "nodata,nodata,nodata"
        ),
        LATEST_HOME_COLUMN: (
            f"{home_spread},-110,nodata|nodata,nodata,nodata|"
            "nodata,nodata,nodata"
        ),
        LATEST_TOTALS_COLUMN: (
            f"{total},-110,-110|nodata,nodata,nodata|nodata,nodata,nodata"
        ),
    }


def _final(
    event_id: str,
    week: int,
    away: str,
    home: str,
    kickoff: str,
    away_score: int,
    home_score: int,
    season: int = 2026,
) -> dict:
    return {
        "event_id": event_id,
        "season": season,
        "week": week,
        "kickoff_utc": kickoff,
        "away_team": away,
        "home_team": home,
        "away_score": away_score,
        "home_score": home_score,
    }


def _kick(day: int) -> str:
    return f"2026-09-{day:02d}T17:00:00+00:00"


def _board() -> tuple[dict, list[dict], list[dict]]:
    """Ravens (away) at Bills (home), week 3, after two weeks each."""
    games = [
        _line_row("Ravens", "Bills", _kick(13), home_spread=-3, total=44),
        _line_row(
            "Jets", "Ravens", _kick(20), home_spread=2.5, total=41, week=2
        ),
        _line_row(
            "Bills", "Dolphins", _kick(20), home_spread=-6.5, total=47.5,
            week=2,
        ),
        _line_row(
            "Ravens", "Bills", _kick(27), home_spread=-1, total=45, week=3
        ),
    ]
    finals = [
        _final("e1", 1, "Ravens", "Bills", _kick(13), 17, 27),
        _final("e2", 2, "Jets", "Ravens", _kick(20), 30, 20),
        _final("e3", 2, "Bills", "Dolphins", _kick(20), 31, 10),
    ]
    return games[3], games, finals


class DisappointmentInputTests(unittest.TestCase):
    def test_measures_against_the_closing_line(self) -> None:
        target, games, finals = _board()
        payload = build_disappointment_input(target, games, finals)

        ravens = payload["teams"]["away_team"]
        week_one = ravens["game_log"]["week_1"]
        self.assertEqual(week_one["venue"], "away")
        self.assertEqual(week_one["closing_spread"], 3)
        self.assertEqual(week_one["implied_team_total"], 20.5)
        self.assertEqual(week_one["implied_opponent_total"], 23.5)
        self.assertEqual(week_one["ats_margin"], -7.0)
        self.assertEqual(week_one["ats_result"], "non_cover")
        self.assertEqual(week_one["offense_surplus"], -3.5)
        self.assertEqual(week_one["defense_surplus"], -3.5)
        self.assertEqual(week_one["total_result"], "push")
        week_two = ravens["game_log"]["week_2"]
        self.assertEqual(week_two["venue"], "home")
        self.assertEqual(week_two["closing_spread"], 2.5)
        self.assertEqual(week_two["offense_surplus"], 0.75)
        self.assertEqual(week_two["defense_surplus"], -8.25)
        self.assertEqual(ravens["season"]["ats"], {"wins": 0, "losses": 2, "ties": 0})
        self.assertEqual(ravens["season"]["mean_ats_margin"], -7.25)
        self.assertEqual(ravens["season"]["profile"], "disappointing")

        bills = payload["teams"]["home_team"]
        self.assertEqual(bills["game_log"]["week_2"]["ats_margin"], 27.5)
        self.assertEqual(bills["season"]["profile"], "exceeding")
        self.assertTrue(bills["last_3"]["same_as_season"])

        season = payload["comparison"]["season"]
        self.assertEqual(season["ats"]["more_disappointing_team"], "Ravens")
        self.assertEqual(
            season["statement"],
            "Ravens profile disappointing; Bills profile exceeding.",
        )
        self.assertEqual(
            payload["current_market"],
            {
                "status": "available",
                "captured_at": "2026-09-27T12:00:00+00:00",
                "home_spread": -1,
                "away_spread": 1,
                "total": 45,
                "implied_away_total": 22.0,
                "implied_home_total": 23.0,
            },
        )

    def test_only_prior_games_with_a_pregame_line_count(self) -> None:
        target, games, finals = _board()
        # A line captured at kickoff is not a closing line.
        games[2]["latest_captured_at"] = _kick(20)
        finals += [
            # Another season, and the target game itself: never counted.
            _final("old", 3, "Ravens", "Bills", "2025-09-27T17:00:00+00:00",
                   3, 40, season=2025),
            _final("self", 3, "Ravens", "Bills", _kick(27), 10, 13),
        ]
        payload = build_disappointment_input(target, games, finals)

        self.assertEqual(
            list(payload["teams"]["away_team"]["game_log"]),
            ["week_1", "week_2"],
        )
        self.assertEqual(
            list(payload["teams"]["home_team"]["game_log"]), ["week_1"]
        )
        self.assertEqual(
            payload["data_limits"]["home_games_without_closing_line"], 1
        )

    def test_last_three_window(self) -> None:
        games, finals = [], []
        # Raiders covered early, then fell short three weeks running.
        results = [(30, 10), (28, 14), (13, 30), (10, 27), (9, 24)]
        kickoffs = ["09-06", "09-13", "09-20", "09-27", "10-04"]
        for week, (scored, allowed) in enumerate(results, start=1):
            kickoff = f"2026-{kickoffs[week - 1]}T17:00:00+00:00"
            games.append(
                _line_row(
                    f"Opp{week}", "Raiders", kickoff,
                    home_spread=-3, total=44, week=week,
                )
            )
            finals.append(
                _final(f"r{week}", week, f"Opp{week}", "Raiders", kickoff,
                       allowed, scored)
            )
        target = _line_row(
            "Raiders", "Chiefs", "2026-10-18T17:00:00+00:00",
            home_spread=-7, total=46, week=6,
        )
        payload = build_disappointment_input(target, [*games, target], finals)

        raiders = payload["teams"]["away_team"]
        self.assertEqual(raiders["season"]["games"], 5)
        self.assertFalse(raiders["last_3"]["same_as_season"])
        self.assertEqual(raiders["last_3"]["weeks"], [3, 4, 5])
        self.assertEqual(raiders["last_3"]["profile"], "disappointing")
        self.assertEqual(raiders["season"]["ats"], {"wins": 2, "losses": 3, "ties": 0})
        chiefs = payload["teams"]["home_team"]
        self.assertEqual(chiefs["season"]["profile"], "no_games")
        self.assertEqual(
            payload["comparison"]["season"]["ats"]["more_disappointing_team"],
            "unavailable",
        )

    def test_missing_current_line_is_explicit(self) -> None:
        target, games, finals = _board()
        target = {
            **target,
            LATEST_HOME_COLUMN: "",
            LATEST_AWAY_COLUMN: "nodata,nodata,nodata",
        }
        payload = build_disappointment_input(target, games, finals)
        self.assertEqual(payload["current_market"], {"status": "unavailable"})


def _valid_output(payload: dict) -> dict:
    season = payload["comparison"]["season"]
    return {
        "perspective": "regression",
        "predicted_winner": "Ravens",
        "predicted_away_score": 24,
        "predicted_home_score": 21,
        "home_win_probability": 0.45,
        "expected_home_margin": -3.0,
        "confidence_stars": 2,
        "thesis": {
            "claim": (
                f"{season['statement']} The regression reading backs the "
                "disappointing Ravens against a home spread of -1."
            ),
            "evidence_paths": ["comparison.season", "current_market"],
        },
        "supporting_factors": [
            {
                "claim": (
                    "Regression: the Ravens are 0-2 ATS with a mean ATS "
                    "margin of -7.25, a deviation the closing line already "
                    "absorbs."
                ),
                "evidence_paths": [
                    "teams.away_team.season.ats",
                    "teams.away_team.season.mean_ats_margin",
                ],
            },
            {
                "claim": "The Ravens lost 27-17 in week 1.",
                "evidence_paths": ["teams.away_team.game_log.week_1"],
            },
        ],
        "counterarguments": [
            {
                "claim": (
                    "Momentum: the Bills are 2-0 ATS with a mean defense "
                    "surplus of 10.25."
                ),
                "evidence_paths": [
                    "teams.home_team.season.ats",
                    "teams.home_team.season.mean_defense_surplus",
                ],
            }
        ],
        "no_signal_factors": [
            {
                "claim": "Injuries and prior seasons are prohibited inputs.",
                "evidence_paths": ["data_limits"],
            }
        ],
        "discarded_considerations": ["Roster news is prohibited."],
    }


class MemoryStore:
    def __init__(self) -> None:
        self.rows: list[dict] = []

    def append(self, row: dict) -> None:
        self.rows.append(dict(row))

    def list(self, event_id: str | None = None) -> list[dict]:
        return list(self.rows)


class DisappointmentGenerationTests(unittest.IsolatedAsyncioTestCase):
    async def _generate(
        self, output: dict, store: MemoryStore | None = None
    ) -> dict:
        target, games, finals = _board()

        async def create_fn(**_kwargs):
            return SimpleNamespace(
                content=[SimpleNamespace(text=json.dumps(output))]
            )

        return await generate_opinion(
            expert_id="disappointment",
            game=target,
            games=games,
            history=[],
            current_season_results=finals,
            store=store if store is not None else MemoryStore(),
            create_fn=create_fn,
        )

    async def _invalid(self, output: dict) -> str:
        """The persisted audit row's error; generation re-raises it."""
        store = MemoryStore()
        with self.assertRaises(ValueError):
            await self._generate(output, store)
        (row,) = store.rows
        self.assertEqual(row["generation_status"], "invalid")
        return row["generation_error"]

    def _payload(self) -> dict:
        target, games, finals = _board()
        return build_disappointment_input(target, games, finals)

    async def test_valid_opinion_is_approved(self) -> None:
        row = await self._generate(_valid_output(self._payload()))

        self.assertEqual(row["generation_status"], "valid", row["generation_error"])
        self.assertEqual(row["review_status"], "approved")
        self.assertEqual(row["expert_id"], "disappointment")
        self.assertTrue(
            row["full_opinion"].startswith(
                "Perspective: Regression (over momentum)."
            )
        )

    async def test_rejected_perspective_must_be_argued(self) -> None:
        output = _valid_output(self._payload())
        output["counterarguments"][0]["claim"] = (
            "The Bills are 2-0 ATS with a mean defense surplus of 10.25."
        )
        error = await self._invalid(output)
        self.assertIn("rejected perspective: momentum", error)

    async def test_perspective_must_be_one_of_two(self) -> None:
        output = _valid_output(self._payload())
        output["perspective"] = "contrarian"
        error = await self._invalid(output)
        self.assertIn("momentum or regression", error)

    async def test_comparison_statement_is_required(self) -> None:
        output = _valid_output(self._payload())
        output["thesis"]["claim"] = (
            "The regression reading backs the disappointing Ravens against a "
            "home spread of -1."
        )
        error = await self._invalid(output)
        self.assertIn("comparison.season", error)

    async def test_both_teams_season_blocks_are_required(self) -> None:
        output = _valid_output(self._payload())
        output["counterarguments"][0]["claim"] = (
            "Momentum: the Ravens scored 17 in week 1."
        )
        output["counterarguments"][0]["evidence_paths"] = [
            "teams.away_team.game_log.week_1"
        ]
        error = await self._invalid(output)
        self.assertIn("teams.home_team.season", error)

    async def test_confidence_is_capped_by_sample(self) -> None:
        output = copy.deepcopy(_valid_output(self._payload()))
        output["confidence_stars"] = 3
        error = await self._invalid(output)
        self.assertIn("cannot exceed 2 stars", error)

    async def test_unsigned_shortfall_is_rejected(self) -> None:
        output = _valid_output(self._payload())
        output["supporting_factors"][0]["claim"] = (
            "Regression: the Ravens missed the spread by 7.25 on average."
        )
        error = await self._invalid(output)
        self.assertIn("7.25", error)


class DisappointmentRegistryTests(unittest.TestCase):
    def test_expert_configuration(self) -> None:
        expert = load_expert("disappointment")

        self.assertEqual(expert["version"], 1)
        self.assertEqual(expert["prompt_version"], 1)
        self.assertEqual(expert["input_profile"], "disappointment")
        self.assertEqual(expert["output_schema_version"], 3)
        self.assertEqual(expert["default_model"], "claude-opus-5-5")
        self.assertTrue(expert["committee_optional"])
        self.assertEqual(expert["markets"], ["side", "total"])
        self.assertIn("Disappointment Expert v1", expert["prompt_text"])
        self.assertIn("regression", VOICE_LENSES["disappointment"])


if __name__ == "__main__":
    unittest.main()
