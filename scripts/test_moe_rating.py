#!/usr/bin/env python3
"""Tests for the Elo rating voice (``rating_elo``): the Elo arithmetic, the
committed prior reproduced from ``data/nfl_lines_history.csv``, the rating
input and its deterministic response, generation on the deterministic
backend, the weekly generator, and the bulk review mode. Offline; Unix
only because ``moe.py`` imports ``fcntl``."""

from __future__ import annotations

import contextlib
import hashlib
import io
import json
import math
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from moe import (
    OPINION_HEADERS,
    generate_opinion,
    load_expert,
    opinion_detail,
    opinion_output_sha256,
    opinion_summary,
    validate_opinion,
)
from moe_god import (
    DETERMINISTIC_BACKEND,
    DETERMINISTIC_MODEL,
    VOICE_LENSES,
    _scores_from_estimate,
    aggregator_policy,
    build_aggregator_input,
    build_judge_request,
    canonical_json,
    load_registry,
    select_voice_rows,
    sha256_text,
)
from moe_rating import (
    BASE_RATING,
    ELO_PRIOR_PATH,
    LINES_CSV_PATH,
    PRIOR_SCHEMA_VERSION,
    RATING_EXPERT_ID,
    RATING_MODE,
    RATING_PROFILE,
    STAR_MARGINS,
    apply_finals,
    brier_score,
    build_rating_input,
    closing_moneyline_brier,
    elo_update,
    expected_margin,
    finals_before_kickoff,
    fit_parameters,
    fit_points_per_elo,
    league_scoring_rate,
    load_prior,
    margin_multiplier,
    margin_rmse,
    normalize_rating_opinion,
    preseason_ratings,
    rating_response,
    read_lines_csv,
    regress_ratings,
    replay_games,
    stars_for_margin,
    win_probability,
)
from nfl_win_predictions import TEAM_ABBREVIATIONS
from scripts.generate_rating_week import existing_row, generate_week, week_games
from scripts.god_judge_runner import committee_experts
from scripts.review_moe_opinion import (
    approve_rows,
    format_week_table,
    parse_args,
    week_rows,
)
from scripts.test_moe_god import EVENT_ID, KICKOFF, _committee, _game, _opinion

AWAY = "New England Patriots"
HOME = "Seattle Seahawks"
REVIEW_SCRIPT = ROOT / "scripts" / "review_moe_opinion.py"


class MemoryStore:
    def __init__(self) -> None:
        self.rows: list[dict] = []
        self.reviews: list[tuple[str, str, str, str]] = []

    def append(self, row: dict) -> None:
        self.rows.append(dict(row))

    def list(self, event_id: str | None = None) -> list[dict]:
        if event_id is None:
            return list(self.rows)
        return [row for row in self.rows if str(row.get("event_id")) == str(event_id)]

    def review(self, opinion_id: str, *, status: str, reviewed_by: str, note: str) -> None:
        self.reviews.append((opinion_id, status, reviewed_by, note))
        for row in self.rows:
            if row["opinion_id"] == opinion_id:
                row["review_status"] = status
                row["reviewed_by"] = reviewed_by


def _final(
    event_id: str,
    kickoff: str,
    away: str,
    home: str,
    away_score: int,
    home_score: int,
    *,
    week: int = 1,
    season: int = 2026,
    status: str = "final",
) -> dict:
    return {
        "event_id": event_id,
        "season": season,
        "week": week,
        "status": status,
        "kickoff_utc": kickoff,
        "away_team": away,
        "home_team": home,
        "away_score": away_score,
        "home_score": home_score,
    }


def _finals() -> list[dict]:
    """Three finals before the Week 1 Seahawks kickoff plus one at kickoff."""
    return [
        _final("f1", "2026-09-06T17:00:00+00:00", HOME, "Denver Broncos", 24, 20),
        _final("f2", "2026-09-06T20:25:00+00:00", AWAY, "Buffalo Bills", 17, 27),
        _final("f3", "2026-09-07T00:20:00+00:00", "Houston Texans", HOME, 21, 21),
        # Kicks off with the game itself: strictly-before excludes it.
        _final("f4", KICKOFF, "Dallas Cowboys", AWAY, 3, 30),
    ]


def _csv_row(season: int, week: int, gameday: str, away: str, home: str, away_score, home_score, **extra) -> dict:
    row = {
        "season": str(season),
        "week": str(week),
        "gameday": gameday,
        "gametime": "",
        "away_team": away,
        "home_team": home,
        "away_score": "" if away_score is None else str(away_score),
        "home_score": "" if home_score is None else str(home_score),
        "home_spread": "",
        "home_moneyline": "",
        "away_moneyline": "",
    }
    row.update({key: str(value) for key, value in extra.items()})
    return row


PARAMS = {"k": 20.0, "hfa": 40.0, "regression": 0.25, "points_per_elo": 25.0}


