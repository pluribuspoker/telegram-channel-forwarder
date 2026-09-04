"""Authoritative complete NFL regular-season schedule from ESPN."""

from __future__ import annotations

from collections import Counter
from datetime import datetime
from typing import Any

import httpx

from nfl_game_history import ET, fetch_calendar_scoreboard

SCHEDULE_TAB = "nfl_schedule"
SCHEDULE_HEADERS = [
    "event_id",
    "season",
    "season_type",
    "week",
    "status",
    "kickoff_utc",
    "kickoff_et",
    "away_team",
    "home_team",
    "neutral_site",
    "source",
]

REGULAR_SEASON_TYPE = 2
EXPECTED_GAMES = 272
EXPECTED_WEEKS = set(range(1, 19))


def _competitor(
    competition: dict[str, Any], home_away: str
) -> dict[str, Any]:
    matches = [
        item
        for item in competition.get("competitors") or []
        if item.get("homeAway") == home_away
    ]
    if len(matches) != 1:
        raise ValueError(
            f"Expected one {home_away} competitor, found {len(matches)}"
        )
    return matches[0]


def _team_name(competitor: dict[str, Any]) -> str:
    name = str((competitor.get("team") or {}).get("displayName") or "")
    if not name:
        raise ValueError("ESPN schedule competitor has no display name")
    return name


def parse_schedule_event(event: dict[str, Any]) -> dict[str, Any]:
    competitions = event.get("competitions") or []
    if len(competitions) != 1:
        raise ValueError(
            f"Event {event.get('id')} has {len(competitions)} competitions"
        )
    competition = competitions[0]
    kickoff = datetime.fromisoformat(
        str(event["date"]).replace("Z", "+00:00")
    )
    status_type = (event.get("status") or {}).get("type") or {}
    return {
        "event_id": str(event["id"]),
        "season": int((event.get("season") or {})["year"]),
        "season_type": "regular",
        "week": int((event.get("week") or {})["number"]),
        "status": str(
            status_type.get("name")
            or status_type.get("description")
            or "scheduled"
        ),
        "kickoff_utc": kickoff.isoformat(),
        "kickoff_et": kickoff.astimezone(ET).isoformat(),
        "away_team": _team_name(_competitor(competition, "away")),
        "home_team": _team_name(_competitor(competition, "home")),
        "neutral_site": bool(competition.get("neutralSite")),
        "source": "espn",
    }


def fetch_regular_season_schedule(
    season: int,
    *,
    client: httpx.Client | None = None,
) -> list[dict[str, Any]]:
    owns_client = client is None
    http = client or httpx.Client(timeout=30)
    try:
        events = [
            *fetch_calendar_scoreboard(season, client=http),
            *fetch_calendar_scoreboard(season + 1, client=http),
        ]
    finally:
        if owns_client:
            http.close()
    by_id = {
        str(event["id"]): event
        for event in events
        if str(event.get("id") or "")
        and int((event.get("season") or {}).get("year") or 0) == season
        and int((event.get("season") or {}).get("type") or 0)
        == REGULAR_SEASON_TYPE
    }
    rows = [parse_schedule_event(event) for event in by_id.values()]
    rows.sort(
        key=lambda row: (
            int(row["week"]),
            str(row["kickoff_utc"]),
            str(row["event_id"]),
        )
    )
    validate_schedule(rows, season=season)
    return rows


def validate_schedule(
    rows: list[dict[str, Any]],
    *,
    season: int,
    expected_games: int = EXPECTED_GAMES,
) -> None:
    if len(rows) != expected_games:
        raise ValueError(
            f"{season} schedule has {len(rows)} games, expected "
            f"{expected_games}"
        )
    ids = [str(row.get("event_id") or "") for row in rows]
    if not all(ids) or len(ids) != len(set(ids)):
        raise ValueError("NFL schedule has blank or duplicate event IDs")
    if {int(row["season"]) for row in rows} != {season}:
        raise ValueError("NFL schedule contains another season")
    weeks = {int(row["week"]) for row in rows}
    if weeks != EXPECTED_WEEKS:
        raise ValueError(
            f"{season} schedule weeks {sorted(weeks)} do not cover 1-18"
        )
    team_games = Counter(
        team
        for row in rows
        for team in (str(row["away_team"]), str(row["home_team"]))
    )
    if len(team_games) != 32 or set(team_games.values()) != {17}:
        raise ValueError(
            f"{season} team game counts are invalid: {dict(team_games)}"
        )

