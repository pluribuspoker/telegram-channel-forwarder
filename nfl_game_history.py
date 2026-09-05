"""Completed NFL game history and matchup classification helpers."""

from __future__ import annotations

from collections import Counter, defaultdict
from datetime import datetime
from typing import Any, Iterable
from zoneinfo import ZoneInfo

import httpx

ET = ZoneInfo("America/New_York")
SCOREBOARD_URL = (
    "https://site.api.espn.com/apis/site/v2/sports/football/nfl/scoreboard"
)
REGULAR_SEASON_TYPE = 2
EXPECTED_GAMES_PER_SEASON = 272
EXPECTED_MATCHUP_COUNTS = {
    "division": 96,
    "conference": 96,
    "non_conference": 80,
}

GAME_HISTORY_TAB = "nfl_game_history"
GAME_HISTORY_HEADERS = [
    "event_id",
    "season",
    "season_type",
    "week",
    "status",
    "kickoff_utc",
    "kickoff_et",
    "away_team",
    "home_team",
    "away_score",
    "home_score",
    "home_result",
    "home_margin",
    "total_points",
    "away_conference",
    "away_division",
    "home_conference",
    "home_division",
    "same_conference",
    "same_division",
    "matchup_type",
    "division_meeting_number",
    "neutral_site",
    "overtime",
    "tags",
    "source",
]


def fetch_calendar_scoreboard(
    calendar_year: int,
    *,
    client: httpx.Client | None = None,
) -> list[dict[str, Any]]:
    owns_client = client is None
    http = client or httpx.Client(timeout=30)
    try:
        response = http.get(
            SCOREBOARD_URL,
            params={"dates": calendar_year, "limit": 1000},
        )
        response.raise_for_status()
        payload = response.json()
    finally:
        if owns_client:
            http.close()
    if not isinstance(payload, dict):
        raise ValueError("ESPN scoreboard response must be an object")
    events = payload.get("events")
    if not isinstance(events, list):
        raise ValueError("ESPN scoreboard response has no events list")
    return events


def fetch_regular_season_events(
    season: int,
    *,
    client: httpx.Client | None = None,
    expected_games: int | None = EXPECTED_GAMES_PER_SEASON,
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
        str(event.get("id") or ""): event
        for event in events
        if str(event.get("id") or "")
        and int((event.get("season") or {}).get("year") or 0) == season
        and int((event.get("season") or {}).get("type") or 0)
        == REGULAR_SEASON_TYPE
        and bool(((event.get("status") or {}).get("type") or {}).get("completed"))
    }
    result = sorted(
        by_id.values(),
        key=lambda event: (
            str(event.get("date") or ""),
            str(event.get("id") or ""),
        ),
    )
    if expected_games is not None and len(result) != expected_games:
        raise ValueError(
            f"ESPN returned {len(result)} completed regular-season games "
            f"for {season}, expected {expected_games}"
        )
    return result


def team_alignment(
    standings: Iterable[dict[str, Any]],
) -> dict[str, tuple[str, str]]:
    alignment: dict[str, tuple[str, str]] = {}
    for row in standings:
        team = str(row.get("team") or "")
        conference = str(row.get("conference") or "")
        division = str(row.get("division") or "")
        if not team or not conference or not division:
            raise ValueError("Standings row is missing team alignment")
        alignment[team] = (conference, division)
    if len(alignment) != 32:
        raise ValueError(
            f"Standings contain {len(alignment)} aligned teams, expected 32"
        )
    return alignment


def _competitor(
    competition: dict[str, Any],
    home_away: str,
) -> dict[str, Any]:
    matches = [
        competitor
        for competitor in competition.get("competitors") or []
        if competitor.get("homeAway") == home_away
    ]
    if len(matches) != 1:
        raise ValueError(
            f"Expected one {home_away} competitor, found {len(matches)}"
        )
    return matches[0]


def _team_name(competitor: dict[str, Any]) -> str:
    team = str((competitor.get("team") or {}).get("displayName") or "")
    if not team:
        raise ValueError("ESPN competitor is missing a display name")
    return team


def _score(competitor: dict[str, Any]) -> int:
    raw = competitor.get("score")
    try:
        score = float(raw)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"Invalid ESPN score: {raw!r}") from exc
    if not score.is_integer():
        raise ValueError(f"NFL score must be an integer: {raw!r}")
    return int(score)


