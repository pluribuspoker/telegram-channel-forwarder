"""Fetch BetOnline NFL lines and persist opening/latest snapshots to Google Sheets."""

from __future__ import annotations

import base64
import json
import os
import time
from dataclasses import asdict, dataclass, replace
from datetime import datetime, timedelta, timezone
from typing import Any, Callable
from zoneinfo import ZoneInfo

import gspread
import httpx
from google.oauth2.service_account import Credentials

ODDS_API_BASE = "https://api.the-odds-api.com/v4"
BOOKMAKER_KEY = "betonlineag"
MARKETS = "h2h,spreads,totals"
PERIOD_MARKETS = (
    "h2h_h1,spreads_h1,totals_h1,"
    "h2h_q1,spreads_q1,totals_q1"
)
DEFAULT_PERIOD_WINDOW_HOURS = 0
NO_DATA = "nodata"
SPORT_KEYS = {
    "regular": "americanfootball_nfl",
    "preseason": "americanfootball_nfl_preseason",
}
SHEET_TABS = {
    "games": "nfl_games",
    "snapshots": "nfl_line_snapshots",
}
ET = ZoneInfo("America/New_York")

SNAPSHOT_HEADERS = [
    "captured_at",
    "event_id",
    "commence_time_utc",
    "commence_time_et",
    "away_team",
    "home_team",
    "bookmaker",
    (
        "away_game_spread_spreadprice_moneyline__"
        "h1_spread_spreadprice_moneyline__"
        "q1_spread_spreadprice_moneyline"
    ),
    (
        "home_game_spread_spreadprice_moneyline__"
        "h1_spread_spreadprice_moneyline__"
        "q1_spread_spreadprice_moneyline"
    ),
    (
        "totals_game_total_overprice_underprice__"
        "h1_total_overprice_underprice__"
        "q1_total_overprice_underprice"
    ),
    "api_requests_used",
    "api_requests_remaining",
]
AWAY_SNAPSHOT_COLUMN = SNAPSHOT_HEADERS[7]
HOME_SNAPSHOT_COLUMN = SNAPSHOT_HEADERS[8]
TOTALS_SNAPSHOT_COLUMN = SNAPSHOT_HEADERS[9]
OPENING_AWAY_COLUMN = f"opening_{AWAY_SNAPSHOT_COLUMN}"
OPENING_HOME_COLUMN = f"opening_{HOME_SNAPSHOT_COLUMN}"
OPENING_TOTALS_COLUMN = f"opening_{TOTALS_SNAPSHOT_COLUMN}"
LATEST_AWAY_COLUMN = f"latest_{AWAY_SNAPSHOT_COLUMN}"
LATEST_HOME_COLUMN = f"latest_{HOME_SNAPSHOT_COLUMN}"
LATEST_TOTALS_COLUMN = f"latest_{TOTALS_SNAPSHOT_COLUMN}"

GAME_HEADERS = [
    "event_id",
    "season",
    "season_type",
    "week",
    "status",
    "commence_time_utc",
    "commence_time_et",
    "away_team",
    "home_team",
    "bookmaker",
    "opening_captured_at",
    "latest_captured_at",
    OPENING_AWAY_COLUMN,
    OPENING_HOME_COLUMN,
    OPENING_TOTALS_COLUMN,
    LATEST_AWAY_COLUMN,
    LATEST_HOME_COLUMN,
    LATEST_TOTALS_COLUMN,
    "last_updated_at",
    "period_last_checked_at",
]

LINE_FIELDS = (
    "away_spread",
    "away_spread_price",
    "home_spread",
    "home_spread_price",
    "away_moneyline",
    "home_moneyline",
    "total",
    "over_price",
    "under_price",
)
PERIOD_PREFIXES = ("first_half", "first_quarter")

