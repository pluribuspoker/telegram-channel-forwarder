#!/usr/bin/env python3
"""Tests for the God Expert aggregator (rules arm, judge arm, shared policy)."""

from __future__ import annotations

import json
import math
import statistics
import unittest
from pathlib import Path
from types import SimpleNamespace

from moe import (
    OPINION_HEADERS,
    generate_opinion,
    load_expert,
    opinion_output_sha256,
    validate_opinion,
)
from moe_god import (
    AGGREGATOR_PROFILE,
    DEFAULT_POLICY,
    DETERMINISTIC_BACKEND,
    DETERMINISTIC_MODEL,
    GRADE_HEADERS,
    JUDGE_REQUEST_PROFILE,
    MEAN_OF_ARMS_ID,
    aggregator_policy,
    american_to_implied,
    apply_policy,
    arm_pairs,
    build_aggregator_input,
    build_judge_request,
    build_market_block,
    build_scoreboard,
    canonical_json,
    compare_legs,
    cover_probability,
    disagreement_report,
    fair_pair,
    format_disagreement_report,
    grade_all,
    grade_opinion_row,
    hedge_weights,
    ledger_row,
    load_registry,
    mean_of_arms_results,
    normalize_aggregator_opinion,
    over_probability,
    rules_arm_response,
    select_voice_rows,
    sha256_text,
)
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

EVENT_ID = "8c94552d022acec4a0458d70c19d3da9"
AWAY = "New England Patriots"
HOME = "Seattle Seahawks"
KICKOFF = "2026-09-10T00:20:00+00:00"


class MemoryStore:
    def __init__(self) -> None:
        self.rows: list[dict] = []

    def append(self, row: dict) -> None:
        self.rows.append(dict(row))

    def list(self, event_id: str | None = None) -> list[dict]:
        if event_id is None:
            return list(self.rows)
        return [
            row for row in self.rows if str(row.get("event_id")) == str(event_id)
        ]


def _game(
    *,
    event_id: str = EVENT_ID,
    away: str = AWAY,
    home: str = HOME,
    kickoff: str = KICKOFF,
    week: int | str = 1,
) -> dict:
    opening_away = "3.5,-110,165|2.5,-105,135|0.5,-135,120"
    opening_home = "-3.5,-110,-190|-2.5,-115,-155|-0.5,115,-140"
    opening_totals = "44,-110,-110|21.5,-110,-110|7.5,-105,-115"
    latest_away = "3.5,-120,158|2.5,-105,135|0.5,-135,120"
    latest_home = "-3.5,100,-181|-2.5,-115,-155|-0.5,115,-140"
    latest_totals = "44.5,-105,-115|21.5,-110,-110|7.5,-105,-115"
    return {
        "event_id": event_id,
        "season": 2026,
        "season_type": "regular",
        "week": week,
        "status": "upcoming",
        "commence_time_utc": kickoff,
        "commence_time_et": "2026-09-09T20:20:00-04:00",
        "away_team": away,
        "home_team": home,
        "bookmaker": "BetOnline.ag",
        "opening_captured_at": "2026-08-04T23:25:37Z",
        "latest_captured_at": "2026-09-06T20:35:17Z",
        OPENING_AWAY_COLUMN: opening_away,
        OPENING_HOME_COLUMN: opening_home,
        OPENING_TOTALS_COLUMN: opening_totals,
        LATEST_AWAY_COLUMN: latest_away,
        LATEST_HOME_COLUMN: latest_home,
        LATEST_TOTALS_COLUMN: latest_totals,
    }


def _opinion(
    expert_id: str,
    *,
    model: str,
    probability: float,
    margin: float,
    away_score: int,
    home_score: int,
    stars: int = 3,
    generated_at: str = "2026-09-05T02:00:00+00:00",
    opinion_id: str | None = None,
    event_id: str = EVENT_ID,
    away: str = AWAY,
    home: str = HOME,
    kickoff: str = KICKOFF,
    side_leg: dict | None = None,
    total_leg: dict | None = None,
    review_status: str = "approved",
    factors: list | None = None,
) -> dict:
    winner = home if probability > 0.5 else away
    row = {header: "" for header in OPINION_HEADERS}
    row.update(
        {
            "opinion_id": opinion_id or f"{expert_id}-{model}-{generated_at}",
            "generated_at_utc": generated_at,
            "event_id": event_id,
            "season": 2026,
            "week": 1,
            "commence_time_utc": kickoff,
            "away_team": away,
            "home_team": home,
            "expert_id": expert_id,
            "expert_name": f"{expert_id.title()} Expert",
            "expert_mode": "agent",
            "expert_version": 9,
            "prompt_version": 3,
            "model": model,
            "generation_backend": "agent_runtime",
            "generation_effort": "max",
            "predicted_winner": winner,
            "home_win_probability": probability,
            "expected_home_margin": margin,
            "predicted_away_score": away_score,
            "predicted_home_score": home_score,
            "confidence_stars": stars,
            "pick_market": "straight_up",
            "pick_side": winner,
            "thesis": f"{expert_id} leans {winner}.",
            "supporting_factors_json": json.dumps(
                factors
                if factors is not None
                else [f"{expert_id} factor one", f"{expert_id} factor two"]
            ),
            "counterarguments_json": json.dumps([f"{expert_id} counter"]),
            "no_signal_factors_json": json.dumps([]),
            "discarded_considerations_json": json.dumps(["Injuries unavailable."]),
            "full_opinion": f"{expert_id} full opinion text",
            "generation_status": "valid",
            "review_status": review_status,
        }
    )
    if side_leg is not None or total_leg is not None:
        row["pick_market"] = "side_and_total"
        row["side_pick_json"] = json.dumps(side_leg or {"selection": "PASS", "line": None, "confidence_stars": 1})
        row["total_pick_json"] = json.dumps(total_leg or {"selection": "PASS", "line": None, "confidence_stars": 1})
    digest = opinion_output_sha256(row)
    row["output_sha256"] = digest
    row["approved_output_sha256"] = digest if review_status == "approved" else ""
    return row


def _committee() -> list[dict]:
    return [
        _opinion("schedule", model="claude-opus-4-8", probability=0.66, margin=6, away_score=20, home_score=26),
        # A newer Fable run of the same expert must NOT displace the default-model row.
        _opinion(
            "schedule",
            model="claude-fable-5",
            probability=0.72,
            margin=10,
            away_score=17,
            home_score=27,
            generated_at="2026-09-06T02:00:00+00:00",
        ),
        _opinion("divisional", model="claude-opus-4-8", probability=0.70, margin=5, away_score=19, home_score=24),
        _opinion("win_total", model="claude-opus-4-8", probability=0.57, margin=1, away_score=23, home_score=24, stars=2),
        _opinion(
            "ak",
            model="claude-opus-4-8",
            probability=0.62,
            margin=6,
            away_score=21,
            home_score=27,
            stars=1,
            side_leg={"selection": "PASS", "line": None, "confidence_stars": 1},
            total_leg={"selection": "Under", "line": 44.5, "confidence_stars": 1},
        ),
        # Pending rows never count.
        _opinion(
            "divisional",
            model="claude-opus-4-8",
            probability=0.90,
            margin=20,
            away_score=10,
            home_score=30,
            generated_at="2026-09-07T02:00:00+00:00",
            review_status="pending",
        ),
    ]


def _snapshot(event_id: str, captured_at: str, *, away: str, home: str, totals: str) -> dict:
    return {
        "captured_at": captured_at,
        "event_id": event_id,
        "commence_time_utc": KICKOFF,
        "away_team": AWAY,
        "home_team": HOME,
        "bookmaker": "BetOnline.ag",
        AWAY_SNAPSHOT_COLUMN: away,
        HOME_SNAPSHOT_COLUMN: home,
        TOTALS_SNAPSHOT_COLUMN: totals,
    }


