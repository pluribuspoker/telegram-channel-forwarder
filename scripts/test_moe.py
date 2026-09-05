#!/usr/bin/env python3
"""Tests for versioned NFL mixture-of-experts opinions."""

from __future__ import annotations

import json
import tempfile
import unittest
import uuid
from datetime import datetime, timezone
from types import SimpleNamespace
from pathlib import Path
from unittest.mock import patch

from moe import (
    OPINION_HEADERS,
    build_divisional_input,
    build_schedule_input,
    generate_opinion,
    latest_opinions,
    latest_model_opinions,
    load_expert,
    opinion_detail,
    opinion_model_picker,
    opinion_output_sha256,
    opinion_summary,
    validate_opinion,
    _persist_attempt,
)


class MemoryStore:
    def __init__(self) -> None:
        self.rows: list[dict] = []

    def append(self, row: dict) -> None:
        self.rows.append(dict(row))

    def list(self, event_id: str | None = None) -> list[dict]:
        if event_id is None:
            return list(self.rows)
        return [
            row
            for row in self.rows
            if str(row.get("event_id")) == str(event_id)
        ]


class FailingStore(MemoryStore):
    def append(self, row: dict) -> None:
        raise RuntimeError("sheet unavailable")


def _game() -> dict:
    return {
        "event_id": "401772510",
        "season": 2026,
        "season_type": "regular",
        "week": 1,
        "status": "upcoming",
        "commence_time_utc": "2026-09-11T00:20:00+00:00",
        "commence_time_et": "2026-09-10T20:20:00-04:00",
        "away_team": "Dallas Cowboys",
        "home_team": "Philadelphia Eagles",
        "bookmaker": "BetOnline",
        "latest_away_market": "+7",
    }


def _history_row(
    event_id: str,
    *,
    season: int,
    week: int,
    away_team: str,
    home_team: str,
    away_score: int,
    home_score: int,
    kickoff: str,
    same_division: bool = True,
    meeting_number: int | str = 1,
) -> dict:
    return {
        "event_id": event_id,
        "season": season,
        "season_type": "regular",
        "week": week,
        "status": "final",
        "kickoff_utc": kickoff,
        "kickoff_et": kickoff,
        "away_team": away_team,
        "home_team": home_team,
        "away_score": away_score,
        "home_score": home_score,
        "home_result": "W" if home_score > away_score else "L",
        "home_margin": home_score - away_score,
        "total_points": home_score + away_score,
        "away_conference": "NFC",
        "away_division": "NFC East",
        "home_conference": "NFC",
        "home_division": "NFC East" if same_division else "NFC North",
        "same_conference": True,
        "same_division": same_division,
        "matchup_type": "division" if same_division else "conference",
        "division_meeting_number": meeting_number if same_division else "",
        "neutral_site": False,
        "overtime": False,
        "tags": "divisional_game_1" if same_division else "conference_game",
        "source": "espn",
    }


def _history() -> list[dict]:
    return [
        _history_row(
            "old-1",
            season=2024,
            week=1,
            away_team="Dallas Cowboys",
            home_team="Philadelphia Eagles",
            away_score=20,
            home_score=27,
            kickoff="2024-09-05T20:20:00-04:00",
        ),
        _history_row(
            "old-2",
            season=2025,
            week=10,
            away_team="Philadelphia Eagles",
            home_team="Dallas Cowboys",
            away_score=24,
            home_score=17,
            kickoff="2025-11-06T20:20:00-05:00",
        ),
        _history_row(
            "future",
            season=2026,
            week=1,
            away_team="Dallas Cowboys",
            home_team="Philadelphia Eagles",
            away_score=99,
            home_score=0,
            kickoff="2026-09-10T20:20:00-04:00",
        ),
    ]