LEAN_HEADERS = [
    "submission_id",
    "submitted_at_utc",
    "submitted_at_et",
    "telegram_user_id",
    "telegram_username",
    "telegram_first_name",
    "telegram_last_name",
    "telegram_message_id",
    "event_id",
    "season",
    "season_type",
    "week",
    "commence_time_utc",
    "commence_time_et",
    "away_team",
    "home_team",
    "bookmaker",
    "period",
    "market",
    "side",
    "opening_captured_at",
    "latest_captured_at",
    "opening_selected_line",
    "opening_selected_price",
    "latest_selected_line",
    "latest_selected_price",
    OPENING_AWAY_COLUMN,
    OPENING_HOME_COLUMN,
    OPENING_TOTALS_COLUMN,
    LATEST_AWAY_COLUMN,
    LATEST_HOME_COLUMN,
    LATEST_TOTALS_COLUMN,
    "lean_text",
]


@dataclass(frozen=True)
class GameLines:
    captured_at: str
    event_id: str
    season: int
    season_type: str
    commence_time_utc: str
    commence_time_et: str
    away_team: str
    home_team: str
    bookmaker: str
    away_spread: float | None
    away_spread_price: int | None
    home_spread: float | None
    home_spread_price: int | None
    away_moneyline: int | None
    home_moneyline: int | None
    total: float | None
    over_price: int | None
    under_price: int | None
    first_half_away_spread: float | None
    first_half_away_spread_price: int | None
    first_half_home_spread: float | None
    first_half_home_spread_price: int | None
    first_half_away_moneyline: int | None
    first_half_home_moneyline: int | None
    first_half_total: float | None
    first_half_over_price: int | None
    first_half_under_price: int | None
    first_quarter_away_spread: float | None
    first_quarter_away_spread_price: int | None
    first_quarter_home_spread: float | None
    first_quarter_home_spread_price: int | None
    first_quarter_away_moneyline: int | None
    first_quarter_home_moneyline: int | None
    first_quarter_total: float | None
    first_quarter_over_price: int | None
    first_quarter_under_price: int | None
    api_requests_used: str
    api_requests_remaining: str
    period_checked_at: str | None

    def signature(self) -> tuple[Any, ...]:
        return (
            self.away_spread,
            self.away_spread_price,
            self.home_spread,
            self.home_spread_price,
            self.away_moneyline,
            self.home_moneyline,
            self.total,
            self.over_price,
            self.under_price,
            self.first_half_away_spread,
            self.first_half_away_spread_price,
            self.first_half_home_spread,
            self.first_half_home_spread_price,
            self.first_half_away_moneyline,
            self.first_half_home_moneyline,
            self.first_half_total,
            self.first_half_over_price,
            self.first_half_under_price,
            self.first_quarter_away_spread,
            self.first_quarter_away_spread_price,
            self.first_quarter_home_spread,
            self.first_quarter_home_spread_price,
            self.first_quarter_away_moneyline,
            self.first_quarter_home_moneyline,
            self.first_quarter_total,
            self.first_quarter_over_price,
            self.first_quarter_under_price,
        )


@dataclass(frozen=True)
class FetchResult:
    games: list[GameLines]
    requests_used: str
    requests_remaining: str


