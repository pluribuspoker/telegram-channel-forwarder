"""Deterministic input builder for the NFL Win Total Expert."""

from __future__ import annotations

import hashlib
import statistics
from collections import Counter
from typing import Any, Callable

from nfl_win_predictions import latest_predictions_for_user


def _opaque_reference(*parts: Any) -> str:
    value = "\0".join(str(part or "") for part in parts)
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _preferred_side(
    away_team: str,
    home_team: str,
    away_value: float,
    home_value: float,
) -> tuple[str, float]:
    difference = round(home_value - away_value, 2)
    if difference > 0:
        return home_team, difference
    if difference < 0:
        return away_team, difference
    return "tie", difference


def _prior_win_bucket(wins: int) -> str:
    if wins >= 12:
        return "12_plus"
    if wins >= 9:
        return "9_to_11"
    if wins >= 6:
        return "6_to_8"
    return "0_to_5"


def _result_sample(
    games: list[dict[str, Any]],
    perspective: Callable[[dict[str, Any]], str],
    *,
    description: str,
) -> dict[str, Any]:
    wins = 0
    losses = 0
    ties = 0
    margins: list[int] = []
    for game in games:
        side = perspective(game)
        away_score = int(game["away_score"])
        home_score = int(game["home_score"])
        margin = (
            home_score - away_score
            if side == "home"
            else away_score - home_score
        )
        margins.append(margin)
        if margin > 0:
            wins += 1
        elif margin < 0:
            losses += 1
        else:
            ties += 1
    total = len(games)
    return {
        "description": description,
        "games": total,
        "wins": wins,
        "losses": losses,
        "ties": ties,
        "win_rate": round(wins / total, 4) if total else None,
        "average_margin": (
            round(sum(margins) / total, 2) if total else None
        ),
    }


def _latest_game_pick(
    leans: list[dict[str, Any]],
    *,
    event_id: str,
    user_id: str,
    display_name: str,
) -> dict[str, Any] | None:
    candidates = []
    celebrity_name = display_name.removeprefix("🎤 ").strip().casefold()
    for row in leans:
        if str(row.get("event_id") or "") != event_id:
            continue
        if user_id.startswith("-"):
            matches = (
                str(row.get("celebrity_name") or "").strip().casefold()
                == celebrity_name
            )
        else:
            matches = str(row.get("telegram_user_id") or "") == user_id
            if row.get("celebrity_name"):
                matches = False
        if matches:
            candidates.append(row)
    if not candidates:
        return None
    return max(
        candidates,
        key=lambda row: (
            str(row.get("submitted_at_utc") or ""),
            str(row.get("submission_id") or ""),
        ),
    )