class ArithmeticTests(unittest.TestCase):
    def test_win_probability_and_margin(self) -> None:
        self.assertEqual(win_probability(0), 0.5)
        self.assertAlmostEqual(win_probability(400), 10 / 11, places=9)
        self.assertAlmostEqual(win_probability(130) + win_probability(-130), 1.0, places=12)
        self.assertEqual(expected_margin(44, 22), 2.0)
        self.assertEqual(expected_margin(-55, 25), -2.2)

    def test_margin_multiplier(self) -> None:
        self.assertEqual(margin_multiplier(0, 100), 0.0)  # a tie moves nothing
        self.assertAlmostEqual(margin_multiplier(7, 0), math.log(8), places=12)
        # A favorite's blowout is damped; an upset of the same size is amplified.
        self.assertAlmostEqual(margin_multiplier(7, 1000), math.log(8) * 2.2 / 3.2, places=12)
        self.assertGreater(margin_multiplier(7, -300), margin_multiplier(7, 300))
        self.assertEqual(margin_multiplier(-7, 0), margin_multiplier(7, 0))

    def test_elo_update_is_zero_sum(self) -> None:
        home, away, p_home = elo_update(1500, 1500, 27, 20, PARAMS)
        self.assertAlmostEqual(p_home, win_probability(40), places=12)
        self.assertGreater(home, 1500)
        self.assertAlmostEqual(home - 1500, 1500 - away, places=12)
        self.assertAlmostEqual(home - 1500, 20 * margin_multiplier(7, 40) * (1 - p_home), places=12)
        home, away, _ = elo_update(1500, 1500, 20, 27, PARAMS)
        self.assertLess(home, 1500)
        self.assertAlmostEqual(home - 1500, 1500 - away, places=12)
        tied_home, tied_away, _ = elo_update(1520, 1480, 21, 21, PARAMS)
        self.assertEqual((tied_home, tied_away), (1520.0, 1480.0))

    def test_regression_and_stars(self) -> None:
        regressed = regress_ratings({"A": 1600.0, "B": 1400.0}, 1 / 3, 1500.0)
        self.assertAlmostEqual(regressed["A"], 1566.6667, places=4)
        self.assertAlmostEqual(regressed["B"], 1433.3333, places=4)
        self.assertEqual(regress_ratings({"A": 1600.0}, 0.0), {"A": 1600.0})
        self.assertEqual(regress_ratings({"A": 1600.0}, 1.0), {"A": 1500.0})
        self.assertEqual(STAR_MARGINS, (3.0, 7.0, 10.0, 14.0))
        self.assertEqual([stars_for_margin(m) for m in (0.5, 2.99, 3, 6.9, 7, 10, 13.9, 14, 30)], [1, 1, 2, 2, 3, 4, 4, 5, 5])
        self.assertEqual(stars_for_margin(-7.5), 3)


