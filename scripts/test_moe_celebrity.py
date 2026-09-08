#!/usr/bin/env python3
"""Tests for the NFL Celebrity Consensus Expert."""

from __future__ import annotations

import json
import unittest
from types import SimpleNamespace

from celebrity_picks import build_celebrity_rows, parse_custom_pick_text
from moe import generate_opinion, load_expert
from moe_celebrity import build_celebrity_input
from nfl_lines import (
    LATEST_AWAY_COLUMN,
    LATEST_HOME_COLUMN,
    LATEST_TOTALS_COLUMN,
)


EVENT_ID = "patriots-seahawks"


def _game() -> dict:
    return {
        "event_id": EVENT_ID,
        "season": 2026,
        "week": 1,
        "commence_time_utc": "2026-09-10T00:20:00+00:00",
        "away_team": "New England Patriots",
        "home_team": "Seattle Seahawks",
        LATEST_AWAY_COLUMN: (
            "3.5,-118,155|nodata,nodata,nodata|nodata,nodata,nodata"
        ),
        LATEST_HOME_COLUMN: (
            "-3.5,-102,-177|nodata,nodata,nodata|nodata,nodata,nodata"
        ),
        LATEST_TOTALS_COLUMN: (
            "44.5,-110,-110|nodata,nodata,nodata|nodata,nodata,nodata"
        ),
    }


def _standard(
    *,
    submission_id: str,
    celebrity: str,
    submitted_at: str,
    market: str,
    side: str,
    line: float | str,
) -> dict:
    return build_celebrity_rows(
        submission={
            "submission_id": submission_id,
            "submitted_at_utc": submitted_at,
            "submitted_at_et": submitted_at,
            "event_id": EVENT_ID,
            "season": 2026,
            "week": 1,
            "commence_time_utc": _game()["commence_time_utc"],
            "commence_time_et": "2026-09-09T20:20:00-04:00",
            "away_team": _game()["away_team"],
            "home_team": _game()["home_team"],
            "period": "game",
            "market": market,
            "side": side,
            "latest_selected_line": line,
            "latest_selected_price": -110,
            "raw_pick_text": "Exact celebrity explanation.",
        },
        names=[celebrity],
    )[0]


def _current_rows() -> list[dict]:
    rows = [
        _standard(
            submission_id="bill-1",
            celebrity="Bill Simmons",
            submitted_at="2026-09-07T18:00:00+00:00",
            market="moneyline",
            side="Seattle Seahawks",
            line="",
        ),
        _standard(
            submission_id="sal-1",
            celebrity="Cousin Sal",
            submitted_at="2026-09-07T19:00:00+00:00",
            market="spread",
            side="Seattle Seahawks",
            line=-3.5,
        ),
        _standard(
            submission_id="sal-total",
            celebrity="Cousin Sal",
            submitted_at="2026-09-07T19:01:00+00:00",
            market="total",
            side="Under",
            line=44.5,
        ),
    ]
    custom = parse_custom_pick_text(
        "Subject: Drake Maye\n"
        "Market: Passing touchdowns\n"
        "Pick: Over 1.5\n"
        "Odds: -105\n"
        "Rationale: Expects red-zone success.",
        market_family="player_prop",
    )
    rows.extend(
        build_celebrity_rows(
            submission={
                "submission_id": "bill-prop",
                "submitted_at_utc": "2026-09-07T18:05:00+00:00",
                "submitted_at_et": "2026-09-07T14:05:00-04:00",
                "event_id": EVENT_ID,
                "season": 2026,
                "week": 1,
                "commence_time_utc": _game()["commence_time_utc"],
                "commence_time_et": "2026-09-09T20:20:00-04:00",
                "away_team": _game()["away_team"],
                "home_team": _game()["home_team"],
                "period": "game",
                **custom,
            },
            names=["Bill Simmons"],
        )
    )
    return rows


class MemoryStore:
    def __init__(self) -> None:
        self.rows: list[dict] = []

    def append(self, row: dict) -> None:
        self.rows.append(dict(row))

    def list(self, event_id: str | None = None) -> list[dict]:
        return list(self.rows)


