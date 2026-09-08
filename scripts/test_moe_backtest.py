#!/usr/bin/env python3
"""Tests for the God Expert backtest harness (roadmap WP8) and the ledger
refit tooling (WP10): the two refactors it rests on, the market and voice
built through the production path, the scoring arithmetic on hand-built
games, the grid and its selection rule, the as-of empirical table, the veto
table, the ledger replay on the Week 1 fixtures, deterministic JSON, and a
headline check on the committed data."""

from __future__ import annotations

import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path

import moe_backtest as bt
import moe_god
from moe_god import (
    DEFAULT_POLICY,
    EV_FLOOR_REASON,
    MARKET_FIELDS,
    aggregator_policy,
    apply_policy,
    build_market_block,
    load_margin_table,
    load_registry,
    margin_table_for,
    margin_table_override,
    market_block_from_lines,
    parse_margin_table,
)
from moe_rating import (
    PARAMETER_KEYS,
    build_rating_input,
    load_prior,
    preseason_ratings,
    rating_estimate,
    replay_games,
)
from scripts.backtest_god import build_parser, main
from scripts.test_moe_god import AWAY, HOME, KICKOFF, _game

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "god_week1"


def _fixture(name: str) -> dict:
    return json.loads((FIXTURES / f"{name}.json").read_text(encoding="utf-8"))


def _close(**overrides) -> dict:
    """A decoded closing market: home -3 at -110/-110, total 44.5 at -110."""
    market = {
        "away_spread": 3.0,
        "away_spread_price": -110,
        "away_moneyline": 135,
        "home_spread": -3.0,
        "home_spread_price": -110,
        "home_moneyline": -155,
        "total": 44.5,
        "over_price": -110,
        "under_price": -110,
    }
    market.update(overrides)
    return market


def _history_row(
    season: int,
    week: int,
    away: str,
    home: str,
    away_score: int,
    home_score: int,
    *,
    espn_id: str = "",
    home_spread: str = "-3",
    total: str = "44.5",
    prices: dict | None = None,
) -> dict:
    row = {
        "season": str(season),
        "week": str(week),
        "gameday": f"{season}-09-{7 + week:02d}",
        "weekday": "Sunday",
        "gametime": "13:00",
        "espn_id": espn_id,
        "away_team": away,
        "home_team": home,
        "away_score": str(away_score),
        "home_score": str(home_score),
        "home_spread": home_spread,
        "total": total,
        "away_moneyline": "135",
        "home_moneyline": "-155",
        "away_spread_price": "-110",
        "home_spread_price": "-110",
        "over_price": "-110",
        "under_price": "-110",
        "nflverse_spread_line": "",
    }
    row.update(prices or {})
    return row


