"""Two-phase Pikkit Expert input builder and output normalizer."""

from __future__ import annotations

import json
import math
from datetime import datetime, timezone
from typing import Any, Iterable

from nfl_pikkit import (
    analyze_snapshot,
    final_snapshot,
    first_snapshot,
    line_snapshot_market,
    latest_line_snapshot_at_or_before,
    parse_time,
    snapshot_movement,
    snapshots_for_event,
)

PIKKIT_PROFILE = "pikkit_splits"
INITIAL_PHASE = "initial"
FINAL_PHASE = "final_t_minus_2h"
PHASES = (INITIAL_PHASE, FINAL_PHASE)
WATCH_STATUSES = ("triggered", "not_triggered", "invalidated", "unobservable")


def _parsed_object(value: Any, field: str) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    if value in (None, ""):
        return {}
    try:
        parsed = json.loads(str(value))
    except json.JSONDecodeError as exc:
        raise ValueError(f"Invalid {field}") from exc
    if not isinstance(parsed, dict):
        raise ValueError(f"{field} must be an object")
    return parsed


def opinion_phase(row: dict[str, Any]) -> str:
    summary = _parsed_object(row.get("calibration_summary_json"), "summary")
    return str(summary.get("generation_phase") or "")


def opinion_phase_identity(row: dict[str, Any]) -> tuple[str, str, str]:
    """Phase identity from a valid row or its persisted prebuilt input."""
    summary = _parsed_object(row.get("calibration_summary_json"), "summary")
    if summary:
        return (
            str(summary.get("generation_phase") or ""),
            str(summary.get("selected_snapshot_id") or ""),
            str(summary.get("prior_opinion_id") or ""),
        )
    payload = _parsed_object(row.get("input_json"), "input_json")
    return (
        str(payload.get("generation_phase") or ""),
        str((payload.get("selected_snapshot") or {}).get("snapshot_id") or ""),
        str((payload.get("initial_opinion") or {}).get("opinion_id") or ""),
    )


def _initial_watch(row: dict[str, Any]) -> list[dict[str, Any]]:
    summary = _parsed_object(row.get("calibration_summary_json"), "summary")
    watch = summary.get("movement_watch") or []
    if not isinstance(watch, list):
        raise ValueError("Initial Pikkit movement watch must be a list")
    return [dict(item) for item in watch if isinstance(item, dict)]


def _line_for_snapshot(
    line_rows: Iterable[dict[str, Any]],
    snapshot: dict[str, Any],
) -> dict[str, Any]:
    required = (
        "home_moneyline",
        "away_moneyline",
        "home_spread",
        "total",
    )
    line = latest_line_snapshot_at_or_before(
        line_rows,
        str(snapshot["nfl_event_id"]),
        parse_time(snapshot["captured_at_utc"]),
    )
    if line is not None:
        market = line_snapshot_market(line)
        if all(market.get(field) not in (None, "") for field in required):
            return line
    candidates = sorted(
        (
            dict(row)
            for row in line_rows
            if str(row.get("event_id") or "")
            == str(snapshot["nfl_event_id"])
            and row.get("captured_at")
            and parse_time(row["captured_at"])
            <= parse_time(snapshot["captured_at_utc"])
        ),
        key=lambda row: str(row.get("captured_at") or ""),
        reverse=True,
    )
    for candidate in candidates:
        market = line_snapshot_market(candidate)
        if all(market.get(field) not in (None, "") for field in required):
            return candidate
    if line is None:
        raise ValueError(
            f"No BetOnline snapshot exists before Pikkit snapshot "
            f"{snapshot['snapshot_id']}"
        )
    raise ValueError(
        f"No complete BetOnline baseline exists before Pikkit snapshot "
        f"{snapshot['snapshot_id']}"
    )


def _matching_final(
    event: dict[str, Any],
    finals: Iterable[dict[str, Any]],
) -> dict[str, Any] | None:
    kickoff = parse_time(
        event.get("commence_time_utc") or event.get("kickoff_utc")
    )
    away = str(event.get("away_team") or "")
    home = str(event.get("home_team") or "")
    candidates = [
        row
        for row in finals
        if str(row.get("away_team") or "") == away
        and str(row.get("home_team") or "") == home
        and abs(
            (
                parse_time(
                    row.get("kickoff_utc") or row.get("commence_time_utc")
                )
                - kickoff
            ).total_seconds()
        )
        <= 6 * 3600
    ]
    return max(
        candidates,
        key=lambda row: str(
            row.get("kickoff_utc") or row.get("commence_time_utc") or ""
        ),
        default=None,
    )


