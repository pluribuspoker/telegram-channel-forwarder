"""Dedicated native Telegram interface for NFL lean intake."""

from __future__ import annotations

import asyncio
import html
import os
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
                    KeyboardButton(text="/suggest"),
                ]
            )
        ],
        resize=True,
        single_use=False,
        persistent=True,
        placeholder="Guess an NFL game or suggest an improvement",
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


def _game_button_label(game: dict[str, Any]) -> str:
    kickoff = _parse_time(str(game["commence_time_utc"])).astimezone(ET)
    return (
        f"{kickoff:%a %-m/%-d %-I:%M %p} · "
        f"{game['away_team']} @ {game['home_team']}"
    )


def game_browser(
    records: list[dict[str, Any]],
    *,
    days: int,
    page: int,
    now: datetime,
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
        buttons.append([Button.inline(_game_button_label(game), callback)])

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


def load_intake_data() -> tuple[list[dict[str, Any]], dict[str, str]]:
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
    return games, team_emojis


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


async def _intake_data() -> tuple[list[dict[str, Any]], dict[str, str]]:
    return await asyncio.to_thread(load_intake_data)


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
            pattern=r"^/(?:start|guess_nfl_game)(?:@\w+)?$",
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
                "Tap /guess_nfl_game to browse games or /suggest to send feedback.",
                buttons=command_keyboard(),
            )
        guess_states.pop(event.sender_id, None)
        records, _ = await _intake_data()
        text, buttons = game_browser(
            records,
            days=10,
            page=0,
            now=datetime.now(timezone.utc),
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
        if (
            state is None
            or state.get("prompt_msg_id") != event.reply_to_msg_id
        ):
            return
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
            game=state["game"],
            period=state["period"],
            market=state["market"],
            side=state["side"],
            lean_text=lean_text,
        )
        appended = await asyncio.to_thread(append_lean, row)
        game = state["game"]
        summary = (
            f"{PERIOD_LABELS[state['period']]} · "
            f"{state['market'].title()} · "
            f"{selection_side_label(game, state['market'], state['side'])}"
        )
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
        records, team_emojis = await _intake_data()
        if data.startswith("games:"):
            guess_states.pop(event.sender_id, None)
            _, days_raw, page_raw = data.split(":", 2)
            text, buttons = game_browser(
                records,
                days=int(days_raw),
                page=int(page_raw),
                now=datetime.now(timezone.utc),
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