class RefactorTests(unittest.TestCase):
    """The two behavior-preserving refactors the harness rests on."""

    def test_market_block_from_lines_equals_build_market_block(self) -> None:
        from moe_ak import _market_from_packed

        game = _game()
        expected = build_market_block(game)
        rebuilt = market_block_from_lines(
            _market_from_packed(game, prefix="opening"),
            _market_from_packed(game, prefix="latest"),
            bookmaker=game["bookmaker"],
            opening_captured_at=game["opening_captured_at"],
            latest_captured_at=game["latest_captured_at"],
        )
        self.assertEqual(rebuilt, expected)
        self.assertEqual(expected["bookmaker"], "BetOnline.ag")
        # The persisted Week 1 market rebuilds from its own opening and
        # latest, except the price deltas the fixture predates.
        market = _fixture("sea_rules")["input_json"]["market"]
        again = market_block_from_lines(
            {field: market["opening"][field] for field in MARKET_FIELDS},
            {field: market["latest"][field] for field in MARKET_FIELDS},
            bookmaker=market["bookmaker"],
            opening_captured_at=market["opening"]["captured_at"],
            latest_captured_at=market["latest"]["captured_at"],
        )
        for key in market:
            if key == "movement_since_open":
                continue
            self.assertEqual(again[key], market[key], key)
        self.assertEqual(
            {key: again["movement_since_open"][key] for key in market["movement_since_open"]},
            market["movement_since_open"],
        )

    def test_market_without_an_opening_never_vetoes(self) -> None:
        latest = _close(home_spread_price=100, away_spread_price=-120)
        for opening in (None, {}, {field: None for field in MARKET_FIELDS}):
            with self.subTest(opening=opening):
                market = market_block_from_lines(opening, latest, bookmaker="nflverse")
                self.assertTrue(all(value is None for value in market["movement_since_open"].values()))
                self.assertEqual(market["opening"]["home_spread"], None)
                self.assertEqual(market["opening"]["captured_at"], "")
                self.assertEqual(market["bookmaker"], "nflverse")
                legs = apply_policy(
                    home_win_probability=0.8,
                    expected_home_margin=9.0,
                    projected_total=52.0,
                    market=market,
                    policy=aggregator_policy(load_registry()),
                    away_team=AWAY,
                    home_team=HOME,
                )
                self.assertEqual(legs["side"]["selection"], HOME)
                self.assertIsNone(legs["side"]["pass_reason"])
        with self.assertRaises(ValueError):
            market_block_from_lines(None, _close(over_price=None))
        with self.assertRaises(ValueError):
            market_block_from_lines(None, _close(away_spread=2.5))

    def test_margin_table_override_is_scoped_and_never_touches_check(self) -> None:
        from moe_god import check_margin_table

        committed = load_margin_table()
        toy = parse_margin_table(
            {
                "schema_version": 1,
                "version": "toy",
                "seasons": [2024],
                "games": 4,
                "bin_width": 1,
                "lattice_step": 0.5,
                "min_games": 4,
                "spread": {"bins": {"-4": {"n": 4, "residuals": [[-1, 1], [0, 2], [1, 1]]}}},
                "total": {"bins": {"44": {"n": 4, "residuals": [[-2, 1], [-0.5, 1], [0.5, 1], [2, 1]]}}},
            },
            sha256="toy",
        )
        empirical = {"margin_model": "empirical"}
        self.assertIs(margin_table_for(empirical), committed)
        with margin_table_override(toy) as active:
            self.assertIs(active, toy)
            self.assertIs(margin_table_for(empirical), toy)
            self.assertIsNone(margin_table_for({"margin_model": "normal"}))
            # A live input is still checked against the committed file.
            with self.assertRaises(ValueError):
                check_margin_table({"margin_table": {"sha256": "toy"}})
            check_margin_table({"margin_table": {"sha256": committed["sha256"]}})
            with margin_table_override(committed):
                self.assertIs(margin_table_for(empirical), committed)
            self.assertIs(margin_table_for(empirical), toy)
        self.assertIs(margin_table_for(empirical), committed)
        self.assertIsNone(moe_god._MARGIN_TABLE_OVERRIDE)
        # The override is released on an exception too.
        with self.assertRaises(RuntimeError):
            with margin_table_override(toy):
                raise RuntimeError("boom")
        self.assertIs(margin_table_for(empirical), committed)

    def test_rating_estimate_equals_the_input_estimate(self) -> None:
        prior, _digest = load_prior()
        params = {key: float(prior["parameters"][key]) for key in PARAMETER_KEYS}
        game = {
            "event_id": "evt",
            "season": int(prior["through_season"]) + 1,
            "week": 1,
            "away_team": AWAY,
            "home_team": HOME,
            "commence_time_utc": KICKOFF,
        }
        payload = build_rating_input(game, [])
        preseason = preseason_ratings(prior, game["season"])
        estimate = rating_estimate(
            away_team=AWAY,
            home_team=HOME,
            away_rating=preseason[AWAY],
            home_rating=preseason[HOME],
            params=params,
            total=prior["league_scoring_rate"]["mean_total"],
        )
        self.assertEqual(estimate, payload["estimate"])
        self.assertEqual(payload["ratings"]["home"]["rating"], round(preseason[HOME], 2))
        # Rounding and the leans are the input's: an exactly offsetting gap
        # leans home 0.5001 / +0.01; a tiny gap keeps a coherent margin.
        offset = rating_estimate(away_team=AWAY, home_team=HOME, away_rating=1532.0, home_rating=1500.0, params={**params, "hfa": 32.0}, total=46.0)
        self.assertEqual((offset["home_win_probability"], offset["expected_home_margin"], offset["tie_break"] is not None), (0.5001, 0.01, True))
        self.assertEqual(offset["predicted_winner"], HOME)
        self.assertNotEqual(offset["predicted_away_score"], offset["predicted_home_score"])
        lean = rating_estimate(away_team=AWAY, home_team=HOME, away_rating=1531.9, home_rating=1500.0, params={**params, "hfa": 32.0}, total=46.0)
        self.assertGreater(lean["home_win_probability"], 0.5)
        self.assertEqual(lean["expected_home_margin"], 0.01)
        self.assertIsNone(lean["tie_break"])

    def test_replay_predictions_carry_pregame_ratings(self) -> None:
        rows = [
            _history_row(2024, 1, "A", "B", 10, 20),
            _history_row(2024, 2, "B", "A", 21, 14),
        ]
        params = {"k": 20.0, "hfa": 40.0, "regression": 0.3, "points_per_elo": 25.0}
        predictions = replay_games(rows, params)["predictions"]
        self.assertEqual((predictions[0]["home_rating"], predictions[0]["away_rating"]), (1500.0, 1500.0))
        self.assertAlmostEqual(predictions[1]["adjusted_gap"], predictions[1]["home_rating"] - predictions[1]["away_rating"] + 40.0, places=9)
        self.assertGreater(predictions[1]["away_rating"], predictions[1]["home_rating"])


class DataTests(unittest.TestCase):
    def test_history_games_and_markets(self) -> None:
        rows = [
            _history_row(2024, 2, "A", "B", 20, 27, espn_id="2"),
            _history_row(2024, 1, "C", "D", 17, 17, espn_id="1"),
            _history_row(2023, 1, "A", "B", 3, 0, espn_id="0"),
            {**_history_row(2024, 3, "E", "F", 0, 0), "home_score": "", "away_score": ""},
            {**_history_row(2024, 4, "G", "H", 1, 2), "over_price": ""},
        ]
        games = bt.history_games(rows, [2024])
        self.assertEqual([(game["week"], game["event_id"]) for game in games], [(1, "1"), (2, "2")])
        self.assertEqual(games[1]["final"], {"away_score": 20, "home_score": 27})
        self.assertEqual(games[1]["close"], _close())
        self.assertEqual(set(games[1]["close"]), set(MARKET_FIELDS))
        self.assertIsNone(bt.market_from_history_row({**rows[0], "home_spread": ""}))
        self.assertEqual(bt.market_from_espn_block(_close()), _close())
        self.assertIsNone(bt.market_from_espn_block({**_close(), "away_spread": 2.5}))
        self.assertIsNone(bt.market_from_espn_block({**_close(), "over_price": None}))
        self.assertIsNone(bt.market_from_espn_block(None))
        attached = bt.attach_open_close(games, {"2": {"provider": "ESPN BET", "open": _close(total=43.5), "close": _close()}})
        self.assertEqual(attached[1]["espn_open"]["total"], 43.5)
        self.assertEqual(attached[1]["espn_provider"], "ESPN BET")
        self.assertNotIn("espn_open", attached[0])
        self.assertEqual(bt.parse_seasons(["2023-2024", "2025"]), [2023, 2024, 2025])

    def test_rating_inputs_use_the_previous_season_scoring_rate(self) -> None:
        rows = [
            _history_row(2023, 1, "A", "B", 20, 24),
            _history_row(2023, 2, "B", "A", 30, 10),
            _history_row(2024, 1, "A", "B", 14, 21),
        ]
        params = {"k": 20.0, "hfa": 40.0, "regression": 0.3, "points_per_elo": 25.0}
        inputs = bt.rating_inputs(rows, seasons=[2024], params=params)
        self.assertEqual(list(inputs), [(2024, 1, "A", "B")])
        entry = inputs[(2024, 1, "A", "B")]
        self.assertEqual(entry["total"], 42.0)  # (44 + 40) / 2 from 2023
        replay = replay_games(rows, params, collect_from=2024)["predictions"][0]
        self.assertEqual(entry["home_rating"], replay["home_rating"])
        self.assertEqual(entry["elo_probability"], replay["home_win_probability"])


