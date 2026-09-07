#!/usr/bin/env python3
"""Tests for the God Expert aggregator (rules arm, judge arm, shared policy)."""

from __future__ import annotations

import json
import math
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
    ADVERSE_MOVE_REASON,
    AGGREGATOR_PROFILE,
    DETERMINISTIC_BACKEND,
    DETERMINISTIC_MODEL,
    EV_FLOOR_REASON,
    JUDGE_REQUEST_PROFILE,
    NO_EXPECTATION_REASON,
    aggregator_policy,
    american_to_implied,
    apply_policy,
    build_aggregator_input,
    build_judge_request,
    build_market_block,
    build_scoreboard,
    canonical_json,
    cover_probability,
    fair_pair,
    grade_opinion_row,
    hedge_weights,
    load_registry,
    movement_since_open,
    normalize_aggregator_opinion,
    over_probability,
    price_cents,
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
        self.assertEqual(policy["version"], 1)
        self.assertEqual(policy["veto_adverse_spread_points"], 0.5)
        self.assertEqual(policy["veto_adverse_total_points"], 1.0)
        self.assertEqual(policy["veto_adverse_price_cents"], 10)
        self.assertEqual(policy["min_ev_per_unit"], 0.02)


class MovementRenderTests(unittest.TestCase):
    def test_rendering_mentions_price_moves_and_policy_notes(self) -> None:
        registry = load_registry()
        policy = aggregator_policy(registry)
        payload = build_aggregator_input(
            _game(), approved_opinions=_committee(), finals=[], snapshots=[], registry=registry, policy=policy
        )
        # The knobs ride in the input, so they are hash-bound like the rest.
        self.assertEqual(payload["policy"]["min_ev_per_unit"], 0.02)
        self.assertEqual(payload["policy"]["veto_adverse_price_cents"], 10.0)
        self.assertEqual(payload["market"]["movement_since_open"]["home_spread_price"], 10.0)
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
        self.assertIn("Policy: ev floor: Over 44.5 (-105) ev +0.019 under 0.020.", opinion["counterarguments"])
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


if __name__ == "__main__":
    unittest.main()
