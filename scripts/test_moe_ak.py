#!/usr/bin/env python3
"""Tests for deterministic AK Expert parsing and generation."""

from __future__ import annotations

import json
import unittest
from types import SimpleNamespace

from moe import generate_opinion
from moe_ak import (
    _favorite,
    _projection_from_row,
    _side_gap_bucket,
    _total_gap_bucket,
    _wnba_total_prior,
    build_ak_input,
    load_wnba_prior,
    parse_ak_projection,
)
from scripts.backfill_ak_predictions import apply_reviewed_override
from nfl_lines import (
    AWAY_SNAPSHOT_COLUMN,
    HOME_SNAPSHOT_COLUMN,
    LATEST_AWAY_COLUMN,
    LATEST_HOME_COLUMN,
    LATEST_TOTALS_COLUMN,
    OPENING_AWAY_COLUMN,
    OPENING_HOME_COLUMN,
    OPENING_TOTALS_COLUMN,
    TOTALS_SNAPSHOT_COLUMN,
)


EVENT_ID = "rams-49ers"


def _game() -> dict:
    away = "3.5,-110,150|nodata,nodata,nodata|nodata,nodata,nodata"
    home = "-3.5,-110,-170|nodata,nodata,nodata|nodata,nodata,nodata"
    totals = "45.5,-110,-110|nodata,nodata,nodata|nodata,nodata,nodata"
    return {
        "event_id": EVENT_ID,
        "season": 2026,
        "week": 1,
        "commence_time_utc": "2026-09-11T00:00:00+00:00",
        "away_team": "San Francisco 49ers",
        "home_team": "Los Angeles Rams",
        "bookmaker": "betonlineag",
        OPENING_AWAY_COLUMN: away,
        OPENING_HOME_COLUMN: home,
        OPENING_TOTALS_COLUMN: totals,
        LATEST_AWAY_COLUMN: away,
        LATEST_HOME_COLUMN: home,
        LATEST_TOTALS_COLUMN: totals,
    }


def _lean() -> dict:
    game = _game()
    return {
        **game,
        "submission_id": "telegram:123:456",
        "submitted_at_utc": "2026-09-01T00:00:00+00:00",
        "telegram_user_id": "123",
        "period": "game",
        "market": "moneyline",
        "side": "Los Angeles Rams",
        "lean_text": (
            "Great Rams team hosting the 49ers. "
            "Expect Rams to win 27-23 as a -3.5 home favorite."
        ),
    }


class MemoryStore:
    def __init__(self) -> None:
        self.rows: list[dict] = []

    def append(self, row: dict) -> None:
        self.rows.append(row)

    def list(self, event_id: str | None = None) -> list[dict]:
        return self.rows