class ScoringTests(unittest.TestCase):
    """Four hand-built games under one policy: Brier, legs, units, CLV, stars."""

    def setUp(self) -> None:
        self.registry = load_registry()
        self.base = bt.base_policy_block(self.registry)
        self.policy = bt.make_policy(self.base)

    def test_grade_legs_units_and_clv(self) -> None:
        game = {"away_team": AWAY, "home_team": HOME, "final": {"away_score": 20, "home_score": 27}}
        legs = {
            "side": {"selection": HOME, "line": -3.0, "price": -110, "edge": 0.05, "confidence_stars": 2, "pass_reason": None},
            "total": {"selection": "Over", "line": 47.0, "price": 100, "edge": 0.04, "confidence_stars": 1, "pass_reason": None},
        }
        closing = _close(home_spread=-4.5, away_spread=4.5, total=45.5)
        graded = bt.grade_legs(legs, game=game, closing=closing)
        self.assertEqual([(leg["kind"], leg["result"], leg["units"], leg["clv_points"], leg["stars"]) for leg in graded], [("side", "W", round(100 / 110, 4), 1.5, 2), ("total", "P", 0.0, -1.5, 1)])
        self.assertEqual(graded[0]["label"], f"{HOME} -3")
        losing = {**legs, "side": {**legs["side"], "selection": AWAY, "line": 3.0}}
        self.assertEqual(bt.grade_legs(losing, game=game, closing=None)[0]["units"], -1.0)
        self.assertIsNone(bt.grade_legs(losing, game=game, closing=None)[0]["clv_points"])
        passes = {kind: {"selection": "PASS", "line": None, "price": None, "edge": 0.0, "confidence_stars": 1, "pass_reason": None} for kind in ("side", "total")}
        self.assertEqual(bt.grade_legs(passes, game=game, closing=None), [])
        self.assertEqual(bt.cover_outcome(game, -7.0), None)  # 27-20 against -7 pushes
        self.assertEqual(bt.cover_outcome(game, -6.5), 1.0)
        self.assertEqual(bt.cover_outcome(game, -7.5), 0.0)
        self.assertEqual(bt.win_outcome({"final": {"away_score": 3, "home_score": 3}}), None)

    def test_tally_summary_arithmetic(self) -> None:
        tally = bt.Tally()
        tally.add_game(outcome=1.0, probability=0.7, market_probability=0.6, elo_probability=0.55, cover=1.0, p_cover_home=0.6, fair_cover=0.5)
        tally.add_game(outcome=0.0, probability=0.4, market_probability=0.5, elo_probability=None, cover=None, p_cover_home=0.5, fair_cover=0.5)
        tally.add_game(outcome=None, probability=0.5, market_probability=0.5, elo_probability=0.5, cover=0.0, p_cover_home=0.4, fair_cover=0.45)
        legs = {
            "side": {"selection": HOME, "line": -3.0, "price": -110, "edge": 0.05, "confidence_stars": 2, "pass_reason": None},
            "total": {"selection": "PASS", "line": None, "price": None, "edge": 0.01, "confidence_stars": 1, "pass_reason": EV_FLOOR_REASON},
        }
        tally.add_legs(legs, [{"kind": "side", "label": "x", "price": -110, "edge": 0.05, "stars": 2, "result": "W", "units": 0.9091, "clv_points": 1.0}])
        tally.add_legs(legs, [{"kind": "side", "label": "y", "price": -110, "edge": 0.05, "stars": 2, "result": "L", "units": -1.0, "clv_points": -0.5}])
        summary = tally.summary()
        self.assertEqual(summary["games"], 3)
        self.assertEqual(summary["ml"]["n"], 2)
        self.assertAlmostEqual(summary["ml"]["brier"], ((0.3) ** 2 + (0.4) ** 2) / 2, places=5)
        self.assertAlmostEqual(summary["ml"]["market_brier"], (0.16 + 0.25) / 2, places=5)
        self.assertAlmostEqual(summary["ml"]["elo_brier"], 0.45 ** 2, places=5)  # one game carried an Elo probability
        self.assertEqual(summary["cover"]["n"], 2)
        self.assertAlmostEqual(summary["cover"]["brier"], (0.16 + 0.16) / 2, places=5)
        self.assertAlmostEqual(summary["cover"]["fair_brier"], (0.25 + 0.2025) / 2, places=5)
        self.assertEqual(summary["legs"]["record"], "1-1-0")
        self.assertAlmostEqual(summary["legs"]["units"], -0.09, places=2)
        self.assertAlmostEqual(summary["legs"]["roi"], -0.0455, places=3)
        self.assertEqual(summary["legs"]["clv_mean"], 0.25)
        self.assertEqual(summary["by_stars"]["2"]["bets"], 2)
        self.assertEqual(summary["by_kind"]["total"]["bets"], 0)
        self.assertEqual(summary["pass_reasons"], {"ev floor": 2})
        json.dumps(summary)

    def test_rules_estimate_depends_on_lambda_only(self) -> None:
        market = market_block_from_lines(None, _close(), bookmaker="nflverse")
        game = {"season": 2024, "week": 1, "event_id": "e", "away_team": AWAY, "home_team": HOME, "final": {"away_score": 20, "home_score": 27}}
        params = bt.elo_parameters()
        inputs = {"home_rating": 1560.0, "away_rating": 1500.0, "total": 46.0, "elo_probability": 0.6}
        voice = bt.rating_voice(game, inputs, params=params, registry=self.registry, market=market, policy=self.policy)
        self.assertEqual(voice["voice_id"], "rating_elo")
        self.assertEqual(voice["markets"], ["side"])
        self.assertEqual(voice["evidence"], {"tuples": [], "cohorts": []})
        expected = rating_estimate(away_team=AWAY, home_team=HOME, away_rating=1500.0, home_rating=1560.0, params=params, total=46.0)
        self.assertEqual(voice["home_win_probability"], expected["home_win_probability"])
        self.assertEqual(voice["expected_home_margin"], expected["expected_home_margin"])
        half = bt.rules_estimate([voice], market, bt.make_policy(self.base, shrink_lambda=0.5), home_team=HOME)
        fair_home = market["fair"]["home_ml"]
        self.assertAlmostEqual(half["home_win_probability"], round(0.5 * expected["home_win_probability"] + 0.5 * fair_home, 4), places=4)
        self.assertAlmostEqual(half["expected_home_margin"], round(0.5 * expected["expected_home_margin"] + 0.5 * 3.0, 2), places=2)
        self.assertEqual(half["projected_total"], 44.5)  # the empty total pool takes the line
        self.assertEqual(half["feature_block"]["markets"], {"side": ["rating_elo"], "total": []})
        for sigma, model in ((12.0, "normal"), (15.0, "empirical")):
            other = bt.rules_estimate([voice], market, bt.make_policy(self.base, shrink_lambda=0.5, sigma_margin=sigma, margin_model=model), home_team=HOME)
            self.assertEqual({key: other[key] for key in ("home_win_probability", "expected_home_margin", "projected_total")}, {key: half[key] for key in ("home_win_probability", "expected_home_margin", "projected_total")})
        zero = bt.rules_estimate([voice], market, bt.make_policy(self.base, shrink_lambda=0.0), home_team=HOME)
        self.assertEqual(zero["home_win_probability"], round(fair_home, 4))
        self.assertEqual(zero["expected_home_margin"], 3.0)

    def test_replay_scores_four_games(self) -> None:
        # Two home covers, one dog that wins, two pushes at -3 (17-14 and
        # 23-20); the rating voice is warmed up by the 2023 rows.
        rows = [
            _history_row(2023, 1, AWAY, HOME, 10, 31),
            _history_row(2023, 2, "Dallas Cowboys", "Philadelphia Eagles", 7, 28),
            _history_row(2023, 3, HOME, AWAY, 27, 24),
            _history_row(2024, 1, AWAY, HOME, 20, 27, espn_id="a"),
            _history_row(2024, 2, "Dallas Cowboys", "Philadelphia Eagles", 24, 21, espn_id="b"),
            _history_row(2024, 3, HOME, AWAY, 14, 17, espn_id="c"),
            _history_row(2024, 4, "Philadelphia Eagles", "Dallas Cowboys", 20, 23, espn_id="d"),
        ]
        points = [(bt.make_policy(self.base, shrink_lambda=1.0, edge_threshold=0.02, min_ev_per_unit=0.0), "default")]
        result = bt.replay_seasons(rows, seasons=[2024], points=points, registry=self.registry)
        self.assertEqual(result["games"], 4)
        self.assertEqual(result["tables"], {"2024": list(range(2016, 2024))})
        summary = next(iter(result["results"].values()))
        self.assertEqual(summary["games"], 4)
        self.assertEqual(summary["ml"]["n"], 4)
        self.assertEqual(summary["cover"]["n"], 2)  # 17-14 and 23-20 against -3 push
        self.assertEqual(summary["legs"]["bets"] + summary["pass_reasons"].get("under threshold", 0) + sum(count for reason, count in summary["pass_reasons"].items() if reason != "under threshold"), 8)
        self.assertEqual(summary["by_kind"]["total"]["bets"], 0)
        self.assertIsNone(summary["legs"]["clv_mean"])
        # Duplicate points are scored once.
        twice = bt.replay_seasons(rows, seasons=[2024], points=points + points, registry=self.registry)
        self.assertEqual(twice["results"], result["results"])
        # The open-market mode needs both ESPN blocks and grades CLV.
        open_close = {
            "a": {"provider": "ESPN BET", "open": _close(home_spread=-2.5, away_spread=2.5, total=43.5), "close": _close()},
            "b": {"provider": "ESPN BET", "open": _close(), "close": None},
        }
        opened = bt.replay_seasons(rows, seasons=[2024], points=points, registry=self.registry, market_source="open", open_close=open_close)
        self.assertEqual(opened["games"], 1)
        self.assertEqual(opened["skipped"], {"no ESPN open and close": 3})
        legs = next(iter(opened["results"].values()))["legs"]
        if legs["bets"]:
            self.assertEqual(legs["clv_n"], legs["bets"])