class ScheduleInputTest(unittest.TestCase):
    def test_excludes_current_markets_and_current_season_results(self) -> None:
        payload = build_schedule_input(_game(), _history())
        serialized = json.dumps(payload)

        self.assertEqual(payload["input_profile"], "schedule_only")
        self.assertEqual(payload["game"]["weekday"], "Thursday")
        self.assertEqual(payload["game"]["matchup_type"], "division")
        self.assertEqual(payload["historical_data"]["seasons"], [2024, 2025])
        self.assertIn(
            "September",
            payload["historical_data"]["home_team"]["by_month"],
        )
        self.assertIn(
            "as_away",
            payload["historical_data"]["home_team"],
        )
        self.assertEqual(
            payload["historical_data"]["home_team"][
                "same_month_as_home"
            ]["games"],
            1,
        )
        self.assertEqual(
            payload["historical_data"]["away_team"][
                "same_month_as_away"
            ]["games"],
            1,
        )
        self.assertEqual(
            payload["historical_data"]["home_team"][
                "same_month_rankings"
            ],
            {
                "month": "September",
                "months_compared": 2,
                "win_rate_rank_high_to_low": 1,
                "average_margin_rank_high_to_low": 1,
                "average_points_for_rank_high_to_low": 1,
            },
        )
        self.assertNotIn("bookmaker", serialized)
        self.assertNotIn("latest_away_market", serialized)
        self.assertNotIn("99", serialized)

    def test_historical_scores_feed_margin_summaries(self) -> None:
        payload = build_schedule_input(_game(), _history())
        away = payload["historical_data"]["away_team"]["all_games"]
        home = payload["historical_data"]["home_team"]["all_games"]

        self.assertEqual(away["games"], 2)
        self.assertEqual(away["average_margin"], -7.0)
        self.assertEqual(home["average_margin"], 7.0)

    def test_missing_current_week_is_preserved_as_unknown(self) -> None:
        game = _game()
        game["week"] = ""

        payload = build_schedule_input(game, _history())

        self.assertNotIn("week", payload["game"])
        self.assertNotIn(
            "same_week_number",
            payload["historical_data"]["away_team"],
        )


