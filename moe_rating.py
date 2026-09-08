"""Elo rating voice for the NFL mixture of experts.

``rating_elo`` (mode ``model``, input profile ``rating``) is a deterministic
committee input: one Elo rating per team built from every regular-season
final since 1999 in ``data/nfl_lines_history.csv``, with home advantage and a
margin-of-victory multiplier, regressed toward the mean between seasons and
updated through the current season's finals before kickoff. It is a voice
like any other registered expert -- approved by a human, pooled by the God
Expert aggregator -- and never a third aggregator arm. No model reads
anything here.

The fitted parameters and the end-of-season ratings live in
``moe/priors/nfl_elo_v1.json``, written by ``scripts/fit_nfl_elo.py`` and
read offline at generation time the way the WNBA prior is; the prior's path,
schema version and hash ride in every persisted input.

Arithmetic (the FiveThirtyEight NFL Elo form)::

    adjusted gap        g = home rating - away rating + hfa
    home win probability p = 1 / (1 + 10 ** (-g / 400))
    expected home margin  g / points_per_elo
    rating update       K * m * (S - p) to the home team, the mirror to the
                        away team; S is 1 / 0.5 / 0 for a home win / tie /
                        loss
    margin multiplier   m = ln(|margin| + 1) * 2.2 / (0.001 * winner_gap + 2.2)
                        (winner_gap is the adjusted gap from the winner's
                        side, so a favorite's blowout moves ratings less; a
                        tie has margin 0 and multiplier 0, leaving ratings
                        unchanged)
    season regression   rating -> base + (rating - base) * (1 - fraction)

Predicted scores split the league scoring rate (the mean total of the last
completed season) by the expected margin, never tied; stars come from the
size of the expected margin (``STAR_MARGINS``). The projected total carries
no game-specific signal and the opinion says so.
"""

from __future__ import annotations

import csv
import hashlib
import json
import math
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable

from moe_god import _scores_from_estimate, canonical_json, fair_pair, sha256_text

ROOT = Path(__file__).resolve().parent
LINES_CSV_PATH = ROOT / "data" / "nfl_lines_history.csv"
ELO_PRIOR_PATH = ROOT / "moe" / "priors" / "nfl_elo_v1.json"

RATING_MODE = "model"
RATING_PROFILE = "rating"
RATING_EXPERT_ID = "rating_elo"
PRIOR_SCHEMA_VERSION = 1
BASE_RATING = 1500.0
# |expected margin| in points at which the second .. fifth star lights up.
STAR_MARGINS = (3.0, 7.0, 10.0, 14.0)
PARAMETER_KEYS = ("k", "hfa", "regression", "points_per_elo")
DEFAULT_PARAMETERS: dict[str, float] = {
    "k": 20.0,
    "hfa": 48.0,
    "regression": 1.0 / 3.0,
    "points_per_elo": 25.0,
}
RESPONSE_FIELDS = {
    "predicted_winner",
    "predicted_away_score",
    "predicted_home_score",
    "home_win_probability",
    "expected_home_margin",
    "confidence_stars",
    "thesis",
    "supporting_factors",
    "counterarguments",
    "no_signal_factors",
    "discarded_considerations",
    "full_opinion",
}


def _parse_time(value: Any) -> datetime:
    return datetime.fromisoformat(str(value).replace("Z", "+00:00"))


# --------------------------------------------------------------------------
# Elo arithmetic


def win_probability(adjusted_gap: float) -> float:
    """P(home wins) from the home-minus-away gap with home field included."""
    return 1.0 / (1.0 + 10.0 ** (-float(adjusted_gap) / 400.0))


def expected_margin(adjusted_gap: float, points_per_elo: float) -> float:
    return float(adjusted_gap) / float(points_per_elo)


def margin_multiplier(margin: float, winner_gap: float) -> float:
    """ln(|margin| + 1) damped when the winner was already the stronger side.

    ``winner_gap`` is the adjusted rating gap seen from the winner (positive
    when the favorite won). A tie has margin 0 and multiplier 0.
    """
    return (
        math.log(abs(float(margin)) + 1.0)
        * 2.2
        / (0.001 * float(winner_gap) + 2.2)
    )


