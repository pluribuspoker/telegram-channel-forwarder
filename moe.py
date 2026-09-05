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
from moe_ak import WNBA_PRIOR_PATH, build_ak_input

ROOT = Path(__file__).resolve().parent
MOE_ROOT = ROOT / "moe"
EXPERTS_PATH = MOE_ROOT / "experts.yaml"
FACTUALITY_PROMPT_PATH = MOE_ROOT / "prompts" / "factuality" / "v2.md"
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
    "generation_backend",
    "generation_max_tokens",
    "generation_effort",
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
    "nondeterministic_analysis_raw",
    "nondeterministic_analysis_usable",
    "nondeterministic_factuality_status",
    "nondeterministic_factuality_json",
    "side_pick_json",
    "total_pick_json",
    "calibration_summary_json",
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
    if int(expert.get("output_schema_version") or 0) == 4:
        paths.append(FACTUALITY_PROMPT_PATH)
    if expert.get("input_profile") == "ak_calibration":
        paths.extend((ROOT / "moe_ak.py", WNBA_PRIOR_PATH))
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


def _home_team_full_sample(rows: list[dict[str, Any]]) -> dict[str, Any]:
    games = len(rows)
    wins = sum(int(row["home_score"]) > int(row["away_score"]) for row in rows)
    losses = sum(int(row["home_score"]) < int(row["away_score"]) for row in rows)
    ties = games - wins - losses
    points_for = [int(row["home_score"]) for row in rows]
    points_against = [int(row["away_score"]) for row in rows]
    margins = [
        home_score - away_score
        for home_score, away_score in zip(points_for, points_against)
    ]
    return {
        "games": games,
        "wins": wins,
        "losses": losses,
        "ties": ties,
        "win_rate": round(wins / games, 4) if games else None,
        "average_points_for": (
            round(sum(points_for) / games, 2) if games else None
        ),
        "average_points_against": (
            round(sum(points_against) / games, 2) if games else None
        ),
        "average_margin": round(sum(margins) / games, 2) if games else None,
    }


def _home_team_meeting_cohort(
    rows: list[dict[str, Any]],
) -> dict[str, Any]:
    return {
        f"meeting_{meeting_number}": _home_team_full_sample(
            [
                row
                for row in rows
                if int(row.get("division_meeting_number") or 0)
                == meeting_number
            ]
        )
        for meeting_number in (1, 2)
    }


def _pair_series_summary(
    rows: list[dict[str, Any]], team: str
) -> dict[str, Any]:
    by_season: dict[int, list[dict[str, Any]]] = {}
    for row in rows:
        by_season.setdefault(int(row["season"]), []).append(row)
    sweeps = 0
    swept = 0
    splits = 0
    second_after_first: dict[str, list[dict[str, Any]]] = {
        "win": [],
        "loss": [],
        "tie": [],
    }
    complete_seasons = 0
    for season_rows in by_season.values():
        ordered = sorted(
            season_rows,
            key=lambda row: (
                int(row.get("division_meeting_number") or 99),
                str(row["kickoff_utc"]),
            ),
        )
        if len(ordered) != 2:
            continue
        complete_seasons += 1
        results = [_historical_result(row, team) for row in ordered]
        if results == ["W", "W"]:
            sweeps += 1
        elif results == ["L", "L"]:
            swept += 1
        else:
            splits += 1
        first_key = {"W": "win", "L": "loss", "T": "tie"}[results[0]]
        second_after_first[first_key].append(ordered[1])
    return {
        "complete_two_game_seasons": complete_seasons,
        "team_sweeps": sweeps,
        "team_swept": swept,
        "season_splits_or_ties": splits,
        "second_meeting_after_first_win": _team_sample(
            second_after_first["win"], team
        ),
        "second_meeting_after_first_loss": _team_sample(
            second_after_first["loss"], team
        ),
        "second_meeting_after_first_tie": _team_sample(
            second_after_first["tie"], team
        ),
    }


