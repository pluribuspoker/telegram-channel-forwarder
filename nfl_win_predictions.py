"""Backend data model and Sheet helpers for NFL season-win predictions."""

from __future__ import annotations

import statistics
from collections import Counter
from datetime import datetime, timezone
from typing import Any, Iterable
from zoneinfo import ZoneInfo

import gspread
import httpx

ET = ZoneInfo("America/New_York")
STANDINGS_URL = (
    "https://site.api.espn.com/apis/v2/sports/football/nfl/standings"
)

TEAM_ABBREVIATIONS = {
    "Arizona Cardinals": "ARI",
    "Atlanta Falcons": "ATL",
    "Baltimore Ravens": "BAL",
    "Buffalo Bills": "BUF",
    "Carolina Panthers": "CAR",
    "Chicago Bears": "CHI",
    "Cincinnati Bengals": "CIN",
    "Cleveland Browns": "CLE",
    "Dallas Cowboys": "DAL",
    "Denver Broncos": "DEN",
    "Detroit Lions": "DET",
    "Green Bay Packers": "GB",
    "Houston Texans": "HOU",
    "Indianapolis Colts": "IND",
    "Jacksonville Jaguars": "JAX",
    "Kansas City Chiefs": "KC",
    "Las Vegas Raiders": "LV",
    "Los Angeles Chargers": "LAC",
    "Los Angeles Rams": "LAR",
    "Miami Dolphins": "MIA",
    "Minnesota Vikings": "MIN",
    "New England Patriots": "NE",
    "New Orleans Saints": "NO",
    "New York Giants": "NYG",
    "New York Jets": "NYJ",
    "Philadelphia Eagles": "PHI",
    "Pittsburgh Steelers": "PIT",
    "San Francisco 49ers": "SF",
    "Seattle Seahawks": "SEA",
    "Tampa Bay Buccaneers": "TB",
    "Tennessee Titans": "TEN",
    "Washington Commanders": "WAS",
}

WIN_TOTAL_HEADERS = [
    "season",
    "team",
    "team_abbreviation",
    "bookmaker",
    "win_total",
    "over_price",
    "under_price",
    "captured_at_utc",
    "captured_at_et",
    "source",
]

TEAM_HISTORY_HEADERS = [
    "season",
    "team",
    "team_abbreviation",
    "conference",
    "division",
    "division_rank",
    "wins",
    "losses",
    "ties",
    "playoff_team",
    "next_season",
    "next_season_wins",
    "next_season_division_rank",
    "next_season_playoff_team",
    "win_change",
]

RANK_BENCHMARK_HEADERS = [
    "prior_division_rank",
    "sample_size",
    "average_next_season_wins",
    "median_next_season_wins",
    "stddev_next_season_wins",
    "minimum_next_season_wins",
    "maximum_next_season_wins",
    "average_win_change",
    "improved_win_rate",
    "next_season_playoff_rate",
    "next_season_division_title_rate",
    "cohorts",
]

PREDICTION_HEADERS = [
    "revision_id",
    "submitted_at_utc",
    "submitted_at_et",
    "telegram_user_id",
    "telegram_username",
    "telegram_display_name",
    "season",
    "team",
    "team_abbreviation",
    "predicted_wins",
    "market_win_total",
    "market_captured_at_et",
    "prior_season",
    "prior_division",
    "prior_division_rank",
    "actual_wins_at_submission",
    "actual_week_at_submission",
]

LATEST_PREDICTION_HEADERS = [
    "telegram_user_id",
    "telegram_username",
    "telegram_display_name",
    "season",
    "guess_count",
    "team",
    "team_abbreviation",
    "predicted_wins",
    "latest_revision_id",
    "latest_submitted_at_et",
    "market_win_total",
    "market_captured_at_et",
    "prior_season",
    "prior_division",
    "prior_division_rank",
    "actual_wins_at_latest_submission",
    "actual_week_at_latest_submission",
]

