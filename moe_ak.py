"""Deterministic input construction for the NFL AK calibration expert."""

from __future__ import annotations

import hashlib
import json
import math
import re
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

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
    decode_packed_markets,
)
from nfl_win_predictions import TEAM_ABBREVIATIONS

ROOT = Path(__file__).resolve().parent
WNBA_PRIOR_PATH = ROOT / "moe" / "priors" / "ak_wnba_v1.json"


def _parse_time(value: Any) -> datetime:
    return datetime.fromisoformat(str(value).replace("Z", "+00:00")).astimezone(
        timezone.utc
    )


def _aliases(team: str) -> set[str]:
    words = team.lower().split()
    return {
        team.lower(),
        TEAM_ABBREVIATIONS.get(team, "").lower(),
        words[-1],
    } - {""}


def parse_ak_projection(
    text: str,
    *,
    away_team: str,
    home_team: str,
) -> dict[str, Any]:
    """Parse only score formats whose team-to-score mapping is provable."""
    normalized = " ".join(str(text).split())

    labeled_scores: dict[str, int] = {}
    for team in (away_team, home_team):
        scores: set[int] = set()
        for alias in sorted(_aliases(team), key=len, reverse=True):
            scores.update(
                int(match.group(1))
                for match in re.finditer(
                    rf"\b{re.escape(alias)}\b\s*[:=-]?\s*"
                    rf"(\d{{1,2}})(?!\.\d)\b",
                    normalized,
                    re.IGNORECASE,
                )
            )
        if len(scores) > 1:
            return {
                "status": "conflicting",
                "reason": f"Multiple scores found for {team}",
            }
        if scores:
            labeled_scores[team] = next(iter(scores))
    if set(labeled_scores) == {away_team, home_team}:
        away_score = labeled_scores[away_team]
        home_score = labeled_scores[home_team]
        if away_score == home_score:
            return {
                "status": "ambiguous",
                "reason": "Tied projections are unsupported",
            }
        return {
            "status": "parsed",
            "away_score": away_score,
            "home_score": home_score,
            "winner": away_team if away_score > home_score else home_team,
            "loser": home_team if away_score > home_score else away_team,
            "reason": "Mapped from explicit team-labeled scores",
        }

    pair_pattern = re.compile(
        r"(?<![\d.])(\d{1,2})\s*[-–:]\s*(\d{1,2})(?!\d|\.\d)"
    )
    pairs = list(pair_pattern.finditer(normalized))
    if not pairs:
        return {"status": "missing", "reason": "No integer score pair found"}
    unique_pairs = {(int(match.group(1)), int(match.group(2))) for match in pairs}
    if len(unique_pairs) != 1:
        return {
            "status": "conflicting",
            "reason": "Multiple incompatible score pairs found",
        }
    first_score, second_score = next(iter(unique_pairs))
    if first_score == second_score:
        return {"status": "ambiguous", "reason": "Tied projections are unsupported"}

    match = pairs[-1]
    prefix = normalized[: match.start()].lower()
    candidates = [
        team
        for team in (away_team, home_team)
        if any(
            re.search(
                rf"\b{re.escape(alias)}\b(?:\s+team)?\s+to\s+win\s*$",
                prefix,
                re.IGNORECASE,
            )
            for alias in _aliases(team)
        )
    ]
    if len(candidates) == 1:
            winner = candidates[0]
            loser = home_team if winner == away_team else away_team
            if first_score < second_score:
                return {
                    "status": "conflicting",
                    "reason": "Named winner has the lower projected score",
                }
            return {
                "status": "parsed",
                "away_score": first_score if winner == away_team else second_score,
                "home_score": first_score if winner == home_team else second_score,
                "winner": winner,
                "loser": loser,
                "reason": "Mapped from an explicit '<team> to win A-B' statement",
            }

    return {
        "status": "ambiguous",
        "reason": "Score pair exists but away/home ownership is not provable",
    }


