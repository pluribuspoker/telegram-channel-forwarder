#!/usr/bin/env python3
"""Tests for the empirical margin table (roadmap WP6).

The build script, the lookup arithmetic, the ``margin_model`` policy switch,
the table's identity riding in the input, generation under ``empirical``, the
Week 1 replay, and the committed ``moe/priors/nfl_margins_v1.json`` against a
recomputation from ``data/nfl_lines_history.csv``. Unix only (``moe.py``
imports ``fcntl``).
"""

from __future__ import annotations

import hashlib
import json
import unittest
from pathlib import Path

import moe
from moe import generate_opinion, load_expert, validate_opinion
from moe_god import (
    DEFAULT_POLICY,
    DETERMINISTIC_BACKEND,
    MARGIN_MODELS,
    MARGINS_TABLE_PATH,
    ROOT,
    aggregator_policy,
    apply_policy,
    build_aggregator_input,
    build_judge_request,
    canonical_json,
    check_margin_table,
    cover_probability,
    empirical_survival,
    load_margin_table,
    load_registry,
    margin_table_descriptor,
    margin_table_for,
    normal_cdf,
    normalize_aggregator_opinion,
    over_probability,
    parse_margin_table,
    rules_arm_response,
    sha256_text,
)
from scripts.build_nfl_margins import (
    DEFAULT_MIN_GAMES,
    LINES_CSV,
    bin_key,
    build_document,
    build_table,
    game_from_row,
    half_point,
    parse_seasons,
    read_games,
    table_text,
    threshold_grid,
)
from scripts.test_moe_god import (
    AWAY,
    HOME,
    STEADY_HOME,
    MemoryStore,
    _committee,
    _game,
)

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "god_week1"


def _fixture(name: str) -> dict:
    return json.loads((FIXTURES / f"{name}.json").read_text(encoding="utf-8"))


def _row(
    season: int,
    away_score: str,
    home_score: str,
    home_spread: str,
    total: str,
    week: int = 1,
) -> dict[str, str]:
    return {
        "season": str(season),
        "week": str(week),
        "away_score": away_score,
        "home_score": home_score,
        "home_spread": home_spread,
        "total": total,
    }


def _toy_raw(min_games: int = 4) -> dict:
    """Two spread bins and one total bin; bin 3 is under the support bar."""
    return {
        "schema_version": 1,
        "version": "toy",
        "seasons": [2024],
        "games": 6,
        "bin_width": 1,
        "lattice_step": 0.5,
        "min_games": min_games,
        "spread": {
            "bins": {
                "-4": {"n": 4, "residuals": [[-1, 1], [0, 2], [1, 1]]},
                "3": {"n": 2, "residuals": [[-3, 1], [3, 1]]},
            }
        },
        "total": {
            "bins": {
                "44": {"n": 4, "residuals": [[-2, 1], [-0.5, 1], [0.5, 1], [2, 1]]},
            }
        },
    }