def _metric(values: list[float]) -> dict[str, Any]:
    if not values:
        return {"n": 0, "mean": None}
    return {"n": len(values), "mean": round(sum(values) / len(values), 6)}


def _actual_market_outcomes(
    analysis: dict[str, Any],
    final: dict[str, Any],
) -> dict[str, str]:
    away_score = float(final["away_score"])
    home_score = float(final["home_score"])
    margin = home_score - away_score
    total_points = home_score + away_score
    betonline = analysis["betonline"]
    spread_result = margin + float(betonline["home_spread"])
    total_result = total_points - float(betonline["total"])
    return {
        "moneyline": (
            "home_win"
            if margin > 0
            else "away_win"
            if margin < 0
            else "push"
        ),
        "spread": (
            "home_cover"
            if spread_result > 0
            else "away_cover"
            if spread_result < 0
            else "push"
        ),
        "total": (
            "over"
            if total_result > 0
            else "under"
            if total_result < 0
            else "push"
        ),
    }


def build_historical_calibration(
    *,
    snapshot_rows: Iterable[dict[str, Any]],
    line_rows: Iterable[dict[str, Any]],
    opinion_rows: Iterable[dict[str, Any]],
    finals: Iterable[dict[str, Any]],
    as_of: datetime,
) -> dict[str, Any]:
    """Build time-safe Pikkit records using only games resolved before ``as_of``."""
    snapshots = list(snapshot_rows)
    lines = list(line_rows)
    opinions = list(opinion_rows)
    finals = list(finals)
    event_ids = sorted(
        {
            str(row.get("nfl_event_id") or "")
            for row in snapshots
            if str(row.get("capture_kind") or "") == FINAL_PHASE
            and parse_time(row["commence_time_utc"]) < as_of
        }
        - {""}
    )
    market_records = {
        market: {
            "resolved": 0,
            "sportsbook_preferred_wins": 0,
            "lower_handle_proxy_wins": 0,
            "pushes": 0,
            "majority_disagreement": {"resolved": 0, "preferred_wins": 0},
        }
        for market in ("moneyline", "spread", "total")
    }
    phase_errors = {
        phase: {"brier": [], "margin_absolute_error": [], "total_absolute_error": []}
        for phase in PHASES
    }
    baseline_errors = {
        "brier": [],
        "margin_absolute_error": [],
        "total_absolute_error": [],
    }
    resolved_games = 0
    for event_id in event_ids:
        snapshot = final_snapshot(snapshots, event_id)
        if snapshot is None:
            continue
        event = {
            "away_team": snapshot["away_team"],
            "home_team": snapshot["home_team"],
            "commence_time_utc": snapshot["commence_time_utc"],
        }
        final = _matching_final(event, finals)
        if final is None or parse_time(
            final.get("kickoff_utc") or final.get("commence_time_utc")
        ) >= as_of:
            continue
        try:
            analysis = analyze_snapshot(
                snapshot, _line_for_snapshot(lines, snapshot)
            )
        except ValueError:
            continue
        actual = _actual_market_outcomes(analysis, final)
        resolved_games += 1
        home_won = (
            1.0
            if float(final["home_score"]) > float(final["away_score"])
            else 0.0
            if float(final["home_score"]) < float(final["away_score"])
            else 0.5
        )
        actual_margin = float(final["home_score"]) - float(final["away_score"])
        actual_total = float(final["home_score"]) + float(final["away_score"])
        baseline = analysis["market_baseline"]
        baseline_errors["brier"].append(
            (float(baseline["home_win_probability"]) - home_won) ** 2
        )
        baseline_errors["margin_absolute_error"].append(
            abs(float(baseline["expected_home_margin"]) - actual_margin)
        )
        baseline_errors["total_absolute_error"].append(
            abs(float(baseline["projected_total"]) - actual_total)
        )
        for market, market_analysis in analysis["markets"].items():
            sportsbook = market_analysis.get("sportsbook")
            if not sportsbook or actual[market] == "push":
                if actual[market] == "push":
                    market_records[market]["pushes"] += 1
                continue
            record = market_records[market]
            record["resolved"] += 1
            preferred_win = actual[market] == sportsbook["best_outcome"]
            proxy_win = actual[market] == sportsbook["lower_handle_proxy"]
            record["sportsbook_preferred_wins"] += int(preferred_win)
            record["lower_handle_proxy_wins"] += int(proxy_win)
            if not market_analysis["majorities_agree"]:
                cohort = record["majority_disagreement"]
                cohort["resolved"] += 1
                cohort["preferred_wins"] += int(preferred_win)
        for phase in PHASES:
            candidates = [
                row
                for row in opinions
                if str(row.get("event_id") or "") == event_id
                and str(row.get("expert_id") or "") == "pikkit"
                and str(row.get("generation_status") or "") == "valid"
                and str(row.get("review_status") or "") == "approved"
                and opinion_phase(row) == phase
                and parse_time(row["generated_at_utc"]) < as_of
            ]
            if not candidates:
                continue
            row = max(
                candidates,
                key=lambda item: (
                    str(item.get("generated_at_utc") or ""),
                    str(item.get("opinion_id") or ""),
                ),
            )
            errors = phase_errors[phase]
            errors["brier"].append(
                (float(row["home_win_probability"]) - home_won) ** 2
            )
            errors["margin_absolute_error"].append(
                abs(float(row["expected_home_margin"]) - actual_margin)
            )
            projected_total = float(row["predicted_away_score"]) + float(
                row["predicted_home_score"]
            )
            errors["total_absolute_error"].append(
                abs(projected_total - actual_total)
            )
    for record in market_records.values():
        resolved = record["resolved"]
        record["sportsbook_preferred_rate"] = (
            round(record["sportsbook_preferred_wins"] / resolved, 6)
            if resolved
            else None
        )
        record["lower_handle_proxy_rate"] = (
            round(record["lower_handle_proxy_wins"] / resolved, 6)
            if resolved
            else None
        )
        cohort = record["majority_disagreement"]
        cohort["preferred_rate"] = (
            round(cohort["preferred_wins"] / cohort["resolved"], 6)
            if cohort["resolved"]
            else None
        )
    return {
        "as_of_utc": as_of.isoformat(),
        "resolved_games": resolved_games,
        "status": "available" if resolved_games else "insufficient_sample",
        "markets": market_records,
        "prediction_accuracy": {
            phase: {name: _metric(values) for name, values in metrics.items()}
            for phase, metrics in phase_errors.items()
        },
        "market_baseline_accuracy": {
            name: _metric(values) for name, values in baseline_errors.items()
        },
    }


