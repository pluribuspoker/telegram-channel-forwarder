"""Dedicated native Telegram interface for NFL lean intake."""

from __future__ import annotations

import asyncio
import html
import os
import threading
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from dotenv import load_dotenv
from telethon import Button, TelegramClient, events
from telethon.errors import MessageNotModifiedError
from telethon.sessions import StringSession
from telethon.tl import functions, types
from telethon.tl.types import (
    KeyboardButton,
    KeyboardButtonRow,
    ReplyKeyboardMarkup,
)

from nfl_lines import (
    LEAN_HEADERS,
    LATEST_AWAY_COLUMN,
    LATEST_HOME_COLUMN,
    LATEST_TOTALS_COLUMN,
    OPENING_AWAY_COLUMN,
    OPENING_HOME_COLUMN,
    OPENING_TOTALS_COLUMN,
    decode_packed_markets,
    get_gspread_client,
)
from nfl_win_predictions import (
    PREDICTION_HEADERS,
    TAB_HEADERS,
    TEAM_ABBREVIATIONS,
    build_latest_prediction_rows,
    latest_predictions_for_user,
    replace_rows,
)

ROOT = Path(__file__).resolve().parent
load_dotenv(ROOT / ".env")
load_dotenv(ROOT / ".env.local", override=True)