class ArithmeticTests(unittest.TestCase):
    def test_devig_matches_playbook_example(self) -> None:
        self.assertAlmostEqual(american_to_implied(-181), 181 / 281, places=6)
        self.assertAlmostEqual(american_to_implied(158), 100 / 258, places=6)
        fair_home, fair_away, hold = fair_pair(-181, 158)
        self.assertAlmostEqual(fair_home, 0.6243, places=3)
        self.assertAlmostEqual(fair_home + fair_away, 1.0, places=9)
        self.assertGreater(hold, 0.02)
        with self.assertRaises(ValueError):
            american_to_implied(50)

    def test_sigma_conversions(self) -> None:
        self.assertAlmostEqual(cover_probability(5, -3.5, 13.5), 0.5443, places=3)
        self.assertAlmostEqual(cover_probability(3.5, -3.5, 13.5), 0.5, places=9)
        self.assertAlmostEqual(over_probability(46, 44.5, 13.5), 0.5443, places=3)
        self.assertLess(over_probability(40, 44.5, 13.5), 0.5)

    def test_market_block_and_movement(self) -> None:
        market = build_market_block(_game())
        self.assertAlmostEqual(market["fair"]["home_ml"], 0.6243, places=3)
        self.assertEqual(market["market_expectation"], {"home_margin": 3.5, "total": 44.5})
        self.assertEqual(market["movement_since_open"]["total"], 0.5)
        self.assertEqual(market["movement_since_open"]["home_spread"], 0.0)
        self.assertEqual(market["implied_totals"], {"away": 20.5, "home": 24.0})


class PolicyTests(unittest.TestCase):
    def setUp(self) -> None:
        self.policy = aggregator_policy(load_registry())
        self.market = build_market_block(_game())

    def _legs(self, probability: float, margin: float, total: float) -> dict:
        return apply_policy(
            home_win_probability=probability,
            expected_home_margin=margin,
            projected_total=total,
            market=self.market,
            policy=self.policy,
            away_team=AWAY,
            home_team=HOME,
        )

    def test_small_edges_pass(self) -> None:
        # Margin equal to the spread: p(cover) = 0.5 against a fair 0.478 on
        # the +100 home side is a 2.2% edge, under the 3% bar. Total 45 vs
        # 44.5 is a 2.6% edge, also under the bar.
        legs = self._legs(0.60, 3.5, 45.0)
        self.assertLess(abs(legs["side"]["edge"]), self.policy["edge_threshold"])
        self.assertEqual(legs["side"]["selection"], "PASS")
        self.assertEqual(legs["total"]["selection"], "PASS")
        self.assertEqual(legs["side"]["confidence_stars"], 1)
        self.assertEqual(legs["side"]["stake_units"], 0.0)

    def test_large_edge_bets_with_stars_and_kelly(self) -> None:
        legs = self._legs(0.80, 9.0, 52.0)
        side, total = legs["side"], legs["total"]
        self.assertEqual(side["selection"], HOME)
        self.assertEqual(side["line"], -3.5)
        self.assertEqual(side["price"], 100)
        self.assertGreaterEqual(side["edge"], self.policy["edge_threshold"])
        self.assertEqual(total["selection"], "Over")
        self.assertEqual(total["line"], 44.5)
        self.assertGreater(side["stake_units"], 0)
        self.assertLessEqual(side["stake_fraction"], self.policy["max_stake_fraction"])
        # ¼-Kelly at even money: (p*1 - q) / 1 * 0.25 with p = P(home covers)
        p = side["probability"]
        self.assertAlmostEqual(side["stake_fraction"], min(0.05, 0.25 * (2 * p - 1)), places=3)
        self.assertEqual(side["confidence_stars"], sum(1 for t in self.policy["star_edges"] if side["edge"] >= t))

    def test_away_side_and_under(self) -> None:
        legs = self._legs(0.30, -6.0, 36.0)
        self.assertEqual(legs["side"]["selection"], AWAY)
        self.assertEqual(legs["side"]["line"], 3.5)
        self.assertEqual(legs["total"]["selection"], "Under")


class VoiceSelectionTests(unittest.TestCase):
    def test_default_model_row_wins_over_newer_other_model(self) -> None:
        registry = load_registry()
        policy = aggregator_policy(registry)
        selected = select_voice_rows(_committee(), event_id=EVENT_ID, registry=registry, policy=policy)
        by_expert = {expert_id: (row, rule) for expert_id, _config, row, rule in selected}
        self.assertEqual(set(by_expert), {"ak", "divisional", "schedule", "win_total"})
        self.assertEqual(by_expert["schedule"][0]["model"], "claude-opus-4-8")
        self.assertEqual(by_expert["schedule"][1], "default_model")
        self.assertEqual(by_expert["divisional"][0]["home_win_probability"], 0.70)

    def test_fallback_to_any_model_is_recorded(self) -> None:
        registry = load_registry()
        policy = aggregator_policy(registry)
        rows = [
            _opinion("schedule", model="claude-haiku-4-5", probability=0.6, margin=3, away_score=20, home_score=23)
        ]
        selected = select_voice_rows(rows, event_id=EVENT_ID, registry=registry, policy=policy)
        self.assertEqual([(item[0], item[3]) for item in selected], [("schedule", "latest_any_model")])
        policy["voice_fallback"] = "skip"
        self.assertEqual(select_voice_rows(rows, event_id=EVENT_ID, registry=registry, policy=policy), [])

    def test_aggregator_rows_are_never_voices(self) -> None:
        registry = load_registry()
        policy = aggregator_policy(registry)
        rows = _committee() + [
            _opinion("god_rules", model=DETERMINISTIC_MODEL, probability=0.6, margin=3, away_score=20, home_score=23)
        ]
        selected = select_voice_rows(rows, event_id=EVENT_ID, registry=registry, policy=policy)
        self.assertNotIn("god_rules", {item[0] for item in selected})


class InputTests(unittest.TestCase):
    def setUp(self) -> None:
        self.registry = load_registry()
        self.policy = aggregator_policy(self.registry)
        self.payload = build_aggregator_input(
            _game(),
            approved_opinions=_committee(),
            finals=[],
            snapshots=[],
            registry=self.registry,
            policy=self.policy,
        )

    def test_feature_block_arithmetic(self) -> None:
        feature = self.payload["feature_block"]
        self.assertEqual(feature["n_voices"], 4)
        self.assertFalse(feature["weights_active"])
        expected_pool = (0.66 + 0.70 + 0.57 + 0.62) / 4
        self.assertAlmostEqual(feature["pool"]["home_win_probability"], expected_pool, places=4)
        fair_home = self.payload["market"]["fair"]["home_ml"]
        self.assertAlmostEqual(
            feature["shrunk"]["home_win_probability"], 0.5 * expected_pool + 0.5 * fair_home, places=4
        )
        self.assertAlmostEqual(feature["pool"]["expected_home_margin"], 4.5, places=6)
        self.assertEqual(feature["dispersion"]["home_winner_votes"], 4)
        voices = {voice["voice_id"]: voice for voice in self.payload["voices"]}
        self.assertEqual(voices["ak"]["legs"]["total"]["selection"], "Under")
        self.assertEqual(voices["schedule"]["selection_rule"], "default_model")
        self.assertEqual(self.payload["scoreboard"]["resolved_games"], 0)
        self.assertEqual(self.payload["input_profile"], AGGREGATOR_PROFILE)

    def test_judge_request_is_masked_and_deterministic(self) -> None:
        request = build_judge_request(self.payload)
        rendered = json.dumps(request)
        for leak in ("Schedule Expert", "claude-opus-4-8", "opinion_id", "expert_id", '"ak"', "Divisional"):
            self.assertNotIn(leak, rendered)
        self.assertEqual(request["input_profile"], JUDGE_REQUEST_PROFILE)
        self.assertEqual([voice["label"] for voice in request["voices"]], ["Voice A", "Voice B", "Voice C", "Voice D"])
        self.assertEqual(set(request["feature_block"]["weights"]), {"Voice A", "Voice B", "Voice C", "Voice D"})
        again = build_aggregator_input(
            _game(), approved_opinions=_committee(), finals=[], snapshots=[], registry=self.registry, policy=self.policy
        )
        self.assertEqual(again["judge_view"], self.payload["judge_view"])
        self.assertEqual(build_judge_request(again), request)
        # The label map is not exposed, but every voice is reachable through it.
        self.assertEqual(set(self.payload["judge_view"]["labels"].values()), {"ak", "divisional", "schedule", "win_total"})

    def test_factor_lists_are_capped(self) -> None:
        long_factors = [f"factor {index} " + "x" * 300 for index in range(12)]
        rows = [
            _opinion("schedule", model="claude-opus-4-8", probability=0.6, margin=3, away_score=20, home_score=23, factors=long_factors)
        ]
        payload = build_aggregator_input(
            _game(), approved_opinions=rows, finals=[], snapshots=[], registry=self.registry, policy=self.policy
        )
        factors = payload["voices"][0]["supporting_factors"]
        self.assertEqual(len(factors["items"]), self.policy["factor_limit"])
        self.assertTrue(factors["truncated"])
        self.assertEqual(factors["total"], 12)
        self.assertTrue(all(len(item) <= self.policy["factor_chars"] for item in factors["items"]))