def build_pikkit_input(
    game: dict[str, Any],
    *,
    phase: str,
    snapshot_rows: Iterable[dict[str, Any]],
    line_rows: Iterable[dict[str, Any]],
    initial_opinion: dict[str, Any] | None = None,
    historical_calibration: dict[str, Any] | None = None,
) -> dict[str, Any]:
    if phase not in PHASES:
        raise ValueError(f"Unsupported Pikkit generation phase: {phase}")
    event_id = str(game["event_id"])
    event_snapshots = snapshots_for_event(snapshot_rows, event_id)
    first = first_snapshot(event_snapshots, event_id)
    if first is None:
        raise ValueError("Pikkit Expert requires a first snapshot")
    selected = first if phase == INITIAL_PHASE else final_snapshot(
        event_snapshots, event_id
    )
    if selected is None:
        raise ValueError("Final Pikkit opinion requires the T-2h snapshot")
    if phase == FINAL_PHASE:
        if initial_opinion is None:
            raise ValueError("Final Pikkit opinion requires the initial opinion")
        if opinion_phase(initial_opinion) != INITIAL_PHASE:
            raise ValueError("Linked Pikkit opinion is not the initial phase")
        if str(initial_opinion.get("event_id") or "") != event_id:
            raise ValueError("Initial Pikkit opinion describes another game")

    first_analysis = analyze_snapshot(first, _line_for_snapshot(line_rows, first))
    selected_analysis = analyze_snapshot(
        selected, _line_for_snapshot(line_rows, selected)
    )
    used = [
        row
        for row in event_snapshots
        if parse_time(row["captured_at_utc"])
        <= parse_time(selected["captured_at_utc"])
    ]
    return {
        "input_profile": PIKKIT_PROFILE,
        "generation_phase": phase,
        "game": {
            "event_id": event_id,
            "season": int(game["season"]),
            "week": int(game["week"]) if str(game.get("week") or "").strip() else None,
            "away_team": str(game["away_team"]),
            "home_team": str(game["home_team"]),
            "commence_time_utc": parse_time(game["commence_time_utc"]).isoformat(),
        },
        "assumption": (
            "For estimated sportsbook outcomes only, BetOnline is assumed to "
            "have Pikkit's side-level handle distribution at the paired "
            "BetOnline prices. This is not actual BetOnline liability."
        ),
        "first_snapshot": first_analysis,
        "selected_snapshot": selected_analysis,
        "movement": (
            None if first["snapshot_id"] == selected["snapshot_id"] else
            snapshot_movement(first, selected)
        ),
        "snapshot_ids": [str(row["snapshot_id"]) for row in used],
        "initial_opinion": (
            None
            if initial_opinion is None
            else {
                "opinion_id": str(initial_opinion["opinion_id"]),
                "prediction": {
                    "predicted_winner": str(initial_opinion["predicted_winner"]),
                    "home_win_probability": float(
                        initial_opinion["home_win_probability"]
                    ),
                    "expected_home_margin": float(
                        initial_opinion["expected_home_margin"]
                    ),
                    "predicted_away_score": int(
                        float(initial_opinion["predicted_away_score"])
                    ),
                    "predicted_home_score": int(
                        float(initial_opinion["predicted_home_score"])
                    ),
                },
                "movement_watch": _initial_watch(initial_opinion),
                "thesis": str(initial_opinion.get("thesis") or ""),
            }
        ),
        "historical_calibration": historical_calibration
        or {
            "resolved_games": 0,
            "status": "insufficient_sample",
            "cohorts": [],
        },
    }