def elo_update(
    home_rating: float,
    away_rating: float,
    home_score: int,
    away_score: int,
    params: dict[str, float],
) -> tuple[float, float, float]:
    """One game: returns (new home rating, new away rating, pregame p_home)."""
    adjusted_gap = float(home_rating) - float(away_rating) + float(params["hfa"])
    p_home = win_probability(adjusted_gap)
    margin = int(home_score) - int(away_score)
    if margin > 0:
        outcome, winner_gap = 1.0, adjusted_gap
    elif margin < 0:
        outcome, winner_gap = 0.0, -adjusted_gap
    else:
        outcome, winner_gap = 0.5, abs(adjusted_gap)
    delta = float(params["k"]) * margin_multiplier(margin, winner_gap) * (
        outcome - p_home
    )
    return float(home_rating) + delta, float(away_rating) - delta, p_home


def regress_ratings(
    ratings: dict[str, float], fraction: float, base: float = BASE_RATING
) -> dict[str, float]:
    """Pull every rating ``fraction`` of the way back to ``base``."""
    return {
        team: float(base) + (float(rating) - float(base)) * (1.0 - float(fraction))
        for team, rating in ratings.items()
    }


def outcome_value(home_margin: int | float) -> float:
    return 1.0 if home_margin > 0 else 0.0 if home_margin < 0 else 0.5


def stars_for_margin(margin: float) -> int:
    return 1 + sum(1 for threshold in STAR_MARGINS if abs(float(margin)) >= threshold)


# --------------------------------------------------------------------------
# Historical replay (data/nfl_lines_history.csv rows)


def read_lines_csv(path: Path = LINES_CSV_PATH) -> list[dict[str, str]]:
    with open(path, encoding="utf-8", newline="") as handle:
        return list(csv.DictReader(handle))


def _csv_sort_key(row: dict[str, Any]) -> tuple[int, int, str, str, str]:
    return (
        int(row["season"]),
        int(row["week"]),
        str(row.get("gameday") or ""),
        str(row.get("gametime") or ""),
        str(row["home_team"]),
    )


def replay_games(
    rows: Iterable[dict[str, Any]],
    params: dict[str, float],
    *,
    ratings: dict[str, float] | None = None,
    base: float = BASE_RATING,
    collect_from: int | None = None,
) -> dict[str, Any]:
    """Replay every played game in season, week, day order.

    Ratings regress toward ``base`` at each season boundary inside the
    replay (never before the first season replayed). Unplayed rows (empty
    scores) are skipped. ``predictions`` holds one pregame estimate per game
    from ``collect_from`` onward (all seasons when None).
    """
    current = dict(ratings or {})
    predictions: list[dict[str, Any]] = []
    season_seen: int | None = None
    k_hfa = float(params["hfa"])
    ppe = float(params["points_per_elo"])
    fraction = float(params["regression"])
    for row in sorted(rows, key=_csv_sort_key):
        if row.get("home_score") in ("", None) or row.get("away_score") in ("", None):
            continue
        season = int(row["season"])
        if season != season_seen:
            if season_seen is not None:
                current = regress_ratings(current, fraction, base)
            season_seen = season
        home = str(row["home_team"])
        away = str(row["away_team"])
        home_rating = current.get(home, base)
        away_rating = current.get(away, base)
        home_score = int(row["home_score"])
        away_score = int(row["away_score"])
        new_home, new_away, p_home = elo_update(
            home_rating, away_rating, home_score, away_score, params
        )
        if collect_from is None or season >= collect_from:
            adjusted_gap = home_rating - away_rating + k_hfa
            predictions.append(
                {
                    "season": season,
                    "week": int(row["week"]),
                    "away_team": away,
                    "home_team": home,
                    # Pregame ratings, unrounded; the backtest feeds them to
                    # rating_estimate the way build_rating_input does.
                    "home_rating": home_rating,
                    "away_rating": away_rating,
                    "adjusted_gap": adjusted_gap,
                    "home_win_probability": p_home,
                    "expected_home_margin": adjusted_gap / ppe,
                    "home_margin": home_score - away_score,
                    "outcome": outcome_value(home_score - away_score),
                    "home_spread": _number(row.get("home_spread")),
                    "home_moneyline": _number(row.get("home_moneyline")),
                    "away_moneyline": _number(row.get("away_moneyline")),
                }
            )
        current[home] = new_home
        current[away] = new_away
    return {"ratings": current, "predictions": predictions, "last_season": season_seen}


def _number(value: Any) -> float | None:
    if value in (None, ""):
        return None
    return float(value)