class RulesArmTests(unittest.TestCase):
    def test_rules_response_normalizes_and_validates(self) -> None:
        registry = load_registry()
        policy = aggregator_policy(registry)
        payload = build_aggregator_input(
            _game(), approved_opinions=_committee(), finals=[], snapshots=[], registry=registry, policy=policy
        )
        response = rules_arm_response(payload)
        self.assertEqual(response["home_win_probability"], payload["feature_block"]["shrunk"]["home_win_probability"])
        opinion = normalize_aggregator_opinion(response, payload, expert=load_expert("god_rules"))
        validate_opinion(opinion, away_team=AWAY, home_team=HOME, schedule_input=payload)
        self.assertEqual(opinion["pick_market"], "side_and_total")
        self.assertEqual(opinion["predicted_winner"], HOME)
        self.assertLessEqual(len(opinion["thesis"]), 500)
        for section in ("Market", "Voices", "Blend", "Side pick", "Total pick", "Why", "Conclusion"):
            self.assertIn(section, opinion["full_opinion"])
        self.assertIn("Schedule Expert", opinion["full_opinion"])
        summary = json.loads(opinion["calibration_summary_json"])
        self.assertEqual(summary["arm"], "rules")
        self.assertIsNone(summary["judge_labels"])
        self.assertEqual(set(summary["voices"]), {"ak", "divisional", "schedule", "win_total"})

    def test_sign_disagreement_is_clamped_and_recorded(self) -> None:
        registry = load_registry()
        policy = aggregator_policy(registry)
        payload = build_aggregator_input(
            _game(), approved_opinions=_committee(), finals=[], snapshots=[], registry=registry, policy=policy
        )
        payload["feature_block"]["shrunk"]["expected_home_margin"] = -2.0
        response = rules_arm_response(payload)
        self.assertEqual(response["expected_home_margin"], 0.5)
        self.assertTrue(any("clamped" in note for note in response["discarded_considerations"]))


class JudgeNormalizationTests(unittest.TestCase):
    def setUp(self) -> None:
        registry = load_registry()
        self.policy = aggregator_policy(registry)
        self.payload = build_aggregator_input(
            _game(), approved_opinions=_committee(), finals=[], snapshots=[], registry=registry, policy=self.policy
        )
        self.expert = load_expert("god_judge")

    def _response(self, **overrides) -> dict:
        response = {
            "home_win_probability": 0.64,
            "expected_home_margin": 4.5,
            "projected_total": 46.0,
            "key_reasons": [
                {"voice": "Voice A", "text": "Rests on a broad cohort."},
                {"voice": "market", "text": "The number moved half a point toward the over."},
            ],
            "counterpoints": [{"voice": "Voice B", "text": "Cites a single game."}],
            "discarded_considerations": ["Injuries are not in the input."],
        }
        response.update(overrides)
        return response

    def test_valid_judge_response(self) -> None:
        opinion = normalize_aggregator_opinion(self._response(), self.payload, expert=self.expert, model="claude-fable-5-1")
        validate_opinion(opinion, away_team=AWAY, home_team=HOME, schedule_input=self.payload)
        self.assertIn("Voice A (", opinion["supporting_factors"][0])
        self.assertIn("model claude-fable-5-1", opinion["full_opinion"])
        summary = json.loads(opinion["calibration_summary_json"])
        self.assertEqual(summary["arm"], "judge")
        self.assertEqual(set(summary["judge_labels"]), {"Voice A", "Voice B", "Voice C", "Voice D"})
        self.assertEqual(opinion["predicted_away_score"] + opinion["predicted_home_score"], 46)

    def test_rejections(self) -> None:
        bad = [
            {"home_win_probability": 0.5},
            {"expected_home_margin": 0},
            {"expected_home_margin": -3},
            {"home_win_probability": 1.2},
            {"projected_total": 12},
            {"key_reasons": [{"voice": "Voice Z", "text": "unknown"}, {"voice": "market", "text": "x"}]},
            {"key_reasons": [{"voice": "schedule", "text": "unmasked id"}, {"voice": "market", "text": "x"}]},
            {"key_reasons": [{"voice": "Voice A", "text": "only one"}]},
            {"side": {"selection": HOME}},
            {"discarded_considerations": [""]},
        ]
        for overrides in bad:
            with self.subTest(overrides=overrides):
                with self.assertRaises(ValueError):
                    normalize_aggregator_opinion(self._response(**overrides), self.payload, expert=self.expert)