def _historical_analogs(
    game: dict[str, Any],
    game_history: list[dict[str, Any]],
    team_history: list[dict[str, Any]],
) -> tuple[dict[str, Any], dict[str, Any]]:
    season = int(game["season"])
    away_team = str(game["away_team"])
    home_team = str(game["home_team"])
    wins_by_team_season = {
        (int(row["season"]), str(row["team"])): int(row["wins"])
        for row in team_history
    }
    away_prior = wins_by_team_season.get((season - 1, away_team))
    home_prior = wins_by_team_season.get((season - 1, home_team))
    team_context = {
        "prior_season": season - 1,
        "away_team": {
            "team": away_team,
            "wins": away_prior,
        },
        "home_team": {
            "team": home_team,
            "wins": home_prior,
        },
    }
    if away_prior is None or home_prior is None:
        return team_context, {
            "method": "prior_season_wins_only",
            "limitation": (
                "Historical preseason bookmaker win totals are unavailable."
            ),
            "exact_prior_win_pair": _result_sample(
                [],
                lambda _: "home",
                description="No prior-season record is available.",
            ),
            "matching_prior_win_gap": _result_sample(
                [],
                lambda _: "home",
                description="No prior-season record is available.",
            ),
            "matching_prior_win_level": _result_sample(
                [],
                lambda _: "home",
                description="No prior-season record is available.",
            ),
        }

    annotated = []
    for row in game_history:
        row_season = int(row["season"])
        if row_season >= season:
            continue
        historical_away_wins = wins_by_team_season.get(
            (row_season - 1, str(row["away_team"]))
        )
        historical_home_wins = wins_by_team_season.get(
            (row_season - 1, str(row["home_team"]))
        )
        if historical_away_wins is None or historical_home_wins is None:
            continue
        annotated.append(
            {
                **row,
                "away_prior_wins": historical_away_wins,
                "home_prior_wins": historical_home_wins,
            }
        )

    exact = [
        row
        for row in annotated
        if int(row["away_prior_wins"]) == away_prior
        and int(row["home_prior_wins"]) == home_prior
    ]
    current_gap = abs(home_prior - away_prior)
    if current_gap == 0:
        matching_gap = [
            row
            for row in annotated
            if int(row["away_prior_wins"]) == int(row["home_prior_wins"])
        ]
        gap_perspective = lambda _row: "home"
        gap_description = (
            "Historical home-side results when both teams entered with equal "
            "prior-season win totals."
        )
    else:
        gap_bucket = (
            "1"
            if current_gap == 1
            else "2_to_3"
            if current_gap <= 3
            else "4_plus"
        )

        def in_gap_bucket(row: dict[str, Any]) -> bool:
            gap = abs(
                int(row["home_prior_wins"])
                - int(row["away_prior_wins"])
            )
            candidate = (
                "1" if gap == 1 else "2_to_3" if gap <= 3 else "4_plus"
            )
            return gap > 0 and candidate == gap_bucket

        matching_gap = [row for row in annotated if in_gap_bucket(row)]
        gap_perspective = lambda row: (
            "home"
            if int(row["home_prior_wins"])
            > int(row["away_prior_wins"])
            else "away"
        )
        gap_description = (
            "Historical results for the side entering with more prior-season "
            f"wins in the {gap_bucket} win-gap bucket."
        )

    away_bucket = _prior_win_bucket(away_prior)
    home_bucket = _prior_win_bucket(home_prior)
    matching_level = [
        row
        for row in annotated
        if _prior_win_bucket(int(row["away_prior_wins"])) == away_bucket
        and _prior_win_bucket(int(row["home_prior_wins"])) == home_bucket
    ]
    return team_context, {
        "method": "prior_season_wins_only",
        "limitation": (
            "Historical preseason bookmaker win totals are unavailable, so "
            "these analogs use only records entering the season."
        ),
        "exact_prior_win_pair": _result_sample(
            exact,
            lambda _row: "home",
            description=(
                "Historical home-side results with the exact current "
                f"away/home prior-win pair ({away_prior}, {home_prior})."
            ),
        )
        | {
            "away_prior_wins": away_prior,
            "home_prior_wins": home_prior,
        },
        "matching_prior_win_gap": _result_sample(
            matching_gap,
            gap_perspective,
            description=gap_description,
        )
        | {"current_prior_win_gap": current_gap},
        "matching_prior_win_level": _result_sample(
            matching_level,
            lambda _row: "home",
            description=(
                "Historical home-side results with the current prior-win "
                f"level pair ({away_bucket}, {home_bucket})."
            ),
        )
        | {
            "away_prior_win_bucket_minimum": (
                12
                if away_bucket == "12_plus"
                else 9
                if away_bucket == "9_to_11"
                else 6
                if away_bucket == "6_to_8"
                else 0
            ),
            "home_prior_win_bucket_minimum": (
                12
                if home_bucket == "12_plus"
                else 9
                if home_bucket == "9_to_11"
                else 6
                if home_bucket == "6_to_8"
                else 0
            ),
        },
    }


