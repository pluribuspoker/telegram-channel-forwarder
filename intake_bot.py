"""Dedicated native Telegram interface for NFL lean intake."""

from __future__ import annotations

import asyncio
import hashlib
import html
import logging
import os
import re
import threading
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from dotenv import load_dotenv
from gspread.exceptions import WorksheetNotFound
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

log = logging.getLogger(__name__)

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
_CELEBRITY_REGISTRY_LOCK = threading.Lock()
CELEBRITY_TAB = "celebrity_picks"
# One row per (submission, celebrity name). Game context is denormalized onto
# each row so season-long "who thinks alike on which game types" analysis pivots
# without joining back to nfl_leans.
CELEBRITY_HEADERS = [
    "submission_id",
    "submitted_at_utc",
    "submitted_at_et",
    "telegram_user_id",
    "telegram_username",
    "event_id",
    "season",
    "week",
    "commence_time_et",
    "away_team",
    "home_team",
    "period",
    "market",
    "side",
    "celebrity_name",
]
# Toggle keyboards get unwieldy past a couple dozen buttons; ➕ New name always
# stays reachable, so this only caps the prefilled roster shown at once.
MAX_CELEBRITY_BUTTONS = 30
MAX_CELEBRITY_NAME_LEN = 60
MAX_CELEBRITY_NAMES = 10