TAB_HEADERS = {
    "nfl_win_totals": WIN_TOTAL_HEADERS,
    "nfl_team_history": TEAM_HISTORY_HEADERS,
    "nfl_rank_benchmarks": RANK_BENCHMARK_HEADERS,
    "nfl_win_predictions": PREDICTION_HEADERS,
    "nfl_win_predictions_latest": LATEST_PREDICTION_HEADERS,
}


def _number(stats: list[dict[str, Any]], name: str) -> float:
    for stat in stats:
        if stat.get("name") == name:
            return float(stat.get("value") or 0)
    return 0


def parse_standings(payload: dict[str, Any], season: int) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for conference in payload.get("children") or []:
        conference_name = str(conference.get("name") or "")
        for division in conference.get("children") or []:
            division_name = str(division.get("name") or "")
            standings = division.get("standings") or {}
            entries = list(standings.get("entries") or [])
            entries.sort(
                key=lambda entry: (
                    _number(entry.get("stats") or [], "playoffSeed")
                    or 99
                )
            )
            for rank, entry in enumerate(
                entries, start=1
            ):
                team = str((entry.get("team") or {}).get("displayName") or "")
                stats = entry.get("stats") or []
                playoff_seed = _number(stats, "playoffSeed")
                rows.append(
                    {
                        "season": season,
                        "team": team,
                        "team_abbreviation": TEAM_ABBREVIATIONS.get(team, ""),
                        "conference": conference_name,
                        "division": division_name,
                        "division_rank": rank,
                        "wins": int(_number(stats, "wins")),
                        "losses": int(_number(stats, "losses")),
                        "ties": int(_number(stats, "ties")),
                        "playoff_team": playoff_seed > 0 and playoff_seed <= 7,
                    }
                )
    if len(rows) != 32:
        raise ValueError(
            f"ESPN standings for {season} contained {len(rows)} teams, not 32"
        )
    return rows


def fetch_standings(
    season: int, *, client: httpx.Client | None = None
) -> list[dict[str, Any]]:
    owns_client = client is None
    http = client or httpx.Client(timeout=30)
    try:
        response = http.get(
            STANDINGS_URL,
            params={"season": season, "level": 3},
        )
        response.raise_for_status()
        payload = response.json()
    finally:
        if owns_client:
            http.close()
    if not isinstance(payload, dict):
        raise ValueError("ESPN standings response must be an object")
    return parse_standings(payload, season)


def build_team_history(
    standings_by_season: dict[int, list[dict[str, Any]]],
) -> list[dict[str, Any]]:
    indexed = {
        (season, str(row["team"])): row
        for season, rows in standings_by_season.items()
        for row in rows
    }
    history: list[dict[str, Any]] = []
    for season in sorted(standings_by_season):
        for row in standings_by_season[season]:
            next_row = indexed.get((season + 1, str(row["team"])))
            history.append(
                {
                    **row,
                    "next_season": season + 1 if next_row else "",
                    "next_season_wins": (
                        next_row["wins"] if next_row else ""
                    ),
                    "next_season_division_rank": (
                        next_row["division_rank"] if next_row else ""
                    ),
                    "next_season_playoff_team": (
                        next_row["playoff_team"] if next_row else ""
                    ),
                    "win_change": (
                        int(next_row["wins"]) - int(row["wins"])
                        if next_row
                        else ""
                    ),
                }
            )
    return history


