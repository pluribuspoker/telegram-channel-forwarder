"""Deterministic market-outlier input for the NFL Hi Lo Expert."""

from __future__ import annotations

import csv
import math
from collections import defaultdict
from pathlib import Path
from typing import Any, Iterable

from nfl_lines import (
    LATEST_AWAY_COLUMN,
    LATEST_HOME_COLUMN,
    LATEST_TOTALS_COLUMN,
    decode_packed_markets,
)


ROOT = Path(__file__).resolve().parent
HISTORICAL_LINES_PATH = ROOT / "data" / "nfl_lines_history.csv"
PERIODS = ("game", "first_half", "first_quarter")
MIN_BOARD_GAMES = 4


def _number(value: Any) -> float | None:
    if value in (None, ""):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _decoded_game(row: dict[str, Any]) -> dict[str, Any]:
    markets = decode_packed_markets(
        str(row.get(LATEST_AWAY_COLUMN) or ""),
        str(row.get(LATEST_HOME_COLUMN) or ""),
        str(row.get(LATEST_TOTALS_COLUMN) or ""),
    )
    return {
        "event_id": str(row["event_id"]),
        "away_team": str(row["away_team"]),
        "home_team": str(row["home_team"]),
        "markets": markets,
    }


def _market_values(
    decoded: dict[str, Any],
    period: str,
) -> dict[str, dict[str, Any]]:
    market = decoded["markets"][period]
    away = decoded["away_team"]
    home = decoded["home_team"]
    home_spread = _number(market.get("home_spread"))
    away_ml = _number(market.get("away_moneyline"))
    home_ml = _number(market.get("home_moneyline"))
    values: dict[str, dict[str, Any]] = {}
    if home_spread is not None and home_spread != 0:
        values["largest_spread"] = {
            "value": abs(home_spread),
            "direction": "high",
            "selected_side": home if home_spread > 0 else away,
            "selection": "underdog ATS",
        }
    total = _number(market.get("total"))
    if total is not None:
        values["highest_total"] = {
            "value": total,
            "direction": "high",
            "selected_side": "Over",
            "selection": "Over",
        }
        values["lowest_total"] = {
            "value": total,
            "direction": "low",
            "selected_side": "Under",
            "selection": "Under",
        }
    if away_ml is not None and home_ml is not None:
        dog_side, dog_price = (
            (away, away_ml) if away_ml > home_ml else (home, home_ml)
        )
        favorite_side, favorite_price = (
            (away, away_ml) if away_ml < home_ml else (home, home_ml)
        )
        values["largest_moneyline_underdog"] = {
            "value": dog_price,
            "direction": "high",
            "selected_side": dog_side,
            "selection": "moneyline underdog",
        }
        values["largest_moneyline_favorite"] = {
            "value": favorite_price,
            "direction": "low",
            "selected_side": favorite_side,
            "selection": "moneyline favorite",
        }
    return values


def _distance_to_next(values: list[float], extreme: float, direction: str) -> float | None:
    distinct = sorted(set(values), reverse=direction == "high")
    if len(distinct) < 2:
        return None
    return round(abs(extreme - distinct[1]), 3)