def _parse_time(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def _format_time(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _nfl_season_year(commence: datetime) -> int:
    eastern = commence.astimezone(ET)
    return eastern.year - 1 if eastern.month <= 2 else eastern.year


def _find_outcome(
    outcomes: list[dict[str, Any]], name: str
) -> tuple[float | None, int | None]:
    for outcome in outcomes:
        if outcome.get("name") == name:
            point = outcome.get("point")
            price = outcome.get("price")
            return (
                float(point) if point is not None else None,
                int(price) if price is not None else None,
            )
    return None, None


def parse_game(
    game: dict[str, Any],
    *,
    season_type: str,
    captured_at: datetime,
    requests_used: str,
    requests_remaining: str,
    period_checked_at: str | None = None,
) -> GameLines | None:
    bookmaker = next(
        (
            item
            for item in game.get("bookmakers", [])
            if item.get("key") == BOOKMAKER_KEY
        ),
        None,
    )
    if bookmaker is None:
        return None

    markets = {
        market.get("key"): market.get("outcomes", [])
        for market in bookmaker.get("markets", [])
    }
    away = str(game.get("away_team", ""))
    home = str(game.get("home_team", ""))
    commence = _parse_time(str(game["commence_time"]))

    away_spread, away_spread_price = _find_outcome(
        markets.get("spreads", []), away
    )
    home_spread, home_spread_price = _find_outcome(
        markets.get("spreads", []), home
    )
    _, away_moneyline = _find_outcome(markets.get("h2h", []), away)
    _, home_moneyline = _find_outcome(markets.get("h2h", []), home)
    total, over_price = _find_outcome(markets.get("totals", []), "Over")
    under_total, under_price = _find_outcome(
        markets.get("totals", []), "Under"
    )
    if total is None:
        total = under_total
    first_half_away_spread, first_half_away_spread_price = _find_outcome(
        markets.get("spreads_h1", []), away
    )
    first_half_home_spread, first_half_home_spread_price = _find_outcome(
        markets.get("spreads_h1", []), home
    )
    _, first_half_away_moneyline = _find_outcome(
        markets.get("h2h_h1", []), away
    )
    _, first_half_home_moneyline = _find_outcome(
        markets.get("h2h_h1", []), home
    )
    first_half_total, first_half_over_price = _find_outcome(
        markets.get("totals_h1", []), "Over"
    )
    first_half_under_total, first_half_under_price = _find_outcome(
        markets.get("totals_h1", []), "Under"
    )
    if first_half_total is None:
        first_half_total = first_half_under_total
    (
        first_quarter_away_spread,
        first_quarter_away_spread_price,
    ) = _find_outcome(markets.get("spreads_q1", []), away)
    (
        first_quarter_home_spread,
        first_quarter_home_spread_price,
    ) = _find_outcome(markets.get("spreads_q1", []), home)
    _, first_quarter_away_moneyline = _find_outcome(
        markets.get("h2h_q1", []), away
    )
    _, first_quarter_home_moneyline = _find_outcome(
        markets.get("h2h_q1", []), home
    )
    first_quarter_total, first_quarter_over_price = _find_outcome(
        markets.get("totals_q1", []), "Over"
    )
    first_quarter_under_total, first_quarter_under_price = _find_outcome(
        markets.get("totals_q1", []), "Under"
    )
    if first_quarter_total is None:
        first_quarter_total = first_quarter_under_total

    return GameLines(
        captured_at=_format_time(captured_at),
        event_id=str(game["id"]),
        season=_nfl_season_year(commence),
        season_type=season_type,
        commence_time_utc=_format_time(commence),
        commence_time_et=commence.astimezone(ET).isoformat(),
        away_team=away,
        home_team=home,
        bookmaker=str(bookmaker.get("title") or "BetOnline.ag"),
        away_spread=away_spread,
        away_spread_price=away_spread_price,
        home_spread=home_spread,
        home_spread_price=home_spread_price,
        away_moneyline=away_moneyline,
        home_moneyline=home_moneyline,
        total=total,
        over_price=over_price,
        under_price=under_price,
        first_half_away_spread=first_half_away_spread,
        first_half_away_spread_price=first_half_away_spread_price,
        first_half_home_spread=first_half_home_spread,
        first_half_home_spread_price=first_half_home_spread_price,
        first_half_away_moneyline=first_half_away_moneyline,
        first_half_home_moneyline=first_half_home_moneyline,
        first_half_total=first_half_total,
        first_half_over_price=first_half_over_price,
        first_half_under_price=first_half_under_price,
        first_quarter_away_spread=first_quarter_away_spread,
        first_quarter_away_spread_price=first_quarter_away_spread_price,
        first_quarter_home_spread=first_quarter_home_spread,
        first_quarter_home_spread_price=first_quarter_home_spread_price,
        first_quarter_away_moneyline=first_quarter_away_moneyline,
        first_quarter_home_moneyline=first_quarter_home_moneyline,
        first_quarter_total=first_quarter_total,
        first_quarter_over_price=first_quarter_over_price,
        first_quarter_under_price=first_quarter_under_price,
        api_requests_used=requests_used,
        api_requests_remaining=requests_remaining,
        period_checked_at=period_checked_at,
    )


def _check_odds_response(response: httpx.Response) -> None:
    if response.is_success:
        return
    body = response.text[:500]
    raise RuntimeError(
        f"The Odds API returned HTTP {response.status_code}: {body}"
    )


def _merge_period_markets(
    game: dict[str, Any], period_data: dict[str, Any]
) -> None:
    source = next(
        (
            item
            for item in period_data.get("bookmakers", [])
            if item.get("key") == BOOKMAKER_KEY
        ),
        None,
    )
    if source is None:
        return
    target = next(
        (
            item
            for item in game.get("bookmakers", [])
            if item.get("key") == BOOKMAKER_KEY
        ),
        None,
    )
    if target is None:
        game.setdefault("bookmakers", []).append(source)
        return
    by_key = {
        market.get("key"): market
        for market in target.get("markets", [])
    }
    for market in source.get("markets", []):
        by_key[market.get("key")] = market
    target["markets"] = list(by_key.values())


def fetch_sport(
    api_key: str,
    sport_key: str,
    season_type: str,
    *,
    client: httpx.Client | None = None,
    now: datetime | None = None,
    period_window_hours: int = DEFAULT_PERIOD_WINDOW_HOURS,
    period_event_ids: set[str] | None = None,
    known_event_ids: set[str] | None = None,
) -> FetchResult:
    captured_at = now or datetime.now(timezone.utc)
    owns_client = client is None
    if client is None:
        client = httpx.Client(timeout=20)
    try:
        response = client.get(
            f"{ODDS_API_BASE}/sports/{sport_key}/odds/",
            params={
                "apiKey": api_key,
                "bookmakers": BOOKMAKER_KEY,
                "markets": MARKETS,
                "oddsFormat": "american",
                "dateFormat": "iso",
            },
        )
        _check_odds_response(response)
        used = response.headers.get("x-requests-used", "")
        remaining = response.headers.get("x-requests-remaining", "")
        raw_games = response.json()
        games: list[GameLines] = []
        for game in raw_games:
            commence = _parse_time(str(game["commence_time"]))
            if commence <= captured_at:
                continue
            event_id = str(game["id"])
            period_due = (
                period_event_ids is None
                or event_id in period_event_ids
                or (
                    known_event_ids is not None
                    and event_id not in known_event_ids
                )
            )
            period_checked_at = None
            if period_due and (
                period_window_hours <= 0
                or commence - captured_at
                <= timedelta(hours=period_window_hours)
            ):
                period_response = client.get(
                    (
                        f"{ODDS_API_BASE}/sports/{sport_key}/events/"
                        f"{game['id']}/odds"
                    ),
                    params={
                        "apiKey": api_key,
                        "bookmakers": BOOKMAKER_KEY,
                        "markets": PERIOD_MARKETS,
                        "oddsFormat": "american",
                        "dateFormat": "iso",
                    },
                )
                _check_odds_response(period_response)
                used = period_response.headers.get(
                    "x-requests-used", used
                )
                remaining = period_response.headers.get(
                    "x-requests-remaining", remaining
                )
                _merge_period_markets(game, period_response.json())
                period_checked_at = _format_time(captured_at)
            parsed = parse_game(
                game,
                season_type=season_type,
                captured_at=captured_at,
                requests_used=used,
                requests_remaining=remaining,
                period_checked_at=period_checked_at,
            )
            if parsed is not None:
                games.append(parsed)
        return FetchResult(
            games=sorted(games, key=lambda game: game.commence_time_utc),
            requests_used=used,
            requests_remaining=remaining,
        )
    finally:
        if owns_client:
            client.close()


def fetch_all(
    api_key: str,
    season_types: list[str],
    *,
    client: httpx.Client | None = None,
    now: datetime | None = None,
    period_window_hours: int = DEFAULT_PERIOD_WINDOW_HOURS,
    period_event_ids: set[str] | None = None,
    known_event_ids: set[str] | None = None,
) -> list[GameLines]:
    games: list[GameLines] = []
    for season_type in season_types:
        result = fetch_sport(
            api_key,
            SPORT_KEYS[season_type],
            season_type,
            client=client,
            now=now,
            period_window_hours=period_window_hours,
            period_event_ids=period_event_ids,
            known_event_ids=known_event_ids,
        )
        games.extend(result.games)
    return deduplicate_games(games)


def deduplicate_games(games: list[GameLines]) -> list[GameLines]:
    games_by_id = {game.event_id: game for game in games}
    return sorted(
        games_by_id.values(), key=lambda game: game.commence_time_utc
    )


def poll_interval_for_game(
    commence_time: str, now: datetime
) -> timedelta | None:
    until_kickoff = _parse_time(commence_time) - now
    if until_kickoff.total_seconds() <= 0:
        return None
    if until_kickoff > timedelta(days=7):
        return timedelta(days=1)
    if until_kickoff > timedelta(hours=24):
        return timedelta(hours=12)
    if until_kickoff > timedelta(hours=4):
        return timedelta(hours=1)
    return timedelta(minutes=30)


def _timestamp_due(
    value: Any, interval: timedelta, now: datetime
) -> bool:
    if value in (None, ""):
        return True
    return now - _parse_time(str(value)) >= interval


def scheduled_poll_plan(
    records: list[dict[str, Any]], now: datetime
) -> tuple[bool, set[str], set[str]]:
    known_event_ids = {
        str(record["event_id"])
        for record in records
        if record.get("event_id")
    }
    upcoming = []
    for record in records:
        commence = record.get("commence_time_utc")
        if not commence:
            continue
        interval = poll_interval_for_game(str(commence), now)
        if interval is not None:
            upcoming.append((record, interval))

    if not upcoming:
        latest_update = max(
            (
                _parse_time(str(record["last_updated_at"]))
                for record in records
                if record.get("last_updated_at")
            ),
            default=None,
        )
        discovery_due = (
            latest_update is None
            or now - latest_update >= timedelta(days=1)
        )
        return discovery_due, set(), known_event_ids

    full_fetch_due = any(
        _timestamp_due(record.get("last_updated_at"), interval, now)
        for record, interval in upcoming
    )
    period_event_ids = {
        str(record["event_id"])
        for record, interval in upcoming
        if record.get("event_id")
        and _timestamp_due(
            record.get("period_last_checked_at"), interval, now
        )
    }
    return (
        full_fetch_due or bool(period_event_ids),
        period_event_ids,
        known_event_ids,
    )


def get_gspread_client(credentials_b64: str) -> gspread.Client:
    if not credentials_b64:
        raise ValueError("GOOGLE_CREDENTIALS is not set")
    info = json.loads(base64.b64decode(credentials_b64).decode())
    credentials = Credentials.from_service_account_info(
        info,
        scopes=[
            "https://www.googleapis.com/auth/spreadsheets",
            "https://www.googleapis.com/auth/drive",
        ],
    )
    return gspread.authorize(credentials)


def _call_with_retry(
    operation: Callable[..., Any], *args: Any, retries: int = 6, **kwargs: Any
) -> Any:
    delay = 15
    for attempt in range(retries):
        try:
            return operation(*args, **kwargs)
        except gspread.exceptions.APIError as exc:
            if exc.response.status_code != 429 or attempt == retries - 1:
                raise
            time.sleep(delay)
            delay = min(delay * 2, 120)
    raise RuntimeError("unreachable")


def snapshot_row(game: GameLines) -> dict[str, Any]:
    data = asdict(game)
    return {
        "captured_at": game.captured_at,
        "event_id": game.event_id,
        "commence_time_utc": game.commence_time_utc,
        "commence_time_et": game.commence_time_et,
        "away_team": game.away_team,
        "home_team": game.home_team,
        "bookmaker": game.bookmaker,
        AWAY_SNAPSHOT_COLUMN: encode_team_snapshot(game, "away"),
        HOME_SNAPSHOT_COLUMN: encode_team_snapshot(game, "home"),
        TOTALS_SNAPSHOT_COLUMN: encode_totals_snapshot(game),
        "api_requests_used": data["api_requests_used"],
        "api_requests_remaining": data["api_requests_remaining"],
    }


def _encode_groups(groups: list[tuple[Any, ...]]) -> str:
    return "|".join(
        ",".join(NO_DATA if value is None else str(value) for value in group)
        for group in groups
    )


def encode_team_snapshot(game: GameLines, side: str) -> str:
    groups = []
    for prefix in ("", "first_half_", "first_quarter_"):
        groups.append(
            (
                getattr(game, f"{prefix}{side}_spread"),
                getattr(game, f"{prefix}{side}_spread_price"),
                getattr(game, f"{prefix}{side}_moneyline"),
            )
        )
    return _encode_groups(groups)


def encode_totals_snapshot(game: GameLines) -> str:
    groups = []
    for prefix in ("", "first_half_", "first_quarter_"):
        groups.append(
            (
                getattr(game, f"{prefix}total"),
                getattr(game, f"{prefix}over_price"),
                getattr(game, f"{prefix}under_price"),
            )
        )
    return _encode_groups(groups)


def new_game_row(game: GameLines) -> dict[str, Any]:
    away = encode_team_snapshot(game, "away")
    home = encode_team_snapshot(game, "home")
    totals = encode_totals_snapshot(game)
    return {
        "event_id": game.event_id,
        "season": game.season,
        "season_type": game.season_type,
        "week": "",
        "status": "upcoming",
        "commence_time_utc": game.commence_time_utc,
        "commence_time_et": game.commence_time_et,
        "away_team": game.away_team,
        "home_team": game.home_team,
        "bookmaker": game.bookmaker,
        "opening_captured_at": game.captured_at,
        "latest_captured_at": game.captured_at,
        OPENING_AWAY_COLUMN: away,
        OPENING_HOME_COLUMN: home,
        OPENING_TOTALS_COLUMN: totals,
        LATEST_AWAY_COLUMN: away,
        LATEST_HOME_COLUMN: home,
        LATEST_TOTALS_COLUMN: totals,
        "last_updated_at": game.captured_at,
        "period_last_checked_at": game.period_checked_at or "",
    }


def merge_packed_opening(existing: Any, candidate: str) -> str:
    existing_groups = _decode_groups(str(existing or ""))
    candidate_groups = _decode_groups(candidate)
    merged = []
    for existing_group, candidate_group in zip(
        existing_groups, candidate_groups
    ):
        merged.append(
            tuple(
                old if old is not None else new
                for old, new in zip(existing_group, candidate_group)
            )
        )
    return _encode_groups(merged)


def preserve_unchecked_periods(
    game: GameLines, existing: dict[str, Any] | None
) -> GameLines:
    if game.period_checked_at or not existing:
        return game
    away = _decode_groups(str(existing.get(LATEST_AWAY_COLUMN, "")))
    home = _decode_groups(str(existing.get(LATEST_HOME_COLUMN, "")))
    totals = _decode_groups(str(existing.get(LATEST_TOTALS_COLUMN, "")))
    changes: dict[str, Any] = {}
    for index, period in enumerate(PERIOD_PREFIXES, start=1):
        changes.update(
            {
                f"{period}_away_spread": away[index][0],
                f"{period}_away_spread_price": away[index][1],
                f"{period}_away_moneyline": away[index][2],
                f"{period}_home_spread": home[index][0],
                f"{period}_home_spread_price": home[index][1],
                f"{period}_home_moneyline": home[index][2],
                f"{period}_total": totals[index][0],
                f"{period}_over_price": totals[index][1],
                f"{period}_under_price": totals[index][2],
            }
        )
    return replace(game, **changes)


def update_game_row(existing: dict[str, Any], game: GameLines) -> dict[str, Any]:
    updated = dict(existing)
    away = encode_team_snapshot(game, "away")
    home = encode_team_snapshot(game, "home")
    totals = encode_totals_snapshot(game)
    updated[OPENING_AWAY_COLUMN] = merge_packed_opening(
        updated.get(OPENING_AWAY_COLUMN), away
    )
    updated[OPENING_HOME_COLUMN] = merge_packed_opening(
        updated.get(OPENING_HOME_COLUMN), home
    )
    updated[OPENING_TOTALS_COLUMN] = merge_packed_opening(
        updated.get(OPENING_TOTALS_COLUMN), totals
    )
    updated.update(
        {
            "season": game.season,
            "season_type": game.season_type,
            "status": "upcoming",
            "commence_time_utc": game.commence_time_utc,
            "commence_time_et": game.commence_time_et,
            "away_team": game.away_team,
            "home_team": game.home_team,
            "bookmaker": game.bookmaker,
            "latest_captured_at": game.captured_at,
            LATEST_AWAY_COLUMN: away,
            LATEST_HOME_COLUMN: home,
            LATEST_TOTALS_COLUMN: totals,
            "last_updated_at": game.captured_at,
        }
    )
    if game.period_checked_at:
        updated["period_last_checked_at"] = game.period_checked_at
    return {header: updated.get(header, "") for header in GAME_HEADERS}


def snapshot_signature(row: dict[str, Any]) -> tuple[Any, ...]:
    if not row.get(AWAY_SNAPSHOT_COLUMN):
        return ()
    away = _decode_groups(str(row.get(AWAY_SNAPSHOT_COLUMN, "")))
    home = _decode_groups(str(row.get(HOME_SNAPSHOT_COLUMN, "")))
    totals = _decode_groups(str(row.get(TOTALS_SNAPSHOT_COLUMN, "")))
    values: list[Any] = []
    for index in range(3):
        values.extend(
            (
                away[index][0],
                away[index][1],
                home[index][0],
                home[index][1],
                away[index][2],
                home[index][2],
                totals[index][0],
                totals[index][1],
                totals[index][2],
            )
        )
    return tuple(values)


def _decode_groups(value: str) -> list[tuple[float | int | None, ...]]:
    groups = []
    for raw_group in value.split("|"):
        parsed = []
        for raw_value in raw_group.split(","):
            if raw_value in ("", NO_DATA):
                parsed.append(None)
                continue
            number = float(raw_value)
            parsed.append(int(number) if number.is_integer() else number)
        groups.append(tuple(parsed))
    while len(groups) < 3:
        groups.append((None, None, None))
    return groups[:3]


def decode_packed_markets(
    away_value: str, home_value: str, totals_value: str
) -> dict[str, dict[str, float | int | None]]:
    away = _decode_groups(away_value)
    home = _decode_groups(home_value)
    totals = _decode_groups(totals_value)
    decoded = {}
    for index, period in enumerate(
        ("game", "first_half", "first_quarter")
    ):
        decoded[period] = {
            "away_spread": away[index][0],
            "away_spread_price": away[index][1],
            "away_moneyline": away[index][2],
            "home_spread": home[index][0],
            "home_spread_price": home[index][1],
            "home_moneyline": home[index][2],
            "total": totals[index][0],
            "over_price": totals[index][1],
            "under_price": totals[index][2],
        }
    return decoded


def should_append_snapshot(
    game: GameLines, previous: dict[str, Any] | None
) -> bool:
    if previous is None:
        return True
    return game.signature() != snapshot_signature(previous)


def indexed_records(
    values: list[list[str]], key_header: str
) -> list[tuple[int, dict[str, str]]]:
    if not values or key_header not in values[0]:
        return []
    headers = values[0]
    records = []
    for row_number, row in enumerate(values[1:], start=2):
        record = dict(zip(headers, row))
        if record.get(key_header):
            records.append((row_number, record))
    return records


def require_headers(
    values: list[list[str]], expected: list[str], tab_name: str
) -> None:
    actual = values[0] if values else []
    if actual != expected:
        raise ValueError(
            f"{tab_name} headers do not match the expected schema; "
            "run the workbook schema setup before writing"
        )


def write_to_sheets(
    games: list[GameLines], *, sheet_id: str, credentials_b64: str
) -> tuple[int, int]:
    spreadsheet = get_gspread_client(credentials_b64).open_by_key(sheet_id)
    games_ws = spreadsheet.worksheet(SHEET_TABS["games"])
    snapshots_ws = spreadsheet.worksheet(SHEET_TABS["snapshots"])

    game_values = _call_with_retry(games_ws.get_all_values)
    snapshot_values = _call_with_retry(snapshots_ws.get_all_values)
    require_headers(game_values, GAME_HEADERS, SHEET_TABS["games"])
    require_headers(
        snapshot_values, SNAPSHOT_HEADERS, SHEET_TABS["snapshots"]
    )
    games_by_id = {
        row["event_id"]: (row_number, row)
        for row_number, row in indexed_records(game_values, "event_id")
    }
    latest_snapshot_by_id: dict[str, dict[str, Any]] = {}
    for _, row in indexed_records(snapshot_values, "event_id"):
        latest_snapshot_by_id[row["event_id"]] = row

    new_rows: list[list[Any]] = []
    updates: list[dict[str, Any]] = []
    snapshots: list[list[Any]] = []
    processed_event_ids: set[str] = set()
    for game in games:
        if game.event_id in processed_event_ids:
            continue
        processed_event_ids.add(game.event_id)
        existing_entry = games_by_id.get(game.event_id)
        if existing_entry is None:
            row = new_game_row(game)
            new_rows.append([row.get(header, "") for header in GAME_HEADERS])
        else:
            row_number, existing = existing_entry
            game = preserve_unchecked_periods(game, existing)
            row = update_game_row(existing, game)
            updates.append(
                {
                    "range": (
                        f"A{row_number}:"
                        f"{gspread.utils.rowcol_to_a1(row_number, len(GAME_HEADERS))}"
                    ),
                    "values": [[row.get(header, "") for header in GAME_HEADERS]],
                }
            )

        previous_snapshot = latest_snapshot_by_id.get(game.event_id)
        if should_append_snapshot(game, previous_snapshot):
            row = snapshot_row(game)
            snapshots.append(
                [row.get(header, "") for header in SNAPSHOT_HEADERS]
            )
            latest_snapshot_by_id[game.event_id] = row

    if new_rows:
        _call_with_retry(games_ws.append_rows, new_rows, value_input_option="RAW")
    if updates:
        _call_with_retry(games_ws.batch_update, updates, value_input_option="RAW")
    if snapshots:
        _call_with_retry(
            snapshots_ws.append_rows, snapshots, value_input_option="RAW"
        )
    return len(new_rows) + len(updates), len(snapshots)


def format_summary(games: list[GameLines]) -> str:
    if not games:
        return "No upcoming BetOnline NFL games found."
    lines = []
    for game in games:
        summary = (
            f"{game.season_type:9} {game.away_team} @ {game.home_team} | "
            f"{game.commence_time_et} | "
            f"spread {game.away_team} {game.away_spread} "
            f"({game.away_spread_price}) | "
            f"ML {game.away_moneyline}/{game.home_moneyline} | "
            f"total {game.total} ({game.over_price}/{game.under_price})"
        )
        if any(
            getattr(game, f"first_half_{field}") is not None
            for field in LINE_FIELDS
        ):
            summary += (
                f" | H1 spread {game.first_half_away_spread} "
                f"({game.first_half_away_spread_price}) "
                f"ML {game.first_half_away_moneyline}/"
                f"{game.first_half_home_moneyline} "
                f"total {game.first_half_total}"
            )
        if any(
            getattr(game, f"first_quarter_{field}") is not None
            for field in LINE_FIELDS
        ):
            summary += (
                f" | Q1 spread {game.first_quarter_away_spread} "
                f"({game.first_quarter_away_spread_price}) "
                f"ML {game.first_quarter_away_moneyline}/"
                f"{game.first_quarter_home_moneyline} "
                f"total {game.first_quarter_total}"
            )
        lines.append(summary)
    return "\n".join(lines)


def required_env() -> tuple[str, str, str]:
    api_key = os.getenv("ODDS_API_KEY", "")
    credentials = os.getenv("GOOGLE_CREDENTIALS", "")
    sheet_id = os.getenv("NFL_INTAKE_SHEET_ID", "")
    if not api_key:
        raise ValueError("ODDS_API_KEY is not set")
    return api_key, credentials, sheet_id