class BuildTests(unittest.TestCase):
    def test_residuals_bins_and_lattice(self) -> None:
        # Home favored by 3 wins 27-20: beat the expectation by 4. Total 47
        # against a 44.5 close: +2.5.
        game = game_from_row(_row(2024, "20", "27", "-3", "44.5"))
        self.assertEqual(game["margin_residual"], 4.0)
        self.assertEqual(game["total_residual"], 2.5)
        # Home underdog by 2.5 loses 17-24: the market expected -2.5, got -7.
        game = game_from_row(_row(2024, "24", "17", "2.5", "40"))
        self.assertEqual(game["margin_residual"], -4.5)
        self.assertEqual(game["total_residual"], 1.0)
        self.assertEqual([bin_key(v) for v in (-3.5, -4, -3, 0.5, 44.5, 44)], [-4, -4, -3, 0, 44, 44])
        self.assertEqual(half_point(2.49), 2.5)
        self.assertEqual(half_point(-0.25), 0.0)
        self.assertIsNone(game_from_row(_row(2024, "", "27", "-3", "44.5")))
        self.assertIsNone(game_from_row(_row(2024, "20", "27", "", "44.5")))
        self.assertEqual(parse_seasons(["2016-2018", "2025"]), [2016, 2017, 2018, 2025])
        with self.assertRaises(ValueError):
            parse_seasons(["2020-2016"])

    def test_table_bins_support_and_stable_text(self) -> None:
        rows = [
            _row(2023, "20", "27", "-3.5", "44.5"),   # bin -4, r=+3.5 ; total +2.5
            _row(2023, "24", "24", "-3", "44"),       # bin -3, r=-3   ; total +4
            _row(2024, "10", "31", "-3.5", "44.5"),   # bin -4, r=+17.5; total -3.5
            _row(2024, "21", "20", "-3.5", "44"),     # bin -4, r=-4.5 ; total -3
            _row(2025, "30", "13", "6", "50"),        # bin 6,  r=-11  ; total -7
        ]
        games = [game_from_row(row) for row in rows]
        table = build_table(games, seasons=[2023, 2024, 2025], min_games=3)
        spread = table["spread"]
        self.assertEqual(table["games"], 5)
        self.assertEqual(sorted(spread["bins"]), ["-3", "-4", "6"])
        self.assertEqual(spread["bins"]["-4"], {"n": 3, "residuals": [[-4.5, 1], [3.5, 1], [17.5, 1]]})
        self.assertEqual(spread["bins"]["-3"]["residuals"], [[-3, 1]])
        self.assertEqual((spread["supported_bins"], spread["supported_games"]), (1, 3))
        total = table["total"]
        self.assertEqual(sorted(total["bins"]), ["44", "50"])
        self.assertEqual(total["bins"]["44"], {"n": 4, "residuals": [[-3.5, 1], [-3, 1], [2.5, 1], [4, 1]]})
        self.assertEqual((total["supported_bins"], total["supported_games"]), (1, 4))
        self.assertEqual(table["min_games"], 3)
        self.assertEqual(table["source"]["close"], "nflverse")
        text = table_text(table)
        self.assertEqual(text, table_text(build_table(games, seasons=[2023, 2024, 2025], min_games=3)))
        self.assertTrue(text.endswith("\n"))
        self.assertNotIn("\r", text)
        self.assertEqual(json.loads(text), table)
        self.assertEqual(threshold_grid()[:3], [-10.0, -9.5, -9.0])
        self.assertEqual(len(threshold_grid()), 41)


