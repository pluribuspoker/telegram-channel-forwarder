"""Deterministic input construction for the NFL Cee Expert."""

from __future__ import annotations

import hashlib
from collections import Counter
from datetime import datetime, timezone
from typing import Any, Iterable

from nfl_lines import (
    LATEST_AWAY_COLUMN,
    LATEST_HOME_COLUMN,
    LATEST_TOTALS_COLUMN,
    decode_packed_markets,
)


def _parse_time(value: Any) -> datetime:
    return datetime.fromisoformat(str(value).replace("Z", "+00:00")).astimezone(
        timezone.utc
    )


def _opaque_reference(*parts: Any) -> str:
    value = "\0".join(str(part or "") for part in parts)
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _preferred_side(
    away_team: str,
    home_team: str,
    away_wins: int,
    home_wins: int,
) -> tuple[str, int]:
    difference = home_wins - away_wins
    if difference > 0:
        return home_team, difference
    if difference < 0:
        return away_team, difference
    return "tie", difference


def _gap_bucket(difference: int) -> str:
    magnitude = abs(difference)
    if magnitude == 0:
        return "tie"
    if magnitude == 1:
        return "one_win"
    if magnitude <= 3:
        return "two_to_three_wins"
    return "four_plus_wins"


def _predictions_as_of(
    predictions: Iterable[dict[str, Any]],
    *,
    user_id: str,
    season: int,
    away_team: str,
    home_team: str,
    as_of: datetime,
) -> dict[str, dict[str, Any]]:
    latest: dict[str, tuple[tuple[str, str], dict[str, Any]]] = {}
    for row in predictions:
        if str(row.get("telegram_user_id") or "") != str(user_id):
            continue
        if int(row.get("season") or 0) != season:
            continue
        team = str(row.get("team") or "")
        if team not in {away_team, home_team}:
            continue
        submitted = _parse_time(row["submitted_at_utc"])
        if submitted > as_of:
            continue
        key = (
            submitted.isoformat(),
            str(row.get("revision_id") or ""),
        )
        if team not in latest or key > latest[team][0]:
            latest[team] = (key, row)
    return {team: row for team, (_, row) in latest.items()}


def _season_context(
    lean: dict[str, Any],
    predictions: Iterable[dict[str, Any]],
    *,
    user_id: str,
) -> dict[str, Any] | None:
    away_team = str(lean["away_team"])
    home_team = str(lean["home_team"])
    rows = _predictions_as_of(
        predictions,
        user_id=user_id,
        season=int(lean["season"]),
        away_team=away_team,
        home_team=home_team,
        as_of=_parse_time(lean["submitted_at_utc"]),
    )
    if set(rows) != {away_team, home_team}:
        return None
    away_wins = int(rows[away_team]["predicted_wins"])
    home_wins = int(rows[home_team]["predicted_wins"])
    preferred_side, difference = _preferred_side(
        away_team,
        home_team,
        away_wins,
        home_wins,
    )
    selected_side = str(lean.get("side") or "")
    if preferred_side == "tie":
        consistency = "tied_season_picks"
    elif selected_side == preferred_side:
        consistency = "consistent"
    else:
        consistency = "inconsistent"
    return {
        "away_team": away_team,
        "away_predicted_wins": away_wins,
        "home_team": home_team,
        "home_predicted_wins": home_wins,
        "season_preferred_side": preferred_side,
        "home_minus_away": difference,
        "season_gap_bucket": _gap_bucket(difference),
        "consistency_with_game_pick": consistency,
        "prediction_reference": _opaque_reference(
            rows[away_team].get("revision_id"),
            rows[home_team].get("revision_id"),
        ),
    }


def _matching_history_game(
    lean: dict[str, Any],
    history: Iterable[dict[str, Any]],
) -> dict[str, Any] | None:
    kickoff = _parse_time(lean["commence_time_utc"])
    matches = [
        row
        for row in history
        if str(row.get("away_team")) == str(lean.get("away_team"))
        and str(row.get("home_team")) == str(lean.get("home_team"))
        and abs((_parse_time(row["kickoff_utc"]) - kickoff).total_seconds())
        <= 6 * 3600
    ]
    return matches[0] if len(matches) == 1 else None


def _record(results: Iterable[str]) -> dict[str, Any]:
    values = list(results)
    counts = Counter(values)
    return {
        "wins": counts["W"],
        "losses": counts["L"],
        "ties": counts["T"],
        "games": len(values),
        "win_rate": (
            round(counts["W"] / len(values), 4) if values else None
        ),
        "chronological_results": "".join(values),
    }


def _grade_pick(lean: dict[str, Any], result: dict[str, Any]) -> str:
    away_score = int(result["away_score"])
    home_score = int(result["home_score"])
    if away_score == home_score:
        return "T"
    winner = (
        str(lean["away_team"])
        if away_score > home_score
        else str(lean["home_team"])
    )
    return "W" if str(lean["side"]) == winner else "L"


