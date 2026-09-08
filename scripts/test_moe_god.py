#!/usr/bin/env python3
"""Tests for the God Expert aggregator (rules arm, judge arm, shared policy)."""

from __future__ import annotations

import json
import math
import re
import statistics
import unittest
from pathlib import Path
from types import SimpleNamespace

from moe import (
    OPINION_HEADERS,
    GoogleSheetsMoeOpinionStore,
    approved_opinions,
    generate_opinion,
    latest_opinions,
    load_expert,
    opinion_output_sha256,
    validate_opinion,
)
from moe_god import (
    ADVERSE_MOVE_REASON,
    AGGREGATOR_PROFILE,
    DEFAULT_POLICY,
    DETERMINISTIC_BACKEND,
    DETERMINISTIC_MODEL,
    ENSEMBLE_RULE,
    EV_FLOOR_REASON,
    FENCE_NOTE,
    GRADE_HEADERS,
    JUDGE_REQUEST_PROFILE,
    MARKETS,
    MEAN_OF_ARMS_ID,
    NO_EXPECTATION_REASON,
    SIGN_NOTE,
    aggregator_policy,
    american_to_implied,
    apply_policy,
    arm_pairs,
    build_aggregator_input,
    build_feature_block,
    build_judge_request,
    build_market_block,
    build_scoreboard,
    canonical_json,
    coherent_estimate,
    committee_key,
    compare_legs,
    cover_probability,
    disagreement_report,
    ensemble_response,
    evidence_overlap,
    extract_evidence,
    fair_pair,
    format_disagreement_report,
    grade_all,
    grade_opinion_row,
    hedge_weights,
    ledger_row,
    load_registry,
    mean_of_arms_results,
    movement_since_open,
    normalize_aggregator_opinion,
    over_probability,
    overlap_adjusted_weights,
    price_cents,
    reason_reference_text,
    rules_arm_response,
    select_voice_rows,
    sha256_text,
    voice_evidence,
    voice_markets,
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
from scripts.review_moe_opinion import week_rows

EVENT_ID = "8c94552d022acec4a0458d70c19d3da9"
AWAY = "New England Patriots"
HOME = "Seattle Seahawks"
KICKOFF = "2026-09-10T00:20:00+00:00"
# The four persisted Week 1 rows (two rules, two judge), parsed.
FIXTURES = Path(__file__).resolve().parent / "fixtures" / "god_week1"


def _fixture(name: str) -> dict:
    return json.loads((FIXTURES / f"{name}.json").read_text(encoding="utf-8"))


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
    opening_away: str = "3.5,-110,165|2.5,-105,135|0.5,-135,120",
    opening_home: str = "-3.5,-110,-190|-2.5,-115,-155|-0.5,115,-140",
    opening_totals: str = "44,-110,-110|21.5,-110,-110|7.5,-105,-115",
    latest_away: str = "3.5,-120,158|2.5,-105,135|0.5,-135,120",
    latest_home: str = "-3.5,100,-181|-2.5,-115,-155|-0.5,115,-140",
    latest_totals: str = "44.5,-105,-115|21.5,-110,-110|7.5,-105,-115",
) -> dict:
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
    counters: list | None = None,
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
            "counterarguments_json": json.dumps(
                counters if counters is not None else [f"{expert_id} counter"]
            ),
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
        movement = market["movement_since_open"]
        self.assertEqual(movement["away_spread"], 0.0)
        self.assertEqual(movement["home_moneyline"], 9.0)
        self.assertAlmostEqual(movement["fair_home_ml"], -0.0102, places=4)
        self.assertEqual(movement["home_spread_price"], 10.0)  # -110 -> +100
        self.assertEqual(movement["away_spread_price"], -10.0)  # -110 -> -120
        self.assertEqual(movement["over_price"], 5.0)  # -110 -> -105
        self.assertEqual(movement["under_price"], -5.0)  # -110 -> -115


# The default home line opens at +100, so its price never moves since open.
# The default market's -110 -> +100 home price is the Week 1 veto case.
STEADY_HOME = "-3.5,100,-190|-2.5,-115,-155|-0.5,115,-140"
NO_DATA_GROUPS = "nodata,nodata,nodata|nodata,nodata,nodata|nodata,nodata,nodata"