def _number(value: Any, field: str) -> float:
    if isinstance(value, bool):
        raise ValueError(f"{field} must be numeric")
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{field} must be numeric") from exc
    if not math.isfinite(number):
        raise ValueError(f"{field} must be finite")
    return number


def _strings(value: Any, field: str, *, required: bool) -> list[str]:
    if not isinstance(value, list) or any(
        not isinstance(item, str) or not item.strip() for item in value
    ):
        raise ValueError(f"{field} must be a list of non-empty strings")
    if required and not value:
        raise ValueError(f"{field} must not be empty")
    return [item.strip() for item in value]


def _watch_conditions(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list) or not value:
        raise ValueError("movement_watch must be a non-empty list")
    normalized = []
    seen = set()
    for item in value:
        if not isinstance(item, dict):
            raise ValueError("movement_watch items must be objects")
        watch_id = str(item.get("id") or "").strip()
        if not watch_id or watch_id in seen:
            raise ValueError("movement_watch ids must be non-empty and unique")
        seen.add(watch_id)
        market = str(item.get("market") or "")
        side = str(item.get("side") or "")
        metric = str(item.get("metric") or "")
        expected = str(item.get("expected_movement") or "")
        if market not in {"moneyline", "spread", "total"}:
            raise ValueError("movement_watch market is invalid")
        if side not in {"home", "away", "over", "under"}:
            raise ValueError("movement_watch side is invalid")
        if metric not in {
            "bet_pct",
            "handle_pct",
            "handle_minus_bet_pct",
            "majority_side",
        }:
            raise ValueError("movement_watch metric is invalid")
        if expected not in {
            "rise",
            "fall",
            "widen",
            "narrow",
            "flip",
            "remain_stable",
        }:
            raise ValueError("movement_watch expected_movement is invalid")
        normalized.append(
            {
                "id": watch_id,
                "market": market,
                "side": side,
                "metric": metric,
                "expected_movement": expected,
                "threshold": str(item.get("threshold") or "").strip(),
                "if_observed": str(item.get("if_observed") or "").strip(),
                "if_not_observed": str(item.get("if_not_observed") or "").strip(),
                "invalidation": str(item.get("invalidation") or "").strip(),
            }
        )
        if any(not str(value).strip() for value in normalized[-1].values()):
            raise ValueError("movement_watch fields must be non-empty")
    return normalized


