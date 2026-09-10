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
    submission_terms,
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
    current_decision_pattern: str,
    current_spread: dict[str, Any] | None,
    current_spread_decision_pattern: str | None,
) -> dict[str, Any]:
    overall: list[str] = []
    matching_consistency: list[str] = []
    matching_gap: list[str] = []
    matching_decision_pattern: list[str] = []
    excluded = Counter()
    grouped: dict[str, list[dict[str, Any]]] = {}
    for lean in leans:
        if str(lean.get("telegram_user_id") or "") != str(user_id):
            continue
        if str(lean.get("event_id") or "") == str(current["event_id"]):
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
            continue
        grouped.setdefault(str(lean["event_id"]), []).append(lean)

    for submissions in grouped.values():
        submissions.sort(
            key=lambda row: (
                str(row.get("submitted_at_utc") or ""),
                str(row.get("submission_id") or ""),
            )
        )
        final = submissions[-1]
        if _parse_time(final["submitted_at_utc"]) >= _parse_time(
            current["submitted_at_utc"]
        ):
            excluded["not_prior_to_current_pick"] += 1
            continue
        result = _matching_history_game(final, history)
        if result is None:
            excluded["no_completed_game"] += 1
            continue
        if _parse_time(result["kickoff_utc"]) >= _parse_time(
            current["commence_time_utc"]
        ):
            excluded["result_after_current_kickoff"] += 1
            continue
        season = _season_context(final, predictions, user_id=user_id)
        if season is None:
            excluded["incomplete_season_predictions"] += 1
            continue
        decision = _decision_history(submissions, market="moneyline")
        assert decision is not None
        verdict = _grade_pick(final, result)
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
        if decision["decision_pattern"] == current_decision_pattern:
            matching_decision_pattern.append(verdict)
    calibration = {
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
        "matching_decision_pattern": {
            "decision_pattern": current_decision_pattern,
            **_record(matching_decision_pattern),
        },
    }
    if (
        current_spread is not None
        and current_spread_decision_pattern is not None
    ):
        calibration["matching_spread_decision_pattern"] = (
            _spread_decision_calibration(
                current=current_spread,
                current_decision_pattern=current_spread_decision_pattern,
                leans=leans,
                history=history,
                user_id=user_id,
            )
        )
    return calibration


def _submission_market(row: dict[str, Any]) -> dict[str, Any]:
    return decode_packed_markets(
        str(row.get(LATEST_AWAY_COLUMN) or ""),
        str(row.get(LATEST_HOME_COLUMN) or ""),
        str(row.get(LATEST_TOTALS_COLUMN) or ""),
    )["game"]


def _selected_spread_terms(row: dict[str, Any]) -> dict[str, Any]:
    terms = submission_terms(row)
    if terms["line"] is not None or terms["source"] != "legacy_betonline":
        return terms
    market = _submission_market(row)
    side = str(row.get("side") or "")
    if side == str(row.get("away_team") or ""):
        line = market["away_spread"]
    elif side == str(row.get("home_team") or ""):
        line = market["home_spread"]
    else:
        line = None
    return {**terms, "line": line}


def _grade_spread_pick(
    lean: dict[str, Any],
    result: dict[str, Any],
) -> str | None:
    terms = _selected_spread_terms(lean)
    selected_side = str(lean["side"])
    away_team = str(lean["away_team"])
    home_team = str(lean["home_team"])
    if selected_side == away_team:
        line = terms["line"]
        selected_score = int(result["away_score"])
        opponent_score = int(result["home_score"])
    elif selected_side == home_team:
        line = terms["line"]
        selected_score = int(result["home_score"])
        opponent_score = int(result["away_score"])
    else:
        return None
    if line is None:
        return None
    adjusted_margin = selected_score - opponent_score + float(line)
    if adjusted_margin > 0:
        return "W"
    if adjusted_margin < 0:
        return "L"
    return "T"