class AkProjectionTest(unittest.TestCase):
    def test_applies_reviewed_score_ownership_override(self) -> None:
        item = {
            "event_id": "browns-jaguars",
            "away_team": "Cleveland Browns",
            "home_team": "Jacksonville Jaguars",
            "status": "ambiguous",
        }

        corrected = apply_reviewed_override(
            item,
            {"away_score": 18, "home_score": 24},
        )

        self.assertEqual(corrected["status"], "parsed")
        self.assertEqual(corrected["parse_version"], "human_v1")
        self.assertEqual(corrected["winner"], "Jacksonville Jaguars")

    def test_parses_explicit_winner_score(self) -> None:
        parsed = parse_ak_projection(
            _lean()["lean_text"],
            away_team="San Francisco 49ers",
            home_team="Los Angeles Rams",
        )

        self.assertEqual(parsed["status"], "parsed")
        self.assertEqual(parsed["away_score"], 23)
        self.assertEqual(parsed["home_score"], 27)
        self.assertEqual(parsed["winner"], "Los Angeles Rams")

    def test_rejects_unlabeled_score_pair(self) -> None:
        parsed = parse_ak_projection(
            "I think this ends 27-23.",
            away_team="San Francisco 49ers",
            home_team="Los Angeles Rams",
        )

        self.assertEqual(parsed["status"], "ambiguous")

    def test_parses_exact_intake_prompt_format(self) -> None:
        parsed = parse_ak_projection(
            "Score: San Francisco 49ers 23, Los Angeles Rams 27",
            away_team="San Francisco 49ers",
            home_team="Los Angeles Rams",
        )

        self.assertEqual(parsed["status"], "parsed")
        self.assertEqual(parsed["away_score"], 23)
        self.assertEqual(parsed["home_score"], 27)

    def test_rejects_conflicting_labeled_correction(self) -> None:
        parsed = parse_ak_projection(
            "Score: 49ers 23, Rams 27. Correction: 49ers 24, Rams 28.",
            away_team="San Francisco 49ers",
            home_team="Los Angeles Rams",
        )

        self.assertEqual(parsed["status"], "conflicting")

    def test_reviewed_normalized_scores_are_authoritative(self) -> None:
        row = {
            **_lean(),
            "prediction_parse_status": "parsed",
            "predicted_away_score": 21,
            "predicted_home_score": 24,
            "lean_text": "Rams to win 40-10.",
        }

        parsed = _projection_from_row(row)

        self.assertEqual(parsed["away_score"], 21)
        self.assertEqual(parsed["home_score"], 24)

    def test_spread_in_rationale_is_not_parsed_as_score(self) -> None:
        parsed = parse_ak_projection(
            "Score: 49ers 23, Rams 27. Rationale: Rams -3.5 at home.",
            away_team="San Francisco 49ers",
            home_team="Los Angeles Rams",
        )

        self.assertEqual(parsed["status"], "parsed")
        self.assertEqual(parsed["home_score"], 27)

    def test_builds_capped_cold_start_input(self) -> None:
        payload = build_ak_input(
            _game(),
            [],
            [_lean()],
            ak_user_id="123",
        )

        self.assertEqual(payload["market_gaps"]["side_gap_bucket"], "within_1")
        self.assertEqual(payload["market_gaps"]["total_gap"], 4.5)
        self.assertEqual(
            payload["market_gaps"]["total_gap_bucket"],
            "plus_3_to_9",
        )
        self.assertEqual(
            payload["cross_sport_prior"]["total"]["wnba_mapping_band"],
            "wnba_6_to_12",
        )
        self.assertEqual(
            payload["cross_sport_prior"][
                "side"
            ][
                "effective_equivalent_nfl_observations"
            ],
            2,
        )
        self.assertEqual(
            payload["nfl_calibration"]["eligible_predictions"],
            0,
        )
        self.assertNotIn(
            "submission_id",
            payload["ak_submission"],
        )

    def test_uses_reviewed_nfl_to_wnba_total_bands(self) -> None:
        self.assertEqual(_total_gap_bucket(2), "plus_0_to_3")
        self.assertEqual(_total_gap_bucket(3.5), "plus_3_to_9")
        self.assertEqual(_total_gap_bucket(9), "plus_9_to_12")
        self.assertEqual(_total_gap_bucket(12), "plus_12_or_more")
        band, summary = _wnba_total_prior(
            2,
            load_wnba_prior(),
        )

        self.assertEqual(band, "wnba_0_to_6")
        self.assertIn("3 of 4", summary)

    def test_unavailable_wnba_band_cannot_support_a_pick(self) -> None:
        lean = {
            **_lean(),
            "prediction_parse_status": "parsed",
            "predicted_away_score": 30,
            "predicted_home_score": 30,
        }
        lean["predicted_home_score"] = 31
        payload = build_ak_input(
            _game(),
            [],
            [lean],
            ak_user_id="123",
        )
        total_prior = next(
            item
            for item in payload["evidence_catalog"]
            if item["id"] == "wnba_total_prior"
        )

        self.assertEqual(
            payload["cross_sport_prior"]["total"]["interpretation"],
            "no_direction",
        )
        self.assertFalse(total_prior["supporting_allowed"])
        self.assertNotIn("total_gap", payload["cross_sport_prior"])

    def test_separates_pickem_and_missing_spread(self) -> None:
        favorite, magnitude, state = _favorite(
            {"away_spread": 0, "home_spread": 0},
            "Away",
            "Home",
        )
        self.assertIsNone(favorite)
        self.assertEqual(magnitude, 0)
        self.assertEqual(
            _side_gap_bucket("Away", favorite, None, state),
            "pickem",
        )
        favorite, magnitude, state = _favorite(
            {"away_spread": None, "home_spread": None},
            "Away",
            "Home",
        )
        self.assertIsNone(favorite)
        self.assertIsNone(magnitude)
        self.assertEqual(
            _side_gap_bucket("Away", favorite, None, state),
            "no_market",
        )

    def test_grades_submission_and_closing_markets_separately(self) -> None:
        past = {
            **_lean(),
            "event_id": "past",
            "submission_id": "telegram:123:455",
            "commence_time_utc": "2025-09-11T00:00:00+00:00",
            "submitted_at_utc": "2025-09-01T00:00:00+00:00",
        }
        history = [{
            "event_id": "espn-past",
            "kickoff_utc": "2025-09-11T00:00:00+00:00",
            "away_team": "San Francisco 49ers",
            "home_team": "Los Angeles Rams",
            "away_score": 20,
            "home_score": 24,
        }]
        snapshots = [{
            "captured_at": "2025-09-10T23:00:00+00:00",
            "event_id": "past",
            "bookmaker": past["bookmaker"],
            AWAY_SNAPSHOT_COLUMN: (
                "4.5,-110,170|nodata,nodata,nodata|nodata,nodata,nodata"
            ),
            HOME_SNAPSHOT_COLUMN: (
                "-4.5,-110,-190|nodata,nodata,nodata|nodata,nodata,nodata"
            ),
            TOTALS_SNAPSHOT_COLUMN: (
                "43.5,-110,-110|nodata,nodata,nodata|nodata,nodata,nodata"
            ),
        }]

        payload = build_ak_input(
            _game(),
            history,
            [_lean(), past],
            snapshots,
            ak_user_id="123",
        )

        side = payload["nfl_calibration"]["matching_side_bucket"]
        total = payload["nfl_calibration"]["matching_total_bucket"]
        self.assertEqual(side["ak_side_ats_at_submission"]["chronological_results"], "W")
        self.assertEqual(side["ak_side_ats_at_close"]["chronological_results"], "L")
        self.assertEqual(
            total["actual_total_results_at_submission"]["chronological_results"],
            "U",
        )
        self.assertEqual(
            total["actual_total_results_at_close"]["chronological_results"],
            "O",
        )

    def test_missing_total_does_not_discard_side_history(self) -> None:
        past = {
            **_lean(),
            "event_id": "past",
            "submission_id": "telegram:123:455",
            "commence_time_utc": "2025-09-11T00:00:00+00:00",
            "submitted_at_utc": "2025-09-01T00:00:00+00:00",
            LATEST_TOTALS_COLUMN: (
                "nodata,nodata,nodata|nodata,nodata,nodata|"
                "nodata,nodata,nodata"
            ),
        }
        history = [{
            "event_id": "espn-past",
            "kickoff_utc": "2025-09-11T00:00:00+00:00",
            "away_team": "San Francisco 49ers",
            "home_team": "Los Angeles Rams",
            "away_score": 20,
            "home_score": 24,
        }]

        payload = build_ak_input(
            _game(),
            history,
            [_lean(), past],
            ak_user_id="123",
        )

        calibration = payload["nfl_calibration"]
        self.assertEqual(calibration["side_eligible_predictions"], 1)
        self.assertEqual(calibration["total_eligible_predictions"], 0)
        self.assertEqual(
            calibration["matching_side_bucket"][
                "ak_side_ats_at_submission"
            ]["chronological_results"],
            "W",
        )