class DivisionalInputTest(unittest.TestCase):
    def _division_history(self) -> list[dict]:
        return [
            _history_row(
                "2024-first",
                season=2024,
                week=3,
                away_team="Dallas Cowboys",
                home_team="Philadelphia Eagles",
                away_score=17,
                home_score=24,
                kickoff="2024-09-20T00:00:00+00:00",
                meeting_number=1,
            ),
            _history_row(
                "2024-second",
                season=2024,
                week=12,
                away_team="Philadelphia Eagles",
                home_team="Dallas Cowboys",
                away_score=20,
                home_score=27,
                kickoff="2024-11-20T00:00:00+00:00",
                meeting_number=2,
            ),
            _history_row(
                "2025-first",
                season=2025,
                week=5,
                away_team="Philadelphia Eagles",
                home_team="Dallas Cowboys",
                away_score=28,
                home_score=21,
                kickoff="2025-10-05T00:00:00+00:00",
                meeting_number=1,
            ),
            _history_row(
                "2025-second",
                season=2025,
                week=17,
                away_team="Dallas Cowboys",
                home_team="Philadelphia Eagles",
                away_score=14,
                home_score=31,
                kickoff="2025-12-28T00:00:00+00:00",
                meeting_number=2,
            ),
        ]

    def test_second_division_meeting_includes_only_prior_current_result(
        self,
    ) -> None:
        game = {
            **_game(),
            "event_id": "2026-second",
            "week": 15,
            "commence_time_utc": "2026-12-13T18:00:00+00:00",
        }
        schedule = [
            {
                "event_id": "espn-2026-first",
                "season": 2026,
                "week": 4,
                "kickoff_utc": "2026-09-27T18:00:00+00:00",
                "away_team": "Philadelphia Eagles",
                "home_team": "Dallas Cowboys",
            },
            {
                "event_id": "espn-2026-second",
                "season": 2026,
                "week": 15,
                "kickoff_utc": "2026-12-13T18:00:00+00:00",
                "away_team": "Dallas Cowboys",
                "home_team": "Philadelphia Eagles",
            },
        ]
        current_results = [
            _history_row(
                "espn-2026-first",
                season=2026,
                week=4,
                away_team="Philadelphia Eagles",
                home_team="Dallas Cowboys",
                away_score=27,
                home_score=20,
                kickoff="2026-09-27T18:00:00+00:00",
                meeting_number=1,
            ),
            _history_row(
                "unrelated",
                season=2026,
                week=5,
                away_team="Other Away",
                home_team="Other Home",
                away_score=99,
                home_score=0,
                kickoff="2026-10-04T18:00:00+00:00",
                same_division=False,
            ),
        ]

        payload = build_divisional_input(
            game,
            self._division_history(),
            schedule,
            current_results,
        )
        serialized = json.dumps(payload)

        self.assertTrue(payload["game"]["is_divisional"])
        self.assertEqual(
            payload["game"]["schedule_event_id"], "espn-2026-second"
        )
        self.assertEqual(payload["game"]["division_meeting_number"], 2)
        self.assertEqual(payload["game"]["scheduled_pair_meetings"], 2)
        self.assertEqual(
            payload["current_season_prior_meeting"]["away_score"], 27
        )
        self.assertEqual(
            payload["current_season_prior_meeting"]["winner"],
            "Philadelphia Eagles",
        )
        self.assertEqual(
            payload["current_season_prior_meeting"]["home_margin"], -7
        )
        self.assertEqual(
            payload["historical_data"]["home_team"][
                "against_current_opponent"
            ]["games"],
            4,
        )
        self.assertEqual(
            payload["historical_data"]["away_team"][
                "current_opponent_series"
            ]["complete_two_game_seasons"],
            2,
        )
        cohorts = payload["historical_data"][
            "divisional_home_side_meeting_cohorts"
        ]
        self.assertIn("not the current home team", cohorts["perspective"])
        self.assertEqual(cohorts["nfl"]["meeting_1"]["games"], 2)
        self.assertEqual(cohorts["nfl"]["meeting_1"]["wins"], 1)
        self.assertEqual(cohorts["division"]["name"], "NFC East")
        self.assertEqual(cohorts["division"]["meeting_2"]["wins"], 2)
        self.assertEqual(
            cohorts["opponent_pair"]["meeting_1"]["average_margin"], 0.0
        )
        self.assertNotIn("99", serialized)
        self.assertNotIn("Other Away", serialized)

    def test_non_divisional_game_uses_one_scheduled_meeting(self) -> None:
        history = [
            *self._division_history(),
            _history_row(
                "conference-game",
                season=2025,
                week=18,
                away_team="Minnesota Vikings",
                home_team="Philadelphia Eagles",
                away_score=21,
                home_score=24,
                kickoff="2025-10-26T18:00:00+00:00",
                same_division=False,
            ),
        ]
        game = {
            **_game(),
            "event_id": "2026-conference",
            "away_team": "Minnesota Vikings",
        }
        schedule = [
            {
                "event_id": "espn-2026-conference",
                "season": 2026,
                "week": 1,
                "kickoff_utc": game["commence_time_utc"],
                "away_team": "Minnesota Vikings",
                "home_team": "Philadelphia Eagles",
            }
        ]

        payload = build_divisional_input(game, history, schedule)

        self.assertFalse(payload["game"]["is_divisional"])
        self.assertEqual(payload["game"]["matchup_type"], "conference")
        self.assertIsNone(payload["game"]["division_meeting_number"])
        self.assertEqual(payload["game"]["scheduled_pair_meetings"], 1)
        self.assertIsNone(payload["current_season_prior_meeting"])
        self.assertIsNone(
            payload["historical_data"][
                "divisional_home_side_meeting_cohorts"
            ]
        )