class GridTests(unittest.TestCase):
    def setUp(self) -> None:
        self.registry = load_registry()
        self.base = bt.base_policy_block(self.registry)

    def test_grid_points_validate_and_include_the_registry(self) -> None:
        points = bt.grid_points(self.base)
        self.assertEqual(len(points), 5 * 5 * 2 * 4 * 4 * 2)
        labels = {bt.key_label(bt.policy_key(policy, ladder)) for policy, ladder in points}
        self.assertEqual(len(labels), len(points))
        registry_key = bt.policy_key(bt.make_policy(self.base, ladder=bt.registry_ladder(self.base)), bt.registry_ladder(self.base))
        self.assertIn(bt.key_label(registry_key), labels)
        for policy, ladder in points[:40]:
            self.assertEqual(policy["star_edges"][0], policy["edge_threshold"])
            self.assertEqual(policy["star_edges"], bt.star_edges_for(policy["edge_threshold"], ladder))
            aggregator_policy({"aggregator_policy": {key: policy[key] for key in DEFAULT_POLICY}})
        self.assertEqual(bt.star_edges_for(0.03, "default"), [0.03, 0.05, 0.08, 0.12, 0.16])
        self.assertEqual(bt.star_edges_for(0.02, "alternative"), [0.02, 0.05, 0.08, 0.11, 0.14])
        # A registry value off the grid still becomes a point.
        off = bt.grid_points({**self.base, "shrink_lambda": 0.6}, {**bt.GRID, "shrink_lambda": (0.0, 1.0)})
        self.assertEqual(sorted({policy["shrink_lambda"] for policy, _ladder in off}), [0.0, 0.6, 1.0])
        self.assertEqual(bt._parse_label(bt.key_label(registry_key)), registry_key)

    def _summary(self, *, brier: float, cover: float, bets: int, wins: int, stars: dict | None = None, clv: float | None = None) -> dict:
        losses = bets - wins
        units = round(wins * 0.9091 - losses, 2)
        legs = {"bets": bets, "record": f"{wins}-{losses}-0", "wins": wins, "losses": losses, "pushes": 0, "units": units, "roi": None if not bets else round(units / bets, 4), "clv_n": 0 if clv is None else bets, "clv_mean": clv}
        return {
            "games": 100,
            "ml": {"n": 100, "brier": brier, "log_loss": 0.6, "market_brier": 0.21, "elo_brier": 0.22},
            "cover": {"n": 98, "brier": cover, "fair_brier": 0.25},
            "legs": legs,
            "by_kind": {"side": legs, "total": legs},
            "by_stars": stars or {"1": legs},
            "pass_reasons": {},
        }

    def test_selection_rule(self) -> None:
        results = {}
        for policy, ladder in bt.grid_points(self.base):
            key = bt.policy_key(policy, ladder)
            lam, sigma, model, edge, floor, _ladder = key
            brier = 0.20 + abs(lam - 0.25) * 0.01  # lambda 0.25 wins
            cover = 0.25 + abs(sigma - 14.0) * 0.001 + (0.002 if model == "empirical" else 0.0)  # sigma 14, normal
            # A 4% threshold with a 1% floor wins big with enough bets; a
            # 5% one wins bigger but on too few bets.
            if (edge, floor) == (0.04, 0.01):
                bets, wins = 80, 60
            elif edge == 0.05:
                bets, wins = 20, 20
            else:
                bets, wins = 100, 50
            results[bt.key_label(key)] = self._summary(brier=brier, cover=cover, bets=bets, wins=wins)
        selection = bt.select_policy(results, base=self.base)
        self.assertEqual(selection["overrides"], {"shrink_lambda": 0.25, "sigma_margin": 14.0, "margin_model": "normal", "edge_threshold": 0.04, "min_ev_per_unit": 0.01})
        self.assertEqual(selection["star_ladder"], "default")
        self.assertEqual(set(selection["changed"]), {"shrink_lambda", "sigma_margin", "edge_threshold", "min_ev_per_unit"})
        self.assertEqual([step["knob"] for step in selection["steps"]], ["shrink_lambda", "sigma_margin, margin_model", "edge_threshold, min_ev_per_unit", "star_edges"])
        self.assertEqual(selection["steps"][0]["table"]["0.25"], 0.2)
        view = selection["at_registry_lambda"]
        self.assertEqual(view["shrink_lambda"], 0.5)
        self.assertEqual(view["legs_by_edge_floor"]["0.04|0.01"]["bets"], 80)
        self.assertEqual(set(view["cover_brier_by_sigma_model"]), {f"{sigma:g}|{model}" for sigma in bt.GRID["sigma_margin"] for model in bt.GRID["margin_model"]})
        self.assertEqual(set(view["by_stars"]), {"default", "alternative"})
        self.assertIn("shrink_lambda: 0.25   # changed", bt.policy_block_text(self.base, selection))
        self.assertIn("star_edges: [0.04, 0.06, 0.09, 0.13, 0.17]", bt.policy_block_text(self.base, selection))
        # With CLV required the 4% candidate must also beat the default's CLV.
        with_clv = {label: {**summary, "legs": {**summary["legs"], "clv_n": summary["legs"]["bets"], "clv_mean": 0.5 if label.endswith("|edge_threshold=0.03|min_ev_per_unit=0.02|star_ladder=default") else 0.1}} for label, summary in results.items()}
        strict = bt.select_policy(with_clv, base=self.base, require_clv=True)
        self.assertEqual((strict["overrides"]["edge_threshold"], strict["overrides"]["min_ev_per_unit"]), (0.03, 0.02))
        # Ties on lambda resolve toward the registry value; a monotone
        # alternative ladder displaces a non-monotone default.
        flat = {}
        for policy, ladder in bt.grid_points(self.base):
            key = bt.policy_key(policy, ladder)
            stars = {"1": self._summary(brier=0.2, cover=0.25, bets=30, wins=15)["legs"], "2": self._summary(brier=0.2, cover=0.25, bets=30, wins=20 if ladder == "alternative" else 10)["legs"], "3": self._summary(brier=0.2, cover=0.25, bets=30, wins=25 if ladder == "alternative" else 20)["legs"]}
            flat[bt.key_label(key)] = self._summary(brier=0.2, cover=0.25, bets=90, wins=45, stars=stars)
        tied = bt.select_policy(flat, base=self.base)
        self.assertEqual(tied["overrides"]["shrink_lambda"], self.base["shrink_lambda"])
        self.assertEqual(tied["star_ladder"], "alternative")
        self.assertEqual(tied["changed"], {"star_ladder": {"registry": "default", "chosen": "alternative"}})
        self.assertTrue(bt._monotone_roi(flat[tied["key"]]["by_stars"]))
        self.assertIsNone(bt._monotone_roi({"1": {"bets": 3, "roi": 0.1}}))

    def test_as_of_table_uses_earlier_seasons_only(self) -> None:
        rows = bt.load_rows()
        table, seasons = bt._as_of_table(rows, 2024)
        self.assertEqual(seasons, list(range(2016, 2024)))
        self.assertEqual(table["seasons"], seasons)
        self.assertEqual(table["path"], "as-of-2024")
        earlier, _ = bt._as_of_table(rows, 2023)
        self.assertLess(earlier["games"], table["games"])
        self.assertEqual(bt._as_of_table(rows, 2016), (None, []))
        # The committed table holds 2016-2025 and is never what a replay
        # season reads.
        self.assertEqual(load_margin_table()["seasons"], list(range(2016, 2026)))