def _calibration(
    *,
    current: dict[str, Any],
    current_season: dict[str, Any],
    leans: Iterable[dict[str, Any]],
    predictions: Iterable[dict[str, Any]],
    history: Iterable[dict[str, Any]],
    user_id: str,
) -> dict[str, Any]:
    overall: list[str] = []
    matching_consistency: list[str] = []
    matching_gap: list[str] = []
    excluded = Counter()
    for lean in sorted(
        leans,
        key=lambda row: str(row.get("submitted_at_utc") or ""),
    ):
        if str(lean.get("telegram_user_id") or "") != str(user_id):
            continue
        if str(lean.get("event_id") or "") == str(current["event_id"]):
            continue
        if _parse_time(lean["submitted_at_utc"]) >= _parse_time(
            current["submitted_at_utc"]
        ):
            excluded["not_prior_to_current_pick"] += 1
            continue
        if str(lean.get("period") or "").casefold() != "game":
            continue
        if str(lean.get("market") or "").casefold() != "moneyline":
            continue
        if str(lean.get("side") or "") not in {
            str(lean.get("away_team") or ""),
            str(lean.get("home_team") or ""),
        }:
            excluded["invalid_side"] += 1
            continue
        if _parse_time(lean["submitted_at_utc"]) >= _parse_time(
            lean["commence_time_utc"]
        ):
            excluded["submitted_after_kickoff"] += 1
            continue
        result = _matching_history_game(lean, history)
        if result is None:
            excluded["no_completed_game"] += 1
            continue
        if _parse_time(result["kickoff_utc"]) >= _parse_time(
            current["commence_time_utc"]
        ):
            excluded["result_after_current_kickoff"] += 1
            continue
        season = _season_context(lean, predictions, user_id=user_id)
        if season is None:
            excluded["incomplete_season_predictions"] += 1
            continue
        verdict = _grade_pick(lean, result)
        overall.append(verdict)
        if (
            season["consistency_with_game_pick"]
            == current_season["consistency_with_game_pick"]
        ):
            matching_consistency.append(verdict)
        if (
            season["season_gap_bucket"]
            == current_season["season_gap_bucket"]
        ):
            matching_gap.append(verdict)
    return {
        "method": (
            "Resolved pre-kickoff Cee full-game moneyline picks, using Cee's "
            "latest season-win predictions available when each pick was made."
        ),
        "eligible_predictions": len(overall),
        "excluded_counts": dict(sorted(excluded.items())),
        "overall": _record(overall),
        "matching_consistency": {
            "consistency_label": current_season[
                "consistency_with_game_pick"
            ],
            **_record(matching_consistency),
        },
        "matching_season_gap": {
            "season_gap_bucket": current_season["season_gap_bucket"],
            **_record(matching_gap),
        },
    }


def _submission_market(row: dict[str, Any]) -> dict[str, Any]:
    return decode_packed_markets(
        str(row.get(LATEST_AWAY_COLUMN) or ""),
        str(row.get(LATEST_HOME_COLUMN) or ""),
        str(row.get(LATEST_TOTALS_COLUMN) or ""),
    )["game"]


def build_cee_input(
    game: dict[str, Any],
    history: list[dict[str, Any]],
    leans: list[dict[str, Any]],
    win_predictions: list[dict[str, Any]],
    *,
    cee_user_id: str,
) -> dict[str, Any]:
    """Build a whitelisted Cee Expert input for one game."""
    matching = [
        row
        for row in leans
        if str(row.get("telegram_user_id") or "") == str(cee_user_id)
        and str(row.get("event_id") or "") == str(game["event_id"])
        and str(row.get("period") or "").casefold() == "game"
        and str(row.get("market") or "").casefold() == "moneyline"
    ]
    if not matching:
        raise ValueError("Cee has no full-game moneyline pick for this event")
    current = max(
        matching,
        key=lambda row: (
            str(row.get("submitted_at_utc") or ""),
            str(row.get("submission_id") or ""),
        ),
    )
    if _parse_time(current["submitted_at_utc"]) >= _parse_time(
        game["commence_time_utc"]
    ):
        raise ValueError("Cee pick was submitted after kickoff")
    selected_side = str(current.get("side") or "")
    if selected_side not in {
        str(game["away_team"]),
        str(game["home_team"]),
    }:
        raise ValueError("Cee moneyline pick does not select either game team")
    season = _season_context(
        current,
        win_predictions,
        user_id=cee_user_id,
    )
    if season is None:
        raise ValueError(
            "Cee Expert requires season-win predictions for both teams "
            "submitted before the game pick"
        )
    calibration = _calibration(
        current=current,
        current_season=season,
        leans=leans,
        predictions=win_predictions,
        history=history,
        user_id=cee_user_id,
    )
    return {
        "input_profile": "cee_calibration",
        "game": {
            "event_id": str(game["event_id"]),
            "season": int(game["season"]),
            "week": (
                int(game["week"])
                if str(game.get("week") or "").strip()
                else None
            ),
            "commence_time_utc": str(game["commence_time_utc"]),
            "away_team": str(game["away_team"]),
            "home_team": str(game["home_team"]),
        },
        "cee_submission": {
            "submission_reference": _opaque_reference(
                current.get("submission_id"),
                current.get("submitted_at_utc"),
            ),
            "submitted_at_utc": str(current["submitted_at_utc"]),
            "selected_market": "moneyline",
            "selected_side": selected_side,
            "rationale": str(current.get("lean_text") or ""),
        },
        "season_predictions_at_submission": season,
        "submission_market": _submission_market(current),
        "nfl_calibration": calibration,
    }