class ReplayTests(unittest.TestCase):
    def _rows(self) -> list[dict]:
        return [
            # Deliberately out of order; the replay sorts by season, week, day.
            _csv_row(2024, 2, "2024-09-15", "B", "A", 10, 20),
            _csv_row(2024, 1, "2024-09-08", "A", "B", 17, 27),
            _csv_row(2025, 1, "2025-09-07", "A", "B", 24, 21, home_spread="-3", home_moneyline="-150", away_moneyline="130"),
            _csv_row(2025, 2, "2025-09-14", "B", "A", None, None),  # unplayed
        ]

    def test_order_regression_and_predictions(self) -> None:
        replay = replay_games(self._rows(), PARAMS)
        predictions = replay["predictions"]
        self.assertEqual([(p["season"], p["week"]) for p in predictions], [(2024, 1), (2024, 2), (2025, 1)])
        # Week 1 2024: fresh ratings, home field only.
        self.assertAlmostEqual(predictions[0]["home_win_probability"], win_probability(40), places=12)
        self.assertEqual(predictions[0]["outcome"], 1.0)
        self.assertEqual(predictions[0]["home_margin"], 10)
        # Week 2 uses the updated ratings, no regression inside a season.
        home_after, away_after, _ = elo_update(1500, 1500, 27, 17, PARAMS)
        self.assertAlmostEqual(predictions[1]["adjusted_gap"], away_after - home_after + 40, places=9)
        # The 2025 opener regresses both teams a quarter of the way back first.
        after_2024 = replay_games(self._rows()[:2], PARAMS)["ratings"]
        regressed = regress_ratings(after_2024, 0.25)
        self.assertAlmostEqual(predictions[2]["adjusted_gap"], regressed["B"] - regressed["A"] + 40, places=9)
        self.assertEqual(predictions[2]["home_spread"], -3.0)
        self.assertEqual(replay["last_season"], 2025)
        self.assertEqual(set(replay["ratings"]), {"A", "B"})
        # collect_from keeps only the later seasons' predictions.
        self.assertEqual([p["season"] for p in replay_games(self._rows(), PARAMS, collect_from=2025)["predictions"]], [2025])

    def test_scores(self) -> None:
        replay = replay_games(self._rows(), PARAMS)
        brier, games = brier_score(replay["predictions"], [2024])
        self.assertEqual(games, 2)
        expected = ((win_probability(40) - 1) ** 2 + (replay["predictions"][1]["home_win_probability"] - 1) ** 2) / 2
        self.assertAlmostEqual(brier, expected, places=12)
        self.assertEqual(brier_score(replay["predictions"], [2030]), (None, 0))
        ml, ml_games = closing_moneyline_brier(replay["predictions"], [2025])
        self.assertEqual(ml_games, 1)
        fair_home = (150 / 250) / ((150 / 250) + (100 / 230))
        self.assertAlmostEqual(ml, (fair_home - 0) ** 2, places=12)  # A won on the road
        self.assertEqual(closing_moneyline_brier(replay["predictions"], [2024]), (None, 0))
        rmse, count = margin_rmse(replay["predictions"], [2025], source="closing_spread")
        self.assertEqual((rmse, count), (abs(3 - (-3)), 1))
        elo_rmse, _ = margin_rmse(replay["predictions"], [2025], source="elo")
        self.assertAlmostEqual(elo_rmse, abs(replay["predictions"][2]["expected_home_margin"] - (-3)), places=12)
        self.assertEqual(league_scoring_rate(self._rows(), 2024), {"season": 2024, "mean_total": 37.0, "games": 2})

    def test_points_per_elo_least_squares(self) -> None:
        predictions = [
            {"season": 2024, "adjusted_gap": 22.0, "home_margin": 1},
            {"season": 2024, "adjusted_gap": 44.0, "home_margin": 2},
            {"season": 2025, "adjusted_gap": 1000.0, "home_margin": -50},
        ]
        self.assertAlmostEqual(fit_points_per_elo(predictions, [2024]), 22.0, places=12)
        self.assertIsNone(fit_points_per_elo(predictions, [2025]))

    def test_fit_picks_the_grid_minimum(self) -> None:
        rows = self._rows()
        k_grid, hfa_grid, regression_grid = [10.0, 30.0], [0.0, 60.0], [0.25, 0.5]
        best = fit_parameters(rows, fit_seasons=[2025], k_grid=k_grid, hfa_grid=hfa_grid, regression_grid=regression_grid)
        self.assertEqual(best["evaluated"], 8)
        self.assertEqual(best["games"], 1)
        brute = min(
            (
                brier_score(replay_games(rows, {"k": k, "hfa": hfa, "regression": regression, "points_per_elo": 25.0}, collect_from=2025)["predictions"], [2025])[0],
                regression,
                k,
                hfa,
            )
            for regression in regression_grid
            for k in k_grid
            for hfa in hfa_grid
        )
        self.assertAlmostEqual(best["brier"], brute[0], places=12)
        self.assertEqual((best["params"]["regression"], best["params"]["k"], best["params"]["hfa"]), brute[1:])


