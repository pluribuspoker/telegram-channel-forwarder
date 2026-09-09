"""Dedicated native Telegram interface for NFL lean intake."""

from __future__ import annotations

import asyncio
import hashlib
import html
import logging
import os
import re
import threading
import time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import anthropic
from dotenv import load_dotenv
from gspread.exceptions import APIError, WorksheetNotFound
from telethon import Button, TelegramClient, events
from telethon.errors import MessageNotModifiedError
from telethon.sessions import StringSession
from telethon.tl import functions, types
from telethon.tl.types import (
    KeyboardButton,
    KeyboardButtonRow,
    ReplyKeyboardMarkup,
)

from ai import claude_parse
from celebrity_picks import (
    CELEBRITY_HEADERS,
    CELEBRITY_TAB,
    CUSTOM_MARKET_FAMILIES,
    LEGACY_CELEBRITY_HEADERS,
    build_celebrity_rows,
    canonical_pick_key,
    parse_custom_pick_text,
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
from moe import (
    approved_opinions as approved_moe_opinions,
    configured_opinion_store,
    latest_model_opinions as latest_moe_model_opinions,
    opinion_detail as moe_opinion_detail,
    opinion_model_picker as moe_opinion_model_picker,
    opinion_output_sha256,
    opinion_summary as moe_opinion_summary,
)
from moe_ak import parse_ak_projection
from moe_desk import (
    BotApi as DeskBotApi,
    build_desks as build_desk_model,
    content_hash as desk_content_hash,
    desk_config_from_env,
    load_state as load_desk_state,
    parse_callback as parse_desk_callback,
    desk_ids_report,
    parse_start_param,
    resolve_picks_view,
    review_targets as desk_review_targets,
    render_picks_card,
    topic_id_from_reply,
    save_state as save_desk_state,
    sync_desk,
)
from moe_god import load_registry as load_moe_registry
from moe_identity import (
    REVIEWER_ROLE,
    resolve_moe_expert_user_id_from_spreadsheet,
    resolve_role_user_ids_from_spreadsheet,
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
CELEBRITY_PERIOD_LABELS = {
    **PERIOD_LABELS,
    "second_half": "Second half",
    "second_quarter": "Second quarter",
    "third_quarter": "Third quarter",
    "fourth_quarter": "Fourth quarter",
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
_SHEET_CACHE_LOCK = threading.RLock()
_SHEET_CACHE: dict[str, tuple[float, Any]] = {}
_INTAKE_SPREADSHEET: Any | None = None
_MOE_STORE: Any | None = None
GAMES_CACHE_TTL_SECONDS = 3600
TEAM_EMOJI_CACHE_TTL_SECONDS = 600
MOE_CACHE_TTL_SECONDS = 3600
DESK_REVIEWERS_CACHE_TTL_SECONDS = 300
_DESK_SYNC_LOCK = threading.Lock()
WIN_TOTALS_CACHE_TTL_SECONDS = 3600
TEAM_HISTORY_CACHE_TTL_SECONDS = 21600
WIN_PREDICTIONS_CACHE_TTL_SECONDS = 30
CELEBRITY_REGISTRY_CACHE_TTL_SECONDS = 300
# Toggle keyboards get unwieldy past a couple dozen buttons; ➕ New name always
# stays reachable, so this only caps the prefilled roster shown at once.
MAX_CELEBRITY_BUTTONS = 30
MAX_CELEBRITY_NAME_LEN = 60

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
    celebrity_name: str | None = None,
) -> tuple[str, list[list[Button]]]:
    games = select_games(records, days=days, now=now)
    current, page, page_count = page_games(games, page)
    context = (
        f"\n🎤 <b>{html.escape(celebrity_name)}</b>"
        if celebrity_name
        else ""
    )
    text = (
        f"🏈 NFL games in the next {days} days\n"
        f"{len(games)} game{'s' if len(games) != 1 else ''} available"
        f"{context}"
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
    if celebrity_name:
        buttons.append(
            [
                Button.inline(
                    "↩ Back to my guesses",
                    f"celebgame:self:{days}:{page}".encode(),
                )
            ]
        )
    else:
        buttons.append(
            [
                Button.inline(
                    "🎤 Guess as a celebrity",
                    f"celebgame:start:{days}:{page}".encode(),
                )
            ]
        )
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
    celebrity_name: str | None = None,
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
    if celebrity_name:
        sections.insert(1, f"🎤 <b>{html.escape(celebrity_name)}</b>")
    buttons = [
        [
            Button.inline("Full game", b"period:game"),
            Button.inline("First half", b"period:first_half"),
            Button.inline("First quarter", b"period:first_quarter"),
        ],
        [
            Button.inline(
                "🧠 MOE opinions",
                f"moe:view:{game['event_id']}:0".encode(),
            )
        ],
        [Button.inline("← Back to games", f"games:{days}:{page}".encode())]
    ]
    return "\n\n".join(sections), buttons


def market_buttons(*, allow_custom: bool = False) -> list[list[Button]]:
    rows = [
        [
            Button.inline("Spread", b"market:spread"),
            Button.inline("Moneyline", b"market:moneyline"),
            Button.inline("Total", b"market:total"),
        ],
    ]
    if allow_custom:
        rows.append(
            [Button.inline("Prop / other", b"market:custom")]
        )
    rows.append([Button.inline("← Back to periods", b"back:game")])
    return rows


def custom_market_buttons() -> list[list[Button]]:
    return [
        [
            Button.inline("Player prop", b"custom:player_prop"),
            Button.inline("Team prop", b"custom:team_prop"),
        ],
        [Button.inline("Other", b"custom:other")],
        [Button.inline("← Back to markets", b"back:markets")],
    ]


def custom_pick_prompt(market_family: str) -> str:
    label = {
        "player_prop": "player prop",
        "team_prop": "team prop",
        "other": "other market",
    }[market_family]
    return (
        f"Reply with the celebrity's {label} in normal free-form text.\n\n"
        "You can optionally use this structure for an exact manual parse:\n\n"
        "Subject: player, team, or bet subject\n"
        "Market: stat or market name\n"
        "Pick: Over 0.5, Under 250.5, Yes, No, or the exact selection\n"
        "Odds: -110 (optional)\n"
        "Rationale: exact explanation (optional)\n\n"
        "The exact reply is retained alongside the structured fields."
    )


def has_complete_custom_pick_fields(raw_text: str) -> bool:
    fields = {
        match.group(1).casefold()
        for match in re.finditer(
            r"(?im)^(Subject|Market|Pick)\s*:",
            raw_text,
        )
    }
    return {"subject", "market", "pick"} <= fields


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


def ak_projection_example(game: dict[str, Any]) -> str:
    away = str(game["away_team"]).rsplit(" ", 1)[-1]
    home = str(game["home_team"]).rsplit(" ", 1)[-1]
    return f"Score: {away} 23, {home} 27"


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
    celebrity_name: str | None = None,
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
    sections = [
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
    if celebrity_name:
        sections.insert(1, f"🎤 <b>{html.escape(celebrity_name)}</b>")
    return "\n\n".join(sections)


def market_side_summary(
    game: dict[str, Any],
    *,
    period: str,
    market: str,
    team_emojis: dict[str, str] | None = None,
    celebrity_name: str | None = None,
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

    sections = [
            (
                f"🏈 <b>{team_emoji(away, team_emojis)} {html.escape(away)} @ "
                f"{team_emoji(home, team_emojis)} {html.escape(home)}</b>"
            ),
            f"<b>{PERIOD_LABELS[period]} · {market.title()}</b>",
            f"Opening\n{values(opening)}\n\nLatest\n{values(latest)}",
            "Choose a side:",
    ]
    if celebrity_name:
        sections.insert(1, f"🎤 <b>{html.escape(celebrity_name)}</b>")
    return "\n\n".join(sections)


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
    prediction: dict[str, Any] | None = None,
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
        "predicted_away_score": (
            prediction["away_score"] if prediction else ""
        ),
        "predicted_home_score": (
            prediction["home_score"] if prediction else ""
        ),
        "prediction_parse_version": 1 if prediction else "",
        "prediction_parse_status": "parsed" if prediction else "not_applicable",
    }


def build_custom_celebrity_submission(
    *,
    submitted_at: datetime,
    user_id: int,
    username: str | None,
    message_id: int,
    game: dict[str, Any],
    period: str,
    market_family: str,
    raw_text: str,
) -> dict[str, Any]:
    parsed = parse_custom_pick_text(
        raw_text,
        market_family=market_family,
    )
    submitted_at_utc = submitted_at.astimezone(timezone.utc)
    submission_id = f"telegram:{user_id}:{message_id}"
    canonical_key = canonical_pick_key(
        period=period,
        market_family=market_family,
        market=str(parsed["market"]),
        subject=str(parsed["subject"]),
        stat=str(parsed["stat"]),
    )
    return {
        "submission_id": submission_id,
        "submitted_at_utc": submitted_at_utc.isoformat(),
        "submitted_at_et": submitted_at_utc.astimezone(ET).isoformat(),
        "telegram_user_id": user_id,
        "telegram_username": username or "",
        "event_id": game.get("event_id", ""),
        "season": game.get("season", ""),
        "week": game.get("week", ""),
        "commence_time_utc": game.get("commence_time_utc", ""),
        "commence_time_et": game.get("commence_time_et", ""),
        "away_team": game.get("away_team", ""),
        "home_team": game.get("home_team", ""),
        "period": period,
        "market": parsed["market"],
        "side": parsed["side"],
        "pick_id": "",
        "canonical_key": canonical_key,
        "market_family": market_family,
        "subject": parsed["subject"],
        "stat": parsed["stat"],
        "direction": parsed["direction"],
        "line": parsed["line"],
        "price": parsed["price"],
        "selection_text": parsed["selection_text"],
        "raw_pick_text": parsed["raw_pick_text"],
    }


def build_freeform_celebrity_submissions(
    *,
    submitted_at: datetime,
    user_id: int,
    username: str | None,
    message_id: int,
    game: dict[str, Any],
    parsed: dict[str, Any],
    raw_text: str,
) -> list[dict[str, Any]]:
    """Convert one free-form NFL parse into canonical celebrity bet legs."""
    if str(parsed.get("sport") or "").upper() != "NFL":
        raise ValueError("The free-form pick did not parse as an NFL bet")
    parsed_picks = parsed.get("picks")
    if not isinstance(parsed_picks, list) or not parsed_picks:
        raise ValueError("No celebrity bet legs were found in the free-form text")

    period_map = {
        "game": "game",
        "1h": "first_half",
        "2h": "second_half",
        "1q": "first_quarter",
        "2q": "second_quarter",
        "3q": "third_quarter",
        "4q": "fourth_quarter",
    }
    away = str(game["away_team"])
    home = str(game["home_team"])
    teams_by_name = {away.casefold(): away, home.casefold(): home}
    submitted_at_utc = submitted_at.astimezone(timezone.utc)
    base = {
        "submission_id": f"telegram:{user_id}:{message_id}",
        "submitted_at_utc": submitted_at_utc.isoformat(),
        "submitted_at_et": submitted_at_utc.astimezone(ET).isoformat(),
        "telegram_user_id": user_id,
        "telegram_username": username or "",
        "event_id": game.get("event_id", ""),
        "season": game.get("season", ""),
        "week": game.get("week", ""),
        "commence_time_utc": game.get("commence_time_utc", ""),
        "commence_time_et": game.get("commence_time_et", ""),
        "away_team": away,
        "home_team": home,
        "pick_id": "",
        "price": "",
        "raw_pick_text": raw_text,
    }
    submissions = []
    for pick in parsed_picks:
        if not isinstance(pick, dict):
            raise ValueError("A parsed celebrity bet leg is malformed")
        pick_sport = str(pick.get("sport") or parsed["sport"]).upper()
        if pick_sport != "NFL":
            raise ValueError("Every free-form celebrity leg must be an NFL bet")
        period = period_map.get(str(pick.get("period") or "game"))
        if period is None:
            raise ValueError(
                "Free-form celebrity picks support full game, halves, "
                "and quarters only"
            )
        bet_type = str(pick.get("bet_type") or "")
        parsed_team_names = [
            str(name) for name in pick.get("teams") or []
        ]
        unknown_teams = [
            name
            for name in parsed_team_names
            if name.casefold() not in teams_by_name
        ]
        if unknown_teams:
            raise ValueError(
                "A free-form leg names a team outside the selected game: "
                + ", ".join(unknown_teams)
            )
        parsed_teams = [
            teams_by_name[name.casefold()] for name in parsed_team_names
        ]
        line = pick.get("line")
        direction = str(pick.get("direction") or "").title()
        if bet_type in {"spread", "moneyline"}:
            if len(parsed_teams) != 1:
                raise ValueError(
                    f"Could not identify the selected team for {bet_type}"
                )
            selected = parsed_teams[0]
            if bet_type == "spread" and not isinstance(line, (int, float)):
                raise ValueError("A free-form spread leg requires a numeric line")
            market_family = "side"
            subject = "game"
            stat = bet_type
            side = selected
            selection = (
                selected
                if bet_type == "moneyline"
                else f"{selected} {float(line):+g}"
            )
        elif bet_type == "total":
            if direction not in {"Over", "Under"} or not isinstance(
                line, (int, float)
            ):
                raise ValueError(
                    "A free-form total leg requires Over/Under and a numeric line"
                )
            market_family = "total"
            subject = "game"
            stat = "total"
            side = direction
            selection = f"{direction} {float(line):g}"
        elif bet_type == "team_total":
            if (
                len(parsed_teams) != 1
                or direction not in {"Over", "Under"}
                or not isinstance(line, (int, float))
            ):
                raise ValueError(
                    "A free-form team-total leg requires one team, "
                    "Over/Under, and a numeric line"
                )
            market_family = "team_prop"
            subject = parsed_teams[0]
            stat = "team total"
            side = direction
            selection = f"{direction} {float(line):g}"
        elif bet_type == "prop":
            subject = str(pick.get("player") or "")
            stat = str(pick.get("prop_stat") or "")
            if (
                not subject
                or not stat
                or not direction
                or len(parsed_teams) != 1
            ):
                raise ValueError(
                    "A free-form player prop requires player, current team, "
                    "market, and pick"
                )
            market_family = "player_prop"
            side = direction
            selection = (
                f"{direction} {float(line):g}"
                if isinstance(line, (int, float))
                else direction
            )
        else:
            raise ValueError(
                f"Unsupported free-form celebrity market: {bet_type or 'unknown'}"
            )
        canonical_key = canonical_pick_key(
            period=period,
            market_family=market_family,
            market=bet_type,
            subject=subject,
            stat=stat,
        )
        submissions.append(
            {
                **base,
                "period": period,
                "market": bet_type,
                "side": side,
                "canonical_key": canonical_key,
                "market_family": market_family,
                "subject": subject,
                "stat": stat,
                "direction": side,
                "line": "" if line is None else line,
                "selection_text": selection,
            }
        )
    return submissions


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
        {"over", "under"}
        if market == "total"
        else {"custom"}
        if market == "custom"
        else {"away", "home"}
    )
    if (
        not isinstance(game, dict)
        or period not in PERIOD_LABELS
        or market not in {"spread", "moneyline", "total", "custom"}
        or side not in valid_sides
        or (
            market == "custom"
            and state.get("custom_market_family")
            not in CUSTOM_MARKET_FAMILIES
        )
    ):
        return "invalid", None

    return (
        "ready",
        {
            "game": dict(game),
            "period": period,
            "market": market,
            "side": side,
            "custom_market_family": state.get("custom_market_family"),
            "prompt_msg_id": state["prompt_msg_id"],
            "days": int(state.get("days", 10)),
            "page": int(state.get("page", 0)),
            "celebrity": (
                dict(state["celebrity"])
                if isinstance(state.get("celebrity"), dict)
                else None
            ),
        },
    )


def requires_ak_projection(
    sender_id: int,
    ak_user_id: str,
    celebrity: Any,
) -> bool:
    return (
        str(sender_id) == ak_user_id
        and not isinstance(celebrity, dict)
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


def _celebrity_identity_picker(
    roster: list[str],
    *,
    namespace: str,
    callback_suffix: str = "",
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
                (
                    f"{namespace}:pick:{celebrity_user_id(name)}"
                    f"{callback_suffix}"
                ).encode(),
            )
        )
        if len(row) == 2:
            buttons.append(row)
            row = []
    if row:
        buttons.append(row)
    buttons.append(
        [
            Button.inline(
                "➕ New celebrity",
                f"{namespace}:new{callback_suffix}".encode(),
            )
        ]
    )
    buttons.append(
        [
            Button.inline(
                "← Cancel",
                f"{namespace}:cancel{callback_suffix}".encode(),
            )
        ]
    )
    return text, buttons


def win_celebrity_picker(
    roster: list[str],
) -> tuple[str, list[list[Button]]]:
    return _celebrity_identity_picker(roster, namespace="celebwin")


def game_celebrity_picker(
    roster: list[str],
    *,
    days: int,
    page: int,
) -> tuple[str, list[list[Button]]]:
    return _celebrity_identity_picker(
        roster,
        namespace="celebgame",
        callback_suffix=f":{days}:{page}",
    )


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
    spreadsheet = _intake_spreadsheet()
    totals = _cached_sheet_value(
        "nfl_win_totals",
        WIN_TOTALS_CACHE_TTL_SECONDS,
        lambda: spreadsheet.worksheet(WIN_TOTALS_TAB).get_all_records(
            expected_headers=TAB_HEADERS[WIN_TOTALS_TAB]
        ),
    )
    history = _cached_sheet_value(
        "nfl_team_history",
        TEAM_HISTORY_CACHE_TTL_SECONDS,
        lambda: spreadsheet.worksheet(TEAM_HISTORY_TAB).get_all_records(
            expected_headers=TAB_HEADERS[TEAM_HISTORY_TAB]
        ),
    )
    predictions = _cached_sheet_value(
        "nfl_win_predictions",
        WIN_PREDICTIONS_CACHE_TTL_SECONDS,
        lambda: spreadsheet.worksheet(
            WIN_PREDICTIONS_TAB
        ).get_all_records(expected_headers=PREDICTION_HEADERS),
    )
    return totals, history, predictions


def append_win_prediction(row: dict[str, Any]) -> bool:
    with _WIN_PREDICTION_WRITE_LOCK:
        spreadsheet = _intake_spreadsheet()
        predictions_worksheet = spreadsheet.worksheet(WIN_PREDICTIONS_TAB)
        predictions = predictions_worksheet.get_all_records(
            expected_headers=PREDICTION_HEADERS
        )
        _set_sheet_cache("nfl_win_predictions", predictions)
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
        _set_sheet_cache("nfl_win_predictions", predictions)
        totals = _cached_sheet_value(
            "nfl_win_totals",
            WIN_TOTALS_CACHE_TTL_SECONDS,
            lambda: spreadsheet.worksheet(WIN_TOTALS_TAB).get_all_records(
                expected_headers=TAB_HEADERS[WIN_TOTALS_TAB]
            ),
        )
        latest_rows = build_latest_prediction_rows(predictions, totals)
        replace_rows(
            spreadsheet.worksheet(WIN_PREDICTIONS_LATEST_TAB),
            TAB_HEADERS[WIN_PREDICTIONS_LATEST_TAB],
            latest_rows,
        )
        return True


def _cached_sheet_value(key: str, ttl: int, loader) -> Any:
    now = time.monotonic()
    with _SHEET_CACHE_LOCK:
        cached = _SHEET_CACHE.get(key)
        if cached is not None and now - cached[0] < ttl:
            return cached[1]
        try:
            value = loader()
        except APIError as exc:
            status = getattr(exc.response, "status_code", None)
            # Quota (429) and Google-side outages (5xx) both get the last
            # good value for one more TTL; the timestamp reset is the backoff.
            if cached is not None and (
                status == 429 or (isinstance(status, int) and status >= 500)
            ):
                log.warning(
                    "Sheets %s refreshing %s; serving stale cache", status, key
                )
                _SHEET_CACHE[key] = (now, cached[1])
                return cached[1]
            raise
        _SHEET_CACHE[key] = (now, value)
        return value


def _set_sheet_cache(key: str, value: Any) -> None:
    with _SHEET_CACHE_LOCK:
        _SHEET_CACHE[key] = (time.monotonic(), value)


def _patch_sheet_cache(key: str, value: Any) -> None:
    """Replace cached rows without delaying the next full Sheet refresh."""
    with _SHEET_CACHE_LOCK:
        cached = _SHEET_CACHE.get(key)
        timestamp = cached[0] if cached is not None else time.monotonic()
        _SHEET_CACHE[key] = (timestamp, value)


def _intake_spreadsheet() -> Any:
    global _INTAKE_SPREADSHEET
    with _SHEET_CACHE_LOCK:
        if _INTAKE_SPREADSHEET is None:
            _INTAKE_SPREADSHEET = get_gspread_client(
                os.environ["GOOGLE_CREDENTIALS"]
            ).open_by_key(os.environ["NFL_INTAKE_SHEET_ID"])
        return _INTAKE_SPREADSHEET


def _moe_store() -> Any:
    global _MOE_STORE
    with _SHEET_CACHE_LOCK:
        if _MOE_STORE is None:
            _MOE_STORE = configured_opinion_store()
        return _MOE_STORE


def load_cached_moe_opinions(
    event_id: str | None = None,
) -> list[dict[str, Any]]:
    rows = _cached_sheet_value(
        "moe_opinions",
        MOE_CACHE_TTL_SECONDS,
        lambda: _moe_store().list(),
    )
    if event_id is None:
        return rows
    return [
        row
        for row in rows
        if str(row.get("event_id")) == str(event_id)
    ]


def expire_sheet_cache(key: str) -> None:
    """Force the next load while retaining the last good outage fallback."""
    with _SHEET_CACHE_LOCK:
        cached = _SHEET_CACHE.get(key)
        if cached is not None:
            _SHEET_CACHE[key] = (float("-inf"), cached[1])


def load_desk_reviewers() -> dict[int, str]:
    """Telegram id -> display name for every ``reviewer`` row in
    ``allowed_users`` (the desk group's approve/reject buttons and the
    pending-opinion deep links are open to exactly these people)."""
    return _cached_sheet_value(
        "desk_reviewers",
        DESK_REVIEWERS_CACHE_TTL_SECONDS,
        lambda: resolve_role_user_ids_from_spreadsheet(
            _intake_spreadsheet(), REVIEWER_ROLE
        ),
    )


def game_stub(row: dict[str, Any]) -> dict[str, Any]:
    """Enough of a game record for the MOE views when the opinion's game
    has left the slate; the line views refuse it gracefully."""
    return {
        "event_id": str(row.get("event_id") or ""),
        "away_team": str(row.get("away_team") or ""),
        "home_team": str(row.get("home_team") or ""),
        "commence_time_utc": str(row.get("commence_time_utc") or ""),
        "status": "past",
    }


def desk_sync_once(
    config: Any,
    api: Any,
    *,
    now: datetime | None = None,
    priority_event_id: str | None = None,
) -> Any:
    """One reconcile pass over the desk group (moe_desk.sync_desk): the
    sheet's opinions and games in, silent posts and edits out, loud
    replies for new bet legs and near locks. Serialised so the periodic
    loop and a button tap never race on the state file."""
    now = now or datetime.now(timezone.utc)
    with _DESK_SYNC_LOCK:
        rows = load_cached_moe_opinions()
        games, _, team_abbrevs = load_intake_data()
        registry = load_moe_registry()
        desks = build_desk_model(
            games, rows, approved_moe_opinions(rows), registry, now=now
        )
        state = load_desk_state(config.state_path)
        try:
            summary = sync_desk(
                config=config,
                api=api,
                state=state,
                desks=desks,
                now=now,
                team_abbrevs=team_abbrevs,
                priority_event_id=priority_event_id,
            )
        finally:
            save_desk_state(config.state_path, state)
    if any(
        (
            summary.posted,
            summary.edited,
            summary.alerts,
            summary.deleted,
            summary.deferred,
            summary.errors,
        )
    ):
        print(
            f"desk: posted {summary.posted} edited {summary.edited} "
            f"alerts {summary.alerts} deleted {summary.deleted} "
            f"deferred {summary.deferred} errors {summary.errors}"
        )
    return summary


def update_desk_picks_view(
    config: Any,
    api: Any,
    event_id: str,
    *,
    view: Any,
    message_id: int,
    force_opinions_refresh: bool = False,
) -> tuple[Any, str | None]:
    """Atomically select and edit one existing Picks card; never post it."""
    with _DESK_SYNC_LOCK:
        state = load_desk_state(config.state_path)
        raw_previous = state["expanded_picks"].get(event_id)
        previous = raw_previous
        now = datetime.now(timezone.utc)
        if force_opinions_refresh:
            expire_sheet_cache("moe_opinions")
        rows = load_cached_moe_opinions()
        games, _, _ = load_intake_data()
        desks = build_desk_model(
            games,
            rows,
            approved_moe_opinions(rows),
            load_moe_registry(),
            now=now,
        )
        desk = next(
            (item for item in desks if item.event_id == event_id),
            None,
        )
        if desk is None or not desk.show_picks:
            return previous, "game is no longer available"
        effective_view = resolve_picks_view(desk, view)
        text, keyboard = render_picks_card(
            desk,
            config=config,
            view=effective_view,
        )
        try:
            edited = api.edit(
                config.chat_id,
                int(message_id),
                text,
                keyboard=keyboard,
            )
        except Exception as exc:  # noqa: BLE001 - returned to callback logger
            return previous, f"{type(exc).__name__}: {exc}"
        if not edited:
            return previous, "existing message could not be edited"
        if effective_view is None:
            state["expanded_picks"].pop(event_id, None)
        else:
            state["expanded_picks"][event_id] = effective_view
        state["cards"][f"picks:{event_id}"] = {
            "message_id": int(message_id),
            "hash": desk_content_hash(
                text,
                keyboard,
                config.picks_topic,
            ),
            "topic": config.picks_topic,
            "reply_to": None,
        }
        save_desk_state(config.state_path, state)
        return previous, None


def desk_review(action: str, target: str, *, reviewer: str) -> tuple[str, bool]:
    """Review one desk target without racing refresh or synchronization."""
    with _DESK_SYNC_LOCK:
        return _desk_review_locked(action, target, reviewer=reviewer)


def _desk_review_locked(
    action: str,
    target: str,
    *,
    reviewer: str,
) -> tuple[str, bool]:
    """Apply a desk button. Picks the target rows from the cached tab,
    confirms each is still pending with a single-row read (``store.fetch``)
    so the other reviewer's tap a moment ago is respected, runs the store's
    hash-checked review signed with the reviewer's display name, then
    patches the cached rows so the cards re-render at once without
    re-reading the whole tab (the next timed pass re-reads anyway).
    Returns the outcome text and whether it succeeded."""
    rows = load_cached_moe_opinions()
    targets, error = desk_review_targets(action, target, rows)
    if error:
        return error, False
    status = "rejected" if action == "no" else "approved"
    store = _moe_store()
    fetch = getattr(store, "fetch", None)
    done: list[str] = []
    reviewed_at = datetime.now(timezone.utc).isoformat()
    for row in targets:
        opinion_id = str(row["opinion_id"])
        if fetch is not None:
            live = fetch(opinion_id)
            if live is None:
                return "That row is no longer in the sheet.", False
            live_status = str(live.get("review_status") or "pending").strip().lower()
            if live_status != "pending":
                by = str(live.get("reviewed_by") or "someone").strip()
                row.update(
                    review_status=live_status,
                    reviewed_by=by,
                    reviewed_at_utc=str(live.get("reviewed_at_utc") or ""),
                    approved_output_sha256=str(live.get("approved_output_sha256") or ""),
                )
                return f"Already {live_status} by {by}.", False
        store.review(opinion_id, status=status, reviewed_by=reviewer, note="")
        row["review_status"] = status
        row["reviewed_by"] = reviewer
        row["reviewed_at_utc"] = reviewed_at
        row["review_note"] = ""
        row["approved_output_sha256"] = (
            opinion_output_sha256(row) if status == "approved" else ""
        )
        done.append(str(row.get("expert_name") or row.get("expert_id")))
    _patch_sheet_cache("moe_opinions", rows)
    verb = "Rejected" if status == "rejected" else "Approved"
    return f"{verb} {', '.join(done)} as {reviewer}.", True


def load_intake_data() -> tuple[list[dict[str, Any]], dict[str, str], dict[str, str]]:
    spreadsheet = _intake_spreadsheet()
    games = _cached_sheet_value(
        "nfl_games",
        GAMES_CACHE_TTL_SECONDS,
        lambda: spreadsheet.worksheet("nfl_games").get_all_records(),
    )
    emoji_rows = _cached_sheet_value(
        "team_emojis",
        TEAM_EMOJI_CACHE_TTL_SECONDS,
        lambda: spreadsheet.worksheet(TEAM_EMOJI_TAB).get_all_records(
            expected_headers=TEAM_EMOJI_HEADERS
        ),
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


def _celebrity_worksheet(spreadsheet: Any) -> Any:
    """Return the celebrity_picks worksheet, creating it with headers the first
    time. The one supported legacy header is expanded in place."""
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
        worksheet.resize(cols=len(CELEBRITY_HEADERS))
        worksheet.update([CELEBRITY_HEADERS])
    elif header == LEGACY_CELEBRITY_HEADERS:
        worksheet.resize(cols=len(CELEBRITY_HEADERS))
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
    spreadsheet = _intake_spreadsheet()
    values = _cached_sheet_value(
        "celebrity_registry",
        CELEBRITY_REGISTRY_CACHE_TTL_SECONDS,
        lambda: _celebrity_registry_worksheet(
            spreadsheet
        ).get_all_values(),
    )
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
    """Append rows, deduping each submission/name/canonical-bet tuple."""
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
        key_index = header_row.index("canonical_key")
        legacy_pairs = set()
        for row in values[1:]:
            if len(row) > max(sub_index, name_index):
                pair = (row[sub_index], row[name_index])
                canonical = row[key_index] if len(row) > key_index else ""
                if canonical:
                    existing.add((*pair, canonical))
                else:
                    legacy_pairs.add(pair)
    else:
        legacy_pairs = set()
    to_append: list[list[Any]] = []
    for row in rows:
        key = (
            str(row["submission_id"]),
            str(row["celebrity_name"]),
            str(row["canonical_key"]),
        )
        if key[:2] in legacy_pairs or key in existing:
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
    with _CELEBRITY_REGISTRY_LOCK:
        spreadsheet = _intake_spreadsheet()
        worksheet = _celebrity_registry_worksheet(spreadsheet)
        norm_index = CELEBRITY_REGISTRY_HEADERS.index("normalized_name")
        name_index = CELEBRITY_REGISTRY_HEADERS.index("celebrity_name")
        values = worksheet.get_all_values()
        _set_sheet_cache("celebrity_registry", values)
        for row in values[1:]:
            if len(row) > norm_index and row[norm_index] == key:
                existing = (
                    row[name_index] if len(row) > name_index else normalized
                )
                return {
                    "celebrity_id": celeb_id,
                    "celebrity_name": existing or normalized,
                }
        now = datetime.now(timezone.utc)
        new_row = [
            celeb_id,
            normalized,
            key,
            now.isoformat(),
            now.astimezone(ET).isoformat(),
            created_by_user_id if created_by_user_id is not None else "",
            created_by_username or "",
        ]
        worksheet.append_row(new_row, value_input_option="RAW")
        _set_sheet_cache("celebrity_registry", [*values, new_row])
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
    ak_user_id = resolve_moe_expert_user_id_from_spreadsheet(
        _intake_spreadsheet(),
        "ak",
    )

    client = TelegramClient(StringSession(session), api_id, api_hash)
    pending_suggestions: dict[int, int] = {}
    guess_states: dict[int, dict[str, Any]] = {}
    game_celeb_states: dict[int, dict[str, Any]] = {}
    win_guess_states: dict[int, dict[str, Any]] = {}
    # The desk group (moe_desk.py); filled after client.start() once the
    # bot's username is known, read by the callback and deep-link handlers.
    desk: dict[str, Any] = {"config": None, "api": None, "task": None}

    def open_game_state(user_id: int, game: dict[str, Any]) -> None:
        game_celeb_states.pop(user_id, None)
        win_guess_states.pop(user_id, None)
        guess_states[user_id] = {
            "game": game,
            "days": 10,
            "page": 0,
            "celebrity": None,
        }

    async def resync_desk(
        *,
        priority_event_id: str | None = None,
    ) -> str | None:
        """Re-render the cards now; the problem text when that failed."""
        if desk["config"] is None or desk["api"] is None:
            return None
        try:
            summary = await asyncio.to_thread(
                desk_sync_once,
                desk["config"],
                desk["api"],
                priority_event_id=priority_event_id,
            )
        except Exception as exc:  # noqa: BLE001 - the loop retries
            print(f"desk: sync after a review failed: {type(exc).__name__}: {exc}")
            return f"{type(exc).__name__}: {exc}"[:200]
        relevant_deferred = [
            key
            for key in summary.deferred
            if priority_event_id is None
            or key == f"picks:{priority_event_id}"
            or key.startswith(f"picks-detail:{priority_event_id}:")
        ]
        relevant_errors = [
            error
            for error in summary.errors
            if priority_event_id is None
            or error.startswith(f"picks:{priority_event_id}:")
            or error.startswith(f"picks-detail:{priority_event_id}:")
        ]
        if relevant_errors or relevant_deferred:
            parts = [
                *relevant_errors,
                *[f"{key}: deferred" for key in relevant_deferred],
            ]
            return "; ".join(parts)[:200]
        return None

    desk_inflight: set[str] = set()
    desk_tasks: set[asyncio.Task] = set()

    async def desk_reply(event, text: str) -> None:
        """A silent reply under the card the tap came from — both reviewers
        see the outcome, and it survives Telegram's callback timeout."""
        try:
            await event.reply(text, silent=True)
        except Exception as exc:  # noqa: BLE001 - logged, nothing else to do
            print(f"desk: reply failed: {type(exc).__name__}: {exc}")

    async def finish_desk_action(event, action: str, target: str, key: str) -> None:
        try:
            try:
                reviewers = await asyncio.to_thread(load_desk_reviewers)
            except Exception as exc:  # noqa: BLE001 - Sheets down
                await desk_reply(
                    event,
                    "❌ Could not read the reviewer list "
                    f"({type(exc).__name__}: {exc}). Tap again in a minute."[:400],
                )
                return
            reviewer = reviewers.get(event.sender_id)
            if reviewer is None:
                await desk_reply(
                    event,
                    "❌ Reviewers only. Add the reviewer role to your allowed_users row.",
                )
                return
            try:
                text, ok = await asyncio.to_thread(
                    desk_review, action, target, reviewer=reviewer
                )
            except Exception as exc:  # noqa: BLE001 - shown under the card
                text, ok = f"{type(exc).__name__}: {exc}"[:350], False
            if not ok:
                await desk_reply(event, f"❌ {text}")
                return
            print(f"desk: {text}")
            problem = await resync_desk()
            if problem:
                await desk_reply(
                    event,
                    f"✅ {text} The card could not refresh yet ({problem}); "
                    "it will on the next pass.",
                )
        finally:
            desk_inflight.discard(key)

    async def handle_desk_callback(event, data: str) -> None:
        if desk["config"] is None or str(event.chat_id) != str(
            desk["config"].chat_id
        ):
            await event.answer(
                "This action belongs to the MOE group.",
                alert=True,
            )
            return
        parsed = parse_desk_callback(data)
        if parsed is None:
            await event.answer("Unknown desk action.", alert=True)
            return
        action, target = parsed
        if action in {"show", "hide", "op", "part", "page", "refresh"}:
            event_id = target
            desired_view: Any = (
                "menu" if action in {"show", "page", "refresh"} else None
            )
            if action == "page":
                legacy_event_id, separator, _ = target.rpartition(":")
                if separator and legacy_event_id:
                    event_id = legacy_event_id
            elif action == "op":
                event_id, separator, expert = target.rpartition(":")
                if not separator or not event_id:
                    await event.answer("Invalid opinion.", alert=True)
                    return
                desired_view = {
                    "mode": "opinion",
                    "expert": expert,
                    "chunk": 0,
                }
            elif action == "part":
                event_and_expert, separator, raw_chunk = target.rpartition(":")
                event_id, expert_separator, expert = (
                    event_and_expert.rpartition(":")
                )
                if not separator or not expert_separator or not event_id:
                    await event.answer("Invalid opinion page.", alert=True)
                    return
                try:
                    desired_view = {
                        "mode": "opinion",
                        "expert": expert,
                        "chunk": max(0, int(raw_chunk)),
                    }
                except ValueError:
                    await event.answer("Invalid opinion page.", alert=True)
                    return
            key = f"view:{event_id}"
            if key in desk_inflight:
                await event.answer("Still updating this game.")
                return
            desk_inflight.add(key)
            try:
                await event.answer(
                    "Returning to picks…"
                    if desired_view is None
                    else "Loading opinions…"
                )
                message_id = (
                    getattr(event, "message_id", None)
                    or getattr(event.query, "msg_id", None)
                )
                if message_id is None:
                    print("desk: callback had no message id")
                    return
                _, problem = await asyncio.to_thread(
                    update_desk_picks_view,
                    desk["config"],
                    desk["api"],
                    event_id,
                    view=desired_view,
                    message_id=message_id,
                    force_opinions_refresh=action == "refresh",
                )
                if problem:
                    print(
                        "desk: same-card view failed without changing state: "
                        f"{problem}"
                    )
            finally:
                desk_inflight.discard(key)
            return
        key = f"{action}:{target}"
        if key in desk_inflight:
            await event.answer("Still working on the last tap.")
            return
        desk_inflight.add(key)
        # Answer at once: Telegram discards a callback answer after a few
        # seconds, and every step of the work reads the sheet.
        try:
            await event.answer("Working on it…")
        except Exception as exc:  # noqa: BLE001 - the work still runs
            print(f"desk: answer failed: {type(exc).__name__}: {exc}")
        task = asyncio.create_task(finish_desk_action(event, action, target, key))
        desk_tasks.add(task)
        task.add_done_callback(desk_tasks.discard)

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
            game_celeb_states.pop(event.sender_id, None)
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
        game_celeb_states.pop(event.sender_id, None)
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
            pattern=r"^/start(?:@\w+)?\s+\S+",
            incoming=True,
            func=lambda event: event.is_private,
        )
    )
    async def open_deep_link(event):
        """``/start op_<opinion>`` and ``/start game_<event>`` — the desk
        group's 👁 buttons land here, in the tapper's own DM."""
        if event.sender_id not in allowed:
            await event.respond("Not authorized.")
            return
        parsed = parse_start_param(event.raw_text)
        if parsed is None:
            await event.respond(
                "Tap /guess_nfl_game to browse games.",
                buttons=command_keyboard(),
            )
            return
        kind, value = parsed
        records, _, _ = await _intake_data()
        rows = await asyncio.to_thread(load_cached_moe_opinions)
        if kind == "game":
            game = next(
                (g for g in records if str(g.get("event_id")) == value), None
            )
            if game is None:
                await event.respond("That game is not in the current slate.")
                return
            open_game_state(event.sender_id, game)
            text, buttons = moe_opinion_summary(
                game,
                [r for r in rows if str(r.get("event_id")) == value],
                event_id=value,
            )
            await event.respond(text, buttons=buttons, parse_mode="html")
            return
        opinion = next(
            (r for r in rows if str(r.get("opinion_id")) == value), None
        )
        if opinion is None or str(
            opinion.get("generation_status") or "valid"
        ) != "valid":
            await event.respond("That opinion is not available.")
            return
        if str(opinion.get("review_status") or "") != "approved":
            reviewers = await asyncio.to_thread(load_desk_reviewers)
            if event.sender_id not in reviewers:
                await event.respond(
                    "That opinion is available once it is approved."
                )
                return
        elif not approved_moe_opinions([opinion]):
            await event.respond("That opinion is no longer available.")
            return
        event_id = str(opinion.get("event_id") or "")
        game = next(
            (g for g in records if str(g.get("event_id")) == event_id), None
        )
        open_game_state(event.sender_id, game or game_stub(opinion))
        text, buttons = moe_opinion_detail(opinion, event_id=event_id)
        await event.respond(text, buttons=buttons, parse_mode="html")

    @client.on(
        events.NewMessage(
            pattern=r"^/desk(?:@\w+)?$",
            incoming=True,
            func=lambda event: not event.is_private,
        )
    )
    async def report_desk_ids(event):
        """Sent inside the group: answer with the chat id (and the topic id
        when sent inside a topic) the desk needs, and whether the group is
        a supergroup with Topics yet. Commands reach the bot in groups even
        with privacy mode on."""
        if event.sender_id not in allowed:
            return
        chat = await event.get_chat()
        text = desk_ids_report(
            event.chat_id,
            title=str(getattr(chat, "title", "") or ""),
            supergroup=bool(getattr(chat, "megagroup", False)),
            topics=bool(getattr(chat, "forum", False)),
            topic_id=topic_id_from_reply(getattr(event.message, "reply_to", None)),
        )
        print("desk: " + text.replace("\n", " · "))
        await event.reply(text)

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

        game_celeb_state = game_celeb_states.get(event.sender_id)
        if (
            game_celeb_state is not None
            and game_celeb_state.get("prompt_msg_id") is not None
            and game_celeb_state.get("prompt_msg_id")
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
            game_celeb_state.pop("prompt_msg_id", None)
            game_celeb_state.pop("roster", None)
            game_celeb_state["celebrity"] = {
                "id": celebrity["celebrity_id"],
                "name": celebrity["celebrity_name"],
            }
            guess_states.pop(event.sender_id, None)
            records, _, team_abbrevs = await _intake_data()
            text, buttons = game_browser(
                records,
                days=int(game_celeb_state["days"]),
                page=int(game_celeb_state["page"]),
                now=datetime.now(timezone.utc),
                team_abbrevs=team_abbrevs,
                celebrity_name=celebrity["celebrity_name"],
            )
            await event.respond(text, buttons=buttons, parse_mode="html")
            return

        state = guess_states.get(event.sender_id)
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
        raw_lean_text = event.raw_text
        lean_text = raw_lean_text.strip()
        if not lean_text:
            await event.respond(
                "Your lean cannot be empty. Reply to the prompt with some text."
            )
            return
        celebrity = submission.get("celebrity")
        is_custom = submission["market"] == "custom"
        if is_custom and not isinstance(celebrity, dict):
            await event.respond(
                "Custom markets are available only for attributed celebrity "
                "picks."
            )
            return
        prediction = None
        if not is_custom and requires_ak_projection(
            event.sender_id,
            ak_user_id,
            celebrity,
        ):
            prediction = parse_ak_projection(
                lean_text,
                away_team=str(submission["game"]["away_team"]),
                home_team=str(submission["game"]["home_team"]),
            )
            if prediction["status"] != "parsed":
                await event.respond(
                    "AK submissions require one exact team-labeled projected "
                    "score. Example:\n"
                    f"{ak_projection_example(submission['game'])}\n"
                    "Rationale: your game analysis.",
                )
                return
        sender = await event.get_sender()
        submitted_at = datetime.now(timezone.utc)
        if is_custom:
            try:
                if has_complete_custom_pick_fields(raw_lean_text):
                    custom_submissions = [
                        build_custom_celebrity_submission(
                            submitted_at=submitted_at,
                            user_id=event.sender_id,
                            username=getattr(sender, "username", None),
                            message_id=event.id,
                            game=submission["game"],
                            period=submission["period"],
                            market_family=str(
                                submission["custom_market_family"]
                            ),
                            raw_text=raw_lean_text,
                        )
                    ]
                else:
                    parsed = await claude_parse(
                        raw_lean_text,
                        date=submitted_at.astimezone(ET).date().isoformat(),
                    )
                    if parsed is None:
                        raise ValueError(
                            "The free-form celebrity pick could not be parsed"
                        )
                    custom_submissions = build_freeform_celebrity_submissions(
                        submitted_at=submitted_at,
                        user_id=event.sender_id,
                        username=getattr(sender, "username", None),
                        message_id=event.id,
                        game=submission["game"],
                        parsed=parsed,
                        raw_text=raw_lean_text,
                    )
            except (anthropic.APIError, TimeoutError):
                log.exception(
                    "Celebrity free-form parser failed user=%s message=%s",
                    event.sender_id,
                    event.id,
                )
                prompt = await event.respond(
                    "The celebrity pick parser is temporarily unavailable, "
                    "so this pick was not saved. Reply again to retry.\n\n"
                    + custom_pick_prompt(
                        str(submission["custom_market_family"])
                    ),
                    buttons=Button.force_reply(
                        single_use=True,
                        placeholder="Reply with the celebrity's exact pick",
                    ),
                )
                if guess_states.get(event.sender_id) is state:
                    state["prompt_msg_id"] = prompt.id
                return
            except ValueError as exc:
                prompt = await event.respond(
                    f"{exc}\n\n{custom_pick_prompt(str(submission['custom_market_family']))}",
                    buttons=Button.force_reply(
                        single_use=True,
                        placeholder="Reply with the celebrity's exact pick",
                    ),
                )
                if guess_states.get(event.sender_id) is state:
                    state["prompt_msg_id"] = prompt.id
                return
            celebrity_rows = [
                row
                for custom_submission in custom_submissions
                for row in build_celebrity_rows(
                    submission=custom_submission,
                    names=[str(celebrity["name"])],
                )
            ]
            appended = bool(
                await asyncio.to_thread(
                    append_celebrity_picks,
                    celebrity_rows,
                )
            )
            summary = "\n".join(
                (
                    f"{CELEBRITY_PERIOD_LABELS[str(row['period'])]} · "
                    f"{str(row['market_family']).replace('_', ' ').title()} · "
                    f"{html.escape(str(row['subject']))} · "
                    f"{html.escape(str(row['selection_text']))}"
                )
                for row in celebrity_rows
            )
        else:
            row = build_lean_row(
                submitted_at=submitted_at,
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
                prediction=prediction,
            )
            appended = await asyncio.to_thread(append_lean, row)
            if isinstance(celebrity, dict):
                celebrity_rows = build_celebrity_rows(
                    submission={
                        "submission_id": row["submission_id"],
                        "submitted_at_utc": row["submitted_at_utc"],
                        "submitted_at_et": row["submitted_at_et"],
                        "telegram_user_id": event.sender_id,
                        "telegram_username": (
                            getattr(sender, "username", None) or ""
                        ),
                        "event_id": row["event_id"],
                        "season": row["season"],
                        "week": row["week"],
                        "commence_time_utc": row["commence_time_utc"],
                        "commence_time_et": row["commence_time_et"],
                        "away_team": row["away_team"],
                        "home_team": row["home_team"],
                        "period": row["period"],
                        "market": row["market"],
                        "side": row["side"],
                        "latest_selected_line": row["latest_selected_line"],
                        "latest_selected_price": row["latest_selected_price"],
                        "raw_pick_text": raw_lean_text,
                    },
                    names=[str(celebrity["name"])],
                )
                celebrity_written = await asyncio.to_thread(
                    append_celebrity_picks,
                    celebrity_rows,
                )
                appended = appended or bool(celebrity_written)
            summary = (
                f"{PERIOD_LABELS[submission['period']]} · "
                f"{submission['market'].title()} · "
                f"{html.escape(str(row['side']))}"
            )
        status = "✅ Guess saved." if appended else "✅ Guess was already saved."
        saved_summary = f"{status}\n{summary}"
        if isinstance(celebrity, dict):
            saved_summary += (
                f"\n🎤 <b>{html.escape(str(celebrity['name']))}</b>"
            )

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

        guess_states.pop(event.sender_id, None)
        await event.respond(
            saved_summary, buttons=command_keyboard(), parse_mode="html"
        )
        active = game_celeb_states.get(event.sender_id, {}).get("celebrity")
        records, _, team_abbrevs = await _intake_data()
        text, buttons = game_browser(
            records,
            days=int(submission["days"]),
            page=int(submission["page"]),
            now=datetime.now(timezone.utc),
            team_abbrevs=team_abbrevs,
            celebrity_name=(
                str(active["name"]) if isinstance(active, dict) else None
            ),
        )
        await event.respond(text, buttons=buttons, parse_mode="html")

    @client.on(events.CallbackQuery)
    async def handle_callback(event):
        if event.sender_id not in allowed:
            await event.answer("Not authorized.", alert=True)
            return
        data = event.data.decode()
        if data.startswith("desk:"):
            await handle_desk_callback(event, data)
            return
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
        is_moe_callback = (
            data.startswith("moe:view")
            or data.startswith("moe:expert:")
            or data.startswith("moe:opinion:")
        )
        if not is_moe_callback:
            records, team_emojis, team_abbrevs = await _intake_data()
        if is_moe_callback:
            state = guess_states.get(event.sender_id)
            if state is None:
                await event.answer(
                    "This game view expired. Choose the game again.",
                    alert=True,
                )
                return
            if data.startswith("moe:view"):
                parts = data.split(":")
                if len(parts) != 4:
                    await event.answer("Invalid MOE view.", alert=True)
                    return
                try:
                    event_id = parts[2]
                    page = int(parts[3])
                except ValueError:
                    await event.answer("Invalid MOE page.", alert=True)
                    return
                if str(state["game"]["event_id"]) != event_id:
                    await event.answer(
                        "This MOE view expired. Reopen the game.",
                        alert=True,
                    )
                    return
                opinions = await asyncio.to_thread(
                    load_cached_moe_opinions, event_id
                )
                text, buttons = moe_opinion_summary(
                    state["game"],
                    opinions,
                    page=page,
                    event_id=event_id,
                )
                await edit_callback(event, text, buttons)
                return
            if data.startswith("moe:opinion:"):
                parts = data.split(":")
                if len(parts) != 4:
                    await event.answer("Invalid MOE view.", alert=True)
                    return
                opinion_id = parts[2]
                try:
                    page = int(parts[3])
                except ValueError:
                    await event.answer("Invalid MOE page.", alert=True)
                    return
                event_id = str(state["game"]["event_id"])
                opinions = await asyncio.to_thread(
                    load_cached_moe_opinions, event_id
                )
                candidates = approved_moe_opinions(opinions)
                if event.sender_id in await asyncio.to_thread(load_desk_reviewers):
                    # Reviewers page through pending and rejected rows too
                    # (the desk group's 👁 deep link lands on them).
                    candidates = candidates + [
                        row
                        for row in opinions
                        if str(row.get("generation_status") or "valid") == "valid"
                        and str(row.get("review_status") or "") != "approved"
                    ]
                opinion = next(
                    (
                        row
                        for row in candidates
                        if str(row.get("opinion_id")) == opinion_id
                    ),
                    None,
                )
                if opinion is None:
                    await event.answer(
                        "That model opinion is no longer available.",
                        alert=True,
                    )
                    return
                model_choices = latest_moe_model_opinions(
                    opinions, str(opinion["expert_id"])
                )
                text, buttons = moe_opinion_detail(
                    opinion,
                    page=page,
                    event_id=event_id,
                    show_model_picker=len(model_choices) > 1,
                )
                await edit_callback(event, text, buttons)
                return
            parts = data.split(":")
            if len(parts) != 5:
                await event.answer("Invalid MOE view.", alert=True)
                return
            expert_id = parts[2]
            event_id = parts[3]
            try:
                page = int(parts[4])
            except ValueError:
                await event.answer("Invalid MOE page.", alert=True)
                return
            if str(state["game"]["event_id"]) != event_id:
                await event.answer(
                    "This MOE view expired. Reopen the game.",
                    alert=True,
                )
                return
            opinions = await asyncio.to_thread(
                load_cached_moe_opinions, event_id
            )
            model_choices = latest_moe_model_opinions(
                opinions, expert_id
            )
            if not model_choices:
                await event.answer(
                    "That expert opinion is no longer available.",
                    alert=True,
                )
                return
            if len(model_choices) > 1:
                text, buttons = moe_opinion_model_picker(
                    opinions,
                    expert_id=expert_id,
                    page=page,
                    event_id=event_id,
                )
                await edit_callback(event, text, buttons)
                return
            text, buttons = moe_opinion_detail(
                model_choices[0], page=page, event_id=event_id
            )
            await edit_callback(event, text, buttons)
            return
        if data.startswith("celebgame:"):
            parts = data.split(":")
            action = parts[1] if len(parts) > 1 else ""
            if action == "pick":
                if len(parts) != 5:
                    await event.answer("Invalid selection.", alert=True)
                    return
                celebrity_id, days_raw, page_raw = parts[2:]
            elif action in {"start", "new", "self", "cancel"}:
                if len(parts) != 4:
                    await event.answer("Invalid selection.", alert=True)
                    return
                days_raw, page_raw = parts[2:]
                celebrity_id = ""
            else:
                await event.answer("Invalid selection.", alert=True)
                return
            try:
                days = int(days_raw)
                page = int(page_raw)
            except ValueError:
                await event.answer("Invalid selection.", alert=True)
                return
            state = game_celeb_states.setdefault(event.sender_id, {})
            state["days"] = days
            state["page"] = page
            if action == "start":
                roster = await asyncio.to_thread(load_celebrity_roster)
                state["roster"] = {
                    str(celebrity_user_id(name)): name for name in roster
                }
                text, buttons = game_celebrity_picker(
                    roster,
                    days=days,
                    page=page,
                )
                await edit_callback(event, text, buttons)
                return
            if action == "pick":
                roster = state.get("roster")
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
                state["celebrity"] = {
                    "id": celebrity["celebrity_id"],
                    "name": celebrity["celebrity_name"],
                }
                state.pop("roster", None)
                guess_states.pop(event.sender_id, None)
            elif action == "new":
                prompt = await event.respond(
                    "Type the celebrity name:",
                    buttons=Button.force_reply(
                        single_use=True,
                        placeholder="e.g. LeBron James",
                    ),
                )
                state["prompt_msg_id"] = prompt.id
                await event.answer()
                return
            elif action == "self":
                state.pop("celebrity", None)
                guess_states.pop(event.sender_id, None)
            elif action == "cancel":
                pass
            state.pop("roster", None)
            state.pop("prompt_msg_id", None)
            active = state.get("celebrity")
            text, buttons = game_browser(
                records,
                days=days,
                page=page,
                now=datetime.now(timezone.utc),
                team_abbrevs=team_abbrevs,
                celebrity_name=(
                    str(active["name"]) if isinstance(active, dict) else None
                ),
            )
            await edit_callback(event, text, buttons)
            return
        if data.startswith("games:"):
            guess_states.pop(event.sender_id, None)
            win_guess_states.pop(event.sender_id, None)
            _, days_raw, page_raw = data.split(":", 2)
            game_celeb_state = game_celeb_states.get(event.sender_id, {})
            game_celeb_state.pop("prompt_msg_id", None)
            game_celeb_state.pop("roster", None)
            active = game_celeb_state.get("celebrity")
            text, buttons = game_browser(
                records,
                days=int(days_raw),
                page=int(page_raw),
                now=datetime.now(timezone.utc),
                team_abbrevs=team_abbrevs,
                celebrity_name=(
                    str(active["name"]) if isinstance(active, dict) else None
                ),
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
            game_celeb_state = game_celeb_states.get(event.sender_id, {})
            game_celeb_state.pop("prompt_msg_id", None)
            game_celeb_state.pop("roster", None)
            active = game_celeb_state.get("celebrity")
            guess_states[event.sender_id] = {
                "game": game,
                "days": int(days_raw),
                "page": int(page_raw),
                "celebrity": dict(active) if isinstance(active, dict) else None,
            }
            text, buttons = game_detail(
                game,
                days=int(days_raw),
                page=int(page_raw),
                team_emojis=team_emojis,
                celebrity_name=(
                    str(active["name"]) if isinstance(active, dict) else None
                ),
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
            state.pop("custom_market_family", None)
            state.pop("prompt_msg_id", None)
            try:
                text, buttons = game_detail(
                    state["game"],
                    days=state["days"],
                    page=state["page"],
                    team_emojis=team_emojis,
                    celebrity_name=(
                        str(state["celebrity"]["name"])
                        if isinstance(state.get("celebrity"), dict)
                        else None
                    ),
                )
            except (KeyError, TypeError, ValueError):
                # A deep-linked opinion whose game has left the slate.
                await event.answer(
                    "That game has left the slate. Open /guess_nfl_game.",
                    alert=True,
                )
                return
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
            state.pop("custom_market_family", None)
            state.pop("prompt_msg_id", None)
            text = period_market_summary(
                state["game"],
                period=state["period"],
                team_emojis=team_emojis,
                celebrity_name=(
                    str(state["celebrity"]["name"])
                    if isinstance(state.get("celebrity"), dict)
                    else None
                ),
            )
            await edit_callback(
                event,
                text,
                market_buttons(
                    allow_custom=isinstance(
                        state.get("celebrity"),
                        dict,
                    )
                ),
            )
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
                celebrity_name=(
                    str(state["celebrity"]["name"])
                    if isinstance(state.get("celebrity"), dict)
                    else None
                ),
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
                celebrity_name=(
                    str(state["celebrity"]["name"])
                    if isinstance(state.get("celebrity"), dict)
                    else None
                ),
            )
            await edit_callback(
                event,
                text,
                market_buttons(
                    allow_custom=isinstance(
                        state.get("celebrity"),
                        dict,
                    )
                ),
            )
            return
        if data.startswith("market:"):
            state = guess_states.get(event.sender_id)
            market = data.split(":", 1)[1]
            if (
                market == "custom"
                and state is not None
                and "period" in state
                and isinstance(state.get("celebrity"), dict)
            ):
                state["market"] = "custom"
                state.pop("side", None)
                state.pop("custom_market_family", None)
                await edit_callback(
                    event,
                    (
                        f"🎤 <b>{html.escape(str(state['celebrity']['name']))}"
                        "</b>\n\nChoose the custom pick type:"
                    ),
                    custom_market_buttons(),
                )
                return
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
                celebrity_name=(
                    str(state["celebrity"]["name"])
                    if isinstance(state.get("celebrity"), dict)
                    else None
                ),
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
        if data.startswith("custom:"):
            state = guess_states.get(event.sender_id)
            market_family = data.split(":", 1)[1]
            if (
                state is None
                or state.get("market") != "custom"
                or market_family not in CUSTOM_MARKET_FAMILIES
                or not isinstance(state.get("celebrity"), dict)
            ):
                await event.answer(
                    "This guess expired. Choose the game again.", alert=True
                )
                return
            state["custom_market_family"] = market_family
            state["side"] = "custom"
            await edit_callback(
                event,
                (
                    f"🎤 <b>{html.escape(str(state['celebrity']['name']))}</b>"
                    "\n\n"
                    f"<b>{html.escape(PERIOD_LABELS[state['period']])} · "
                    f"{html.escape(market_family.replace('_', ' ').title())}"
                    "</b>"
                ),
                [[Button.inline("← Back to markets", b"back:markets")]],
            )
            prompt = await event.respond(
                custom_pick_prompt(market_family),
                buttons=Button.force_reply(
                    single_use=True,
                    placeholder="Enter the structured celebrity pick",
                ),
            )
            state["prompt_msg_id"] = prompt.id
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
                (
                    f"🎤 <b>{html.escape(str(state['celebrity']['name']))}</b>"
                    "\n\n"
                )
                if isinstance(state.get("celebrity"), dict)
                else ""
            ) + (
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
            prompt_text = (
                "Enter one exact team-labeled projected score and your "
                "reasoning. Example:\n"
                f"{ak_projection_example(state['game'])}\n"
                "Rationale: your game analysis."
                if requires_ak_projection(
                    event.sender_id,
                    ak_user_id,
                    state.get("celebrity"),
                )
                else (
                    "Enter your lean, reasoning, and the line or price where "
                    "your preference changes:"
                )
            )
            prompt = await event.respond(
                prompt_text,
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
    desk_config = desk_config_from_env()
    if desk_config is None:
        print("Desk group disabled (MOE_DESK_CHAT_ID / topic ids unset)")
    else:
        desk["config"] = desk_config.with_username(identity.username or "")
        desk["api"] = DeskBotApi(desk_config.bot_token)
        print(
            f"Desk group enabled: chat {desk_config.chat_id}, review topic "
            f"{desk_config.review_topic}, picks topic {desk_config.picks_topic}, "
            f"scores topic {desk_config.scores_topic}, sync every "
            f"{desk_config.sync_seconds}s"
        )

        async def desk_loop() -> None:
            while True:
                # Keep the reviewer list warm so a tap never waits on it.
                try:
                    await asyncio.to_thread(load_desk_reviewers)
                except Exception as exc:  # noqa: BLE001 - Sheets down
                    print(f"desk: reviewers refresh failed: {type(exc).__name__}: {exc}")
                try:
                    await asyncio.to_thread(
                        desk_sync_once, desk["config"], desk["api"]
                    )
                except Exception as exc:  # noqa: BLE001 - keep the loop alive
                    print(f"desk: sync failed: {type(exc).__name__}: {exc}")
                await asyncio.sleep(desk["config"].sync_seconds)

        desk["task"] = asyncio.create_task(desk_loop())
    await client.run_until_disconnected()


if __name__ == "__main__":
    asyncio.run(main())