class LookupTests(unittest.TestCase):
    def setUp(self) -> None:
        self.table = parse_margin_table(_toy_raw(), sha256="abc", path="toy.json")

    def test_parse_builds_the_survival_lattice(self) -> None:
        self.assertEqual(sorted(self.table["spread"]), [-4])  # bin 3 has 2 < 4 games
        entry = self.table["spread"][-4]
        self.assertEqual(entry["n"], 4)
        self.assertEqual(entry["lattice_start"], -1.5)
        self.assertEqual(entry["step"], 0.5)
        # u: -1.5, -1, -0.5, 0, 0.5, 1, 1.5 with counts 0,1,0,2,0,1,0.
        self.assertEqual(entry["survival"], [1.0, 0.875, 0.75, 0.5, 0.25, 0.125, 0.0])
        self.assertEqual((self.table["sha256"], self.table["path"], self.table["min_games"]), ("abc", "toy.json", 4))
        for bad in (
            {"schema_version": 2},
            {"spread": {"bins": {"-4": {"n": 5, "residuals": [[-1, 1], [0, 2], [1, 1]]}}}},
            {"spread": {"bins": {"-4": {"n": 4, "residuals": [[-1, 1], [0.25, 2], [1, 1]]}}}},
        ):
            with self.subTest(bad=bad):
                with self.assertRaises(ValueError):
                    parse_margin_table({**_toy_raw(), **bad})

    def test_survival_is_exact_on_the_lattice_and_linear_between(self) -> None:
        bins = self.table["spread"]
        self.assertEqual(empirical_survival(bins, -3.5, -1.0), 0.875)
        self.assertEqual(empirical_survival(bins, -4.0, 0.0), 0.5)
        self.assertEqual(empirical_survival(bins, -3.5, 1.0), 0.125)
        self.assertEqual(empirical_survival(bins, -3.5, -0.5), 0.75)  # unobserved point
        self.assertAlmostEqual(empirical_survival(bins, -3.5, -0.75), 0.8125, places=9)
        self.assertAlmostEqual(empirical_survival(bins, -3.5, 0.25), 0.375, places=9)
        self.assertEqual(empirical_survival(bins, -3.5, -1.5), 1.0)
        self.assertEqual(empirical_survival(bins, -3.5, -9.0), 1.0)
        self.assertEqual(empirical_survival(bins, -3.5, 1.5), 0.0)
        self.assertEqual(empirical_survival(bins, -3.5, 40.0), 0.0)
        self.assertIsNone(empirical_survival(bins, 3.5, 0.0))  # unsupported bin
        self.assertIsNone(empirical_survival(bins, -2.5, 0.0))  # absent bin
        # Half-push mass: P(r > 0) = 1/4, P(r = 0) = 2/4 -> 0.25 + 0.25.
        self.assertEqual(empirical_survival(bins, -3.5, 0.0), 0.25 + 0.5 / 2)

    def test_cover_and_over_use_the_table_and_fall_back(self) -> None:
        table = self.table
        # m + s = 4 - 3.5 = 0.5 -> t = -0.5 -> 0.75 (the normal says 0.5148).
        self.assertEqual(cover_probability(4.0, -3.5, 13.5, table=table), 0.75)
        self.assertAlmostEqual(cover_probability(4.0, -3.5, 13.5), 0.5148, places=4)
        self.assertEqual(cover_probability(4.0, -3.5, 13.5, table=None), cover_probability(4.0, -3.5, 13.5))
        # Bin 3 is unsupported: the empirical call equals the normal one.
        self.assertEqual(cover_probability(1.0, 3.5, 13.5, table=table), cover_probability(1.0, 3.5, 13.5))
        self.assertEqual(cover_probability(1.0, 3.5, 13.5), normal_cdf((1.0 + 3.5) / 13.5))
        # Totals: line 44.5, projection 45 -> t = -0.5 -> P(r > -0.5) + half of P(r = -0.5)
        # = (2 + 0.5) / 4.
        self.assertEqual(over_probability(45.0, 44.5, 13.5, table=table), 0.625)
        self.assertAlmostEqual(over_probability(45.0, 44.5, 13.5), 0.5148, places=4)
        self.assertEqual(over_probability(45.0, 52.0, 13.5, table=table), over_probability(45.0, 52.0, 13.5))
        # The 3-argument normal forms are the same numbers as before WP6.
        self.assertAlmostEqual(cover_probability(5, -3.5, 13.5), 0.5443, places=3)
        self.assertAlmostEqual(over_probability(46, 44.5, 13.5), 0.5443, places=3)

    def test_policy_edges_sum_to_zero_under_the_table(self) -> None:
        # The half-push convention keeps home + away and over + under at 1,
        # which apply_policy relies on for the mirrored edges.
        from moe_god import build_market_block

        market = build_market_block(_game(opening_home=STEADY_HOME))
        for model in MARGIN_MODELS:
            with self.subTest(model=model):
                policy = dict(aggregator_policy(load_registry()), margin_model=model)
                legs = apply_policy(
                    home_win_probability=0.62, expected_home_margin=4.0, projected_total=46.0,
                    market=market, policy=policy, away_team=AWAY, home_team=HOME,
                )
                edges = legs["edges"]
                self.assertAlmostEqual(edges["home_cover"] + edges["away_cover"], 0.0, places=3)
                self.assertAlmostEqual(edges["over"] + edges["under"], 0.0, places=3)
                self.assertAlmostEqual(legs["side"]["probability"] + (1 - legs["side"]["probability"]), 1.0, places=9)


