#!/usr/bin/env python3
"""Tests for the NFL Cee Expert."""

from __future__ import annotations

import json
import unittest
from types import SimpleNamespace

from moe import _normalize_cited_claim, generate_opinion, load_expert
from moe_cee import build_cee_input
from nfl_lines import (
    LATEST_AWAY_COLUMN,
    LATEST_HOME_COLUMN,
    LATEST_TOTALS_COLUMN,
)


EVENT_ID = "patriots-seahawks"
CEE_ID = "6097731988"


def _game() -> dict:
    return {
        "event_id": EVENT_ID,
        "season": 2026,
        "week": 1,
        "commence_time_utc": "2026-09-10T00:20:00+00:00",
        "away_team": "New England Patriots",
        "home_team": "Seattle Seahawks",
    }


def _market_columns() -> dict:
    return {
        LATEST_AWAY_COLUMN: (
            "4,-110,170|nodata,nodata,nodata|nodata,nodata,nodata"
        ),
        LATEST_HOME_COLUMN: (
            "-4,-110,-190|nodata,nodata,nodata|nodata,nodata,nodata"
        ),
        LATEST_TOTALS_COLUMN: (
            "45.5,-110,-110|nodata,nodata,nodata|nodata,nodata,nodata"
        ),
    }


def _current_lean() -> dict:
    return {
        **_game(),
        **_market_columns(),
        "submission_id": "telegram:6097731988:436",
        "submitted_at_utc": "2026-09-07T20:34:11+00:00",
        "telegram_user_id": CEE_ID,
        "period": "game",
        "market": "moneyline",
        "side": "New England Patriots",
        "lean_text": "Because I took the spread",
    }


def _spread_lean() -> dict:
    return {
        **_game(),
        **_market_columns(),
        "submission_id": "telegram:6097731988:435",
        "submitted_at_utc": "2026-09-07T19:34:11+00:00",
        "telegram_user_id": CEE_ID,
        "period": "game",
        "market": "spread",
        "side": "Seattle Seahawks",
        "lean_text": "Seattle should cover at home",
    }


def _earlier_current_moneyline(
    *,
    side: str = "Seattle Seahawks",
    submitted_at: str = "2026-09-06T20:34:11+00:00",
) -> dict:
    return {
        **_current_lean(),
        "submission_id": "telegram:6097731988:400",
        "submitted_at_utc": submitted_at,
        "side": side,
        "lean_text": "Earlier moneyline position",
    }


def _prediction(
    revision_id: str,
    season: int,
    team: str,
    wins: int,
    submitted_at: str,
) -> dict:
    return {
        "revision_id": revision_id,
        "submitted_at_utc": submitted_at,
        "telegram_user_id": CEE_ID,
        "season": season,
        "team": team,
        "predicted_wins": wins,
    }


def _predictions() -> list[dict]:
    return [
        _prediction(
            "2026-ne",
            2026,
            "New England Patriots",
            12,
            "2026-08-10T00:00:00+00:00",
        ),
        _prediction(
            "2026-sea",
            2026,
            "Seattle Seahawks",
            11,
            "2026-08-10T00:00:00+00:00",
        ),
        _prediction(
            "2026-sea-late",
            2026,
            "Seattle Seahawks",
            13,
            "2026-09-08T00:00:00+00:00",
        ),
        _prediction(
            "2025-ne",
            2025,
            "New England Patriots",
            8,
            "2025-08-10T00:00:00+00:00",
        ),
        _prediction(
            "2025-sea",
            2025,
            "Seattle Seahawks",
            10,
            "2025-08-10T00:00:00+00:00",
        ),
    ]


def _prior_lean() -> dict:
    return {
        **_market_columns(),
        "event_id": "prior-patriots-seahawks",
        "season": 2025,
        "away_team": "New England Patriots",
        "home_team": "Seattle Seahawks",
        "commence_time_utc": "2025-09-10T00:20:00+00:00",
        "submission_id": "telegram:6097731988:100",
        "submitted_at_utc": "2025-09-07T20:00:00+00:00",
        "telegram_user_id": CEE_ID,
        "period": "game",
        "market": "moneyline",
        "side": "Seattle Seahawks",
        "lean_text": "Seattle should win.",
    }