class AkGenerationTest(unittest.IsolatedAsyncioTestCase):
    async def test_generates_structured_side_and_total(self) -> None:
        response = {
            "predicted_winner": "Los Angeles Rams",
            "predicted_away_score": 23,
            "predicted_home_score": 26,
            "home_win_probability": 0.56,
            "expected_home_margin": 2.5,
            "side": {
                "selection": "Los Angeles Rams",
                "line": -3.5,
                "confidence_stars": 1,
                "evidence_ids": ["side_gap", "wnba_side_prior"],
                "counterargument_ids": ["nfl_side_bucket"],
            },
            "total": {
                "selection": "PASS",
                "line": None,
                "confidence_stars": 1,
                "evidence_ids": [],
                "counterargument_ids": ["nfl_total_bucket"],
            },
            "discarded_considerations": [
                "Injuries are unavailable in the supplied input."
            ],
        }

        async def create_fn(**_kwargs):
            return SimpleNamespace(
                content=[SimpleNamespace(text=json.dumps(response))]
            )

        row = await generate_opinion(
            expert_id="ak",
            game=_game(),
            history=[],
            leans=[_lean()],
            ak_user_id="123",
            store=MemoryStore(),
            create_fn=create_fn,
        )

        self.assertEqual(row["generation_status"], "valid")
        self.assertEqual(row["expert_id"], "ak")
        self.assertEqual(row["output_schema_version"], 5)
        self.assertEqual(row["pick_market"], "side_and_total")
        self.assertIn("Los Angeles Rams -3.5", row["pick_side"])
        self.assertEqual(
            json.loads(row["total_pick_json"])["selection"],
            "PASS",
        )
        self.assertIn(
            "WNBA-only total interpretation: this is an Under warning",
            row["full_opinion"],
        )
        self.assertIn(
            "NFL 3 to <9 -> WNBA 6 to <12",
            row["full_opinion"],
        )


if __name__ == "__main__":
    unittest.main()