class GradingTests(unittest.TestCase):
    def _finals(self) -> list[dict]:
        return [
            {
                "event_id": "espn-1",
                "kickoff_utc": KICKOFF,
                "away_team": AWAY,
                "home_team": HOME,
                "away_score": 20,
                "home_score": 27,
                "week": 1,
            }
        ]

    def _snapshots(self) -> list[dict]:
        return [
            _snapshot(EVENT_ID, "2026-09-09T12:00:00+00:00", away="3.5,-110,160|nodata,nodata,nodata|nodata,nodata,nodata", home="-3.5,-110,-185|nodata,nodata,nodata|nodata,nodata,nodata", totals="44,-110,-110|nodata,nodata,nodata|nodata,nodata,nodata"),
            # Closing: home -4.5, total 45.5.
            _snapshot(EVENT_ID, "2026-09-09T23:50:00+00:00", away="4.5,-110,170|nodata,nodata,nodata|nodata,nodata,nodata", home="-4.5,-110,-200|nodata,nodata,nodata|nodata,nodata,nodata", totals="45.5,-110,-110|nodata,nodata,nodata|nodata,nodata,nodata"),
            # After kickoff: must be ignored.
            _snapshot(EVENT_ID, "2026-09-10T01:00:00+00:00", away="7,-110,300|nodata,nodata,nodata|nodata,nodata,nodata", home="-7,-110,-400|nodata,nodata,nodata|nodata,nodata,nodata", totals="50,-110,-110|nodata,nodata,nodata|nodata,nodata,nodata"),
        ]

    def test_grade_row_brier_ats_ou_and_clv(self) -> None:
        row = _opinion(
            "god_rules",
            model=DETERMINISTIC_MODEL,
            probability=0.7,
            margin=5,
            away_score=21,
            home_score=26,
            side_leg={"selection": HOME, "line": -3.5, "confidence_stars": 2},
            total_leg={"selection": "Under", "line": 44.5, "confidence_stars": 1},
        )
        graded = grade_opinion_row(row, finals=self._finals(), snapshots=self._snapshots())
        self.assertIsNotNone(graded)
        self.assertEqual(graded["final"], "20-27")
        self.assertAlmostEqual(graded["brier"], (0.7 - 1) ** 2, places=6)
        self.assertEqual(graded["ats_at_close"], "W")  # Seahawks -4.5 covered a 7-point win
        self.assertEqual(graded["ou_at_close"], "W")  # projected 47 > 45.5, actual 47 over
        legs = {leg["kind"]: leg for leg in graded["legs"]}
        self.assertEqual(legs["side"]["result"], "W")
        self.assertEqual(legs["side"]["clv_points"], 1.0)  # took -3.5, closed -4.5
        self.assertEqual(legs["total"]["result"], "L")
        self.assertEqual(legs["total"]["clv_points"], -1.0)  # Under 44.5, closed 45.5
        self.assertTrue(graded["closing_available"])

    def test_unresolved_and_missing_closing(self) -> None:
        row = _opinion("schedule", model="claude-opus-4-8", probability=0.6, margin=3, away_score=20, home_score=23)
        self.assertIsNone(grade_opinion_row(row, finals=[], snapshots=self._snapshots()))
        graded = grade_opinion_row(row, finals=self._finals(), snapshots=[])
        self.assertIsNone(graded["ats_at_close"])
        self.assertFalse(graded["closing_available"])
        self.assertAlmostEqual(graded["brier"], 0.16, places=6)

    def test_scoreboard_and_hedge_weights(self) -> None:
        registry = load_registry()
        policy = aggregator_policy(registry)
        rows = []
        finals = []
        for index in range(policy["weights_min_resolved"]):
            event_id = f"event-{index}"
            kickoff = f"2026-09-{13 + index:02d}T17:00:00+00:00"
            finals.append({"event_id": f"espn-{index}", "kickoff_utc": kickoff, "away_team": AWAY, "home_team": HOME, "away_score": 17, "home_score": 24})
            rows.append(_opinion("schedule", model="claude-opus-4-8", probability=0.75, margin=7, away_score=17, home_score=24, event_id=event_id, kickoff=kickoff))
            rows.append(_opinion("divisional", model="claude-opus-4-8", probability=0.55, margin=1, away_score=21, home_score=22, event_id=event_id, kickoff=kickoff))
        board = build_scoreboard(rows, finals=finals, snapshots=[], registry=registry, policy=policy, as_of="now")
        self.assertEqual(board["resolved_games"], policy["weights_min_resolved"])
        self.assertAlmostEqual(board["by_expert"]["schedule"]["brier"], 0.0625, places=4)
        self.assertAlmostEqual(board["by_expert"]["divisional"]["brier"], 0.2025, places=4)
        self.assertEqual(board["by_expert"]["ak"]["resolved"], 0)
        weighting = hedge_weights(board, ["schedule", "divisional", "ak"], policy)
        self.assertTrue(weighting["active"])
        self.assertGreater(weighting["weights"]["schedule"], 1.0)
        self.assertLess(weighting["weights"]["divisional"], 1.0)
        self.assertEqual(weighting["weights"]["ak"], 1.0)
        self.assertLessEqual(weighting["weights"]["schedule"], policy["weight_cap"])
        self.assertGreaterEqual(weighting["weights"]["divisional"], policy["weight_floor"])
        short = build_scoreboard(rows[:2], finals=finals[:1], snapshots=[], registry=registry, policy=policy, as_of="now")
        self.assertFalse(hedge_weights(short, ["schedule", "divisional"], policy)["active"])


class GenerationTests(unittest.IsolatedAsyncioTestCase):
    async def test_rules_arm_end_to_end(self) -> None:
        store = MemoryStore()
        row = await generate_opinion(
            expert_id="god_rules",
            game=_game(),
            history=[],
            line_snapshots=[],
            current_season_results=[],
            opinions=_committee(),
            store=store,
            generation_backend=DETERMINISTIC_BACKEND,
        )
        self.assertEqual(store.rows, [row])
        self.assertEqual(row["generation_status"], "valid")
        self.assertEqual(row["expert_mode"], "aggregator")
        self.assertEqual(row["model"], DETERMINISTIC_MODEL)
        self.assertEqual(row["generation_backend"], DETERMINISTIC_BACKEND)
        self.assertEqual(row["generation_effort"], "")
        self.assertEqual(row["output_schema_version"], 8)
        self.assertEqual(row["pick_market"], "side_and_total")
        self.assertEqual(set(row), set(OPINION_HEADERS))
        self.assertEqual(json.loads(row["input_json"])["input_profile"], AGGREGATOR_PROFILE)
        self.assertEqual(row["output_sha256"], opinion_output_sha256(row))
        self.assertEqual(json.loads(row["raw_response"])["home_win_probability"], row["home_win_probability"])
        self.assertIn("God Expert (rules)", row["thesis"])

    async def test_rules_arm_refuses_other_backends(self) -> None:
        with self.assertRaises(ValueError):
            await generate_opinion(
                expert_id="god_rules",
                game=_game(),
                history=[],
                opinions=_committee(),
                store=MemoryStore(),
                generation_backend="agent_runtime",
            )

    async def test_judge_arm_end_to_end(self) -> None:
        registry = load_registry()
        policy = aggregator_policy(registry)
        from moe import approved_opinions

        payload = build_aggregator_input(
            _game(), approved_opinions=approved_opinions(_committee()), finals=[], snapshots=[], registry=registry, policy=policy
        )
        request = build_judge_request(payload)
        labels = [voice["label"] for voice in request["voices"]]
        response = {
            "home_win_probability": 0.61,
            "expected_home_margin": 3.0,
            "projected_total": 45.0,
            "key_reasons": [
                {"voice": labels[0], "text": "Broad cohort, no single-game claims."},
                {"voice": "pool", "text": "The pool already sits close to the market."},
            ],
            "counterpoints": [{"voice": labels[1], "text": "Leans on one meeting."}],
            "discarded_considerations": [],
        }
        captured = {}

        async def create_fn(**kwargs):
            captured.update(kwargs)
            return SimpleNamespace(content=[SimpleNamespace(text=json.dumps(response))])

        store = MemoryStore()
        row = await generate_opinion(
            expert_id="god_judge",
            game=_game(),
            history=[],
            line_snapshots=[],
            current_season_results=[],
            opinions=_committee(),
            store=store,
            model="claude-fable-5-1",
            create_fn=create_fn,
            generation_backend="agent_runtime",
            generation_effort="max",
        )
        self.assertEqual(row["generation_status"], "valid")
        self.assertEqual(row["model"], "claude-fable-5-1")
        self.assertEqual(row["generation_backend"], "agent_runtime")
        self.assertEqual(row["generation_effort"], "max")
        self.assertEqual(captured["model"], "claude-fable-5-1")
        self.assertIn("God Expert Judge v1", captured["system"])
        persisted_input = json.loads(row["input_json"])
        self.assertEqual(persisted_input["input_profile"], JUDGE_REQUEST_PROFILE)
        self.assertEqual(persisted_input, request)
        self.assertNotIn("Schedule Expert", row["input_json"])
        self.assertIn(request["aggregator_input_sha256"], row["input_json"])
        summary = json.loads(row["calibration_summary_json"])
        self.assertEqual(summary["judge_labels"], payload["judge_view"]["labels"])
        self.assertEqual(row["home_win_probability"], 0.61)
        self.assertEqual(row["output_sha256"], opinion_output_sha256(row))

    async def test_judge_refuses_api_backend_and_other_models(self) -> None:
        async def create_fn(**_kwargs):
            raise AssertionError("must not be called")

        with self.assertRaises(ValueError):
            await generate_opinion(
                expert_id="god_judge",
                game=_game(),
                history=[],
                opinions=_committee(),
                store=MemoryStore(),
                create_fn=create_fn,
                generation_backend="anthropic_api",
            )
        with self.assertRaises(ValueError):
            await generate_opinion(
                expert_id="god_judge",
                game=_game(),
                history=[],
                opinions=_committee(),
                store=MemoryStore(),
                model="claude-opus-4-8",
                create_fn=create_fn,
                generation_backend="agent_runtime",
            )

    async def test_expected_input_hash_gates_persistence(self) -> None:
        from moe import approved_opinions
        from moe_god import canonical_json, sha256_text

        registry = load_registry()
        policy = aggregator_policy(registry)
        payload = build_aggregator_input(
            _game(), approved_opinions=approved_opinions(_committee()), finals=[], snapshots=[], registry=registry, policy=policy
        )
        request = build_judge_request(payload)
        labels = [voice["label"] for voice in request["voices"]]
        response = {
            "home_win_probability": 0.6,
            "expected_home_margin": 2.5,
            "projected_total": 44.0,
            "key_reasons": [
                {"voice": labels[0], "text": "Broad cohort."},
                {"voice": "market", "text": "Close to fair."},
            ],
            "counterpoints": [],
            "discarded_considerations": [],
        }

        async def create_fn(**_kwargs):
            return SimpleNamespace(content=[SimpleNamespace(text=json.dumps(response))])

        store = MemoryStore()
        with self.assertRaises(ValueError):
            await generate_opinion(
                expert_id="god_judge",
                game=_game(),
                history=[],
                opinions=_committee(),
                store=store,
                create_fn=create_fn,
                generation_backend="agent_runtime",
                expected_input_sha256="0" * 64,
            )
        self.assertEqual(store.rows, [])
        row = await generate_opinion(
            expert_id="god_judge",
            game=_game(),
            history=[],
            opinions=_committee(),
            store=store,
            create_fn=create_fn,
            generation_backend="agent_runtime",
            expected_input_sha256=sha256_text(canonical_json(request)),
        )
        self.assertEqual(row["generation_status"], "valid")
        self.assertEqual(row["input_sha256"], sha256_text(canonical_json(request)))

    async def test_invalid_judge_response_is_persisted_as_audit_row(self) -> None:
        async def create_fn(**_kwargs):
            return SimpleNamespace(content=[SimpleNamespace(text=json.dumps({"home_win_probability": 0.5}))])

        store = MemoryStore()
        with self.assertRaises(ValueError):
            await generate_opinion(
                expert_id="god_judge",
                game=_game(),
                history=[],
                opinions=_committee(),
                store=store,
                create_fn=create_fn,
                generation_backend="agent_runtime",
            )
        self.assertEqual(len(store.rows), 1)
        self.assertEqual(store.rows[0]["generation_status"], "invalid")
        self.assertEqual(store.rows[0]["review_status"], "not_applicable")


