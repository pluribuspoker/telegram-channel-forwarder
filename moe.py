"""Versioned NFL mixture-of-experts generation and persistence."""

from __future__ import annotations

import calendar
import fcntl
import hashlib
import html
import json
import math
import os
import re
import subprocess
import threading
import uuid
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Awaitable, Callable, Iterable, Protocol
from zoneinfo import ZoneInfo

import yaml
import gspread
from gspread.exceptions import WorksheetNotFound

from ai import _claude_create_with_retry
from nfl_lines import _call_with_retry, get_gspread_client
from nfl_win_predictions import ensure_worksheet

ROOT = Path(__file__).resolve().parent
MOE_ROOT = ROOT / "moe"
EXPERTS_PATH = MOE_ROOT / "experts.yaml"
MOE_PENDING_PATH = ROOT / "logs" / "moe_pending.jsonl"
MOE_PENDING_LOCK_PATH = ROOT / "logs" / "moe_pending.lock"
ET = ZoneInfo("America/New_York")
MAX_SHEET_CELL_CHARS = 50_000
MAX_THESIS_CHARS = 500
MAX_THESIS_RENDERED_CHARS = 500
TELEGRAM_DETAIL_CHARS = 2_800
TELEGRAM_EXPERTS_PER_PAGE = 5

OPINIONS_TAB = "moe_opinions"
OPINION_HEADERS = [
    "opinion_id",
    "generated_at_utc",
    "generated_at_et",
    "event_id",
    "season",
    "week",
    "commence_time_utc",
    "commence_time_et",
    "away_team",
    "home_team",
    "expert_id",
    "expert_name",
    "expert_mode",
    "expert_version",
    "prompt_version",
    "input_profile",
    "prompt_path",
    "prompt_sha256",
    "expert_config_sha256",
    "git_commit",
    "output_schema_version",
    "model",
    "generation_max_tokens",
    "input_sha256",
    "predicted_winner",
    "home_win_probability",
    "expected_home_margin",
    "confidence_stars",
    "pick_market",
    "pick_side",
    "thesis",
    "supporting_factors_json",
    "counterarguments_json",
    "full_opinion",
    "input_json",
    "raw_response",
    "generation_status",
    "generation_error",
    "git_dirty",
    "source_sha256",
    "review_status",
    "reviewed_at_utc",
    "reviewed_by",
    "review_note",
    "output_sha256",
    "approved_output_sha256",
    "no_signal_factors_json",
    "discarded_considerations_json",
    "predicted_away_score",
    "predicted_home_score",
]

FORBIDDEN_SCHEDULE_INPUT_KEYS = {
    "bookmaker",
    "opening_captured_at",
    "latest_captured_at",
    "opening_selected_line",
    "opening_selected_price",
    "latest_selected_line",
    "latest_selected_price",
    "status",
    "last_updated_at",
    "period_last_checked_at",
}

CreateFn = Callable[..., Awaitable[Any]]


class MoeOpinionStore(Protocol):
    def append(self, row: dict[str, Any]) -> None: ...

    def list(self, event_id: str | None = None) -> list[dict[str, Any]]: ...

    def review(
        self,
        opinion_id: str,
        *,
        status: str,
        reviewed_by: str,
        note: str,
    ) -> None: ...