def brier_score(
    predictions: Iterable[dict[str, Any]], seasons: Iterable[int]
) -> tuple[float | None, int]:
    """Mean squared error of ``home_win_probability`` against the outcome
    (a tie counts 0.5) over the given seasons; (None, 0) without games."""
    wanted = set(int(season) for season in seasons)
    total = 0.0
    count = 0
    for item in predictions:
        if int(item["season"]) not in wanted:
            continue
        total += (float(item["home_win_probability"]) - float(item["outcome"])) ** 2
        count += 1
    return (None, 0) if not count else (total / count, count)


def closing_moneyline_brier(
    predictions: Iterable[dict[str, Any]], seasons: Iterable[int]
) -> tuple[float | None, int]:
    """Brier of the de-vigged closing moneyline over games that carry one."""
    wanted = set(int(season) for season in seasons)
    total = 0.0
    count = 0
    for item in predictions:
        if int(item["season"]) not in wanted:
            continue
        home_ml, away_ml = item.get("home_moneyline"), item.get("away_moneyline")
        if home_ml is None or away_ml is None:
            continue
        fair_home = fair_pair(home_ml, away_ml)[0]
        total += (fair_home - float(item["outcome"])) ** 2
        count += 1
    return (None, 0) if not count else (total / count, count)


def margin_rmse(
    predictions: Iterable[dict[str, Any]],
    seasons: Iterable[int],
    *,
    source: str,
) -> tuple[float | None, int]:
    """Root mean squared error of the expected margin (``elo``) or the
    closing spread's implied margin (``closing_spread``)."""
    wanted = set(int(season) for season in seasons)
    total = 0.0
    count = 0
    for item in predictions:
        if int(item["season"]) not in wanted:
            continue
        if source == "elo":
            estimate = float(item["expected_home_margin"])
        else:
            spread = item.get("home_spread")
            if spread is None:
                continue
            estimate = -float(spread)
        total += (estimate - float(item["home_margin"])) ** 2
        count += 1
    return (None, 0) if not count else (math.sqrt(total / count), count)


def fit_points_per_elo(
    predictions: Iterable[dict[str, Any]], seasons: Iterable[int]
) -> float | None:
    """Least-squares slope of the actual margin on the adjusted gap,
    expressed as rating points per point of margin."""
    wanted = set(int(season) for season in seasons)
    gap_sq = 0.0
    gap_margin = 0.0
    for item in predictions:
        if int(item["season"]) not in wanted:
            continue
        gap = float(item["adjusted_gap"])
        gap_sq += gap * gap
        gap_margin += gap * float(item["home_margin"])
    if gap_sq <= 0 or gap_margin <= 0:
        return None
    return gap_sq / gap_margin


def league_scoring_rate(rows: Iterable[dict[str, Any]], season: int) -> dict[str, Any]:
    totals = [
        int(row["home_score"]) + int(row["away_score"])
        for row in rows
        if int(row["season"]) == int(season)
        and row.get("home_score") not in ("", None)
        and row.get("away_score") not in ("", None)
    ]
    if not totals:
        raise ValueError(f"No played games in season {season}")
    return {
        "season": int(season),
        "mean_total": round(sum(totals) / len(totals), 2),
        "games": len(totals),
    }


def fit_parameters(
    rows: list[dict[str, Any]],
    *,
    fit_seasons: Iterable[int],
    k_grid: Iterable[float],
    hfa_grid: Iterable[float],
    regression_grid: Iterable[float],
    base: float = BASE_RATING,
) -> dict[str, Any]:
    """Grid search minimizing the fit seasons' Brier; ties keep the first
    grid point in (regression, k, hfa) order. Every point replays the whole
    file, so the warm-up seasons see the candidate parameters too."""
    fit_seasons = sorted(int(season) for season in fit_seasons)
    first_fit = min(fit_seasons)
    best: dict[str, Any] | None = None
    evaluated = 0
    for regression in regression_grid:
        for k in k_grid:
            for hfa in hfa_grid:
                params = {
                    "k": float(k),
                    "hfa": float(hfa),
                    "regression": float(regression),
                    "points_per_elo": DEFAULT_PARAMETERS["points_per_elo"],
                }
                replay = replay_games(rows, params, base=base, collect_from=first_fit)
                score, games = brier_score(replay["predictions"], fit_seasons)
                evaluated += 1
                if score is None:
                    continue
                if best is None or score < best["brier"] - 1e-15:
                    best = {"params": params, "brier": score, "games": games}
    if best is None:
        raise ValueError("No fit-season games to score")
    best["evaluated"] = evaluated
    return best