def _watch_results(
    value: Any, expected_watch: list[dict[str, Any]]
) -> list[dict[str, str]]:
    if not isinstance(value, list):
        raise ValueError("watch_results must be a list")
    expected = {item["id"] for item in expected_watch}
    results: dict[str, dict[str, str]] = {}
    for item in value:
        if not isinstance(item, dict):
            raise ValueError("watch_results items must be objects")
        watch_id = str(item.get("id") or "")
        status = str(item.get("status") or "")
        evidence = str(item.get("evidence") or "").strip()
        impact = str(item.get("impact") or "").strip()
        if watch_id not in expected or watch_id in results:
            raise ValueError("watch_results ids must match the initial watch")
        if status not in WATCH_STATUSES:
            raise ValueError("watch_results status is invalid")
        if not evidence or not impact:
            raise ValueError("watch_results evidence and impact are required")
        results[watch_id] = {
            "id": watch_id,
            "status": status,
            "evidence": evidence,
            "impact": impact,
        }
    if set(results) != expected:
        raise ValueError("Final response must account for every watch condition")
    return [results[item["id"]] for item in expected_watch]


def _leg(
    selection: str,
    *,
    kind: str,
    input_payload: dict[str, Any],
    stars: int,
) -> dict[str, Any]:
    market = input_payload["selected_snapshot"]["betonline"]
    game = input_payload["game"]
    if selection == "PASS":
        return {
            "selection": "PASS",
            "line": None,
            "price": None,
            "confidence_stars": 1,
            "pass_reason": "shadow pass",
        }
    if kind == "side":
        if "spread" not in input_payload["selected_snapshot"]["markets"]:
            raise ValueError("A side leg requires complete spread splits")
        if selection == game["home_team"]:
            line, price = market["home_spread"], market["home_spread_price"]
        elif selection == game["away_team"]:
            line, price = market["away_spread"], market["away_spread_price"]
        else:
            raise ValueError("side_selection must be a team or PASS")
    else:
        if "total" not in input_payload["selected_snapshot"]["markets"]:
            raise ValueError("A total leg requires complete total splits")
        if selection not in {"Over", "Under"}:
            raise ValueError("total_selection must be Over, Under, or PASS")
        line = market["total"]
        price = market["over_price"] if selection == "Over" else market["under_price"]
    if line is None or price is None:
        raise ValueError(f"{kind} leg is missing BetOnline terms")
    return {
        "selection": selection,
        "line": float(line),
        "price": int(price),
        "confidence_stars": stars,
        "pass_reason": None,
        "shadow": True,
    }