class PolicyTests(unittest.TestCase):
    def test_margin_model_switch(self) -> None:
        base = dict(load_registry()["aggregator_policy"])
        self.assertEqual(base["margin_model"], "normal")  # stays normal until the check is read
        self.assertEqual(DEFAULT_POLICY["margin_model"], "normal")
        self.assertEqual(aggregator_policy({"aggregator_policy": {**base, "margin_model": "empirical"}})["margin_model"], "empirical")
        for bad in ("student_t", "", 1, True, None):
            with self.subTest(bad=bad):
                with self.assertRaises(ValueError):
                    aggregator_policy({"aggregator_policy": {**base, "margin_model": bad}})
        without = {key: value for key, value in base.items() if key != "margin_model"}
        self.assertEqual(aggregator_policy({"aggregator_policy": without})["margin_model"], "normal")
        self.assertIsNone(margin_table_for({}))
        self.assertIsNone(margin_table_for({"margin_model": "normal"}))
        table = margin_table_for({"margin_model": "empirical"})
        self.assertIs(table, load_margin_table())
        self.assertEqual(table["path"], "moe/priors/nfl_margins_v1.json")
        self.assertEqual(table["sha256"], hashlib.sha256(MARGINS_TABLE_PATH.read_bytes()).hexdigest())
        self.assertIsNone(margin_table_descriptor(None))
        self.assertEqual(
            set(margin_table_descriptor(table)),
            {"path", "sha256", "schema_version", "version", "seasons", "games", "min_games", "bin_width"},
        )


class InputTests(unittest.TestCase):
    def setUp(self) -> None:
        self.registry = load_registry()
        self.policy = aggregator_policy(self.registry)

    def _payload(self, model: str) -> dict:
        return build_aggregator_input(
            _game(), approved_opinions=_committee(), finals=[], snapshots=[],
            registry=self.registry, policy=dict(self.policy, margin_model=model),
        )

    def test_normal_input_records_no_table_and_the_request_names_the_model(self) -> None:
        payload = self._payload("normal")
        self.assertIn("margin_table", payload)
        self.assertIsNone(payload["margin_table"])
        self.assertEqual(payload["policy"]["margin_model"], "normal")
        request = build_judge_request(payload)
        self.assertEqual(request["policy"]["margin_model"], "normal")
        self.assertNotIn("margin_table", request)
        # An input persisted before the switch derives the same request as before.
        legacy = json.loads(json.dumps(payload))
        del legacy["margin_table"]
        del legacy["policy"]["margin_model"]
        legacy_request = build_judge_request(legacy)
        self.assertNotIn("margin_model", legacy_request["policy"])
        self.assertNotIn("margin_table", legacy_request)
        self.assertEqual(set(legacy_request["policy"]), {"sigma_margin", "sigma_total", "shrink_lambda", "edge_threshold"})
        check_margin_table(legacy)  # nothing recorded, nothing to check

    def test_empirical_input_carries_the_table_identity(self) -> None:
        payload = self._payload("empirical")
        table = load_margin_table()
        self.assertEqual(payload["margin_table"], margin_table_descriptor(table))
        self.assertEqual(payload["margin_table"]["sha256"], table["sha256"])
        self.assertEqual(payload["margin_table"]["path"], "moe/priors/nfl_margins_v1.json")
        self.assertEqual(payload["margin_table"]["seasons"], list(range(2016, 2026)))
        request = build_judge_request(payload)
        self.assertEqual(request["policy"]["margin_model"], "empirical")
        self.assertEqual(request["margin_table"], payload["margin_table"])
        # The committee key ignores the table; the input hash does not.
        normal = self._payload("normal")
        self.assertEqual(payload["committee_key"], normal["committee_key"])
        self.assertNotEqual(sha256_text(canonical_json(payload)), sha256_text(canonical_json(normal)))
        # Voices' derived probabilities and the pooled edges come from the table.
        by_id = {voice["voice_id"]: voice for voice in payload["voices"]}
        normal_by_id = {voice["voice_id"]: voice for voice in normal["voices"]}
        self.assertNotEqual(by_id["schedule"]["derived"], normal_by_id["schedule"]["derived"])
        latest = payload["market"]["latest"]
        self.assertEqual(
            by_id["schedule"]["derived"]["p_cover_home"],
            round(cover_probability(6, latest["home_spread"], 13.5, table=table), 4),
        )
        self.assertNotEqual(payload["feature_block"]["edges_if_shrunk"], normal["feature_block"]["edges_if_shrunk"])
        self.assertEqual(payload["feature_block"]["shrunk"], normal["feature_block"]["shrunk"])

    def test_foreign_table_is_refused(self) -> None:
        payload = self._payload("empirical")
        check_margin_table(payload)
        tampered = json.loads(json.dumps(payload))
        tampered["margin_table"]["sha256"] = "0" * 64
        with self.assertRaises(ValueError):
            check_margin_table(tampered)
        with self.assertRaises(ValueError):
            normalize_aggregator_opinion(rules_arm_response(tampered), tampered, expert=load_expert("god_rules"))