# --------------------------------------------------------------------------
# The prior file


def load_prior(path: Path = ELO_PRIOR_PATH) -> tuple[dict[str, Any], str]:
    """The committed prior and the SHA-256 of its bytes."""
    raw = Path(path).read_bytes()
    prior = json.loads(raw.decode("utf-8"))
    if not isinstance(prior, dict) or prior.get("schema_version") != PRIOR_SCHEMA_VERSION:
        raise ValueError(f"Unsupported Elo prior schema in {path}")
    for key in ("parameters", "ratings", "through_season", "league_scoring_rate", "base_rating"):
        if key not in prior:
            raise ValueError(f"Elo prior is missing {key}")
    missing = [key for key in PARAMETER_KEYS if key not in prior["parameters"]]
    if missing:
        raise ValueError(f"Elo prior parameters are missing {missing}")
    return prior, hashlib.sha256(raw).hexdigest()


def preseason_ratings(prior: dict[str, Any], season: int) -> dict[str, float]:
    """The prior's end-of-season ratings regressed for ``season``.

    The prior covers one completed season; the voice rates the next one.
    Rating an older season would double count its finals, and skipping a
    season would ignore one entirely, so both refuse and ask for a refit.
    """
    through = int(prior["through_season"])
    if int(season) <= through:
        raise ValueError(
            f"The Elo prior already covers season {through}; it cannot rate "
            f"season {season}"
        )
    if int(season) != through + 1:
        raise ValueError(
            f"The Elo prior ends at season {through}; refit it "
            "(scripts/fit_nfl_elo.py) before rating season "
            f"{season}"
        )
    return regress_ratings(
        {team: float(value) for team, value in prior["ratings"].items()},
        float(prior["parameters"]["regression"]),
        float(prior["base_rating"]),
    )


def finals_before_kickoff(
    current_season_results: Iterable[dict[str, Any]],
    *,
    season: int,
    kickoff: datetime,
) -> list[dict[str, Any]]:
    """This season's finals that kicked off strictly before ``kickoff``,
    in kickoff order. Rows are shaped like ``nfl_game_history``."""
    finals = []
    for row in current_season_results:
        if str(row.get("status") or "final") != "final":
            continue
        if row.get("home_score") in (None, "") or row.get("away_score") in (None, ""):
            continue
        if str(row.get("season") or "").strip() and int(row["season"]) != int(season):
            continue
        if _parse_time(row["kickoff_utc"]) >= kickoff:
            continue
        finals.append(row)
    finals.sort(
        key=lambda row: (
            _parse_time(row["kickoff_utc"]),
            str(row.get("week") or ""),
            str(row.get("event_id") or ""),
            str(row.get("home_team") or ""),
        )
    )
    return finals


def apply_finals(
    ratings: dict[str, float],
    finals: Iterable[dict[str, Any]],
    params: dict[str, float],
    *,
    base: float = BASE_RATING,
) -> tuple[dict[str, float], dict[str, dict[str, int]]]:
    """Update ``ratings`` with already-ordered finals; returns the new
    ratings and each team's games / wins / losses / ties this season."""
    current = dict(ratings)
    records: dict[str, dict[str, int]] = {}

    def record(team: str) -> dict[str, int]:
        return records.setdefault(team, {"games": 0, "wins": 0, "losses": 0, "ties": 0})

    for row in finals:
        home = str(row["home_team"])
        away = str(row["away_team"])
        home_score = int(row["home_score"])
        away_score = int(row["away_score"])
        new_home, new_away, _p_home = elo_update(
            current.get(home, base), current.get(away, base), home_score, away_score, params
        )
        current[home] = new_home
        current[away] = new_away
        for team, own, other in ((home, home_score, away_score), (away, away_score, home_score)):
            tally = record(team)
            tally["games"] += 1
            if own > other:
                tally["wins"] += 1
            elif own < other:
                tally["losses"] += 1
            else:
                tally["ties"] += 1
    return current, records


def _record_text(tally: dict[str, int]) -> str:
    text = f"{tally['wins']}-{tally['losses']}"
    if tally["ties"]:
        text += f"-{tally['ties']}"
    return text