def _projection_from_row(row: dict[str, Any]) -> dict[str, Any]:
    status = str(row.get("prediction_parse_status") or "").strip()
    if status == "parsed":
        try:
            away_score = int(row["predicted_away_score"])
            home_score = int(row["predicted_home_score"])
        except (KeyError, TypeError, ValueError):
            return {
                "status": "conflicting",
                "reason": "Normalized parsed status has invalid score columns",
            }
        if away_score < 0 or home_score < 0 or away_score == home_score:
            return {
                "status": "conflicting",
                "reason": "Normalized score columns are invalid",
            }
        away_team = str(row["away_team"])
        home_team = str(row["home_team"])
        return {
            "status": "parsed",
            "away_score": away_score,
            "home_score": home_score,
            "winner": away_team if away_score > home_score else home_team,
            "loser": home_team if away_score > home_score else away_team,
            "reason": "Loaded reviewed normalized score columns",
        }
    if status:
        return {
            "status": status,
            "reason": "Reviewed normalization status excludes this row",
        }
    return parse_ak_projection(
        str(row.get("lean_text") or ""),
        away_team=str(row.get("away_team")),
        home_team=str(row.get("home_team")),
    )


def _market_from_packed(
    row: dict[str, Any],
    *,
    prefix: str,
) -> dict[str, float | int | None]:
    if prefix == "opening":
        columns = (
            OPENING_AWAY_COLUMN,
            OPENING_HOME_COLUMN,
            OPENING_TOTALS_COLUMN,
        )
    else:
        columns = (
            LATEST_AWAY_COLUMN,
            LATEST_HOME_COLUMN,
            LATEST_TOTALS_COLUMN,
        )
    return decode_packed_markets(
        str(row.get(columns[0]) or ""),
        str(row.get(columns[1]) or ""),
        str(row.get(columns[2]) or ""),
    )["game"]


def _favorite(
    market: dict[str, float | int | None],
    away_team: str,
    home_team: str,
) -> tuple[str | None, float | None, str]:
    away_spread = market.get("away_spread")
    home_spread = market.get("home_spread")
    if away_spread is None or home_spread is None:
        return None, None, "missing"
    if float(away_spread) < 0:
        return away_team, abs(float(away_spread)), "favorite"
    if float(home_spread) < 0:
        return home_team, abs(float(home_spread)), "favorite"
    return None, 0.0, "pickem"


def _total_gap_bucket(gap: float) -> str:
    if gap <= -7:
        return "minus_7_or_less"
    if gap <= -3:
        return "minus_7_to_3"
    if gap < 0:
        return "minus_3_to_0"
    if gap < 3:
        return "plus_0_to_3"
    if gap < 9:
        return "plus_3_to_9"
    if gap < 12:
        return "plus_9_to_12"
    return "plus_12_or_more"


def _wnba_total_prior(
    total_gap: float,
    prior: dict[str, Any],
) -> tuple[str, str]:
    if total_gap < 0:
        return (
            "negative_unmapped",
            "WNBA cross-sport prior: the revised NFL-to-WNBA mapping does "
            "not assign negative total gaps to a directional band.",
        )
    if total_gap < 3:
        return (
            "wnba_0_to_6",
            "WNBA cross-sport prior: the NFL 0 to <3 band maps to WNBA 0 to "
            "<6; the available fresh WNBA sample finished under in 3 of 4 "
            "games.",
        )
    if total_gap < 9:
        return (
            "wnba_6_to_12",
            "WNBA cross-sport prior: the NFL 3 to <9 band maps to WNBA 6 to "
            "<12; the full WNBA sample finished under in 7 of 10 games.",
        )
    if total_gap < 12:
        return (
            "wnba_12_to_16",
            "WNBA cross-sport prior: the NFL 9 to <12 band maps to WNBA 12 "
            "to <16; the fresh WNBA sample finished under in 2 of 2 games, "
            "while the earlier 12+ sample is not separable from 16+.",
        )
    return (
        "wnba_16_or_more",
        "WNBA cross-sport prior: the NFL 12+ band maps to WNBA 16+; no "
        "isolated WNBA 16+ record is available.",
    )