CELEBRITY_REGISTRY_TAB = "celebrities"
# Single source of truth for every celebrity we track, shared by all
# celebrity-aware features. `celebrity_id` is a STABLE, NEGATIVE synthetic
# Telegram-style id derived from the normalized name: negative so it can never
# collide with a real (positive) Telegram user id, which is how downstream code
# tells a celebrity's rows apart from a real user's. Storing the id here lets a
# celebrity's win-total guesses live in nfl_win_predictions under that id using
# the EXISTING columns (no schema change there). Provenance — who first added a
# celebrity — lives on the registry row, not on every guess.
CELEBRITY_REGISTRY_HEADERS = [
    "celebrity_id",
    "celebrity_name",
    "normalized_name",
    "created_at_utc",
    "created_at_et",
    "created_by_user_id",
    "created_by_username",
]
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
    celebrity_name: str | None = None,
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
    context = (
        f"\n🎤 <b>{html.escape(celebrity_name)}</b>"
        if celebrity_name
        else ""
    )
    text = (
        f"🏈 <b>{season} NFL Win Predictions</b>{context}\n"
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
    if celebrity_name:
        buttons.append(
            [Button.inline("↩ Back to my guesses", b"celebwin:self")]
        )
    else:
        buttons.append(
            [Button.inline("🎤 Guess as a celebrity", b"celebwin:start")]
        )
    return text, buttons


def win_celebrity_picker(
    roster: list[str],
) -> tuple[str, list[list[Button]]]:
    text = (
        "🎤 <b>Guess as a celebrity</b>\n\n"
        "Choose someone below or add a new celebrity."
    )
    buttons: list[list[Button]] = []
    row: list[Button] = []
    for name in roster:
        row.append(
            Button.inline(
                name,
                f"celebwin:pick:{celebrity_user_id(name)}".encode(),
            )
        )
        if len(row) == 2:
            buttons.append(row)
            row = []
    if row:
        buttons.append(row)
    buttons.append([Button.inline("➕ New celebrity", b"celebwin:new")])
    buttons.append([Button.inline("← Cancel", b"celebwin:cancel")])
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
    celebrity_name: str | None = None,
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
    if celebrity_name:
        sections.insert(1, f"🎤 <b>{html.escape(celebrity_name)}</b>")
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
    user_id: int,
    celebrity_name: str | None = None,
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
    context = (
        f"🎤 <b>{html.escape(celebrity_name)}</b>\n\n"
        if celebrity_name
        else ""
    )
    text = context + (
        f"Confirm <b>{html.escape(team)}</b>: {predicted_wins} wins?\n\n"
        f"BetOnline: {_number_text(total)}\n"
        f"Your prediction: {predicted_wins}\n"
        f"Difference: {difference:+g} wins"
    )
    return text, [
        [
            Button.inline(
                "Save prediction",
                f"winsave:{abbreviation}:{predicted_wins}:{user_id}".encode(),
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
    celebrity_id: int | None = None,
    celebrity_name: str | None = None,
) -> dict[str, Any]:
    submitted_at_utc = submitted_at.astimezone(timezone.utc)
    display_name = " ".join(
        value for value in (first_name, last_name) if value
    )
    if celebrity_id is not None:
        if not celebrity_name:
            raise ValueError("celebrity_name is required with celebrity_id")
        user_id = celebrity_id
        username = ""
        display_name = celebrity_name
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


def normalize_celebrity_name(raw: str) -> str:
    """Collapse whitespace and cap length so the same person is stored one way."""
    return " ".join(str(raw).split())[:MAX_CELEBRITY_NAME_LEN].strip()


def parse_celebrity_names(raw: str) -> list[str]:
    """Split a free-text reply on commas/newlines into clean, de-duplicated
    display names (case-insensitive dedupe, order preserved, capped)."""
    names: list[str] = []
    seen: set[str] = set()
    for part in re.split(r"[,\n]", str(raw)):
        name = normalize_celebrity_name(part)
        if not name:
            continue
        key = name.casefold()
        if key in seen:
            continue
        seen.add(key)
        names.append(name)
        if len(names) >= MAX_CELEBRITY_NAMES:
            break
    return names


def build_celebrity_rows(
    *, submission: dict[str, Any], names: list[str]
) -> list[dict[str, Any]]:
    """One denormalized row per selected celebrity, sharing the lean's
    submission context."""
    return [{**submission, "celebrity_name": name} for name in names]


def celebrity_screen(
    *, saved_summary: str, roster: list[str], selected: list[str]
) -> tuple[str, list[list[Button]]]:
    """Render the toggle-and-confirm celebrity picker. Existing names are
    tappable buttons (✅ when selected); ➕ New name adds one; Save finalizes
    with whatever is selected (zero = no celebrity info)."""
    text = (
        f"{saved_summary}\n\n"
        "🎤 <b>Whose read does this reflect?</b>\n"
        "Tap anyone below, ➕ add new names, then Save — or Save with none."
    )
    rows: list[list[Button]] = []
    pair: list[Button] = []
    for index, name in enumerate(roster):
        label = ("✅ " if name in selected else "") + name
        pair.append(Button.inline(label, f"celeb:tog:{index}".encode()))
        if len(pair) == 2:
            rows.append(pair)
            pair = []
    if pair:
        rows.append(pair)
    rows.append([Button.inline("➕ New name", b"celeb:new")])
    count = len(selected)
    save_label = (
        f"✅ Save ({count} selected)" if count else "Save — no celebrity info"
    )
    rows.append([Button.inline(save_label, b"celeb:save")])
    return text, rows


def _celebrity_worksheet(spreadsheet: Any) -> Any:
    """Return the celebrity_picks worksheet, creating it with headers the first
    time. Refuses to touch a tab whose existing header row disagrees."""
    try:
        worksheet = spreadsheet.worksheet(CELEBRITY_TAB)
    except WorksheetNotFound:
        worksheet = spreadsheet.add_worksheet(
            title=CELEBRITY_TAB, rows=1000, cols=len(CELEBRITY_HEADERS)
        )
        worksheet.update([CELEBRITY_HEADERS])
        return worksheet
    header = worksheet.row_values(1)
    if not header:
        worksheet.update([CELEBRITY_HEADERS])
    elif header != CELEBRITY_HEADERS:
        raise RuntimeError(
            "celebrity_picks headers do not match the finalized schema"
        )
    return worksheet


def load_celebrity_roster(limit: int = MAX_CELEBRITY_BUTTONS) -> list[str]:
    """Distinct celebrity names from the shared `celebrities` registry (the
    single source of truth for every feature), most-recently-added first, for
    prefilling any celebrity picker. Empty until someone is entered."""
    credentials = os.environ["GOOGLE_CREDENTIALS"]
    sheet_id = os.environ["NFL_INTAKE_SHEET_ID"]
    spreadsheet = get_gspread_client(credentials).open_by_key(sheet_id)
    worksheet = _celebrity_registry_worksheet(spreadsheet)
    values = worksheet.get_all_values()
    if len(values) < 2 or "celebrity_name" not in values[0]:
        return []
    name_index = values[0].index("celebrity_name")
    names: list[str] = []
    seen: set[str] = set()
    for row in reversed(values[1:]):
        if len(row) <= name_index:
            continue
        name = str(row[name_index]).strip()
        if not name:
            continue
        key = name.casefold()
        if key in seen:
            continue
        seen.add(key)
        names.append(name)
        if len(names) >= limit:
            break
    return names


def append_celebrity_picks(rows: list[dict[str, Any]]) -> int:
    """Append celebrity rows, skipping any (submission_id, celebrity_name) pair
    that already exists so a double-tap can't duplicate. Returns rows written."""
    if not rows:
        return 0
    credentials = os.environ["GOOGLE_CREDENTIALS"]
    sheet_id = os.environ["NFL_INTAKE_SHEET_ID"]
    spreadsheet = get_gspread_client(credentials).open_by_key(sheet_id)
    worksheet = _celebrity_worksheet(spreadsheet)
    existing: set[tuple[str, str]] = set()
    values = worksheet.get_all_values()
    if len(values) > 1:
        header_row = values[0]
        sub_index = header_row.index("submission_id")
        name_index = header_row.index("celebrity_name")
        for row in values[1:]:
            if len(row) > max(sub_index, name_index):
                existing.add((row[sub_index], row[name_index]))
    to_append: list[list[Any]] = []
    for row in rows:
        key = (str(row["submission_id"]), str(row["celebrity_name"]))
        if key in existing:
            continue
        existing.add(key)
        to_append.append([row.get(header, "") for header in CELEBRITY_HEADERS])
    if to_append:
        worksheet.append_rows(to_append, value_input_option="RAW")
    return len(to_append)


def celebrity_user_id(name: str) -> int:
    """Stable NEGATIVE synthetic id for a celebrity, derived from the
    case-folded normalized name. Deterministic (same name -> same id across
    runs) and negative so it can never collide with a real, positive Telegram
    user id."""
    key = normalize_celebrity_name(name).casefold()
    digest = hashlib.sha1(key.encode("utf-8")).digest()
    return -(int.from_bytes(digest[:6], "big") + 1)


def _seed_registry_from_game_picks(spreadsheet: Any, registry: Any) -> None:
    """One-time backfill run when the registry tab is first created: pull the
    distinct celebrity names already used on game picks into the registry so the
    shared roster is not empty on day one."""
    try:
        picks = spreadsheet.worksheet(CELEBRITY_TAB)
    except WorksheetNotFound:
        return
    values = picks.get_all_values()
    if len(values) < 2 or "celebrity_name" not in values[0]:
        return
    idx = values[0].index("celebrity_name")
    now = datetime.now(timezone.utc)
    seen: set[str] = set()
    rows: list[list[Any]] = []
    for row in values[1:]:
        if len(row) <= idx:
            continue
        name = normalize_celebrity_name(row[idx])
        if not name:
            continue
        key = name.casefold()
        if key in seen:
            continue
        seen.add(key)
        rows.append(
            [
                celebrity_user_id(name),
                name,
                key,
                now.isoformat(),
                now.astimezone(ET).isoformat(),
                "",
                "",
            ]
        )
    if rows:
        registry.append_rows(rows, value_input_option="RAW")


def _celebrity_registry_worksheet(spreadsheet: Any) -> Any:
    """Return the celebrities registry worksheet, creating it (with headers, and
    seeded once from any names already used on game picks) the first time.
    Refuses to touch a tab whose existing header row disagrees."""
    try:
        worksheet = spreadsheet.worksheet(CELEBRITY_REGISTRY_TAB)
    except WorksheetNotFound:
        worksheet = spreadsheet.add_worksheet(
            title=CELEBRITY_REGISTRY_TAB,
            rows=1000,
            cols=len(CELEBRITY_REGISTRY_HEADERS),
        )
        worksheet.update([CELEBRITY_REGISTRY_HEADERS])
        _seed_registry_from_game_picks(spreadsheet, worksheet)
        return worksheet
    header = worksheet.row_values(1)
    if not header:
        worksheet.update([CELEBRITY_REGISTRY_HEADERS])
    elif header != CELEBRITY_REGISTRY_HEADERS:
        raise RuntimeError(
            "celebrities registry headers do not match the finalized schema"
        )
    return worksheet


def get_or_create_celebrity(
    name: str,
    *,
    created_by_user_id: int | None = None,
    created_by_username: str | None = None,
) -> dict[str, Any]:
    """Resolve a free-text celebrity name to a registry record, creating it if
    new. Idempotent on the case-folded normalized name, so the same person is
    one row (and one id) no matter how many features or users enter them.
    Returns ``{"celebrity_id", "celebrity_name"}``."""
    normalized = normalize_celebrity_name(name)
    if not normalized:
        raise ValueError("celebrity name cannot be empty")
    key = normalized.casefold()
    celeb_id = celebrity_user_id(normalized)
    credentials = os.environ["GOOGLE_CREDENTIALS"]
    sheet_id = os.environ["NFL_INTAKE_SHEET_ID"]
    with _CELEBRITY_REGISTRY_LOCK:
        spreadsheet = get_gspread_client(credentials).open_by_key(sheet_id)
        worksheet = _celebrity_registry_worksheet(spreadsheet)
        norm_index = CELEBRITY_REGISTRY_HEADERS.index("normalized_name")
        name_index = CELEBRITY_REGISTRY_HEADERS.index("celebrity_name")
        for row in worksheet.get_all_values()[1:]:
            if len(row) > norm_index and row[norm_index] == key:
                existing = (
                    row[name_index] if len(row) > name_index else normalized
                )
                return {
                    "celebrity_id": celeb_id,
                    "celebrity_name": existing or normalized,
                }
        now = datetime.now(timezone.utc)
        worksheet.append_row(
            [
                celeb_id,
                normalized,
                key,
                now.isoformat(),
                now.astimezone(ET).isoformat(),
                created_by_user_id if created_by_user_id is not None else "",
                created_by_username or "",
            ],
            value_input_option="RAW",
        )
    return {"celebrity_id": celeb_id, "celebrity_name": normalized}


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
    win_guess_states: dict[int, dict[str, Any]] = {}

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
            win_guess_states.pop(event.sender_id, None)
            totals, _, predictions = await _win_prediction_data()
            text, buttons = win_prediction_browser(
                totals,
                predictions,
                user_id=event.sender_id,
            )
            await event.respond(text, buttons=buttons, parse_mode="html")
            return
        guess_states.pop(event.sender_id, None)
        win_guess_states.pop(event.sender_id, None)
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

        win_state = win_guess_states.get(event.sender_id)
        if (
            win_state is not None
            and win_state.get("win_celeb_prompt_msg_id") is not None
            and win_state.get("win_celeb_prompt_msg_id")
            == event.reply_to_msg_id
        ):
            name = normalize_celebrity_name(event.raw_text)
            if not name:
                await event.respond(
                    "Celebrity name cannot be empty. Reply with a name."
                )
                return
            sender = await event.get_sender()
            celebrity = await asyncio.to_thread(
                get_or_create_celebrity,
                name,
                created_by_user_id=event.sender_id,
                created_by_username=getattr(sender, "username", None),
            )
            win_state.pop("win_celeb_prompt_msg_id", None)
            win_state.pop("win_celeb_roster", None)
            win_state["win_celeb"] = {
                "id": celebrity["celebrity_id"],
                "name": celebrity["celebrity_name"],
            }
            totals, _, predictions = await _win_prediction_data()
            text, buttons = win_prediction_browser(
                totals,
                predictions,
                user_id=celebrity["celebrity_id"],
                celebrity_name=celebrity["celebrity_name"],
            )
            await event.respond(text, buttons=buttons, parse_mode="html")
            return

        state = guess_states.get(event.sender_id)
        # Reply with celebrity name(s) after tapping ➕ New name.
        if (
            state is not None
            and state.get("celeb_prompt_msg_id") is not None
            and state.get("celeb_prompt_msg_id") == event.reply_to_msg_id
        ):
            additions = parse_celebrity_names(event.raw_text)
            if not additions:
                await event.respond(
                    "No valid names found. Reply with one or more names "
                    "separated by commas."
                )
                return
            roster = state["celeb_roster"]
            selected = state["celeb_selected"]
            by_key = {name.casefold(): name for name in roster}
            for name in additions:
                canonical = by_key.get(name.casefold())
                if canonical is None:
                    roster.append(name)
                    by_key[name.casefold()] = name
                    canonical = name
                if canonical not in selected:
                    selected.append(canonical)
            state["celeb_prompt_msg_id"] = None
            text, buttons = celebrity_screen(
                saved_summary=state["celeb_summary"],
                roster=roster,
                selected=selected,
            )
            try:
                await client.edit_message(
                    event.chat_id,
                    state["celeb_msg_id"],
                    text,
                    buttons=buttons,
                    parse_mode="html",
                )
            except MessageNotModifiedError:
                pass
            return

        # Reply with the free-text lean itself (guarded against stale state).
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
        summary = (
            f"{PERIOD_LABELS[submission['period']]} · "
            f"{submission['market'].title()} · "
            f"{html.escape(str(row['side']))}"
        )
        status = "✅ Guess saved." if appended else "✅ Guess was already saved."
        saved_summary = f"{status}\n{summary}"

        # If the user moved on while the lean was being saved, don't overwrite
        # their new selection state with the celebrity step -- just confirm.
        current_state = guess_states.get(event.sender_id)
        if not (
            current_state is state
            and current_state.get("prompt_msg_id") == submission["prompt_msg_id"]
        ):
            await event.respond(
                saved_summary, buttons=command_keyboard(), parse_mode="html"
            )
            return

        # The lean is now persisted; the celebrity step is a purely additive
        # follow-on keyed by the same submission_id, so abandoning it just
        # leaves a lean with no celebrity rows.
        roster = await asyncio.to_thread(load_celebrity_roster)
        state.pop("prompt_msg_id", None)
        state["celeb_roster"] = roster
        state["celeb_selected"] = []
        state["celeb_summary"] = saved_summary
        state["celeb_submission"] = {
            "submission_id": row["submission_id"],
            "submitted_at_utc": row["submitted_at_utc"],
            "submitted_at_et": row["submitted_at_et"],
            "telegram_user_id": event.sender_id,
            "telegram_username": getattr(sender, "username", None) or "",
            "event_id": row["event_id"],
            "season": row["season"],
            "week": row["week"],
            "commence_time_et": row["commence_time_et"],
            "away_team": row["away_team"],
            "home_team": row["home_team"],
            "period": row["period"],
            "market": row["market"],
            "side": row["side"],
        }
        text, buttons = celebrity_screen(
            saved_summary=saved_summary, roster=roster, selected=[]
        )
        screen = await event.respond(text, buttons=buttons, parse_mode="html")
        state["celeb_msg_id"] = screen.id

    @client.on(events.CallbackQuery)
    async def handle_callback(event):
        if event.sender_id not in allowed:
            await event.answer("Not authorized.", alert=True)
            return
        data = event.data.decode()
        if data.startswith("celebwin:"):
            state = win_guess_states.setdefault(event.sender_id, {})
            if data == "celebwin:start":
                roster = await asyncio.to_thread(load_celebrity_roster)
                state["win_celeb_roster"] = {
                    str(celebrity_user_id(name)): name for name in roster
                }
                text, buttons = win_celebrity_picker(roster)
                await edit_callback(event, text, buttons)
                return
            if data.startswith("celebwin:pick:"):
                roster = state.get("win_celeb_roster")
                celebrity_id = data.split(":", 2)[2]
                if (
                    not isinstance(roster, dict)
                    or celebrity_id not in roster
                ):
                    await event.answer(
                        "This list expired. Choose celebrity mode again.",
                        alert=True,
                    )
                    return
                sender = await event.get_sender()
                celebrity = await asyncio.to_thread(
                    get_or_create_celebrity,
                    roster[celebrity_id],
                    created_by_user_id=event.sender_id,
                    created_by_username=getattr(sender, "username", None),
                )
                state["win_celeb"] = {
                    "id": celebrity["celebrity_id"],
                    "name": celebrity["celebrity_name"],
                }
                state.pop("win_celeb_roster", None)
                totals, _, predictions = await _win_prediction_data()
                text, buttons = win_prediction_browser(
                    totals,
                    predictions,
                    user_id=celebrity["celebrity_id"],
                    celebrity_name=celebrity["celebrity_name"],
                )
                await edit_callback(event, text, buttons)
                return
            if data == "celebwin:new":
                prompt = await event.respond(
                    "Type the celebrity name:",
                    buttons=Button.force_reply(
                        single_use=True,
                        placeholder="e.g. LeBron James",
                    ),
                )
                state["win_celeb_prompt_msg_id"] = prompt.id
                await event.answer()
                return
            if data == "celebwin:self":
                state.pop("win_celeb", None)
            elif data != "celebwin:cancel":
                await event.answer("Invalid selection.", alert=True)
                return
            state.pop("win_celeb_roster", None)
            state.pop("win_celeb_prompt_msg_id", None)
            active_celebrity = state.get("win_celeb")
            effective_user_id = (
                int(active_celebrity["id"])
                if active_celebrity
                else event.sender_id
            )
            celebrity_name = (
                str(active_celebrity["name"])
                if active_celebrity
                else None
            )
            totals, _, predictions = await _win_prediction_data()
            text, buttons = win_prediction_browser(
                totals,
                predictions,
                user_id=effective_user_id,
                celebrity_name=celebrity_name,
            )
            await edit_callback(event, text, buttons)
            return
        if (
            data == "wins:teams"
            or data.startswith("winteam:")
            or data.startswith("winpick:")
            or data.startswith("winsave:")
        ):
            state = win_guess_states.get(event.sender_id, {})
            active_celebrity = state.get("win_celeb")
            effective_user_id = (
                int(active_celebrity["id"])
                if active_celebrity
                else event.sender_id
            )
            celebrity_name = (
                str(active_celebrity["name"])
                if active_celebrity
                else None
            )
            totals, history, predictions = await _win_prediction_data()
            if data == "wins:teams":
                text, buttons = win_prediction_browser(
                    totals,
                    predictions,
                    user_id=effective_user_id,
                    celebrity_name=celebrity_name,
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
                        user_id=effective_user_id,
                        abbreviation=abbreviation,
                        celebrity_name=celebrity_name,
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
                try:
                    predicted_wins = int(wins_raw)
                except ValueError:
                    await event.answer("Invalid prediction.", alert=True)
                    return
                if not 0 <= predicted_wins <= 17:
                    await event.answer("Invalid prediction.", alert=True)
                    return
                try:
                    text, buttons = win_prediction_confirmation(
                        totals,
                        abbreviation=abbreviation,
                        predicted_wins=predicted_wins,
                        user_id=effective_user_id,
                        celebrity_name=celebrity_name,
                    )
                except (StopIteration, ValueError):
                    await event.answer(
                        "Team data is unavailable.", alert=True
                    )
                    return
                await edit_callback(event, text, buttons)
                return
            parts = data.split(":")
            if len(parts) != 4:
                await event.answer(
                    "This confirmation expired. Choose the team again.",
                    alert=True,
                )
                return
            _, abbreviation, wins_raw, identity_raw = parts
            try:
                predicted_wins = int(wins_raw)
                confirmation_user_id = int(identity_raw)
            except ValueError:
                await event.answer("Invalid prediction.", alert=True)
                return
            if confirmation_user_id != effective_user_id:
                await event.answer(
                    "The active guesser changed. Choose the team again.",
                    alert=True,
                )
                return
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
                celebrity_id=effective_user_id if active_celebrity else None,
                celebrity_name=celebrity_name,
            )
            appended = await asyncio.to_thread(
                append_win_prediction, prediction_row
            )
            if appended:
                predictions.append(prediction_row)
            latest, _ = latest_predictions_for_user(
                predictions, effective_user_id
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
            context = (
                f"\n🎤 <b>{html.escape(celebrity_name)}</b>"
                if celebrity_name
                else ""
            )
            text = f"{status}{context}\n\nProgress: {len(latest)}/32 teams"
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
            win_guess_states.pop(event.sender_id, None)
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
            win_guess_states.pop(event.sender_id, None)
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
        if data.startswith("celeb:tog:"):
            state = guess_states.get(event.sender_id)
            if state is None or "celeb_roster" not in state:
                await event.answer(
                    "This guess expired. Choose the game again.", alert=True
                )
                return
            try:
                index = int(data.split(":", 2)[2])
            except ValueError:
                await event.answer("Invalid selection.", alert=True)
                return
            roster = state["celeb_roster"]
            selected = state["celeb_selected"]
            if 0 <= index < len(roster):
                name = roster[index]
                if name in selected:
                    selected.remove(name)
                else:
                    selected.append(name)
            text, buttons = celebrity_screen(
                saved_summary=state["celeb_summary"],
                roster=roster,
                selected=selected,
            )
            await edit_callback(event, text, buttons)
            return
        if data == "celeb:new":
            state = guess_states.get(event.sender_id)
            if state is None or "celeb_roster" not in state:
                await event.answer(
                    "This guess expired. Choose the game again.", alert=True
                )
                return
            prompt = await event.respond(
                "Type the celebrity name(s), separated by commas:",
                buttons=Button.force_reply(
                    single_use=True,
                    placeholder="e.g. LeBron, Drake",
                ),
            )
            state["celeb_prompt_msg_id"] = prompt.id
            await event.answer()
            return
        if data == "celeb:save":
            state = guess_states.get(event.sender_id)
            if state is None or "celeb_roster" not in state:
                await event.answer(
                    "This guess expired. Choose the game again.", alert=True
                )
                return
            names = list(state.get("celeb_selected", []))
            if names:
                rows = build_celebrity_rows(
                    submission=state["celeb_submission"], names=names
                )
                await asyncio.to_thread(append_celebrity_picks, rows)
                # Keep the shared registry complete: any name entered here must
                # also become a first-class celebrity so it shows up as a roster
                # button in every feature (single source of truth).
                sender = await event.get_sender()
                for name in names:
                    await asyncio.to_thread(
                        get_or_create_celebrity,
                        name,
                        created_by_user_id=event.sender_id,
                        created_by_username=getattr(sender, "username", None),
                    )
            guess_states.pop(event.sender_id, None)
            if names:
                receipt = (
                    f"{state['celeb_summary']}\n\n"
                    f"🎤 Celebrity reads: {html.escape(', '.join(names))}"
                )
            else:
                receipt = (
                    f"{state['celeb_summary']}\n\n🎤 No celebrity info added."
                )
            await edit_callback(event, receipt, None)
            await event.respond(
                "Tap /guess_nfl_game for another.",
                buttons=command_keyboard(),
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