class VetoTests(unittest.TestCase):
    def test_adverse_legs_follow_the_policy_directions(self) -> None:
        game = {
            "event_id": "e",
            "season": 2024,
            "away_team": AWAY,
            "home_team": HOME,
            "final": {"away_score": 20, "home_score": 27},
            # Home spread -4 -> -3.5 (home cheaper), total 45.5 -> 44.5 (Over
            # cheaper), home price -110 -> +100 (home cheaper by 10 cents).
            "espn_open": _close(home_spread=-4.0, away_spread=4.0, total=45.5),
            "espn_close": _close(home_spread=-3.5, away_spread=3.5, total=44.5, home_spread_price=100, away_spread_price=-120),
        }
        spread = bt.adverse_legs(game, kind="spread", threshold=0.5)
        self.assertEqual([(leg["label"], leg["price"], leg["result"], leg["units"]) for leg in spread], [(f"{HOME} -3.5", 100, "W", 1.0)])
        self.assertEqual(bt.adverse_legs(game, kind="spread", threshold=1.0), [])
        total = bt.adverse_legs(game, kind="total", threshold=1.0)
        self.assertEqual([(leg["label"], leg["result"]) for leg in total], [("Over 44.5", "W")])  # 47 > 44.5
        self.assertEqual(bt.adverse_legs(game, kind="total", threshold=1.5), [])
        price = bt.adverse_legs(game, kind="price", threshold=10.0)
        self.assertEqual([(leg["label"], leg["move"]) for leg in price], [(f"{HOME} -3.5", 10.0)])
        self.assertEqual(bt.adverse_legs(game, kind="price", threshold=15.0), [])
        # The mirror moves veto the other sides: away, Under, the away price.
        mirrored = {**game, "espn_open": game["espn_close"], "espn_close": {**game["espn_open"], "away_spread_price": 100, "home_spread_price": -120}}
        self.assertEqual([leg["label"] for leg in bt.adverse_legs(mirrored, kind="spread", threshold=0.5)], [f"{AWAY} +4"])
        self.assertEqual([leg["label"] for leg in bt.adverse_legs(mirrored, kind="total", threshold=1.0)], ["Under 45.5"])
        self.assertEqual([leg["label"] for leg in bt.adverse_legs(mirrored, kind="price", threshold=10.0)], [f"{AWAY} +4"])
        with self.assertRaises(ValueError):
            bt.adverse_legs(game, kind="moneyline", threshold=1.0)

    def test_run_veto_table_and_recommendation(self) -> None:
        rows = [_history_row(2024, week, AWAY, HOME, 20, 27, espn_id=str(week)) for week in range(1, 6)]
        open_close = {
            str(week): {
                "provider": "ESPN BET",
                "open": _close(home_spread=-4.0, away_spread=4.0),
                "close": _close(home_spread=-3.0 if week < 4 else -4.0, away_spread=3.0 if week < 4 else 4.0),
            }
            for week in range(1, 6)
        }
        result = bt.run_veto(rows, seasons=[2024], open_close=open_close, grid={"spread": (0.5, 1.0, 2.0), "total": (1.0,), "price": (10.0,)})
        self.assertEqual((result["events"], result["events_with_movement"], result["events_without_open_close"]), (5, 3, 0))
        spread = result["table"]["spread"]
        self.assertEqual((spread["0.5"]["bets"], spread["0.5"]["record"], spread["1"]["bets"], spread["2"]["bets"]), (3, "3-0-0", 3, 0))
        self.assertEqual(spread["0.5"]["by_kind"]["side"]["bets"], 3)
        self.assertEqual(result["table"]["total"]["1"]["bets"], 0)
        self.assertEqual(result["table"]["price"]["10"]["bets"], 0)
        recommendation = result["recommendation"]
        self.assertTrue(all(block["keep"] for block in recommendation.values()))
        self.assertEqual(recommendation["veto_adverse_spread_points"]["current"], 0.5)
        lines = bt.format_veto_report(result)
        self.assertTrue(lines[0].startswith("Veto calibration"))
        self.assertIn("keep", lines[-1])
        json.dumps(result)