def _side_gap_bucket(
    ak_favorite: str,
    market_favorite: str | None,
    gap: float | None,
    market_state: str,
) -> str:
    if market_state == "missing":
        return "no_market"
    if market_state == "pickem":
        return "pickem"
    if market_favorite != ak_favorite:
        return "favorite_flip"
    assert gap is not None
    if gap < -3:
        return "laid_more_than_3_less"
    if gap < -1:
        return "laid_1_to_3_less"
    if gap <= 1:
        return "within_1"
    if gap <= 3:
        return "laid_1_to_3_more"
    return "laid_more_than_3_more"


def _record(results: Iterable[str]) -> dict[str, Any]:
    values = list(results)
    counts = Counter(values)
    return {
        "wins": counts["W"],
        "losses": counts["L"],
        "pushes": counts["P"],
        "games": len(values),
        "chronological_results": "".join(values),
    }


def _market_from_snapshot(row: dict[str, Any]) -> dict[str, Any]:
    return decode_packed_markets(
        str(row.get(AWAY_SNAPSHOT_COLUMN) or ""),
        str(row.get(HOME_SNAPSHOT_COLUMN) or ""),
        str(row.get(TOTALS_SNAPSHOT_COLUMN) or ""),
    )["game"]


def _closing_market(
    lean: dict[str, Any],
    snapshots: Iterable[dict[str, Any]],
) -> dict[str, Any] | None:
    kickoff = _parse_time(lean["commence_time_utc"])
    eligible = [
        row
        for row in snapshots
        if str(row.get("event_id")) == str(lean.get("event_id"))
        and str(row.get("bookmaker")) == str(lean.get("bookmaker"))
        and _parse_time(row["captured_at"]) < kickoff
    ]
    if not eligible:
        return None
    return _market_from_snapshot(
        max(eligible, key=lambda row: _parse_time(row["captured_at"]))
    )


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


def _grade_side(
    *,
    projected_winner: str,
    away_team: str,
    home_team: str,
    market: dict[str, Any],
    result: dict[str, Any],
) -> str | None:
    line = (
        market.get("away_spread")
        if projected_winner == away_team
        else market.get("home_spread")
    )
    if line is None:
        return None
    actual_margin = (
        int(result["away_score"]) - int(result["home_score"])
        if projected_winner == away_team
        else int(result["home_score"]) - int(result["away_score"])
    )
    settled = actual_margin + float(line)
    return "W" if settled > 0 else "L" if settled < 0 else "P"


def _grade_total(market: dict[str, Any], result: dict[str, Any]) -> str | None:
    total = market.get("total")
    if total is None:
        return None
    actual = int(result["away_score"]) + int(result["home_score"])
    return "O" if actual > float(total) else "U" if actual < float(total) else "P"