def _prior_spread_lean(
    *,
    submission_id: str = "telegram:6097731988:101",
    submitted_at: str = "2025-09-07T21:00:00+00:00",
) -> dict:
    return {
        **_prior_lean(),
        "submission_id": submission_id,
        "submitted_at_utc": submitted_at,
        "market": "spread",
        "side": "Seattle Seahawks",
        "lean_text": "Seattle should cover.",
    }


def _history() -> list[dict]:
    return [
        {
            "kickoff_utc": "2025-09-10T00:20:00+00:00",
            "away_team": "New England Patriots",
            "home_team": "Seattle Seahawks",
            "away_score": 20,
            "home_score": 24,
        }
    ]


class MemoryStore:
    def __init__(self) -> None:
        self.rows: list[dict] = []

    def append(self, row: dict) -> None:
        self.rows.append(dict(row))

    def list(self, event_id: str | None = None) -> list[dict]:
        return list(self.rows)


class CeeInputTest(unittest.TestCase):
    def test_builds_time_frozen_season_and_nfl_calibration(self) -> None:
        payload = build_cee_input(
            _game(),
            _history(),
            [_current_lean(), _spread_lean(), _prior_lean()],
            _predictions(),
            cee_user_id=CEE_ID,
        )

        season = payload["season_predictions_at_submission"]
        self.assertEqual(season["away_predicted_wins"], 12)
        self.assertEqual(season["home_predicted_wins"], 11)
        self.assertEqual(season["season_preferred_side"], "New England Patriots")
        self.assertEqual(
            season["consistency_with_game_pick"],
            "consistent",
        )
        self.assertEqual(
            payload["cee_submissions"]["moneyline"]["rationale"],
            "Because I took the spread",
        )
        self.assertEqual(
            payload["cee_submissions"]["spread"]["selected_side"],
            "Seattle Seahawks",
        )
        self.assertEqual(
            payload["cee_submissions"]["spread"]["rationale"],
            "Seattle should cover at home",
        )
        self.assertEqual(
            payload["market_relationship"]["status"],
            "split_conflicting",
        )
        self.assertEqual(
            payload["decision_history"]["moneyline"]["decision_pattern"],
            "initial_only",
        )
        self.assertEqual(
            payload["decision_history"]["moneyline"]["submission_count"],
            1,
        )
        self.assertEqual(
            payload["spread_season_predictions_at_submission"][
                "consistency_with_game_pick"
            ],
            "inconsistent",
        )
        calibration = payload["nfl_calibration"]
        self.assertEqual(calibration["overall"]["chronological_results"], "W")
        self.assertEqual(
            calibration["matching_consistency"]["chronological_results"],
            "W",
        )
        self.assertEqual(calibration["matching_season_gap"]["games"], 0)
        self.assertEqual(
            calibration["matching_decision_pattern"][
                "chronological_results"
            ],
            "W",
        )
        self.assertEqual(
            calibration["matching_spread_decision_pattern"]["games"],
            0,
        )

    def test_requires_full_game_moneyline(self) -> None:
        with self.assertRaisesRegex(
            ValueError,
            "no full-game moneyline pick",
        ):
            build_cee_input(
                _game(),
                [],
                [_spread_lean()],
                _predictions(),
                cee_user_id=CEE_ID,
            )

    def test_selects_latest_submission_for_each_market(self) -> None:
        old_spread = {
            **_spread_lean(),
            "submission_id": "telegram:6097731988:400",
            "submitted_at_utc": "2026-09-06T19:34:11+00:00",
            "side": "New England Patriots",
            "lean_text": "Old spread position",
        }

        payload = build_cee_input(
            _game(),
            [],
            [_current_lean(), old_spread, _spread_lean()],
            _predictions(),
            cee_user_id=CEE_ID,
        )

        self.assertEqual(
            payload["cee_submissions"]["spread"]["rationale"],
            "Seattle should cover at home",
        )
        self.assertEqual(
            payload["market_relationship"]["status"],
            "split_conflicting",
        )
        self.assertEqual(
            payload["decision_history"]["spread"]["decision_pattern"],
            "changed",
        )
        self.assertEqual(
            payload["decision_history"]["spread"]["initial_side"],
            "New England Patriots",
        )
        self.assertEqual(
            payload["decision_history"]["spread"]["final_side"],
            "Seattle Seahawks",
        )

    def test_classifies_reaffirmed_and_changed_decisions(self) -> None:
        earlier_spread = {
            **_spread_lean(),
            "submission_id": "telegram:6097731988:410",
            "submitted_at_utc": "2026-09-06T19:34:11+00:00",
            "lean_text": "Earlier Seattle spread position",
        }

        payload = build_cee_input(
            _game(),
            [],
            [
                _earlier_current_moneyline(),
                _current_lean(),
                earlier_spread,
                _spread_lean(),
            ],
            _predictions(),
            cee_user_id=CEE_ID,
        )

        moneyline = payload["decision_history"]["moneyline"]
        self.assertEqual(moneyline["decision_pattern"], "changed")
        self.assertEqual(moneyline["change_count"], 1)
        self.assertEqual(moneyline["reaffirmation_count"], 0)
        self.assertEqual(moneyline["initial_side"], "Seattle Seahawks")
        self.assertEqual(moneyline["final_side"], "New England Patriots")
        self.assertEqual(
            [row["selected_side"] for row in moneyline["submissions"]],
            ["Seattle Seahawks", "New England Patriots"],
        )

        spread = payload["decision_history"]["spread"]
        self.assertEqual(spread["decision_pattern"], "reaffirmed")
        self.assertEqual(spread["change_count"], 0)
        self.assertEqual(spread["reaffirmation_count"], 1)
        self.assertEqual(spread["submission_count"], 2)

    def test_calibration_counts_each_game_once_by_final_decision(self) -> None:
        prior_reaffirmation = {
            **_prior_lean(),
            "submission_id": "telegram:6097731988:90",
            "submitted_at_utc": "2025-09-06T20:00:00+00:00",
            "lean_text": "Earlier Seattle moneyline position.",
        }

        payload = build_cee_input(
            _game(),
            _history(),
            [
                _earlier_current_moneyline(side="New England Patriots"),
                _current_lean(),
                prior_reaffirmation,
                _prior_lean(),
            ],
            _predictions(),
            cee_user_id=CEE_ID,
        )

        calibration = payload["nfl_calibration"]
        self.assertEqual(calibration["overall"]["games"], 1)
        self.assertEqual(calibration["overall"]["chronological_results"], "W")
        self.assertEqual(
            calibration["matching_decision_pattern"]["decision_pattern"],
            "reaffirmed",
        )
        self.assertEqual(
            calibration["matching_decision_pattern"][
                "chronological_results"
            ],
            "W",
        )

    def test_calibrates_reaffirmed_spread_decisions_at_submitted_line(
        self,
    ) -> None:
        earlier_current_spread = {
            **_spread_lean(),
            "submission_id": "telegram:6097731988:410",
            "submitted_at_utc": "2026-09-06T19:34:11+00:00",
        }
        earlier_prior_spread = _prior_spread_lean(
            submission_id="telegram:6097731988:91",
            submitted_at="2025-09-06T21:00:00+00:00",
        )

        payload = build_cee_input(
            _game(),
            _history(),
            [
                _current_lean(),
                earlier_current_spread,
                _spread_lean(),
                _prior_lean(),
                earlier_prior_spread,
                _prior_spread_lean(),
            ],
            _predictions(),
            cee_user_id=CEE_ID,
        )

        spread = payload["nfl_calibration"][
            "matching_spread_decision_pattern"
        ]
        self.assertEqual(spread["decision_pattern"], "reaffirmed")
        self.assertEqual(spread["games"], 1)
        self.assertEqual(spread["chronological_results"], "T")

    def test_latest_eligible_submission_ignores_post_kickoff_revision(
        self,
    ) -> None:
        post_kickoff = {
            **_spread_lean(),
            "submission_id": "telegram:6097731988:999",
            "submitted_at_utc": "2026-09-10T01:00:00+00:00",
            "side": "New England Patriots",
            "lean_text": "Too late",
        }

        payload = build_cee_input(
            _game(),
            [],
            [_current_lean(), _spread_lean(), post_kickoff],
            _predictions(),
            cee_user_id=CEE_ID,
        )

        self.assertEqual(
            payload["cee_submissions"]["spread"]["rationale"],
            "Seattle should cover at home",
        )

    def test_spread_survives_without_prior_season_predictions(self) -> None:
        predictions = [
            {
                **row,
                "submitted_at_utc": (
                    "2026-09-07T19:50:00+00:00"
                    if row["season"] == 2026
                    else row["submitted_at_utc"]
                ),
            }
            for row in _predictions()
        ]

        payload = build_cee_input(
            _game(),
            [],
            [_current_lean(), _spread_lean()],
            predictions,
            cee_user_id=CEE_ID,
        )

        self.assertEqual(
            payload["cee_submissions"]["spread"]["selected_side"],
            "Seattle Seahawks",
        )
        self.assertEqual(
            payload["spread_season_predictions_at_submission"],
            {"status": "unavailable_before_submission"},
        )