def _base_row(
    event: dict[str, Any],
    alignment: dict[str, tuple[str, str]],
) -> dict[str, Any]:
    competitions = event.get("competitions") or []
    if len(competitions) != 1:
        raise ValueError(
            f"Event {event.get('id')} has {len(competitions)} competitions"
        )
    competition = competitions[0]
    home = _competitor(competition, "home")
    away = _competitor(competition, "away")
    home_team = _team_name(home)
    away_team = _team_name(away)
    if home_team not in alignment or away_team not in alignment:
        raise ValueError(
            f"Missing alignment for {away_team} at {home_team}"
        )
    home_conference, home_division = alignment[home_team]
    away_conference, away_division = alignment[away_team]
    same_conference = home_conference == away_conference
    same_division = home_division == away_division
    matchup_type = (
        "division"
        if same_division
        else "conference"
        if same_conference
        else "non_conference"
    )
    home_score = _score(home)
    away_score = _score(away)
    kickoff = datetime.fromisoformat(
        str(event["date"]).replace("Z", "+00:00")
    )
    event_status = event.get("status") or {}
    competition_status = competition.get("status") or {}
    period = max(
        int(event_status.get("period") or 0),
        int(competition_status.get("period") or 0),
    )
    if not period:
        raise ValueError(f"Event {event.get('id')} is missing its final period")
    return {
        "event_id": str(event["id"]),
        "season": int((event.get("season") or {})["year"]),
        "season_type": "regular",
        "week": int((event.get("week") or {})["number"]),
        "status": "final",
        "kickoff_utc": kickoff.isoformat(),
        "kickoff_et": kickoff.astimezone(ET).isoformat(),
        "away_team": away_team,
        "home_team": home_team,
        "away_score": away_score,
        "home_score": home_score,
        "home_result": (
            "W"
            if home_score > away_score
            else "L"
            if home_score < away_score
            else "T"
        ),
        "home_margin": home_score - away_score,
        "total_points": home_score + away_score,
        "away_conference": away_conference,
        "away_division": away_division,
        "home_conference": home_conference,
        "home_division": home_division,
        "same_conference": same_conference,
        "same_division": same_division,
        "matchup_type": matchup_type,
        "division_meeting_number": "",
        "neutral_site": bool(competition.get("neutralSite")),
        "overtime": period > 4,
        "tags": "",
        "source": "espn",
    }