class RenderTests(unittest.IsolatedAsyncioTestCase):
    async def test_bot_views_render_aggregator_rows_with_two_legs(self) -> None:
        from moe import opinion_detail, opinion_summary

        row = await generate_opinion(
            expert_id="god_rules",
            game=_game(),
            history=[],
            opinions=_committee(),
            store=MemoryStore(),
            generation_backend=DETERMINISTIC_BACKEND,
        )
        row["review_status"] = "approved"
        row["approved_output_sha256"] = row["output_sha256"]
        text, buttons = opinion_detail(row, page=0, event_id=EVENT_ID)
        self.assertIn("God Expert (Rules)", text)
        self.assertIn("<b>Side:</b>", text)
        self.assertIn("<b>Total:</b>", text)
        self.assertIn("deterministic", text)
        summary_text, summary_buttons = opinion_summary(
            _game(), [row], page=0, event_id=EVENT_ID
        )
        self.assertIn("God Expert (Rules)", summary_text)
        self.assertTrue(summary_buttons)


class RegistryTests(unittest.TestCase):
    def test_registry_entries(self) -> None:
        rules = load_expert("god_rules")
        judge = load_expert("god_judge")
        self.assertEqual(rules["mode"], "aggregator")
        self.assertEqual(rules["allowed_backends"], ["deterministic"])
        self.assertEqual(rules["output_schema_version"], 8)
        self.assertEqual(judge["mode"], "aggregator_judge")
        self.assertEqual(judge["default_model"], "claude-fable-5-1")
        self.assertEqual(judge["allowed_models"], ["claude-fable-5-1"])
        self.assertEqual(judge["allowed_backends"], ["agent_runtime"])
        self.assertEqual(judge["reasoning_effort"], "max")
        self.assertIn("Return exactly one JSON object", judge["prompt_text"])
        policy = aggregator_policy(load_registry())
        self.assertEqual(policy["edge_threshold"], 0.03)
        self.assertEqual(policy["shrink_lambda"], 0.5)
        self.assertTrue(math.isclose(policy["kelly_fraction"], 0.25))


FIXTURES = Path(__file__).resolve().parent / "fixtures" / "god_week1"
PASS_LEG = {"selection": "PASS", "line": None, "confidence_stars": 1}


def _stamp(row: dict) -> dict:
    """Recompute the approval hashes after editing a hashed column."""
    digest = opinion_output_sha256(row)
    row["output_sha256"] = digest
    row["approved_output_sha256"] = (
        digest if row.get("review_status") == "approved" else ""
    )
    return row


def _with_input(row: dict, payload: dict) -> dict:
    """Persist ``payload`` as the row's input the way generation does."""
    row["input_json"] = canonical_json(payload)
    row["input_sha256"] = sha256_text(row["input_json"])
    return _stamp(row)


def _final(
    event_id: str,
    kickoff: str,
    away_score: int,
    home_score: int,
    *,
    away: str = AWAY,
    home: str = HOME,
) -> dict:
    return {
        "event_id": f"espn-{event_id}",
        "kickoff_utc": kickoff,
        "away_team": away,
        "home_team": home,
        "away_score": away_score,
        "home_score": home_score,
    }


def _arm(
    expert_id: str,
    *,
    event_id: str,
    kickoff: str,
    away: str = AWAY,
    home: str = HOME,
    probability: float,
    margin: float,
    away_score: int,
    home_score: int,
    side_leg: dict | None = None,
    total_leg: dict | None = None,
    generated_at: str = "2026-09-05T02:00:00+00:00",
    opinion_id: str | None = None,
) -> dict:
    """An approved row for one arm; legs default to PASS."""
    return _opinion(
        expert_id,
        model=DETERMINISTIC_MODEL if expert_id == "god_rules" else "claude-fable-5-1",
        probability=probability,
        margin=margin,
        away_score=away_score,
        home_score=home_score,
        stars=1,
        generated_at=generated_at,
        opinion_id=opinion_id or f"{expert_id}-{event_id}",
        event_id=event_id,
        away=away,
        home=home,
        kickoff=kickoff,
        side_leg=side_leg or PASS_LEG,
        total_leg=total_leg or PASS_LEG,
    )


def _fixture_row(name: str) -> dict:
    """A persisted Week 1 row as the sheet returns it, approved.

    The fixture holds the JSON columns parsed; canonical JSON of
    ``input_json`` reproduces ``input_sha256``. The fixture omits the factor
    columns, so the approval hash is recomputed rather than copied.
    """
    data = json.loads((FIXTURES / f"{name}.json").read_text(encoding="utf-8"))
    row = {header: "" for header in OPINION_HEADERS}
    for key, value in data.items():
        if key == "fixture_note":
            continue
        row[key] = canonical_json(value) if isinstance(value, (dict, list)) else value
    row["review_status"] = "approved"
    return _stamp(row)