def build_rank_benchmarks(
    history: Iterable[dict[str, Any]],
) -> list[dict[str, Any]]:
    completed = [
        row for row in history if row.get("next_season_wins") != ""
    ]
    rows: list[dict[str, Any]] = []
    for rank in range(1, 5):
        sample = [
            row for row in completed if int(row["division_rank"]) == rank
        ]
        wins = [int(row["next_season_wins"]) for row in sample]
        changes = [int(row["win_change"]) for row in sample]
        rows.append(
            {
                "prior_division_rank": rank,
                "sample_size": len(sample),
                "average_next_season_wins": round(statistics.mean(wins), 2),
                "median_next_season_wins": round(statistics.median(wins), 2),
                "stddev_next_season_wins": round(
                    statistics.pstdev(wins), 2
                ),
                "minimum_next_season_wins": min(wins),
                "maximum_next_season_wins": max(wins),
                "average_win_change": round(statistics.mean(changes), 2),
                "improved_win_rate": round(
                    sum(change > 0 for change in changes) / len(changes), 4
                ),
                "next_season_playoff_rate": round(
                    sum(bool(row["next_season_playoff_team"]) for row in sample)
                    / len(sample),
                    4,
                ),
                "next_season_division_title_rate": round(
                    sum(
                        int(row["next_season_division_rank"]) == 1
                        for row in sample
                    )
                    / len(sample),
                    4,
                ),
                "cohorts": ",".join(
                    f"{season}->{season + 1}"
                    for season in sorted(
                        {int(row["season"]) for row in sample}
                    )
                ),
            }
        )
    return rows


def validate_win_totals(rows: list[dict[str, Any]]) -> None:
    teams = [str(row.get("team") or "") for row in rows]
    duplicates = sorted(
        team for team, count in Counter(teams).items() if count > 1
    )
    missing = sorted(set(TEAM_ABBREVIATIONS) - set(teams))
    extra = sorted(set(teams) - set(TEAM_ABBREVIATIONS))
    if len(rows) != 32 or duplicates or missing or extra:
        raise ValueError(
            "Win totals must contain exactly 32 unique NFL teams; "
            f"duplicates={duplicates}, missing={missing}, extra={extra}"
        )
    for row in rows:
        total = float(row["win_total"])
        if total < 0 or total > 17:
            raise ValueError(f"Invalid win total for {row['team']}: {total}")


def build_win_total_rows(
    totals: dict[str, float],
    *,
    season: int,
    captured_at: datetime,
    bookmaker: str = "BetOnline",
    source: str = "manual",
) -> list[dict[str, Any]]:
    captured_utc = captured_at.astimezone(timezone.utc)
    rows = [
        {
            "season": season,
            "team": team,
            "team_abbreviation": TEAM_ABBREVIATIONS[team],
            "bookmaker": bookmaker,
            "win_total": total,
            "over_price": "",
            "under_price": "",
            "captured_at_utc": captured_utc.isoformat(),
            "captured_at_et": captured_utc.astimezone(ET).isoformat(),
            "source": source,
        }
        for team, total in totals.items()
    ]
    rows.sort(key=lambda row: str(row["team_abbreviation"]))
    validate_win_totals(rows)
    return rows


def validate_prediction_revision(rows: list[dict[str, Any]]) -> None:
    if len(rows) != 32:
        raise ValueError("A prediction revision must contain all 32 teams")
    teams = {str(row.get("team") or "") for row in rows}
    if teams != set(TEAM_ABBREVIATIONS):
        raise ValueError("Prediction revision team set is incomplete")
    for row in rows:
        wins = row.get("predicted_wins")
        if not isinstance(wins, int) or isinstance(wins, bool):
            raise ValueError("Predicted wins must be whole numbers")
        if wins < 0 or wins > 17:
            raise ValueError("Predicted wins must be between 0 and 17")


def latest_predictions_for_user(
    predictions: Iterable[dict[str, Any]],
    user_id: int | str,
) -> tuple[dict[str, dict[str, Any]], Counter[str]]:
    matching = [
        row
        for row in predictions
        if str(row.get("telegram_user_id") or "") == str(user_id)
    ]
    counts = Counter(str(row.get("team") or "") for row in matching)
    latest: dict[str, tuple[tuple[str, str], dict[str, Any]]] = {}
    for row in matching:
        team = str(row.get("team") or "")
        key = (
            str(row.get("submitted_at_utc") or ""),
            str(row.get("revision_id") or ""),
        )
        if team and (team not in latest or key > latest[team][0]):
            latest[team] = (key, row)
    return (
        {team: row for team, (_, row) in latest.items()},
        counts,
    )