class GenerationTests(unittest.IsolatedAsyncioTestCase):
    async def test_rules_arm_persists_the_table_and_bets_by_it(self) -> None:
        registry = load_registry()
        policy = dict(aggregator_policy(registry), margin_model="empirical")
        payload = build_aggregator_input(
            _game(opening_home=STEADY_HOME), approved_opinions=_committee(), finals=[], snapshots=[],
            registry=registry, policy=policy,
        )
        store = MemoryStore()
        row = await generate_opinion(
            expert_id="god_rules", game=_game(opening_home=STEADY_HOME), history=[],
            input_payload=payload, store=store, generation_backend=DETERMINISTIC_BACKEND,
        )
        self.assertEqual(row["generation_status"], "valid")
        persisted = json.loads(row["input_json"])
        self.assertEqual(persisted["policy"]["margin_model"], "empirical")
        self.assertEqual(persisted["margin_table"]["sha256"], load_margin_table()["sha256"])
        estimate = rules_arm_response(payload)
        expected = apply_policy(
            home_win_probability=estimate["home_win_probability"],
            expected_home_margin=estimate["expected_home_margin"],
            projected_total=estimate["projected_total"],
            market=payload["market"], policy=policy, away_team=AWAY, home_team=HOME,
        )
        self.assertEqual(json.loads(row["side_pick_json"]), expected["side"])
        self.assertEqual(json.loads(row["total_pick_json"]), expected["total"])
        as_normal = apply_policy(
            home_win_probability=estimate["home_win_probability"],
            expected_home_margin=estimate["expected_home_margin"],
            projected_total=estimate["projected_total"],
            market=payload["market"], policy=dict(policy, margin_model="normal"),
            away_team=AWAY, home_team=HOME,
        )
        self.assertNotEqual(expected["side"]["probability"], as_normal["side"]["probability"])
        opinion = normalize_aggregator_opinion(estimate, payload, expert=load_expert("god_rules"))
        validate_opinion(opinion, away_team=AWAY, home_team=HOME, schedule_input=payload)

    def test_source_hash_covers_the_table(self) -> None:
        expert = load_expert("god_rules")
        before = moe._source_sha256(expert)
        original = moe.MARGINS_TABLE_PATH
        # _source_sha256 hashes repo-relative paths, so the stand-in lives
        # beside the real file for the duration of the test.
        other = MARGINS_TABLE_PATH.with_name("nfl_margins_v1.test-only.json")
        other.write_bytes(MARGINS_TABLE_PATH.read_bytes() + b"\n")
        moe.MARGINS_TABLE_PATH = other
        try:
            changed = moe._source_sha256(expert)
        finally:
            moe.MARGINS_TABLE_PATH = original
            other.unlink()
        self.assertNotEqual(before, changed)
        self.assertEqual(moe._source_sha256(expert), before)
        # Non-aggregator experts do not hash the table.
        schedule = load_expert("schedule")
        self.assertEqual(moe._source_sha256(schedule), moe._source_sha256(schedule))