def build_game_history(
    events_by_season: dict[int, list[dict[str, Any]]],
    standings_by_season: dict[int, list[dict[str, Any]]],
    *,
    validate: bool = True,
    require_complete_divisional_pairs: bool = True,
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for season in sorted(events_by_season):
        if season not in standings_by_season:
            raise ValueError(f"Missing standings for {season}")
        alignment = team_alignment(standings_by_season[season])
        season_rows = [
            _base_row(event, alignment)
            for event in events_by_season[season]
        ]
        pair_rows: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
        for row in season_rows:
            if row["same_division"]:
                pair = tuple(
                    sorted((str(row["away_team"]), str(row["home_team"])))
                )
                pair_rows[pair].append(row)
        for pair, meetings in pair_rows.items():
            meetings.sort(
                key=lambda row: (
                    str(row["kickoff_utc"]),
                    str(row["event_id"]),
                )
            )
            if require_complete_divisional_pairs and len(meetings) != 2:
                raise ValueError(
                    f"{season} divisional pair {pair} has "
                    f"{len(meetings)} meetings, expected 2"
                )
            for number, row in enumerate(meetings, start=1):
                row["division_meeting_number"] = number
        for row in season_rows:
            tags = [
                (
                    f"divisional_game_{row['division_meeting_number']}"
                    if row["same_division"]
                    else f"{row['matchup_type']}_game"
                )
            ]
            if int(row["week"]) == 1:
                tags.append("week_1")
            if row["neutral_site"]:
                tags.append("neutral_site")
            if row["overtime"]:
                tags.append("overtime")
            row["tags"] = ",".join(tags)
        rows.extend(season_rows)
    rows.sort(
        key=lambda row: (
            int(row["season"]),
            int(row["week"]),
            str(row["kickoff_utc"]),
            str(row["event_id"]),
        )
    )
    if validate:
        validate_game_history(rows, expected_seasons=set(events_by_season))
    return rows


def validate_game_history(
    rows: list[dict[str, Any]],
    *,
    expected_seasons: set[int],
    expected_games_per_season: int = EXPECTED_GAMES_PER_SEASON,
    expected_matchup_counts: dict[str, int] = EXPECTED_MATCHUP_COUNTS,
) -> None:
    ids = [str(row.get("event_id") or "") for row in rows]
    if len(ids) != len(set(ids)):
        raise ValueError("Game history contains duplicate event ids")
    seasons = {int(row["season"]) for row in rows}
    if seasons != expected_seasons:
        raise ValueError(
            f"Game history seasons {seasons} do not match {expected_seasons}"
        )
    for season in sorted(expected_seasons):
        season_rows = [
            row for row in rows if int(row["season"]) == season
        ]
        if len(season_rows) != expected_games_per_season:
            raise ValueError(
                f"{season} has {len(season_rows)} games, "
                f"expected {expected_games_per_season}"
            )
        matchup_counts = Counter(
            str(row["matchup_type"]) for row in season_rows
        )
        if dict(matchup_counts) != expected_matchup_counts:
            raise ValueError(
                f"{season} matchup counts {dict(matchup_counts)} do not "
                f"match {expected_matchup_counts}"
            )
        if {int(row["week"]) for row in season_rows} != set(range(1, 19)):
            raise ValueError(f"{season} does not contain weeks 1 through 18")
        if any(
            _as_bool(row["same_division"])
            and not _as_bool(row["same_conference"])
            for row in season_rows
        ):
            raise ValueError(
                f"{season} has a divisional game outside its conference"
            )
        meeting_counts = Counter(
            int(row["division_meeting_number"])
            for row in season_rows
            if _as_bool(row["same_division"])
        )
        if meeting_counts != Counter({1: 48, 2: 48}):
            raise ValueError(
                f"{season} divisional meeting counts are "
                f"{dict(meeting_counts)}"
            )
        team_games = Counter(
            team
            for row in season_rows
            for team in (str(row["away_team"]), str(row["home_team"]))
        )
        if len(team_games) != 32 or set(team_games.values()) != {17}:
            raise ValueError(
                f"{season} team game counts are invalid: "
                f"{dict(team_games)}"
            )
        division_pairs: dict[tuple[str, str], list[dict[str, Any]]] = (
            defaultdict(list)
        )
        for row in season_rows:
            away_score = int(row["away_score"])
            home_score = int(row["home_score"])
            margin = home_score - away_score
            expected_result = (
                "W" if margin > 0 else "L" if margin < 0 else "T"
            )
            if (
                int(row["home_margin"]) != margin
                or int(row["total_points"]) != home_score + away_score
                or str(row["home_result"]) != expected_result
            ):
                raise ValueError(
                    f"Event {row['event_id']} has inconsistent score fields"
                )
            if _as_bool(row["same_division"]):
                pair = tuple(
                    sorted(
                        (str(row["away_team"]), str(row["home_team"]))
                    )
                )
                division_pairs[pair].append(row)
        for pair, meetings in division_pairs.items():
            if {str(row["home_team"]) for row in meetings} != set(pair):
                raise ValueError(
                    f"{season} divisional pair {pair} is not reciprocal "
                    "home-and-away"
                )


def _as_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, int) and value in (0, 1):
        return bool(value)
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in {"true", "1"}:
            return True
        if normalized in {"false", "0"}:
            return False
    raise ValueError(f"Invalid boolean value: {value!r}")


def summarize_game_history(
    rows: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    summaries: list[dict[str, Any]] = []
    for season in sorted({int(row["season"]) for row in rows}):
        season_rows = [
            row for row in rows if int(row["season"]) == season
        ]
        result_counts = Counter(
            str(row["home_result"]) for row in season_rows
        )
        matchup_counts = Counter(
            str(row["matchup_type"]) for row in season_rows
        )
        week_one_division = [
            row
            for row in season_rows
            if int(row["week"]) == 1 and row["same_division"]
        ]
        week_one_results = Counter(
            str(row["home_result"]) for row in week_one_division
        )
        summaries.append(
            {
                "season": season,
                "games": len(season_rows),
                "division": matchup_counts["division"],
                "conference": matchup_counts["conference"],
                "non_conference": matchup_counts["non_conference"],
                "home_wins": result_counts["W"],
                "home_losses": result_counts["L"],
                "home_ties": result_counts["T"],
                "week_1_division_games": len(week_one_division),
                "week_1_division_home_record": (
                    f"{week_one_results['W']}-"
                    f"{week_one_results['L']}-"
                    f"{week_one_results['T']}"
                ),
            }
        )
    return summaries