class CeeGenerationTest(unittest.IsolatedAsyncioTestCase):
    def test_accepts_numbers_from_cited_cee_rationales(self) -> None:
        payload = build_cee_input(
            _game(),
            [],
            [
                {
                    **_current_lean(),
                    "lean_text": (
                        "Champions are 16-4 in this spot over 20 years."
                    ),
                },
                {
                    **_spread_lean(),
                    "lean_text": "Take Seattle -4 based on this angle.",
                },
            ],
            _predictions(),
            cee_user_id=CEE_ID,
        )

        moneyline = _normalize_cited_claim(
            {
                "claim": (
                    "Cee cited a 16-4 record over 20 years in support of the "
                    "moneyline pick."
                ),
                "evidence_paths": ["cee_submissions.moneyline"],
            },
            payload,
            role="supporting factor",
        )
        spread = _normalize_cited_claim(
            {
                "claim": "Cee's spread rationale selected Seattle -4.",
                "evidence_paths": ["decision_history.spread"],
            },
            payload,
            role="supporting factor",
        )
        dated_history = _normalize_cited_claim(
            {
                "claim": (
                    "Cee submitted the moneyline position on 2026-09-07."
                ),
                "evidence_paths": ["decision_history.moneyline"],
            },
            payload,
            role="supporting factor",
        )

        self.assertIn("16-4", moneyline["claim"])
        self.assertIn("-4", spread["claim"])
        self.assertIn("2026-09-07", dated_history["claim"])

    def test_does_not_trust_numbers_from_other_freeform_prose(self) -> None:
        with self.assertRaisesRegex(ValueError, "16-4"):
            _normalize_cited_claim(
                {
                    "claim": "An uncited source claimed a 16-4 record.",
                    "evidence_paths": ["other"],
                },
                {
                    "game": _game(),
                    "other": {"rationale": "The record was 16-4."},
                },
                role="supporting factor",
            )

    def test_zero_eligible_calibration_is_valid_no_signal(self) -> None:
        payload = build_cee_input(
            _game(),
            [],
            [_current_lean()],
            _predictions(),
            cee_user_id=CEE_ID,
        )

        normalized = _normalize_cited_claim(
            {
                "claim": (
                    "Calibration has 0 eligible predictions, so it provides "
                    "no signal despite the 12 to 11 season ordering."
                ),
                "evidence_paths": [
                    "nfl_calibration",
                    "season_predictions_at_submission",
                ],
            },
            payload,
            role="no_signal_factors[0]",
        )

        self.assertEqual(
            normalized["evidence"][0]["path"],
            "nfl_calibration",
        )

    def _output(self) -> dict:
        return {
            "predicted_winner": "New England Patriots",
            "predicted_away_score": 24,
            "predicted_home_score": 23,
            "home_win_probability": 0.45,
            "expected_home_margin": -1.0,
            "confidence_stars": 2,
            "thesis": {
                "claim": (
                    "Cee selects the New England Patriots on the moneyline "
                    "and the Seattle Seahawks -4 against the spread, a "
                    "split_conflicting position that cannot also cash with "
                    "a New England Patriots win."
                ),
                "evidence_paths": [
                    "cee_submissions.moneyline",
                    "cee_submissions.spread",
                    "market_relationship",
                ],
            },
            "supporting_factors": [
                {
                    "claim": (
                        "Cee projected the New England Patriots for 12 wins "
                        "and the Seattle Seahawks for 11, so the New England "
                        "Patriots game pick is consistent with the season "
                        "ordering in the one_win bucket."
                    ),
                    "evidence_paths": [
                        "season_predictions_at_submission"
                    ],
                },
                {
                    "claim": (
                        "At the spread submission, Cee's 12-win New England "
                        "Patriots projection ranked above the 11-win Seattle "
                        "Seahawks projection, making the Seattle Seahawks "
                        "spread pick inconsistent with the season ordering."
                    ),
                    "evidence_paths": [
                        "spread_season_predictions_at_submission"
                    ],
                },
                {
                    "claim": (
                        "Cee's resolved NFL moneyline record is 1-0 across "
                        "1 eligible resolved pick."
                    ),
                    "evidence_paths": ["nfl_calibration.overall"],
                },
                {
                    "claim": (
                        "The matching consistent-pick bucket is 1-0 across "
                        "1 game."
                    ),
                    "evidence_paths": [
                        "nfl_calibration.matching_consistency"
                    ],
                },
                {
                    "claim": (
                        "Calibration uses 1 eligible resolved pre-kickoff "
                        "Cee moneyline pick."
                    ),
                    "evidence_paths": ["nfl_calibration"],
                },
                {
                    "claim": (
                        "Cee's moneyline decision pattern is initial_only "
                        "across 1 submission."
                    ),
                    "evidence_paths": ["decision_history.moneyline"],
                },
                {
                    "claim": (
                        "Cee's spread decision history shows a single "
                        "initial_only New England spread submission."
                    ),
                    "evidence_paths": ["decision_history.spread"],
                },
                {
                    "claim": (
                        "The matching initial_only decision-pattern bucket "
                        "is 1-0 across 1 game."
                    ),
                    "evidence_paths": [
                        "nfl_calibration.matching_decision_pattern"
                    ],
                },
            ],
            "counterarguments": [
                {
                    "claim": (
                        "At the moneyline submission Seattle was -190 with "
                        "a 45.5 total."
                    ),
                    "evidence_paths": ["submission_markets.moneyline"],
                },
                {
                    "claim": (
                        "At the spread submission Seattle was -4 at -110."
                    ),
                    "evidence_paths": ["submission_markets.spread"],
                }
            ],
            "no_signal_factors": [
                {
                    "claim": (
                        "The matching two_to_three_wins season-gap bucket "
                        "has 0 games, so it provides no signal."
                    ),
                    "evidence_paths": [
                        "nfl_calibration.matching_season_gap"
                    ],
                },
                {
                    "claim": (
                        "The matching initial_only spread decision-pattern "
                        "bucket has 0 games, so it provides no ATS signal."
                    ),
                    "evidence_paths": [
                        "nfl_calibration.matching_spread_decision_pattern"
                    ],
                },
            ],
            "discarded_considerations": [
                "Unverified factual premises in the rationale were treated "
                "only as Cee's stated belief."
            ],
        }

    async def test_generates_cited_side_only_opinion(self) -> None:
        async def create_fn(**_kwargs):
            return SimpleNamespace(
                content=[SimpleNamespace(text=json.dumps(self._output()))]
            )

        store = MemoryStore()
        row = await generate_opinion(
            expert_id="cee",
            game=_game(),
            history=_history(),
            leans=[_current_lean(), _spread_lean(), _prior_lean()],
            win_predictions=_predictions(),
            cee_user_id=CEE_ID,
            store=store,
            create_fn=create_fn,
        )

        self.assertEqual(row["generation_status"], "valid")
        self.assertEqual(row["expert_id"], "cee")
        self.assertEqual(row["output_schema_version"], 3)
        self.assertEqual(row["pick_market"], "straight_up")
        self.assertEqual(row["pick_side"], "New England Patriots")
        self.assertEqual(store.rows, [row])

    async def test_validation_repair_keeps_cee_identity(self) -> None:
        responses = [{}, self._output()]

        async def create_fn(**_kwargs):
            return SimpleNamespace(
                content=[SimpleNamespace(text=json.dumps(responses.pop(0)))]
            )

        store = MemoryStore()
        row = await generate_opinion(
            expert_id="cee",
            game=_game(),
            history=_history(),
            leans=[_current_lean(), _spread_lean(), _prior_lean()],
            win_predictions=_predictions(),
            cee_user_id=CEE_ID,
            store=store,
            create_fn=create_fn,
            repair_attempts=1,
        )

        self.assertEqual(row["generation_status"], "valid")
        self.assertEqual(
            [item["generation_status"] for item in store.rows],
            ["invalid", "valid"],
        )

    async def test_accepts_spelled_zero_calibration_counts(self) -> None:
        output = self._output()
        output["supporting_factors"][2]["claim"] = (
            "Cee's NFL calibration contains zero resolved pre-kickoff "
            "full-game moneyline picks."
        )
        output["supporting_factors"][3]["claim"] = (
            "The matching consistent-pick bucket is 0-0-0 across zero games."
        )
        output["supporting_factors"][4]["claim"] = (
            "Calibration uses zero eligible resolved pre-kickoff Cee "
            "moneyline picks."
        )
        output["supporting_factors"][7]["claim"] = (
            "The matching initial_only decision-pattern bucket has zero "
            "games, so it provides no signal."
        )

        async def create_fn(**_kwargs):
            return SimpleNamespace(
                content=[SimpleNamespace(text=json.dumps(output))]
            )

        row = await generate_opinion(
            expert_id="cee",
            game=_game(),
            history=[],
            leans=[_current_lean(), _spread_lean()],
            win_predictions=_predictions(),
            cee_user_id=CEE_ID,
            store=MemoryStore(),
            create_fn=create_fn,
        )

        self.assertEqual(row["generation_status"], "valid")

    async def test_accepts_prediction_as_calibration_count_noun(self) -> None:
        output = self._output()
        output["supporting_factors"][2]["claim"] = (
            "Cee's resolved NFL moneyline record is 1-0 across 1 eligible "
            "prediction."
        )

        async def create_fn(**_kwargs):
            return SimpleNamespace(
                content=[SimpleNamespace(text=json.dumps(output))]
            )

        row = await generate_opinion(
            expert_id="cee",
            game=_game(),
            history=_history(),
            leans=[_current_lean(), _spread_lean(), _prior_lean()],
            win_predictions=_predictions(),
            cee_user_id=CEE_ID,
            store=MemoryStore(),
            create_fn=create_fn,
        )

        self.assertEqual(row["generation_status"], "valid")

    async def test_rejects_missing_season_gap_bucket(self) -> None:
        output = self._output()
        output["supporting_factors"][0]["claim"] = (
            "Cee projected the New England Patriots for 12 wins and the "
            "Seattle Seahawks for 11, so the New England Patriots game pick "
            "is consistent with the season ordering."
        )

        async def create_fn(**_kwargs):
            return SimpleNamespace(
                content=[SimpleNamespace(text=json.dumps(output))]
            )

        with self.assertRaisesRegex(ValueError, "season gap bucket"):
            await generate_opinion(
                expert_id="cee",
                game=_game(),
                history=_history(),
                leans=[_current_lean(), _spread_lean(), _prior_lean()],
                win_predictions=_predictions(),
                cee_user_id=CEE_ID,
                store=MemoryStore(),
                create_fn=create_fn,
            )

    async def test_rejects_spread_pick_override(self) -> None:
        output = self._output()
        output["pick_market"] = "spread"
        output["pick_side"] = "Seattle Seahawks -4"

        async def create_fn(**_kwargs):
            return SimpleNamespace(
                content=[SimpleNamespace(text=json.dumps(output))]
            )

        with self.assertRaisesRegex(ValueError, "remain straight_up"):
            await generate_opinion(
                expert_id="cee",
                game=_game(),
                history=_history(),
                leans=[_current_lean(), _spread_lean(), _prior_lean()],
                win_predictions=_predictions(),
                cee_user_id=CEE_ID,
                store=MemoryStore(),
                create_fn=create_fn,
            )

    def test_expert_configuration_is_versioned(self) -> None:
        expert = load_expert("cee")

        self.assertEqual(expert["version"], 4)
        self.assertEqual(expert["prompt_version"], 4)
        self.assertEqual(expert["output_schema_version"], 3)
        self.assertEqual(expert["prompt_path"], "moe/prompts/cee/v4.md")
        self.assertIn(
            "discarded-consideration string must contain no digits",
            expert["prompt_text"],
        )
        self.assertEqual(
            expert["allowed_models"],
            [
                "claude-opus-4-8",
                "claude-fable-5",
                "claude-sonnet-4-6",
                "claude-haiku-4-5",
            ],
        )


if __name__ == "__main__":
    unittest.main()