def build_divisional_input(
    game: dict[str, Any],
    history: list[dict[str, Any]],
    schedule: list[dict[str, Any]],
    current_season_results: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Build the whitelisted input for the divisional-matchup expert."""
    kickoff_utc = _parse_time(game["commence_time_utc"]).astimezone(timezone.utc)
    kickoff_et = kickoff_utc.astimezone(ET)
    away = str(game["away_team"])
    home = str(game["home_team"])
    season = int(game["season"])
    historical = [row for row in history if int(row["season"]) < season]
    matchup_type = _matchup_type(historical, away, home)
    is_divisional = matchup_type == "division"
    pair = {away, home}
    historical_division_rows = [
        row for row in historical if str(row["matchup_type"]) == "division"
    ]
    current_division = None
    if is_divisional:
        latest_alignment_row = max(
            (
                row
                for row in historical
                if home in {str(row["away_team"]), str(row["home_team"])}
            ),
            key=lambda row: (
                int(row["season"]),
                int(row["week"]),
                str(row["kickoff_utc"]),
            ),
        )
        current_division = (
            str(latest_alignment_row["home_division"])
            if str(latest_alignment_row["home_team"]) == home
            else str(latest_alignment_row["away_division"])
        )
    pair_schedule = sorted(
        [
            row
            for row in schedule
            if int(row["season"]) == season
            and {str(row["away_team"]), str(row["home_team"])} == pair
        ],
        key=lambda row: (
            str(row.get("kickoff_utc") or row.get("commence_time_utc") or ""),
            str(row["event_id"]),
        ),
    )
    schedule_matches = [
        (index, row)
        for index, row in enumerate(pair_schedule)
        if (
            str(row["event_id"]) == str(game["event_id"])
            or (
                str(row["away_team"]) == away
                and str(row["home_team"]) == home
                and _parse_time(row["kickoff_utc"]) == kickoff_utc
            )
        )
    ]
    if len(schedule_matches) != 1:
        raise ValueError(
            "Current game must match exactly one authoritative schedule row"
        )
    current_index, current_schedule_row = schedule_matches[0]
    if is_divisional and len(pair_schedule) != 2:
        raise ValueError(
            "Divisional opponents must have exactly two scheduled meetings"
        )
    meeting_number = current_index + 1 if is_divisional else None
    days_between_meetings = None
    if is_divisional:
        first_kickoff = _parse_time(pair_schedule[0]["kickoff_utc"])
        second_kickoff = _parse_time(pair_schedule[1]["kickoff_utc"])
        days_between_meetings = (second_kickoff - first_kickoff).days

    def team_context(team: str) -> dict[str, Any]:
        team_rows = [
            row
            for row in historical
            if team in {str(row["away_team"]), str(row["home_team"])}
        ]
        division_rows = [
            row for row in team_rows if str(row["matchup_type"]) == "division"
        ]
        non_division_rows = [
            row for row in team_rows if str(row["matchup_type"]) != "division"
        ]
        opponent_rows = [
            row
            for row in team_rows
            if {str(row["away_team"]), str(row["home_team"])} == pair
        ]
        return {
            "all_games": _team_sample(team_rows, team),
            "division_games": _team_sample(division_rows, team),
            "non_division_games": _team_sample(non_division_rows, team),
            "division_as_home": _team_sample(
                [
                    row
                    for row in division_rows
                    if str(row["home_team"]) == team
                ],
                team,
            ),
            "division_as_away": _team_sample(
                [
                    row
                    for row in division_rows
                    if str(row["away_team"]) == team
                ],
                team,
            ),
            "conference_non_division": _team_sample(
                [
                    row
                    for row in team_rows
                    if str(row["matchup_type"]) == "conference"
                ],
                team,
            ),
            "non_conference": _team_sample(
                [
                    row
                    for row in team_rows
                    if str(row["matchup_type"]) == "non_conference"
                ],
                team,
            ),
            "against_current_opponent": _team_sample(opponent_rows, team),
            "against_current_opponent_as_home": _team_sample(
                [
                    row
                    for row in opponent_rows
                    if str(row["home_team"]) == team
                ],
                team,
            ),
            "against_current_opponent_as_away": _team_sample(
                [
                    row
                    for row in opponent_rows
                    if str(row["away_team"]) == team
                ],
                team,
            ),
            "division_meeting_1": _team_sample(
                [
                    row
                    for row in division_rows
                    if int(row.get("division_meeting_number") or 0) == 1
                ],
                team,
            ),
            "division_meeting_2": _team_sample(
                [
                    row
                    for row in division_rows
                    if int(row.get("division_meeting_number") or 0) == 2
                ],
                team,
            ),
            "current_opponent_series": _pair_series_summary(
                opponent_rows, team
            ),
        }

    prior_meeting: dict[str, Any] | None = None
    if is_divisional and meeting_number == 2:
        prior_schedule = pair_schedule[0]
        prior_result = next(
            (
                row
                for row in current_season_results or []
                if str(row["event_id"]) == str(prior_schedule["event_id"])
            ),
            None,
        )
        if prior_result is not None:
            away_score = int(prior_result["away_score"])
            home_score = int(prior_result["home_score"])
            winner = (
                str(prior_result["home_team"])
                if home_score > away_score
                else (
                    str(prior_result["away_team"])
                    if away_score > home_score
                    else "tie"
                )
            )
            prior_meeting = {
                "event_id": str(prior_result["event_id"]),
                "week": int(prior_result["week"]),
                "kickoff_utc": str(prior_result["kickoff_utc"]),
                "away_team": str(prior_result["away_team"]),
                "home_team": str(prior_result["home_team"]),
                "away_score": away_score,
                "home_score": home_score,
                "winner": winner,
                "home_margin": home_score - away_score,
            }

    payload = {
        "input_profile": "divisional",
        "game": {
            "event_id": str(game["event_id"]),
            "schedule_event_id": str(current_schedule_row["event_id"]),
            "season": season,
            "week": int(game["week"]),
            "commence_time_utc": kickoff_utc.isoformat(),
            "commence_time_et": kickoff_et.isoformat(),
            "away_team": away,
            "home_team": home,
            "matchup_type": matchup_type,
            "is_divisional": is_divisional,
            "division_meeting_number": meeting_number,
            "scheduled_pair_meetings": len(pair_schedule),
            "days_between_pair_meetings": days_between_meetings,
        },
        "historical_data": {
            "seasons": sorted({int(row["season"]) for row in historical}),
            "away_team": team_context(away),
            "home_team": team_context(home),
            "divisional_home_side_meeting_cohorts": (
                {
                    "perspective": (
                        "historical home side in each game, not the current "
                        "home team"
                    ),
                    "nfl": _home_team_meeting_cohort(
                        historical_division_rows
                    ),
                    "division": {
                        "name": current_division,
                        **_home_team_meeting_cohort(
                            [
                                row
                                for row in historical_division_rows
                                if str(row["home_division"])
                                == current_division
                            ]
                        ),
                    },
                    "opponent_pair": {
                        "teams": sorted(pair),
                        **_home_team_meeting_cohort(
                            [
                                row
                                for row in historical_division_rows
                                if {
                                    str(row["away_team"]),
                                    str(row["home_team"]),
                                }
                                == pair
                            ]
                        ),
                    },
                }
                if is_divisional
                else None
            ),
        },
        "current_season_prior_meeting": prior_meeting,
    }
    _assert_schedule_input_safe(payload)
    return payload


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


def _resolve_evidence_path(
    input_payload: dict[str, Any],
    path: str,
) -> Any:
    if not re.fullmatch(r"[a-z0-9_]+(?:\.[a-z0-9_]+)*", path):
        raise ValueError(f"Invalid evidence path: {path}")
    value: Any = input_payload
    for segment in path.split("."):
        if not isinstance(value, dict) or segment not in value:
            raise ValueError(f"Evidence path does not exist: {path}")
        value = value[segment]
    return value


def _numeric_evidence_values(value: Any) -> list[float]:
    if isinstance(value, bool):
        return []
    if isinstance(value, (int, float)):
        return [float(value)]
    if isinstance(value, dict):
        values: list[float] = []
        for child in value.values():
            values.extend(_numeric_evidence_values(child))
        return values
    if isinstance(value, list):
        values = []
        for child in value:
            values.extend(_numeric_evidence_values(child))
        return values
    return []


def _matching_record_paths(
    value: Any,
    record: tuple[int, ...],
    *,
    prefix: str = "",
) -> list[str]:
    matches: list[str] = []
    if isinstance(value, dict):
        candidate = (
            int(value.get("wins", -1)),
            int(value.get("losses", -1)),
            *(
                (int(value.get("ties", -1)),)
                if len(record) == 3
                else ()
            ),
        )
        if candidate == record and prefix:
            matches.append(prefix)
        for key, child in value.items():
            path = f"{prefix}.{key}" if prefix else str(key)
            matches.extend(
                _matching_record_paths(child, record, prefix=path)
            )
    elif isinstance(value, list):
        for index, child in enumerate(value):
            path = f"{prefix}.{index}" if prefix else str(index)
            matches.extend(
                _matching_record_paths(child, record, prefix=path)
            )
    return matches


def _validate_claim_numbers(
    claim: str,
    paths: list[str],
    evidence: list[Any],
    input_payload: dict[str, Any],
) -> None:
    normalized_claim = claim.replace("−", "-").replace("–", "-")
    remaining = normalized_claim
    for match in list(
        re.finditer(r"\b(\d+)-(\d+)(?:-(\d+))?\b", normalized_claim)
    ):
        record = tuple(
            int(group) for group in match.groups() if group is not None
        )
        if not any(
            isinstance(value, dict)
            and (
                int(value.get("wins", -1)),
                int(value.get("losses", -1)),
                *(
                    (int(value.get("ties", -1)),)
                    if len(record) == 3
                    else ()
                ),
            )
            == record
            for value in evidence
        ):
            candidates = _matching_record_paths(input_payload, record)
            raise ValueError(
                f"Claim record {match.group(0)} is absent from cited evidence; "
                f"candidate paths: {candidates[:8]}"
            )
        remaining = remaining.replace(match.group(0), " ", 1)
    numeric_values = [
        number for value in evidence for number in _numeric_evidence_values(value)
    ]
    path_numbers = [
        float(number)
        for path in paths
        for number in re.findall(r"\d+", path)
    ]
    for token in re.findall(
        r"(?<![\w])[-+]?(?:\d+(?:\.\d+)?|\.\d+)%?(?![\w])",
        remaining,
    ):
        is_percent = token.endswith("%")
        claimed = float(token.rstrip("%"))
        candidates = (
            [number * 100 for number in numeric_values if 0 <= number <= 1]
            if is_percent
            else [*numeric_values, *path_numbers]
        )
        decimals = len(token.rstrip("%").partition(".")[2])
        tolerance = 0.5 * (10 ** -decimals) if decimals else 0.5
        if not any(
            abs(candidate - claimed) <= tolerance + 1e-9
            for candidate in candidates
        ):
            raise ValueError(
                f"Claim number {token} is absent from cited evidence"
            )


def _complete_unique_record_paths(
    claim: str,
    paths: list[str],
    input_payload: dict[str, Any],
) -> list[str]:
    completed = list(paths)
    normalized_claim = claim.replace("−", "-").replace("–", "-")
    away = str(input_payload["game"]["away_team"])
    home = str(input_payload["game"]["home_team"])
    claim_lower = claim.lower()

    def mentions(team: str) -> bool:
        aliases = {team.lower(), team.rsplit(" ", 1)[-1].lower()}
        return any(alias in claim_lower for alias in aliases)

    for match in re.finditer(
        r"\b(\d+)-(\d+)(?:-(\d+))?\b",
        normalized_claim,
    ):
        record = tuple(
            int(group) for group in match.groups() if group is not None
        )
        candidates = _matching_record_paths(input_payload, record)
        if len(candidates) != 1 or candidates[0] in completed:
            continue
        candidate = candidates[0]
        is_matching_team = (
            ".away_team." in candidate
            and mentions(away)
            and not mentions(home)
        ) or (
            ".home_team." in candidate
            and mentions(home)
            and not mentions(away)
        )
        is_matching_cohort = (
            ".divisional_home_side_meeting_cohorts.nfl." in candidate
            and "nfl-wide home-side" in claim_lower
        ) or (
            ".divisional_home_side_meeting_cohorts.division." in candidate
            and f"{input_payload['historical_data']['divisional_home_side_meeting_cohorts']['division']['name'].lower()} home-side"
            in claim_lower
        ) or (
            ".divisional_home_side_meeting_cohorts.opponent_pair." in candidate
            and "opponent-pair home-side" in claim_lower
        )
        if is_matching_team or is_matching_cohort:
            completed.append(candidate)
    return completed


def _normalize_cited_claim(
    value: Any,
    input_payload: dict[str, Any],
    *,
    role: str,
) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError(f"{role} must be a cited claim object")
    claim = str(value.get("claim") or "").strip()
    paths = value.get("evidence_paths")
    if not claim:
        raise ValueError(f"{role}.claim is required")
    if not isinstance(paths, list) or not paths or not all(
        isinstance(path, str) and path.strip() for path in paths
    ):
        raise ValueError(f"{role}.evidence_paths must be non-empty")
    cleaned_paths = [
        re.split(r"\s+\(", path.strip(), maxsplit=1)[0]
        for path in paths
    ]
    normalized_paths = _complete_unique_record_paths(
        claim,
        cleaned_paths,
        input_payload,
    )
    resolved = [
        _resolve_evidence_path(input_payload, path)
        for path in normalized_paths
    ]
    _validate_claim_numbers(
        claim,
        normalized_paths,
        resolved,
        input_payload,
    )
    if re.search(
        r"\b(?:early[- ]season|season opener|opening week)\b",
        claim,
        re.IGNORECASE,
    ):
        raise ValueError(
            f"{role} treats pair meeting number as season timing"
        )
    superlative_claim = re.sub(
        r"\bat best\b",
        "",
        claim,
        flags=re.IGNORECASE,
    )
    if re.search(
        r"\b(?:best|worst|strongest|weakest)\b",
        superlative_claim,
        re.IGNORECASE,
    ):
        raise ValueError(
            f"{role} uses an unnecessary superlative"
        )
    if re.search(
        r"\b(?:preparation|preparedness|readiness)\b",
        claim,
        re.IGNORECASE,
    ):
        raise ValueError(
            f"{role} infers an unsupported team process"
        )
    team_names = {
        str(input_payload["game"]["away_team"]),
        str(input_payload["game"]["home_team"]),
    }
    if any(team.lower() in claim.lower() for team in team_names) and all(
        path.startswith(
            "historical_data.divisional_home_side_meeting_cohorts."
        )
        for path in normalized_paths
    ):
        raise ValueError(
            f"{role} attributes home-side cohort evidence to a named team"
        )
    if re.search(
        r"\b(?:overall team|overall quality|better team|stronger team)\b",
        claim,
        re.IGNORECASE,
    ) and not any(path.endswith(".all_games") for path in normalized_paths):
        raise ValueError(f"{role} makes an overall-team claim without all_games")
    if role.startswith("no_signal") and not all(
        value is None
        or (
            isinstance(value, dict)
            and int(value.get("games", -1)) == 0
        )
        for value in resolved
    ):
        raise ValueError(f"{role} cites evidence with a nonzero sample")
    return {
        "claim": claim,
        "evidence": [
            {"path": path, "value": resolved_value}
            for path, resolved_value in zip(normalized_paths, resolved)
        ],
    }


def _render_cited_opinion(
    opinion: dict[str, Any],
    *,
    away_team: str,
    home_team: str,
) -> str:
    home_probability = float(opinion["home_win_probability"])
    stars = "★" * int(opinion["confidence_stars"])

    def section(title: str, claims: list[dict[str, Any]]) -> list[str]:
        return [
            title,
            *(
                [f"- {item['claim']}" for item in claims]
                if claims
                else ["- None."]
            ),
        ]

    lines = [
        "Pick",
        (
            f"- {opinion['predicted_winner']} over "
            f"{home_team if opinion['predicted_winner'] == away_team else away_team}, "
            f"{away_team} {opinion['predicted_away_score']}, "
            f"{home_team} {opinion['predicted_home_score']}, "
            f"{home_probability:.0%} home win probability, "
            f"expected home margin {float(opinion['expected_home_margin']):+.1f}, "
            f"{stars}"
        ),
        "",
        *section("Why the pick", opinion["supporting_factors"]),
        "",
        *section("Why it may be wrong", opinion["counterarguments"]),
        "",
        *section("No signal", opinion["no_signal_factors"]),
        "",
        "Discarded considerations",
        *[
            f"- {item}"
            for item in opinion["discarded_considerations"]
        ],
        "",
        "Conclusion",
        opinion["thesis"],
    ]
    return "\n".join(lines)


def _normalize_cited_opinion(
    opinion: dict[str, Any],
    input_payload: dict[str, Any],
) -> dict[str, Any]:
    normalized = dict(opinion)
    thesis = _normalize_cited_claim(
        opinion.get("thesis"),
        input_payload,
        role="thesis",
    )
    normalized["thesis"] = thesis["claim"]
    normalized["thesis_citation"] = thesis
    for field in (
        "supporting_factors",
        "counterarguments",
        "no_signal_factors",
    ):
        values = opinion.get(field)
        if not isinstance(values, list):
            raise ValueError(f"{field} must be a list")
        normalized[field] = [
            _normalize_cited_claim(
                value,
                input_payload,
                role=f"{field}[{index}]",
            )
            for index, value in enumerate(values)
        ]
    discarded = opinion.get("discarded_considerations")
    if not isinstance(discarded, list) or not all(
        isinstance(value, str) and value.strip() for value in discarded
    ):
        raise ValueError(
            "discarded_considerations must contain non-empty strings"
        )
    if any(re.search(r"\d", value) for value in discarded):
        raise ValueError(
            "discarded_considerations cannot contain uncited numeric claims"
        )
    normalized["full_opinion"] = _render_cited_opinion(
        normalized,
        away_team=str(input_payload["game"]["away_team"]),
        home_team=str(input_payload["game"]["home_team"]),
    )
    return normalized


_TEAM_EVIDENCE_LABELS = {
    "all_games": "all games",
    "division_games": "divisional games",
    "non_division_games": "non-divisional games",
    "division_as_home": "divisional home games",
    "division_as_away": "divisional away games",
    "conference_non_division": "conference non-divisional games",
    "non_conference": "non-conference games",
    "against_current_opponent": "games against the current opponent",
    "against_current_opponent_as_home": (
        "home games against the current opponent"
    ),
    "against_current_opponent_as_away": (
        "away games against the current opponent"
    ),
    "division_meeting_1": "first annual divisional meetings",
    "division_meeting_2": "second annual divisional meetings",
}


def _sample_card(label: str, value: dict[str, Any]) -> str:
    games = int(value["games"])
    if not games:
        return f"{label}: no sample (0 games)"
    record = f"{value['wins']}-{value['losses']}"
    if int(value["ties"]):
        record += f"-{value['ties']}"
    return (
        f"{label}: {record}, {float(value['win_rate']):.1%} win rate, "
        f"{float(value['average_margin']):+.2f} average margin "
        f"({games} games)"
    )


def _evidence_card(
    path: str,
    value: Any,
    input_payload: dict[str, Any],
) -> str:
    parts = path.split(".")
    if len(parts) >= 3 and parts[:2] == ["historical_data", "away_team"]:
        team = str(input_payload["game"]["away_team"])
        key = parts[2]
    elif len(parts) >= 3 and parts[:2] == ["historical_data", "home_team"]:
        team = str(input_payload["game"]["home_team"])
        key = parts[2]
    else:
        team = ""
        key = ""
    if key in _TEAM_EVIDENCE_LABELS and len(parts) == 3:
        return _sample_card(
            f"{team} — {_TEAM_EVIDENCE_LABELS[key]}",
            value,
        )
    if (
        key == "current_opponent_series"
        and len(parts) == 4
        and parts[3].startswith("second_meeting_after_first_")
    ):
        result = parts[3].removeprefix("second_meeting_after_first_")
        return _sample_card(
            f"{team} — second meetings after a first-meeting {result}",
            value,
        )
    if key == "current_opponent_series" and len(parts) == 3:
        return (
            f"{team} — completed opponent-pair seasons: "
            f"{value['complete_two_game_seasons']}; sweeps "
            f"{value['team_sweeps']}; times swept {value['team_swept']}; "
            f"splits or ties {value['season_splits_or_ties']}"
        )
    cohort_prefix = (
        "historical_data.divisional_home_side_meeting_cohorts."
    )
    if path.startswith(cohort_prefix):
        meeting = parts[-1].removeprefix("meeting_")
        if ".nfl." in path:
            label = f"NFL-wide home-side meeting {meeting}"
        elif ".division." in path:
            division = input_payload["historical_data"][
                "divisional_home_side_meeting_cohorts"
            ]["division"]["name"]
            label = f"{division} home-side meeting {meeting}"
        else:
            label = f"Opponent-pair home-side meeting {meeting}"
        return _sample_card(label, value)
    if path == "current_season_prior_meeting":
        if value is None:
            return "Current-season prior meeting: none"
        return (
            f"Current-season prior meeting: {value['away_team']} "
            f"{value['away_score']}, {value['home_team']} "
            f"{value['home_score']}"
        )
    raise ValueError(f"Evidence path is not selectable: {path}")


def _evidence_favored_side(
    path: str,
    value: Any,
    input_payload: dict[str, Any],
) -> str | None:
    if path == "current_season_prior_meeting" and value is not None:
        winner = str(value["winner"])
        if winner == str(input_payload["game"]["home_team"]):
            return "home"
        if winner == str(input_payload["game"]["away_team"]):
            return "away"
        return None
    if not isinstance(value, dict):
        return None
    if "games" in value:
        if not int(value["games"]):
            return None
        win_rate = float(value["win_rate"])
        margin = float(value["average_margin"])
        if win_rate > 0.5 and margin > 0:
            evidence_side = "sample"
        elif win_rate < 0.5 and margin < 0:
            evidence_side = "opponent"
        else:
            return None
        if ".divisional_home_side_meeting_cohorts." in path:
            sample_side = "home"
        elif path.startswith("historical_data.away_team."):
            sample_side = "away"
        elif path.startswith("historical_data.home_team."):
            sample_side = "home"
        else:
            return None
        if evidence_side == "sample":
            return sample_side
        return "away" if sample_side == "home" else "home"
    if path.endswith(".current_opponent_series"):
        sweeps = int(value["team_sweeps"])
        swept = int(value["team_swept"])
        if sweeps == swept:
            return None
        sample_side = (
            "away"
            if path.startswith("historical_data.away_team.")
            else "home"
        )
        if sweeps > swept:
            return sample_side
        return "away" if sample_side == "home" else "home"
    return None


def _normalize_evidence_card_opinion(
    opinion: dict[str, Any],
    input_payload: dict[str, Any],
) -> dict[str, Any]:
    normalized = dict(opinion)
    confidence_label = {
        1: "very low-confidence",
        2: "low-confidence",
        3: "moderate-confidence",
        4: "high-confidence",
        5: "very high-confidence",
    }.get(int(opinion.get("confidence_stars") or 0))
    if confidence_label is None:
        raise ValueError("confidence_stars must be an integer from 1 through 5")
    thesis = (
        f"The selected divisional evidence yields a {confidence_label} lean "
        f"toward {opinion.get('predicted_winner')}, with conflicting evidence "
        "retained as counterarguments."
    )
    normalized["thesis"] = thesis
    normalized["thesis_citation"] = {
        "claim": thesis,
        "evidence": [],
    }
    selected: set[str] = set()
    supporting: list[dict[str, Any]] = []
    counterarguments: list[dict[str, Any]] = []
    predicted_side = (
        "home"
        if str(opinion.get("predicted_winner"))
        == str(input_payload["game"]["home_team"])
        else "away"
    )
    paths = opinion.get("evidence_paths")
    if not isinstance(paths, list) or not paths or not all(
        isinstance(path, str) and path.strip() for path in paths
    ):
        raise ValueError("evidence_paths must be a non-empty list")
    for path in paths:
        path = path.strip()
        if path in selected:
            raise ValueError(f"Evidence path selected more than once: {path}")
        selected.add(path)
        value = _resolve_evidence_path(input_payload, path)
        is_no_signal = value is None or (
            isinstance(value, dict) and int(value.get("games", -1)) == 0
        )
        if is_no_signal:
            raise ValueError(f"evidence_paths contains no-signal path: {path}")
        card = {
            "claim": _evidence_card(path, value, input_payload),
            "evidence": [{"path": path, "value": value}],
        }
        favored_side = _evidence_favored_side(path, value, input_payload)
        if favored_side == predicted_side:
            supporting.append(card)
        else:
            counterarguments.append(card)
    if not supporting or not counterarguments:
        raise ValueError(
            "Selected evidence must produce support and counterarguments"
        )
    normalized["supporting_factors"] = supporting
    normalized["counterarguments"] = counterarguments

    paths = opinion.get("no_signal_evidence_paths")
    if not isinstance(paths, list) or not all(
        isinstance(path, str) and path.strip() for path in paths
    ):
        raise ValueError("no_signal_evidence_paths must be a list")
    cards = []
    for path in paths:
        path = path.strip()
        if path in selected:
            raise ValueError(f"Evidence path selected more than once: {path}")
        selected.add(path)
        value = _resolve_evidence_path(input_payload, path)
        is_no_signal = value is None or (
            isinstance(value, dict) and int(value.get("games", -1)) == 0
        )
        if not is_no_signal:
            raise ValueError(f"No-signal path has usable evidence: {path}")
        cards.append(
            {
                "claim": _evidence_card(path, value, input_payload),
                "evidence": [{"path": path, "value": value}],
            }
        )
    normalized["no_signal_factors"] = cards
    cohorts = input_payload["historical_data"].get(
        "divisional_home_side_meeting_cohorts"
    )
    meeting = input_payload["game"].get("division_meeting_number")
    if cohorts and meeting in {1, 2}:
        required = {
            f"historical_data.divisional_home_side_meeting_cohorts."
            f"{scope}.meeting_{meeting}"
            for scope in ("nfl", "division", "opponent_pair")
        }
        missing = required - selected
        if missing:
            raise ValueError(
                f"Missing required home-side cohort paths: {sorted(missing)}"
            )
    discarded = opinion.get("discarded_considerations")
    if not isinstance(discarded, list) or not all(
        isinstance(value, str) and value.strip() for value in discarded
    ):
        raise ValueError(
            "discarded_considerations must contain non-empty strings"
        )
    normalized["discarded_considerations"] = [
        value for value in discarded if not re.search(r"\d", value)
    ]
    normalized["full_opinion"] = _render_cited_opinion(
        normalized,
        away_team=str(input_payload["game"]["away_team"]),
        home_team=str(input_payload["game"]["home_team"]),
    )
    return normalized


def _normalize_ak_opinion(
    opinion: dict[str, Any],
    input_payload: dict[str, Any],
) -> dict[str, Any]:
    normalized = dict(opinion)
    catalog = {
        str(item["id"]): item
        for item in input_payload["evidence_catalog"]
    }

    def normalize_pick(field: str) -> dict[str, Any]:
        raw = opinion.get(field)
        if not isinstance(raw, dict):
            raise ValueError(f"{field} must be an object")
        selection = str(raw.get("selection") or "")
        stars = raw.get("confidence_stars")
        if (
            isinstance(stars, bool)
            or not isinstance(stars, int)
            or not 1 <= stars <= 5
        ):
            raise ValueError(
                f"{field}.confidence_stars must be an integer from 1 through 5"
            )
        evidence_ids = raw.get("evidence_ids")
        counter_ids = raw.get("counterargument_ids")
        if not isinstance(evidence_ids, list) or not all(
            isinstance(value, str) and value in catalog
            for value in evidence_ids
        ):
            raise ValueError(f"{field}.evidence_ids contains an unknown ID")
        if not isinstance(counter_ids, list) or not all(
            isinstance(value, str) and value in catalog
            for value in counter_ids
        ):
            raise ValueError(
                f"{field}.counterargument_ids contains an unknown ID"
            )
        for evidence_id in evidence_ids + counter_ids:
            if catalog[evidence_id]["scope"] not in {field, "both"}:
                raise ValueError(
                    f"Evidence {evidence_id} does not apply to {field}"
                )
        if selection == "PASS":
            if raw.get("line") is not None or stars != 1 or evidence_ids:
                raise ValueError(
                    f"{field} PASS requires null line, one star, and no evidence"
                )
            if not counter_ids:
                raise ValueError(f"{field} PASS requires a counterargument")
        elif field == "side":
            if selection not in {
                str(input_payload["game"]["away_team"]),
                str(input_payload["game"]["home_team"]),
            }:
                raise ValueError("side.selection must be a game team or PASS")
            market = input_payload["markets"]["current_latest"]
            expected_line = (
                market["away_spread"]
                if selection == input_payload["game"]["away_team"]
                else market["home_spread"]
            )
            if expected_line is None or float(raw.get("line")) != float(
                expected_line
            ):
                raise ValueError("side.line must equal the current supplied line")
            if not evidence_ids or not counter_ids:
                raise ValueError(
                    "Non-pass side requires evidence and counterarguments"
                )
        else:
            if selection not in {"Over", "Under"}:
                raise ValueError("total.selection must be Over, Under, or PASS")
            expected_line = input_payload["markets"]["current_latest"]["total"]
            if expected_line is None or float(raw.get("line")) != float(
                expected_line
            ):
                raise ValueError("total.line must equal the current supplied total")
            if not evidence_ids or not counter_ids:
                raise ValueError(
                    "Non-pass total requires evidence and counterarguments"
                )
        return {
            "selection": selection,
            "line": raw.get("line"),
            "confidence_stars": stars,
            "evidence_ids": evidence_ids,
            "counterargument_ids": counter_ids,
            "evidence": [catalog[value]["text"] for value in evidence_ids],
            "counterarguments": [
                catalog[value]["text"] for value in counter_ids
            ],
        }

    side = normalize_pick("side")
    total = normalize_pick("total")
    for field, pick in (("side", side), ("total", total)):
        uses_wnba = any(
            evidence_id.startswith("wnba_")
            for evidence_id in pick["evidence_ids"]
        )
        if not uses_wnba:
            continue
        prior_leg = input_payload["cross_sport_prior"][field]
        nfl_games = int(prior_leg["matching_nfl_bucket_games"])
        expires = int(
            input_payload["cross_sport_prior"]["expires_at_nfl_bucket_size"]
        )
        if nfl_games >= expires:
            raise ValueError(
                f"WNBA prior has expired for the {field} bucket"
            )
        if nfl_games == 0 and pick["confidence_stars"] > 1:
            raise ValueError(
                "WNBA prior cannot be the zero-NFL-sample basis for "
                "confidence above one star"
            )
        if pick["confidence_stars"] > 2:
            raise ValueError(
                "A recommendation using the WNBA prior cannot exceed two stars"
            )
    projection = input_payload["ak_submission"]["projection"]
    gaps = input_payload["market_gaps"]
    side_label = (
        "PASS"
        if side["selection"] == "PASS"
        else f"{side['selection']} {float(side['line']):+g}"
    )
    total_label = (
        "PASS"
        if total["selection"] == "PASS"
        else f"{total['selection']} {float(total['line']):g}"
    )
    normalized["side"] = side
    normalized["total"] = total
    normalized["confidence_stars"] = max(
        side["confidence_stars"], total["confidence_stars"]
    )
    normalized["pick_market"] = "side_and_total"
    normalized["pick_side"] = f"{side_label} | {total_label}"
    normalized["thesis"] = (
        f"AK calibration: side {side_label} "
        f"{'★' * side['confidence_stars']}; total {total_label} "
        f"{'★' * total['confidence_stars']}."
    )
    normalized["supporting_factors"] = (
        [f"Side: {value}" for value in side["evidence"]]
        + [f"Total: {value}" for value in total["evidence"]]
        or ["Both calibrated recommendations are PASS."]
    )
    normalized["counterarguments"] = (
        [f"Side: {value}" for value in side["counterarguments"]]
        + [f"Total: {value}" for value in total["counterarguments"]]
    )
    normalized["no_signal_factors"] = [
        catalog[key]["text"]
        for key in ("nfl_side_bucket", "nfl_total_bucket")
        if "has 0 resolved" in catalog[key]["text"]
    ]
    discarded = opinion.get("discarded_considerations")
    if not isinstance(discarded, list) or not all(
        isinstance(value, str) and value.strip() for value in discarded
    ):
        raise ValueError(
            "discarded_considerations must contain non-empty strings"
        )
    normalized["discarded_considerations"] = discarded

    def bullets(values: list[str]) -> str:
        return "\n".join(f"- {value}" for value in values) or "- None"

    normalized["full_opinion"] = (
        "AK projection\n"
        f"- {input_payload['game']['away_team']} "
        f"{projection['away_score']}, {input_payload['game']['home_team']} "
        f"{projection['home_score']}\n\n"
        "Market gaps\n"
        f"- Side bucket: {gaps['side_gap_bucket']}; margin gap: "
        f"{gaps['side_margin_gap']}\n"
        f"- Total bucket: {gaps['total_gap_bucket']}; total gap: "
        f"{gaps['total_gap']:+g}\n\n"
        f"Side pick\n- {side_label} "
        f"{'★' * side['confidence_stars']}\n\n"
        f"Why the side\n{bullets(side['evidence'])}\n\n"
        f"Why the side may be wrong\n{bullets(side['counterarguments'])}\n\n"
        f"Total pick\n- {total_label} "
        f"{'★' * total['confidence_stars']}\n\n"
        f"Why the total\n{bullets(total['evidence'])}\n\n"
        "Why the total may be wrong\n"
        f"{bullets(total['counterarguments'])}\n\n"
        "NFL calibration\n"
        f"- Eligible resolved predictions: "
        f"{input_payload['nfl_calibration']['eligible_predictions']}\n\n"
        "WNBA cold-start prior\n"
        f"- {catalog['wnba_prior_cap']['text']}\n"
        f"- {catalog['wnba_side_prior']['text']}\n"
        f"- {catalog['wnba_total_prior']['text']}\n\n"
        f"No signal\n{bullets(normalized['no_signal_factors'])}\n\n"
        "Discarded considerations\n"
        f"{bullets(discarded)}\n\n"
        f"Conclusion\n{normalized['thesis']}"
    )
    normalized["side_pick_json"] = _canonical_json(side)
    normalized["total_pick_json"] = _canonical_json(total)
    normalized["calibration_summary_json"] = _canonical_json(
        input_payload["nfl_calibration"]
    )
    return normalized


async def _fact_check_nondeterministic_analysis(
    claims: Any,
    input_payload: dict[str, Any],
    *,
    model: str,
    reasoning_effort: str,
    create_fn: CreateFn,
) -> tuple[str, str, str, str]:
    if not isinstance(claims, list) or not claims or not all(
        isinstance(claim, str) and claim.strip() for claim in claims
    ):
        raise ValueError(
            "nondeterministic_analysis must be a non-empty string list"
        )
    normalized_claims = [claim.strip() for claim in claims]
    raw_analysis = "\n".join(normalized_claims)
    if len(raw_analysis) >= MAX_SHEET_CELL_CHARS:
        raise ValueError("nondeterministic analysis exceeds the Sheet limit")
    prompt = FACTUALITY_PROMPT_PATH.read_text(encoding="utf-8")
    response = await create_fn(
        model=model,
        max_tokens=4000,
        output_config={"effort": reasoning_effort},
        system=prompt,
        messages=[
            {
                "role": "user",
                "content": (
                    "Classify these exact claims against the supplied input.\n"
                    f"Input:\n{_canonical_json(input_payload)}\n"
                    f"Claims:\n{_canonical_json(normalized_claims)}"
                ),
            }
        ],
    )
    raw_response = str(response.content[0].text).strip()
    report = _parse_response(raw_response)
    results = report.get("claims")
    if not isinstance(results, list) or len(results) != len(normalized_claims):
        raise ValueError("Factuality report must classify every claim once")
    usable: list[str] = []
    normalized_results: list[dict[str, Any]] = []
    seen: set[int] = set()
    for result in results:
        if not isinstance(result, dict):
            raise ValueError("Factuality claim result must be an object")
        index = result.get("index")
        if isinstance(index, bool) or not isinstance(index, int):
            raise ValueError("Factuality claim index must be an integer")
        if index < 0 or index >= len(normalized_claims) or index in seen:
            raise ValueError("Factuality claim index is invalid or duplicated")
        seen.add(index)
        classification = str(result.get("classification") or "")
        if classification not in {
            "supported",
            "reasonable_inference",
            "unsupported",
        }:
            raise ValueError("Invalid factuality classification")
        evidence: list[dict[str, Any]] = []
        if classification != "unsupported":
            try:
                cited = _normalize_cited_claim(
                    {
                        "claim": normalized_claims[index],
                        "evidence_paths": result.get("evidence_paths"),
                    },
                    input_payload,
                    role=f"factuality.claims[{index}]",
                )
            except ValueError as exc:
                classification = "unsupported"
                result["reason"] = (
                    f"{str(result.get('reason') or '').strip()} "
                    f"Validator rejected citations: {exc}"
                ).strip()
            else:
                evidence = cited["evidence"]
                prefix = (
                    ""
                    if classification == "supported"
                    else "Interpretation: "
                )
                usable.append(f"{prefix}{normalized_claims[index]}")
        normalized_results.append(
            {
                "index": index,
                "text": normalized_claims[index],
                "classification": classification,
                "evidence": evidence,
                "reason": str(result.get("reason") or "").strip(),
            }
        )
    normalized_results.sort(key=lambda item: item["index"])
    unsupported_count = sum(
        item["classification"] == "unsupported"
        for item in normalized_results
    )
    status = (
        "rejected"
        if not usable
        else "partially_verified"
        if unsupported_count
        else "verified"
    )
    usable_text = "\n".join(f"- {claim}" for claim in usable)
    factuality_json = _canonical_json(
        {
            "analysis_sha256": _sha256_text(raw_analysis),
            "model": model,
            "prompt_path": str(FACTUALITY_PROMPT_PATH.relative_to(ROOT)),
            "prompt_sha256": _sha256_text(prompt),
            "raw_response": raw_response,
            "claims": normalized_results,
        }
    )
    return raw_analysis, usable_text, status, factuality_json


def _validate_divisional_cohort_coverage(
    opinion: dict[str, Any],
    input_payload: dict[str, Any],
) -> None:
    cohorts = input_payload["historical_data"].get(
        "divisional_home_side_meeting_cohorts"
    )
    meeting_number = input_payload["game"].get("division_meeting_number")
    if not cohorts or meeting_number not in {1, 2}:
        return
    meeting_key = f"meeting_{meeting_number}"
    required = (
        ("NFL-wide home-side", cohorts["nfl"][meeting_key]),
        (
            f"{cohorts['division']['name']} home-side",
            cohorts["division"][meeting_key],
        ),
        (
            "Opponent-pair home-side",
            cohorts["opponent_pair"][meeting_key],
        ),
    )
    full_opinion = str(opinion.get("full_opinion") or "")
    for label, sample in required:
        record = f"{sample['wins']}-{sample['losses']}"
        if int(sample["ties"]):
            record += f"-{sample['ties']}"
        if not re.search(
            rf"{re.escape(label)}[^\n]{{0,160}}\b{re.escape(record)}\b",
            full_opinion,
            re.IGNORECASE,
        ):
            raise ValueError(
                f"full_opinion must include {label} record {record}"
            )


def validate_opinion(
    opinion: dict[str, Any],
    *,
    away_team: str,
    home_team: str,
    schedule_input: dict[str, Any] | None = None,
) -> None:
    cited_schema = bool(
        schedule_input is not None
        and schedule_input.get("input_profile") == "divisional"
        and isinstance(opinion.get("thesis_citation"), dict)
    )
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
        valid_items = (
            all(
                isinstance(item, dict)
                and str(item.get("claim") or "").strip()
                and isinstance(item.get("evidence"), list)
                and item["evidence"]
                for item in value
            )
            if cited_schema and isinstance(value, list)
            else all(
                isinstance(item, str) and item.strip()
                for item in value or []
            )
        )
        if not isinstance(value, list) or not value or not valid_items:
            raise ValueError(
                f"{field} must be a non-empty list of non-empty strings"
            )
    for field in ("no_signal_factors", "discarded_considerations"):
        value = opinion.get(field)
        if field == "no_signal_factors" and cited_schema:
            valid_items = isinstance(value, list) and all(
                isinstance(item, dict)
                and str(item.get("claim") or "").strip()
                and isinstance(item.get("evidence"), list)
                and item["evidence"]
                for item in value
            )
        else:
            valid_items = isinstance(value, list) and all(
                isinstance(item, str) and item.strip() for item in value
            )
        if not valid_items:
            raise ValueError(f"{field} must be a list of non-empty strings")
    if (
        schedule_input is not None
        and schedule_input.get("input_profile") == "divisional"
    ):
        _validate_divisional_cohort_coverage(opinion, schedule_input)
        unsupported_labels = re.search(
            r"\b(?:elite|dominant|dominance|dominating|commanding|"
            r"statistically significant|significant(?:ly)?|"
            r"outstanding)\b",
            _canonical_json(opinion),
            re.IGNORECASE,
        )
        if unsupported_labels:
            raise ValueError(
                "Opinion uses unsupported qualitative label: "
                f"{unsupported_labels.group(0)}"
            )
        full_opinion = str(opinion["full_opinion"])
        measured_label = re.search(
            r"\b(?:markedly|strong(?:er|est)?|superior(?:ity)?)\b",
            full_opinion,
            re.IGNORECASE,
        )
        if measured_label and not (
            len(re.findall(r"\b\d+-\d+(?:-\d+)?\b", full_opinion)) >= 2
            and re.search(r"\b\d+\s+games?\b", full_opinion, re.IGNORECASE)
        ):
            raise ValueError(
                "Measured comparison lacks records and sample sizes: "
                f"{measured_label.group(0)}"
            )
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
    schedule: list[dict[str, Any]] | None = None,
    current_season_results: list[dict[str, Any]] | None = None,
    leans: list[dict[str, Any]] | None = None,
    line_snapshots: list[dict[str, Any]] | None = None,
    ak_user_id: str | None = None,
    store: MoeOpinionStore,
    model: str | None = None,
    create_fn: CreateFn = _claude_create_with_retry,
    generation_backend: str = "anthropic_api",
    repair_attempts: int = 0,
    _repair_response: str = "",
    _repair_error: str = "",
) -> dict[str, Any]:
    expert = load_expert(expert_id)
    if expert["input_profile"] == "schedule_only":
        input_payload = build_schedule_input(game, history)
    elif expert["input_profile"] == "divisional":
        if schedule is None:
            raise ValueError("Divisional expert requires the current schedule")
        input_payload = build_divisional_input(
            game,
            history,
            schedule,
            current_season_results,
        )
    elif expert["input_profile"] == "ak_calibration":
        if leans is None or not ak_user_id:
            raise ValueError("AK expert requires leans and ak_user_id")
        input_payload = build_ak_input(
            game,
            history,
            leans,
            line_snapshots,
            ak_user_id=ak_user_id,
        )
    else:
        raise NotImplementedError(
            f"Unsupported input profile: {expert['input_profile']}"
        )
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
    reasoning_effort = str(expert.get("reasoning_effort") or "").strip()
    if reasoning_effort not in {"low", "medium", "high", "xhigh", "max"}:
        raise ValueError(
            f"Invalid reasoning effort for expert {expert_id}: "
            f"{reasoning_effort or '<missing>'}"
        )
    if generation_backend not in {
        "anthropic_api",
        "agent_runtime",
        "copilot_subagent",
    }:
        raise ValueError(
            f"Unsupported MOE generation backend: {generation_backend}"
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
        "generation_backend": generation_backend,
        "generation_max_tokens": max_tokens,
        "generation_effort": reasoning_effort,
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
        "nondeterministic_analysis_raw": "",
        "nondeterministic_analysis_usable": "",
        "nondeterministic_factuality_status": "",
        "nondeterministic_factuality_json": "",
        "side_pick_json": "",
        "total_pick_json": "",
        "calibration_summary_json": "",
    }
    try:
        request = (
            f"Generate the {expert['name']} opinion from this exact input:\n"
            f"{input_json}"
        )
        if _repair_response:
            request += (
                "\n\nYour previous JSON response failed validation. Correct "
                "only the response so it satisfies the same prompt and input."
                f"\nValidation error: {_repair_error}"
                f"\nPrevious response:\n{_repair_response}"
            )
        response = await create_fn(
            model=selected_model,
            max_tokens=max_tokens,
            output_config={"effort": reasoning_effort},
            system=expert["prompt_text"],
            messages=[
                {
                    "role": "user",
                    "content": request,
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
        if int(expert["output_schema_version"]) == 3:
            opinion = _normalize_cited_opinion(opinion, input_payload)
        elif int(expert["output_schema_version"]) == 4:
            nondeterministic = await _fact_check_nondeterministic_analysis(
                opinion.get("nondeterministic_analysis"),
                input_payload,
                model=selected_model,
                reasoning_effort=reasoning_effort,
                create_fn=create_fn,
            )
            opinion = _normalize_evidence_card_opinion(
                opinion,
                input_payload,
            )
        elif int(expert["output_schema_version"]) == 5:
            opinion = _normalize_ak_opinion(opinion, input_payload)
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
                "pick_market": str(
                    opinion.get("pick_market") or "straight_up"
                ),
                "pick_side": str(
                    opinion.get("pick_side") or opinion["predicted_winner"]
                ),
                "thesis": str(opinion["thesis"]).strip(),
                "supporting_factors_json": _canonical_json(
                    (
                        {
                            "thesis": opinion["thesis_citation"],
                            "items": opinion["supporting_factors"],
                        }
                        if int(expert["output_schema_version"]) in {3, 4}
                        else opinion["supporting_factors"]
                    )
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
                "nondeterministic_analysis_raw": (
                    nondeterministic[0]
                    if int(expert["output_schema_version"]) == 4
                    else ""
                ),
                "nondeterministic_analysis_usable": (
                    nondeterministic[1]
                    if int(expert["output_schema_version"]) == 4
                    else ""
                ),
                "nondeterministic_factuality_status": (
                    nondeterministic[2]
                    if int(expert["output_schema_version"]) == 4
                    else ""
                ),
                "nondeterministic_factuality_json": (
                    nondeterministic[3]
                    if int(expert["output_schema_version"]) == 4
                    else ""
                ),
                "side_pick_json": str(
                    opinion.get("side_pick_json") or ""
                ),
                "total_pick_json": str(
                    opinion.get("total_pick_json") or ""
                ),
                "calibration_summary_json": str(
                    opinion.get("calibration_summary_json") or ""
                ),
                "generation_status": "valid",
            }
        )
        if row["nondeterministic_analysis_usable"]:
            row["full_opinion"] += (
                "\n\nNon-deterministic perspective\n"
                f"{row['nondeterministic_analysis_usable']}"
            )
        row["output_sha256"] = opinion_output_sha256(row)
    except Exception as exc:
        row["generation_status"] = "invalid"
        row["generation_error"] = f"{type(exc).__name__}: {exc}"
        row["review_status"] = "not_applicable"
        _persist_attempt(store, row)
        if (
            repair_attempts > 0
            and row["raw_response"]
            and isinstance(exc, ValueError)
        ):
            return await generate_opinion(
                expert_id=expert_id,
                game=game,
                history=history,
                schedule=schedule,
                current_season_results=current_season_results,
                leans=leans,
                line_snapshots=line_snapshots,
                ak_user_id=ak_user_id,
                store=store,
                model=selected_model,
                create_fn=create_fn,
                generation_backend=generation_backend,
                repair_attempts=repair_attempts - 1,
                _repair_response=row["raw_response"],
                _repair_error=row["generation_error"],
            )
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
    if row.get("generation_backend"):
        payload["generation_backend"] = str(row["generation_backend"])
    if row.get("generation_effort"):
        payload["generation_effort"] = str(row["generation_effort"])
    if any(
        row.get(field)
        for field in (
            "nondeterministic_analysis_raw",
            "nondeterministic_analysis_usable",
            "nondeterministic_factuality_status",
            "nondeterministic_factuality_json",
        )
    ):
        payload.update(
            {
                field: str(row.get(field) or "")
                for field in (
                    "nondeterministic_analysis_raw",
                    "nondeterministic_analysis_usable",
                    "nondeterministic_factuality_status",
                    "nondeterministic_factuality_json",
                )
            }
        )
    if any(
        row.get(field)
        for field in (
            "side_pick_json",
            "total_pick_json",
            "calibration_summary_json",
        )
    ):
        payload.update(
            {
                field: str(row.get(field) or "")
                for field in (
                    "side_pick_json",
                    "total_pick_json",
                    "calibration_summary_json",
                )
            }
        )
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
    for index, row in enumerate(visible):
        if index:
            lines.append("")
        if str(row.get("expert_id")) == "ak" and row.get("side_pick_json"):
            side = json.loads(str(row["side_pick_json"]))
            total = json.loads(str(row["total_pick_json"]))
            side_stars = "★" * int(side["confidence_stars"])
            total_stars = "★" * int(total["confidence_stars"])
            side_label = (
                "PASS"
                if side["selection"] == "PASS"
                else f"{side['selection']} {float(side['line']):+g}"
            )
            total_label = (
                "PASS"
                if total["selection"] == "PASS"
                else f"{total['selection']} {float(total['line']):g}"
            )
            lines.append(
                f"<b>{html.escape(str(row['expert_name']))}</b> "
                f"· <code>{html.escape(str(row['model']))}</code>"
            )
            lines.append(
                f"Side: {html.escape(side_label)} {side_stars} · "
                f"Total: {html.escape(total_label)} {total_stars} · "
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
            continue
        stars = "★" * int(row["confidence_stars"])
        winner = html.escape(str(row["predicted_winner"]))
        probability = float(row["home_win_probability"])
        winner_probability = (
            probability
            if str(row["predicted_winner"]) == home
            else 1 - probability
        )
        lines.append(
            f"<b>{html.escape(str(row['expert_name']))}</b> "
            f"· <code>{html.escape(str(row['model']))}</code>: {winner} "
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
        if str(row.get("expert_id")) == "ak" and row.get("side_pick_json"):
            side = json.loads(str(row["side_pick_json"]))
            total = json.loads(str(row["total_pick_json"]))
            side_label = (
                "PASS"
                if side["selection"] == "PASS"
                else f"{side['selection']} {float(side['line']):+g}"
            )
            total_label = (
                "PASS"
                if total["selection"] == "PASS"
                else f"{total['selection']} {float(total['line']):g}"
            )
            lines.append(
                f"<b>{html.escape(model)}</b> — "
                f"Side {html.escape(side_label)} "
                f"{'★' * int(side['confidence_stars'])} · "
                f"Total {html.escape(total_label)} "
                f"{'★' * int(total['confidence_stars'])} · "
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
            continue
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

    home_probability = float(row["home_win_probability"])
    margin = float(row["expected_home_margin"])
    away_score = int(row["predicted_away_score"])
    home_score = int(row["predicted_home_score"])
    if str(row.get("expert_id")) == "ak" and row.get("side_pick_json"):
        side = json.loads(str(row["side_pick_json"]))
        total = json.loads(str(row["total_pick_json"]))
        side_label = (
            "PASS"
            if side["selection"] == "PASS"
            else f"{side['selection']} {float(side['line']):+g}"
        )
        total_label = (
            "PASS"
            if total["selection"] == "PASS"
            else f"{total['selection']} {float(total['line']):g}"
        )
        heading = (
            f"🧠 <b>{html.escape(str(row['expert_name']))}</b>\n\n"
            f"<b>Side:</b> {html.escape(side_label)} "
            f"{'★' * int(side['confidence_stars'])}\n"
            f"<b>Total:</b> {html.escape(total_label)} "
            f"{'★' * int(total['confidence_stars'])}\n"
            f"<b>Predicted score:</b> "
            f"{html.escape(str(row['away_team']))} {away_score} — "
            f"{html.escape(str(row['home_team']))} {home_score}\n"
            f"<b>Home win probability:</b> {home_probability:.0%}\n"
            f"<b>Expected home margin:</b> {margin:+.1f}\n\n"
        )
    else:
        stars = "★" * int(row["confidence_stars"])
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
    effort = str(row.get("generation_effort") or "").strip()
    backend = str(row.get("generation_backend") or "").strip()
    execution = ""
    if effort:
        execution += f" · effort {html.escape(effort)}"
    if backend:
        execution += f" · {html.escape(backend)}"
    footer = (
        f"\n\n<i>Expert v{row['expert_version']} · "
        f"model {html.escape(str(row['model']))}{execution} · "
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
