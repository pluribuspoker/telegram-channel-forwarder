#!/usr/bin/env python3
"""Tests for the NFL Celebrity Expert."""

from __future__ import annotations

import json
import unittest
from types import SimpleNamespace

from celebrity_picks import build_celebrity_rows, parse_custom_pick_text
from celebrity_grades import build_celebrity_grade_rows
from moe import generate_opinion, load_expert
from moe_celebrity import _conditional_lift, _record, build_celebrity_input
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
    def test_standard_pick_uses_user_line_and_retains_betonline(self) -> None:
        row = build_celebrity_rows(
            submission={
                "submission_id": "anthony-1",
                "submitted_at_utc": "2026-09-09T18:00:00+00:00",
                "event_id": EVENT_ID,
                "season": 2026,
                "week": 1,
                "commence_time_utc": _game()["commence_time_utc"],
                "away_team": _game()["away_team"],
                "home_team": _game()["home_team"],
                "period": "game",
                "market": "spread",
                "side": "New England Patriots",
                "latest_selected_line": 3,
                "latest_selected_price": -105,
                "user_selected_line": 3.5,
                "user_selected_price": -110,
                "user_terms_source": "entered",
            },
            names=["Anthony Dabbundo"],
        )[0]

        self.assertEqual(row["line"], 3.5)
        self.assertEqual(row["price"], -110)
        self.assertEqual(row["betonline_line"], 3)
        self.assertEqual(row["betonline_price"], -105)
        self.assertEqual(row["line_source"], "entered")

    def test_entered_line_does_not_inherit_unaccepted_betonline_price(
        self,
    ) -> None:
        row = build_celebrity_rows(
            submission={
                "submission_id": "anthony-2",
                "period": "game",
                "market": "spread",
                "side": "New England Patriots",
                "latest_selected_line": 3,
                "latest_selected_price": -105,
                "user_selected_line": 3.5,
                "user_selected_price": "nodata",
                "user_terms_source": "entered",
            },
            names=["Anthony Dabbundo"],
        )[0]

        self.assertEqual(row["line"], 3.5)
        self.assertEqual(row["price"], "nodata")
        self.assertEqual(row["betonline_price"], -105)

    def test_tracks_props_and_side_distribution(self) -> None:
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
            participation["side_distribution"]["label"],
            "unanimous",
        )
        self.assertEqual(
            participation["side_distribution"]["selection"],
            "Seattle Seahawks",
        )
        self.assertEqual(
            participation["total_distribution"]["label"],
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
            payload["participation"]["side_distribution"]["label"],
            "split",
        )
        self.assertIsNone(
            payload["participation"]["side_distribution"]["selection"]
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
            payload["participation"]["side_distribution"]["label"],
            "unanimous",
        )
        self.assertEqual(
            payload["participation"]["side_distribution"]["selection"],
            "Seattle Seahawks",
        )

    def test_calibrates_exact_identity_permutation(self) -> None:
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

        permutations = payload["nfl_calibration"][
            "exact_current_permutation"
        ]
        self.assertEqual(
            permutations["side"]["pattern"],
            {"Bill Simmons": "home", "Cousin Sal": "home"},
        )
        self.assertEqual(permutations["side"]["matching_games"], 1)
        self.assertEqual(
            permutations["side"]["records_by_celebrity"]["Bill Simmons"][
                "wins"
            ],
            1,
        )
        self.assertEqual(
            permutations["total"]["records_by_celebrity"]["Cousin Sal"][
                "wins"
            ],
            1,
        )

    def test_exact_permutation_keeps_market_specific_verdicts(self) -> None:
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

        side = payload["nfl_calibration"]["exact_current_permutation"][
            "side"
        ]
        self.assertEqual(
            side["records_by_celebrity"]["Bill Simmons"]["wins"],
            1,
        )
        self.assertEqual(
            side["records_by_celebrity"]["Cousin Sal"]["losses"],
            1,
        )
        calibration = payload["nfl_calibration"]
        self.assertEqual(
            calibration["individual"]["Bill Simmons"]["moneyline"]["wins"],
            1,
        )
        self.assertEqual(
            calibration["individual"]["Bill Simmons"]["spread"]["games"],
            0,
        )
        self.assertEqual(
            calibration["individual"]["Cousin Sal"]["spread"]["losses"],
            1,
        )
        self.assertEqual(
            calibration["exact_current_permutation"]["spread"]["pattern"],
            {"Cousin Sal": "home_favorite"},
        )

    def test_pairwise_lift_uses_market_baseline_and_sample_counts(self) -> None:
        current = _current_rows()
        bill_spread = _standard(
            submission_id="bill-spread",
            celebrity="Bill Simmons",
            submitted_at="2026-09-07T18:10:00+00:00",
            market="spread",
            side="Seattle Seahawks",
            line=-3.5,
        )
        current.append(bill_spread)

        agreement_rows = []
        for row in current:
            past = dict(row)
            past["event_id"] = "spread-agreement"
            past["commence_time_utc"] = "2026-09-08T00:20:00+00:00"
            agreement_rows.append(past)
        bill_loss = dict(bill_spread)
        bill_loss["event_id"] = "bill-baseline-loss"
        bill_loss["commence_time_utc"] = "2026-09-09T00:20:00+00:00"
        history = [
            {
                "event_id": "spread-agreement",
                "kickoff_utc": "2026-09-08T00:20:00+00:00",
                "away_team": _game()["away_team"],
                "home_team": _game()["home_team"],
                "away_score": 17,
                "home_score": 24,
                "completed": True,
            },
            {
                "event_id": "bill-baseline-loss",
                "kickoff_utc": "2026-09-09T00:20:00+00:00",
                "away_team": _game()["away_team"],
                "home_team": _game()["home_team"],
                "away_score": 20,
                "home_score": 21,
                "completed": True,
            },
        ]

        payload = build_celebrity_input(
            _game(),
            history,
            current + agreement_rows + [bill_loss],
            [],
        )

        spread = payload["nfl_calibration"]["pairwise"][0]["spread"]
        bill_lift = spread["agreement_lift_by_celebrity"]["Bill Simmons"]
        self.assertEqual(spread["current_relation"], "agreement")
        self.assertEqual(spread["agreement_record"]["games"], 1)
        self.assertEqual(bill_lift["baseline_games"], 2)
        self.assertEqual(bill_lift["baseline_decisions"], 2)
        self.assertEqual(bill_lift["conditional_games"], 1)
        self.assertEqual(bill_lift["lift"], 0.5)
        pair_item = next(
            item
            for item in payload["evidence_catalog"]
            if item["id"] == "pair_01_spread"
        )
        self.assertTrue(pair_item["supporting_allowed"])

    def test_agreement_tracks_each_celebritys_actual_line(self) -> None:
        current = _current_rows()
        current.append(
            _standard(
                submission_id="bill-spread",
                celebrity="Bill Simmons",
                submitted_at="2026-09-07T18:10:00+00:00",
                market="spread",
                side="Seattle Seahawks",
                line=-3.5,
            )
        )
        past_rows = []
        for row in current:
            past = dict(row)
            past["event_id"] = "different-spread-lines"
            past["commence_time_utc"] = "2026-09-08T00:20:00+00:00"
            if (
                past["celebrity_name"] == "Bill Simmons"
                and past["market"] == "spread"
            ):
                past["line"] = -2
            past_rows.append(past)
        history = [
            {
                "event_id": "different-spread-lines",
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
            current + past_rows,
            [],
        )

        spread = payload["nfl_calibration"]["pairwise"][0]["spread"]
        self.assertEqual(spread["agreement_record"]["games"], 0)
        self.assertEqual(spread["first_record_when_agreeing"]["wins"], 1)
        self.assertEqual(spread["second_record_when_agreeing"]["losses"], 1)
        self.assertEqual(
            spread["agreement_lift_by_celebrity"]["Bill Simmons"][
                "conditional_decisions"
            ],
            1,
        )
        self.assertEqual(
            spread["agreement_lift_by_celebrity"]["Cousin Sal"][
                "conditional_decisions"
            ],
            1,
        )

    def test_push_only_record_cannot_support_a_recommendation(self) -> None:
        bill_moneyline = next(
            row
            for row in _current_rows()
            if row["celebrity_name"] == "Bill Simmons"
            and row["market"] == "moneyline"
        )
        past = dict(bill_moneyline)
        past["event_id"] = "moneyline-push"
        past["commence_time_utc"] = "2026-09-08T00:20:00+00:00"
        history = [
            {
                "event_id": "moneyline-push",
                "kickoff_utc": "2026-09-08T00:20:00+00:00",
                "away_team": _game()["away_team"],
                "home_team": _game()["home_team"],
                "away_score": 20,
                "home_score": 20,
                "completed": True,
            }
        ]

        payload = build_celebrity_input(
            _game(),
            history,
            _current_rows() + [past],
            [],
        )

        item = next(
            catalog_item
            for catalog_item in payload["evidence_catalog"]
            if catalog_item["id"] == "individual_01_moneyline"
        )
        self.assertFalse(item["supporting_allowed"])
        self.assertEqual(payload["confidence_cap"], 2)

    def test_pair_support_matches_the_current_relation(self) -> None:
        past_rows = []
        for row in _current_rows():
            past = dict(row)
            past["event_id"] = "historical-disagreement"
            past["commence_time_utc"] = "2026-09-08T00:20:00+00:00"
            if (
                past["celebrity_name"] == "Bill Simmons"
                and past["market"] == "moneyline"
            ):
                past["direction"] = "New England Patriots"
                past["side"] = "New England Patriots"
            past_rows.append(past)
        history = [
            {
                "event_id": "historical-disagreement",
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

        pair = payload["nfl_calibration"]["pairwise"][0]["side"]
        self.assertEqual(pair["current_relation"], "agreement")
        self.assertEqual(pair["agreement_record"]["decisions"], 0)
        self.assertGreater(
            pair["first_record_when_disagreeing"]["decisions"],
            0,
        )
        item = next(
            catalog_item
            for catalog_item in payload["evidence_catalog"]
            if catalog_item["id"] == "pair_01_side"
        )
        self.assertFalse(item["supporting_allowed"])

    def test_zero_decision_records_have_no_rate_or_lift(self) -> None:
        push_only = _record(["P"])
        empty = _record([])

        self.assertEqual(push_only["games"], 1)
        self.assertEqual(push_only["decisions"], 0)
        self.assertIsNone(push_only["win_rate"])
        self.assertIsNone(_conditional_lift(push_only, empty)["lift"])

    def test_persisted_grades_reproduce_dynamic_calibration(self) -> None:
        past_rows = []
        for row in _current_rows():
            past = dict(row)
            past["event_id"] = "persisted-grade-game"
            past["commence_time_utc"] = "2026-09-08T00:20:00+00:00"
            past_rows.append(past)
        history = [
            {
                "event_id": "persisted-grade-game",
                "kickoff_utc": "2026-09-08T00:20:00+00:00",
                "away_team": _game()["away_team"],
                "home_team": _game()["home_team"],
                "away_score": 17,
                "home_score": 24,
                "completed": True,
            }
        ]
        all_rows = _current_rows() + past_rows
        grades = build_celebrity_grade_rows(all_rows, [], history)

        dynamic = build_celebrity_input(_game(), history, all_rows, [])
        persisted = build_celebrity_input(
            _game(),
            history,
            all_rows,
            [],
            grades,
        )

        self.assertEqual(persisted, dynamic)

    def test_persisted_grade_disagreement_fails_closed(self) -> None:
        past = dict(_current_rows()[0])
        past["event_id"] = "bad-persisted-grade"
        past["commence_time_utc"] = "2026-09-08T00:20:00+00:00"
        history = [
            {
                "event_id": "bad-persisted-grade",
                "kickoff_utc": "2026-09-08T00:20:00+00:00",
                "away_team": _game()["away_team"],
                "home_team": _game()["home_team"],
                "away_score": 17,
                "home_score": 24,
                "completed": True,
            }
        ]
        grades = build_celebrity_grade_rows([past], [], history)
        grades[0]["result"] = "L"

        with self.assertRaisesRegex(
            RuntimeError,
            "Persisted celebrity grade disagrees",
        ):
            build_celebrity_input(
                _game(),
                history,
                _current_rows() + [past],
                [],
                grades,
            )

    def test_pairwise_disagreement_tracks_each_celebrity(self) -> None:
        current = _current_rows()
        current.append(
            _standard(
                submission_id="bill-away",
                celebrity="Bill Simmons",
                submitted_at="2026-09-07T20:00:00+00:00",
                market="moneyline",
                side="New England Patriots",
                line="",
            )
        )
        past_rows = []
        for row in current:
            past = dict(row)
            past["event_id"] = "patriots-seahawks-disagreement"
            past["commence_time_utc"] = "2026-09-08T00:20:00+00:00"
            past_rows.append(past)
        history = [
            {
                "event_id": "patriots-seahawks-disagreement",
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
            current + past_rows,
            [],
        )

        pair = payload["nfl_calibration"]["pairwise"][0]["side"]
        self.assertEqual(pair["current_relation"], "disagreement")
        self.assertEqual(pair["disagreement_games"], 1)
        self.assertNotIn(
            "chronological_results",
            pair["first_record_when_disagreeing"],
        )
        self.assertEqual(pair["first_record_when_disagreeing"]["losses"], 1)
        self.assertEqual(pair["second_record_when_disagreeing"]["wins"], 1)


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
                "evidence_ids": ["current_pick_02"],
                "counterargument_ids": ["current_participation"],
            },
            "total": {
                "selection": "Under",
                "line": 44.5,
                "confidence_stars": 2,
                "evidence_ids": ["current_pick_04"],
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

        self.assertEqual(expert["name"], "Celebrity Expert")
        self.assertEqual(expert["version"], 3)
        self.assertEqual(expert["prompt_version"], 3)
        self.assertEqual(expert["prompt"], "prompts/celebrity/v3.md")
        self.assertEqual(expert["input_profile"], "celebrity_patterns")
        self.assertEqual(expert["allowed_models"], ["claude-opus-4-8"])
        self.assertTrue(expert["committee_optional"])
        self.assertEqual(expert["markets"], ["side", "total"])


if __name__ == "__main__":
    unittest.main()