def _spread_decision_calibration(
    *,
    current: dict[str, Any],
    current_decision_pattern: str,
    leans: Iterable[dict[str, Any]],
    history: Iterable[dict[str, Any]],
    user_id: str,
) -> dict[str, Any]:
    matching: list[str] = []
    excluded = Counter()
    grouped: dict[str, list[dict[str, Any]]] = {}
    for lean in leans:
        if str(lean.get("telegram_user_id") or "") != str(user_id):
            continue
        if str(lean.get("event_id") or "") == str(current["event_id"]):
            continue
        if str(lean.get("period") or "").casefold() != "game":
            continue
        if str(lean.get("market") or "").casefold() != "spread":
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
            continue
        grouped.setdefault(str(lean["event_id"]), []).append(lean)

    for submissions in grouped.values():
        submissions.sort(
            key=lambda row: (
                str(row.get("submitted_at_utc") or ""),
                str(row.get("submission_id") or ""),
            )
        )
        final = submissions[-1]
        if _parse_time(final["submitted_at_utc"]) >= _parse_time(
            current["submitted_at_utc"]
        ):
            excluded["not_prior_to_current_pick"] += 1
            continue
        result = _matching_history_game(final, history)
        if result is None:
            excluded["no_completed_game"] += 1
            continue
        if _parse_time(result["kickoff_utc"]) >= _parse_time(
            current["commence_time_utc"]
        ):
            excluded["result_after_current_kickoff"] += 1
            continue
        decision = _decision_history(submissions, market="spread")
        assert decision is not None
        if decision["decision_pattern"] != current_decision_pattern:
            continue
        verdict = _grade_spread_pick(final, result)
        if verdict is None:
            excluded["missing_spread_line"] += 1
            continue
        matching.append(verdict)
    return {
        "method": (
            "Resolved pre-kickoff Cee full-game spread picks, counted once "
            "per game using the final eligible spread and its submitted line."
        ),
        "decision_pattern": current_decision_pattern,
        "excluded_counts": dict(sorted(excluded.items())),
        **_record(matching),
    }


def _eligible_submissions(
    leans: Iterable[dict[str, Any]],
    *,
    game: dict[str, Any],
    user_id: str,
    market: str,
) -> list[dict[str, Any]]:
    kickoff = _parse_time(game["commence_time_utc"])
    teams = {
        str(game["away_team"]),
        str(game["home_team"]),
    }
    matching = sorted(
        (
            row
            for row in leans
            if str(row.get("telegram_user_id") or "") == str(user_id)
            and str(row.get("event_id") or "") == str(game["event_id"])
            and str(row.get("period") or "").casefold() == "game"
            and str(row.get("market") or "").casefold() == market
            and _parse_time(row["submitted_at_utc"]) < kickoff
            and str(row.get("side") or "") in teams
        ),
        key=lambda row: (
            str(row.get("submitted_at_utc") or ""),
            str(row.get("submission_id") or ""),
        ),
    )
    return matching


def _submission_summary(row: dict[str, Any], *, market: str) -> dict[str, Any]:
    terms = (
        _selected_spread_terms(row)
        if market == "spread"
        else submission_terms(row)
    )
    return {
        "submission_reference": _opaque_reference(
            row.get("submission_id"),
            row.get("submitted_at_utc"),
        ),
        "submitted_at_utc": str(row["submitted_at_utc"]),
        "selected_market": market,
        "selected_side": str(row["side"]),
        "selected_line": terms["line"],
        "selected_price": terms["price"],
        "terms_source": terms["source"],
        "rationale": str(row.get("lean_text") or ""),
    }


def _decision_history(
    submissions: list[dict[str, Any]],
    *,
    market: str,
) -> dict[str, Any] | None:
    if not submissions:
        return None
    summaries = [
        _submission_summary(row, market=market) for row in submissions
    ]
    changes = sum(
        previous["selected_side"] != current["selected_side"]
        for previous, current in zip(summaries, summaries[1:])
    )
    reaffirmations = len(summaries) - 1 - changes
    if len(summaries) == 1:
        pattern = "initial_only"
    elif changes:
        pattern = "changed"
    else:
        pattern = "reaffirmed"
    return {
        "decision_pattern": pattern,
        "submission_count": len(summaries),
        "change_count": changes,
        "reaffirmation_count": reaffirmations,
        "initial_side": summaries[0]["selected_side"],
        "final_side": summaries[-1]["selected_side"],
        "submissions": summaries,
    }