def _historical_calibration(
    *,
    current_side_bucket: str,
    current_total_bucket: str,
    leans: Iterable[dict[str, Any]],
    history: Iterable[dict[str, Any]],
    snapshots: Iterable[dict[str, Any]],
    ak_user_id: str,
    current_event_id: str,
) -> dict[str, Any]:
    side_submission_results: list[str] = []
    side_closing_results: list[str] = []
    total_submission_results: list[str] = []
    total_closing_results: list[str] = []
    eligible = 0
    side_eligible = 0
    total_eligible = 0
    excluded = Counter()
    for lean in sorted(leans, key=lambda row: str(row.get("submitted_at_utc") or "")):
        if str(lean.get("telegram_user_id")) != str(ak_user_id):
            continue
        if str(lean.get("event_id")) == current_event_id:
            continue
        parsed = _projection_from_row(lean)
        if parsed["status"] != "parsed":
            excluded[f"projection_{parsed['status']}"] += 1
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
        market = _market_from_packed(lean, prefix="latest")
        market_favorite, spread, market_state = _favorite(
            market,
            str(lean["away_team"]),
            str(lean["home_team"]),
        )
        predicted_margin = abs(parsed["away_score"] - parsed["home_score"])
        side_gap = (
            predicted_margin - float(spread)
            if market_favorite == parsed["winner"] and spread is not None
            else None
        )
        side_bucket = _side_gap_bucket(
            parsed["winner"], market_favorite, side_gap, market_state
        )
        close = _closing_market(lean, snapshots)
        eligible += 1
        if market_state == "missing":
            excluded["side_missing_submission_spread"] += 1
        else:
            side_eligible += 1
        if market_state != "missing" and side_bucket == current_side_bucket:
            outcome = _grade_side(
                projected_winner=parsed["winner"],
                away_team=str(lean["away_team"]),
                home_team=str(lean["home_team"]),
                market=market,
                result=result,
            )
            if outcome:
                side_submission_results.append(outcome)
            if close:
                outcome = _grade_side(
                    projected_winner=parsed["winner"],
                    away_team=str(lean["away_team"]),
                    home_team=str(lean["home_team"]),
                    market=close,
                    result=result,
                )
                if outcome:
                    side_closing_results.append(outcome)
        total = market.get("total")
        if total is None:
            excluded["total_missing_submission_total"] += 1
            continue
        total_eligible += 1
        total_gap = parsed["away_score"] + parsed["home_score"] - float(total)
        total_bucket = _total_gap_bucket(total_gap)
        if total_bucket == current_total_bucket:
            outcome = _grade_total(market, result)
            if outcome:
                total_submission_results.append(outcome)
            if close:
                outcome = _grade_total(close, result)
                if outcome:
                    total_closing_results.append(outcome)
    return {
        "eligible_predictions": eligible,
        "side_eligible_predictions": side_eligible,
        "total_eligible_predictions": total_eligible,
        "excluded_counts": dict(sorted(excluded.items())),
        "matching_side_bucket": {
            "bucket": current_side_bucket,
            "ak_side_ats_at_submission": _record(
                side_submission_results
            ),
            "ak_side_ats_at_close": _record(side_closing_results),
        },
        "matching_total_bucket": {
            "bucket": current_total_bucket,
            "actual_total_results_at_submission": {
                "overs": total_submission_results.count("O"),
                "unders": total_submission_results.count("U"),
                "pushes": total_submission_results.count("P"),
                "games": len(total_submission_results),
                "chronological_results": "".join(total_submission_results),
            },
            "actual_total_results_at_close": {
                "overs": total_closing_results.count("O"),
                "unders": total_closing_results.count("U"),
                "pushes": total_closing_results.count("P"),
                "games": len(total_closing_results),
                "chronological_results": "".join(total_closing_results),
            },
        },
    }


def load_wnba_prior() -> dict[str, Any]:
    return json.loads(WNBA_PRIOR_PATH.read_text(encoding="utf-8"))