# --------------------------------------------------------------------------
# The input and the deterministic response


def build_rating_input(
    game: dict[str, Any],
    current_season_results: Iterable[dict[str, Any]],
    *,
    prior_path: Path = ELO_PRIOR_PATH,
) -> dict[str, Any]:
    """The rating voice's input: prior, this season's replay, the estimate.

    Deterministic for fixed inputs; the estimate is computed from the
    ratings as they are written (two decimals), so the numbers in the input
    reproduce it exactly.
    """
    prior, digest = load_prior(prior_path)
    season = int(game["season"])
    kickoff = _parse_time(game["commence_time_utc"])
    away = str(game["away_team"])
    home = str(game["home_team"])
    params = {key: float(prior["parameters"][key]) for key in PARAMETER_KEYS}
    base = float(prior["base_rating"])
    preseason = preseason_ratings(prior, season)
    for team in (away, home):
        if team not in preseason:
            raise ValueError(f"The Elo prior has no rating for {team}")
    finals = finals_before_kickoff(
        current_season_results, season=season, kickoff=kickoff
    )
    current, records = apply_finals(preseason, finals, params, base=base)
    estimate = rating_estimate(
        away_team=away,
        home_team=home,
        away_rating=current[away],
        home_rating=current[home],
        params=params,
        total=float(prior["league_scoring_rate"]["mean_total"]),
    )
    week = game.get("week")
    empty = {"games": 0, "wins": 0, "losses": 0, "ties": 0}

    def side(team: str) -> dict[str, Any]:
        tally = records.get(team, empty)
        return {
            "team": team,
            "preseason_rating": round(preseason[team], 2),
            "rating": round(current[team], 2),
            "games_played": tally["games"],
            "record": _record_text(tally),
            "wins": tally["wins"],
            "losses": tally["losses"],
            "ties": tally["ties"],
        }

    return {
        "input_profile": RATING_PROFILE,
        "game": {
            "event_id": str(game["event_id"]),
            "season": season,
            "week": int(week) if str(week or "").strip() else None,
            "away_team": away,
            "home_team": home,
            "commence_time_utc": kickoff.isoformat(),
            "commence_time_et": str(game.get("commence_time_et") or ""),
        },
        "prior": {
            "path": _prior_path_text(prior_path),
            "sha256": digest,
            "schema_version": int(prior["schema_version"]),
            "version": str(prior.get("version") or ""),
            "through_season": int(prior["through_season"]),
            "base_rating": base,
            "parameters": params,
            "league_scoring_rate": dict(prior["league_scoring_rate"]),
            "fit": dict(prior.get("fit") or {}),
            "source": dict(prior.get("source") or {}),
        },
        "season": {
            "season": season,
            "preseason_regression": params["regression"],
            "finals_applied": len(finals),
            "finals_through_utc": (
                _parse_time(finals[-1]["kickoff_utc"]).isoformat() if finals else None
            ),
        },
        "ratings": {"away": side(away), "home": side(home)},
        "estimate": estimate,
    }


def rating_estimate(
    *,
    away_team: str,
    home_team: str,
    away_rating: float,
    home_rating: float,
    params: dict[str, float],
    total: float,
) -> dict[str, Any]:
    """The voice's estimate from two pregame ratings, as the input records it.

    Ratings are used as written to two decimals, so the numbers in the input
    reproduce the estimate exactly; an adjusted gap of exactly 0 leans home
    (0.5001) and a margin of 0, or one whose sign disagrees with the
    probability, leans 0.01 the probability's way. ``total`` is the league
    scoring rate (the voice carries no total signal). Shared by
    :func:`build_rating_input` and the backtest (``moe_backtest``), so both
    run one arithmetic.
    """
    away_rating = round(float(away_rating), 2)
    home_rating = round(float(home_rating), 2)
    gap = round(home_rating - away_rating, 2)
    adjusted_gap = round(gap + params["hfa"], 2)
    probability = round(win_probability(adjusted_gap), 4)
    tie_break = None
    if probability == 0.5:
        # Only an exactly offsetting gap lands here; the home side breaks it.
        probability = 0.5001
        tie_break = "adjusted gap of exactly 0; the home side breaks the tie"
    margin = round(expected_margin(adjusted_gap, params["points_per_elo"]), 2)
    if margin == 0 or (margin > 0) != (probability > 0.5):
        margin = 0.01 if probability > 0.5 else -0.01
    total = float(total)
    away_score, home_score = _scores_from_estimate(probability, margin, total)
    return {
        "rating_gap": gap,
        "home_field_advantage": params["hfa"],
        "adjusted_gap": adjusted_gap,
        "home_win_probability": probability,
        "expected_home_margin": margin,
        "projected_total": total,
        "predicted_away_score": int(away_score),
        "predicted_home_score": int(home_score),
        "confidence_stars": stars_for_margin(margin),
        "predicted_winner": home_team if probability > 0.5 else away_team,
        "tie_break": tie_break,
    }