def build_win_total_input(
    game: dict[str, Any],
    game_history: list[dict[str, Any]],
    win_totals: list[dict[str, Any]],
    win_predictions: list[dict[str, Any]],
    team_history: list[dict[str, Any]],
    leans: list[dict[str, Any]],
) -> dict[str, Any]:
    """Build the whitelisted Win Total Expert input."""
    season = int(game["season"])
    event_id = str(game["event_id"])
    away_team = str(game["away_team"])
    home_team = str(game["home_team"])

    market_by_team: dict[str, dict[str, Any]] = {}
    for row in win_totals:
        if int(row["season"]) != season:
            continue
        team = str(row["team"])
        if team not in {away_team, home_team}:
            continue
        current = market_by_team.get(team)
        key = (
            str(row.get("captured_at_utc") or ""),
            str(row.get("captured_at_et") or ""),
        )
        current_key = (
            str(current.get("captured_at_utc") or ""),
            str(current.get("captured_at_et") or ""),
        ) if current else ("", "")
        if current is None or key > current_key:
            market_by_team[team] = row
    if set(market_by_team) != {away_team, home_team}:
        raise ValueError("Win Total Expert requires both current market totals")
    away_market = float(market_by_team[away_team]["win_total"])
    home_market = float(market_by_team[home_team]["win_total"])
    market_side, market_difference = _preferred_side(
        away_team, home_team, away_market, home_market
    )
    market = {
        "bookmaker": str(market_by_team[home_team]["bookmaker"]),
        "season": season,
        "away_team": {
            "team": away_team,
            "win_total": away_market,
        },
        "home_team": {
            "team": home_team,
            "win_total": home_market,
        },
        "higher_total_side": market_side,
        "home_minus_away": market_difference,
        "captured_at_et": str(
            market_by_team[home_team].get("captured_at_et") or ""
        ),
    }

    rows_by_user: dict[str, list[dict[str, Any]]] = {}
    for row in win_predictions:
        if int(row["season"]) != season:
            continue
        user_id = str(row.get("telegram_user_id") or "")
        if user_id:
            rows_by_user.setdefault(user_id, []).append(row)

    forecasters: dict[str, dict[str, Any]] = {}
    complete_pairs: list[tuple[float, float]] = []
    for index, (user_id, rows) in enumerate(
        sorted(
            rows_by_user.items(),
            key=lambda item: (
                str(item[1][-1].get("telegram_display_name") or "").casefold(),
                item[0],
            ),
        ),
        start=1,
    ):
        latest, _ = latest_predictions_for_user(rows, user_id)
        away_row = latest.get(away_team)
        home_row = latest.get(home_team)
        if away_row is None and home_row is None:
            continue
        display_name = str(
            (home_row or away_row or rows[-1]).get(
                "telegram_display_name"
            )
            or (home_row or away_row or rows[-1]).get("telegram_username")
            or f"Forecaster {index}"
        )
        away_prediction = (
            int(away_row["predicted_wins"]) if away_row is not None else None
        )
        home_prediction = (
            int(home_row["predicted_wins"]) if home_row is not None else None
        )
        preferred_side = "incomplete"
        difference = None
        if away_prediction is not None and home_prediction is not None:
            preferred_side, difference = _preferred_side(
                away_team,
                home_team,
                away_prediction,
                home_prediction,
            )
            complete_pairs.append((away_prediction, home_prediction))

        game_pick = _latest_game_pick(
            leans,
            event_id=event_id,
            user_id=user_id,
            display_name=display_name,
        )
        if game_pick is None:
            consistency = "no_game_pick"
            game_pick_payload = {"available": False}
        else:
            side = str(game_pick.get("side") or "")
            comparable = side in {away_team, home_team}
            if preferred_side in {"incomplete", "tie"}:
                consistency = (
                    "incomplete_season_picks"
                    if preferred_side == "incomplete"
                    else "tied_season_picks"
                )
            elif not comparable:
                consistency = "no_comparable_game_pick"
            else:
                consistency = (
                    "consistent" if side == preferred_side else "inconsistent"
                )
            game_pick_payload = {
                "available": True,
                "market": str(game_pick.get("market") or ""),
                "period": str(game_pick.get("period") or ""),
                "side": side,
                "consistency_with_season_picks": consistency,
                "submission_reference": _opaque_reference(
                    game_pick.get("submission_id"),
                    game_pick.get("submitted_at_utc"),
                ),
            }
        forecasters[f"forecaster_{index:02d}"] = {
            "display_name": display_name,
            "identity_type": (
                "celebrity" if user_id.startswith("-") else "telegram_user"
            ),
            "away_predicted_wins": away_prediction,
            "home_predicted_wins": home_prediction,
            "season_preferred_side": preferred_side,
            "home_minus_away": difference,
            "latest_game_pick": game_pick_payload,
            "season_prediction_reference": _opaque_reference(
                away_row.get("revision_id") if away_row else "",
                home_row.get("revision_id") if home_row else "",
            ),
        }

    if not complete_pairs:
        raise ValueError(
            "Win Total Expert requires at least one complete season prediction"
        )
    away_values = [pair[0] for pair in complete_pairs]
    home_values = [pair[1] for pair in complete_pairs]
    home_higher = sum(home > away for away, home in complete_pairs)
    away_higher = sum(away > home for away, home in complete_pairs)
    ties = len(complete_pairs) - home_higher - away_higher
    average_away = round(statistics.mean(away_values), 2)
    average_home = round(statistics.mean(home_values), 2)
    consensus_side, consensus_difference = _preferred_side(
        away_team, home_team, average_away, average_home
    )
    consensus = {
        "forecasters_with_both_teams": len(complete_pairs),
        "home_team_higher_count": home_higher,
        "away_team_higher_count": away_higher,
        "tied_count": ties,
        "average_away_wins": average_away,
        "average_home_wins": average_home,
        "median_away_wins": statistics.median(away_values),
        "median_home_wins": statistics.median(home_values),
        "higher_average_side": consensus_side,
        "average_home_minus_away": consensus_difference,
    }
    team_context, historical_analogs = _historical_analogs(
        game,
        game_history,
        team_history,
    )
    return {
        "input_profile": "win_total",
        "game": {
            "event_id": event_id,
            "season": season,
            "week": (
                int(game["week"])
                if str(game.get("week") or "").strip()
                else None
            ),
            "commence_time_utc": str(game["commence_time_utc"]),
            "away_team": away_team,
            "home_team": home_team,
        },
        "market": market,
        "consensus": consensus,
        "forecasters": forecasters,
        "team_context": team_context,
        "historical_analogs": historical_analogs,
    }