ET = ZoneInfo("America/New_York")
PAGE_SIZE = 6
WINDOWS = (10, 30, 365)
PERIOD_LABELS = {
    "game": "Full game",
    "first_half": "First half",
    "first_quarter": "First quarter",
}
TEAM_EMOJI_TAB = "team_emojis"
TEAM_EMOJI_HEADERS = ["team_name", "emoji"]
SUGGESTIONS_TAB = "suggestions"
SUGGESTION_HEADERS = [
    "submitted_at_utc",
    "submitted_at_et",
    "telegram_user_id",
    "telegram_username",
    "telegram_first_name",
    "telegram_last_name",
    "telegram_message_id",
    "suggestion",
]
WIN_TOTALS_TAB = "nfl_win_totals"
TEAM_HISTORY_TAB = "nfl_team_history"
WIN_PREDICTIONS_TAB = "nfl_win_predictions"
WIN_PREDICTIONS_LATEST_TAB = "nfl_win_predictions_latest"
_WIN_PREDICTION_WRITE_LOCK = threading.Lock()
DEFAULT_NFL_TEAM_EMOJIS = {
    "Arizona Cardinals": "🐦",
    "Atlanta Falcons": "🦅",
    "Baltimore Ravens": "🐦‍⬛",
    "Buffalo Bills": "🦬",
    "Carolina Panthers": "🐆",
    "Chicago Bears": "🐻",
    "Cincinnati Bengals": "🐅",
    "Cleveland Browns": "🟤",
    "Dallas Cowboys": "⭐",
    "Denver Broncos": "🐴",
    "Detroit Lions": "🦁",
    "Green Bay Packers": "🧀",
    "Houston Texans": "🤠",
    "Indianapolis Colts": "🐎",
    "Jacksonville Jaguars": "🐆",
    "Kansas City Chiefs": "👑",
    "Las Vegas Raiders": "☠️",
    "Los Angeles Chargers": "⚡",
    "Los Angeles Rams": "🐏",
    "Miami Dolphins": "🐬",
    "Minnesota Vikings": "🛡️",
    "New England Patriots": "🇺🇸",
    "New Orleans Saints": "⚜️",
    "New York Giants": "🗽",
    "New York Jets": "✈️",
    "Philadelphia Eagles": "🦅",
    "Pittsburgh Steelers": "🔩",
    "San Francisco 49ers": "⛏️",
    "Seattle Seahawks": "🦅",
    "Tampa Bay Buccaneers": "🏴‍☠️",
    "Tennessee Titans": "⚔️",
    "Washington Commanders": "🪖",
}
DEFAULT_NFL_TEAM_ABBREVS = {
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


def _parse_time(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def allowed_user_ids() -> set[int]:
    return {
        int(value.strip())
        for value in os.getenv("INTAKE_ALLOWED_USER_IDS", "").split(",")
        if value.strip()
    }


def command_keyboard() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        rows=[
            KeyboardButtonRow(
                buttons=[
                    KeyboardButton(text="/guess_nfl_game"),
                    KeyboardButton(text="/predict_nfl_wins"),
                ]
            ),
            KeyboardButtonRow(
                buttons=[
                    KeyboardButton(text="/suggest"),
                ]
            )
        ],
        resize=True,
        single_use=False,
        persistent=True,
        placeholder="Choose an NFL prediction flow",
    )


def select_games(
    records: list[dict[str, Any]],
    *,
    days: int,
    now: datetime,
) -> list[dict[str, Any]]:
    cutoff = now + timedelta(days=days)
    games = []
    for record in records:
        commence = record.get("commence_time_utc")
        if not commence:
            continue
        kickoff = _parse_time(str(commence))
        if now < kickoff <= cutoff:
            games.append(record)
    return sorted(games, key=lambda game: str(game["commence_time_utc"]))


def page_games(
    games: list[dict[str, Any]], page: int
) -> tuple[list[dict[str, Any]], int, int]:
    page_count = max(1, (len(games) + PAGE_SIZE - 1) // PAGE_SIZE)
    normalized_page = min(max(page, 0), page_count - 1)
    start = normalized_page * PAGE_SIZE
    return (
        games[start : start + PAGE_SIZE],
        normalized_page,
        page_count,
    )


def team_abbrev(team: str, team_abbrevs: dict[str, str] | None = None) -> str:
    mapping = DEFAULT_NFL_TEAM_ABBREVS if team_abbrevs is None else team_abbrevs
    return mapping.get(team, team)


def _game_button_label(
    game: dict[str, Any], team_abbrevs: dict[str, str] | None = None
) -> str:
    kickoff = _parse_time(str(game["commence_time_utc"])).astimezone(ET)
    away = team_abbrev(str(game["away_team"]), team_abbrevs)
    home = team_abbrev(str(game["home_team"]), team_abbrevs)
    return f"{kickoff:%-m/%-d} {away}@{home}"


def game_browser(
    records: list[dict[str, Any]],
    *,
    days: int,
    page: int,
    now: datetime,
    team_abbrevs: dict[str, str] | None = None,
) -> tuple[str, list[list[Button]]]:
    games = select_games(records, days=days, now=now)
    current, page, page_count = page_games(games, page)
    text = (
        f"🏈 NFL games in the next {days} days\n"
        f"{len(games)} game{'s' if len(games) != 1 else ''} available"
    )
    if not current:
        text += "\n\nNo BetOnline games are currently available."

    buttons: list[list[Button]] = []
    for game in current:
        callback = f"game:{days}:{page}:{game['event_id']}".encode()
        buttons.append(
            [Button.inline(_game_button_label(game, team_abbrevs), callback)]
        )

    navigation = []
    if page > 0:
        navigation.append(
            Button.inline("◀ Prev", f"games:{days}:{page - 1}".encode())
        )
    if page + 1 < page_count:
        navigation.append(
            Button.inline("Next ▶", f"games:{days}:{page + 1}".encode())
        )
    if navigation:
        buttons.append(navigation)

    filters = [
        Button.inline(
            ("✓ " if window == days else "") + f"{window} days",
            f"games:{window}:0".encode(),
        )
        for window in WINDOWS
    ]
    buttons.append(filters)
    text += f"\nPage {page + 1} of {page_count}"
    return text, buttons


def _signed(value: Any) -> str:
    if value is None:
        return "nodata"
    number = int(value) if isinstance(value, float) and value.is_integer() else value
    if isinstance(number, (int, float)) and number > 0:
        return f"+{number}"
    return str(number)


def _line(value: Any) -> str:
    if value is None:
        return "nodata"
    return str(
        int(value)
        if isinstance(value, float) and value.is_integer()
        else value
    )


def team_emoji(team: str, team_emojis: dict[str, str] | None = None) -> str:
    mapping = DEFAULT_NFL_TEAM_EMOJIS if team_emojis is None else team_emojis
    return mapping.get(team, "🏈")


def implied_score(
    values: dict[str, Any]
) -> tuple[float, float] | None:
    total = values.get("total")
    home_spread = values.get("home_spread")
    if total is None:
        return None
    if home_spread is None:
        away_spread = values.get("away_spread")
        if away_spread is None:
            return None
        home_spread = -away_spread
    away_score = (float(total) + float(home_spread)) / 2
    home_score = (float(total) - float(home_spread)) / 2
    return away_score, home_score


def implied_score_tldr(
    latest: dict[str, dict[str, Any]],
    away: str,
    home: str,
    team_emojis: dict[str, str] | None = None,
) -> str:
    away_icon = team_emoji(away, team_emojis)
    home_icon = team_emoji(home, team_emojis)
    rows = ["<b>TL;DR · BetOnline implied score</b>"]
    for label, period in (
        ("Q1", "first_quarter"),
        ("H1", "first_half"),
        ("Final", "game"),
    ):
        score = implied_score(latest[period])
        if score is None:
            rows.append(f"{label}: {away_icon} nodata · {home_icon} nodata")
        else:
            away_score, home_score = score
            rows.append(
                f"{label}: {away_icon} {_line(away_score)} · "
                f"{home_icon} {_line(home_score)}"
            )
    return "\n".join(rows)


def _period_lines(
    label: str,
    opening: dict[str, Any],
    latest: dict[str, Any],
    away: str,
    home: str,
    team_emojis: dict[str, str] | None = None,
) -> str:
    if all(value is None for value in latest.values()):
        return f"<b>{label}</b>\nNo BetOnline data yet."

    away_icon = team_emoji(away, team_emojis)
    home_icon = team_emoji(home, team_emojis)

    def snapshot(
        name: str, values: dict[str, Any], *, heading_newlines: str
    ) -> str:
        return (
            f"{name}{heading_newlines}"
            f"<u>Spread</u>: {away_icon} {_signed(values['away_spread'])} "
            f"({_signed(values['away_spread_price'])}) · "
            f"{home_icon} {_signed(values['home_spread'])} "
            f"({_signed(values['home_spread_price'])})\n"
            f"<u>Moneyline</u>: {away_icon} {_signed(values['away_moneyline'])} · "
            f"{home_icon} {_signed(values['home_moneyline'])}\n"
            f"<u>Total</u>: {_line(values['total'])} "
            f"(O {_signed(values['over_price'])} / "
            f"U {_signed(values['under_price'])})"
        )

    return (
        f"<b>{label}</b>\n"
        f"{snapshot('Opening', opening, heading_newlines='\n\n')}\n\n"
        f"{snapshot('Latest', latest, heading_newlines='\n')}"
    )


def game_detail(
    game: dict[str, Any],
    *,
    days: int,
    page: int,
    team_emojis: dict[str, str] | None = None,
) -> tuple[str, list[list[Button]]]:
    opening = decode_packed_markets(
        str(game[OPENING_AWAY_COLUMN]),
        str(game[OPENING_HOME_COLUMN]),
        str(game[OPENING_TOTALS_COLUMN]),
    )
    latest = decode_packed_markets(
        str(game[LATEST_AWAY_COLUMN]),
        str(game[LATEST_HOME_COLUMN]),
        str(game[LATEST_TOTALS_COLUMN]),
    )
    kickoff = _parse_time(str(game["commence_time_utc"])).astimezone(ET)
    away = str(game["away_team"])
    home = str(game["home_team"])
    sections = [
        (
            f"🏈 <b>{team_emoji(away, team_emojis)} {html.escape(away)} @ "
            f"{team_emoji(home, team_emojis)} {html.escape(home)}</b>"
        ),
        f"{kickoff:%A, %B %-d at %-I:%M %p ET}",
        implied_score_tldr(latest, away, home, team_emojis),
        f"Book: {html.escape(str(game['bookmaker']))}",
        _period_lines(
            "Full game",
            opening["game"],
            latest["game"],
            away,
            home,
            team_emojis,
        ),
        _period_lines(
            "First half",
            opening["first_half"],
            latest["first_half"],
            away,
            home,
            team_emojis,
        ),
        _period_lines(
            "First quarter",
            opening["first_quarter"],
            latest["first_quarter"],
            away,
            home,
            team_emojis,
        ),
    ]
    buttons = [
        [
            Button.inline("Full game", b"period:game"),
            Button.inline("First half", b"period:first_half"),
            Button.inline("First quarter", b"period:first_quarter"),
        ],
        [Button.inline("← Back to games", f"games:{days}:{page}".encode())]
    ]
    return "\n\n".join(sections), buttons


def market_buttons() -> list[list[Button]]:
    return [
        [
            Button.inline("Spread", b"market:spread"),
            Button.inline("Moneyline", b"market:moneyline"),
            Button.inline("Total", b"market:total"),
        ],
        [Button.inline("← Back to periods", b"back:game")],
    ]


def side_buttons(
    market: str, away: str, home: str
) -> list[list[Button]]:
    if market == "total":
        return [
            [
                Button.inline("Over", b"side:over"),
                Button.inline("Under", b"side:under"),
            ],
            [Button.inline("← Back to markets", b"back:markets")],
        ]
    return [
        [
            Button.inline(away, b"side:away"),
            Button.inline(home, b"side:home"),
        ],
        [Button.inline("← Back to markets", b"back:markets")],
    ]


def selected_market_context(
    game: dict[str, Any],
    *,
    period: str,
    market: str,
    side: str,
) -> dict[str, Any]:
    opening = decode_packed_markets(
        str(game[OPENING_AWAY_COLUMN]),
        str(game[OPENING_HOME_COLUMN]),
        str(game[OPENING_TOTALS_COLUMN]),
    )[period]
    latest = decode_packed_markets(
        str(game[LATEST_AWAY_COLUMN]),
        str(game[LATEST_HOME_COLUMN]),
        str(game[LATEST_TOTALS_COLUMN]),
    )[period]

    def values(snapshot: dict[str, Any]) -> tuple[Any, Any]:
        if market == "spread":
            return (
                snapshot[f"{side}_spread"],
                snapshot[f"{side}_spread_price"],
            )
        if market == "moneyline":
            return None, snapshot[f"{side}_moneyline"]
        return snapshot["total"], snapshot[f"{side}_price"]

    opening_line, opening_price = values(opening)
    latest_line, latest_price = values(latest)
    return {
        "opening_line": opening_line,
        "opening_price": opening_price,
        "latest_line": latest_line,
        "latest_price": latest_price,
    }


def selection_side_label(game: dict[str, Any], market: str, side: str) -> str:
    if market == "total":
        return side.title()
    return str(game[f"{side}_team"])


def selection_price_text(
    market: str, side_label: str, line: Any, price: Any
) -> str:
    if market == "moneyline":
        return f"{side_label} {_signed(price)}"
    if market == "total":
        return f"{side_label} {_line(line)} ({_signed(price)})"
    return f"{side_label} {_signed(line)} ({_signed(price)})"


def period_market_summary(
    game: dict[str, Any],
    *,
    period: str,
    team_emojis: dict[str, str] | None = None,
) -> str:
    opening = decode_packed_markets(
        str(game[OPENING_AWAY_COLUMN]),
        str(game[OPENING_HOME_COLUMN]),
        str(game[OPENING_TOTALS_COLUMN]),
    )[period]
    latest = decode_packed_markets(
        str(game[LATEST_AWAY_COLUMN]),
        str(game[LATEST_HOME_COLUMN]),
        str(game[LATEST_TOTALS_COLUMN]),
    )[period]
    away = str(game["away_team"])
    home = str(game["home_team"])
    return "\n\n".join(
        [
            (
                f"🏈 <b>{team_emoji(away, team_emojis)} {html.escape(away)} @ "
                f"{team_emoji(home, team_emojis)} {html.escape(home)}</b>"
            ),
            _period_lines(
                PERIOD_LABELS[period],
                opening,
                latest,
                away,
                home,
                team_emojis,
            ),
            "Choose a market:",
        ]
    )


def market_side_summary(
    game: dict[str, Any],
    *,
    period: str,
    market: str,
    team_emojis: dict[str, str] | None = None,
) -> str:
    opening = decode_packed_markets(
        str(game[OPENING_AWAY_COLUMN]),
        str(game[OPENING_HOME_COLUMN]),
        str(game[OPENING_TOTALS_COLUMN]),
    )[period]
    latest = decode_packed_markets(
        str(game[LATEST_AWAY_COLUMN]),
        str(game[LATEST_HOME_COLUMN]),
        str(game[LATEST_TOTALS_COLUMN]),
    )[period]
    away = str(game["away_team"])
    home = str(game["home_team"])
    away_icon = team_emoji(away, team_emojis)
    home_icon = team_emoji(home, team_emojis)

    def values(snapshot: dict[str, Any]) -> str:
        if market == "spread":
            return (
                f"{away_icon} {_signed(snapshot['away_spread'])} "
                f"({_signed(snapshot['away_spread_price'])}) · "
                f"{home_icon} {_signed(snapshot['home_spread'])} "
                f"({_signed(snapshot['home_spread_price'])})"
            )
        if market == "moneyline":
            return (
                f"{away_icon} {_signed(snapshot['away_moneyline'])} · "
                f"{home_icon} {_signed(snapshot['home_moneyline'])}"
            )
        return (
            f"Over {_line(snapshot['total'])} "
            f"({_signed(snapshot['over_price'])}) · "
            f"Under {_line(snapshot['total'])} "
            f"({_signed(snapshot['under_price'])})"
        )

    return "\n\n".join(
        [
            (
                f"🏈 <b>{team_emoji(away, team_emojis)} {html.escape(away)} @ "
                f"{team_emoji(home, team_emojis)} {html.escape(home)}</b>"
            ),
            f"<b>{PERIOD_LABELS[period]} · {market.title()}</b>",
            f"Opening\n{values(opening)}\n\nLatest\n{values(latest)}",
            "Choose a side:",
        ]
    )


def build_lean_row(
    *,
    submitted_at: datetime,
    user_id: int,
    username: str | None,
    first_name: str | None,
    last_name: str | None,
    message_id: int,
    game: dict[str, Any],
    period: str,
    market: str,
    side: str,
    lean_text: str,
) -> dict[str, Any]:
    context = selected_market_context(
        game, period=period, market=market, side=side
    )
    submitted_at_utc = submitted_at.astimezone(timezone.utc)

    def stored(value: Any) -> Any:
        return "nodata" if value is None else value

    return {
        "submission_id": f"telegram:{user_id}:{message_id}",
        "submitted_at_utc": submitted_at_utc.isoformat(),
        "submitted_at_et": submitted_at_utc.astimezone(ET).isoformat(),
        "telegram_user_id": user_id,
        "telegram_username": username or "",
        "telegram_first_name": first_name or "",
        "telegram_last_name": last_name or "",
        "telegram_message_id": message_id,
        "event_id": game.get("event_id", ""),
        "season": game.get("season", ""),
        "season_type": game.get("season_type", ""),
        "week": game.get("week", ""),
        "commence_time_utc": game.get("commence_time_utc", ""),
        "commence_time_et": game.get("commence_time_et", ""),
        "away_team": game.get("away_team", ""),
        "home_team": game.get("home_team", ""),
        "bookmaker": game.get("bookmaker", ""),
        "period": period,
        "market": market,
        "side": selection_side_label(game, market, side),
        "opening_captured_at": game.get("opening_captured_at", ""),
        "latest_captured_at": game.get("latest_captured_at", ""),
        "opening_selected_line": stored(context["opening_line"]),
        "opening_selected_price": stored(context["opening_price"]),
        "latest_selected_line": stored(context["latest_line"]),
        "latest_selected_price": stored(context["latest_price"]),
        OPENING_AWAY_COLUMN: game.get(OPENING_AWAY_COLUMN, ""),
        OPENING_HOME_COLUMN: game.get(OPENING_HOME_COLUMN, ""),
        OPENING_TOTALS_COLUMN: game.get(OPENING_TOTALS_COLUMN, ""),
        LATEST_AWAY_COLUMN: game.get(LATEST_AWAY_COLUMN, ""),
        LATEST_HOME_COLUMN: game.get(LATEST_HOME_COLUMN, ""),
        LATEST_TOTALS_COLUMN: game.get(LATEST_TOTALS_COLUMN, ""),
        "lean_text": lean_text,
    }


def snapshot_lean_submission(
    state: dict[str, Any] | None,
    *,
    reply_to_msg_id: int | None,
) -> tuple[str, dict[str, Any] | None]:
    if (
        state is None
        or state.get("prompt_msg_id") != reply_to_msg_id
    ):
        return "unrelated", None

    game = state.get("game")
    period = state.get("period")
    market = state.get("market")
    side = state.get("side")
    valid_sides = (
        {"over", "under"} if market == "total" else {"away", "home"}
    )
    if (
        not isinstance(game, dict)
        or period not in PERIOD_LABELS
        or market not in {"spread", "moneyline", "total"}
        or side not in valid_sides
    ):
        return "invalid", None

    return (
        "ready",
        {
            "game": dict(game),
            "period": period,
            "market": market,
            "side": side,
            "prompt_msg_id": state["prompt_msg_id"],
        },
    )


def _record(wins: Any, losses: Any, ties: Any = 0) -> str:
    values = [str(int(wins)), str(int(losses))]
    if int(ties or 0):
        values.append(str(int(ties)))
    return "–".join(values)


def _number_text(value: float) -> str:
    return str(int(value)) if value.is_integer() else str(value)


def _ordinal(value: int) -> str:
    if 10 <= value % 100 <= 20:
        suffix = "th"
    else:
        suffix = {1: "st", 2: "nd", 3: "rd"}.get(value % 10, "th")
    return f"{value}{suffix}"


def _team_by_abbreviation(abbreviation: str) -> str | None:
    return next(
        (
            team
            for team, candidate in TEAM_ABBREVIATIONS.items()
            if candidate == abbreviation
        ),
        None,
    )


def _current_win_totals(
    totals: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    if not totals:
        raise ValueError("No NFL win totals are available")
    season = max(int(row["season"]) for row in totals)
    current = [row for row in totals if int(row["season"]) == season]
    if len(current) != 32:
        raise ValueError(
            f"NFL win totals for {season} contain {len(current)} teams"
        )
    return current


def win_prediction_browser(
    totals: list[dict[str, Any]],
    predictions: list[dict[str, Any]],
    *,
    user_id: int,
) -> tuple[str, list[list[Button]]]:
    current_totals = _current_win_totals(totals)
    market_by_team = {str(row["team"]): row for row in current_totals}
    latest, counts = latest_predictions_for_user(predictions, user_id)
    teams = sorted(
        market_by_team,
        key=lambda team: (
            counts[team],
            TEAM_ABBREVIATIONS.get(team, team),
        ),
    )
    progress = len(latest)
    season = int(current_totals[0]["season"])
    text = (
        f"🏈 <b>{season} NFL Win Predictions</b>\n"
        f"Progress: {progress}/32 teams\n\n"
        "Choose a team. Unmarked teams are shown first."
    )
    buttons: list[list[Button]] = []
    row: list[Button] = []
    for team in teams:
        abbreviation = TEAM_ABBREVIATIONS[team]
        previous = latest.get(team)
        label = abbreviation
        if previous:
            label += f" · {previous['predicted_wins']}"
        row.append(
            Button.inline(label, f"winteam:{abbreviation}".encode())
        )
        if len(row) == 4:
            buttons.append(row)
            row = []
    if row:
        buttons.append(row)
    return text, buttons


def _history_index(
    history: list[dict[str, Any]],
) -> dict[tuple[int, str], dict[str, Any]]:
    return {
        (int(row["season"]), str(row["team"])): row
        for row in history
    }


def win_prediction_team_detail(
    totals: list[dict[str, Any]],
    history: list[dict[str, Any]],
    predictions: list[dict[str, Any]],
    *,
    user_id: int,
    abbreviation: str,
) -> tuple[str, list[list[Button]]]:
    team = _team_by_abbreviation(abbreviation)
    if team is None:
        raise ValueError(f"Unknown NFL team abbreviation: {abbreviation}")
    market = next(
        row for row in _current_win_totals(totals)
        if str(row["team"]) == team
    )
    season = int(market["season"])
    prior_season = season - 1
    history_by_key = _history_index(history)
    prior = history_by_key[(prior_season, team)]
    division = str(prior["division"])
    rank = int(prior["division_rank"])
    division_rows = sorted(
        (
            row
            for row in history
            if int(row["season"]) == prior_season
            and str(row["division"]) == division
        ),
        key=lambda row: int(row["division_rank"]),
    )
    latest, _ = latest_predictions_for_user(predictions, user_id)
    sections = [
        f"🏈 <b>{html.escape(team)} ({abbreviation})</b>",
        f"BetOnline total: {_number_text(float(market['win_total']))} wins",
    ]
    previous = latest.get(team)
    if previous:
        sections.append(
            f"Your previous prediction: {previous['predicted_wins']} wins"
        )
    standings = [f"<b>{prior_season} {html.escape(division)}:</b>"]
    for row in division_rows:
        row_abbreviation = str(row["team_abbreviation"])
        label = (
            f"({row_abbreviation})"
            if row_abbreviation == abbreviation
            else row_abbreviation
        )
        standings.append(
            f"{int(row['division_rank'])}. {label} "
            f"{_record(row['wins'], row['losses'], row['ties'])}"
        )
    sections.append("\n".join(standings))

    cohorts: list[str] = [
        (
            "<b>Historical results for teams previously finishing "
            f"{_ordinal(rank)}:</b>"
        )
    ]
    completed_seasons = sorted(
        {
            int(row["season"])
            for row in history
            if row.get("next_season_wins") != ""
        }
    )[-2:]
    for cohort_season in completed_seasons:
        same_rank = [
            row
            for row in history
            if int(row["season"]) == cohort_season
            and int(row["division_rank"]) == rank
            and row.get("next_season_wins") != ""
        ]
        same_division = next(
            row for row in same_rank if str(row["division"]) == division
        )
        next_row = history_by_key[
            (cohort_season + 1, str(same_division["team"]))
        ]
        other_wins = sorted(
            (
                int(row["next_season_wins"])
                for row in same_rank
                if str(row["division"]) != division
            ),
            reverse=True,
        )
        if len(other_wins) != 7:
            raise ValueError(
                f"{cohort_season} rank {rank} has "
                f"{len(other_wins)} other-division results"
            )
        average = sum(other_wins) / len(other_wins)
        same_abbreviation = str(same_division["team_abbreviation"])
        cohorts.append(
            "\n".join(
                [
                    f"<b>{cohort_season} → {cohort_season + 1}</b>",
                    "Same division:",
                    (
                        f"{same_abbreviation} finished {_ordinal(rank)} in the "
                        f"{cohort_season} {html.escape(division)} at "
                        f"{_record(same_division['wins'], same_division['losses'], same_division['ties'])}."
                    ),
                    (
                        f"Their {cohort_season + 1} record: "
                        f"{_record(next_row['wins'], next_row['losses'], next_row['ties'])}."
                    ),
                    "",
                    "Other 7 divisions:",
                    (
                        f"Average {cohort_season + 1} wins: "
                        f"{average:.2f}"
                    ),
                    f"({', '.join(str(wins) for wins in other_wins)})",
                ]
            )
        )
    sections.append("\n\n".join(cohorts))
    sections.append("How many regular-season wins do you predict?")

    buttons = [
        [
            Button.inline(
                str(wins),
                f"winpick:{abbreviation}:{wins}".encode(),
            )
            for wins in range(start, start + 6)
        ]
        for start in (0, 6, 12)
    ]
    buttons.append([Button.inline("← Teams", b"wins:teams")])
    return "\n\n".join(sections), buttons


def win_prediction_confirmation(
    totals: list[dict[str, Any]],
    *,
    abbreviation: str,
    predicted_wins: int,
) -> tuple[str, list[list[Button]]]:
    team = _team_by_abbreviation(abbreviation)
    if team is None:
        raise ValueError(f"Unknown NFL team abbreviation: {abbreviation}")
    market = next(
        row for row in _current_win_totals(totals)
        if str(row["team"]) == team
    )
    total = float(market["win_total"])
    difference = predicted_wins - total
    text = (
        f"Confirm <b>{html.escape(team)}</b>: {predicted_wins} wins?\n\n"
        f"BetOnline: {_number_text(total)}\n"
        f"Your prediction: {predicted_wins}\n"
        f"Difference: {difference:+g} wins"
    )
    return text, [
        [
            Button.inline(
                "Save prediction",
                f"winsave:{abbreviation}:{predicted_wins}".encode(),
            ),
            Button.inline(
                "Change",
                f"winteam:{abbreviation}".encode(),
            ),
        ]
    ]


def build_win_prediction_row(
    *,
    submitted_at: datetime,
    user_id: int,
    username: str | None,
    first_name: str | None,
    last_name: str | None,
    team: str,
    predicted_wins: int,
    market: dict[str, Any],
    prior: dict[str, Any],
) -> dict[str, Any]:
    submitted_at_utc = submitted_at.astimezone(timezone.utc)
    display_name = " ".join(
        value for value in (first_name, last_name) if value
    )
    return {
        "revision_id": str(uuid.uuid4()),
        "submitted_at_utc": submitted_at_utc.isoformat(),
        "submitted_at_et": submitted_at_utc.astimezone(ET).isoformat(),
        "telegram_user_id": user_id,
        "telegram_username": username or "",
        "telegram_display_name": display_name,
        "season": market["season"],
        "team": team,
        "team_abbreviation": TEAM_ABBREVIATIONS[team],
        "predicted_wins": predicted_wins,
        "market_win_total": market["win_total"],
        "market_captured_at_et": market["captured_at_et"],
        "prior_season": prior["season"],
        "prior_division": prior["division"],
        "prior_division_rank": prior["division_rank"],
        "actual_wins_at_submission": "",
        "actual_week_at_submission": "",
    }


def load_win_prediction_data(
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    credentials = os.environ["GOOGLE_CREDENTIALS"]
    sheet_id = os.environ["NFL_INTAKE_SHEET_ID"]
    spreadsheet = get_gspread_client(credentials).open_by_key(sheet_id)
    totals = spreadsheet.worksheet(WIN_TOTALS_TAB).get_all_records(
        expected_headers=TAB_HEADERS[WIN_TOTALS_TAB]
    )
    history = spreadsheet.worksheet(TEAM_HISTORY_TAB).get_all_records(
        expected_headers=TAB_HEADERS[TEAM_HISTORY_TAB]
    )
    predictions = spreadsheet.worksheet(
        WIN_PREDICTIONS_TAB
    ).get_all_records(expected_headers=PREDICTION_HEADERS)
    return totals, history, predictions


def append_win_prediction(row: dict[str, Any]) -> bool:
    credentials = os.environ["GOOGLE_CREDENTIALS"]
    sheet_id = os.environ["NFL_INTAKE_SHEET_ID"]
    with _WIN_PREDICTION_WRITE_LOCK:
        spreadsheet = get_gspread_client(credentials).open_by_key(sheet_id)
        predictions_worksheet = spreadsheet.worksheet(WIN_PREDICTIONS_TAB)
        predictions = predictions_worksheet.get_all_records(
            expected_headers=PREDICTION_HEADERS
        )
        latest, _ = latest_predictions_for_user(
            predictions, row["telegram_user_id"]
        )
        previous = latest.get(str(row["team"]))
        if (
            previous is not None
            and int(previous["predicted_wins"])
            == int(row["predicted_wins"])
        ):
            return False
        predictions_worksheet.append_row(
            [row.get(header, "") for header in PREDICTION_HEADERS],
            value_input_option="RAW",
        )
        predictions.append(row)
        totals = spreadsheet.worksheet(WIN_TOTALS_TAB).get_all_records(
            expected_headers=TAB_HEADERS[WIN_TOTALS_TAB]
        )
        latest_rows = build_latest_prediction_rows(predictions, totals)
        replace_rows(
            spreadsheet.worksheet(WIN_PREDICTIONS_LATEST_TAB),
            TAB_HEADERS[WIN_PREDICTIONS_LATEST_TAB],
            latest_rows,
        )
        return True


def load_intake_data() -> tuple[list[dict[str, Any]], dict[str, str], dict[str, str]]:
    credentials = os.environ["GOOGLE_CREDENTIALS"]
    sheet_id = os.environ["NFL_INTAKE_SHEET_ID"]
    spreadsheet = get_gspread_client(credentials).open_by_key(sheet_id)
    games = spreadsheet.worksheet("nfl_games").get_all_records()
    emoji_rows = spreadsheet.worksheet(TEAM_EMOJI_TAB).get_all_records(
        expected_headers=TEAM_EMOJI_HEADERS
    )
    team_emojis = {
        str(row["team_name"]).strip(): str(row["emoji"]).strip()
        for row in emoji_rows
        if str(row.get("team_name", "")).strip()
        and str(row.get("emoji", "")).strip()
    }
    team_abbrevs = {
        str(row["team_name"]).strip(): str(row.get("abbreviation", "")).strip()
        for row in emoji_rows
        if str(row.get("team_name", "")).strip()
        and str(row.get("abbreviation", "")).strip()
    }
    return games, team_emojis, team_abbrevs


def build_suggestion_row(
    *,
    submitted_at: datetime,
    user_id: int,
    username: str | None,
    first_name: str | None,
    last_name: str | None,
    message_id: int,
    suggestion: str,
) -> dict[str, Any]:
    submitted_at_utc = submitted_at.astimezone(timezone.utc)
    return {
        "submitted_at_utc": submitted_at_utc.isoformat(),
        "submitted_at_et": submitted_at_utc.astimezone(ET).isoformat(),
        "telegram_user_id": user_id,
        "telegram_username": username or "",
        "telegram_first_name": first_name or "",
        "telegram_last_name": last_name or "",
        "telegram_message_id": message_id,
        "suggestion": suggestion,
    }


def append_suggestion(row: dict[str, Any]) -> None:
    credentials = os.environ["GOOGLE_CREDENTIALS"]
    sheet_id = os.environ["NFL_INTAKE_SHEET_ID"]
    worksheet = (
        get_gspread_client(credentials)
        .open_by_key(sheet_id)
        .worksheet(SUGGESTIONS_TAB)
    )
    worksheet.append_row(
        [row.get(header, "") for header in SUGGESTION_HEADERS],
        value_input_option="RAW",
    )


def append_lean(row: dict[str, Any]) -> bool:
    credentials = os.environ["GOOGLE_CREDENTIALS"]
    sheet_id = os.environ["NFL_INTAKE_SHEET_ID"]
    worksheet = (
        get_gspread_client(credentials)
        .open_by_key(sheet_id)
        .worksheet("nfl_leans")
    )
    headers = worksheet.row_values(1)
    if headers != LEAN_HEADERS:
        raise RuntimeError(
            "nfl_leans headers do not match the finalized schema"
        )
    submission_id = str(row["submission_id"])
    if submission_id in set(worksheet.col_values(1)[1:]):
        return False
    worksheet.append_row(
        [row.get(header, "") for header in LEAN_HEADERS],
        value_input_option="RAW",
    )
    return True


async def _intake_data() -> tuple[list[dict[str, Any]], dict[str, str], dict[str, str]]:
    return await asyncio.to_thread(load_intake_data)


async def _win_prediction_data(
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    return await asyncio.to_thread(load_win_prediction_data)


async def edit_callback(event, text: str, buttons) -> None:
    try:
        await event.edit(text, buttons=buttons, parse_mode="html")
    except MessageNotModifiedError:
        pass
    await event.answer()


async def main() -> None:
    api_id = int(os.environ["TELEGRAM_API_ID"])
    api_hash = os.environ["TELEGRAM_API_HASH"]
    token = os.environ["INTAKE_BOT_TOKEN"]
    session = os.getenv("INTAKE_BOT_SESSION", "")
    allowed = allowed_user_ids()
    if not allowed:
        raise RuntimeError("INTAKE_ALLOWED_USER_IDS is empty")

    client = TelegramClient(StringSession(session), api_id, api_hash)
    pending_suggestions: dict[int, int] = {}
    guess_states: dict[int, dict[str, Any]] = {}

    @client.on(
        events.NewMessage(
            pattern=r"^/(?:start|guess_nfl_game|predict_nfl_wins)(?:@\w+)?$",
            incoming=True,
            func=lambda event: event.is_private,
        )
    )
    async def show_games(event):
        if event.sender_id not in allowed:
            await event.respond("Not authorized.")
            return
        if event.raw_text.startswith("/start"):
            await event.respond(
                "Tap /guess_nfl_game to browse games, /predict_nfl_wins "
                "to predict season totals, or /suggest to send feedback.",
                buttons=command_keyboard(),
            )
        if event.raw_text.startswith("/predict_nfl_wins"):
            guess_states.pop(event.sender_id, None)
            totals, _, predictions = await _win_prediction_data()
            text, buttons = win_prediction_browser(
                totals,
                predictions,
                user_id=event.sender_id,
            )
            await event.respond(text, buttons=buttons, parse_mode="html")
            return
        guess_states.pop(event.sender_id, None)
        records, _, team_abbrevs = await _intake_data()
        text, buttons = game_browser(
            records,
            days=10,
            page=0,
            now=datetime.now(timezone.utc),
            team_abbrevs=team_abbrevs,
        )
        await event.respond(text, buttons=buttons)

    @client.on(
        events.NewMessage(
            pattern=r"^/suggest(?:@\w+)?$",
            incoming=True,
            func=lambda event: event.is_private,
        )
    )
    async def request_suggestion(event):
        if event.sender_id not in allowed:
            await event.respond("Not authorized.")
            return
        prompt = await event.respond(
            "What would you like to suggest?",
            buttons=Button.force_reply(
                single_use=True,
                placeholder="Type your suggestion",
            ),
        )
        pending_suggestions[event.sender_id] = prompt.id

    @client.on(
        events.NewMessage(
            incoming=True,
            func=lambda event: event.is_private,
        )
    )
    async def capture_free_text(event):
        if event.sender_id not in allowed:
            return
        suggestion_prompt_id = pending_suggestions.get(event.sender_id)
        if (
            suggestion_prompt_id is not None
            and event.reply_to_msg_id == suggestion_prompt_id
        ):
            suggestion = event.raw_text.strip()
            if not suggestion:
                await event.respond(
                    "Suggestion cannot be empty. Reply to the prompt with some text."
                )
                return
            sender = await event.get_sender()
            row = build_suggestion_row(
                submitted_at=datetime.now(timezone.utc),
                user_id=event.sender_id,
                username=getattr(sender, "username", None),
                first_name=getattr(sender, "first_name", None),
                last_name=getattr(sender, "last_name", None),
                message_id=event.id,
                suggestion=suggestion,
            )
            await asyncio.to_thread(append_suggestion, row)
            pending_suggestions.pop(event.sender_id, None)
            await event.respond(
                "✅ Suggestion saved. Thank you.",
                buttons=command_keyboard(),
            )
            return

        state = guess_states.get(event.sender_id)
        submission_status, submission = snapshot_lean_submission(
            state,
            reply_to_msg_id=event.reply_to_msg_id,
        )
        if submission_status == "unrelated":
            return
        if submission_status == "invalid":
            log.warning(
                "Rejected stale NFL lean reply user=%s message=%s prompt=%s",
                event.sender_id,
                event.id,
                event.reply_to_msg_id,
            )
            await event.respond(
                "That selection changed or expired, so this lean wasn't "
                "saved. Continue from the current menu or restart with "
                "/guess_nfl_game.",
                buttons=command_keyboard(),
            )
            return
        assert submission is not None
        lean_text = event.raw_text.strip()
        if not lean_text:
            await event.respond(
                "Your lean cannot be empty. Reply to the prompt with some text."
            )
            return
        sender = await event.get_sender()
        row = build_lean_row(
            submitted_at=datetime.now(timezone.utc),
            user_id=event.sender_id,
            username=getattr(sender, "username", None),
            first_name=getattr(sender, "first_name", None),
            last_name=getattr(sender, "last_name", None),
            message_id=event.id,
            game=submission["game"],
            period=submission["period"],
            market=submission["market"],
            side=submission["side"],
            lean_text=lean_text,
        )
        appended = await asyncio.to_thread(append_lean, row)
        game = submission["game"]
        summary = (
            f"{PERIOD_LABELS[submission['period']]} · "
            f"{submission['market'].title()} · "
            f"{selection_side_label(game, submission['market'], submission['side'])}"
        )
        current_state = guess_states.get(event.sender_id)
        if (
            current_state is state
            and current_state.get("prompt_msg_id")
            == submission["prompt_msg_id"]
        ):
            guess_states.pop(event.sender_id, None)
        status = "✅ Guess saved." if appended else "✅ Guess was already saved."
        await event.respond(
            f"{status}\n{summary}",
            buttons=command_keyboard(),
        )

    @client.on(events.CallbackQuery)
    async def handle_callback(event):
        if event.sender_id not in allowed:
            await event.answer("Not authorized.", alert=True)
            return
        data = event.data.decode()
        if (
            data == "wins:teams"
            or data.startswith("winteam:")
            or data.startswith("winpick:")
            or data.startswith("winsave:")
        ):
            totals, history, predictions = await _win_prediction_data()
            if data == "wins:teams":
                text, buttons = win_prediction_browser(
                    totals,
                    predictions,
                    user_id=event.sender_id,
                )
                await edit_callback(event, text, buttons)
                return
            if data.startswith("winteam:"):
                abbreviation = data.split(":", 1)[1]
                try:
                    text, buttons = win_prediction_team_detail(
                        totals,
                        history,
                        predictions,
                        user_id=event.sender_id,
                        abbreviation=abbreviation,
                    )
                except (KeyError, StopIteration, ValueError):
                    await event.answer(
                        "Team data is unavailable.", alert=True
                    )
                    return
                await edit_callback(event, text, buttons)
                return
            if data.startswith("winpick:"):
                _, abbreviation, wins_raw = data.split(":", 2)
                predicted_wins = int(wins_raw)
                if not 0 <= predicted_wins <= 17:
                    await event.answer("Invalid prediction.", alert=True)
                    return
                try:
                    text, buttons = win_prediction_confirmation(
                        totals,
                        abbreviation=abbreviation,
                        predicted_wins=predicted_wins,
                    )
                except (StopIteration, ValueError):
                    await event.answer(
                        "Team data is unavailable.", alert=True
                    )
                    return
                await edit_callback(event, text, buttons)
                return
            _, abbreviation, wins_raw = data.split(":", 2)
            predicted_wins = int(wins_raw)
            team = _team_by_abbreviation(abbreviation)
            if team is None or not 0 <= predicted_wins <= 17:
                await event.answer("Invalid prediction.", alert=True)
                return
            try:
                market = next(
                    row
                    for row in _current_win_totals(totals)
                    if str(row["team"]) == team
                )
                prior = next(
                    row
                    for row in history
                    if str(row["team"]) == team
                    and int(row["season"]) == int(market["season"]) - 1
                )
            except (StopIteration, ValueError):
                await event.answer(
                    "Team data is unavailable.", alert=True
                )
                return
            sender = await event.get_sender()
            prediction_row = build_win_prediction_row(
                submitted_at=datetime.now(timezone.utc),
                user_id=event.sender_id,
                username=getattr(sender, "username", None),
                first_name=getattr(sender, "first_name", None),
                last_name=getattr(sender, "last_name", None),
                team=team,
                predicted_wins=predicted_wins,
                market=market,
                prior=prior,
            )
            appended = await asyncio.to_thread(
                append_win_prediction, prediction_row
            )
            if appended:
                predictions.append(prediction_row)
            latest, _ = latest_predictions_for_user(
                predictions, event.sender_id
            )
            unmarked = sorted(
                (
                    candidate
                    for candidate in TEAM_ABBREVIATIONS
                    if candidate not in latest
                ),
                key=lambda candidate: TEAM_ABBREVIATIONS[candidate],
            )
            status = (
                f"✅ {html.escape(team)} saved at {predicted_wins} wins."
                if appended
                else (
                    f"✅ {html.escape(team)} was already saved at "
                    f"{predicted_wins} wins."
                )
            )
            text = f"{status}\n\nProgress: {len(latest)}/32 teams"
            buttons = []
            if unmarked:
                next_team = unmarked[0]
                next_abbreviation = TEAM_ABBREVIATIONS[next_team]
                text += (
                    f"\nNext unmarked team: {html.escape(next_team)} "
                    f"({next_abbreviation})"
                )
                buttons.append(
                    [
                        Button.inline(
                            f"Next: {next_abbreviation}",
                            f"winteam:{next_abbreviation}".encode(),
                        ),
                        Button.inline("All teams", b"wins:teams"),
                    ]
                )
            else:
                text += "\nAll 32 teams are complete."
                buttons.append(
                    [Button.inline("Review teams", b"wins:teams")]
                )
            await edit_callback(event, text, buttons)
            return
        records, team_emojis, team_abbrevs = await _intake_data()
        if data.startswith("games:"):
            guess_states.pop(event.sender_id, None)
            _, days_raw, page_raw = data.split(":", 2)
            text, buttons = game_browser(
                records,
                days=int(days_raw),
                page=int(page_raw),
                now=datetime.now(timezone.utc),
                team_abbrevs=team_abbrevs,
            )
            await edit_callback(event, text, buttons)
            return
        if data.startswith("game:"):
            _, days_raw, page_raw, event_id = data.split(":", 3)
            game = next(
                (
                    record
                    for record in records
                    if str(record.get("event_id")) == event_id
                ),
                None,
            )
            if game is None:
                await event.answer("Game no longer available.", alert=True)
                return
            guess_states[event.sender_id] = {
                "game": game,
                "days": int(days_raw),
                "page": int(page_raw),
            }
            text, buttons = game_detail(
                game,
                days=int(days_raw),
                page=int(page_raw),
                team_emojis=team_emojis,
            )
            await edit_callback(event, text, buttons)
            return
        if data == "back:game":
            state = guess_states.get(event.sender_id)
            if state is None:
                await event.answer(
                    "This guess expired. Choose the game again.", alert=True
                )
                return
            state.pop("period", None)
            state.pop("market", None)
            state.pop("side", None)
            state.pop("prompt_msg_id", None)
            text, buttons = game_detail(
                state["game"],
                days=state["days"],
                page=state["page"],
                team_emojis=team_emojis,
            )
            await edit_callback(event, text, buttons)
            return
        if data == "back:markets":
            state = guess_states.get(event.sender_id)
            if state is None or "period" not in state:
                await event.answer(
                    "This guess expired. Choose the game again.", alert=True
                )
                return
            state.pop("market", None)
            state.pop("side", None)
            state.pop("prompt_msg_id", None)
            text = period_market_summary(
                state["game"],
                period=state["period"],
                team_emojis=team_emojis,
            )
            await edit_callback(event, text, market_buttons())
            return
        if data == "back:sides":
            state = guess_states.get(event.sender_id)
            if state is None or "market" not in state:
                await event.answer(
                    "This guess expired. Choose the game again.", alert=True
                )
                return
            prompt_msg_id = state.pop("prompt_msg_id", None)
            if prompt_msg_id is not None:
                await client.delete_messages(event.chat_id, [prompt_msg_id])
            state.pop("side", None)
            game = state["game"]
            text = market_side_summary(
                game,
                period=state["period"],
                market=state["market"],
                team_emojis=team_emojis,
            )
            await edit_callback(
                event,
                text,
                side_buttons(
                    state["market"],
                    str(game["away_team"]),
                    str(game["home_team"]),
                ),
            )
            return
        if data.startswith("period:"):
            state = guess_states.get(event.sender_id)
            period = data.split(":", 1)[1]
            if state is None or period not in PERIOD_LABELS:
                await event.answer(
                    "This guess expired. Choose the game again.", alert=True
                )
                return
            state["period"] = period
            text = period_market_summary(
                state["game"],
                period=period,
                team_emojis=team_emojis,
            )
            await edit_callback(event, text, market_buttons())
            return
        if data.startswith("market:"):
            state = guess_states.get(event.sender_id)
            market = data.split(":", 1)[1]
            if (
                state is None
                or "period" not in state
                or market not in {"spread", "moneyline", "total"}
            ):
                await event.answer(
                    "This guess expired. Choose the game again.", alert=True
                )
                return
            state["market"] = market
            game = state["game"]
            text = market_side_summary(
                game,
                period=state["period"],
                market=market,
                team_emojis=team_emojis,
            )
            await edit_callback(
                event,
                text,
                side_buttons(
                    market,
                    str(game["away_team"]),
                    str(game["home_team"]),
                ),
            )
            return
        if data.startswith("side:"):
            state = guess_states.get(event.sender_id)
            side = data.split(":", 1)[1]
            if state is None or "market" not in state:
                await event.answer(
                    "This guess expired. Choose the game again.", alert=True
                )
                return
            market = state["market"]
            valid_sides = (
                {"over", "under"} if market == "total" else {"away", "home"}
            )
            if side not in valid_sides:
                await event.answer("Invalid side.", alert=True)
                return
            state["side"] = side
            game = state["game"]
            side_label = selection_side_label(game, market, side)
            context = selected_market_context(
                game,
                period=state["period"],
                market=market,
                side=side,
            )
            opening_text = selection_price_text(
                market,
                side_label,
                context["opening_line"],
                context["opening_price"],
            )
            latest_text = selection_price_text(
                market,
                side_label,
                context["latest_line"],
                context["latest_price"],
            )
            selection = (
                "<blockquote>"
                f"<b>{PERIOD_LABELS[state['period']]} · "
                f"{market.title()} · {html.escape(side_label)}</b>\n"
                f"Opening: {html.escape(opening_text)}\n"
                f"Latest: {html.escape(latest_text)}"
                "</blockquote>"
            )
            await edit_callback(
                event,
                selection,
                [[Button.inline("← Back to sides", b"back:sides")]],
            )
            prompt = await event.respond(
                "Enter your lean, reasoning, and the line or price where your "
                "preference changes:",
                buttons=Button.force_reply(
                    single_use=True,
                    placeholder="Type your NFL lean",
                ),
            )
            state["prompt_msg_id"] = prompt.id

    await client.start(bot_token=token)
    await client(
        functions.bots.SetBotCommandsRequest(
            scope=types.BotCommandScopeDefault(),
            lang_code="",
            commands=[
                types.BotCommand(
                    command="guess_nfl_game",
                    description="Browse NFL games and submit a guess",
                ),
                types.BotCommand(
                    command="predict_nfl_wins",
                    description="Predict every NFL team's season wins",
                ),
                types.BotCommand(
                    command="suggest",
                    description="Suggest an improvement",
                ),
            ],
        )
    )
    identity = await client.get_me()
    print(f"Intake bot running as @{identity.username}")
    await client.run_until_disconnected()


if __name__ == "__main__":
    asyncio.run(main())