def _prior_path_text(prior_path: Path) -> str:
    """Repo-relative when the prior lives in the repo, else absolute."""
    resolved = Path(prior_path).resolve()
    try:
        return resolved.relative_to(ROOT).as_posix()
    except ValueError:
        return resolved.as_posix()


def _fmt(value: Any, spec: str) -> str:
    return format(float(value), spec)


def _count(value: int, noun: str) -> str:
    return f"{int(value)} {noun}{'' if int(value) == 1 else 's'}"


def rating_response(input_payload: dict[str, Any]) -> dict[str, Any]:
    """The opinion JSON, written from the input's own numbers."""
    game = input_payload["game"]
    prior = input_payload["prior"]
    params = prior["parameters"]
    season = input_payload["season"]
    ratings = input_payload["ratings"]
    estimate = input_payload["estimate"]
    away, home = str(game["away_team"]), str(game["home_team"])
    away_side, home_side = ratings["away"], ratings["home"]
    probability = float(estimate["home_win_probability"])
    margin = float(estimate["expected_home_margin"])
    total = float(estimate["projected_total"])
    winner = str(estimate["predicted_winner"])
    loser = away if winner == home else home
    stars = int(estimate["confidence_stars"])
    fit = prior.get("fit") or {}
    scoring = prior["league_scoring_rate"]
    applied = int(season["finals_applied"])
    winner_probability = probability if winner == home else 1 - probability

    def rated(side: dict[str, Any]) -> str:
        played = int(side["games_played"])
        if not played:
            return f"{side['team']} {_fmt(side['rating'], '.0f')} (preseason, no finals yet)"
        return (
            f"{side['team']} {_fmt(side['rating'], '.0f')} "
            f"({side['record']} this season, from {_fmt(side['preseason_rating'], '.0f')})"
        )

    first_season = str(prior.get("source", {}).get("seasons") or "1999").split("-")[0]
    supporting = [
        f"Ratings before kickoff: {rated(home_side)}; {rated(away_side)}.",
        f"Gap {_fmt(estimate['rating_gap'], '+.0f')} rating points plus "
        f"{_fmt(estimate['home_field_advantage'], '.0f')} for home field: adjusted "
        f"edge {_fmt(estimate['adjusted_gap'], '+.0f')}, a home win probability of "
        f"{probability:.1%} and an expected margin of {margin:+.1f} points "
        f"({_fmt(params['points_per_elo'], 'g')} rating points per point).",
        f"Elo replays every regular-season final since {first_season} (K "
        f"{_fmt(params['k'], 'g')}, margin-of-victory multiplier, "
        f"{_fmt(params['regression'], '.0%')} regression to "
        f"{_fmt(prior['base_rating'], '.0f')} between seasons); "
        f"{_count(applied, 'final')} from this season applied.",
    ]
    if fit.get("check_elo_brier") is not None and fit.get("check_closing_ml_brier") is not None:
        supporting.append(
            f"{fit.get('check_season')} check season: Brier "
            f"{_fmt(fit['check_elo_brier'], '.4f')} for the rating against "
            f"{_fmt(fit['check_closing_ml_brier'], '.4f')} for the de-vigged closing "
            "moneyline, roughly market-grade on the winner."
        )
    counter = [
        "Elo sees only final scores: injuries, quarterback changes, rest, "
        "weather, travel and the betting market are not inputs.",
        f"Preseason ratings are last season's regressed "
        f"{_fmt(params['regression'], '.0%')} toward "
        f"{_fmt(prior['base_rating'], '.0f')}, so with {_count(applied, 'final')} "
        "applied the estimate leans on last season's results.",
        f"{loser} would need to outperform the rating gap by about "
        f"{abs(margin):.1f} points to win; a single game's noise is far larger "
        "than that.",
    ]
    if estimate.get("tie_break"):
        counter.append(f"Tie break: {estimate['tie_break']}.")
    no_signal = [
        f"Total: the projected total {total:.1f} is the league scoring rate of the "
        f"{scoring['season']} season, not a game-specific estimate; the split "
        f"{away} {estimate['predicted_away_score']} - {home} "
        f"{estimate['predicted_home_score']} follows the margin only.",
    ]
    discarded = [
        "Head-to-head history, schedule cohorts, division context and season "
        "win totals are not inputs to a rating model and were not considered.",
    ]
    # No expert name here: the thesis reaches the judge unmasked.
    thesis = (
        f"Elo ratings: {winner} by {abs(margin):.1f} ({winner_probability:.0%}); "
        f"{home} {_fmt(home_side['rating'], '.0f')} vs {away} "
        f"{_fmt(away_side['rating'], '.0f')} with home field "
        f"+{_fmt(estimate['home_field_advantage'], '.0f')} Elo, "
        f"{_count(applied, 'final')} applied this season."
    )
    full_opinion = "\n\n".join(
        [
            "Ratings\n"
            f"- {home}: {_fmt(home_side['rating'], '.1f')} (preseason "
            f"{_fmt(home_side['preseason_rating'], '.1f')}, "
            f"{_count(home_side['games_played'], 'game')}, {home_side['record']} "
            "this season)\n"
            f"- {away}: {_fmt(away_side['rating'], '.1f')} (preseason "
            f"{_fmt(away_side['preseason_rating'], '.1f')}, "
            f"{_count(away_side['games_played'], 'game')}, {away_side['record']} "
            "this season)\n"
            f"- Home field +{_fmt(estimate['home_field_advantage'], '.0f')} Elo; "
            f"adjusted gap {_fmt(estimate['adjusted_gap'], '+.1f')}",
            "Estimate\n"
            f"- p({home}) {probability:.3f} · margin {margin:+.1f} · total {total:.1f} "
            f"(league scoring rate, {scoring['season']})\n"
            f"- Predicted score: {away} {estimate['predicted_away_score']} — {home} "
            f"{estimate['predicted_home_score']}\n"
            f"- Confidence: {'★' * stars} (|margin| thresholds "
            f"{'/'.join(_fmt(t, 'g') for t in STAR_MARGINS)})",
            "Model\n"
            f"- Elo K {_fmt(params['k'], 'g')}, home field {_fmt(params['hfa'], 'g')}, "
            "margin multiplier ln(|margin|+1)·2.2/(0.001·gap+2.2), "
            f"{_fmt(params['regression'], '.1%')} regression to "
            f"{_fmt(prior['base_rating'], '.0f')} between seasons, "
            f"{_fmt(params['points_per_elo'], 'g')} rating points per point of margin\n"
            + (
                f"- Fit {fit.get('fit_seasons')}: Brier {_fmt(fit['fit_brier'], '.4f')}; "
                f"check {fit.get('check_season')}: Brier "
                f"{_fmt(fit['check_elo_brier'], '.4f')} vs closing moneyline "
                f"{_fmt(fit['check_closing_ml_brier'], '.4f')}\n"
                if fit.get("fit_brier") is not None
                and fit.get("check_elo_brier") is not None
                and fit.get("check_closing_ml_brier") is not None
                else ""
            )
            + f"- Prior {prior['path']} ({prior['sha256'][:12]}), ratings through "
            f"{prior['through_season']}; {_count(applied, 'final')} applied this season"
            + (f" through {season['finals_through_utc']}" if season.get("finals_through_utc") else ""),
            "Why\n" + "\n".join(f"- {item}" for item in supporting),
            "Why it may be wrong\n" + "\n".join(f"- {item}" for item in counter),
            "No signal\n" + "\n".join(f"- {item}" for item in no_signal),
            "Discarded considerations\n" + "\n".join(f"- {item}" for item in discarded),
            f"Conclusion\n{thesis}",
        ]
    )
    return {
        "predicted_winner": winner,
        "predicted_away_score": int(estimate["predicted_away_score"]),
        "predicted_home_score": int(estimate["predicted_home_score"]),
        "home_win_probability": probability,
        "expected_home_margin": margin,
        "confidence_stars": stars,
        "thesis": thesis,
        "supporting_factors": supporting,
        "counterarguments": counter,
        "no_signal_factors": no_signal,
        "discarded_considerations": discarded,
        "full_opinion": full_opinion,
    }