def build_ak_input(
    game: dict[str, Any],
    history: list[dict[str, Any]],
    leans: list[dict[str, Any]],
    snapshots: list[dict[str, Any]] | None = None,
    *,
    ak_user_id: str,
) -> dict[str, Any]:
    matching = [
        row
        for row in leans
        if str(row.get("telegram_user_id")) == str(ak_user_id)
        and str(row.get("event_id")) == str(game["event_id"])
        and str(row.get("period")) == "game"
    ]
    if not matching:
        raise ValueError("AK has no full-game prediction for this event")
    current = max(matching, key=lambda row: str(row.get("submitted_at_utc") or ""))
    if _parse_time(current["submitted_at_utc"]) >= _parse_time(
        game["commence_time_utc"]
    ):
        raise ValueError("AK prediction was submitted after kickoff")
    projection = _projection_from_row(current)
    if projection["status"] != "parsed":
        raise ValueError(
            f"AK projected score is {projection['status']}: {projection['reason']}"
        )
    submission_market = _market_from_packed(current, prefix="latest")
    current_market = _market_from_packed(game, prefix="latest")
    opening_market = _market_from_packed(game, prefix="opening")
    market_favorite, spread, market_state = _favorite(
        submission_market,
        str(game["away_team"]),
        str(game["home_team"]),
    )
    predicted_margin = abs(projection["away_score"] - projection["home_score"])
    side_gap = (
        predicted_margin - float(spread)
        if market_favorite == projection["winner"] and spread is not None
        else None
    )
    predicted_total = projection["away_score"] + projection["home_score"]
    market_total = submission_market.get("total")
    if market_total is None:
        raise ValueError("AK submission snapshot has no full-game total")
    total_gap = predicted_total - float(market_total)
    side_bucket = _side_gap_bucket(
        projection["winner"], market_favorite, side_gap, market_state
    )
    total_bucket = _total_gap_bucket(total_gap)
    calibration = _historical_calibration(
        current_side_bucket=side_bucket,
        current_total_bucket=total_bucket,
        leans=leans,
        history=history,
        snapshots=snapshots or [],
        ak_user_id=ak_user_id,
        current_event_id=str(game["event_id"]),
    )
    prior = load_wnba_prior()
    max_equivalent = float(prior["max_equivalent_nfl_observations"])
    expires = int(prior["expires_at_nfl_bucket_size"])
    side_nfl_games = calibration["matching_side_bucket"][
        "ak_side_ats_at_submission"
    ]["games"]
    total_nfl_games = calibration["matching_total_bucket"][
        "actual_total_results_at_submission"
    ]["games"]

    def prior_weight(games: int) -> float:
        return round(
            max_equivalent
            * max(0.0, 1 - min(games, expires) / expires),
            2,
        )

    side_prior_weight = prior_weight(side_nfl_games)
    total_prior_weight = prior_weight(total_nfl_games)
    total_prior_band, total_prior = _wnba_total_prior(
        total_gap,
        prior,
    )
    total_prior_supported = total_prior_band in {
        "wnba_0_to_6",
        "wnba_6_to_12",
        "wnba_12_to_16",
    }
    total_gap_fraction = total_gap / float(market_total)

    home_spread = submission_market.get("home_spread")
    implied = {"away": None, "home": None}
    if home_spread is not None:
        implied = {
            "away": round((float(market_total) + float(home_spread)) / 2, 2),
            "home": round((float(market_total) - float(home_spread)) / 2, 2),
        }
    market_gaps = {
        "ak_favorite": projection["winner"],
        "market_favorite": market_favorite,
        "favorite_flip": market_favorite != projection["winner"],
        "ak_predicted_margin": predicted_margin,
        "market_spread_magnitude": spread,
        "side_margin_gap": side_gap,
        "side_gap_bucket": side_bucket,
        "ak_predicted_total": predicted_total,
        "market_total": market_total,
        "total_gap": round(total_gap, 2),
        "total_gap_fraction": round(total_gap_fraction, 4),
        "total_gap_bucket": total_bucket,
        "market_implied_away_score": implied["away"],
        "market_implied_home_score": implied["home"],
        "away_team_total_gap": (
            round(projection["away_score"] - implied["away"], 2)
            if implied["away"] is not None
            else None
        ),
        "home_team_total_gap": (
            round(projection["home_score"] - implied["home"], 2)
            if implied["home"] is not None
            else None
        ),
    }
    side_prior = {
        "within_1": (
            "WNBA cross-sport prior: when AK stayed within one point of the "
            "market, AK's projected side covered 4 of 6 games."
        ),
        "favorite_flip": (
            "WNBA cross-sport prior: AK's projected side covered 0 of 2 "
            "recent favorite-flip games; earlier counterexamples make this a "
            "warning only."
        ),
        "laid_1_to_3_more": (
            "WNBA cross-sport prior: laying more than the market was venue "
            "dependent, with road favorites covering 5 of 6 and home "
            "favorites covering 2 of 5."
        ),
        "laid_more_than_3_more": (
            "WNBA cross-sport prior: laying more than the market was venue "
            "dependent, with road favorites covering 5 of 6 and home "
            "favorites covering 2 of 5."
        ),
        "laid_1_to_3_less": (
            "WNBA cross-sport prior: AK sides laying materially less than the "
            "market covered 2 of 4 games."
        ),
        "laid_more_than_3_less": (
            "WNBA cross-sport prior: AK sides laying materially less than the "
            "market covered 2 of 4 games."
        ),
        "pickem": (
            "WNBA cross-sport prior: no reviewed side bucket directly "
            "matches a pick'em market."
        ),
        "no_market": (
            "WNBA cross-sport prior: no side calibration is available without "
            "a supplied spread."
        ),
    }[side_bucket]
    evidence_catalog = [
        {
            "id": "ak_projection",
            "scope": "both",
            "text": (
                f"AK projects {game['away_team']} {projection['away_score']}, "
                f"{game['home_team']} {projection['home_score']}."
            ),
        },
        {
            "id": "submission_market",
            "scope": "both",
            "text": (
                f"At submission, {market_favorite or 'neither team'} was the "
                f"market favorite by {spread if spread is not None else 'no'} "
                f"points and the total was {float(market_total):g}."
            ),
        },
        {
            "id": "side_gap",
            "scope": "side",
            "text": (
                f"AK's side-gap bucket is {side_bucket}; side margin gap is "
                f"{side_gap:+.1f} points."
                if side_gap is not None
                else f"AK and the market disagree on the favorite ({side_bucket})."
            ),
        },
        {
            "id": "total_gap",
            "scope": "total",
            "text": (
                f"AK projects {predicted_total} total points, "
                f"{total_gap:+.1f} versus the submission total; bucket "
                f"{total_bucket}."
            ),
        },
        {
            "id": "nfl_side_bucket",
            "scope": "side",
            "text": (
                f"Matching NFL side bucket has "
                f"{calibration['matching_side_bucket']['ak_side_ats_at_submission']['games']} "
                "resolved AK predictions."
            ),
        },
        {
            "id": "nfl_total_bucket",
            "scope": "total",
            "text": (
                f"Matching NFL total bucket has "
                f"{calibration['matching_total_bucket']['actual_total_results_at_submission']['games']} "
                "resolved AK predictions."
            ),
        },
        {
            "id": "wnba_prior_cap",
            "scope": "both",
            "text": (
                "WNBA calibration is a cross-sport prior capped at "
                f"{side_prior_weight:g} side and {total_prior_weight:g} total "
                "equivalent NFL observations; it cannot add more than one "
                "confidence star."
            ),
        },
        {
            "id": "wnba_side_prior",
            "scope": "side",
            "text": side_prior,
        },
        {
            "id": "wnba_total_prior",
            "scope": "total",
            "text": total_prior,
            "supporting_allowed": total_prior_supported,
        },
    ]
    return {
        "input_profile": "ak_calibration",
        "game": {
            "event_id": str(game["event_id"]),
            "season": int(game["season"]),
            "week": int(game["week"]) if str(game.get("week") or "") else None,
            "commence_time_utc": str(game["commence_time_utc"]),
            "away_team": str(game["away_team"]),
            "home_team": str(game["home_team"]),
        },
        "ak_submission": {
            "submission_ref": hashlib.sha256(
                str(current["submission_id"]).encode("utf-8")
            ).hexdigest()[:16],
            "submitted_at_utc": str(current["submitted_at_utc"]),
            "selected_market": str(current["market"]),
            "selected_side": str(current["side"]),
            "rationale": str(current["lean_text"]),
            "projection": projection,
        },
        "markets": {
            "opening": opening_market,
            "submission": submission_market,
            "current_latest": current_market,
        },
        "market_gaps": market_gaps,
        "nfl_calibration": calibration,
        "cross_sport_prior": {
            "schema_version": prior["schema_version"],
            "source": prior["source"],
            "max_equivalent_nfl_observations": prior[
                "max_equivalent_nfl_observations"
            ],
            "expires_at_nfl_bucket_size": prior[
                "expires_at_nfl_bucket_size"
            ],
            "max_confidence_star_adjustment": prior[
                "max_confidence_star_adjustment"
            ],
            "applicability": prior["applicability"],
            "side_gap": prior["side_gap"],
            "nfl_total_gap_mapping": prior["nfl_total_gap_mapping"],
            "label": "cross_sport_prior",
            "side": {
                "matching_nfl_bucket_games": side_nfl_games,
                "effective_equivalent_nfl_observations": side_prior_weight,
            },
            "total": {
                "matching_nfl_bucket_games": total_nfl_games,
                "effective_equivalent_nfl_observations": total_prior_weight,
                "wnba_mapping_band": total_prior_band,
                "interpretation": (
                    "under_warning"
                    if total_prior_supported
                    else "no_direction"
                ),
            },
        },
        "evidence_catalog": evidence_catalog,
    }