class DisagreementReportTests(unittest.TestCase):
    """Both arms on three games, graded without closing lines."""

    E1 = {"event_id": "evt-c", "kickoff": "2026-09-13T17:00:00+00:00", "away": AWAY, "home": HOME}
    E2 = {"event_id": "evt-b", "kickoff": "2026-09-13T20:25:00+00:00", "away": "San Francisco 49ers", "home": "Los Angeles Rams"}
    E3 = {"event_id": "evt-a", "kickoff": "2026-09-14T00:20:00+00:00", "away": "Dallas Cowboys", "home": "Philadelphia Eagles"}

    def setUp(self) -> None:
        self.registry = load_registry()
        self.policy = aggregator_policy(self.registry)
        e1, e2, e3 = self.E1, self.E2, self.E3
        self.finals = [
            _final("evt-c", e1["kickoff"], 20, 27),
            _final("evt-b", e2["kickoff"], 24, 20, away=e2["away"], home=e2["home"]),
            _final("evt-a", e3["kickoff"], 21, 24, away=e3["away"], home=e3["home"]),
        ]

        def home_side(game: dict) -> dict:
            return {"selection": game["home"], "line": -3.5, "confidence_stars": 1}

        def away_side(game: dict) -> dict:
            return {"selection": game["away"], "line": 3.5, "confidence_stars": 1}

        def over(line: float) -> dict:
            return {"selection": "Over", "line": line, "confidence_stars": 1}

        self.rows = [
            # 20-27: both arms take Seahawks -3.5 and Over 44.5, both win. An
            # older rules row on the same game must lose to the latest one.
            _arm("god_rules", **e1, probability=0.55, margin=1, away_score=21, home_score=22, generated_at="2026-09-04T00:00:00+00:00", opinion_id="god_rules-evt-c-old"),
            _arm("god_rules", **e1, probability=0.70, margin=5, away_score=21, home_score=26, side_leg=home_side(e1), total_leg=over(44.5)),
            _arm("god_judge", **e1, probability=0.60, margin=3, away_score=21, home_score=24, side_leg=home_side(e1), total_leg=over(44.5)),
            # 24-20 (total 44): rules takes 49ers +3.5 (wins) and Over 44.5
            # (loses); the judge passes both.
            _arm("god_rules", **e2, probability=0.45, margin=-1, away_score=23, home_score=22, side_leg=away_side(e2), total_leg=over(44.5)),
            _arm("god_judge", **e2, probability=0.40, margin=-2, away_score=23, home_score=21),
            # 21-24 (total 45): opposite sides, the judge's Cowboys +3.5 wins;
            # rules' Over 45 pushes against a pass.
            _arm("god_rules", **e3, probability=0.60, margin=4, away_score=21, home_score=25, side_leg=home_side(e3), total_leg=over(45)),
            _arm("god_judge", **e3, probability=0.55, margin=1, away_score=22, home_score=23, side_leg=away_side(e3)),
        ]

    def _pairs(self, rows: list[dict] | None = None) -> list[dict]:
        rows = self.rows if rows is None else rows
        graded = grade_all(rows, finals=self.finals, snapshots=[], registry=self.registry, policy=self.policy)
        return arm_pairs(rows, graded)

    def test_pairs_follow_kickoff_and_fall_back_to_latest_per_arm(self) -> None:
        pairs = self._pairs()
        self.assertEqual([pair["event_id"] for pair in pairs], ["evt-c", "evt-b", "evt-a"])
        self.assertEqual([pair["linked"] for pair in pairs], [False, False, False])
        first = pairs[0]
        self.assertEqual(first["rules_row"]["opinion_id"], "god_rules-evt-c")
        self.assertEqual(first["rules"]["home_win_probability"], 0.7)
        self.assertEqual(first["judge_row"]["opinion_id"], "god_judge-evt-c")
        self.assertEqual((first["away_team"], first["home_team"], first["final"]), (AWAY, HOME, "20-27"))
        self.assertEqual((first["season"], first["week"], first["commence_time_utc"]), (2026, 1, self.E1["kickoff"]))
        self.assertEqual(first["judge"]["expert_id"], "god_judge")
        # A game graded for one arm only never pairs.
        self.assertEqual(self._pairs(self.rows[:2]), [])

    def test_compare_legs(self) -> None:
        def leg(kind: str, selection: str, line: float, result: str) -> dict:
            return {"kind": kind, "selection": selection, "line": line, "result": result, "clv_points": None}

        home_w, home_l, home_p = (leg("side", HOME, -3.5, r) for r in "WLP")
        away_w, away_l, away_p = (leg("side", AWAY, 3.5, r) for r in "WLP")
        self.assertEqual(
            compare_legs(home_w, home_w),
            {"kind": "side", "agreed": True, "rules": f"{HOME} -3.5 (W)", "judge": f"{HOME} -3.5 (W)", "right": None},
        )
        self.assertEqual(
            compare_legs(None, None, kind="total"),
            {"kind": "total", "agreed": True, "rules": "PASS", "judge": "PASS", "right": None},
        )
        self.assertEqual(compare_legs(home_w, None)["right"], "rules")  # bet and won against a pass
        self.assertEqual(compare_legs(home_l, None)["right"], "judge")  # bet and lost against a pass
        self.assertEqual(compare_legs(None, away_w)["right"], "judge")
        self.assertEqual(compare_legs(None, away_l)["right"], "rules")
        self.assertEqual(compare_legs(home_l, away_w)["right"], "judge")  # opposite sides
        self.assertEqual(compare_legs(home_w, away_l)["right"], "rules")
        self.assertEqual(compare_legs(home_p, None)["right"], "neither")  # push
        self.assertEqual(compare_legs(None, away_p)["right"], "neither")
        self.assertEqual(compare_legs(home_p, away_p)["right"], "neither")
        self.assertEqual(compare_legs(home_l, away_l)["right"], "neither")  # two losers
        same_side_other_line = compare_legs(home_w, leg("side", HOME, -4.0, "W"))
        self.assertFalse(same_side_other_line["agreed"])
        self.assertEqual(same_side_other_line["right"], "neither")  # two winners
        self.assertEqual(compare_legs(None, leg("total", "Over", 44.5, "L"))["judge"], "Over 44.5 (L)")

    def test_report_games_and_totals(self) -> None:
        report = disagreement_report(self._pairs())
        games = report["games"]
        self.assertEqual(
            [game["teams"] for game in games],
            [f"{AWAY} @ {HOME}", "San Francisco 49ers @ Los Angeles Rams", "Dallas Cowboys @ Philadelphia Eagles"],
        )
        self.assertEqual([game["final"] for game in games], ["20-27", "24-20", "21-24"])
        self.assertEqual([[leg["agreed"] for leg in game["legs"]] for game in games], [[True, True], [False, False], [False, False]])
        self.assertEqual([[leg["right"] for leg in game["legs"]] for game in games], [[None, None], ["rules", "judge"], ["judge", "neither"]])
        self.assertEqual([leg["kind"] for leg in games[0]["legs"]], ["side", "total"])
        self.assertEqual(games[1]["legs"][0]["rules"], "San Francisco 49ers +3.5 (W)")
        self.assertEqual(games[1]["legs"][0]["judge"], "PASS")
        self.assertEqual(games[1]["legs"][1]["rules"], "Over 44.5 (L)")
        self.assertEqual(games[2]["legs"][0]["judge"], "Dallas Cowboys +3.5 (W)")
        self.assertEqual(games[2]["legs"][1]["rules"], "Over 45 (P)")
        self.assertAlmostEqual(games[0]["brier_rules"], 0.09, places=6)
        self.assertAlmostEqual(games[0]["brier_judge"], 0.16, places=6)
        totals = report["totals"]
        self.assertEqual((totals["n_games"], totals["legs_total"], totals["legs_agreed"]), (3, 6, 2))
        self.assertAlmostEqual(totals["agreement_rate"], round(2 / 6, 4), places=6)
        diffs = [0.09 - 0.16, 0.2025 - 0.16, 0.16 - 0.2025]
        self.assertEqual(totals["brier_diff_n"], 3)
        self.assertAlmostEqual(totals["brier_diff_mean"], round(statistics.mean(diffs), 4), places=6)
        self.assertAlmostEqual(totals["brier_diff_mean"], -0.0233, places=4)
        self.assertAlmostEqual(totals["brier_diff_se"], round(statistics.stdev(diffs) / math.sqrt(len(diffs)), 4), places=6)
        self.assertAlmostEqual(totals["brier_diff_se"], 0.0339, places=4)
        self.assertEqual(totals["disagreement_record"], {"rules": 1, "judge": 2, "neither": 1})
        json.dumps(report)  # the --json output must serialize

    def test_single_game_has_no_standard_error(self) -> None:
        totals = disagreement_report(self._pairs()[:1])["totals"]
        self.assertEqual(totals["brier_diff_n"], 1)
        self.assertAlmostEqual(totals["brier_diff_mean"], -0.07, places=4)
        self.assertIsNone(totals["brier_diff_se"])
        self.assertEqual(totals["agreement_rate"], 1.0)
        self.assertEqual(totals["disagreement_record"], {"rules": 0, "judge": 0, "neither": 0})

    def test_missing_brier_is_left_out_of_the_difference(self) -> None:
        pairs = self._pairs()
        pairs[0]["rules"]["brier"] = None  # a tie leaves no Brier
        totals = disagreement_report(pairs)["totals"]
        self.assertEqual(totals["brier_diff_n"], 2)
        self.assertAlmostEqual(totals["brier_diff_mean"], 0.0, places=6)
        self.assertEqual(totals["n_games"], 3)

    def test_formatted_report(self) -> None:
        lines = format_disagreement_report(disagreement_report(self._pairs()))
        self.assertEqual(lines[0], "Disagreement report: 3 games graded for both arms")
        self.assertTrue(lines[1].startswith(f"wk1   {AWAY} @ {HOME}"), lines[1])
        self.assertIn("final=20-27   brier rules=0.0900 judge=0.1600 diff=-0.0700  linked=no", lines[1])
        self.assertEqual(lines[2].strip(), f"side   agree   {HOME} -3.5 (W)")
        self.assertEqual(lines[3].strip(), "total  agree   Over 44.5 (W)")
        self.assertIn("side   differ  rules=San Francisco 49ers +3.5 (W)  judge=PASS  right=rules", lines[5])
        self.assertIn("total  differ  rules=Over 45 (P)  judge=PASS  right=neither", lines[9])
        self.assertEqual(lines[-3], "games=3 legs=6 agreed=2 agreement_rate=33.3%")
        self.assertEqual(lines[-2], "brier diff (rules - judge): mean=-0.0233 se=0.0339 n=3")
        self.assertEqual(lines[-1], "disagreement record: rules=1 judge=2 neither=1")
        self.assertEqual(len(lines), 1 + 3 * 3 + 3)
        single = format_disagreement_report(disagreement_report(self._pairs()[:1]))
        self.assertEqual(single[0], "Disagreement report: 1 game graded for both arms")
        self.assertEqual(single[-2], "brier diff (rules - judge): mean=-0.0700 se=— n=1")
        self.assertEqual(format_disagreement_report(disagreement_report([])), ["no games graded for both arms"])

    def test_scoreboard_never_lists_mean_of_arms(self) -> None:
        board = build_scoreboard(self.rows, finals=self.finals, snapshots=[], registry=self.registry, policy=self.policy, as_of="now")
        self.assertNotIn(MEAN_OF_ARMS_ID, board["by_expert"])
        self.assertEqual(board["by_expert"]["god_rules"]["resolved"], 4)  # three games plus the older row
        self.assertEqual(board["by_expert"]["god_judge"]["resolved"], 3)
        self.assertNotIn(MEAN_OF_ARMS_ID, hedge_weights(board, list(board["by_expert"]), self.policy)["weights"])
        # The mean needs the rules row's persisted input; these rows carry none.
        with self.assertRaises(ValueError):
            mean_of_arms_results(self._pairs(), finals=self.finals, snapshots=[])