class LedgerTests(unittest.TestCase):
    """WP10: the grid replayed over the persisted Week 1 rules rows."""

    def _rows(self) -> tuple[list[dict], list[dict]]:
        rows, finals = [], []
        for name, (away_score, home_score) in (("sea_rules", (20, 27)), ("lar_rules", (24, 20))):
            row = _fixture(name)
            row["review_status"] = "approved"
            rows.append(row)
            game = row["input_json"]["game"]
            finals.append({"event_id": f"espn-{game['event_id']}", "kickoff_utc": game["commence_time_utc"], "away_team": game["away_team"], "home_team": game["home_team"], "away_score": away_score, "home_score": home_score})
        return rows, finals

    def test_ledger_games_filter_and_shape(self) -> None:
        rows, finals = self._rows()
        prepared = bt.ledger_games(rows, finals=finals, snapshots=[])
        self.assertEqual([game["home_team"] for game in prepared["games"]], [HOME, "Los Angeles Rams"])
        game = prepared["games"][0]
        self.assertEqual(game["final"], {"away_score": 20, "home_score": 27})
        self.assertIsNone(game["closing"])
        self.assertEqual([voice["markets"] for voice in game["voices"]], [["side", "total"], ["side"], ["side", "total"], ["side"]])
        self.assertEqual(game["weighting"], {"weights": {"ak": 1.0, "divisional": 1.0, "schedule": 1.0, "win_total": 1.0}, "active": False, "mean_brier": None})
        self.assertEqual(game["policy"]["veto_adverse_price_cents"], DEFAULT_POLICY["veto_adverse_price_cents"])  # the row predates the knob
        self.assertEqual(game["policy"]["shrink_lambda"], 0.5)
        # Pending rows are skipped unless asked for; unresolved rows always.
        pending = [{**rows[0], "review_status": "pending"}, rows[1]]
        self.assertEqual(bt.ledger_games(pending, finals=finals, snapshots=[])["skipped"], {"not approved": 1})
        self.assertEqual(len(bt.ledger_games(pending, finals=finals, snapshots=[], approved_only=False)["games"]), 2)
        self.assertEqual(bt.ledger_games(rows, finals=finals[:1], snapshots=[])["skipped"], {"unresolved": 1})
        self.assertEqual(bt.ledger_games([{**rows[0], "generation_status": "invalid"}], finals=finals, snapshots=[])["skipped"], {"not valid": 1})

    def test_run_ledger_on_the_fixtures(self) -> None:
        rows, finals = self._rows()
        result = bt.run_ledger(rows, finals=finals, snapshots=[])
        self.assertEqual(result["graded_games"], 2)
        self.assertTrue(result["informational"])
        self.assertEqual(result["minimum_games"], bt.MIN_REFIT_GAMES)
        self.assertEqual([row["final"] for row in result["rows"]], ["20-27", "24-20"])
        registry_key = result["selection"]["registry_key"]
        registry = result["results"][registry_key]
        # The rows re-pool under the live registry (WP5's overlap discount
        # and market masks), so the estimates sit a hair off the persisted
        # 0.6259 and 0.581; the Brier is the mean over the two replays.
        prepared = bt.ledger_games(rows, finals=finals, snapshots=[])["games"]
        estimates = [
            bt.rules_estimate(game["voices"], game["market"], bt.make_policy(game["policy"]), home_team=game["home_team"], weighting=game["weighting"])
            for game in prepared
        ]
        self.assertAlmostEqual(estimates[0]["home_win_probability"], 0.6259, delta=0.001)
        self.assertAlmostEqual(estimates[1]["home_win_probability"], 0.581, delta=0.005)
        expected_brier = ((1 - estimates[0]["home_win_probability"]) ** 2 + estimates[1]["home_win_probability"] ** 2) / 2
        self.assertAlmostEqual(registry["ml"]["brier"], expected_brier, places=4)
        # Seahawks -3.5 is vetoed on the price move; under the WP5 total pool
        # (ak and schedule only) Over 44.5 clears the floor and wins (47);
        # 49ers +3.5 bets and wins (24-20); the Rams total stays under the bar.
        self.assertEqual(registry["legs"]["record"], "2-0-0")
        self.assertEqual((registry["by_kind"]["side"]["record"], registry["by_kind"]["total"]["record"]), ("1-0-0", "1-0-0"))
        self.assertEqual(registry["pass_reasons"], {"adverse move": 1, "under threshold": 1})
        self.assertEqual(result["comparison"]["against"], "registry")
        self.assertTrue(result["comparison"]["knobs"]["edge_threshold"]["agree"])
        veto = result["veto"]
        # The Seahawks side's veto is a price veto: it appears under the
        # price knob at 5 and 10 cents and never under the spread or total.
        self.assertEqual([veto["price"][label]["bets"] for label in ("5", "10", "15", "20")], [2, 1, 0, 0])
        self.assertEqual(veto["price"]["10"]["legs"], [f"{HOME} -3.5 (+100) W"])
        self.assertEqual(veto["spread"]["0.5"]["bets"], 0)
        self.assertEqual(veto["total"]["1"]["bets"], 0)
        fitted = {"overrides": {"shrink_lambda": 0.0, "sigma_margin": 13.5, "margin_model": "normal", "edge_threshold": 0.03, "min_ev_per_unit": 0.02}}
        against = bt.run_ledger(rows, finals=finals, snapshots=[], fitted=fitted)["comparison"]
        self.assertEqual(against["against"], "backtest")
        self.assertEqual(against["knobs"]["shrink_lambda"]["reference"], 0.0)
        lines = bt.format_ledger_report(result)
        self.assertTrue(lines[0].startswith("Ledger refit: 2 graded"))
        self.assertIn("Informational only", lines[1])
        json.dumps(result)
        empty = bt.run_ledger([], finals=finals, snapshots=[])
        self.assertIsNone(empty["selection"])
        self.assertEqual(bt.format_ledger_report(empty)[-1], "  No graded rows; nothing to refit.")