class CelebrityInputTest(unittest.TestCase):
    def test_tracks_props_and_compatible_consensus(self) -> None:
        payload = build_celebrity_input(
            _game(),
            [],
            _current_rows(),
            [],
        )

        participation = payload["participation"]
        self.assertEqual(participation["celebrity_count"], 2)
        self.assertEqual(participation["active_bet_count"], 4)
        self.assertEqual(
            participation["side_consensus"]["label"],
            "unanimous",
        )
        self.assertEqual(
            participation["side_consensus"]["selection"],
            "Seattle Seahawks",
        )
        self.assertEqual(
            participation["total_consensus"]["label"],
            "single",
        )
        prop = next(
            row
            for row in payload["current_picks"]
            if row["market_family"] == "player_prop"
        )
        self.assertEqual(prop["subject"], "Drake Maye")
        self.assertEqual(prop["direction"], "Over")
        self.assertEqual(prop["raw_pick_text"].splitlines()[0], "Subject: Drake Maye")

    def test_latest_revision_wins_per_canonical_bet(self) -> None:
        rows = _current_rows()
        rows.append(
            _standard(
                submission_id="bill-2",
                celebrity="Bill Simmons",
                submitted_at="2026-09-07T20:00:00+00:00",
                market="moneyline",
                side="New England Patriots",
                line="",
            )
        )

        payload = build_celebrity_input(_game(), [], rows, [])

        self.assertEqual(
            payload["participation"]["side_consensus"]["label"],
            "split",
        )
        self.assertIsNone(
            payload["participation"]["side_consensus"]["selection"]
        )

    def test_post_kickoff_revision_does_not_erase_pregame_pick(self) -> None:
        rows = _current_rows()
        rows.append(
            _standard(
                submission_id="bill-postgame",
                celebrity="Bill Simmons",
                submitted_at="2026-09-10T01:00:00+00:00",
                market="moneyline",
                side="New England Patriots",
                line="",
            )
        )

        payload = build_celebrity_input(_game(), [], rows, [])

        self.assertEqual(
            payload["participation"]["side_consensus"]["label"],
            "unanimous",
        )
        self.assertEqual(
            payload["participation"]["side_consensus"]["selection"],
            "Seattle Seahawks",
        )

    def test_calibrates_matching_participation_and_consensus(self) -> None:
        past_rows = []
        for row in _current_rows():
            past = dict(row)
            past["event_id"] = "patriots-seahawks-previous"
            past["commence_time_utc"] = "2026-09-08T00:20:00+00:00"
            past_rows.append(past)
        history = [
            {
                "event_id": "patriots-seahawks-previous",
                "kickoff_utc": "2026-09-08T00:20:00+00:00",
                "away_team": _game()["away_team"],
                "home_team": _game()["home_team"],
                "away_score": 17,
                "home_score": 24,
                "completed": True,
            }
        ]

        payload = build_celebrity_input(
            _game(),
            history,
            _current_rows() + past_rows,
            [],
        )

        cohorts = payload["nfl_calibration"][
            "matching_participation_consensus"
        ]
        self.assertEqual(cohorts["side"]["participant_bucket"], "2")
        self.assertEqual(cohorts["side"]["consensus_label"], "unanimous")
        self.assertEqual(cohorts["side"]["wins"], 1)
        self.assertEqual(cohorts["side"]["games"], 1)
        self.assertEqual(cohorts["total"]["consensus_label"], "single")
        self.assertEqual(cohorts["total"]["wins"], 1)
        self.assertEqual(cohorts["total"]["games"], 1)

    def test_mixed_side_markets_need_same_verdict_for_group_record(self) -> None:
        past_rows = []
        for row in _current_rows():
            past = dict(row)
            past["event_id"] = "patriots-seahawks-close-win"
            past["commence_time_utc"] = "2026-09-08T00:20:00+00:00"
            past_rows.append(past)
        history = [
            {
                "event_id": "patriots-seahawks-close-win",
                "kickoff_utc": "2026-09-08T00:20:00+00:00",
                "away_team": _game()["away_team"],
                "home_team": _game()["home_team"],
                "away_score": 17,
                "home_score": 20,
                "completed": True,
            }
        ]

        payload = build_celebrity_input(
            _game(),
            history,
            _current_rows() + past_rows,
            [],
        )

        cohorts = payload["nfl_calibration"][
            "matching_participation_consensus"
        ]
        self.assertEqual(cohorts["side"]["games"], 0)
        self.assertEqual(cohorts["total"]["wins"], 1)


class CelebrityGenerationTest(unittest.IsolatedAsyncioTestCase):
    async def test_generates_optional_side_and_total_legs(self) -> None:
        response = {
            "predicted_winner": "Seattle Seahawks",
            "predicted_away_score": 20,
            "predicted_home_score": 23,
            "home_win_probability": 0.58,
            "expected_home_margin": 3.0,
            "side": {
                "selection": "Seattle Seahawks",
                "line": -3.5,
                "confidence_stars": 2,
                "evidence_ids": ["current_side_consensus"],
                "counterargument_ids": ["current_participation"],
            },
            "total": {
                "selection": "Under",
                "line": 44.5,
                "confidence_stars": 2,
                "evidence_ids": ["current_total_consensus"],
                "counterargument_ids": ["current_participation"],
            },
            "discarded_considerations": [
                "The player prop does not directly support either game leg."
            ],
        }

        async def create_fn(**_kwargs):
            return SimpleNamespace(
                content=[SimpleNamespace(text=json.dumps(response))]
            )

        store = MemoryStore()
        row = await generate_opinion(
            expert_id="celebrity",
            game=_game(),
            history=[],
            leans=[],
            celebrity_picks=_current_rows(),
            store=store,
            create_fn=create_fn,
        )

        self.assertEqual(row["generation_status"], "valid")
        self.assertEqual(row["pick_market"], "side_and_total")
        self.assertEqual(
            json.loads(row["side_pick_json"])["selection"],
            "Seattle Seahawks",
        )
        self.assertEqual(
            json.loads(row["total_pick_json"])["selection"],
            "Under",
        )

    def test_expert_configuration(self) -> None:
        expert = load_expert("celebrity")

        self.assertEqual(expert["input_profile"], "celebrity_consensus")
        self.assertEqual(expert["allowed_models"], ["claude-opus-4-8"])
        self.assertTrue(expert["committee_optional"])
        self.assertEqual(expert["markets"], ["side", "total"])


if __name__ == "__main__":
    unittest.main()