class Week1ReplayTests(unittest.TestCase):
    """The persisted Week 1 rules estimates under the table vs the normal model."""

    def _legs(self, name: str, model: str) -> tuple[dict, dict]:
        row = _fixture(name)
        payload = row["input_json"]
        self.assertNotIn("margin_table", payload)
        self.assertNotIn("margin_model", payload["policy"])
        estimate = rules_arm_response(payload)
        game = payload["game"]
        legs = apply_policy(
            home_win_probability=estimate["home_win_probability"],
            expected_home_margin=estimate["expected_home_margin"],
            projected_total=estimate["projected_total"],
            market=payload["market"],
            policy={**DEFAULT_POLICY, **payload["policy"], "margin_model": model},
            away_team=game["away_team"], home_team=game["home_team"],
        )
        return row, legs

    def test_legacy_policy_replays_as_normal(self) -> None:
        for name in ("sea_rules", "lar_rules"):
            with self.subTest(name=name):
                row = _fixture(name)
                payload = row["input_json"]
                estimate = rules_arm_response(payload)
                game = payload["game"]
                as_persisted = apply_policy(
                    home_win_probability=estimate["home_win_probability"],
                    expected_home_margin=estimate["expected_home_margin"],
                    projected_total=estimate["projected_total"],
                    market=payload["market"], policy=payload["policy"],
                    away_team=game["away_team"], home_team=game["home_team"],
                )
                _row, as_normal = self._legs(name, "normal")
                self.assertEqual(as_persisted, as_normal)

    def test_seahawks_over_edge_collapses_under_the_table(self) -> None:
        _row, normal = self._legs("sea_rules", "normal")
        _row, empirical = self._legs("sea_rules", "empirical")
        self.assertEqual((normal["p_cover_home"], normal["p_over"]), (0.5112, 0.5222))
        self.assertEqual((empirical["p_cover_home"], empirical["p_over"]), (0.5129, 0.4908))
        # The side is still vetoed on the -110 -> +100 move; its edge grew a hair.
        self.assertEqual(empirical["side"]["selection"], "PASS")
        self.assertEqual(empirical["side"]["pass_reason"], "adverse move")
        self.assertEqual((normal["side"]["edge"], empirical["side"]["edge"]), (0.0329, 0.0346))
        # Over 44.5 against a 45.25 projection: 3.3% edge under the normal
        # model (floored), 0.16% under the table (a plain pass).
        self.assertEqual((normal["total"]["edge"], normal["total"]["pass_reason"]), (0.033, "ev floor"))
        self.assertEqual((empirical["total"]["edge"], empirical["total"]["pass_reason"]), (0.0016, None))
        self.assertEqual(empirical["total"]["selection"], "PASS")
        self.assertEqual(empirical["notes"], ["adverse move: Seattle Seahawks spread price -110 → +100 (+10 cents) since open"])

    def test_rams_side_edge_grows_under_the_table(self) -> None:
        _row, normal = self._legs("lar_rules", "normal")
        _row, empirical = self._legs("lar_rules", "empirical")
        self.assertEqual((normal["p_cover_home"], empirical["p_cover_home"]), (0.4558, 0.4326))
        self.assertEqual((normal["p_over"], empirical["p_over"]), (0.5035, 0.5164))
        side = empirical["side"]
        self.assertEqual((side["selection"], side["line"], side["price"]), ("San Francisco 49ers", 3.5, -110))
        self.assertEqual((normal["side"]["edge"], side["edge"]), (0.0442, 0.0674))
        self.assertEqual((normal["side"]["ev_per_unit"], side["ev_per_unit"]), (0.039, 0.0832))
        self.assertEqual((normal["side"]["confidence_stars"], side["confidence_stars"]), (1, 2))
        self.assertEqual((normal["side"]["stake_units"], side["stake_units"]), (1.1, 2.3))
        self.assertEqual(empirical["total"]["selection"], "PASS")
        self.assertEqual((normal["total"]["edge"], empirical["total"]["edge"]), (0.0073, 0.0056))
        self.assertEqual(empirical["notes"], [])