def normalize_pikkit_opinion(
    response: dict[str, Any],
    input_payload: dict[str, Any],
) -> dict[str, Any]:
    phase = str(input_payload.get("generation_phase") or "")
    if phase not in PHASES:
        raise ValueError("Invalid Pikkit input phase")
    game = input_payload["game"]
    home = str(game["home_team"])
    away = str(game["away_team"])
    probability = _number(
        response.get("home_win_probability"), "home_win_probability"
    )
    margin = _number(response.get("expected_home_margin"), "expected_home_margin")
    projected_total = _number(response.get("projected_total"), "projected_total")
    if not 0.0 <= probability <= 1.0 or projected_total <= 0:
        raise ValueError("Pikkit probability/total is out of range")
    winner = home if probability >= 0.5 else away
    if (winner == home) != (margin >= 0):
        raise ValueError("Pikkit probability and margin disagree")
    away_score = int(round((projected_total - margin) / 2.0))
    home_score = int(round(projected_total - away_score))
    if away_score == home_score:
        if winner == home:
            home_score += 1
        else:
            away_score += 1
    stars = int(response.get("confidence_stars") or 0)
    if not 1 <= stars <= 5:
        raise ValueError("confidence_stars must be 1 through 5")
    supporting = _strings(
        response.get("supporting_factors"), "supporting_factors", required=True
    )
    counters = _strings(
        response.get("counterarguments"), "counterarguments", required=True
    )
    no_signal = _strings(
        response.get("no_signal_factors"), "no_signal_factors", required=False
    )
    discarded = _strings(
        response.get("discarded_considerations"),
        "discarded_considerations",
        required=False,
    )
    if phase == INITIAL_PHASE:
        movement_watch = _watch_conditions(response.get("movement_watch"))
        watch_results: list[dict[str, str]] = []
        side_selection = total_selection = "PASS"
    else:
        initial_watch = input_payload["initial_opinion"]["movement_watch"]
        movement_watch = initial_watch
        watch_results = _watch_results(
            response.get("watch_results"), initial_watch
        )
        side_selection = str(response.get("side_selection") or "PASS")
        total_selection = str(response.get("total_selection") or "PASS")
    side = _leg(
        side_selection, kind="side", input_payload=input_payload, stars=stars
    )
    total = _leg(
        total_selection, kind="total", input_payload=input_payload, stars=stars
    )
    thesis = str(response.get("thesis") or "").strip()
    if not thesis:
        raise ValueError("thesis is required")
    baseline = input_payload["selected_snapshot"]["market_baseline"]
    summary = {
        "generation_phase": phase,
        "prior_opinion_id": (
            ""
            if phase == INITIAL_PHASE
            else str(input_payload["initial_opinion"]["opinion_id"])
        ),
        "first_snapshot_id": input_payload["first_snapshot"]["snapshot_id"],
        "selected_snapshot_id": input_payload["selected_snapshot"]["snapshot_id"],
        "snapshot_ids": input_payload["snapshot_ids"],
        "aggregator_participation": "shadow",
        "market_baseline": baseline,
        "model_adjustment": {
            "home_win_probability": round(
                probability - float(baseline["home_win_probability"]), 6
            ),
            "expected_home_margin": round(
                margin - float(baseline["expected_home_margin"]), 2
            ),
            "projected_total": round(
                projected_total - float(baseline["projected_total"]), 2
            ),
        },
        "sportsbook": {
            market: data.get("sportsbook")
            for market, data in input_payload["selected_snapshot"]["markets"].items()
        },
        "movement": input_payload.get("movement"),
        "movement_watch": movement_watch,
        "watch_results": watch_results,
    }
    sections = [
        f"Phase: {'Initial' if phase == INITIAL_PHASE else 'Final T-2h'}",
        f"Pick: {winner} {away_score}-{home_score}",
        f"Home win probability: {probability:.1%}",
        f"Expected home margin: {margin:+.1f}",
        f"Projected total: {projected_total:.1f}",
        f"Thesis: {thesis}",
        "Supporting factors:\n" + "\n".join(f"- {item}" for item in supporting),
        "Counterarguments:\n" + "\n".join(f"- {item}" for item in counters),
    ]
    if phase == INITIAL_PHASE:
        sections.append(
            "Movement watch:\n"
            + "\n".join(
                f"- {item['id']}: {item['market']} {item['side']} "
                f"{item['metric']} {item['expected_movement']} "
                f"({item['threshold']})"
                for item in movement_watch
            )
        )
    else:
        sections.append(
            "Watch results:\n"
            + "\n".join(
                f"- {item['id']}: {item['status']} — {item['evidence']}"
                for item in watch_results
            )
        )
    if no_signal:
        sections.append(
            "No signal:\n" + "\n".join(f"- {item}" for item in no_signal)
        )
    if discarded:
        sections.append(
            "Discarded considerations:\n"
            + "\n".join(f"- {item}" for item in discarded)
        )
    return {
        "predicted_winner": winner,
        "home_win_probability": probability,
        "expected_home_margin": margin,
        "predicted_away_score": max(0, away_score),
        "predicted_home_score": max(0, home_score),
        "confidence_stars": stars,
        "pick_market": "side_and_total",
        "pick_side": winner,
        "thesis": thesis,
        "supporting_factors": supporting,
        "counterarguments": counters,
        "no_signal_factors": no_signal,
        "discarded_considerations": discarded,
        "side_pick_json": json.dumps(side, sort_keys=True),
        "total_pick_json": json.dumps(total, sort_keys=True),
        "calibration_summary_json": json.dumps(summary, sort_keys=True),
        "full_opinion": "\n\n".join(sections),
    }