class PolicyTests(unittest.TestCase):
    def setUp(self) -> None:
        self.policy = aggregator_policy(load_registry())
        self.market = build_market_block(_game())

    def _market(self, **packed: str) -> dict:
        return build_market_block(_game(**packed))

    def _legs(
        self,
        probability: float,
        margin: float,
        total: float,
        *,
        market: dict | None = None,
        policy: dict | None = None,
    ) -> dict:
        return apply_policy(
            home_win_probability=probability,
            expected_home_margin=margin,
            projected_total=total,
            market=market if market is not None else self.market,
            policy=policy if policy is not None else self.policy,
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
        # On the default market this home bet is vetoed for its +10-cent move
        # (test_veto_side_on_price_move); a home price that opened at +100
        # lets the bet stand, and the stake arithmetic only reads the latest.
        legs = self._legs(0.80, 9.0, 52.0, market=self._market(opening_home=STEADY_HOME))
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

    def test_price_cents_and_movement_arithmetic(self) -> None:
        self.assertEqual(price_cents(-110), -10.0)
        self.assertEqual(price_cents(110), 10.0)
        self.assertEqual(price_cents(100), 0.0)
        self.assertEqual(price_cents(-100), 0.0)
        for bad in (0, 50, -99.5):
            with self.subTest(bad=bad):
                with self.assertRaises(ValueError):
                    price_cents(bad)
        opening = {
            "home_spread": -3.5, "away_spread": 3.5, "total": 44,
            "home_moneyline": -190, "away_moneyline": 165,
            "home_spread_price": -110, "away_spread_price": -105,
            "over_price": -105, "under_price": -115,
        }
        latest = {
            "home_spread": -3, "away_spread": 3, "total": 44.5,
            "home_moneyline": -181, "away_moneyline": 158,
            "home_spread_price": 100, "away_spread_price": 105,
            "over_price": -110, "under_price": 100,
        }
        movement = movement_since_open(opening, latest)
        self.assertEqual(movement["home_spread"], 0.5)
        self.assertEqual(movement["away_spread"], -0.5)
        self.assertEqual(movement["total"], 0.5)
        self.assertEqual(movement["home_moneyline"], 9.0)
        self.assertAlmostEqual(movement["fair_home_ml"], -0.0102, places=4)
        # Crossing even money: -110 -> +100 and -105 -> +105 are both +10.
        self.assertEqual(movement["home_spread_price"], 10.0)
        self.assertEqual(movement["away_spread_price"], 10.0)
        self.assertEqual(movement["over_price"], -5.0)
        self.assertEqual(movement["under_price"], 15.0)
        self.assertEqual(
            set(movement),
            {
                "home_spread", "away_spread", "total", "home_moneyline",
                "fair_home_ml", "home_spread_price", "away_spread_price",
                "over_price", "under_price",
            },
        )
        # A missing opening value leaves its delta unset.
        partial = movement_since_open({**opening, "home_spread": None, "home_spread_price": None, "home_moneyline": None}, latest)
        self.assertIsNone(partial["home_spread"])
        self.assertIsNone(partial["home_spread_price"])
        self.assertIsNone(partial["fair_home_ml"])
        self.assertEqual(partial["total"], 0.5)

    def test_veto_home_side_on_line_move(self) -> None:
        # Home spread -4 -> -3.5 since open: the home side got cheaper.
        market = self._market(
            opening_home="-4,-110,-190|-2.5,-115,-155|-0.5,115,-140",
            opening_away="4,-110,165|2.5,-105,135|0.5,-135,120",
            latest_home="-3.5,-110,-181|-2.5,-115,-155|-0.5,115,-140",
            latest_away="3.5,-110,158|2.5,-105,135|0.5,-135,120",
        )
        self.assertEqual(market["movement_since_open"]["home_spread"], 0.5)
        self.assertEqual(market["movement_since_open"]["home_spread_price"], 0.0)
        legs = self._legs(0.80, 9.0, 52.0, market=market)
        side = legs["side"]
        self.assertEqual(side["selection"], "PASS")
        self.assertEqual(side["pass_reason"], ADVERSE_MOVE_REASON)
        self.assertIsNone(side["line"])
        self.assertIsNone(side["price"])
        self.assertIsNone(side["ev_per_unit"])
        self.assertEqual(side["confidence_stars"], 1)
        self.assertEqual(side["stake_fraction"], 0.0)
        self.assertEqual(side["stake_units"], 0.0)
        self.assertGreater(side["edge"], self.policy["edge_threshold"])
        self.assertEqual(
            legs["notes"],
            [f"adverse move: {HOME} spread -4 → -3.5 (+0.5 points) since open"],
        )
        # The total leg is judged on its own movement.
        self.assertEqual(legs["total"]["selection"], "Over")
        self.assertIsNone(legs["total"]["pass_reason"])

    def test_veto_away_side_on_line_move(self) -> None:
        # Home spread -3.5 -> -4 since open: the away side now gets more points.
        market = self._market(
            latest_home="-4,-110,-181|-2.5,-115,-155|-0.5,115,-140",
            latest_away="4,-110,158|2.5,-105,135|0.5,-135,120",
        )
        self.assertEqual(market["movement_since_open"]["home_spread"], -0.5)
        self.assertEqual(market["movement_since_open"]["away_spread"], 0.5)
        legs = self._legs(0.30, -6.0, 36.0, market=market)
        self.assertEqual(legs["side"]["selection"], "PASS")
        self.assertEqual(legs["side"]["pass_reason"], ADVERSE_MOVE_REASON)
        self.assertEqual(
            legs["notes"],
            [f"adverse move: {AWAY} spread +3.5 → +4 (+0.5 points) since open"],
        )
        self.assertEqual(legs["total"]["selection"], "Under")
        # The mirror move is steam toward the bet: the away side keeps betting.
        favorable = self._market(
            latest_home="-3,-110,-181|-2.5,-115,-155|-0.5,115,-140",
            latest_away="3,-110,158|2.5,-105,135|0.5,-135,120",
        )
        self.assertEqual(self._legs(0.30, -6.0, 36.0, market=favorable)["side"]["selection"], AWAY)

    def test_veto_side_on_price_move(self) -> None:
        # The default market: the home side opened -110 and is now +100.
        legs = self._legs(0.80, 9.0, 52.0)
        side = legs["side"]
        self.assertEqual(side["selection"], "PASS")
        self.assertEqual(side["pass_reason"], ADVERSE_MOVE_REASON)
        self.assertEqual(
            legs["notes"],
            [f"adverse move: {HOME} spread price -110 → +100 (+10 cents) since open"],
        )
        # Nine cents is not ten.
        market = self._market(
            latest_home="-3.5,-101,-181|-2.5,-115,-155|-0.5,115,-140",
            latest_away="3.5,-119,158|2.5,-105,135|0.5,-135,120",
        )
        self.assertEqual(market["movement_since_open"]["home_spread_price"], 9.0)
        legs = self._legs(0.80, 9.0, 52.0, market=market)
        self.assertEqual(legs["side"]["selection"], HOME)
        self.assertIsNone(legs["side"]["pass_reason"])
        # The away side's price shortened (-110 -> -120): no veto for an away bet.
        self.assertEqual(self._legs(0.30, -6.0, 36.0)["side"]["selection"], AWAY)

    def test_veto_total_on_line_move(self) -> None:
        # Total 45.5 -> 44.5 since open: the Over got cheaper.
        market = self._market(
            opening_home=STEADY_HOME,
            opening_totals="45.5,-110,-110|21.5,-110,-110|7.5,-105,-115",
            latest_totals="44.5,-110,-110|21.5,-110,-110|7.5,-105,-115",
        )
        self.assertEqual(market["movement_since_open"]["total"], -1.0)
        legs = self._legs(0.80, 9.0, 52.0, market=market)
        self.assertEqual(legs["side"]["selection"], HOME)
        self.assertEqual(legs["total"]["selection"], "PASS")
        self.assertEqual(legs["total"]["pass_reason"], ADVERSE_MOVE_REASON)
        self.assertEqual(
            legs["notes"],
            ["adverse move: total 45.5 → 44.5 (-1 points) against the Over since open"],
        )
        # Total 43.5 -> 44.5: the Under got cheaper.
        market = self._market(
            opening_totals="43.5,-110,-110|21.5,-110,-110|7.5,-105,-115",
            latest_totals="44.5,-110,-110|21.5,-110,-110|7.5,-105,-115",
        )
        legs = self._legs(0.30, -6.0, 36.0, market=market)
        self.assertEqual(legs["side"]["selection"], AWAY)
        self.assertEqual(legs["total"]["pass_reason"], ADVERSE_MOVE_REASON)
        self.assertEqual(
            legs["notes"],
            ["adverse move: total 43.5 → 44.5 (+1 points) against the Under since open"],
        )
        # Half a point is under the bar: the default 44 -> 44.5 leaves the Under alone.
        self.assertEqual(self._legs(0.30, -6.0, 36.0)["total"]["selection"], "Under")

    def test_veto_total_on_price_move(self) -> None:
        # Over -110 -> +100 since open.
        market = self._market(
            opening_home=STEADY_HOME,
            latest_totals="44.5,100,-120|21.5,-110,-110|7.5,-105,-115",
        )
        self.assertEqual(market["movement_since_open"]["over_price"], 10.0)
        legs = self._legs(0.80, 9.0, 52.0, market=market)
        self.assertEqual(legs["side"]["selection"], HOME)
        self.assertEqual(legs["total"]["selection"], "PASS")
        self.assertEqual(legs["total"]["pass_reason"], ADVERSE_MOVE_REASON)
        self.assertEqual(
            legs["notes"],
            ["adverse move: Over price -110 → +100 (+10 cents) since open"],
        )
        # Under -110 -> +100 since open.
        market = self._market(
            latest_totals="44.5,-120,100|21.5,-110,-110|7.5,-105,-115",
        )
        legs = self._legs(0.30, -6.0, 36.0, market=market)
        self.assertEqual(legs["side"]["selection"], AWAY)
        self.assertEqual(legs["total"]["pass_reason"], ADVERSE_MOVE_REASON)
        self.assertEqual(
            legs["notes"],
            ["adverse move: Under price -110 → +100 (+10 cents) since open"],
        )

    def test_ev_floor(self) -> None:
        # The Week 1 Seahawks estimate on a market whose home price never
        # moved: the side bets (ev 0.0225) while the Over, a 3.3% edge worth
        # only 0.0194 per unit at -105, sits under the 2% floor.
        market = self._market(opening_home=STEADY_HOME)
        legs = self._legs(0.6259, 3.88, 45.25, market=market)
        side, total = legs["side"], legs["total"]
        self.assertEqual(side["selection"], HOME)
        self.assertEqual(side["ev_per_unit"], 0.0225)
        self.assertIsNone(side["pass_reason"])
        self.assertEqual(total["selection"], "PASS")
        self.assertEqual(total["pass_reason"], EV_FLOOR_REASON)
        self.assertEqual(total["edge"], 0.033)
        self.assertEqual(total["probability"], 0.5222)
        self.assertIsNone(total["ev_per_unit"])
        self.assertEqual(total["stake_units"], 0.0)
        self.assertEqual(legs["notes"], ["ev floor: Over 44.5 (-105) ev +0.019 under 0.020"])
        # A floor of zero lets the same leg through, unchanged.
        loose = dict(self.policy, min_ev_per_unit=0.0)
        legs = self._legs(0.6259, 3.88, 45.25, market=market, policy=loose)
        self.assertEqual(legs["total"]["selection"], "Over")
        self.assertEqual(legs["total"]["ev_per_unit"], 0.0194)
        self.assertEqual(legs["total"]["stake_units"], 0.5)
        self.assertEqual(legs["notes"], [])

    def test_no_veto_without_opening_data(self) -> None:
        market = self._market(
            opening_away=NO_DATA_GROUPS,
            opening_home=NO_DATA_GROUPS,
            opening_totals=NO_DATA_GROUPS,
        )
        self.assertTrue(all(value is None for value in market["movement_since_open"].values()))
        legs = self._legs(0.80, 9.0, 52.0, market=market)
        self.assertEqual(legs["side"]["selection"], HOME)
        self.assertEqual(legs["side"]["price"], 100)
        self.assertIsNone(legs["side"]["pass_reason"])
        self.assertEqual(legs["total"]["selection"], "Over")
        self.assertEqual(legs["notes"], [])

    def test_veto_precedes_floor(self) -> None:
        # With the floor above the Seahawks side's 0.0225, the -110 -> +100
        # move still names the veto; the Over is floored as before.
        strict = dict(self.policy, min_ev_per_unit=0.05)
        legs = self._legs(0.6259, 3.88, 45.25, policy=strict)
        self.assertEqual(legs["side"]["pass_reason"], ADVERSE_MOVE_REASON)
        self.assertEqual(legs["total"]["pass_reason"], EV_FLOOR_REASON)
        self.assertTrue(legs["notes"][0].startswith("adverse move: "))
        self.assertEqual(legs["notes"][1], "ev floor: Over 44.5 (-105) ev +0.019 under 0.050")

    def test_pass_reasons(self) -> None:
        self.assertEqual(ADVERSE_MOVE_REASON, "adverse move")
        self.assertEqual(EV_FLOOR_REASON, "ev floor")
        self.assertEqual(NO_EXPECTATION_REASON, "no positive expectation at the posted price")
        # Sub-threshold edges pass with no reason at all.
        legs = self._legs(0.60, 3.5, 45.0)
        self.assertEqual(legs["side"]["selection"], "PASS")
        self.assertIsNone(legs["side"]["pass_reason"])
        self.assertIsNone(legs["total"]["pass_reason"])
        self.assertEqual(legs["notes"], [])
        # A bet carries None as well.
        legs = self._legs(0.30, -6.0, 36.0)
        self.assertEqual(legs["side"]["selection"], AWAY)
        self.assertIsNone(legs["side"]["pass_reason"])
        # A 5.9% edge that still loses money at -150 on both sides.
        market = self._market(
            latest_home="-3.5,-150,-181|-2.5,-115,-155|-0.5,115,-140",
            latest_away="3.5,-150,158|2.5,-105,135|0.5,-135,120",
        )
        legs = self._legs(0.60, 5.5, 45.0, market=market)
        self.assertEqual(legs["side"]["selection"], "PASS")
        self.assertGreater(legs["side"]["edge"], self.policy["edge_threshold"])
        self.assertEqual(legs["side"]["pass_reason"], NO_EXPECTATION_REASON)
        self.assertEqual(legs["notes"], [NO_EXPECTATION_REASON])

    def test_policy_validation_of_veto_and_floor_keys(self) -> None:
        base = dict(load_registry()["aggregator_policy"])
        keys = (
            "veto_adverse_spread_points",
            "veto_adverse_total_points",
            "veto_adverse_price_cents",
            "min_ev_per_unit",
        )
        for key in keys:
            for bad in (-0.1, True, "10"):
                with self.subTest(key=key, bad=bad):
                    with self.assertRaises(ValueError):
                        aggregator_policy({"aggregator_policy": {**base, key: bad}})
        with self.assertRaises(ValueError):
            aggregator_policy({"aggregator_policy": {**base, "min_ev_per_unit": 1.5}})
        policy = aggregator_policy({"aggregator_policy": base})
        for key in keys:
            self.assertIsInstance(policy[key], float)
        self.assertEqual(policy["veto_adverse_price_cents"], 10.0)
        # Zero is allowed for every knob.
        zeros = aggregator_policy({"aggregator_policy": {**base, **{key: 0 for key in keys}}})
        self.assertEqual([zeros[key] for key in keys], [0.0, 0.0, 0.0, 0.0])


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
        self.assertIn("God Expert Judge v2", captured["system"])
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


class CommitteeKeyTests(unittest.TestCase):
    def setUp(self) -> None:
        self.registry = load_registry()
        self.policy = aggregator_policy(self.registry)

    def _payload(self, game: dict | None = None, rows: list[dict] | None = None) -> dict:
        return build_aggregator_input(
            game or _game(),
            approved_opinions=rows or _committee(),
            finals=[],
            snapshots=[],
            registry=self.registry,
            policy=self.policy,
        )

    def test_key_ignores_the_capture_timestamp(self) -> None:
        base = self._payload()
        self.assertEqual(base["committee_key"], committee_key(base))
        self.assertRegex(base["committee_key"], r"^[0-9a-f]{64}$")
        refetched = _game()
        refetched["latest_captured_at"] = "2026-09-07T03:05:00Z"
        again = self._payload(refetched)
        self.assertNotEqual(again["market"]["latest"]["captured_at"], base["market"]["latest"]["captured_at"])
        self.assertNotEqual(sha256_text(canonical_json(again)), sha256_text(canonical_json(base)))
        self.assertEqual(again["committee_key"], base["committee_key"])
        self.assertEqual(build_judge_request(base)["committee_key"], base["committee_key"])

    def test_key_changes_with_a_voice_or_a_price(self) -> None:
        base = self._payload()
        moved = _game()
        moved[LATEST_HOME_COLUMN] = "-3.5,-105,-181|-2.5,-115,-155|-0.5,115,-140"
        self.assertNotEqual(self._payload(moved)["committee_key"], base["committee_key"])
        rows = _committee() + [
            # A newer default-model row displaces the voice: new opinion id, same numbers.
            _opinion("schedule", model="claude-opus-4-8", probability=0.66, margin=6, away_score=20, home_score=26, generated_at="2026-09-07T02:00:00+00:00")
        ]
        self.assertNotEqual(self._payload(rows=rows)["committee_key"], base["committee_key"])

    def test_inputs_without_a_key_derive_one_for_the_request(self) -> None:
        legacy = self._payload()
        del legacy["committee_key"]
        self.assertEqual(build_judge_request(legacy)["committee_key"], committee_key(legacy))


class ReasonGuardTests(unittest.TestCase):
    """The persisted Week 1 judge responses replay; invented numbers do not."""

    def _week1(self, prefix: str) -> tuple[dict, dict, dict]:
        rules = _fixture(f"{prefix}_rules")
        judge = _fixture(f"{prefix}_judge")
        return rules, judge, rules["input_json"]

    def test_week1_requests_derive_from_the_full_input(self) -> None:
        for prefix in ("sea", "lar"):
            with self.subTest(game=prefix):
                rules, judge, full_input = self._week1(prefix)
                self.assertEqual(sha256_text(canonical_json(full_input)), rules["input_sha256"])
                derived = build_judge_request(full_input)
                self.assertEqual(derived.pop("committee_key"), committee_key(full_input))
                self.assertEqual(derived, judge["input_json"])
                self.assertEqual(sha256_text(canonical_json(derived)), judge["input_sha256"])
                self.assertEqual(judge["input_json"]["aggregator_input_sha256"], rules["input_sha256"])

    def test_week1_judge_responses_validate(self) -> None:
        expert = load_expert("god_judge")
        for prefix in ("sea", "lar"):
            with self.subTest(game=prefix):
                _rules, judge, full_input = self._week1(prefix)
                opinion = normalize_aggregator_opinion(judge["raw_response"], full_input, expert=expert, model="claude-fable-5-1")
                validate_opinion(
                    opinion,
                    away_team=full_input["game"]["away_team"],
                    home_team=full_input["game"]["home_team"],
                    schedule_input=full_input,
                )
                self.assertEqual(opinion["home_win_probability"], judge["home_win_probability"])
                self.assertEqual(opinion["expected_home_margin"], judge["expected_home_margin"])
                self.assertEqual(opinion["predicted_winner"], judge["predicted_winner"])

    def _sea(self) -> tuple[dict, dict]:
        _rules, judge, full_input = self._week1("sea")
        return json.loads(json.dumps(judge["raw_response"])), full_input

    def test_invented_record_is_rejected(self) -> None:
        response, full_input = self._sea()
        first = response["key_reasons"][0]
        self.assertIn("11-4", first["text"])
        first["text"] = first["text"].replace("11-4", "12-4")
        with self.assertRaises(ValueError) as caught:
            normalize_aggregator_opinion(response, full_input, expert=load_expert("god_judge"))
        self.assertIn("12-4", str(caught.exception))

    def test_invented_game_count_is_rejected(self) -> None:
        response, full_input = self._sea()
        response["key_reasons"][0]["text"] += " over 40 games"
        with self.assertRaises(ValueError) as caught:
            normalize_aggregator_opinion(response, full_input, expert=load_expert("god_judge"))
        self.assertIn("40 games", str(caught.exception))
        response, full_input = self._sea()
        response["counterpoints"][0]["text"] += " A 9-game slice agrees."
        with self.assertRaises(ValueError):
            normalize_aggregator_opinion(response, full_input, expert=load_expert("god_judge"))

    def test_structured_numbers_are_not_invented(self) -> None:
        response, full_input = self._sea()
        labels = full_input["judge_view"]["labels"]
        ak_label = next(label for label, voice_id in labels.items() if voice_id == "ak")
        response["counterpoints"] = [
            # The AK voice's projected score, an en dash, the winner-vote split.
            {"voice": ak_label, "text": "Its 21-27 projection implies a cover and an over."},
            {"voice": "Voice D", "text": "The 11–4 non-conference mark is the largest cohort."},
            {"voice": "pool", "text": "A 4-0 winner split leaves no dissent to weigh."},
        ]
        normalize_aggregator_opinion(response, full_input, expert=load_expert("god_judge"))
        # The reference is the request text, a newline, then the derived strings.
        derived = reason_reference_text(build_judge_request(full_input)).split("\n")[-1].split()
        self.assertIn("21-27", derived)
        self.assertIn("4-0", derived)
        self.assertIn("0-0-0", derived)

    def test_decimal_fragments_are_not_records(self) -> None:
        # The first live headless response cited the implied totals as
        # "24.0-20.5" and was rejected for a record "0-20" (2026-09-07).
        response, full_input = self._sea()
        response["counterpoints"] = [
            {"voice": "market", "text": "Fair home ML 0.6197 and implied totals 24.0-20.5 anchor the estimate."},
            {"voice": "pool", "text": "A 0.5-1.0 point move and a 13.5-14 sigma band change nothing; 44.5-45 is the total range."},
        ]
        normalize_aggregator_opinion(response, full_input, expert=load_expert("god_judge"))
        # A record next to a decimal is still a record.
        response["counterpoints"] = [{"voice": "market", "text": "Went 11-4 (.733) at home."}]
        normalize_aggregator_opinion(response, full_input, expert=load_expert("god_judge"))
        response["counterpoints"] = [{"voice": "market", "text": "Went 12-4 (.750) at home."}]
        with self.assertRaises(ValueError):
            normalize_aggregator_opinion(response, full_input, expert=load_expert("god_judge"))

    def test_home_first_projected_score_is_grounded(self) -> None:
        # The second live response wrote the AK voice's 21-27 projection as
        # "27-21" and was rejected (2026-09-07); both orders are derived now.
        response, full_input = self._sea()
        labels = full_input["judge_view"]["labels"]
        ak_label = next(label for label, voice_id in labels.items() if voice_id == "ak")
        response["counterpoints"] = [
            {"voice": ak_label, "text": "Chose Under 44.5 despite a 27-21 projection implying 48."},
        ]
        normalize_aggregator_opinion(response, full_input, expert=load_expert("god_judge"))
        derived = reason_reference_text(build_judge_request(full_input)).split("\n")[-1].split()
        self.assertIn("27-21", derived)
        self.assertIn("21-27", derived)
        # A score no voice projected is still invented.
        response["counterpoints"] = [{"voice": ak_label, "text": "A 27-22 projection."}]
        with self.assertRaises(ValueError):
            normalize_aggregator_opinion(response, full_input, expert=load_expert("god_judge"))

    def test_rams_response_needs_the_vote_split_and_the_record_cohort(self) -> None:
        # "The 2-2 split pool" is the winner-vote split, and "17-8 over 25
        # games" is the cohort the 17-8 record implies ("across 25 home games"
        # in the source). Neither is in the prose within the guard's window.
        _rules, judge, full_input = self._week1("lar")
        request = build_judge_request(full_input)
        bare = canonical_json(request).lower()
        self.assertNotIn("2-2", bare)
        self.assertIsNone(re.search(r"game\w*\W{0,12}\b25\b|\b25\b\W{0,12}game", bare))
        self.assertIn("across 25 home games", bare)
        derived = reason_reference_text(request).split("\n")[-1]
        self.assertIn("2-2", derived.split())
        self.assertIn("25 games", derived)
        cited = json.dumps(judge["raw_response"]["key_reasons"])
        self.assertIn("2-2 split pool", cited)
        self.assertIn("17-8 over 25 games", cited)

    def test_rules_arm_reasons_are_not_guarded(self) -> None:
        for prefix in ("sea", "lar"):
            with self.subTest(game=prefix):
                _rules, _judge, full_input = self._week1(prefix)
                response = rules_arm_response(full_input)
                self.assertTrue(any(re.search(r"\b\d+-\d+\b", item["text"]) for item in response["key_reasons"]))
                normalize_aggregator_opinion(response, full_input, expert=load_expert("god_rules"))


class InputFileTests(unittest.IsolatedAsyncioTestCase):
    """generate_opinion(input_payload=...) persists exactly the shown state."""

    def setUp(self) -> None:
        registry = load_registry()
        self.payload = build_aggregator_input(
            _game(), approved_opinions=approved_opinions(_committee()), finals=[], snapshots=[], registry=registry, policy=aggregator_policy(registry)
        )
        # What --input-file reads back: the --show-input document after a trip through JSON text.
        self.file_payload = json.loads(json.dumps(self.payload, indent=2, sort_keys=True))
        self.request = build_judge_request(self.file_payload)
        labels = [voice["label"] for voice in self.request["voices"]]
        self.response = {
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

    async def _create_fn(self, **_kwargs):
        return SimpleNamespace(content=[SimpleNamespace(text=json.dumps(self.response))])

    async def test_rules_arm_persists_the_file_verbatim(self) -> None:
        store = MemoryStore()
        row = await generate_opinion(
            expert_id="god_rules", game=_game(), history=[], input_payload=self.file_payload, store=store, generation_backend=DETERMINISTIC_BACKEND
        )
        self.assertEqual(row["generation_status"], "valid")
        self.assertEqual(row["input_json"], canonical_json(self.file_payload))
        self.assertEqual(row["input_sha256"], sha256_text(canonical_json(self.payload)))
        self.assertEqual(json.loads(row["input_json"])["committee_key"], self.payload["committee_key"])
        self.assertEqual(store.rows, [row])

    async def test_judge_arm_persists_the_derived_request_and_checks_its_hash(self) -> None:
        store = MemoryStore()
        with self.assertRaises(ValueError):
            await generate_opinion(
                expert_id="god_judge", game=_game(), history=[], input_payload=self.file_payload, store=store, create_fn=self._create_fn,
                generation_backend="claude_headless", generation_effort="max", model="claude-fable-5-1", expected_input_sha256="0" * 64,
            )
        self.assertEqual(store.rows, [])
        row = await generate_opinion(
            expert_id="god_judge", game=_game(), history=[], input_payload=self.file_payload, store=store, create_fn=self._create_fn,
            generation_backend="claude_headless", generation_effort="max", model="claude-fable-5-1",
            expected_input_sha256=sha256_text(canonical_json(self.request)),
        )
        self.assertEqual(row["generation_status"], "valid")
        self.assertEqual(row["input_json"], canonical_json(self.request))
        self.assertEqual(row["generation_backend"], "claude_headless")
        self.assertEqual(row["generation_effort"], "max")
        self.assertEqual(row["model"], "claude-fable-5-1")
        self.assertEqual(json.loads(row["input_json"])["aggregator_input_sha256"], sha256_text(canonical_json(self.file_payload)))
        self.assertEqual(json.loads(row["input_json"])["committee_key"], self.payload["committee_key"])

    async def test_prebuilt_input_must_describe_this_game(self) -> None:
        store = MemoryStore()
        cases = [
            ("another event", _game(event_id="another-event"), "god_rules", self.file_payload),
            ("other teams", _game(home="Denver Broncos"), "god_rules", self.file_payload),
            ("a judge request is not an input", _game(), "god_judge", self.request),
            ("a non-aggregator expert", _game(), "schedule", self.file_payload),
        ]
        for label, game, expert_id, payload in cases:
            with self.subTest(case=label):
                with self.assertRaises(ValueError):
                    await generate_opinion(
                        expert_id=expert_id, game=game, history=[], input_payload=payload, store=store, create_fn=self._create_fn,
                        generation_backend=DETERMINISTIC_BACKEND if expert_id == "god_rules" else "agent_runtime",
                    )
        self.assertEqual(store.rows, [])

    async def test_file_policy_is_used_as_shown(self) -> None:
        shown = json.loads(json.dumps(self.file_payload))
        shown["policy"]["shrink_lambda"] = 0.25
        row = await generate_opinion(
            expert_id="god_rules", game=_game(), history=[], input_payload=shown, store=MemoryStore(), generation_backend=DETERMINISTIC_BACKEND
        )
        self.assertEqual(json.loads(row["input_json"])["policy"]["shrink_lambda"], 0.25)
        self.assertEqual(row["input_sha256"], sha256_text(canonical_json(shown)))


class RegistryTests(unittest.TestCase):
    def test_registry_entries(self) -> None:
        rules = load_expert("god_rules")
        judge = load_expert("god_judge")
        self.assertEqual(rules["mode"], "aggregator")
        self.assertEqual(rules["allowed_backends"], ["deterministic"])
        self.assertEqual(rules["output_schema_version"], 8)
        # v2 of both prompts: the pool discounts shared evidence and splits
        # by market; v1 stays on disk for the rows that hash it.
        self.assertEqual((rules["version"], rules["prompt_version"]), (2, 2))
        self.assertEqual(rules["prompt_path"], "moe/prompts/god_rules/v2.md")
        self.assertIn("God Expert Rules v2", rules["prompt_text"])
        self.assertIn("Evidence overlap", rules["prompt_text"])
        self.assertIn("adverse move", rules["prompt_text"])
        self.assertEqual(judge["mode"], "aggregator_judge")
        self.assertEqual((judge["version"], judge["prompt_version"]), (2, 2))
        self.assertEqual(judge["prompt_path"], "moe/prompts/god_judge/v2.md")
        self.assertIn("God Expert Judge v2", judge["prompt_text"])
        self.assertIn("`overlap`", judge["prompt_text"])
        self.assertEqual(judge["default_model"], "claude-fable-5-1")
        self.assertEqual(judge["allowed_models"], ["claude-fable-5-1"])
        self.assertEqual(
            judge["allowed_backends"], ["agent_runtime", "claude_headless"]
        )
        self.assertEqual(judge["reasoning_effort"], "max")
        self.assertIn("Return exactly one JSON object", judge["prompt_text"])
        registry = load_registry()
        self.assertEqual(
            {
                expert_id: voice_markets(config)
                for expert_id, config in registry["experts"].items()
                if config.get("mode") not in {"aggregator", "aggregator_judge"}
            },
            {
                "ak": ["side", "total"],
                "divisional": ["side"],
                # WP7's rating voice: its total is the league scoring rate.
                "rating_elo": ["side"],
                "schedule": ["side", "total"],
                "win_total": ["side"],
            },
        )
        for expert_id in ("god_rules", "god_judge"):
            self.assertNotIn("markets", registry["experts"][expert_id])
        policy = aggregator_policy(load_registry())
        self.assertEqual(policy["edge_threshold"], 0.03)
        self.assertEqual(policy["shrink_lambda"], 0.5)
        self.assertTrue(math.isclose(policy["kelly_fraction"], 0.25))
        self.assertEqual(policy["version"], 1)
        self.assertEqual(policy["veto_adverse_spread_points"], 0.5)
        self.assertEqual(policy["veto_adverse_total_points"], 1.0)
        self.assertEqual(policy["veto_adverse_price_cents"], 10)
        self.assertEqual(policy["min_ev_per_unit"], 0.02)


class MovementRenderTests(unittest.TestCase):
    def test_rendering_mentions_price_moves_and_policy_notes(self) -> None:
        registry = load_registry()
        # Since the total pool holds only the total-informed voices (schedule
        # and ak: 46 and 48), the shrunk total is 45.75 and the Over clears
        # the 2% floor at 0.048; a 5% floor keeps the floor note on show.
        policy = dict(aggregator_policy(registry), min_ev_per_unit=0.05)
        payload = build_aggregator_input(
            _game(), approved_opinions=_committee(), finals=[], snapshots=[], registry=registry, policy=policy
        )
        # The knobs ride in the input, so they are hash-bound like the rest.
        self.assertEqual(payload["policy"]["min_ev_per_unit"], 0.05)
        self.assertEqual(payload["policy"]["veto_adverse_price_cents"], 10.0)
        self.assertEqual(payload["market"]["movement_since_open"]["home_spread_price"], 10.0)
        self.assertEqual(payload["feature_block"]["shrunk"]["projected_total"], 45.75)
        opinion = normalize_aggregator_opinion(rules_arm_response(payload), payload, expert=load_expert("god_rules"))
        validate_opinion(opinion, away_team=AWAY, home_team=HOME, schedule_input=payload)
        side = json.loads(opinion["side_pick_json"])
        total = json.loads(opinion["total_pick_json"])
        self.assertEqual(side["pass_reason"], ADVERSE_MOVE_REASON)
        self.assertEqual(total["pass_reason"], EV_FLOOR_REASON)
        self.assertEqual(opinion["pick_side"], "PASS | PASS")
        self.assertIn(
            f"Policy: adverse move: {HOME} spread price -110 → +100 (+10 cents) since open.",
            opinion["counterarguments"],
        )
        self.assertIn("Policy: ev floor: Over 44.5 (-105) ev +0.048 under 0.050.", opinion["counterarguments"])
        price_text = (
            f"{HOME} spread price +10 cents, {AWAY} spread price -10 cents, "
            "over price +5 cents, under price -5 cents"
        )
        self.assertIn(
            f"Line movement since open: home spread +0 points, total +0.5 points; {price_text}.",
            opinion["counterarguments"],
        )
        self.assertIn(f"- Movement since open: home spread 0.0, total 0.5; {price_text}", opinion["full_opinion"])
        self.assertIn("Policy: adverse move:", opinion["full_opinion"])

    def test_rendering_is_unchanged_without_price_moves(self) -> None:
        registry = load_registry()
        policy = aggregator_policy(registry)
        game = _game(
            opening_away="3.5,-120,165|2.5,-105,135|0.5,-135,120",
            opening_home=STEADY_HOME,
            opening_totals="44,-105,-115|21.5,-110,-110|7.5,-105,-115",
        )
        payload = build_aggregator_input(
            game, approved_opinions=_committee(), finals=[], snapshots=[], registry=registry, policy=policy
        )
        movement = payload["market"]["movement_since_open"]
        self.assertEqual([movement[key] for key in ("home_spread_price", "away_spread_price", "over_price", "under_price")], [0.0, 0.0, 0.0, 0.0])
        opinion = normalize_aggregator_opinion(rules_arm_response(payload), payload, expert=load_expert("god_rules"))
        self.assertIn("Line movement since open: home spread +0 points, total +0.5 points.", opinion["counterarguments"])
        self.assertIn("- Movement since open: home spread 0.0, total 0.5\n", opinion["full_opinion"])
        self.assertNotIn("cents", opinion["full_opinion"])


FIXTURES = Path(__file__).resolve().parent / "fixtures" / "god_week1"


def _fixture(name: str) -> dict:
    return json.loads((FIXTURES / f"{name}.json").read_text(encoding="utf-8"))


def _persisted_leg(row: dict, kind: str) -> dict:
    # The fixtures hold the pick columns already parsed; the sheet holds strings.
    value = row[f"{kind}_pick_json"]
    return json.loads(value) if isinstance(value, str) else dict(value)


class Week1ReplayTests(unittest.TestCase):
    """The persisted Week 1 rules rows replayed under the veto and the floor."""

    def setUp(self) -> None:
        self.policy = aggregator_policy(load_registry())

    def _replay(self, name: str, policy: dict | None = None) -> tuple[dict, dict]:
        row = _fixture(name)
        payload = row["input_json"]
        self.assertEqual(sha256_text(canonical_json(payload)), row["input_sha256"])
        for key in ("sigma_margin", "sigma_total", "shrink_lambda", "edge_threshold"):
            self.assertEqual(self.policy[key], payload["policy"][key])
        estimate = rules_arm_response(payload)
        for key in ("home_win_probability", "expected_home_margin", "projected_total"):
            self.assertEqual(estimate[key], row["raw_response"][key])
        game = payload["game"]
        legs = apply_policy(
            home_win_probability=estimate["home_win_probability"],
            expected_home_margin=estimate["expected_home_margin"],
            projected_total=estimate["projected_total"],
            market=payload["market"],
            policy=policy if policy is not None else self.policy,
            away_team=game["away_team"],
            home_team=game["home_team"],
        )
        return row, legs

    def test_persisted_movement_is_reproduced(self) -> None:
        for name in ("sea_rules", "lar_rules"):
            with self.subTest(name=name):
                market = _fixture(name)["input_json"]["market"]
                persisted = market["movement_since_open"]
                self.assertNotIn("home_spread_price", persisted)
                recomputed = movement_since_open(market["opening"], market["latest"])
                self.assertEqual({key: recomputed[key] for key in persisted}, persisted)

    def test_seahawks_side_vetoed_and_over_floored(self) -> None:
        row, legs = self._replay("sea_rules")
        self.assertEqual(row["home_win_probability"], 0.6259)
        self.assertEqual(row["expected_home_margin"], 3.88)
        side, total = legs["side"], legs["total"]
        self.assertEqual(side["selection"], "PASS")
        self.assertEqual(side["pass_reason"], ADVERSE_MOVE_REASON)
        self.assertEqual(side["edge"], 0.0329)
        self.assertEqual(side["probability"], 0.5112)
        self.assertEqual(total["selection"], "PASS")
        self.assertEqual(total["pass_reason"], EV_FLOOR_REASON)
        self.assertEqual(total["edge"], 0.033)
        self.assertEqual(
            legs["notes"],
            [
                "adverse move: Seattle Seahawks spread price -110 → +100 (+10 cents) since open",
                "ev floor: Over 44.5 (-105) ev +0.019 under 0.020",
            ],
        )
        market = row["input_json"]["market"]
        self.assertEqual(movement_since_open(market["opening"], market["latest"])["home_spread_price"], 10.0)

    def test_rams_side_still_bets(self) -> None:
        row, legs = self._replay("lar_rules")
        self.assertEqual(row["home_win_probability"], 0.581)
        self.assertEqual(row["expected_home_margin"], 2.0)
        side, total = legs["side"], legs["total"]
        self.assertEqual(side["selection"], "San Francisco 49ers")
        self.assertEqual(side["line"], 3.5)
        self.assertEqual(side["price"], -110)
        self.assertEqual(side["edge"], 0.0442)
        self.assertEqual(side["ev_per_unit"], 0.039)
        self.assertEqual(side["stake_units"], 1.1)
        self.assertEqual(side["confidence_stars"], 1)
        self.assertIsNone(side["pass_reason"])
        self.assertEqual(total["selection"], "PASS")
        self.assertIsNone(total["pass_reason"])
        self.assertEqual(total["edge"], 0.0073)
        self.assertEqual(legs["notes"], [])

    def test_persisted_legs_reproduce_without_veto_and_floor(self) -> None:
        loose = dict(
            self.policy,
            veto_adverse_spread_points=1e9,
            veto_adverse_total_points=1e9,
            veto_adverse_price_cents=1e9,
            min_ev_per_unit=0.0,
        )
        for name in ("sea_rules", "lar_rules"):
            with self.subTest(name=name):
                row, legs = self._replay(name, policy=loose)
                self.assertEqual(legs["notes"], [])
                for kind in ("side", "total"):
                    leg = dict(legs[kind])
                    self.assertIn("pass_reason", leg)
                    del leg["pass_reason"]
                    self.assertEqual(leg, _persisted_leg(row, kind))


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
        # The default market opens the home side at -110 and prices it at +100
        # now, which the market-move veto reads as the market leaving the
        # Seahawks; a steady opening price keeps the mean's side leg a bet.
        self.payload = build_aggregator_input(
            _game(opening_home=STEADY_HOME), approved_opinions=committee, finals=[], snapshots=[], registry=self.registry, policy=self.policy
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


# Two voices reciting one table: the same records, the same numbers.
SHARED_TABLE = [
    "Seattle non-conference: 11-4, 73.3% win rate (15 games)",
    "New England non-conference: 6-9, 40.0% win rate (15 games)",
]


def _tuples(voice: dict) -> set[tuple[int, int, int, int]]:
    return {tuple(item) for item in voice_evidence(voice)["tuples"]}


class EvidenceTests(unittest.TestCase):
    """The record-tuple extractor, pinned on the Week 1 fixture texts."""

    def test_week1_seahawks_pair_shares_four_tuples(self) -> None:
        voices = {voice["voice_id"]: voice for voice in _fixture("sea_rules")["input_json"]["voices"]}
        divisional, schedule = _tuples(voices["divisional"]), _tuples(voices["schedule"])
        self.assertEqual(
            divisional,
            {(11, 4, 0, 15), (6, 9, 0, 15), (23, 10, 0, 33), (13, 20, 0, 33), (10, 8, 0, 18), (9, 9, 0, 18)},
        )
        self.assertEqual(len(schedule), 14)
        self.assertEqual(divisional & schedule, {(11, 4, 0, 15), (6, 9, 0, 15), (23, 10, 0, 33), (13, 20, 0, 33)})
        # "14-11 (... over 25 games) ... 19-7 (.7308)": the count belongs to
        # the nearer record, so 19-7 falls back to its own 26.
        self.assertIn((14, 11, 0, 25), schedule)
        self.assertIn((19, 7, 0, 26), schedule)
        # Known limitation: a scoreline ("won by Seattle 23-20") reads as a record.
        self.assertIn((23, 20, 0, 43), schedule)
        self.assertEqual(_tuples(voices["ak"]), set())
        self.assertEqual(_tuples(voices["win_total"]), {(26, 23, 0, 49), (9, 12, 0, 21)})
        overlap = evidence_overlap(list(voices.values()))
        self.assertEqual(overlap["divisional"]["schedule"], 0.25)  # 4 shared of 16 distinct
        self.assertEqual(overlap["schedule"]["divisional"], 0.25)
        self.assertEqual(overlap["ak"], {"divisional": 0.0, "schedule": 0.0, "win_total": 0.0})
        self.assertNotIn("divisional", overlap["divisional"])  # no diagonal

    def test_week1_rams_pair_shares_two_tuples(self) -> None:
        voices = {voice["voice_id"]: voice for voice in _fixture("lar_rules")["input_json"]["voices"]}
        divisional, schedule = _tuples(voices["divisional"]), _tuples(voices["schedule"])
        self.assertEqual((len(divisional), len(schedule)), (9, 11))
        # The head-to-head 4-2 over 6 is the same evidence; the 2-1 over 3 is
        # two different cohorts that happen to share a record.
        self.assertEqual(divisional & schedule, {(4, 2, 0, 6), (2, 1, 0, 3)})
        self.assertEqual(_tuples(voices["win_total"]), {(9, 12, 0, 21), (1, 2, 0, 3)})
        overlap = evidence_overlap(list(voices.values()))
        self.assertEqual(overlap["divisional"]["schedule"], 0.1111)
        self.assertEqual(overlap["divisional"]["win_total"], 0.1)
        self.assertEqual(overlap["schedule"]["win_total"], 0.0)

    def test_count_assignment_dedupe_and_cohorts(self) -> None:
        evidence = extract_evidence(
            [
                "Seattle's overall home-role record (as_home) is its weaker split at 14-11 (.5600, +1.60 over 25 games), below its road split of 19-7 (.7308, +4.62).",
                "September samples (11 games each) show Seattle 8-3 (.7273) versus New England 4-7 (.3636).",
                "home teams went just 9-12-0 in 21 games",
                "no record here, only 7 of 10 games under",
                "Seattle 1-2 (3 games) and New England also 1-2 (3 games)",
            ]
        )
        self.assertEqual(
            evidence["tuples"],
            [[14, 11, 0, 25], [19, 7, 0, 26], [8, 3, 0, 11], [4, 7, 0, 11], [9, 12, 0, 21], [1, 2, 0, 3]],
        )
        self.assertEqual(len(evidence["cohorts"]), len(evidence["tuples"]))
        self.assertEqual(evidence["cohorts"][0], "its weaker split at")
        self.assertEqual(evidence["cohorts"][2], "games each show seattle")
        self.assertEqual(evidence["cohorts"][4], "home teams went just")
        self.assertEqual(extract_evidence([]), {"tuples": [], "cohorts": []})
        self.assertEqual(extract_evidence(["nothing numeric"])["tuples"], [])
        # Decimal fragments are not records: "24.0-20.5" is two implied
        # totals, "0.5-1.0" a range; the record beside them still counts.
        self.assertEqual(
            extract_evidence(["implied totals 24.0-20.5; a 0.5-1.0 move; went 11-4 (15 games)"])["tuples"],
            [[11, 4, 0, 15]],
        )
        json.dumps(evidence)

    def test_overlap_of_voices_without_records_is_zero(self) -> None:
        voices = [
            {"voice_id": "a", "supporting_factors": {"items": []}, "counterarguments": {"items": []}},
            {"voice_id": "b", "supporting_factors": {"items": ["prose only"]}, "counterarguments": {"items": []}},
            {"voice_id": "c", "evidence": {"tuples": [[1, 2, 0, 3]], "cohorts": ["x"]}},
        ]
        self.assertEqual(evidence_overlap(voices), {"a": {"b": 0.0, "c": 0.0}, "b": {"a": 0.0, "c": 0.0}, "c": {"a": 0.0, "b": 0.0}})
        # A persisted evidence block wins over re-extraction.
        self.assertEqual(voice_evidence(voices[2])["tuples"], [[1, 2, 0, 3]])

    def test_voice_markets_validation(self) -> None:
        self.assertEqual(MARKETS, ("side", "total"))
        self.assertEqual(voice_markets({}), ["side", "total"])
        self.assertEqual(voice_markets({"markets": ["total", "side"]}), ["side", "total"])
        self.assertEqual(voice_markets({"markets": ["total"]}), ["total"])
        for bad in ([], ["side", "side"], ["spread"], "side", ["side", "props"]):
            with self.subTest(bad=bad):
                with self.assertRaises(ValueError):
                    voice_markets({"markets": bad})

    def test_voice_from_row_carries_markets_and_full_evidence(self) -> None:
        registry = load_registry()
        policy = aggregator_policy(registry)
        # Twelve factors: the voice keeps five, the evidence sees all twelve.
        factors = [f"cohort {index}: {index + 1}-{index + 2} ({2 * index + 3} games)" for index in range(12)]
        rows = [
            _opinion("divisional", model="claude-opus-4-8", probability=0.6, margin=3, away_score=20, home_score=23, factors=factors, counters=[])
        ]
        payload = build_aggregator_input(_game(), approved_opinions=rows, finals=[], snapshots=[], registry=registry, policy=policy)
        voice = payload["voices"][0]
        self.assertEqual(voice["markets"], ["side"])
        self.assertEqual(len(voice["supporting_factors"]["items"]), policy["factor_limit"])
        self.assertEqual(len(voice["evidence"]["tuples"]), 12)
        self.assertEqual(voice["evidence"]["tuples"][11], [12, 13, 0, 25])


class OverlapWeightTests(unittest.TestCase):
    """Overlap-discounted weights and the per-market pools."""

    def setUp(self) -> None:
        self.registry = load_registry()
        self.policy = aggregator_policy(self.registry)

    def _payload(self, rows: list[dict], **game_kwargs) -> dict:
        return build_aggregator_input(
            _game(**game_kwargs), approved_opinions=rows, finals=[], snapshots=[], registry=self.registry, policy=self.policy
        )

    def _duplicates(self) -> list[dict]:
        return [
            _opinion("divisional", model="claude-opus-4-8", probability=0.66, margin=6, away_score=20, home_score=26, factors=SHARED_TABLE, counters=[]),
            _opinion("schedule", model="claude-opus-4-8", probability=0.66, margin=6, away_score=20, home_score=26, factors=SHARED_TABLE, counters=[]),
            _opinion("win_total", model="claude-opus-4-8", probability=0.57, margin=1, away_score=23, home_score=24, factors=["no records"], counters=[]),
        ]

    def test_duplicate_voices_are_discounted_by_rank(self) -> None:
        feature = self._payload(self._duplicates())["feature_block"]
        self.assertEqual(feature["overlap"]["divisional"]["schedule"], 1.0)
        self.assertEqual(feature["overlap"]["schedule"]["win_total"], 0.0)
        self.assertEqual(feature["hedge_weights"], {"divisional": 1.0, "schedule": 1.0, "win_total": 1.0})
        # Ranked by id: divisional keeps its weight, schedule (the recital)
        # is divided by 1 + 1.0; a voice with no records keeps 1.0. The pair
        # therefore pools as 1.5 voices, not the roadmap's "about one".
        self.assertEqual(feature["weights"], {"divisional": 1.0, "schedule": 0.5, "win_total": 1.0})
        self.assertEqual(feature["pool"]["expected_home_margin"], 4.0)  # (6 + 6·0.5 + 1) / 2.5
        self.assertAlmostEqual(feature["pool"]["home_win_probability"], (0.66 + 0.33 + 0.57) / 2.5, places=4)
        self.assertEqual(overlap_adjusted_weights({"a": 1.0, "b": 1.0, "c": 1.0}, {"a": {"b": 1.0, "c": 1.0}, "b": {"a": 1.0, "c": 1.0}, "c": {"a": 1.0, "b": 1.0}}), {"a": 1.0, "b": 0.5, "c": 0.3333})
        self.assertEqual(overlap_adjusted_weights({"b": 2.0, "a": 0.5}, {"a": {"b": 0.25}, "b": {"a": 0.25}}), {"a": 0.5, "b": 1.6})

    def test_rules_arm_names_the_largest_overlap(self) -> None:
        payload = self._payload(self._duplicates())
        response = rules_arm_response(payload)
        self.assertEqual(
            response["counterpoints"][-1],
            {
                "voice": "pool",
                "text": "Divisional Expert and Schedule Expert cite the same records (overlap 1.00); Schedule Expert pools at weight 0.5 after the discount.",
            },
        )
        self.assertIn("weight 0.5", next(item["text"] for item in response["key_reasons"] if item["voice"] == "schedule"))
        opinion = normalize_aggregator_opinion(response, payload, expert=load_expert("god_rules"))
        validate_opinion(opinion, away_team=AWAY, home_team=HOME, schedule_input=payload)
        self.assertIn(
            "Pool: Divisional Expert and Schedule Expert cite the same records (overlap 1.00); Schedule Expert pools at weight 0.5 after the discount.",
            opinion["counterarguments"],
        )
        self.assertIn("weight 0.5; markets side+total; overlap 1.00", opinion["full_opinion"])
        self.assertIn("(side pool 3 voices, total pool 1)", opinion["full_opinion"])
        summary = json.loads(opinion["calibration_summary_json"])
        self.assertEqual(summary["overlap"]["divisional"]["schedule"], 1.0)
        self.assertEqual(summary["markets"]["total"], ["schedule"])
        # No overlap, no counterpoint: the default committee cites no records.
        plain = rules_arm_response(self._payload(_committee()))
        self.assertEqual([item["voice"] for item in plain["counterpoints"]], ["market"])

    def test_relevance_masks_split_the_pools(self) -> None:
        feature = self._payload(_committee())["feature_block"]
        self.assertEqual(feature["markets"], {"side": ["ak", "divisional", "schedule", "win_total"], "total": ["ak", "schedule"]})
        # Side pool: all four; total pool: schedule 46 and ak 48 only.
        self.assertAlmostEqual(feature["pool"]["home_win_probability"], (0.66 + 0.70 + 0.57 + 0.62) / 4, places=4)
        self.assertEqual(feature["pool"]["expected_home_margin"], 4.5)
        self.assertEqual(feature["pool"]["projected_total"], 47.0)
        self.assertEqual(feature["shrunk"]["projected_total"], 45.75)
        self.assertEqual(feature["dispersion"]["projected_total"], {"min": 46.0, "max": 48.0, "range": 2.0})
        self.assertEqual(feature["dispersion"]["home_winner_votes"], 4)
        self.assertAlmostEqual(
            feature["pool"]["p_over"],
            (over_probability(46, 44.5, 13.5) + over_probability(48, 44.5, 13.5)) / 2,
            places=3,
        )

    def test_empty_total_pool_takes_the_market_total(self) -> None:
        rows = [_opinion("divisional", model="claude-opus-4-8", probability=0.70, margin=5, away_score=19, home_score=24)]
        payload = self._payload(rows)
        feature = payload["feature_block"]
        self.assertEqual(feature["markets"], {"side": ["divisional"], "total": []})
        self.assertIsNone(feature["pool"]["projected_total"])
        self.assertIsNone(feature["pool"]["p_over"])
        self.assertIsNone(feature["dispersion"]["projected_total"])
        self.assertEqual(feature["pool"]["expected_home_margin"], 5.0)
        self.assertEqual(feature["shrunk"]["projected_total"], 44.5)  # the market total
        # With the total at the line, p(over) is 0.5 and the only edge left
        # is the juice asymmetry: 0.5 - 0.4892 on the -105/-115 total.
        self.assertEqual(feature["edges_if_shrunk"]["over"], 0.0108)
        response = rules_arm_response(payload)
        self.assertEqual(response["projected_total"], 44.5)
        self.assertIn("total —", response["key_reasons"][0]["text"])
        opinion = normalize_aggregator_opinion(response, payload, expert=load_expert("god_rules"))
        validate_opinion(opinion, away_team=AWAY, home_team=HOME, schedule_input=payload)
        self.assertEqual(json.loads(opinion["total_pick_json"])["selection"], "PASS")
        self.assertIn("No voice informs the total market; that pool is empty and the blend takes the market total.", opinion["no_signal_factors"])
        self.assertIn("total — (side pool 1 voices, total pool 0)", opinion["full_opinion"])
        request = build_judge_request(payload)
        self.assertEqual(request["feature_block"]["markets"], {"side": ["Voice A"], "total": []})
        self.assertEqual(request["feature_block"]["overlap"], {"Voice A": {}})

    def test_judge_request_relabels_the_new_fields(self) -> None:
        payload = self._payload(self._duplicates())
        request = build_judge_request(payload)
        labels = payload["judge_view"]["labels"]
        label_of = {voice_id: label for label, voice_id in labels.items()}
        feature = request["feature_block"]
        self.assertEqual(set(feature["overlap"]), set(labels))
        self.assertEqual(feature["overlap"][label_of["divisional"]][label_of["schedule"]], 1.0)
        self.assertEqual(feature["hedge_weights"], {label: 1.0 for label in labels})
        self.assertEqual(feature["weights"][label_of["schedule"]], 0.5)
        # Membership lists follow label order, never the alphabetical id order.
        self.assertEqual(feature["markets"]["side"], ["Voice A", "Voice B", "Voice C"])
        self.assertEqual(feature["markets"]["total"], [label_of["schedule"]])
        by_label = {voice["label"]: voice for voice in request["voices"]}
        self.assertEqual(by_label[label_of["schedule"]]["markets"], ["side", "total"])
        self.assertEqual(by_label[label_of["schedule"]]["pool_weight"], 0.5)
        self.assertEqual(by_label[label_of["schedule"]]["hedge_weight"], 1.0)
        self.assertEqual(by_label[label_of["divisional"]]["markets"], ["side"])
        new_fields = json.dumps(
            {
                "feature": {key: feature[key] for key in ("overlap", "hedge_weights", "markets", "weights")},
                "voices": [{key: voice[key] for key in ("markets", "hedge_weight", "pool_weight")} for voice in request["voices"]],
            }
        )
        for leak in ("divisional", "schedule", "win_total", "ak", "Expert", "opinion", "claude"):
            self.assertNotIn(leak, new_fields)
        self.assertNotIn("evidence", json.dumps(request))
        # The request shape is stable across rebuilds.
        self.assertEqual(build_judge_request(self._payload(self._duplicates())), request)

    def test_legacy_input_request_has_no_new_fields(self) -> None:
        # Byte-identity of the Week 1 requests is pinned in ReasonGuardTests;
        # this pins the mechanism: nothing is added that the input lacks.
        for name in ("sea_rules", "lar_rules"):
            with self.subTest(name=name):
                request = build_judge_request(_fixture(name)["input_json"])
                for key in ("overlap", "hedge_weights", "markets"):
                    self.assertNotIn(key, request["feature_block"])
                for voice in request["voices"]:
                    self.assertNotIn("markets", voice)
                    self.assertNotIn("hedge_weight", voice)
                    self.assertNotIn("evidence", voice)


class Week1OverlapReplayTests(unittest.TestCase):
    """The persisted Week 1 voices re-pooled under the discount and the masks.

    The roadmap expected the Seahawks pool margin to move from +4.2 toward
    +3.9 and the Rams side edge from 4.4% toward 3.2%. Under the formula as
    specified (Jaccard over record tuples; a voice divided by one plus its
    overlap with the voices ranked before it) the moves are much smaller,
    because the divisional and schedule voices share only four of sixteen
    distinct tuples on the Seahawks (overlap 0.25) and two of eighteen on the
    Rams (0.11); the roadmap's figures need the pair to pool as about one
    voice. These tests pin the arithmetic and the direction.
    """

    def _replay(self, name: str) -> tuple[dict, dict]:
        payload = _fixture(name)["input_json"]
        registry = load_registry()
        voices = json.loads(json.dumps(payload["voices"]))
        for voice in voices:
            self.assertNotIn("markets", voice)
            self.assertNotIn("evidence", voice)
            voice["markets"] = voice_markets(registry["experts"][voice["voice_id"]])
        persisted = payload["feature_block"]
        weighting = {"weights": persisted["weights"], "active": persisted["weights_active"], "mean_brier": None}
        feature = build_feature_block(voices, payload["market"], payload["policy"], weighting, home_team=payload["game"]["home_team"])
        self.assertEqual(feature["hedge_weights"], persisted["weights"])
        return persisted, feature

    def test_seahawks_pool_margin(self) -> None:
        persisted, feature = self._replay("sea_rules")
        self.assertEqual(persisted["pool"]["expected_home_margin"], 4.25)
        self.assertEqual(persisted["weights"], {"ak": 1.0, "divisional": 1.0, "schedule": 1.0, "win_total": 1.0})
        self.assertEqual(feature["overlap"]["divisional"]["schedule"], 0.25)
        self.assertEqual(feature["weights"], {"ak": 1.0, "divisional": 1.0, "schedule": 0.8, "win_total": 1.0})
        self.assertEqual(feature["markets"], {"side": ["ak", "divisional", "schedule", "win_total"], "total": ["ak", "schedule"]})
        # (6 + 5 + 5·0.8 + 1) / 3.8
        self.assertEqual(feature["pool"]["expected_home_margin"], 4.21)
        self.assertLess(feature["pool"]["expected_home_margin"], persisted["pool"]["expected_home_margin"])
        self.assertEqual(feature["pool"]["home_win_probability"], 0.6258)
        self.assertEqual(feature["pool"]["projected_total"], 47.11)  # (46·0.8 + 48) / 1.8
        self.assertEqual(feature["shrunk"]["expected_home_margin"], 3.86)
        self.assertEqual(feature["shrunk"]["projected_total"], 45.81)
        self.assertEqual(feature["edges_if_shrunk"]["home_cover"], 0.0322)
        self.assertEqual(persisted["edges_if_shrunk"]["home_cover"], 0.0328)
        self.assertEqual(feature["edges_if_shrunk"]["over"], 0.0493)

    def test_rams_side_edge(self) -> None:
        persisted, feature = self._replay("lar_rules")
        self.assertEqual(persisted["edges_if_shrunk"]["home_cover"], -0.0442)
        self.assertEqual(feature["overlap"]["divisional"]["schedule"], 0.1111)
        self.assertEqual(feature["overlap"]["divisional"]["win_total"], 0.1)
        self.assertEqual(feature["weights"], {"ak": 1.0, "divisional": 1.0, "schedule": 0.9, "win_total": 0.9091})
        # (4 - 2 - 2·0.9 + 2·0.9091) / 3.8091
        self.assertEqual(feature["pool"]["expected_home_margin"], 0.53)
        self.assertEqual(feature["shrunk"]["expected_home_margin"], 2.01)
        self.assertEqual(feature["edges_if_shrunk"]["home_cover"], -0.0438)
        self.assertLess(abs(feature["edges_if_shrunk"]["home_cover"]), abs(persisted["edges_if_shrunk"]["home_cover"]))
        self.assertEqual(feature["pool"]["projected_total"], 47.63)  # ak 50 and schedule 45 at 0.9
        self.assertEqual(feature["edges_if_shrunk"]["over"], -0.0162)


class EnsembleTests(unittest.TestCase):
    """The judge ensemble (WP9): the shared coherence step, the mean row, its block."""

    def setUp(self) -> None:
        registry = load_registry()
        self.policy = aggregator_policy(registry)
        self.payload = build_aggregator_input(
            _game(), approved_opinions=_committee(), finals=[], snapshots=[], registry=registry, policy=self.policy
        )
        self.request = build_judge_request(self.payload)
        self.labels = [voice["label"] for voice in self.request["voices"]]
        self.fair_home = float(self.payload["market"]["fair"]["home_ml"])
        self.expert = load_expert("god_judge")

    def _sample(self, opinion_id: str, probability: float, margin: float, total: float) -> dict:
        return {
            "opinion_id": opinion_id,
            "response": {
                "home_win_probability": probability,
                "expected_home_margin": margin,
                "projected_total": total,
                "key_reasons": [
                    {"voice": self.labels[0], "text": f"Sample {opinion_id} rests on a broad cohort."},
                    {"voice": "market", "text": "The pool sits close to fair."},
                ],
                "counterpoints": [{"voice": self.labels[1], "text": f"Sample {opinion_id} leans on one meeting."}],
                "discarded_considerations": [f"Sample {opinion_id}: injuries are not in the input."],
            },
        }

    def _three(self) -> list[dict]:
        return [self._sample("s1", 0.58, 2.0, 44.0), self._sample("s2", 0.64, 4.0, 46.0), self._sample("s3", 0.61, 3.0, 45.0)]

    def test_coherent_estimate(self) -> None:
        self.assertEqual(coherent_estimate(0.61, 3.0, fair_home=0.62), (0.61, 3.0, []))
        self.assertEqual(coherent_estimate(0.5, 2.0, fair_home=0.62), (0.505, 0.5, [FENCE_NOTE]))
        self.assertEqual(coherent_estimate(0.6, 0.0, fair_home=0.4), (0.495, -0.5, [FENCE_NOTE]))
        self.assertEqual(coherent_estimate(0.6, -2.0, fair_home=0.62), (0.6, 0.5, [SIGN_NOTE]))
        self.assertEqual(coherent_estimate(0.4, 2.0, fair_home=0.62), (0.4, -0.5, [SIGN_NOTE]))
        self.assertEqual(FENCE_NOTE, "Blend sat exactly on the fence; the market favorite breaks the tie.")
        self.assertTrue(SIGN_NOTE.startswith("Pooled probability and pooled margin disagreed in sign"))
        # The rules arm runs through it: the persisted Week 1 numbers and notes reproduce.
        for name in ("sea_rules", "lar_rules"):
            with self.subTest(name=name):
                row = _fixture(name)
                response = rules_arm_response(row["input_json"])
                for key in ("home_win_probability", "expected_home_margin", "projected_total", "discarded_considerations"):
                    self.assertEqual(response[key], row["raw_response"][key])
        clamped = json.loads(json.dumps(self.payload))
        clamped["feature_block"]["shrunk"]["expected_home_margin"] = -2.0
        self.assertEqual(rules_arm_response(clamped)["discarded_considerations"], [SIGN_NOTE])

    def test_ensemble_means_and_closest_sample(self) -> None:
        samples = self._three()
        response = ensemble_response(samples, fair_home=self.fair_home, size=3)
        self.assertEqual((response["home_win_probability"], response["expected_home_margin"], response["projected_total"]), (0.61, 3.0, 45.0))
        self.assertEqual(response["key_reasons"], samples[2]["response"]["key_reasons"])
        self.assertEqual(response["counterpoints"], samples[2]["response"]["counterpoints"])
        self.assertEqual(response["discarded_considerations"], ["Sample s3: injuries are not in the input."])
        self.assertEqual(
            response["ensemble"],
            {
                "size": 3,
                "valid": 3,
                "samples": ["s1", "s2", "s3"],
                "estimates": [[0.58, 2.0, 44.0], [0.64, 4.0, 46.0], [0.61, 3.0, 45.0]],
                "reasons_from": "s3",
                "rule": ENSEMBLE_RULE,
            },
        )
        self.assertEqual(
            set(response),
            {"home_win_probability", "expected_home_margin", "projected_total", "key_reasons", "counterpoints", "discarded_considerations", "ensemble"},
        )
        # Copies, not the sample's own lists.
        response["key_reasons"][0]["text"] = "edited"
        self.assertNotEqual(samples[2]["response"]["key_reasons"][0]["text"], "edited")
        # The means round like a response: 4, 2, 2 decimals.
        response = ensemble_response([self._sample("a", 0.6123, 2.333, 44.333), self._sample("b", 0.6001, 2.111, 44.111)], fair_home=0.6, size=2)
        self.assertEqual((response["home_win_probability"], response["expected_home_margin"], response["projected_total"]), (0.6062, 2.22, 44.22))

    def test_closest_sample_tie_breaks(self) -> None:
        # Two samples sit at equal distance on every number: call order wins.
        pair = [self._sample("first", 0.58, 2.0, 44.0), self._sample("second", 0.62, 2.0, 44.0)]
        self.assertEqual(ensemble_response(pair, fair_home=0.6, size=2)["ensemble"]["reasons_from"], "first")
        # |dp| ties between the third and fourth (0.01 each); the margin decides.
        four = [self._sample("a", 0.58, 2.0, 44.0), self._sample("b", 0.62, 2.0, 44.0), self._sample("third", 0.59, 2.5, 44.0), self._sample("fourth", 0.61, 1.0, 44.0)]
        self.assertEqual(ensemble_response(four, fair_home=0.6, size=4)["ensemble"]["reasons_from"], "third")
        # The margins tie too (one point each side of the mean); the total decides.
        four = [self._sample("a", 0.58, 2.0, 44.0), self._sample("b", 0.62, 2.0, 44.0), self._sample("third", 0.59, 3.0, 45.5), self._sample("fourth", 0.61, 1.0, 44.5)]
        self.assertEqual(ensemble_response(four, fair_home=0.6, size=4)["ensemble"]["reasons_from"], "fourth")

    def test_ensemble_coherence_and_single_sample(self) -> None:
        # A fence-sitting mean leans the market favorite's way, with the note.
        samples = [self._sample("a", 0.55, 2.0, 45.0), self._sample("b", 0.45, -2.0, 45.0)]
        response = ensemble_response(samples, fair_home=0.62, size=3)
        self.assertEqual((response["home_win_probability"], response["expected_home_margin"], response["projected_total"]), (0.505, 0.5, 45.0))
        self.assertEqual(response["discarded_considerations"], ["Sample a: injuries are not in the input.", FENCE_NOTE])
        self.assertEqual(response["ensemble"]["reasons_from"], "a")
        self.assertEqual((response["ensemble"]["size"], response["ensemble"]["valid"]), (3, 2))
        # The estimates keep the samples' own numbers, not the coherent mean.
        self.assertEqual(response["ensemble"]["estimates"], [[0.55, 2.0, 45.0], [0.45, -2.0, 45.0]])
        # A sign disagreement keeps the probability and clamps the margin.
        samples = [self._sample("a", 0.56, -3.0, 45.0), self._sample("b", 0.58, 1.0, 45.0)]
        response = ensemble_response(samples, fair_home=0.62, size=2)
        self.assertEqual((response["home_win_probability"], response["expected_home_margin"]), (0.57, 0.5))
        self.assertEqual(response["discarded_considerations"][-1], SIGN_NOTE)
        # One valid sample is its own mean.
        response = ensemble_response([self._sample("only", 0.6, 2.5, 44.0)], fair_home=0.62, size=3)
        self.assertEqual((response["home_win_probability"], response["expected_home_margin"], response["projected_total"]), (0.6, 2.5, 44.0))
        self.assertEqual(
            response["ensemble"],
            {"size": 3, "valid": 1, "samples": ["only"], "estimates": [[0.6, 2.5, 44.0]], "reasons_from": "only", "rule": ENSEMBLE_RULE},
        )
        with self.assertRaises(ValueError):
            ensemble_response([], fair_home=0.6, size=3)
        with self.assertRaises(ValueError):
            ensemble_response(samples, fair_home=0.6, size=1)

    def test_normalize_carries_the_ensemble_block(self) -> None:
        response = ensemble_response(self._three(), fair_home=self.fair_home, size=3)
        opinion = normalize_aggregator_opinion(response, self.payload, expert=self.expert, model="claude-fable-5-1")
        validate_opinion(opinion, away_team=AWAY, home_team=HOME, schedule_input=self.payload)
        summary = json.loads(opinion["calibration_summary_json"])
        self.assertEqual(summary["ensemble"], response["ensemble"])
        self.assertEqual(summary["arm"], "judge")
        self.assertIn("· model claude-fable-5-1 · mean of 3 of 3 samples", opinion["full_opinion"])
        self.assertEqual(opinion["home_win_probability"], 0.61)
        self.assertIn("Sample s3 rests on a broad cohort.", opinion["supporting_factors"][0])
        # Without the block: None in the summary and no mention.
        plain = normalize_aggregator_opinion(
            {key: value for key, value in response.items() if key != "ensemble"}, self.payload, expert=self.expert, model="claude-fable-5-1"
        )
        self.assertIsNone(json.loads(plain["calibration_summary_json"])["ensemble"])
        self.assertNotIn(" · mean of ", plain["full_opinion"])
        # The rules arm never carries one.
        rules = rules_arm_response(self.payload)
        rules["ensemble"] = response["ensemble"]
        with self.assertRaises(ValueError) as caught:
            normalize_aggregator_opinion(rules, self.payload, expert=load_expert("god_rules"))
        self.assertIn("ensemble", str(caught.exception))
        rules_opinion = normalize_aggregator_opinion(rules_arm_response(self.payload), self.payload, expert=load_expert("god_rules"))
        self.assertIsNone(json.loads(rules_opinion["calibration_summary_json"])["ensemble"])

    def test_malformed_ensemble_blocks_are_rejected(self) -> None:
        response = ensemble_response(self._three(), fair_home=self.fair_home, size=3)
        good = response["ensemble"]
        bad_blocks = [
            "not an object",
            [],
            {**good, "extra": 1},
            {key: value for key, value in good.items() if key != "rule"},
            {**good, "size": 0},
            {**good, "size": True},
            {**good, "size": "3"},
            {**good, "valid": 4},
            {**good, "valid": 0},
            {**good, "samples": ["s1", "s2"]},
            {**good, "samples": ["s1", "s1", "s1"]},
            {**good, "samples": ["s1", "", "s3"]},
            {**good, "samples": "s1,s2,s3"},
            {**good, "estimates": good["estimates"][:2]},
            {**good, "estimates": [[0.6, 2.0], [0.6, 2.0, 44.0], [0.6, 2.0, 44.0]]},
            {**good, "estimates": [[0.6, 2.0, float("inf")], [0.6, 2.0, 44.0], [0.6, 2.0, 44.0]]},
            {**good, "estimates": [[True, 2.0, 44.0], [0.6, 2.0, 44.0], [0.6, 2.0, 44.0]]},
            {**good, "reasons_from": "s9"},
            {**good, "rule": ""},
            {**good, "rule": 3},
        ]
        for block in bad_blocks:
            with self.subTest(block=block):
                with self.assertRaises(ValueError):
                    normalize_aggregator_opinion({**response, "ensemble": block}, self.payload, expert=self.expert)
        # Tuples and integers normalize to float triples.
        loose = {**good, "estimates": [(1, 2, 44), [0.64, 4, 46.0], [0.61, 3.0, 45.0]]}
        summary = json.loads(normalize_aggregator_opinion({**response, "ensemble": loose}, self.payload, expert=self.expert)["calibration_summary_json"])
        self.assertEqual(summary["ensemble"]["estimates"][0], [1.0, 2.0, 44.0])


class SampleRowTests(unittest.IsolatedAsyncioTestCase):
    """generate_opinion(sample=True): audit rows every downstream path ignores."""

    def setUp(self) -> None:
        registry = load_registry()
        self.payload = build_aggregator_input(
            _game(), approved_opinions=approved_opinions(_committee()), finals=[], snapshots=[], registry=registry, policy=aggregator_policy(registry)
        )
        self.request = build_judge_request(self.payload)
        labels = [voice["label"] for voice in self.request["voices"]]
        self.response = {
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

    async def _persist(self, text: str, store: MemoryStore) -> dict:
        async def create_fn(**_kwargs):
            return SimpleNamespace(content=[SimpleNamespace(text=text)])

        return await generate_opinion(
            expert_id="god_judge",
            game=_game(),
            history=[],
            input_payload=self.payload,
            opinions=_committee(),
            store=store,
            create_fn=create_fn,
            generation_backend="claude_headless",
            generation_effort="max",
            model="claude-fable-5-1",
            expected_input_sha256=sha256_text(canonical_json(self.request)),
            sample=True,
        )

    async def test_valid_sample_is_an_audit_row_the_pipeline_ignores(self) -> None:
        store = MemoryStore()
        row = await self._persist(json.dumps(self.response), store)
        self.assertEqual(store.rows, [row])
        self.assertEqual(row["generation_status"], "sample")
        self.assertEqual(row["review_status"], "not_applicable")
        self.assertEqual(row["generation_error"], "")
        self.assertEqual(row["generation_backend"], "claude_headless")
        self.assertEqual(row["home_win_probability"], 0.61)
        self.assertEqual(row["raw_response"], json.dumps(self.response))
        self.assertEqual(row["input_json"], canonical_json(self.request))
        self.assertEqual(row["output_sha256"], opinion_output_sha256(row))
        self.assertIsNone(json.loads(row["calibration_summary_json"])["ensemble"])
        # Even stamped as approved it is neither a voice, a display row, nor a bulk-review row.
        stamped = dict(row, review_status="approved", approved_output_sha256=row["output_sha256"])
        self.assertEqual(approved_opinions([stamped]), [])
        self.assertEqual(latest_opinions([stamped]), [])
        self.assertEqual(week_rows([stamped], expert_id="god_judge", week=1, review_status=""), [])
        # A valid judge row on the same input is untouched by the flag.
        judge = dict(row, generation_status="valid", review_status="approved", approved_output_sha256=row["output_sha256"])
        self.assertEqual(approved_opinions([stamped, judge]), [judge])

    async def test_failed_sample_keeps_its_status_and_error(self) -> None:
        store = MemoryStore()
        with self.assertRaises(ValueError) as caught:
            await self._persist(json.dumps({"home_win_probability": 0.5}), store)
        self.assertEqual(len(store.rows), 1)
        row = store.rows[0]
        self.assertEqual(row["generation_status"], "sample")
        self.assertEqual(row["review_status"], "not_applicable")
        self.assertTrue(row["generation_error"].startswith("ValueError: "))
        self.assertIn(str(caught.exception), row["generation_error"])
        self.assertEqual(row["raw_response"], json.dumps({"home_win_probability": 0.5}))
        self.assertEqual(row["output_sha256"], "")
        self.assertEqual(approved_opinions([row]), [])

    def test_store_review_refuses_a_sample_row(self) -> None:
        store = GoogleSheetsMoeOpinionStore("credentials", "sheet-id")
        row = {header: "" for header in OPINION_HEADERS}
        row.update({"opinion_id": "sample-1", "generation_status": "sample", "review_status": "not_applicable"})
        values = [row[header] for header in OPINION_HEADERS]
        worksheet = SimpleNamespace(
            col_values=lambda column: ["opinion_id", "sample-1"],
            row_values=lambda number: values,
            update=lambda *args, **kwargs: self.fail("a sample row was updated"),
        )
        store._spreadsheet_instance = SimpleNamespace(worksheet=lambda name: worksheet)
        with self.assertRaises(ValueError) as caught:
            store.review("sample-1", status="approved", reviewed_by="tester", note="")
        self.assertIn("Only valid opinions", str(caught.exception))


if __name__ == "__main__":
    unittest.main()