class PriorTests(unittest.TestCase):
    """The committed prior is exactly what the CSV and the stored parameters
    reproduce; a changed CSV or a hand-edited prior fails here."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.prior, cls.digest = load_prior()
        cls.rows = [row for row in read_lines_csv() if row["home_score"] != ""]

    def test_shape(self) -> None:
        prior = self.prior
        self.assertEqual(prior["schema_version"], PRIOR_SCHEMA_VERSION)
        self.assertEqual(prior["through_season"], 2025)
        self.assertEqual(set(prior["ratings"]), set(TEAM_ABBREVIATIONS))
        self.assertEqual(set(prior["parameters"]), {"k", "hfa", "regression", "points_per_elo"})
        self.assertEqual(prior["base_rating"], BASE_RATING)
        self.assertEqual(prior["league_scoring_rate"]["season"], 2025)
        self.assertEqual(prior["league_scoring_rate"]["games"], 272)
        self.assertEqual(prior["fit"]["fit_seasons"], [2023, 2024])
        self.assertEqual(prior["fit"]["check_season"], 2025)
        self.assertEqual(prior["source"]["path"], "data/nfl_lines_history.csv")
        self.assertEqual(prior["source"]["seasons"], "1999-2025")
        self.assertEqual(len(self.digest), 64)
        self.assertEqual(
            hashlib.sha256(LINES_CSV_PATH.read_bytes()).hexdigest(),
            prior["source"]["sha256"],
            "data/nfl_lines_history.csv changed since the prior was fit; re-run scripts/fit_nfl_elo.py",
        )

    def test_replay_reproduces_ratings_and_check_numbers(self) -> None:
        prior, rows = self.prior, self.rows
        params = prior["parameters"]
        end = replay_games([row for row in rows if int(row["season"]) <= 2025], params)
        self.assertEqual(end["last_season"], 2025)
        for team, stored in prior["ratings"].items():
            self.assertAlmostEqual(end["ratings"][team], stored, places=3, msg=team)
        replay = replay_games(rows, params, collect_from=2023)
        fit_brier, fit_games = brier_score(replay["predictions"], [2023, 2024])
        self.assertEqual(fit_games, prior["fit"]["fit_games"])
        self.assertEqual(round(fit_brier, 6), prior["fit"]["fit_brier"])
        check_brier, check_games = brier_score(replay["predictions"], [2025])
        ml_brier, ml_games = closing_moneyline_brier(replay["predictions"], [2025])
        self.assertEqual((check_games, ml_games), (272, 272))
        self.assertEqual(round(check_brier, 6), prior["fit"]["check_elo_brier"])
        self.assertEqual(round(ml_brier, 6), prior["fit"]["check_closing_ml_brier"])
        difference = check_brier - ml_brier
        self.assertEqual(round(difference, 6), prior["fit"]["check_brier_difference"])
        self.assertEqual(prior["fit"]["target_within"], 0.01)
        self.assertEqual(prior["fit"]["target_met"], difference <= 0.01)
        # The roadmap's target is within 0.01 of the closing moneyline; the
        # fitted voice sits 0.0108 behind it on 2025 (recorded as not met).
        # A corrupted prior or CSV would move this by far more.
        self.assertLess(difference, 0.02)
        self.assertEqual(league_scoring_rate(rows, 2025), prior["league_scoring_rate"])
        elo_rmse, _ = margin_rmse(replay["predictions"], [2025], source="elo")
        self.assertEqual(round(elo_rmse, 4), prior["fit"]["check_margin_rmse_elo"])


class InputTests(unittest.TestCase):
    def setUp(self) -> None:
        self.prior, self.digest = load_prior()

    def test_finals_before_kickoff(self) -> None:
        kickoff = _final("x", KICKOFF, AWAY, HOME, 0, 0)
        finals = _finals() + [
            _final("late", "2026-09-13T17:00:00+00:00", AWAY, HOME, 7, 3),
            _final("pending", "2026-09-06T17:00:00+00:00", "Miami Dolphins", "New York Jets", 0, 0, status="scheduled"),
            _final("other-season", "2026-09-06T17:00:00+00:00", "Miami Dolphins", "New York Jets", 20, 17, season=2025),
            {**_final("no-score", "2026-09-06T17:00:00+00:00", "Miami Dolphins", "New York Jets", 0, 0), "home_score": ""},
        ]
        kept = finals_before_kickoff(reversed(finals), season=2026, kickoff=__import__("datetime").datetime.fromisoformat(kickoff["kickoff_utc"]))
        self.assertEqual([row["event_id"] for row in kept], ["f1", "f2", "f3"])

    def test_input_replays_finals_and_is_deterministic(self) -> None:
        payload = build_rating_input(_game(), _finals())
        again = build_rating_input(_game(), list(reversed(_finals())))
        self.assertEqual(canonical_json(payload), canonical_json(again))
        self.assertEqual(payload["input_profile"], RATING_PROFILE)
        self.assertEqual(payload["game"]["event_id"], EVENT_ID)
        self.assertEqual(payload["game"]["week"], 1)
        self.assertEqual(payload["prior"]["sha256"], self.digest)
        self.assertEqual(payload["prior"]["path"], "moe/priors/nfl_elo_v1.json")
        self.assertEqual(payload["prior"]["parameters"], self.prior["parameters"])
        self.assertEqual(payload["season"], {"season": 2026, "preseason_regression": self.prior["parameters"]["regression"], "finals_applied": 3, "finals_through_utc": "2026-09-07T00:20:00+00:00"})
        home, away = payload["ratings"]["home"], payload["ratings"]["away"]
        self.assertEqual((home["team"], home["games_played"], home["record"]), (HOME, 2, "1-0-1"))
        self.assertEqual((away["team"], away["games_played"], away["record"]), (AWAY, 1, "0-1"))
        preseason = preseason_ratings(self.prior, 2026)
        self.assertEqual(home["preseason_rating"], round(preseason[HOME], 2))
        replayed, records = apply_finals(preseason, _finals()[:3], self.prior["parameters"])
        self.assertEqual(home["rating"], round(replayed[HOME], 2))
        self.assertEqual(records[HOME], {"games": 2, "wins": 1, "losses": 0, "ties": 1})
        # The estimate reproduces from the ratings as written.
        estimate = payload["estimate"]
        gap = round(home["rating"] - away["rating"], 2)
        adjusted = round(gap + self.prior["parameters"]["hfa"], 2)
        self.assertEqual((estimate["rating_gap"], estimate["adjusted_gap"]), (gap, adjusted))
        self.assertEqual(estimate["home_win_probability"], round(win_probability(adjusted), 4))
        self.assertEqual(estimate["expected_home_margin"], round(adjusted / self.prior["parameters"]["points_per_elo"], 2))
        self.assertEqual(estimate["projected_total"], self.prior["league_scoring_rate"]["mean_total"])
        away_score, home_score = _scores_from_estimate(estimate["home_win_probability"], estimate["expected_home_margin"], estimate["projected_total"])
        self.assertEqual((estimate["predicted_away_score"], estimate["predicted_home_score"]), (away_score, home_score))
        self.assertEqual(estimate["confidence_stars"], stars_for_margin(estimate["expected_home_margin"]))
        self.assertEqual(estimate["predicted_winner"], HOME if estimate["home_win_probability"] > 0.5 else AWAY)
        self.assertIsNone(estimate["tie_break"])
        # Without finals the ratings are the regressed preseason ones.
        preseason_payload = build_rating_input(_game(), [])
        self.assertEqual(preseason_payload["season"]["finals_applied"], 0)
        self.assertEqual(preseason_payload["ratings"]["home"]["rating"], preseason_payload["ratings"]["home"]["preseason_rating"])
        self.assertNotEqual(sha256_text(canonical_json(preseason_payload)), sha256_text(canonical_json(payload)))

    def test_prior_season_guards(self) -> None:
        with self.assertRaises(ValueError):
            build_rating_input(dict(_game(), season=2025), [])
        with self.assertRaises(ValueError):
            build_rating_input(dict(_game(), season=2027), [])
        with self.assertRaises(ValueError):
            build_rating_input(_game(home="Oakland Raiders"), [])

    def test_exact_offset_leans_home(self) -> None:
        prior = json.loads(ELO_PRIOR_PATH.read_text(encoding="utf-8"))
        hfa = prior["parameters"]["hfa"]
        # After a one-third regression the home team sits exactly hfa below
        # the away team, so the adjusted gap is 0.
        prior["ratings"][HOME] = BASE_RATING - hfa * 1.5
        prior["ratings"][AWAY] = BASE_RATING
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "prior.json"
            path.write_text(json.dumps(prior), encoding="utf-8")
            payload = build_rating_input(_game(), [], prior_path=path)
        estimate = payload["estimate"]
        self.assertEqual(estimate["adjusted_gap"], 0.0)
        self.assertEqual(estimate["home_win_probability"], 0.5001)
        self.assertEqual(estimate["expected_home_margin"], 0.01)
        self.assertEqual(estimate["predicted_winner"], HOME)
        self.assertIn("home side breaks the tie", estimate["tie_break"])
        self.assertNotEqual(estimate["predicted_away_score"], estimate["predicted_home_score"])
        opinion = normalize_rating_opinion(rating_response(payload), payload)
        validate_opinion(opinion, away_team=AWAY, home_team=HOME, schedule_input=payload)


class ResponseTests(unittest.TestCase):
    def setUp(self) -> None:
        self.payload = build_rating_input(_game(), _finals())
        self.response = rating_response(self.payload)

    def test_response_normalizes_and_validates(self) -> None:
        response = json.loads(canonical_json(self.response))
        opinion = normalize_rating_opinion(response, self.payload)
        validate_opinion(opinion, away_team=AWAY, home_team=HOME, schedule_input=self.payload)
        estimate = self.payload["estimate"]
        self.assertEqual(opinion["pick_market"], "straight_up")
        self.assertEqual(opinion["pick_side"], estimate["predicted_winner"])
        self.assertEqual(opinion["home_win_probability"], estimate["home_win_probability"])
        self.assertEqual(opinion["expected_home_margin"], estimate["expected_home_margin"])
        self.assertLessEqual(len(opinion["thesis"]), 500)
        self.assertTrue(opinion["thesis"].startswith("Elo ratings:"))
        # The judge reads the thesis and the factors: no expert name, and
        # every factor fits the aggregator's 200-character cap uncut.
        self.assertNotIn("Rating Expert", json.dumps(opinion))
        for field in ("supporting_factors", "counterarguments", "no_signal_factors", "discarded_considerations"):
            for item in opinion[field]:
                self.assertLessEqual(len(item), 200, item)
        for section in ("Ratings", "Estimate", "Model", "Why", "Why it may be wrong", "No signal", "Discarded considerations", "Conclusion"):
            self.assertIn(section, opinion["full_opinion"])
        self.assertIn("1-0-1", opinion["full_opinion"])
        self.assertIn("3 finals", opinion["thesis"])
        self.assertTrue(any("league scoring rate" in item for item in opinion["no_signal_factors"]))
        self.assertTrue(any(str(self.payload["prior"]["fit"]["check_elo_brier"]) [:5] in item for item in opinion["supporting_factors"]))

    def test_rejections(self) -> None:
        base = json.loads(canonical_json(self.response))
        cases = [
            {"home_win_probability": 0.7},
            {"expected_home_margin": -base["expected_home_margin"]},
            {"predicted_winner": AWAY},
            {"predicted_home_score": base["predicted_away_score"]},
            {"confidence_stars": 6},
            {"side": {"selection": HOME}},
            {"supporting_factors": []},
            {"supporting_factors": [""]},
            {"thesis": " "},
        ]
        for overrides in cases:
            with self.subTest(overrides=overrides):
                with self.assertRaises(ValueError):
                    normalize_rating_opinion({**base, **overrides}, self.payload)
        missing = dict(base)
        del missing["full_opinion"]
        with self.assertRaises(ValueError):
            normalize_rating_opinion(missing, self.payload)
        with self.assertRaises(ValueError):
            normalize_rating_opinion(base, {**self.payload, "input_profile": "schedule_only"})


class RegistryTests(unittest.TestCase):
    def test_registry_entry_and_lens(self) -> None:
        expert = load_expert(RATING_EXPERT_ID)
        self.assertEqual(expert["mode"], RATING_MODE)
        self.assertEqual(expert["input_profile"], RATING_PROFILE)
        self.assertEqual(expert["default_model"], DETERMINISTIC_MODEL)
        self.assertEqual(expert["allowed_backends"], [DETERMINISTIC_BACKEND])
        self.assertEqual(expert["output_schema_version"], 9)
        self.assertEqual(expert["markets"], ["side", "total"])
        self.assertTrue(expert["enabled"])
        self.assertEqual(expert["prompt_path"], "moe/prompts/rating_elo/v1.md")
        self.assertIn("Rating Expert (Elo) v1", expert["prompt_text"])
        self.assertIn("Elo", VOICE_LENSES[RATING_EXPERT_ID])
        self.assertIn(RATING_EXPERT_ID, committee_experts(load_registry()))


class GenerationTests(unittest.IsolatedAsyncioTestCase):
    async def _row(self, store: MemoryStore | None = None) -> dict:
        return await generate_opinion(
            expert_id=RATING_EXPERT_ID,
            game=_game(),
            history=[],
            current_season_results=_finals(),
            store=store or MemoryStore(),
            generation_backend=DETERMINISTIC_BACKEND,
        )

    async def test_rating_end_to_end(self) -> None:
        store = MemoryStore()
        row = await self._row(store)
        self.assertEqual(store.rows, [row])
        self.assertEqual(row["generation_status"], "valid")
        self.assertEqual(row["review_status"], "pending")
        self.assertEqual(row["expert_mode"], RATING_MODE)
        self.assertEqual(row["input_profile"], RATING_PROFILE)
        self.assertEqual(row["model"], DETERMINISTIC_MODEL)
        self.assertEqual(row["generation_backend"], DETERMINISTIC_BACKEND)
        self.assertEqual(row["generation_effort"], "")
        self.assertEqual(row["generation_max_tokens"], 0)
        self.assertEqual(row["output_schema_version"], 9)
        self.assertEqual(row["pick_market"], "straight_up")
        self.assertEqual(row["pick_side"], row["predicted_winner"])
        self.assertEqual(set(row), set(OPINION_HEADERS))
        self.assertEqual(row["output_sha256"], opinion_output_sha256(row))
        persisted = json.loads(row["input_json"])
        self.assertEqual(persisted, build_rating_input(_game(), _finals()))
        self.assertEqual(row["input_sha256"], sha256_text(canonical_json(persisted)))
        self.assertEqual(json.loads(row["raw_response"])["home_win_probability"], row["home_win_probability"])
        self.assertTrue(row["thesis"].startswith("Elo ratings:"))
        self.assertNotIn("Rating Expert", row["thesis"])
        self.assertEqual(json.loads(row["supporting_factors_json"])[0][:7], "Ratings")
        self.assertEqual(row["side_pick_json"], "")

    async def test_backend_guards(self) -> None:
        for kwargs in (
            {"generation_backend": "agent_runtime"},
            {"generation_backend": "anthropic_api"},
            {"generation_backend": DETERMINISTIC_BACKEND, "generation_effort": "max"},
            {"generation_backend": DETERMINISTIC_BACKEND, "model": "claude-opus-4-8"},
        ):
            with self.subTest(kwargs=kwargs):
                store = MemoryStore()
                with self.assertRaises(ValueError):
                    await generate_opinion(
                        expert_id=RATING_EXPERT_ID, game=_game(), history=[], current_season_results=[], store=store, **kwargs
                    )
                self.assertEqual(store.rows, [])
        # The deterministic backend stays closed to agent experts.
        with self.assertRaises(ValueError):
            await generate_opinion(
                expert_id="schedule", game=_game(), history=[], store=MemoryStore(), generation_backend=DETERMINISTIC_BACKEND
            )

    async def test_rating_row_is_a_default_model_voice(self) -> None:
        row = await self._row()
        row["review_status"] = "approved"
        row["approved_output_sha256"] = row["output_sha256"]
        registry = load_registry()
        policy = aggregator_policy(registry)
        rows = _committee() + [row]
        selected = {item[0]: item for item in select_voice_rows(rows, event_id=EVENT_ID, registry=registry, policy=policy)}
        self.assertIn(RATING_EXPERT_ID, selected)
        self.assertEqual(selected[RATING_EXPERT_ID][3], "default_model")
        self.assertEqual(selected[RATING_EXPERT_ID][2]["opinion_id"], row["opinion_id"])
        payload = build_aggregator_input(_game(), approved_opinions=rows, finals=[], snapshots=[], registry=registry, policy=policy)
        voices = {voice["voice_id"]: voice for voice in payload["voices"]}
        self.assertEqual(payload["feature_block"]["n_voices"], 5)
        self.assertEqual(voices[RATING_EXPERT_ID]["lens"], VOICE_LENSES[RATING_EXPERT_ID])
        self.assertEqual(voices[RATING_EXPERT_ID]["home_win_probability"], row["home_win_probability"])
        request = build_judge_request(payload)
        self.assertEqual(len(request["voices"]), 5)
        rendered = json.dumps(request)
        self.assertNotIn(RATING_EXPERT_ID, rendered)
        self.assertNotIn("Rating Expert", rendered)
        self.assertIn("Elo", rendered)

    async def test_bot_views_render_a_rating_row(self) -> None:
        row = await self._row()
        row["review_status"] = "approved"
        row["approved_output_sha256"] = row["output_sha256"]
        text, buttons = opinion_detail(row, page=0, event_id=EVENT_ID)
        self.assertIn("Rating Expert (Elo)", text)
        self.assertIn("<b>Pick:</b>", text)
        self.assertIn("deterministic", text)
        self.assertTrue(buttons)
        summary_text, summary_buttons = opinion_summary(_game(), [row], page=0, event_id=EVENT_ID)
        self.assertIn("Rating Expert (Elo)", summary_text)
        self.assertIn("deterministic", summary_text)
        self.assertTrue(summary_buttons)


class WeekGenerationTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.games = [
            _game(event_id="wk3-b", kickoff="2026-09-27T20:25:00+00:00", week=3, away="Dallas Cowboys", home="Denver Broncos"),
            _game(event_id="wk3-a", kickoff="2026-09-27T17:00:00+00:00", week=3),
            _game(event_id="wk4", kickoff="2026-10-04T17:00:00+00:00", week=4),
            {**_game(event_id="wk3-final", kickoff="2026-09-25T00:15:00+00:00", week=3), "status": "final"},
        ]

    def test_week_games(self) -> None:
        self.assertEqual([game["event_id"] for game in week_games(self.games, season=2026, week=3)], ["wk3-a", "wk3-b"])
        self.assertEqual(week_games(self.games, season=2025, week=3), [])

    async def test_generate_week_persists_once_per_input(self) -> None:
        store = MemoryStore()
        with contextlib.redirect_stdout(io.StringIO()) as output:
            summary = await generate_week(games=self.games, opinion_rows=[], finals=[], store=store, season=2026, week=3)
        self.assertEqual([item["event_id"] for item in summary["persisted"]], ["wk3-a", "wk3-b"])
        self.assertEqual(summary["up_to_date"], [])
        self.assertEqual(summary["failed"], [])
        self.assertEqual([row["event_id"] for row in store.rows], ["wk3-a", "wk3-b"])
        self.assertTrue(all(row["review_status"] == "pending" and row["expert_id"] == RATING_EXPERT_ID for row in store.rows))
        self.assertIn("persisted", output.getvalue())
        # Same inputs again: nothing new.
        with contextlib.redirect_stdout(io.StringIO()):
            again = await generate_week(games=self.games, opinion_rows=store.rows, finals=[], store=store, season=2026, week=3)
        self.assertEqual(again["persisted"], [])
        self.assertEqual([item["event_id"] for item in again["up_to_date"]], ["wk3-a", "wk3-b"])
        self.assertEqual(len(store.rows), 2)
        # A new final changes the input, so a fresher row lands; the old one stays.
        finals = [_final("f1", "2026-09-20T17:00:00+00:00", HOME, "Denver Broncos", 24, 20, week=2)]
        with contextlib.redirect_stdout(io.StringIO()):
            fresher = await generate_week(games=self.games, opinion_rows=store.rows, finals=finals, store=store, season=2026, week=3)
        self.assertEqual([item["event_id"] for item in fresher["persisted"]], ["wk3-a", "wk3-b"])
        self.assertEqual(len(store.rows), 4)
        # Dry run persists nothing.
        before = len(store.rows)
        with contextlib.redirect_stdout(io.StringIO()) as output:
            dry = await generate_week(games=self.games, opinion_rows=[], finals=[], store=store, season=2026, week=3, dry_run=True)
        self.assertEqual(len(store.rows), before)
        self.assertTrue(all(item["dry_run"] for item in dry["persisted"]))
        self.assertIn("dry run:", output.getvalue())
        # A rejected row on the same input does not count as up to date.
        rejected = dict(store.rows[0], review_status="rejected")
        self.assertIsNone(existing_row([rejected], event_id="wk3-a", input_sha256=rejected["input_sha256"]))
        self.assertIsNotNone(existing_row(store.rows, event_id="wk3-a", input_sha256=store.rows[0]["input_sha256"]))


class BulkReviewTests(unittest.TestCase):
    def _rows(self) -> list[dict]:
        def rating(event_id: str, kickoff: str, *, week: int = 3, generated_at: str = "2026-09-22T12:00:00+00:00", **overrides) -> dict:
            row = _opinion(RATING_EXPERT_ID, model=DETERMINISTIC_MODEL, probability=0.6, margin=3, away_score=21, home_score=24, stars=2, event_id=event_id, kickoff=kickoff, generated_at=generated_at, review_status="pending")
            row["week"] = week
            row["input_sha256"] = sha256_text(event_id)
            row.update(overrides)
            return row

        return [
            rating("later", "2026-09-27T20:25:00+00:00"),
            rating("early", "2026-09-27T17:00:00+00:00"),
            rating("done", "2026-09-27T17:00:00+00:00", review_status="approved"),
            rating("broken", "2026-09-27T17:00:00+00:00", generation_status="invalid", review_status="not_applicable"),
            rating("next-week", "2026-10-04T17:00:00+00:00", week=4),
            rating("last-season", "2025-09-28T17:00:00+00:00", season=2025),
            _opinion("schedule", model="claude-opus-4-8", probability=0.6, margin=3, away_score=20, home_score=23, event_id="other-expert", review_status="pending"),
        ]

    def test_week_rows_filter_and_order(self) -> None:
        rows = self._rows()
        # Kickoff order, so last season's week 3 comes first without --season.
        selected = week_rows(rows, expert_id=RATING_EXPERT_ID, week=3)
        self.assertEqual([row["event_id"] for row in selected], ["last-season", "early", "later"])
        self.assertEqual([row["event_id"] for row in week_rows(rows, expert_id=RATING_EXPERT_ID, week=3, season=2026)], ["early", "later"])
        self.assertEqual([row["event_id"] for row in week_rows(rows, expert_id=RATING_EXPERT_ID, week=3, season=2026, review_status="approved")], ["done"])
        self.assertEqual(week_rows(rows, expert_id=RATING_EXPERT_ID, week=5), [])

    def test_table_and_approval(self) -> None:
        store = MemoryStore()
        for row in self._rows():
            store.append(row)
        selected = week_rows(store.list(), expert_id=RATING_EXPERT_ID, week=3, season=2026)
        lines = format_week_table(selected)
        self.assertEqual(len(lines), 3)
        self.assertTrue(lines[0].startswith("game"))
        self.assertIn(f"{AWAY} @ {HOME}", lines[1])
        self.assertIn("Sep 27", lines[1])
        self.assertIn("0.600", lines[1])
        self.assertIn("+3.0", lines[1])
        self.assertIn("★★", lines[1])
        self.assertIn(selected[0]["opinion_id"], lines[1])
        self.assertIn(selected[0]["input_sha256"][:12], lines[1])
        self.assertEqual(format_week_table([]), ["no rows"])
        # Listing approves nothing; approve_rows approves exactly the list, in order.
        self.assertEqual(store.reviews, [])
        approved = approve_rows(store, selected, reviewed_by="tester", note="week 3 looks right")
        self.assertEqual(approved, [row["opinion_id"] for row in selected])
        self.assertEqual([item[0] for item in store.reviews], approved)
        self.assertTrue(all(item[1:] == ("approved", "tester", "week 3 looks right") for item in store.reviews))
        self.assertEqual({row["review_status"] for row in store.rows if row["event_id"] in {"early", "later"}}, {"approved"})
        self.assertEqual(week_rows(store.list(), expert_id=RATING_EXPERT_ID, week=3, season=2026), [])

    def test_argument_validation(self) -> None:
        args = parse_args(["--expert", RATING_EXPERT_ID, "--week", "3", "--reviewed-by", "me"])
        self.assertFalse(args.approve)
        self.assertIsNone(args.season)
        args = parse_args(["--expert", RATING_EXPERT_ID, "--week", "3", "--season", "2026", "--reviewed-by", "me", "--approve"])
        self.assertTrue(args.approve)
        single = parse_args(["--opinion-id", "abc", "--status", "rejected", "--reviewed-by", "me"])
        self.assertEqual((single.opinion_id, single.status), ("abc", "rejected"))
        for argv in (
            ["--expert", RATING_EXPERT_ID, "--reviewed-by", "me"],
            ["--expert", RATING_EXPERT_ID, "--week", "3", "--opinion-id", "abc", "--reviewed-by", "me"],
            ["--opinion-id", "abc", "--reviewed-by", "me"],
            ["--opinion-id", "abc", "--status", "approved", "--reviewed-by", "me", "--approve"],
            ["--reviewed-by", "me"],
        ):
            with self.subTest(argv=argv):
                with contextlib.redirect_stderr(io.StringIO()):
                    with self.assertRaises(SystemExit):
                        parse_args(argv)

    def test_cli_refuses_bulk_mode_without_a_week(self) -> None:
        result = subprocess.run(
            [sys.executable, str(REVIEW_SCRIPT), "--expert", RATING_EXPERT_ID, "--reviewed-by", "me"],
            cwd=ROOT,
            capture_output=True,
            check=False,
            text=True,
        )
        self.assertEqual(result.returncode, 2)
        self.assertIn("--expert requires --week", result.stderr)


if __name__ == "__main__":
    unittest.main()