def _string_list(value: Any, field: str, *, required: bool = False) -> list[str]:
    if not isinstance(value, list) or not all(
        isinstance(item, str) and item.strip() for item in value
    ):
        raise ValueError(f"{field} must be a list of non-empty strings")
    if required and not value:
        raise ValueError(f"{field} must not be empty")
    return [item.strip() for item in value]


def normalize_rating_opinion(
    response: dict[str, Any], input_payload: dict[str, Any]
) -> dict[str, Any]:
    """Check a rating response against the input's own estimate.

    The response is arithmetic on the input, so every number must equal the
    estimate the input carries; anything else is a tampered or stale
    response and fails validation (an audit row).
    """
    if input_payload.get("input_profile") != RATING_PROFILE:
        raise ValueError("Not a rating input")
    unknown = set(response) - RESPONSE_FIELDS
    if unknown:
        raise ValueError(f"Response has unexpected fields: {sorted(unknown)}")
    missing = RESPONSE_FIELDS - set(response)
    if missing:
        raise ValueError(f"Response is missing {sorted(missing)}")
    estimate = input_payload["estimate"]
    game = input_payload["game"]
    for field in ("home_win_probability", "expected_home_margin"):
        value = response[field]
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(float(value)):
            raise ValueError(f"{field} must be a finite number")
    probability = float(response["home_win_probability"])
    margin = float(response["expected_home_margin"])
    if not 0.01 <= probability <= 0.99:
        raise ValueError("home_win_probability must be between 0.01 and 0.99")
    if probability == 0.5 or margin == 0:
        raise ValueError("The rating must lean: probability 0.5 or margin 0 is not allowed")
    if (probability > 0.5) != (margin > 0):
        raise ValueError("home_win_probability and expected_home_margin disagree in sign")
    for field in ("predicted_away_score", "predicted_home_score", "confidence_stars"):
        value = response[field]
        if isinstance(value, bool) or not isinstance(value, int):
            raise ValueError(f"{field} must be an integer")
    if response["predicted_away_score"] == response["predicted_home_score"]:
        raise ValueError("predicted final score must not be tied")
    if not 1 <= int(response["confidence_stars"]) <= 5:
        raise ValueError("confidence_stars must be between 1 and 5")
    winner = str(response["predicted_winner"])
    if winner not in {str(game["away_team"]), str(game["home_team"])}:
        raise ValueError("predicted_winner must be one of the game's teams")
    expected = {
        "predicted_winner": str(estimate["predicted_winner"]),
        "predicted_away_score": int(estimate["predicted_away_score"]),
        "predicted_home_score": int(estimate["predicted_home_score"]),
        "home_win_probability": float(estimate["home_win_probability"]),
        "expected_home_margin": float(estimate["expected_home_margin"]),
        "confidence_stars": int(estimate["confidence_stars"]),
    }
    for field, value in expected.items():
        if response[field] != value:
            raise ValueError(
                f"{field} {response[field]!r} does not match the input's estimate {value!r}"
            )
    thesis = str(response["thesis"]).strip()
    full_opinion = str(response["full_opinion"]).strip()
    if not thesis or not full_opinion:
        raise ValueError("thesis and full_opinion are required")
    return {
        "predicted_winner": winner,
        "predicted_away_score": int(response["predicted_away_score"]),
        "predicted_home_score": int(response["predicted_home_score"]),
        "home_win_probability": round(probability, 4),
        "expected_home_margin": round(margin, 2),
        "confidence_stars": int(response["confidence_stars"]),
        "pick_market": "straight_up",
        "pick_side": winner,
        "thesis": thesis,
        "supporting_factors": _string_list(
            response["supporting_factors"], "supporting_factors", required=True
        ),
        "counterarguments": _string_list(
            response["counterarguments"], "counterarguments", required=True
        ),
        "no_signal_factors": _string_list(response["no_signal_factors"], "no_signal_factors"),
        "discarded_considerations": _string_list(
            response["discarded_considerations"], "discarded_considerations"
        ),
        "full_opinion": full_opinion,
    }


def rating_input_sha256(input_payload: dict[str, Any]) -> str:
    return sha256_text(canonical_json(input_payload))