class MeanOfArmsTests(unittest.TestCase):
    """The mean of the two arms, graded from the rules row's persisted input."""

    HOME_35 = {"selection": HOME, "line": -3.5, "confidence_stars": 1}
    OVER_445 = {"selection": "Over", "line": 44.5, "confidence_stars": 1}

    def setUp(self) -> None:
        self.registry = load_registry()
        self.policy = aggregator_policy(self.registry)
        committee = _committee()
        self.payload = build_aggregator_input(
            _game(), approved_opinions=committee, finals=[], snapshots=[], registry=self.registry, policy=self.policy
        )
        self.rules = _with_input(
            _arm("god_rules", event_id=EVENT_ID, kickoff=KICKOFF, probability=0.70, margin=5, away_score=21, home_score=26, side_leg=self.HOME_35, total_leg=self.OVER_445, opinion_id="rules-sea"),
            self.payload,
        )
        self.judge = _with_input(
            _arm("god_judge", event_id=EVENT_ID, kickoff=KICKOFF, probability=0.60, margin=3, away_score=21, home_score=24, opinion_id="judge-sea"),
            build_judge_request(self.payload),
        )
        # A newer judge row from another sheet state must not displace the linked one.
        self.stray = _with_input(
            _arm("god_judge", event_id=EVENT_ID, kickoff=KICKOFF, probability=0.90, margin=10, away_score=17, home_score=27, generated_at="2026-09-08T02:00:00+00:00", opinion_id="judge-sea-stray"),
            {**build_judge_request(self.payload), "aggregator_input_sha256": "0" * 64},
        )
        self.rows = committee + [self.rules, self.judge, self.stray]
        self.finals = [_final("sea", KICKOFF, 20, 27)]
        # Closing: home -4.5, total 45.5.
        self.snapshots = [
            _snapshot(EVENT_ID, "2026-09-09T23:50:00+00:00", away="4.5,-110,170|nodata,nodata,nodata|nodata,nodata,nodata", home="-4.5,-110,-200|nodata,nodata,nodata|nodata,nodata,nodata", totals="45.5,-110,-110|nodata,nodata,nodata|nodata,nodata,nodata"),
        ]

    def _pairs(self, rows: list[dict] | None = None) -> list[dict]:
        rows = self.rows if rows is None else rows
        graded = grade_all(rows, finals=self.finals, snapshots=self.snapshots, registry=self.registry, policy=self.policy)
        return arm_pairs(rows, graded)

    def test_hash_linked_pair_beats_newer_unlinked_judge_row(self) -> None:
        self.assertEqual(json.loads(self.judge["input_json"])["aggregator_input_sha256"], self.rules["input_sha256"])
        pairs = self._pairs()
        self.assertEqual(len(pairs), 1)
        self.assertTrue(pairs[0]["linked"])
        self.assertEqual(pairs[0]["rules_row"]["opinion_id"], "rules-sea")
        self.assertEqual(pairs[0]["judge_row"]["opinion_id"], "judge-sea")
        # Without the linked judge row the newest judge row pairs up, unlinked.
        pairs = self._pairs([row for row in self.rows if row["opinion_id"] != "judge-sea"])
        self.assertFalse(pairs[0]["linked"])
        self.assertEqual(pairs[0]["judge_row"]["opinion_id"], "judge-sea-stray")

    def test_mean_estimate_legs_and_grade(self) -> None:
        means = mean_of_arms_results(self._pairs(), finals=self.finals, snapshots=self.snapshots)
        self.assertEqual(len(means), 1)
        mean = means[0]
        self.assertEqual(mean["expert_id"], MEAN_OF_ARMS_ID)
        self.assertEqual(mean["opinion_id"], "mean:rules-sea:judge-sea")
        self.assertEqual(mean["arms"], {"rules": "rules-sea", "judge": "judge-sea"})
        self.assertEqual((mean["event_id"], mean["season"], mean["week"], mean["final"]), (EVENT_ID, 2026, 1, "20-27"))
        self.assertAlmostEqual(mean["home_win_probability"], 0.65, places=6)  # (0.70 + 0.60) / 2
        self.assertEqual(
            mean["estimate"],
            {"home_win_probability": 0.65, "expected_home_margin": 4.0, "projected_total": 46.0, "predicted_away_score": 21, "predicted_home_score": 25},
        )
        self.assertAlmostEqual(mean["brier"], 0.35**2, places=6)
        self.assertEqual(mean["home_won"], 1.0)
        expected = apply_policy(
            home_win_probability=0.65, expected_home_margin=4.0, projected_total=46.0, market=self.payload["market"], policy=self.policy, away_team=AWAY, home_team=HOME
        )
        legs = {leg["kind"]: leg for leg in mean["legs"]}
        self.assertEqual((legs["side"]["selection"], legs["side"]["line"]), (expected["side"]["selection"], expected["side"]["line"]))
        self.assertEqual((legs["side"]["selection"], legs["side"]["line"], legs["side"]["result"]), (HOME, -3.5, "W"))
        self.assertEqual((legs["total"]["selection"], legs["total"]["line"], legs["total"]["result"]), ("Over", 44.5, "W"))
        self.assertEqual(legs["side"]["clv_points"], 1.0)  # took -3.5, closed -4.5
        self.assertEqual(legs["total"]["clv_points"], 1.0)  # Over 44.5, closed 45.5
        self.assertEqual(mean["ats_at_close"], "W")
        self.assertEqual(mean["ou_at_close"], "W")  # mean total 46 leans over the 45.5 close; 47 landed
        self.assertTrue(mean["closing_available"])
        json.dumps(means)

    def test_persisted_policy_governs_and_missing_keys_take_defaults(self) -> None:
        pairs = self._pairs()
        rules_input = json.loads(self.rules["input_json"])
        # The persisted policy wins over the live registry: a 10% bar passes both legs.
        # (input_sha256 stays as is: pairing reads the hash column, not the content.)
        strict = {**rules_input, "policy": {**rules_input["policy"], "edge_threshold": 0.10}}
        pairs[0]["rules_row"] = {**self.rules, "input_json": canonical_json(strict)}
        means = mean_of_arms_results(pairs, finals=self.finals, snapshots=[])
        self.assertEqual(means[0]["legs"], [])
        # A key the row predates takes its default, so the legs bet again.
        self.assertEqual(DEFAULT_POLICY["edge_threshold"], 0.03)
        older = {**rules_input, "policy": {key: value for key, value in rules_input["policy"].items() if key != "edge_threshold"}}
        pairs[0]["rules_row"] = {**self.rules, "input_json": canonical_json(older)}
        means = mean_of_arms_results(pairs, finals=self.finals, snapshots=[])
        self.assertEqual([(leg["kind"], leg["selection"]) for leg in means[0]["legs"]], [("side", HOME), ("total", "Over")])
        self.assertFalse(means[0]["closing_available"])

    def test_ledger_row_flattens_the_mean(self) -> None:
        mean = mean_of_arms_results(self._pairs(), finals=self.finals, snapshots=self.snapshots)[0]
        row = ledger_row(mean, graded_at_utc="2026-09-10T12:00:00+00:00")
        self.assertEqual(set(row), set(GRADE_HEADERS))
        self.assertEqual(row["expert_id"], MEAN_OF_ARMS_ID)
        self.assertEqual(row["opinion_id"], "mean:rules-sea:judge-sea")
        self.assertEqual((row["side_selection"], row["side_line"], row["side_result"], row["side_clv_points"]), (HOME, -3.5, "W", 1.0))
        self.assertEqual((row["total_selection"], row["total_line"], row["total_result"], row["total_clv_points"]), ("Over", 44.5, "W", 1.0))
        self.assertEqual((row["season"], row["week"], row["final"], row["home_won"]), (2026, 1, "20-27", 1))
        self.assertAlmostEqual(row["brier"], 0.1225, places=6)
        self.assertTrue(row["closing_available"])

    def test_mean_never_reaches_the_scoreboard_or_weights(self) -> None:
        # A stray row claiming the id is neither a voice nor an aggregator.
        stray = _opinion(MEAN_OF_ARMS_ID, model=DETERMINISTIC_MODEL, probability=0.65, margin=4, away_score=21, home_score=25)
        rows = self.rows + [stray]
        self.assertNotIn(MEAN_OF_ARMS_ID, self.registry["experts"])
        self.assertNotIn(MEAN_OF_ARMS_ID, {item[0] for item in select_voice_rows(rows, event_id=EVENT_ID, registry=self.registry, policy=self.policy)})
        board = build_scoreboard(rows, finals=self.finals, snapshots=self.snapshots, registry=self.registry, policy=self.policy, as_of="now")
        self.assertNotIn(MEAN_OF_ARMS_ID, board["by_expert"])
        self.assertEqual(board["by_expert"]["god_rules"]["resolved"], 1)
        self.assertEqual(board["by_expert"]["god_judge"]["resolved"], 2)
        self.assertNotIn(MEAN_OF_ARMS_ID, hedge_weights(board, list(board["by_expert"]), self.policy)["weights"])
        graded = grade_all(rows, finals=self.finals, snapshots=self.snapshots, registry=self.registry, policy=self.policy)
        self.assertNotIn(MEAN_OF_ARMS_ID, {result["expert_id"] for result in graded})

    def test_week1_fixture_pair_links_by_hash(self) -> None:
        rules = _fixture_row("sea_rules")
        judge = _fixture_row("sea_judge")
        self.assertEqual(sha256_text(rules["input_json"]), rules["input_sha256"])
        self.assertEqual(sha256_text(judge["input_json"]), judge["input_sha256"])
        self.assertEqual(json.loads(judge["input_json"])["aggregator_input_sha256"], rules["input_sha256"])
        rows = [rules, judge]
        finals = [_final("sea-week1", "2026-09-10T00:20:00+00:00", 20, 27)]
        graded = grade_all(rows, finals=finals, snapshots=[], registry=self.registry, policy=self.policy)
        self.assertEqual({result["expert_id"] for result in graded}, {"god_rules", "god_judge"})
        pairs = arm_pairs(rows, graded)
        self.assertEqual(len(pairs), 1)
        pair = pairs[0]
        self.assertTrue(pair["linked"])
        self.assertEqual(pair["rules_row"]["opinion_id"], "c884d868-3e1e-4b58-abba-4fa3707e08d8")
        self.assertEqual(pair["judge_row"]["opinion_id"], "444bf3de-ceba-4862-b423-0a5041da0763")
        self.assertEqual((pair["week"], pair["final"], pair["home_team"]), (1, "20-27", HOME))
        report = disagreement_report(pairs)
        game = report["games"][0]
        # Rules bet Seahawks -3.5 and Over 44.5 (both won); the judge passed both.
        self.assertEqual([leg["rules"] for leg in game["legs"]], ["Seattle Seahawks -3.5 (W)", "Over 44.5 (W)"])
        self.assertEqual([leg["judge"] for leg in game["legs"]], ["PASS", "PASS"])
        self.assertEqual([leg["right"] for leg in game["legs"]], ["rules", "rules"])
        self.assertEqual(report["totals"]["disagreement_record"], {"rules": 2, "judge": 0, "neither": 0})
        self.assertAlmostEqual(game["brier_rules"], (1 - 0.6259) ** 2, places=3)
        self.assertAlmostEqual(game["brier_judge"], (1 - 0.62) ** 2, places=3)
        mean = mean_of_arms_results(pairs, finals=finals, snapshots=[])[0]
        self.assertEqual(mean["opinion_id"], "mean:c884d868-3e1e-4b58-abba-4fa3707e08d8:444bf3de-ceba-4862-b423-0a5041da0763")
        self.assertAlmostEqual(mean["home_win_probability"], 0.623, places=3)  # (0.6259 + 0.62) / 2
        self.assertAlmostEqual(mean["estimate"]["expected_home_margin"], 3.74, places=2)  # (3.88 + 3.6) / 2
        self.assertAlmostEqual(mean["estimate"]["projected_total"], 45.5, places=2)  # (46 + 45) / 2
        self.assertEqual((mean["estimate"]["predicted_away_score"], mean["estimate"]["predicted_home_score"]), (21, 25))
        legs = {leg["kind"]: leg for leg in mean["legs"]}
        # Margin 3.74 against -3.5 is a 2.9% cover edge, under the bar; total 45.5 vs 44.5 clears it.
        self.assertNotIn("side", legs)
        self.assertEqual((legs["total"]["selection"], legs["total"]["line"], legs["total"]["result"]), ("Over", 44.5, "W"))
        self.assertEqual(ledger_row(mean, graded_at_utc="now")["expert_id"], MEAN_OF_ARMS_ID)


if __name__ == "__main__":
    unittest.main()