def _celebrity_display_name(user_id: str, display_name: Any) -> str:
    name = str(display_name or "")
    try:
        is_celebrity = int(user_id) < 0
    except (TypeError, ValueError):
        is_celebrity = False
    if is_celebrity and not name.startswith("🎤 "):
        return f"🎤 {name}"
    return name


def build_latest_prediction_rows(
    predictions: list[dict[str, Any]],
    win_totals: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    users = sorted(
        {
            str(row.get("telegram_user_id") or "")
            for row in predictions
            if str(row.get("telegram_user_id") or "")
        }
    )
    market_by_team = {
        str(row["team"]): row for row in win_totals
    }
    output: list[dict[str, Any]] = []
    for user_id in users:
        user_rows = [
            row
            for row in predictions
            if str(row.get("telegram_user_id") or "") == user_id
        ]
        latest, counts = latest_predictions_for_user(user_rows, user_id)
        identity = user_rows[-1] if user_rows else {}
        display_name = _celebrity_display_name(
            user_id, identity.get("telegram_display_name", "")
        )
        for team, abbreviation in TEAM_ABBREVIATIONS.items():
            row = latest.get(team, {})
            market = market_by_team.get(team, {})
            row_display_name = _celebrity_display_name(
                user_id,
                row.get("telegram_display_name") or display_name,
            )
            output.append(
                {
                    "telegram_user_id": user_id,
                    "telegram_username": row.get(
                        "telegram_username",
                        identity.get("telegram_username", ""),
                    ),
                    "telegram_display_name": row_display_name,
                    "season": row.get(
                        "season", market.get("season", "")
                    ),
                    "guess_count": counts[team],
                    "team": team,
                    "team_abbreviation": abbreviation,
                    "predicted_wins": row.get("predicted_wins", ""),
                    "latest_revision_id": row.get("revision_id", ""),
                    "latest_submitted_at_et": row.get(
                        "submitted_at_et", ""
                    ),
                    "market_win_total": market.get("win_total", ""),
                    "market_captured_at_et": market.get(
                        "captured_at_et", ""
                    ),
                    "prior_season": row.get("prior_season", ""),
                    "prior_division": row.get("prior_division", ""),
                    "prior_division_rank": row.get(
                        "prior_division_rank", ""
                    ),
                    "actual_wins_at_latest_submission": row.get(
                        "actual_wins_at_submission", ""
                    ),
                    "actual_week_at_latest_submission": row.get(
                        "actual_week_at_submission", ""
                    ),
                }
            )
    return sorted(
        output,
        key=lambda row: (
            str(row["telegram_user_id"]),
            int(row["guess_count"]),
            str(row["team_abbreviation"]),
        ),
    )


def ensure_worksheet(
    spreadsheet: gspread.Spreadsheet,
    title: str,
    headers: list[str],
) -> gspread.Worksheet:
    try:
        worksheet = spreadsheet.worksheet(title)
    except gspread.WorksheetNotFound:
        worksheet = spreadsheet.add_worksheet(
            title=title,
            rows=1000,
            cols=len(headers),
        )
        worksheet.update([headers], "A1", value_input_option="RAW")
        return worksheet
    existing = worksheet.row_values(1)
    if existing != headers:
        raise RuntimeError(
            f"{title} headers do not match expected schema"
        )
    return worksheet


def replace_rows(
    worksheet: gspread.Worksheet,
    headers: list[str],
    rows: list[dict[str, Any]],
) -> None:
    worksheet.clear()
    values = [headers] + [
        [row.get(header, "") for header in headers] for row in rows
    ]
    worksheet.update(values, "A1", value_input_option="RAW")