class RealDataTests(unittest.TestCase):
    """Headline numbers on the committed data, cheap enough for the suite."""

    def test_seasons_and_the_elo_check(self) -> None:
        rows = bt.load_rows()
        self.assertEqual(len(bt.history_games(rows, [2023, 2024])), 544)
        self.assertEqual(len(bt.history_games(rows, [2025])), 272)
        params = bt.elo_parameters()
        self.assertEqual((params["k"], params["hfa"]), (19.0, 32.0))
        inputs = bt.rating_inputs(rows, seasons=[2025], params=params)
        self.assertEqual(len(inputs), 272)
        prior, _ = load_prior()
        finals = {(game["season"], game["week"], game["away_team"], game["home_team"]): game for game in bt.history_games(rows, [2025])}
        brier = sum((entry["elo_probability"] - bt.win_outcome(finals[key])) ** 2 for key, entry in inputs.items() if bt.win_outcome(finals[key]) is not None)
        count = sum(1 for key in inputs if bt.win_outcome(finals[key]) is not None)
        # The prior counts a tie as 0.5; ties are rare, so the two agree closely.
        self.assertAlmostEqual(brier / count, prior["fit"]["check_elo_brier"], delta=1e-3)
        self.assertEqual(inputs[(2025, 1, "San Francisco 49ers", "Seattle Seahawks")]["total"], float(prior["league_scoring_rate"]["mean_total"]) if prior["league_scoring_rate"]["season"] == 2024 else inputs[(2025, 1, "San Francisco 49ers", "Seattle Seahawks")]["total"])
        # A one-point grid runs end to end and the JSON is deterministic.
        base = bt.base_policy_block()
        point = [(bt.make_policy(base), bt.registry_ladder(base))]
        once = bt.replay_seasons(rows, seasons=[2025], points=point)
        again = bt.replay_seasons(rows, seasons=[2025], points=point)
        once.pop("elapsed_seconds")
        again.pop("elapsed_seconds")
        self.assertEqual(bt.json_text(once), bt.json_text(again))
        summary = next(iter(once["results"].values()))
        self.assertEqual(summary["games"], 272)
        self.assertAlmostEqual(summary["ml"]["elo_brier"], prior["fit"]["check_elo_brier"], delta=1e-3)