class OpinionTest(unittest.IsolatedAsyncioTestCase):
    async def test_generation_records_prompt_and_input_versions(self) -> None:
        output = {
            "predicted_winner": "Philadelphia Eagles",
            "home_win_probability": 0.61,
            "expected_home_margin": 3.5,
            "predicted_away_score": 20,
            "predicted_home_score": 24,
            "confidence_stars": 3,
            "thesis": "The schedule profile modestly favors Philadelphia.",
            "supporting_factors": ["Philadelphia was 2-0 in the sample."],
            "counterarguments": ["The historical sample is small."],
            "no_signal_factors": ["No Thursday history."],
            "discarded_considerations": [
                "Travel fatigue — discarded because no travel input exists."
            ],
            "full_opinion": "A detailed schedule-only opinion.",
        }
        captured = {}

        async def create_fn(**kwargs):
            captured.update(kwargs)
            return SimpleNamespace(
                content=[SimpleNamespace(text=json.dumps(output))]
            )

        store = MemoryStore()
        row = await generate_opinion(
            expert_id="schedule",
            game=_game(),
            history=_history(),
            store=store,
            model="claude-opus-4-6",
            create_fn=create_fn,
        )

        self.assertEqual(row["expert_version"], 21)
        self.assertEqual(row["prompt_version"], 13)
        self.assertEqual(row["model"], "claude-opus-4-6")
        self.assertEqual(row["predicted_winner"], "Philadelphia Eagles")
        self.assertEqual(set(row), set(OPINION_HEADERS))
        self.assertEqual(len(row["prompt_sha256"]), 64)
        self.assertEqual(len(row["input_sha256"]), 64)
        self.assertNotIn("bookmaker", row["input_json"])
        self.assertIn("Schedule Expert v13", captured["system"])
        self.assertEqual(row["generation_status"], "valid")
        self.assertEqual(row["review_status"], "pending")
        self.assertEqual(store.rows, [row])

    async def test_divisional_expert_uses_divisional_input(self) -> None:
        output = {
            "predicted_winner": "Philadelphia Eagles",
            "home_win_probability": 0.58,
            "expected_home_margin": 2.5,
            "predicted_away_score": 20,
            "predicted_home_score": 23,
            "confidence_stars": 2,
            "evidence_paths": [
                "historical_data.home_team.division_games",
                "historical_data.divisional_home_side_meeting_cohorts.nfl.meeting_1",
                "historical_data.divisional_home_side_meeting_cohorts.division.meeting_1",
                "historical_data.divisional_home_side_meeting_cohorts.opponent_pair.meeting_1",
                "historical_data.away_team.division_games",
            ],
            "no_signal_evidence_paths": [
                "current_season_prior_meeting",
            ],
            "nondeterministic_analysis": [
                "Philadelphia appears better positioned for this matchup."
            ],
            "discarded_considerations": [],
        }
        captured = []

        async def create_fn(**kwargs):
            captured.append(kwargs)
            response = (
                output
                if len(captured) == 1
                else {
                    "claims": [
                        {
                            "index": 0,
                            "classification": "reasonable_inference",
                            "evidence_paths": [
                                "historical_data.home_team.division_games"
                            ],
                            "reason": "Grounded interpretation.",
                        }
                    ]
                }
            )
            return SimpleNamespace(
                content=[SimpleNamespace(text=json.dumps(response))]
            )

        store = MemoryStore()
        row = await generate_opinion(
            expert_id="divisional",
            game=_game(),
            history=_history(),
            schedule=[
                {
                    "event_id": "401772510",
                    "season": 2026,
                    "week": 1,
                    "kickoff_utc": _game()["commence_time_utc"],
                    "away_team": "Dallas Cowboys",
                    "home_team": "Philadelphia Eagles",
                },
                {
                    "event_id": "rematch",
                    "season": 2026,
                    "week": 12,
                    "kickoff_utc": "2026-11-26T18:00:00+00:00",
                    "away_team": "Philadelphia Eagles",
                    "home_team": "Dallas Cowboys",
                },
            ],
            store=store,
            create_fn=create_fn,
        )

        self.assertEqual(row["expert_id"], "divisional")
        self.assertEqual(row["input_profile"], "divisional")
        self.assertEqual(row["expert_version"], 28)
        self.assertEqual(row["output_schema_version"], 4)
        self.assertIn("Divisional Expert v17", captured[0]["system"])
        self.assertIn("lean toward Philadelphia Eagles", row["thesis"])
        self.assertEqual(
            row["nondeterministic_factuality_status"],
            "verified",
        )
        self.assertIn(
            "Interpretation:",
            row["nondeterministic_analysis_usable"],
        )
        self.assertIn('"input_profile":"divisional"', row["input_json"])
        self.assertIn("Pick\n", row["full_opinion"])
        stored_support = json.loads(row["supporting_factors_json"])
        self.assertEqual(stored_support["thesis"]["evidence"], [])
        self.assertEqual(
            stored_support["items"][0]["evidence"][0]["path"],
            "historical_data.home_team.division_games",
        )
        self.assertEqual(len(stored_support["items"]), 2)
        self.assertEqual(
            len(json.loads(row["counterarguments_json"])),
            3,
        )

    async def test_schedule_expert_rejects_non_opus_model(self) -> None:
        with self.assertRaisesRegex(ValueError, "not allowed"):
            await generate_opinion(
                expert_id="schedule",
                game=_game(),
                history=_history(),
                store=MemoryStore(),
                model="claude-sonnet-4-6",
            )

    async def test_invalid_model_output_is_persisted_before_error(self) -> None:
        async def create_fn(**kwargs):
            return SimpleNamespace(
                content=[SimpleNamespace(text='{"not": "an opinion"}')]
            )

        store = MemoryStore()
        with self.assertRaises(ValueError):
            await generate_opinion(
                expert_id="schedule",
                game=_game(),
                history=_history(),
                store=store,
                model="claude-opus-4-6",
                create_fn=create_fn,
            )

        self.assertEqual(len(store.rows), 1)
        self.assertEqual(store.rows[0]["generation_status"], "invalid")
        self.assertEqual(store.rows[0]["review_status"], "not_applicable")
        self.assertIn("ValueError", store.rows[0]["generation_error"])
        self.assertEqual(
            store.rows[0]["raw_response"], '{"not": "an opinion"}'
        )

    async def test_validation_repair_persists_invalid_attempt(self) -> None:
        valid = {
            "predicted_winner": "Philadelphia Eagles",
            "home_win_probability": 0.61,
            "expected_home_margin": 3.5,
            "predicted_away_score": 20,
            "predicted_home_score": 24,
            "confidence_stars": 3,
            "thesis": "The schedule profile favors Philadelphia.",
            "supporting_factors": ["Philadelphia was 2-0 in the sample."],
            "counterarguments": ["The historical sample is small."],
            "no_signal_factors": [],
            "discarded_considerations": [],
            "full_opinion": "A repaired schedule opinion.",
        }
        requests = []

        async def create_fn(**kwargs):
            requests.append(kwargs["messages"][0]["content"])
            output = {"not": "an opinion"} if len(requests) == 1 else valid
            return SimpleNamespace(
                content=[SimpleNamespace(text=json.dumps(output))]
            )

        store = MemoryStore()
        row = await generate_opinion(
            expert_id="schedule",
            game=_game(),
            history=_history(),
            store=store,
            create_fn=create_fn,
            repair_attempts=1,
        )

        self.assertEqual(row["generation_status"], "valid")
        self.assertEqual(len(store.rows), 2)
        self.assertEqual(store.rows[0]["generation_status"], "invalid")
        self.assertIn("Validation error:", requests[1])
        self.assertIn('{"not": "an opinion"}', requests[1])

    def test_failed_sheet_append_is_spooled(self) -> None:
        row = {"opinion_id": "pending-1", "raw_response": "complete"}
        with tempfile.TemporaryDirectory() as directory:
            pending = Path(directory) / "moe_pending.jsonl"
            lock = Path(directory) / "moe_pending.lock"
            with (
                patch("moe.MOE_PENDING_PATH", pending),
                patch("moe.MOE_PENDING_LOCK_PATH", lock),
            ):
                with self.assertRaisesRegex(RuntimeError, "spooled"):
                    _persist_attempt(FailingStore(), row)
            payload = json.loads(pending.read_text(encoding="utf-8"))

        self.assertEqual(payload["row"], row)
        self.assertIn("sheet unavailable", payload["append_error"])

    def test_rejects_internally_conflicting_forecast(self) -> None:
        with self.assertRaisesRegex(ValueError, "conflicts"):
            validate_opinion(
                {
                    "predicted_winner": "Philadelphia Eagles",
                    "home_win_probability": 0.4,
                    "expected_home_margin": 3,
                    "predicted_away_score": 20,
                    "predicted_home_score": 24,
                    "confidence_stars": 3,
                    "thesis": "Conflict.",
                    "supporting_factors": ["One."],
                    "counterarguments": ["Two."],
                    "no_signal_factors": [],
                    "discarded_considerations": [],
                    "full_opinion": "Detailed.",
                },
                away_team="Dallas Cowboys",
                home_team="Philadelphia Eagles",
            )

    def test_divisional_opinion_rejects_unsupported_qualitative_labels(
        self,
    ) -> None:
        schedule_input = {
            "input_profile": "divisional",
            "game": {"division_meeting_number": 1, "week": 1},
            "historical_data": {
                "divisional_home_side_meeting_cohorts": {
                    "nfl": {"meeting_1": {"wins": 70, "losses": 74, "ties": 0}},
                    "division": {
                        "name": "NFC West",
                        "meeting_1": {"wins": 8, "losses": 10, "ties": 0},
                    },
                    "opponent_pair": {
                        "meeting_1": {"wins": 1, "losses": 2, "ties": 0}
                    },
                }
            },
        }
        with self.assertRaisesRegex(ValueError, "unsupported qualitative"):
            validate_opinion(
                {
                    "predicted_winner": "Los Angeles Rams",
                    "home_win_probability": 0.56,
                    "expected_home_margin": 2.5,
                    "predicted_away_score": 21,
                    "predicted_home_score": 24,
                    "confidence_stars": 2,
                    "thesis": "An elite divisional record favors the Rams.",
                    "supporting_factors": ["One."],
                    "counterarguments": ["Two."],
                    "no_signal_factors": [],
                    "discarded_considerations": [],
                    "full_opinion": (
                        "NFL-wide home-side: 70-74.\n"
                        "NFC West home-side: 8-10.\n"
                        "Opponent-pair home-side: 1-2."
                    ),
                },
                away_team="San Francisco 49ers",
                home_team="Los Angeles Rams",
                schedule_input=schedule_input,
            )

    def test_requires_exact_score_consistent_with_winner(self) -> None:
        base = {
            "predicted_winner": "Philadelphia Eagles",
            "home_win_probability": 0.6,
            "expected_home_margin": 3.5,
            "predicted_away_score": 20,
            "predicted_home_score": 24,
            "confidence_stars": 3,
            "thesis": "Valid.",
            "supporting_factors": ["One."],
            "counterarguments": ["Two."],
            "no_signal_factors": [],
            "discarded_considerations": [],
            "full_opinion": "Detailed.",
        }
        with self.assertRaisesRegex(ValueError, "predicted_away_score"):
            validate_opinion(
                {**base, "predicted_away_score": 20.5},
                away_team="Dallas Cowboys",
                home_team="Philadelphia Eagles",
            )
        with self.assertRaisesRegex(ValueError, "must not be tied"):
            validate_opinion(
                {**base, "predicted_home_score": 20},
                away_team="Dallas Cowboys",
                home_team="Philadelphia Eagles",
            )
        with self.assertRaisesRegex(ValueError, "conflicts with predicted score"):
            validate_opinion(
                {
                    **base,
                    "predicted_away_score": 27,
                    "predicted_home_score": 20,
                },
                away_team="Dallas Cowboys",
                home_team="Philadelphia Eagles",
            )

    def test_rejects_invented_week_when_input_week_is_unknown(self) -> None:
        schedule_input = build_schedule_input(
            {**_game(), "week": ""},
            _history(),
        )
        with self.assertRaisesRegex(ValueError, "week context"):
            validate_opinion(
                {
                    "predicted_winner": "Philadelphia Eagles",
                    "home_win_probability": 0.6,
                    "expected_home_margin": 3,
                    "predicted_away_score": 20,
                    "predicted_home_score": 24,
                    "confidence_stars": 3,
                    "thesis": "Week 1 favors the home team.",
                    "supporting_factors": ["One."],
                    "counterarguments": ["Two."],
                    "no_signal_factors": [],
                    "discarded_considerations": [],
                    "full_opinion": "This Week 1 matchup favors Philadelphia.",
                },
                away_team="Dallas Cowboys",
                home_team="Philadelphia Eagles",
                schedule_input=schedule_input,
            )
        validate_opinion(
            {
                "predicted_winner": "Philadelphia Eagles",
                "home_win_probability": 0.6,
                "expected_home_margin": 3,
                "predicted_away_score": 20,
                "predicted_home_score": 24,
                "confidence_stars": 3,
                "thesis": "The Thursday weekday split favors the home team.",
                "supporting_factors": ["No games occurred on this day of the week."],
                "counterarguments": ["The weekday sample is empty."],
                "no_signal_factors": ["No Thursday sample."],
                "discarded_considerations": [],
                "full_opinion": "Day-of-the-week evidence is unavailable.",
            },
            away_team="Dallas Cowboys",
            home_team="Philadelphia Eagles",
            schedule_input=schedule_input,
        )

    def test_rejects_empty_evidence_and_oversized_thesis(self) -> None:
        base = {
            "predicted_winner": "Philadelphia Eagles",
            "home_win_probability": 0.6,
            "expected_home_margin": 3,
            "predicted_away_score": 20,
            "predicted_home_score": 24,
            "confidence_stars": 3,
            "thesis": "Valid.",
            "supporting_factors": [],
            "counterarguments": ["Two."],
            "no_signal_factors": [],
            "discarded_considerations": [],
            "full_opinion": "Detailed.",
        }
        with self.assertRaisesRegex(ValueError, "non-empty list"):
            validate_opinion(
                base,
                away_team="Dallas Cowboys",
                home_team="Philadelphia Eagles",
            )
        with self.assertRaisesRegex(ValueError, "500 characters"):
            validate_opinion(
                {
                    **base,
                    "thesis": "x" * 501,
                    "supporting_factors": ["One."],
                },
                away_team="Dallas Cowboys",
                home_team="Philadelphia Eagles",
            )

    def test_rejects_non_finite_margin_and_html_expanded_thesis(self) -> None:
        base = {
            "predicted_winner": "Philadelphia Eagles",
            "home_win_probability": 0.6,
            "expected_home_margin": float("nan"),
            "predicted_away_score": 20,
            "predicted_home_score": 24,
            "confidence_stars": 3,
            "thesis": "Valid.",
            "supporting_factors": ["One."],
            "counterarguments": ["Two."],
            "no_signal_factors": [],
            "discarded_considerations": [],
            "full_opinion": "Detailed.",
        }
        with self.assertRaisesRegex(ValueError, "numeric"):
            validate_opinion(
                base,
                away_team="Dallas Cowboys",
                home_team="Philadelphia Eagles",
            )
        with self.assertRaisesRegex(ValueError, "Telegram summary"):
            validate_opinion(
                {
                    **base,
                    "expected_home_margin": 3,
                    "thesis": "&" * 200,
                },
                away_team="Dallas Cowboys",
                home_team="Philadelphia Eagles",
            )