def _canonical_json(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def _sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _git_commit() -> str:
    try:
        return subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=ROOT,
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
    except (OSError, subprocess.CalledProcessError):
        return ""


def _git_dirty() -> bool:
    try:
        return bool(
            subprocess.run(
                ["git", "status", "--porcelain"],
                cwd=ROOT,
                check=True,
                capture_output=True,
                text=True,
            ).stdout.strip()
        )
    except (OSError, subprocess.CalledProcessError):
        return True


def _source_sha256(expert: dict[str, Any]) -> str:
    paths = [
        ROOT / "moe.py",
        ROOT / "ai.py",
        ROOT / "nfl_lines.py",
        ROOT / "nfl_win_predictions.py",
        ROOT / "scripts" / "generate_moe_opinion.py",
        EXPERTS_PATH,
        ROOT / str(expert["prompt_path"]),
    ]
    digest = hashlib.sha256()
    for path in paths:
        digest.update(str(path.relative_to(ROOT)).encode("utf-8"))
        digest.update(b"\0")
        digest.update(path.read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()


def _spool_failed_append(row: dict[str, Any], error: Exception) -> None:
    MOE_PENDING_PATH.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "spooled_at_utc": datetime.now(timezone.utc).isoformat(),
        "append_error": f"{type(error).__name__}: {error}",
        "row": row,
    }
    line = (_canonical_json(payload) + "\n").encode("utf-8")
    lock_fd = os.open(
        MOE_PENDING_LOCK_PATH,
        os.O_WRONLY | os.O_CREAT,
        0o600,
    )
    try:
        fcntl.flock(lock_fd, fcntl.LOCK_SH)
        fd = os.open(
            MOE_PENDING_PATH,
            os.O_WRONLY | os.O_CREAT | os.O_APPEND,
            0o600,
        )
        try:
            os.write(fd, line)
        finally:
            os.close(fd)
    finally:
        fcntl.flock(lock_fd, fcntl.LOCK_UN)
        os.close(lock_fd)


def _persist_attempt(store: MoeOpinionStore, row: dict[str, Any]) -> None:
    try:
        store.append(row)
    except Exception as exc:
        _spool_failed_append(row, exc)
        raise RuntimeError(
            f"MOE Sheet append failed; attempt spooled to {MOE_PENDING_PATH}"
        ) from exc


def load_expert(expert_id: str) -> dict[str, Any]:
    raw = EXPERTS_PATH.read_text(encoding="utf-8")
    config = yaml.safe_load(raw)
    experts = config.get("experts") if isinstance(config, dict) else None
    if not isinstance(experts, dict) or expert_id not in experts:
        raise KeyError(f"Unknown MOE expert: {expert_id}")
    expert = experts[expert_id]
    if not isinstance(expert, dict) or not expert.get("enabled"):
        raise ValueError(f"MOE expert is disabled: {expert_id}")
    prompt_path = MOE_ROOT / str(expert["prompt"])
    prompt = prompt_path.read_text(encoding="utf-8")
    return {
        "id": expert_id,
        **expert,
        "prompt_text": prompt,
        "prompt_path": str(prompt_path.relative_to(ROOT)),
        "prompt_sha256": _sha256_text(prompt),
        "expert_config_sha256": _sha256_text(_canonical_json(expert)),
    }


def _parse_time(value: Any) -> datetime:
    return datetime.fromisoformat(str(value).replace("Z", "+00:00"))


def _historical_result(row: dict[str, Any], team: str) -> str:
    home_team = str(row["home_team"])
    away_score = int(row["away_score"])
    home_score = int(row["home_score"])
    team_score = home_score if team == home_team else away_score
    opponent_score = away_score if team == home_team else home_score
    return "W" if team_score > opponent_score else "L" if team_score < opponent_score else "T"


def _team_sample(
    rows: Iterable[dict[str, Any]],
    team: str,
) -> dict[str, Any]:
    selected = [
        row
        for row in rows
        if team in {str(row["away_team"]), str(row["home_team"])}
    ]
    results = Counter(_historical_result(row, team) for row in selected)
    points_for: list[int] = []
    points_against: list[int] = []
    for row in selected:
        is_home = str(row["home_team"]) == team
        points_for.append(
            int(row["home_score"]) if is_home else int(row["away_score"])
        )
        points_against.append(
            int(row["away_score"]) if is_home else int(row["home_score"])
        )
    games = len(selected)
    return {
        "games": games,
        "wins": results["W"],
        "losses": results["L"],
        "ties": results["T"],
        "win_rate": round(results["W"] / games, 4) if games else None,
        "average_points_for": (
            round(sum(points_for) / games, 2) if games else None
        ),
        "average_points_against": (
            round(sum(points_against) / games, 2) if games else None
        ),
        "average_margin": (
            round(
                sum(
                    scored - allowed
                    for scored, allowed in zip(points_for, points_against)
                )
                / games,
                2,
            )
            if games
            else None
        ),
    }


def _calendar_match(
    row: dict[str, Any],
    *,
    weekday: str | None = None,
    month: int | None = None,
    week: int | None = None,
) -> bool:
    kickoff = _parse_time(row["kickoff_et"])
    return (
        (weekday is None or kickoff.strftime("%A") == weekday)
        and (month is None or kickoff.month == month)
        and (week is None or int(row["week"]) == week)
    )


def _current_month_rankings(
    by_month: dict[str, dict[str, Any]],
    current_month: str,
) -> dict[str, Any]:
    fields = (
        "win_rate",
        "average_margin",
        "average_points_for",
    )
    current = by_month.get(current_month)
    rankings: dict[str, Any] = {
        "month": current_month,
        "months_compared": len(by_month),
    }
    for field in fields:
        current_value = current[field] if current is not None else None
        rankings[f"{field}_rank_high_to_low"] = (
            1
            + sum(
                sample[field] > current_value
                for sample in by_month.values()
                if sample[field] is not None
            )
            if current_value is not None
            else None
        )
    return rankings


def _matchup_type(
    history: list[dict[str, Any]], away: str, home: str
) -> str:
    alignment: dict[str, tuple[str, str]] = {}
    for row in sorted(
        history,
        key=lambda item: (
            int(item["season"]),
            int(item["week"]),
            str(item["kickoff_utc"]),
        ),
    ):
        alignment[str(row["away_team"])] = (
            str(row["away_conference"]),
            str(row["away_division"]),
        )
        alignment[str(row["home_team"])] = (
            str(row["home_conference"]),
            str(row["home_division"]),
        )
    if away in alignment and home in alignment:
        away_conference, away_division = alignment[away]
        home_conference, home_division = alignment[home]
        if away_division == home_division:
            return "division"
        if away_conference == home_conference:
            return "conference"
        return "non_conference"
    return "unknown"


def build_schedule_input(
    game: dict[str, Any],
    history: list[dict[str, Any]],
) -> dict[str, Any]:
    """Build the only data package the schedule expert is allowed to see."""
    kickoff_utc = _parse_time(game["commence_time_utc"]).astimezone(timezone.utc)
    kickoff_et = kickoff_utc.astimezone(ET)
    away = str(game["away_team"])
    home = str(game["home_team"])
    week_raw = game.get("week")
    week = int(week_raw) if str(week_raw or "").strip() else None
    historical = [
        row for row in history if int(row["season"]) < int(game["season"])
    ]
    weekday = kickoff_et.strftime("%A")
    month = kickoff_et.month

    def team_context(team: str) -> dict[str, Any]:
        team_rows = [
            row
            for row in historical
            if team in {str(row["away_team"]), str(row["home_team"])}
        ]
        by_month = {
            calendar.month_name[candidate_month]: _team_sample(
                [
                    row
                    for row in team_rows
                    if _calendar_match(row, month=candidate_month)
                ],
                team,
            )
            for candidate_month in sorted(
                {
                    _parse_time(row["kickoff_et"]).month
                    for row in team_rows
                }
            )
        }
        context = {
            "all_games": _team_sample(team_rows, team),
            "as_home": _team_sample(
                [
                    row
                    for row in team_rows
                    if str(row["home_team"]) == team
                ],
                team,
            ),
            "as_away": _team_sample(
                [
                    row
                    for row in team_rows
                    if str(row["away_team"]) == team
                ],
                team,
            ),
            "same_weekday": _team_sample(
                [
                    row
                    for row in team_rows
                    if _calendar_match(row, weekday=weekday)
                ],
                team,
            ),
            "same_month": _team_sample(
                [
                    row
                    for row in team_rows
                    if _calendar_match(row, month=month)
                ],
                team,
            ),
            "same_month_as_home": _team_sample(
                [
                    row
                    for row in team_rows
                    if str(row["home_team"]) == team
                    and _calendar_match(row, month=month)
                ],
                team,
            ),
            "same_month_as_away": _team_sample(
                [
                    row
                    for row in team_rows
                    if str(row["away_team"]) == team
                    and _calendar_match(row, month=month)
                ],
                team,
            ),
            "by_month": by_month,
            "same_month_rankings": _current_month_rankings(
                by_month, calendar.month_name[month]
            ),
        }
        if week is not None:
            context["same_week_number"] = _team_sample(
                [
                    row
                    for row in team_rows
                    if _calendar_match(row, week=week)
                ],
                team,
            )
        return context

    head_to_head = [
        row
        for row in historical
        if {str(row["away_team"]), str(row["home_team"])} == {away, home}
    ]
    matchup_type = _matchup_type(historical, away, home)
    comparable = [
        row
        for row in historical
        if str(row.get("matchup_type") or "") == matchup_type
        and _calendar_match(row, weekday=weekday, month=month)
    ]
    payload = {
        "input_profile": "schedule_only",
        "game": {
            "event_id": str(game["event_id"]),
            "season": int(game["season"]),
            "commence_time_utc": kickoff_utc.isoformat(),
            "commence_time_et": kickoff_et.isoformat(),
            "weekday": weekday,
            "month": month,
            "kickoff_hour_et": kickoff_et.hour,
            "away_team": away,
            "home_team": home,
            "matchup_type": matchup_type,
        },
        "historical_data": {
            "seasons": sorted({int(row["season"]) for row in historical}),
            "away_team": team_context(away),
            "home_team": team_context(home),
            "head_to_head": {
                "games": len(head_to_head),
                "away_team_record": _team_sample(head_to_head, away),
                "home_team_record": _team_sample(head_to_head, home),
            },
            "same_weekday_and_month_matchup_type": {
                "matchup_type": matchup_type,
                "games": len(comparable),
                "home_record": _home_side_sample(comparable),
            },
        },
    }
    if week is not None:
        payload["game"]["week"] = week
    _assert_schedule_input_safe(payload)
    return payload


def _home_side_sample(rows: list[dict[str, Any]]) -> dict[str, Any]:
    games = len(rows)
    wins = sum(int(row["home_score"]) > int(row["away_score"]) for row in rows)
    losses = sum(int(row["home_score"]) < int(row["away_score"]) for row in rows)
    ties = games - wins - losses
    margins = [
        int(row["home_score"]) - int(row["away_score"]) for row in rows
    ]
    return {
        "games": games,
        "wins": wins,
        "losses": losses,
        "ties": ties,
        "win_rate": round(wins / games, 4) if games else None,
        "average_margin": round(sum(margins) / games, 2) if games else None,
    }


def _assert_schedule_input_safe(payload: Any) -> None:
    if isinstance(payload, dict):
        forbidden = FORBIDDEN_SCHEDULE_INPUT_KEYS.intersection(payload)
        if forbidden:
            raise ValueError(
                f"Schedule input contains forbidden fields: {sorted(forbidden)}"
            )
        for value in payload.values():
            _assert_schedule_input_safe(value)
    elif isinstance(payload, list):
        for value in payload:
            _assert_schedule_input_safe(value)


def _parse_response(raw: str) -> dict[str, Any]:
    cleaned = raw.strip()
    if cleaned.startswith("```"):
        cleaned = cleaned.removeprefix("```json").removeprefix("```")
        cleaned = cleaned.removesuffix("```").strip()
    def reject_constant(value: str) -> None:
        raise ValueError(f"Non-finite JSON number is not allowed: {value}")

    parsed = json.loads(cleaned, parse_constant=reject_constant)
    if not isinstance(parsed, dict):
        raise ValueError("MOE response must be a JSON object")
    return parsed


def validate_opinion(
    opinion: dict[str, Any],
    *,
    away_team: str,
    home_team: str,
    schedule_input: dict[str, Any] | None = None,
) -> None:
    winner = str(opinion.get("predicted_winner") or "")
    if winner not in {away_team, home_team}:
        raise ValueError("predicted_winner must exactly match one game team")
    probability = opinion.get("home_win_probability")
    if (
        isinstance(probability, bool)
        or not isinstance(probability, (int, float))
        or not 0.01 <= float(probability) <= 0.99
    ):
        raise ValueError("home_win_probability must be between 0.01 and 0.99")
    margin = opinion.get("expected_home_margin")
    if (
        isinstance(margin, bool)
        or not isinstance(margin, (int, float))
        or not math.isfinite(float(margin))
    ):
        raise ValueError("expected_home_margin must be numeric")
    away_score = opinion.get("predicted_away_score")
    home_score = opinion.get("predicted_home_score")
    for field, score in (
        ("predicted_away_score", away_score),
        ("predicted_home_score", home_score),
    ):
        if isinstance(score, bool) or not isinstance(score, int) or score < 0:
            raise ValueError(f"{field} must be a non-negative integer")
    if away_score == home_score:
        raise ValueError("predicted final score must not be tied")
    stars = opinion.get("confidence_stars")
    if isinstance(stars, bool) or not isinstance(stars, int) or not 1 <= stars <= 5:
        raise ValueError("confidence_stars must be an integer from 1 through 5")
    if winner == home_team and (float(probability) < 0.5 or float(margin) < 0):
        raise ValueError("Home-team prediction conflicts with probability or margin")
    if winner == away_team and (float(probability) > 0.5 or float(margin) > 0):
        raise ValueError("Away-team prediction conflicts with probability or margin")
    if winner == home_team and home_score <= away_score:
        raise ValueError("Home-team prediction conflicts with predicted score")
    if winner == away_team and away_score <= home_score:
        raise ValueError("Away-team prediction conflicts with predicted score")
    for field in ("thesis", "full_opinion"):
        if not str(opinion.get(field) or "").strip():
            raise ValueError(f"{field} is required")
        if len(str(opinion[field])) >= MAX_SHEET_CELL_CHARS:
            raise ValueError(f"{field} exceeds the Google Sheets cell limit")
    if len(str(opinion["thesis"])) > MAX_THESIS_CHARS:
        raise ValueError(
            f"thesis must not exceed {MAX_THESIS_CHARS} characters"
        )
    if len(html.escape(str(opinion["thesis"]))) > MAX_THESIS_RENDERED_CHARS:
        raise ValueError(
            "escaped thesis is too long for the Telegram summary"
        )
    for field in ("supporting_factors", "counterarguments"):
        value = opinion.get(field)
        if not isinstance(value, list) or not value or not all(
            isinstance(item, str) and item.strip() for item in value
        ):
            raise ValueError(
                f"{field} must be a non-empty list of non-empty strings"
            )
    for field in ("no_signal_factors", "discarded_considerations"):
        value = opinion.get(field)
        if not isinstance(value, list) or not all(
            isinstance(item, str) and item.strip() for item in value
        ):
            raise ValueError(f"{field} must be a list of non-empty strings")
    if (
        schedule_input is not None
        and schedule_input["game"].get("week") is None
    ):
        rendered = _canonical_json(opinion)
        if re.search(
            r"\b(?:"
            r"week\s+(?:number\s+)?(?:\d+|one|two|three|four|five|six|"
            r"seven|eight|nine|ten|eleven|twelve|thirteen|fourteen|"
            r"fifteen|sixteen|seventeen|eighteen)|"
            r"(?:first|second|third|fourth|fifth|sixth|seventh|eighth|"
            r"ninth|tenth|eleventh|twelfth|thirteenth|fourteenth|"
            r"fifteenth|sixteenth|seventeenth|eighteenth)\s+week|"
            r"season[- ]opener|opener|early[- ]season|mid[- ]season|"
            r"late[- ]season|season finale|final week|opening week"
            r")\b",
            rendered,
            re.IGNORECASE,
        ):
            raise ValueError(
                "Opinion invents week context when the schedule week is null"
            )
async def generate_opinion(
    *,
    expert_id: str,
    game: dict[str, Any],
    history: list[dict[str, Any]],
    store: MoeOpinionStore,
    model: str | None = None,
    create_fn: CreateFn = _claude_create_with_retry,
) -> dict[str, Any]:
    expert = load_expert(expert_id)
    if expert["input_profile"] != "schedule_only":
        raise NotImplementedError(
            f"Unsupported input profile: {expert['input_profile']}"
        )
    input_payload = build_schedule_input(game, history)
    input_json = _canonical_json(input_payload)
    if len(input_json) >= MAX_SHEET_CELL_CHARS:
        raise ValueError("MOE input exceeds the Google Sheets cell limit")
    selected_model = (
        model
        or str(expert.get("default_model") or "")
        or os.getenv("MOE_MODEL")
        or "claude-sonnet-4-6"
    )
    allowed_models = {
        str(candidate) for candidate in expert.get("allowed_models", [])
    }
    if allowed_models and selected_model not in allowed_models:
        raise ValueError(
            f"Model {selected_model} is not allowed for expert {expert_id}"
        )
    max_tokens = 5000
    away = str(game["away_team"])
    home = str(game["home_team"])
    generated_at = datetime.now(timezone.utc)
    kickoff_utc = _parse_time(game["commence_time_utc"]).astimezone(timezone.utc)
    row = {
        "opinion_id": str(uuid.uuid4()),
        "generated_at_utc": generated_at.isoformat(),
        "generated_at_et": generated_at.astimezone(ET).isoformat(),
        "event_id": str(game["event_id"]),
        "season": int(game["season"]),
        "week": (
            int(game["week"])
            if str(game.get("week") or "").strip()
            else ""
        ),
        "commence_time_utc": kickoff_utc.isoformat(),
        "commence_time_et": kickoff_utc.astimezone(ET).isoformat(),
        "away_team": away,
        "home_team": home,
        "expert_id": expert_id,
        "expert_name": expert["name"],
        "expert_mode": expert["mode"],
        "expert_version": expert["version"],
        "prompt_version": expert["prompt_version"],
        "input_profile": expert["input_profile"],
        "prompt_path": expert["prompt_path"],
        "prompt_sha256": expert["prompt_sha256"],
        "expert_config_sha256": expert["expert_config_sha256"],
        "git_commit": _git_commit(),
        "output_schema_version": expert["output_schema_version"],
        "model": selected_model,
        "generation_max_tokens": max_tokens,
        "input_sha256": _sha256_text(input_json),
        "predicted_winner": "",
        "home_win_probability": "",
        "expected_home_margin": "",
        "confidence_stars": "",
        "pick_market": "",
        "pick_side": "",
        "thesis": "",
        "supporting_factors_json": "",
        "counterarguments_json": "",
        "full_opinion": "",
        "input_json": input_json,
        "raw_response": "",
        "generation_status": "started",
        "generation_error": "",
        "git_dirty": _git_dirty(),
        "source_sha256": _source_sha256(expert),
        "output_sha256": "",
        "review_status": "pending",
        "reviewed_at_utc": "",
        "reviewed_by": "",
        "review_note": "",
        "approved_output_sha256": "",
        "no_signal_factors_json": "",
        "discarded_considerations_json": "",
        "predicted_away_score": "",
        "predicted_home_score": "",
    }
    try:
        response = await create_fn(
            model=selected_model,
            max_tokens=max_tokens,
            system=expert["prompt_text"],
            messages=[
                {
                    "role": "user",
                    "content": (
                        "Generate the schedule opinion from this exact input:\n"
                        f"{input_json}"
                    ),
                }
            ],
        )
        raw_response = str(response.content[0].text).strip()
        row["raw_response"] = raw_response
        if len(raw_response) >= MAX_SHEET_CELL_CHARS:
            raise ValueError(
                "MOE response exceeds the Google Sheets cell limit"
            )
        opinion = _parse_response(raw_response)
        validate_opinion(
            opinion,
            away_team=away,
            home_team=home,
            schedule_input=input_payload,
        )
        row.update(
            {
                "predicted_winner": opinion["predicted_winner"],
                "home_win_probability": float(
                    opinion["home_win_probability"]
                ),
                "expected_home_margin": float(
                    opinion["expected_home_margin"]
                ),
                "predicted_away_score": int(
                    opinion["predicted_away_score"]
                ),
                "predicted_home_score": int(
                    opinion["predicted_home_score"]
                ),
                "confidence_stars": int(opinion["confidence_stars"]),
                "pick_market": "straight_up",
                "pick_side": opinion["predicted_winner"],
                "thesis": str(opinion["thesis"]).strip(),
                "supporting_factors_json": _canonical_json(
                    opinion["supporting_factors"]
                ),
                "counterarguments_json": _canonical_json(
                    opinion["counterarguments"]
                ),
                "full_opinion": str(opinion["full_opinion"]).strip(),
                "no_signal_factors_json": _canonical_json(
                    opinion["no_signal_factors"]
                ),
                "discarded_considerations_json": _canonical_json(
                    opinion["discarded_considerations"]
                ),
                "generation_status": "valid",
            }
        )
        row["output_sha256"] = opinion_output_sha256(row)
    except Exception as exc:
        row["generation_status"] = "invalid"
        row["generation_error"] = f"{type(exc).__name__}: {exc}"
        row["review_status"] = "not_applicable"
        _persist_attempt(store, row)
        raise
    _persist_attempt(store, row)
    return row


class GoogleSheetsMoeOpinionStore:
    def __init__(self, credentials: str, sheet_id: str) -> None:
        self._credentials = credentials
        self._sheet_id = sheet_id
        self._spreadsheet_instance: Any | None = None
        self._spreadsheet_lock = threading.Lock()

    def _spreadsheet(self) -> Any:
        with self._spreadsheet_lock:
            if self._spreadsheet_instance is None:
                self._spreadsheet_instance = get_gspread_client(
                    self._credentials
                ).open_by_key(self._sheet_id)
            return self._spreadsheet_instance

    def append(self, row: dict[str, Any]) -> None:
        spreadsheet = self._spreadsheet()
        worksheet = ensure_worksheet(
            spreadsheet, OPINIONS_TAB, OPINION_HEADERS
        )
        opinion_id = str(row["opinion_id"])
        existing_ids = set(
            _call_with_retry(worksheet.col_values, 1)[1:]
        )
        if opinion_id in existing_ids:
            return
        _call_with_retry(
            worksheet.append_row,
            [row.get(header, "") for header in OPINION_HEADERS],
            value_input_option="RAW",
        )

    def list(self, event_id: str | None = None) -> list[dict[str, Any]]:
        try:
            worksheet = self._spreadsheet().worksheet(OPINIONS_TAB)
        except WorksheetNotFound:
            return []
        rows = worksheet.get_all_records(expected_headers=OPINION_HEADERS)
        if event_id is None:
            return rows
        return [
            row
            for row in rows
            if str(row.get("event_id")) == str(event_id)
        ]

    def review(
        self,
        opinion_id: str,
        *,
        status: str,
        reviewed_by: str,
        note: str,
    ) -> None:
        if status not in {"approved", "rejected"}:
            raise ValueError("Review status must be approved or rejected")
        worksheet = self._spreadsheet().worksheet(OPINIONS_TAB)
        ids = worksheet.col_values(
            OPINION_HEADERS.index("opinion_id") + 1
        )
        matches = [
            row_number
            for row_number, value in enumerate(ids, start=1)
            if value == opinion_id
        ]
        if len(matches) != 1:
            raise ValueError(
                f"Expected one opinion_id match, found {len(matches)}"
            )
        row_number = matches[0]
        values = worksheet.row_values(row_number)
        current = dict(
            zip(
                OPINION_HEADERS,
                values + [""] * (len(OPINION_HEADERS) - len(values)),
            )
        )
        if current["generation_status"] != "valid":
            raise ValueError("Only valid opinions can be reviewed")
        current_output_sha256 = opinion_output_sha256(current)
        if current_output_sha256 != current["output_sha256"]:
            raise ValueError(
                "Opinion content changed after generation; review refused"
            )
        reviewed_at = datetime.now(timezone.utc).isoformat()
        start_col = OPINION_HEADERS.index("review_status") + 1
        end_col = OPINION_HEADERS.index("approved_output_sha256") + 1
        start = gspread.utils.rowcol_to_a1(row_number, start_col)
        end = gspread.utils.rowcol_to_a1(row_number, end_col)
        _call_with_retry(
            worksheet.update,
            [
                [
                    status,
                    reviewed_at,
                    reviewed_by,
                    note,
                    current["output_sha256"],
                    current_output_sha256 if status == "approved" else "",
                ]
            ],
            f"{start}:{end}",
            value_input_option="RAW",
        )


def configured_opinion_store() -> MoeOpinionStore:
    backend = os.getenv("MOE_STORAGE_BACKEND", "google_sheets")
    if backend != "google_sheets":
        raise ValueError(f"Unsupported MOE_STORAGE_BACKEND: {backend}")
    return GoogleSheetsMoeOpinionStore(
        os.environ["GOOGLE_CREDENTIALS"],
        os.environ["NFL_INTAKE_SHEET_ID"],
    )


def append_opinion(row: dict[str, Any]) -> None:
    configured_opinion_store().append(row)


def load_opinions(event_id: str | None = None) -> list[dict[str, Any]]:
    return configured_opinion_store().list(event_id)


def approved_opinions(
    rows: Iterable[dict[str, Any]],
) -> list[dict[str, Any]]:
    approved: list[dict[str, Any]] = []
    for row in rows:
        if str(row.get("generation_status") or "valid") != "valid":
            continue
        if str(row.get("review_status") or "") != "approved":
            continue
        try:
            current_output_sha256 = opinion_output_sha256(row)
        except (TypeError, ValueError):
            continue
        if (
            not row.get("approved_output_sha256")
            or current_output_sha256 != str(row["approved_output_sha256"])
        ):
            continue
        approved.append(row)
    return approved


def _latest_by_key(
    rows: Iterable[dict[str, Any]],
    key_fn: Callable[[dict[str, Any]], Any],
) -> list[dict[str, Any]]:
    latest: dict[Any, dict[str, Any]] = {}
    for row in approved_opinions(rows):
        identity = key_fn(row)
        if not identity:
            continue
        expert_id = str(row.get("expert_id") or "")
        key = (
            str(row.get("generated_at_utc") or ""),
            str(row.get("opinion_id") or ""),
        )
        previous = latest.get(identity)
        if expert_id and (
            previous is None
            or key
            > (
                str(previous.get("generated_at_utc") or ""),
                str(previous.get("opinion_id") or ""),
            )
        ):
            latest[identity] = row
    return list(latest.values())


def latest_opinions(rows: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    latest = _latest_by_key(rows, lambda row: str(row.get("expert_id") or ""))
    return sorted(latest, key=lambda row: str(row["expert_name"]))


def latest_model_opinions(
    rows: Iterable[dict[str, Any]], expert_id: str
) -> list[dict[str, Any]]:
    latest = _latest_by_key(
        rows,
        lambda row: (
            str(row.get("expert_id") or ""),
            str(row.get("model") or ""),
        ),
    )
    choices = [
        row for row in latest if str(row.get("expert_id") or "") == expert_id
    ]
    choices.sort(key=lambda row: str(row.get("model") or ""))
    choices.sort(
        key=lambda row: str(row.get("generated_at_utc") or ""),
        reverse=True,
    )
    return choices


def opinion_output_sha256(row: dict[str, Any]) -> str:
    def number(value: Any) -> dict[str, Any]:
        if value is None or value == "":
            return {"present": False, "value": ""}
        numeric = float(value)
        if not math.isfinite(numeric):
            raise ValueError("Approval hash contains a non-finite number")
        return {"present": True, "value": format(numeric, ".12g")}

    payload = {
        "opinion_id": str(row.get("opinion_id") or ""),
        "generated_at_utc": str(row.get("generated_at_utc") or ""),
        "event_id": str(row.get("event_id") or ""),
        "season": str(row.get("season") or ""),
        "week": str(row.get("week") or ""),
        "commence_time_utc": str(row.get("commence_time_utc") or ""),
        "away_team": str(row.get("away_team") or ""),
        "home_team": str(row.get("home_team") or ""),
        "expert_id": str(row.get("expert_id") or ""),
        "expert_name": str(row.get("expert_name") or ""),
        "expert_mode": str(row.get("expert_mode") or ""),
        "expert_version": str(row.get("expert_version") or ""),
        "prompt_version": str(row.get("prompt_version") or ""),
        "input_profile": str(row.get("input_profile") or ""),
        "prompt_path": str(row.get("prompt_path") or ""),
        "prompt_sha256": str(row.get("prompt_sha256") or ""),
        "expert_config_sha256": str(
            row.get("expert_config_sha256") or ""
        ),
        "git_commit": str(row.get("git_commit") or ""),
        "output_schema_version": str(
            row.get("output_schema_version") or ""
        ),
        "model": str(row.get("model") or ""),
        "generation_max_tokens": str(
            row.get("generation_max_tokens") or ""
        ),
        "input_sha256": str(row.get("input_sha256") or ""),
        "input_json": str(row.get("input_json") or ""),
        "source_sha256": str(row.get("source_sha256") or ""),
        "predicted_winner": str(row.get("predicted_winner") or ""),
        "home_win_probability": number(row.get("home_win_probability")),
        "expected_home_margin": number(row.get("expected_home_margin")),
        "predicted_away_score": (
            ""
            if row.get("predicted_away_score") in (None, "")
            else str(row["predicted_away_score"])
        ),
        "predicted_home_score": (
            ""
            if row.get("predicted_home_score") in (None, "")
            else str(row["predicted_home_score"])
        ),
        "confidence_stars": str(row.get("confidence_stars") or ""),
        "pick_market": str(row.get("pick_market") or ""),
        "pick_side": str(row.get("pick_side") or ""),
        "thesis": str(row.get("thesis") or ""),
        "supporting_factors_json": str(
            row.get("supporting_factors_json") or ""
        ),
        "counterarguments_json": str(
            row.get("counterarguments_json") or ""
        ),
        "full_opinion": str(row.get("full_opinion") or ""),
        "raw_response": str(row.get("raw_response") or ""),
        "no_signal_factors_json": str(
            row.get("no_signal_factors_json") or ""
        ),
        "discarded_considerations_json": str(
            row.get("discarded_considerations_json") or ""
        ),
    }
    return _sha256_text(_canonical_json(payload))


def _escaped_chunks(value: str, max_chars: int) -> list[str]:
    chunks: list[str] = []
    current: list[str] = []
    current_length = 0
    for character in value:
        escaped = html.escape(character)
        if current and current_length + len(escaped) > max_chars:
            chunks.append("".join(current))
            current = []
            current_length = 0
        current.append(escaped)
        current_length += len(escaped)
    if current or not chunks:
        chunks.append("".join(current))
    return chunks


def opinion_summary(
    game: dict[str, Any],
    rows: Iterable[dict[str, Any]],
    *,
    page: int = 0,
    event_id: str | None = None,
) -> tuple[str, list[list[Any]]]:
    from telethon import Button

    current = latest_opinions(rows)
    page_count = max(
        1,
        (len(current) + TELEGRAM_EXPERTS_PER_PAGE - 1)
        // TELEGRAM_EXPERTS_PER_PAGE,
    )
    page = max(0, min(page, page_count - 1))
    start = page * TELEGRAM_EXPERTS_PER_PAGE
    visible = current[start : start + TELEGRAM_EXPERTS_PER_PAGE]
    away = str(game["away_team"])
    home = str(game["home_team"])
    callback_event_id = event_id or str(game["event_id"])
    lines = [
        f"🧠 <b>MOE · {html.escape(away)} @ {html.escape(home)}</b>",
        "",
    ]
    buttons: list[list[Any]] = []
    if not current:
        lines.append("No expert opinions have been generated for this game.")
    for row in visible:
        stars = "★" * int(row["confidence_stars"])
        winner = html.escape(str(row["predicted_winner"]))
        probability = float(row["home_win_probability"])
        winner_probability = (
            probability
            if str(row["predicted_winner"]) == home
            else 1 - probability
        )
        lines.append(
            f"<b>{html.escape(str(row['expert_name']))}</b>: {winner} "
            f"{winner_probability:.0%} {stars} · "
            f"score {int(row['predicted_away_score'])}-"
            f"{int(row['predicted_home_score'])}"
        )
        lines.append(html.escape(str(row["thesis"])))
        buttons.append(
            [
                Button.inline(
                    str(row["expert_name"]),
                    (
                        f"moe:expert:{row['expert_id']}:"
                        f"{callback_event_id}:0"
                    ).encode(),
                )
            ]
        )
    navigation = []
    if page > 0:
        navigation.append(
            Button.inline(
                "◀ Prev",
                f"moe:view:{callback_event_id}:{page - 1}".encode(),
            )
        )
    if page + 1 < page_count:
        navigation.append(
            Button.inline(
                "Next ▶",
                f"moe:view:{callback_event_id}:{page + 1}".encode(),
            )
        )
    if navigation:
        buttons.append(navigation)
    if current:
        lines.extend(["", f"Page {page + 1} of {page_count}"])
    buttons.append([Button.inline("← Back to game", b"back:game")])
    return "\n".join(lines), buttons


def opinion_model_picker(
    rows: Iterable[dict[str, Any]],
    *,
    expert_id: str,
    page: int = 0,
    event_id: str,
) -> tuple[str, list[list[Any]]]:
    from telethon import Button

    choices = latest_model_opinions(rows, expert_id)
    if not choices:
        raise ValueError("No approved model opinions are available")
    page_count = max(
        1,
        (len(choices) + TELEGRAM_EXPERTS_PER_PAGE - 1)
        // TELEGRAM_EXPERTS_PER_PAGE,
    )
    page = max(0, min(page, page_count - 1))
    start = page * TELEGRAM_EXPERTS_PER_PAGE
    visible = choices[start : start + TELEGRAM_EXPERTS_PER_PAGE]
    lines = [
        f"🧠 <b>{html.escape(str(choices[0]['expert_name']))}</b>",
        "Choose a model:",
        "",
    ]
    buttons: list[list[Any]] = []
    for row in visible:
        model = str(row.get("model") or "unknown model")
        home = str(row["home_team"])
        probability = float(row["home_win_probability"])
        winner_probability = (
            probability
            if str(row["predicted_winner"]) == home
            else 1 - probability
        )
        stars = "★" * int(row["confidence_stars"])
        lines.append(
            f"<b>{html.escape(model)}</b> — "
            f"{html.escape(str(row['predicted_winner']))} "
            f"{winner_probability:.0%} {stars} · "
            f"score {int(row['predicted_away_score'])}-"
            f"{int(row['predicted_home_score'])}"
        )
        buttons.append(
            [
                Button.inline(
                    model,
                    f"moe:opinion:{row['opinion_id']}:0".encode(),
                )
            ]
        )
    navigation = []
    if page > 0:
        navigation.append(
            Button.inline(
                "◀ Prev",
                f"moe:expert:{expert_id}:{event_id}:{page - 1}".encode(),
            )
        )
    if page + 1 < page_count:
        navigation.append(
            Button.inline(
                "Next ▶",
                f"moe:expert:{expert_id}:{event_id}:{page + 1}".encode(),
            )
        )
    if navigation:
        buttons.append(navigation)
    lines.extend(["", f"Page {page + 1} of {page_count}"])
    buttons.extend([
        [
            Button.inline(
                "← All MOE opinions",
                f"moe:view:{event_id}:0".encode(),
            )
        ],
        [Button.inline("← Back to game", b"back:game")],
    ])
    return "\n".join(lines), buttons


def opinion_detail(
    row: dict[str, Any],
    *,
    page: int = 0,
    event_id: str | None = None,
    show_model_picker: bool = False,
) -> tuple[str, list[list[Any]]]:
    from telethon import Button

    stars = "★" * int(row["confidence_stars"])
    home_probability = float(row["home_win_probability"])
    margin = float(row["expected_home_margin"])
    away_score = int(row["predicted_away_score"])
    home_score = int(row["predicted_home_score"])
    heading = (
        f"🧠 <b>{html.escape(str(row['expert_name']))}</b>\n\n"
        f"<b>Pick:</b> {html.escape(str(row['predicted_winner']))}\n"
        f"<b>Predicted score:</b> "
        f"{html.escape(str(row['away_team']))} {away_score} — "
        f"{html.escape(str(row['home_team']))} {home_score}\n"
        f"<b>Home win probability:</b> {home_probability:.0%}\n"
        f"<b>Expected home margin:</b> {margin:+.1f}\n"
        f"<b>Confidence:</b> {stars}\n\n"
    )
    full_opinion = str(row["full_opinion"])
    chunks = _escaped_chunks(full_opinion, TELEGRAM_DETAIL_CHARS)
    page = max(0, min(page, len(chunks) - 1))
    footer = (
        f"\n\n<i>Expert v{row['expert_version']} · "
        f"model {html.escape(str(row['model']))} · "
        f"prompt {str(row['prompt_sha256'])[:8]} · "
        f"page {page + 1}/{len(chunks)}</i>"
    )
    buttons: list[list[Any]] = []
    navigation = []
    expert_id = str(row["expert_id"])
    callback_event_id = event_id or str(row["event_id"])
    if page > 0:
        navigation.append(
            Button.inline(
                "◀ Prev",
                f"moe:opinion:{row['opinion_id']}:{page - 1}".encode(),
            )
        )
    if page + 1 < len(chunks):
        navigation.append(
            Button.inline(
                "Next ▶",
                f"moe:opinion:{row['opinion_id']}:{page + 1}".encode(),
            )
        )
    if navigation:
        buttons.append(navigation)
    if show_model_picker:
        buttons.append(
            [
                Button.inline(
                    "← Models",
                    f"moe:expert:{expert_id}:{callback_event_id}:0".encode(),
                )
            ]
        )
    buttons.extend([
        [
            Button.inline(
                "← All MOE opinions",
                f"moe:view:{callback_event_id}:0".encode(),
            )
        ],
        [Button.inline("← Back to game", b"back:game")],
    ])
    return heading + chunks[page] + footer, buttons