class CommittedTableTests(unittest.TestCase):
    """moe/priors/nfl_margins_v1.json is exactly what the build script produces."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.raw = json.loads(MARGINS_TABLE_PATH.read_text(encoding="utf-8"))

    def test_shape_and_counts(self) -> None:
        raw = self.raw
        self.assertEqual((raw["schema_version"], raw["version"]), (1, "nfl_margins_v1"))
        self.assertEqual(raw["seasons"], list(range(2016, 2026)))
        self.assertEqual(raw["games"], 2639)
        self.assertEqual((raw["bin_width"], raw["lattice_step"], raw["min_games"]), (1, 0.5, DEFAULT_MIN_GAMES))
        self.assertEqual(raw["source"]["close"], "nflverse")
        self.assertEqual(raw["source"]["file"], "data/nfl_lines_history.csv")
        for market in ("spread", "total"):
            with self.subTest(market=market):
                block = raw[market]
                self.assertEqual(block["games"], 2639)
                self.assertEqual(sum(entry["n"] for entry in block["bins"].values()), 2639)
                for label, entry in block["bins"].items():
                    int(label)
                    self.assertEqual(sum(count for _value, count in entry["residuals"]), entry["n"])
                    values = [value for value, _count in entry["residuals"]]
                    self.assertEqual(values, sorted(values))
                    self.assertTrue(all(float(value * 2).is_integer() for value in values))
                supported = [entry for entry in block["bins"].values() if entry["n"] >= raw["min_games"]]
                self.assertEqual(block["supported_bins"], len(supported))
                self.assertEqual(block["supported_games"], sum(entry["n"] for entry in supported))
        self.assertEqual((raw["spread"]["supported_bins"], raw["spread"]["supported_games"]), (21, 2494))
        self.assertEqual((raw["total"]["supported_bins"], raw["total"]["supported_games"]), (20, 2592))
        table = load_margin_table()
        self.assertEqual(sorted(table["spread"]), [-14, -13, -11, -10, -9, -8, -7, -6, -5, -4, -3, -2, -1, 1, 2, 3, 4, 5, 6, 7, 10])
        self.assertEqual(sorted(table["total"]), list(range(36, 56)))
        # Market expectation (t = 0) sits near a coin flip in the big bins.
        for spread in (-3.5, -3, -7, 3, 6.5):
            self.assertAlmostEqual(cover_probability(-spread, spread, 13.5, table=table), 0.5, delta=0.06)

    def test_file_is_the_script_output_and_the_check_recomputes(self) -> None:
        document, calibration = build_document(
            LINES_CSV,
            seasons=list(range(2016, 2026)),
            fit_seasons=list(range(2016, 2025)),
            check_seasons=[2025],
            min_games=DEFAULT_MIN_GAMES,
            sigma_margin=float(DEFAULT_POLICY["sigma_margin"]),
            sigma_total=float(DEFAULT_POLICY["sigma_total"]),
        )
        self.assertEqual(calibration, self.raw["calibration"])
        self.assertEqual(table_text(document), MARGINS_TABLE_PATH.read_text(encoding="utf-8"))
        # The hold-out numbers of record.
        spread, total = calibration["spread"], calibration["total"]
        self.assertEqual((calibration["fit_games"], calibration["check_games"]), (2367, 272))
        self.assertEqual((spread["supported_games"], total["supported_games"]), (244, 266))
        self.assertEqual((spread["grid_mean"]["empirical"]["brier"], spread["grid_mean"]["normal"]["brier"]), (0.216106, 0.21606))
        self.assertEqual((total["grid_mean"]["empirical"]["brier"], total["grid_mean"]["normal"]["brier"]), (0.219466, 0.21894))
        self.assertLess(spread["grid_mean"]["empirical"]["log_loss"], spread["grid_mean"]["normal"]["log_loss"])
        self.assertLess(spread["key_numbers"]["-7"]["empirical"]["brier"], spread["key_numbers"]["-7"]["normal"]["brier"])
        self.assertGreater(spread["key_numbers"]["3"]["empirical"]["brier"], spread["key_numbers"]["3"]["normal"]["brier"])
        self.assertEqual(set(calibration["min_games_sensitivity"]), {"30", "50", "100"})
        games = read_games(LINES_CSV, range(2016, 2026))
        self.assertEqual(len(games), 2639)


if __name__ == "__main__":
    unittest.main()