def _weekly_positions(
    rows: list[dict[str, Any]],
    target_event_id: str,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    decoded = [_decoded_game(row) for row in rows]
    required_games = max(MIN_BOARD_GAMES, math.ceil(len(decoded) / 2))
    positions: list[dict[str, Any]] = []
    outliers: list[dict[str, Any]] = []
    for period in PERIODS:
        values_by_category: dict[str, list[tuple[dict[str, Any], dict[str, Any]]]] = (
            defaultdict(list)
        )
        for game in decoded:
            for category, item in _market_values(game, period).items():
                values_by_category[category].append((game, item))
        for category, entries in sorted(values_by_category.items()):
            target = next(
                (
                    (game, item)
                    for game, item in entries
                    if game["event_id"] == target_event_id
                ),
                None,
            )
            if target is None:
                continue
            target_game, target_item = target
            direction = str(target_item["direction"])
            ordered = sorted(
                {float(item["value"]) for _, item in entries},
                reverse=direction == "high",
            )
            value = float(target_item["value"])
            rank = ordered.index(value) + 1
            extreme = ordered[0]
            tied = [
                game["event_id"]
                for game, item in entries
                if float(item["value"]) == extreme
            ]
            board_eligible = len(entries) >= required_games
            position = {
                "period": period,
                "category": category,
                "value": value,
                "direction": direction,
                "rank": rank,
                "distinct_values": len(ordered),
                "games_with_market": len(entries),
                "week_games": len(decoded),
                "required_games": required_games,
                "coverage_ratio": round(
                    len(entries) / len(decoded),
                    4,
                ),
                "selected_side": target_item["selected_side"],
                "selection": target_item["selection"],
                "board_eligible": board_eligible,
                "is_weekly_extreme": rank == 1 and board_eligible,
            }
            positions.append(position)
            if rank == 1 and board_eligible:
                outliers.append(
                    {
                        **position,
                        "tied_event_ids": sorted(tied),
                        "tie_count": len(tied),
                        "distance_to_next": _distance_to_next(
                            [float(item["value"]) for _, item in entries],
                            extreme,
                            direction,
                        ),
                    }
                )
    return positions, outliers


def _result_record(results: list[str]) -> dict[str, Any]:
    wins = results.count("W")
    losses = results.count("L")
    pushes = results.count("P")
    decided = wins + losses
    return {
        "observations": len(results),
        "wins": wins,
        "losses": losses,
        "ties": pushes,
        "record": f"{wins}-{losses}-{pushes}",
        "hit_rate": round(wins / decided, 4) if decided else None,
    }


def _historical_rows(path: Path = HISTORICAL_LINES_PATH) -> list[dict[str, str]]:
    with path.open(newline="", encoding="utf-8") as handle:
        return list(csv.DictReader(handle))


def _historical_weekly_extrema(
    categories: set[str],
    *,
    path: Path = HISTORICAL_LINES_PATH,
) -> dict[str, dict[str, Any]]:
    wanted = categories & {
        "largest_spread",
        "highest_total",
        "lowest_total",
        "largest_moneyline_underdog",
        "largest_moneyline_favorite",
    }
    if not wanted:
        return {}
    weeks: dict[tuple[int, int], list[dict[str, str]]] = defaultdict(list)
    for row in _historical_rows(path):
        try:
            weeks[(int(row["season"]), int(row["week"]))].append(row)
        except (KeyError, TypeError, ValueError):
            continue
    results: dict[str, list[str]] = defaultdict(list)
    residuals: dict[str, list[float]] = defaultdict(list)
    week_counts: dict[str, set[tuple[int, int]]] = defaultdict(set)
    for week_key, rows in weeks.items():
        prepared = []
        for row in rows:
            home_spread = _number(row.get("home_spread"))
            total = _number(row.get("total"))
            away_ml = _number(row.get("away_moneyline"))
            home_ml = _number(row.get("home_moneyline"))
            away_score = _number(row.get("away_score"))
            home_score = _number(row.get("home_score"))
            if away_score is None or home_score is None:
                continue
            prepared.append(
                {
                    "row": row,
                    "home_spread": home_spread,
                    "total": total,
                    "away_ml": away_ml,
                    "home_ml": home_ml,
                    "home_margin": home_score - away_score,
                    "points": home_score + away_score,
                }
            )
        metric_values: dict[str, list[float]] = defaultdict(list)
        for item in prepared:
            if item["home_spread"] not in (None, 0):
                metric_values["largest_spread"].append(abs(item["home_spread"]))
            if item["total"] is not None:
                metric_values["highest_total"].append(item["total"])
                metric_values["lowest_total"].append(item["total"])
            if item["away_ml"] is not None and item["home_ml"] is not None:
                metric_values["largest_moneyline_underdog"].append(
                    max(item["away_ml"], item["home_ml"])
                )
                metric_values["largest_moneyline_favorite"].append(
                    min(item["away_ml"], item["home_ml"])
                )
        extremes = {
            category: (
                max(values)
                if category not in {"lowest_total", "largest_moneyline_favorite"}
                else min(values)
            )
            for category, values in metric_values.items()
            if values and category in wanted
        }
        for item in prepared:
            for category, extreme in extremes.items():
                if category == "largest_spread":
                    spread = item["home_spread"]
                    if spread in (None, 0) or abs(spread) != extreme:
                        continue
                    dog_margin = -item["home_margin"] if spread < 0 else item["home_margin"]
                    dog_line = abs(spread)
                    residual = dog_margin + dog_line
                elif category in {"highest_total", "lowest_total"}:
                    if item["total"] != extreme:
                        continue
                    raw_residual = item["points"] - item["total"]
                    residual = (
                        raw_residual
                        if category == "highest_total"
                        else -raw_residual
                    )
                else:
                    if item["away_ml"] is None or item["home_ml"] is None:
                        continue
                    selected_price = (
                        max(item["away_ml"], item["home_ml"])
                        if category == "largest_moneyline_underdog"
                        else min(item["away_ml"], item["home_ml"])
                    )
                    if selected_price != extreme:
                        continue
                    away_selected = (
                        item["away_ml"] == selected_price
                    )
                    selected_margin = (
                        -item["home_margin"] if away_selected else item["home_margin"]
                    )
                    residual = selected_margin
                result = "W" if residual > 0 else "L" if residual < 0 else "P"
                results[category].append(result)
                residuals[category].append(float(residual))
                week_counts[category].add(week_key)
    summaries = {}
    for category in sorted(wanted):
        record = _result_record(results[category])
        summaries[category] = {
            "selection": {
                "largest_spread": "underdog ATS",
                "highest_total": "Over",
                "lowest_total": "Under",
                "largest_moneyline_underdog": "moneyline underdog",
                "largest_moneyline_favorite": "moneyline favorite",
            }[category],
            "weeks": len(week_counts[category]),
            **record,
            "mean_result_margin": (
                round(sum(residuals[category]) / len(residuals[category]), 3)
                if residuals[category]
                else None
            ),
            "data_scope": "regular seasons 1999-2025; tied weekly extrema included",
        }
    return summaries


def _season_extrema(
    rows: list[dict[str, Any]],
    target_outliers: list[dict[str, Any]],
) -> dict[str, dict[str, Any]]:
    decoded = [_decoded_game(row) for row in rows]
    keys = {
        (str(item["period"]), str(item["category"]))
        for item in target_outliers
    }
    result = {}
    for period, category in sorted(keys):
        entries = [
            (game, values[category])
            for game in decoded
            for values in [_market_values(game, period)]
            if category in values
        ]
        if not entries:
            continue
        direction = str(entries[0][1]["direction"])
        values = [float(item["value"]) for _, item in entries]
        extreme = max(values) if direction == "high" else min(values)
        result[f"{period}.{category}"] = {
            "value": extreme,
            "direction": direction,
            "event_ids": sorted(
                game["event_id"]
                for game, item in entries
                if float(item["value"]) == extreme
            ),
            "games_with_market": len(entries),
        }
    return result


def build_hi_lo_input(
    game: dict[str, Any],
    games: Iterable[dict[str, Any]],
    *,
    historical_path: Path = HISTORICAL_LINES_PATH,
) -> dict[str, Any]:
    """Build one target game's tie-aware weekly and season outlier package."""
    season = int(game["season"])
    season_type = str(game.get("season_type") or "")
    week = int(game["week"])
    season_games = [
        row
        for row in games
        if int(row.get("season") or 0) == season
        and str(row.get("season_type") or "") == season_type
    ]
    week_games = [
        row for row in season_games if int(row.get("week") or 0) == week
    ]
    positions, outliers = _weekly_positions(
        week_games,
        str(game["event_id"]),
    )
    full_game_categories = {
        str(item["category"])
        for item in positions
        if item["period"] == "game" and item["board_eligible"]
    }
    historical = _historical_weekly_extrema(
        full_game_categories,
        path=historical_path,
    )
    for period in ("first_half", "first_quarter"):
        if any(item["period"] == period for item in positions):
            historical[period] = {
                "status": "unavailable",
                "reason": (
                    "Historical first-half and first-quarter lines paired "
                    "with period scores are not stored."
                ),
            }
    return {
        "input_profile": "hi_lo_outliers",
        "game": {
            "event_id": str(game["event_id"]),
            "season": season,
            "season_type": str(game.get("season_type") or ""),
            "week": week,
            "commence_time_utc": str(game["commence_time_utc"]),
            "away_team": str(game["away_team"]),
            "home_team": str(game["home_team"]),
        },
        "current_game_outliers": outliers,
        "weekly_outlier_status": {
            "has_eligible_outlier": bool(outliers),
            "eligible_outlier_count": len(outliers),
            "statement": (
                f"This game has {len(outliers)} eligible weekly market "
                f"extreme{'s' if len(outliers) != 1 else ''}."
                if outliers
                else "This game has no eligible weekly market extreme."
            ),
        },
        "weekly_market_positions": positions,
        "season_extrema": _season_extrema(season_games, positions),
        "historical_weekly_extrema": historical,
        "data_limits": {
            "team_quality_inputs": "prohibited",
            "current_week_results": "prohibited",
            "period_outcomes": (
                "unavailable until period scores are persisted"
            ),
        },
    }