class CliTests(unittest.TestCase):
    def test_parser_modes(self) -> None:
        parser = build_parser()
        args = parser.parse_args(["grid", "--fit-seasons", "2023-2024", "--check-seasons", "2025"])
        self.assertEqual((args.mode, args.fit_seasons), ("grid", ["2023-2024"]))
        self.assertEqual(parser.parse_args(["veto"]).seasons, ["2024-2025"])
        self.assertEqual(parser.parse_args(["clv", "--grid-json", "x.json"]).grid_json, Path("x.json"))
        ledger = parser.parse_args(["ledger", "--rows-json", "r.json", "--finals-json", "f.json", "--include-pending"])
        self.assertTrue(ledger.include_pending)
        self.assertIsNone(parser.parse_args([]).mode)

    def test_ledger_mode_from_json_files(self) -> None:
        rows, finals = [], []
        for name, score in (("sea_rules", (20, 27)), ("lar_rules", (24, 20))):
            row = _fixture(name)
            row["review_status"] = "approved"
            rows.append(row)
            game = row["input_json"]["game"]
            finals.append({"event_id": "x", "kickoff_utc": game["commence_time_utc"], "away_team": game["away_team"], "home_team": game["home_team"], "away_score": score[0], "home_score": score[1]})
        with tempfile.TemporaryDirectory() as folder:
            rows_path = Path(folder) / "rows.json"
            finals_path = Path(folder) / "finals.json"
            out_path = Path(folder) / "out.json"
            rows_path.write_text(json.dumps(rows), encoding="utf-8")
            finals_path.write_text(json.dumps(finals), encoding="utf-8")
            stdout = io.StringIO()
            with contextlib.redirect_stdout(stdout):
                status = main(["--json", str(out_path), "ledger", "--rows-json", str(rows_path), "--finals-json", str(finals_path)])
            self.assertEqual(status, 0)
            self.assertIn("Ledger refit: 2 graded", stdout.getvalue())
            self.assertIn("Informational only", stdout.getvalue())
            written = json.loads(out_path.read_text(encoding="utf-8"))
            self.assertEqual(written["graded_games"], 2)
            with contextlib.redirect_stderr(io.StringIO()):
                with self.assertRaises(SystemExit):
                    main(["ledger", "--rows-json", str(rows_path)])


if __name__ == "__main__":
    unittest.main()