class OpinionViewTest(unittest.TestCase):
    def _row(
        self,
        expert_id: str,
        generated: str,
        opinion: str = "Detail",
        *,
        model: str = "claude-sonnet-4-6",
    ) -> dict:
        row = {
            "opinion_id": str(
                uuid.uuid5(
                    uuid.NAMESPACE_URL,
                    f"{expert_id}:{model}:{generated}",
                )
            ),
            "generated_at_utc": generated,
            "event_id": "401772510",
            "away_team": "Dallas Cowboys",
            "home_team": "Philadelphia Eagles",
            "expert_id": expert_id,
            "expert_name": expert_id.title(),
            "expert_version": 1,
            "model": model,
            "prompt_sha256": "a" * 64,
            "generation_status": "valid",
            "review_status": "approved",
            "predicted_winner": "Philadelphia Eagles",
            "home_win_probability": 0.61,
            "expected_home_margin": 3.5,
            "predicted_away_score": 20,
            "predicted_home_score": 24,
            "confidence_stars": 3,
            "thesis": "Schedule advantage.",
            "full_opinion": opinion,
            "supporting_factors_json": '["One."]',
            "counterarguments_json": '["Two."]',
            "no_signal_factors_json": "[]",
            "discarded_considerations_json": "[]",
            "raw_response": "{}",
            "pick_market": "straight_up",
            "pick_side": "Philadelphia Eagles",
        }
        row["output_sha256"] = opinion_output_sha256(row)
        row["approved_output_sha256"] = row["output_sha256"]
        return row

    def test_latest_revision_is_used(self) -> None:
        rows = [
            self._row("schedule", "2026-09-01T00:00:00+00:00"),
            self._row("schedule", "2026-09-02T00:00:00+00:00"),
        ]

        latest = latest_opinions(rows)

        self.assertEqual(len(latest), 1)
        self.assertEqual(
            latest[0]["generated_at_utc"], "2026-09-02T00:00:00+00:00"
        )

    def test_latest_revision_per_model_is_available_for_comparison(self) -> None:
        rows = [
            self._row(
                "schedule",
                "2026-09-01T00:00:00+00:00",
                model="claude-sonnet-4-6",
            ),
            self._row(
                "schedule",
                "2026-09-02T00:00:00+00:00",
                model="claude-sonnet-4-6",
            ),
            self._row(
                "schedule",
                "2026-09-03T00:00:00+00:00",
                model="gpt-5.6",
            ),
        ]

        overview = latest_opinions(rows)
        choices = latest_model_opinions(rows, "schedule")

        self.assertEqual(overview[0]["model"], "gpt-5.6")
        self.assertEqual(
            [(row["model"], row["generated_at_utc"]) for row in choices],
            [
                ("gpt-5.6", "2026-09-03T00:00:00+00:00"),
                ("claude-sonnet-4-6", "2026-09-02T00:00:00+00:00"),
            ],
        )

    def test_invalid_newer_run_does_not_hide_approved_model_run(self) -> None:
        approved = self._row(
            "schedule",
            "2026-09-01T00:00:00+00:00",
            model="claude-sonnet-4-6",
        )
        invalid = self._row(
            "schedule",
            "2026-09-02T00:00:00+00:00",
            model="claude-sonnet-4-6",
        )
        invalid["generation_status"] = "invalid"

        choices = latest_model_opinions([approved, invalid], "schedule")

        self.assertEqual(choices, [approved])

    def test_tampered_approved_opinion_is_hidden(self) -> None:
        row = self._row("schedule", "2026-09-01T00:00:00+00:00")
        row["thesis"] = "Changed after approval."

        self.assertEqual(latest_opinions([row]), [])

    def test_reassigned_approved_opinion_is_hidden(self) -> None:
        row = self._row("schedule", "2026-09-01T00:00:00+00:00")
        row["event_id"] = "different-game"

        self.assertEqual(latest_opinions([row]), [])

    def test_blank_or_malformed_approved_number_is_hidden(self) -> None:
        zero = self._row("schedule", "2026-09-01T00:00:00+00:00")
        zero["expected_home_margin"] = 0.0
        zero["output_sha256"] = opinion_output_sha256(zero)
        zero["approved_output_sha256"] = zero["output_sha256"]
        zero["expected_home_margin"] = ""
        malformed = self._row(
            "schedule-2", "2026-09-02T00:00:00+00:00"
        )
        malformed["home_win_probability"] = "not-a-number"

        self.assertEqual(latest_opinions([zero, malformed]), [])

    def test_summary_and_long_detail_are_paginated(self) -> None:
        rows = [
            self._row(
                f"expert-{index}",
                f"2026-09-{index + 1:02d}T00:00:00+00:00",
            )
            for index in range(6)
        ]
        summary, buttons = opinion_summary(_game(), rows, page=1)
        detail, detail_buttons = opinion_detail(
            self._row("schedule", "2026-09-01T00:00:00+00:00", "x" * 6000),
            page=1,
        )

        self.assertIn("Page 2 of 2", summary)
        self.assertIn("Expert-5", summary)
        self.assertIn("claude-sonnet-4-6", summary)
        self.assertLess(len(detail), 4096)
        self.assertIn("model claude-sonnet-4-6", detail)
        self.assertIn("page 2/3", detail)
        self.assertTrue(detail_buttons)

    def test_model_picker_lists_latest_opinion_for_each_model(self) -> None:
        rows = [
            self._row(
                "schedule",
                "2026-09-01T00:00:00+00:00",
                model="claude-sonnet-4-6",
            ),
            self._row(
                "schedule",
                "2026-09-02T00:00:00+00:00",
                model="gpt-5.6",
            ),
        ]

        text, buttons = opinion_model_picker(
            rows,
            expert_id="schedule",
            event_id="401772510",
        )
        detail, detail_buttons = opinion_detail(
            rows[1],
            event_id="401772510",
            show_model_picker=True,
        )

        self.assertIn("Choose a model", text)
        self.assertIn("claude-sonnet-4-6", text)
        self.assertIn("gpt-5.6", text)
        callbacks = [
            button.data
            for row in buttons
            for button in row
            if getattr(button, "data", b"").startswith(b"moe:opinion:")
        ]
        self.assertEqual(len(callbacks), 2)
        self.assertTrue(all(len(callback) <= 64 for callback in callbacks))
        self.assertTrue(
            any(
                button.text == "← Models"
                for row in detail_buttons
                for button in row
            )
        )
        self.assertIn("model gpt-5.6", detail)

    def test_expert_prompt_is_loaded_from_versioned_file(self) -> None:
        expert = load_expert("schedule")

        self.assertEqual(expert["version"], 21)
        self.assertEqual(expert["prompt_version"], 13)
        self.assertEqual(expert["prompt_path"], "moe/prompts/schedule/v13.md")
        self.assertEqual(expert["default_model"], "claude-opus-4-6")
        self.assertEqual(expert["allowed_models"], ["claude-opus-4-6"])
        self.assertEqual(len(expert["prompt_sha256"]), 64)

        divisional = load_expert("divisional")
        self.assertEqual(divisional["version"], 28)
        self.assertEqual(divisional["prompt_version"], 17)
        self.assertEqual(divisional["output_schema_version"], 4)
        self.assertEqual(
            divisional["prompt_path"],
            "moe/prompts/divisional/v17.md",
        )
        self.assertEqual(divisional["default_model"], "claude-opus-4-6")


if __name__ == "__main__":
    unittest.main()