def _market_relationship(
    *,
    game: dict[str, Any],
    moneyline: dict[str, Any],
    spread: dict[str, Any] | None,
) -> dict[str, Any]:
    moneyline_side = str(moneyline["side"])
    if spread is None:
        return {
            "status": "moneyline_only",
            "moneyline_side": moneyline_side,
            "spread_side": None,
            "selected_spread_line": None,
        }
    spread_side = str(spread["side"])
    selected_spread_line = _selected_spread_terms(spread)["line"]
    if selected_spread_line is None:
        raise ValueError("Cee spread submission has no spread line")
    if spread_side == moneyline_side:
        status = "same_side"
    elif float(selected_spread_line) > 0:
        status = "split_compatible"
    else:
        status = "split_conflicting"
    return {
        "status": status,
        "moneyline_side": moneyline_side,
        "spread_side": spread_side,
        "selected_spread_line": selected_spread_line,
    }


def build_cee_input(
    game: dict[str, Any],
    history: list[dict[str, Any]],
    leans: list[dict[str, Any]],
    win_predictions: list[dict[str, Any]],
    *,
    cee_user_id: str,
) -> dict[str, Any]:
    """Build a whitelisted Cee Expert input for one game."""
    moneyline_submissions = _eligible_submissions(
        leans,
        game=game,
        user_id=cee_user_id,
        market="moneyline",
    )
    if not moneyline_submissions:
        raise ValueError("Cee has no full-game moneyline pick for this event")
    moneyline = moneyline_submissions[-1]
    spread_submissions = _eligible_submissions(
        leans,
        game=game,
        user_id=cee_user_id,
        market="spread",
    )
    spread = spread_submissions[-1] if spread_submissions else None
    moneyline_decision = _decision_history(
        moneyline_submissions,
        market="moneyline",
    )
    assert moneyline_decision is not None
    spread_decision = _decision_history(
        spread_submissions,
        market="spread",
    )
    season = _season_context(
        moneyline,
        win_predictions,
        user_id=cee_user_id,
    )
    if season is None:
        raise ValueError(
            "Cee Expert requires season-win predictions for both teams "
            "submitted before the game pick"
        )
    spread_season = (
        _season_context(spread, win_predictions, user_id=cee_user_id)
        if spread is not None
        else None
    )
    if spread is not None and spread_season is None:
        spread_season = {
            "status": "unavailable_before_submission",
        }
    calibration = _calibration(
        current=moneyline,
        current_season=season,
        leans=leans,
        predictions=win_predictions,
        history=history,
        user_id=cee_user_id,
        current_decision_pattern=str(
            moneyline_decision["decision_pattern"]
        ),
        current_spread=spread,
        current_spread_decision_pattern=(
            str(spread_decision["decision_pattern"])
            if spread_decision is not None
            else None
        ),
    )
    moneyline_market = _submission_market(moneyline)
    spread_market = _submission_market(spread) if spread is not None else None
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
        "cee_submissions": {
            "moneyline": _submission_summary(
                moneyline,
                market="moneyline",
            ),
            "spread": (
                _submission_summary(spread, market="spread")
                if spread is not None
                else None
            ),
        },
        "decision_history": {
            "moneyline": moneyline_decision,
            "spread": spread_decision,
        },
        "season_predictions_at_submission": season,
        "spread_season_predictions_at_submission": spread_season,
        "market_relationship": _market_relationship(
            game=game,
            moneyline=moneyline,
            spread=spread,
        ),
        "submission_markets": {
            "moneyline": moneyline_market,
            "spread": spread_market,
        },
        "nfl_calibration": calibration,
    }
