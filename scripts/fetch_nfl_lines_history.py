#!/usr/bin/env python3
"""Pull historical NFL lines from free sources for the God Expert backtests.

Two committed data files come out of one run:

``data/nfl_lines_history.csv``
    One row per regular-season game from the nflverse games file
    (https://github.com/nflverse/nfldata, ``data/games.csv``): schedule,
    final score, and the closing spread, total, moneylines, and juice.
    Rewritten in full on every run (idempotent). The default span is
    1999-2025 (6,967 games): the Elo rating voice (``moe_rating.py``,
    ``scripts/fit_nfl_elo.py``) warms up on the early seasons, while the
    margin table and the backtests read 2016 onward. Spreads and totals are
    complete from 1999; moneylines and juice start in 2006 and are complete
    from 2010.

``data/nfl_open_close.json``
    ESPN core odds for each game of ``--espn-seasons``: the ``ESPN BET``
    provider's ``open``, ``close``, and ``current`` blocks, keyed by ESPN
    event id (the nflverse ``espn`` column joins the two files). Fetching is
    paced, retried, checkpointed every 25 games, and resumable: ids already
    present are skipped unless ``--refresh``.

Sign convention (verified on the data 2026-09-07, pinned by
``scripts/test_nfl_lines_history.py``)
    ``home_spread`` follows this repo's BetOnline convention: negative means
    the home team is favored (``Kansas City Chiefs -3``). nflverse's
    ``spread_line`` is the opposite sign — positive when the home team is
    favored — so ``home_spread = -spread_line``:

    * 2024 Week 1 Baltimore Ravens @ Kansas City Chiefs: ``spread_line = 3``,
      ``home_moneyline = -148``; the Chiefs won 27-20 → ``home_spread = -3``.
    * 2023 Week 1 Detroit Lions @ Kansas City Chiefs: ``spread_line = 4``,
      ``home_moneyline = -198``; the Chiefs lost 20-21 → ``home_spread = -4``.
    * 2025 Week 1 San Francisco 49ers @ Seattle Seahawks: ``spread_line =
      -2.5``, ``away_moneyline = -135`` → ``home_spread = +2.5``.

    Across 2016-2025 the sign of ``spread_line`` matches the moneyline
    favorite in 2731 of 2746 games; the 15 exceptions are ±1/±1.5 lines with
    near-even moneylines.

    ESPN's per-team ``pointSpread.american`` is already team-relative
    (``homeTeamOdds.close.pointSpread = "-2.5"`` for that same Chiefs game),
    so it is used as ``home_spread`` verbatim. ESPN's top-level ``spread``
    number is also home-relative (``+1.5`` for 49ers @ Seahawks 2025 while
    ``details`` reads ``"SF -1.5"``: ``details`` is favorite-relative, which
    is why the two "did not obviously agree" in the roadmap probe). Only the
    per-team blocks carry open/close, so they are the source of record.

Prices are American integers (``EVEN`` → 100). Empty numeric cells stay empty
strings in the CSV and ``null`` in the JSON.
"""

from __future__ import annotations

import argparse
import csv
import io
import json
import os
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable

import httpx

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from nfl_win_predictions import TEAM_ABBREVIATIONS  # noqa: E402

NFLVERSE_GAMES_URL = (
    "https://raw.githubusercontent.com/nflverse/nfldata/master/data/games.csv"
)
ESPN_ODDS_URL = (
    "https://sports.core.api.espn.com/v2/sports/football/leagues/nfl"
    "/events/{event_id}/competitions/{event_id}/odds"
)
USER_AGENT = (
    "telegram-channel-forwarder/nfl-lines-history "
    "(+https://fightclubpicks.cc; paced research pull)"
)
DATA_DIR = ROOT / "data"
LINES_CSV = DATA_DIR / "nfl_lines_history.csv"
OPEN_CLOSE_JSON = DATA_DIR / "nfl_open_close.json"

DEFAULT_SEASONS = ("1999-2025",)
DEFAULT_ESPN_SEASONS = ("2024", "2025")
DEFAULT_PACE_SECONDS = 0.75
REQUEST_TIMEOUT_SECONDS = 20.0
MAX_RETRIES = 4
CHECKPOINT_EVERY = 25
PREFERRED_PROVIDER = "ESPN BET"
REGULAR_SEASON = "REG"

# nflverse team code → the canonical full name used everywhere in this repo
# (the keys of TEAM_ABBREVIATIONS). Historical codes map to the franchise's
# current name so a team's history is one series.
NFLVERSE_TEAMS: dict[str, str] = {
    "ARI": "Arizona Cardinals",
    "ATL": "Atlanta Falcons",
    "BAL": "Baltimore Ravens",
    "BUF": "Buffalo Bills",
    "CAR": "Carolina Panthers",
    "CHI": "Chicago Bears",
    "CIN": "Cincinnati Bengals",
    "CLE": "Cleveland Browns",
    "DAL": "Dallas Cowboys",
    "DEN": "Denver Broncos",
    "DET": "Detroit Lions",
    "GB": "Green Bay Packers",
    "HOU": "Houston Texans",
    "IND": "Indianapolis Colts",
    "JAX": "Jacksonville Jaguars",
    "KC": "Kansas City Chiefs",
    "LA": "Los Angeles Rams",
    "LAC": "Los Angeles Chargers",
    "LV": "Las Vegas Raiders",
    "MIA": "Miami Dolphins",
    "MIN": "Minnesota Vikings",
    "NE": "New England Patriots",
    "NO": "New Orleans Saints",
    "NYG": "New York Giants",
    "NYJ": "New York Jets",
    "PHI": "Philadelphia Eagles",
    "PIT": "Pittsburgh Steelers",
    "SEA": "Seattle Seahawks",
    "SF": "San Francisco 49ers",
    "TB": "Tampa Bay Buccaneers",
    "TEN": "Tennessee Titans",
    "WAS": "Washington Commanders",
    # Historical codes.
    "OAK": "Las Vegas Raiders",
    "SD": "Los Angeles Chargers",
    "STL": "Los Angeles Rams",
    "LAR": "Los Angeles Rams",
}

LINES_COLUMNS = (
    "season",
    "week",
    "gameday",
    "weekday",
    "gametime",
    "espn_id",
    "away_team",
    "home_team",
    "away_score",
    "home_score",
    "home_spread",
    "total",
    "away_moneyline",
    "home_moneyline",
    "away_spread_price",
    "home_spread_price",
    "over_price",
    "under_price",
    "nflverse_spread_line",
)

BLOCKS = ("open", "close", "current")
BLOCK_FIELDS = (
    "home_spread",
    "away_spread",
    "home_spread_price",
    "away_spread_price",
    "home_moneyline",
    "away_moneyline",
    "total",
    "over_price",
    "under_price",
)
# Plausibility guards: for 2023 events ESPN's block fields carry prices where
# the line should be (``close.total.american = "-110"``), which would
# otherwise read as a total of -110.
MAX_SPREAD_MAGNITUDE = 50.0
MAX_TOTAL = 150.0
# Within this many points ESPN BET's close and nflverse's close count as the
# same number for the cross-check (different books close on different halves).
CROSS_CHECK_TOLERANCE = 0.5


class FetchError(RuntimeError):
    """A single ESPN request failed after every retry."""


# --------------------------------------------------------------------------
# Season arguments


def parse_seasons(tokens: Iterable[str]) -> list[int]:
    """``["2016-2025"]`` or ``["2024", "2025"]`` (or a mix) → sorted years."""
    seasons: set[int] = set()
    for token in tokens:
        for part in str(token).replace(",", " ").split():
            if "-" in part:
                start_text, end_text = part.split("-", 1)
                start, end = int(start_text), int(end_text)
                if end < start:
                    raise ValueError(f"season range {part!r} runs backwards")
                seasons.update(range(start, end + 1))
            else:
                seasons.add(int(part))
    if not seasons:
        raise ValueError("no seasons given")
    return sorted(seasons)


# --------------------------------------------------------------------------
# nflverse games file → data/nfl_lines_history.csv


def team_name(code: str) -> str:
    try:
        return NFLVERSE_TEAMS[code]
    except KeyError:
        raise ValueError(f"unknown nflverse team code {code!r}") from None


def _number(raw: str | None) -> float | None:
    if raw is None or str(raw).strip() == "":
        return None
    return float(raw)


def format_number(value: float | int | None) -> str:
    """``3.0`` → ``"3"``, ``-2.5`` → ``"-2.5"``, ``None`` → ``""``, no ``-0``."""
    if value is None:
        return ""
    number = float(value)
    if number == 0:
        return "0"
    if number.is_integer():
        return str(int(number))
    return str(number)


def _int_text(raw: str | None) -> str:
    number = _number(raw)
    return "" if number is None else str(int(number))


def home_spread_from_nflverse(spread_line: str | None) -> float | None:
    """nflverse ``spread_line`` (positive = home favored) → repo ``home_spread``
    (negative = home favored)."""
    number = _number(spread_line)
    if number is None:
        return None
    return -number if number != 0 else 0.0


def read_nflverse_csv(text: str) -> list[dict[str, str]]:
    return list(csv.DictReader(io.StringIO(text)))


def _lines_row(game: dict[str, str]) -> dict[str, str]:
    return {
        "season": str(int(game["season"])),
        "week": str(int(game["week"])),
        "gameday": game["gameday"],
        "weekday": game.get("weekday", ""),
        "gametime": game.get("gametime", ""),
        "espn_id": game.get("espn", ""),
        "away_team": team_name(game["away_team"]),
        "home_team": team_name(game["home_team"]),
        "away_score": _int_text(game.get("away_score")),
        "home_score": _int_text(game.get("home_score")),
        "home_spread": format_number(home_spread_from_nflverse(game.get("spread_line"))),
        "total": format_number(_number(game.get("total_line"))),
        "away_moneyline": _int_text(game.get("away_moneyline")),
        "home_moneyline": _int_text(game.get("home_moneyline")),
        "away_spread_price": _int_text(game.get("away_spread_odds")),
        "home_spread_price": _int_text(game.get("home_spread_odds")),
        "over_price": _int_text(game.get("over_odds")),
        "under_price": _int_text(game.get("under_odds")),
        "nflverse_spread_line": format_number(_number(game.get("spread_line"))),
    }


def _row_sort_key(row: dict[str, str]) -> tuple[int, int, str, str]:
    return (int(row["season"]), int(row["week"]), row["gameday"], row["home_team"])


def build_lines_rows(
    games: Iterable[dict[str, str]], seasons: Iterable[int]
) -> list[dict[str, str]]:
    """Regular-season games of ``seasons`` as CSV rows, sorted by season,
    week, gameday, home_team."""
    wanted = set(seasons)
    rows = [
        _lines_row(game)
        for game in games
        if game.get("game_type") == REGULAR_SEASON and int(game["season"]) in wanted
    ]
    rows.sort(key=_row_sort_key)
    return rows


def _atomic_write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    with open(tmp, "w", encoding="utf-8", newline="") as handle:
        handle.write(text)
    os.replace(tmp, path)


def lines_csv_text(rows: Iterable[dict[str, str]]) -> str:
    buffer = io.StringIO()
    writer = csv.DictWriter(buffer, fieldnames=LINES_COLUMNS, lineterminator="\n")
    writer.writeheader()
    for row in rows:
        writer.writerow({column: row.get(column, "") for column in LINES_COLUMNS})
    return buffer.getvalue()


def write_lines_csv(rows: Iterable[dict[str, str]], path: Path = LINES_CSV) -> None:
    _atomic_write_text(path, lines_csv_text(rows))


def read_lines_csv(path: Path = LINES_CSV) -> list[dict[str, str]]:
    with open(path, encoding="utf-8", newline="") as handle:
        return list(csv.DictReader(handle))


def season_coverage(rows: Iterable[dict[str, str]]) -> dict[int, dict[str, int]]:
    """Per season: games and how many carry a closing spread / total /
    moneyline pair."""
    coverage: dict[int, dict[str, int]] = {}
    for row in rows:
        stats = coverage.setdefault(
            int(row["season"]), {"games": 0, "spread": 0, "total": 0, "moneyline": 0}
        )
        stats["games"] += 1
        stats["spread"] += bool(row["home_spread"])
        stats["total"] += bool(row["total"])
        stats["moneyline"] += bool(row["home_moneyline"] and row["away_moneyline"])
    return coverage


def format_season_coverage(coverage: dict[int, dict[str, int]]) -> str:
    lines = ["season  games  spread  total  moneyline"]
    for season in sorted(coverage):
        stats = coverage[season]
        lines.append(
            f"{season:<7} {stats['games']:>5}  {stats['spread']:>6}  "
            f"{stats['total']:>5}  {stats['moneyline']:>9}"
        )
    return "\n".join(lines)


# --------------------------------------------------------------------------
# ESPN core odds → data/nfl_open_close.json


def parse_american(raw: object) -> int | None:
    """``"+130"`` / ``"-110"`` / ``"EVEN"`` → int, else None."""
    if raw is None:
        return None
    text = str(raw).replace("+", "").strip()
    if text == "":
        return None
    if text.lower() in ("even", "pk"):
        return 100
    try:
        return int(float(text))
    except ValueError:
        return None


def parse_line(raw: object) -> float | None:
    """``"-2.5"`` / ``"+3"`` / ``"45.5"`` / ``"PK"`` → float, else None."""
    if raw is None:
        return None
    text = str(raw).replace("+", "").strip()
    if text == "":
        return None
    if text.lower() in ("pk", "even", "pick"):
        return 0.0
    try:
        return float(text)
    except ValueError:
        return None


def _american_text(node: object) -> str | None:
    if not isinstance(node, dict):
        return None
    value = node.get("american")
    if value in (None, ""):
        value = node.get("alternateDisplayValue")
    return None if value in (None, "") else str(value)


def _plausible_spread(value: float | None) -> float | None:
    if value is None or abs(value) >= MAX_SPREAD_MAGNITUDE:
        return None
    return value


def _plausible_total(value: float | None) -> float | None:
    if value is None or value <= 0 or value >= MAX_TOTAL:
        return None
    return value


def _team_block(item: dict[str, Any], side: str, block: str) -> dict[str, Any]:
    team = item.get(side)
    if not isinstance(team, dict):
        return {}
    node = team.get(block)
    return node if isinstance(node, dict) else {}


def _total_block(item: dict[str, Any], block: str) -> dict[str, Any]:
    node = item.get(block)
    return node if isinstance(node, dict) else {}


def has_block(item: dict[str, Any], block: str) -> bool:
    return bool(
        _team_block(item, "homeTeamOdds", block)
        or _team_block(item, "awayTeamOdds", block)
        or _total_block(item, block)
    )


def empty_block() -> dict[str, float | int | None]:
    return {name: None for name in BLOCK_FIELDS}


def extract_block(item: dict[str, Any], block: str) -> dict[str, float | int | None]:
    """One of ``open`` / ``close`` / ``current`` in the repo's field names.

    Per team: ``pointSpread`` is the line (team-relative, so the home value is
    ``home_spread`` verbatim), ``spread`` is that line's price, ``moneyLine``
    the moneyline. The top-level block holds the total and its juice.
    """
    home = _team_block(item, "homeTeamOdds", block)
    away = _team_block(item, "awayTeamOdds", block)
    totals = _total_block(item, block)
    return {
        "home_spread": _plausible_spread(parse_line(_american_text(home.get("pointSpread")))),
        "away_spread": _plausible_spread(parse_line(_american_text(away.get("pointSpread")))),
        "home_spread_price": parse_american(_american_text(home.get("spread"))),
        "away_spread_price": parse_american(_american_text(away.get("spread"))),
        "home_moneyline": parse_american(_american_text(home.get("moneyLine"))),
        "away_moneyline": parse_american(_american_text(away.get("moneyLine"))),
        "total": _plausible_total(parse_line(_american_text(totals.get("total")))),
        "over_price": parse_american(_american_text(totals.get("over"))),
        "under_price": parse_american(_american_text(totals.get("under"))),
    }


def provider_name(item: dict[str, Any]) -> str | None:
    provider = item.get("provider")
    if not isinstance(provider, dict):
        return None
    name = provider.get("name")
    return str(name) if name else None


def is_live_provider(name: str | None) -> bool:
    """``ESPN Bet - Live Odds`` and kin: their blocks are in-game snapshots
    (an ``away_moneyline`` of -10000 and a total of 18.5 were observed for
    2024 wk2 PIT @ DEN), never a pregame open or close."""
    return bool(name) and "live" in str(name).lower()


def select_provider(items: list[dict[str, Any]]) -> tuple[str, dict[str, Any]] | None:
    """``ESPN BET`` when it has open and close; else the first provider that
    has both; else ``ESPN BET`` with whatever it has; else the first provider
    with any block. Live-odds providers are never candidates."""
    named = [
        (provider_name(item), item)
        for item in items
        if provider_name(item) and not is_live_provider(provider_name(item))
    ]
    complete = [
        (name, item)
        for name, item in named
        if has_block(item, "open") and has_block(item, "close")
    ]
    for name, item in complete:
        if name == PREFERRED_PROVIDER:
            return name, item
    if complete:
        return complete[0]
    for name, item in named:
        if name == PREFERRED_PROVIDER and any(has_block(item, block) for block in BLOCKS):
            return name, item
    for name, item in named:
        if any(has_block(item, block) for block in BLOCKS):
            return name, item
    return None


def top_level_home_spread(item: dict[str, Any]) -> float | None:
    """ESPN's top-level ``spread`` (home-relative current line), for the
    cross-check against the per-team ``current`` block."""
    return _plausible_spread(parse_line(item.get("spread")))


def extract_open_close(payload: dict[str, Any]) -> tuple[str | None, dict[str, dict[str, Any]]]:
    """Provider name and ``{"open", "close", "current"}`` blocks from one
    ESPN odds payload; a missing block is all ``None``."""
    items = payload.get("items") or []
    chosen = select_provider([item for item in items if isinstance(item, dict)])
    if chosen is None:
        return None, {block: empty_block() for block in BLOCKS}
    name, item = chosen
    return name, {
        block: extract_block(item, block) if has_block(item, block) else empty_block()
        for block in BLOCKS
    }


def build_entry(row: dict[str, str], payload: dict[str, Any], fetched_at: str) -> dict[str, Any]:
    provider, blocks = extract_open_close(payload)
    return {
        "season": int(row["season"]),
        "week": int(row["week"]),
        "away_team": row["away_team"],
        "home_team": row["home_team"],
        "provider": provider,
        "fetched_at": fetched_at,
        "open": blocks["open"],
        "close": blocks["close"],
        "current": blocks["current"],
    }


def load_open_close(path: Path = OPEN_CLOSE_JSON) -> dict[str, dict[str, Any]]:
    if not path.exists():
        return {}
    with open(path, encoding="utf-8") as handle:
        data = json.load(handle)
    return data if isinstance(data, dict) else {}


def open_close_text(entries: dict[str, dict[str, Any]]) -> str:
    return json.dumps(entries, indent=1, sort_keys=True) + "\n"


def write_open_close(entries: dict[str, dict[str, Any]], path: Path = OPEN_CLOSE_JSON) -> None:
    _atomic_write_text(path, open_close_text(entries))


def _retry_delay(attempt: int, response: httpx.Response | None) -> float:
    if response is not None:
        retry_after = response.headers.get("Retry-After")
        if retry_after and retry_after.isdigit():
            return float(retry_after)
    return float(2**attempt)


def fetch_odds_payload(
    client: httpx.Client,
    event_id: str,
    *,
    retries: int = MAX_RETRIES,
    sleep: Callable[[float], None] = time.sleep,
) -> dict[str, Any]:
    """GET one event's odds; exponential backoff on 429/5xx/timeouts."""
    url = ESPN_ODDS_URL.format(event_id=event_id)
    last_error = ""
    for attempt in range(retries + 1):
        response: httpx.Response | None = None
        try:
            response = client.get(url)
            if response.status_code == 200:
                return response.json()
            last_error = f"HTTP {response.status_code}"
            if response.status_code != 429 and response.status_code < 500:
                raise FetchError(f"{event_id}: {last_error}")
        except (httpx.TimeoutException, httpx.TransportError) as exc:
            last_error = f"{type(exc).__name__}: {exc}"
        except ValueError as exc:  # malformed JSON
            raise FetchError(f"{event_id}: bad JSON ({exc})") from exc
        if attempt < retries:
            sleep(_retry_delay(attempt, response))
    raise FetchError(f"{event_id}: {last_error} after {retries + 1} attempts")


@dataclass
class FetchResult:
    entries: dict[str, dict[str, Any]]
    fetched: int = 0
    skipped: int = 0
    no_provider: list[str] = field(default_factory=list)
    failed: list[tuple[str, str]] = field(default_factory=list)
    top_spread_agree: int = 0
    top_spread_disagree: list[tuple[str, float | None, float | None]] = field(
        default_factory=list
    )


def _note_top_spread(result: FetchResult, event_id: str, payload: dict[str, Any], entry: dict[str, Any]) -> None:
    chosen = select_provider([i for i in payload.get("items") or [] if isinstance(i, dict)])
    if chosen is None:
        return
    top = top_level_home_spread(chosen[1])
    current = entry["current"]["home_spread"]
    if top is None or current is None:
        return
    if abs(top - current) < 1e-9:
        result.top_spread_agree += 1
    else:
        result.top_spread_disagree.append((event_id, top, current))


def fetch_open_close(
    rows: Iterable[dict[str, str]],
    existing: dict[str, dict[str, Any]],
    *,
    fetch: Callable[[str], dict[str, Any]],
    refresh: bool = False,
    limit: int | None = None,
    pace: float = DEFAULT_PACE_SECONDS,
    checkpoint: Callable[[dict[str, dict[str, Any]]], None] | None = None,
    checkpoint_every: int = CHECKPOINT_EVERY,
    sleep: Callable[[float], None] = time.sleep,
    now: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
    log: Callable[[str], None] = print,
) -> FetchResult:
    """Merge ESPN odds for ``rows`` into ``existing``.

    Existing ids are kept untouched (and not fetched) unless ``refresh``;
    ``checkpoint`` receives the merged dict every ``checkpoint_every``
    fetches so an interrupted run resumes where it stopped. Failures are
    reported, never persisted, so the next run retries them.
    """
    result = FetchResult(entries=dict(existing))
    since_checkpoint = 0
    for row in rows:
        event_id = row.get("espn_id", "")
        if not event_id:
            continue
        if event_id in result.entries and not refresh:
            result.skipped += 1
            continue
        if limit is not None and result.fetched + len(result.failed) >= limit:
            break
        try:
            payload = fetch(event_id)
        except FetchError as exc:
            result.failed.append((event_id, str(exc)))
            log(f"  failed {event_id}: {exc}")
        else:
            fetched_at = now().replace(microsecond=0).isoformat().replace("+00:00", "Z")
            entry = build_entry(row, payload, fetched_at)
            result.entries[event_id] = entry
            result.fetched += 1
            since_checkpoint += 1
            if entry["provider"] is None:
                result.no_provider.append(event_id)
            _note_top_spread(result, event_id, payload, entry)
            log(
                f"  {row['season']} wk{row['week']:>2} {row['away_team']} @ {row['home_team']} "
                f"[{event_id}] {entry['provider'] or 'no provider'} "
                f"close {format_number(entry['close']['home_spread'])}/{format_number(entry['close']['total'])}"
            )
            if checkpoint is not None and since_checkpoint >= checkpoint_every:
                checkpoint(result.entries)
                since_checkpoint = 0
        if pace > 0:
            sleep(pace)
    if checkpoint is not None and since_checkpoint:
        checkpoint(result.entries)
    return result


# --------------------------------------------------------------------------
# Cross-check ESPN BET close against the nflverse close


def cross_check(
    rows: Iterable[dict[str, str]],
    entries: dict[str, dict[str, Any]],
    *,
    tolerance: float = CROSS_CHECK_TOLERANCE,
) -> dict[str, Any]:
    """How often ESPN's close agrees with nflverse's close for the same game.

    Per market: ``exact`` (same number), ``near`` (within ``tolerance``),
    ``far`` (listed), ``sign_flips`` (the spreads favor different teams,
    the failure mode a wrong convention would produce).
    """
    stats: dict[str, Any] = {
        "compared": 0,
        "spread": {"exact": 0, "near": 0, "far": [], "sign_flips": []},
        "total": {"exact": 0, "near": 0, "far": []},
    }
    for row in rows:
        entry = entries.get(row.get("espn_id", ""))
        if entry is None:
            continue
        close = entry.get("close") or {}
        espn_spread = close.get("home_spread")
        espn_total = close.get("total")
        our_spread = _number(row.get("home_spread"))
        our_total = _number(row.get("total"))
        if espn_spread is None or our_spread is None:
            continue
        stats["compared"] += 1
        label = f"{row['season']} wk{row['week']} {row['away_team']} @ {row['home_team']}"
        _bucket(stats["spread"], label, espn_spread, our_spread, tolerance)
        if espn_spread * our_spread < 0 and min(abs(espn_spread), abs(our_spread)) > 0:
            stats["spread"]["sign_flips"].append((label, espn_spread, our_spread))
        if espn_total is not None and our_total is not None:
            _bucket(stats["total"], label, espn_total, our_total, tolerance)
    return stats


def _bucket(market: dict[str, Any], label: str, espn: float, ours: float, tolerance: float) -> None:
    gap = abs(espn - ours)
    if gap < 1e-9:
        market["exact"] += 1
    elif gap <= tolerance + 1e-9:
        market["near"] += 1
    else:
        market["far"].append((label, espn, ours))


def format_cross_check(stats: dict[str, Any], *, show: int = 8) -> str:
    compared = stats["compared"]
    if not compared:
        return "cross-check: no games with both an ESPN close and an nflverse close"
    lines = [f"cross-check ESPN close vs nflverse close over {compared} games:"]
    for market in ("spread", "total"):
        m = stats[market]
        far = len(m["far"])
        counted = m["exact"] + m["near"] + far
        pct = lambda n: f"{100.0 * n / counted:.1f}%" if counted else "n/a"  # noqa: E731
        lines.append(
            f"  {market:<6} exact {m['exact']} ({pct(m['exact'])})  "
            f"within {CROSS_CHECK_TOLERANCE} {m['near']} ({pct(m['near'])})  "
            f"further {far} ({pct(far)})"
        )
        for label, espn, ours in m["far"][:show]:
            lines.append(f"    {label}: espn {format_number(espn)} vs nflverse {format_number(ours)}")
    flips = stats["spread"]["sign_flips"]
    lines.append(f"  spread sign flips (different favorite): {len(flips)}")
    for label, espn, ours in flips[:show]:
        lines.append(f"    {label}: espn {format_number(espn)} vs nflverse {format_number(ours)}")
    return "\n".join(lines)


# --------------------------------------------------------------------------
# CLI


def download_nflverse_games(client: httpx.Client) -> str:
    response = client.get(NFLVERSE_GAMES_URL)
    response.raise_for_status()
    return response.text


def _http_client(timeout: float = REQUEST_TIMEOUT_SECONDS) -> httpx.Client:
    return httpx.Client(
        timeout=timeout,
        headers={"User-Agent": USER_AGENT, "Accept": "application/json, text/csv;q=0.9, */*;q=0.5"},
        follow_redirects=True,
    )


def _epilog() -> str:
    return (
        "Appending a season later: re-run with\n"
        "  python scripts/fetch_nfl_lines_history.py --seasons 1999-2026 "
        "--espn-seasons 2024 2025 2026\n"
        "The CSV is rebuilt in full; the JSON keeps every id it already has "
        "and fetches only the new ones (use --refresh to re-fetch everything, "
        "--espn-limit N for a smoke run, --skip-espn to rebuild only the CSV)."
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Pull historical NFL lines: nflverse regular-season closes to "
            "data/nfl_lines_history.csv and ESPN BET open/close/current to "
            "data/nfl_open_close.json."
        ),
        epilog=_epilog(),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--seasons",
        nargs="+",
        default=list(DEFAULT_SEASONS),
        help="seasons for the CSV, as a range '1999-2025' or a list (default: 1999-2025)",
    )
    parser.add_argument(
        "--espn-seasons",
        nargs="+",
        default=list(DEFAULT_ESPN_SEASONS),
        help="seasons whose games get ESPN open/close (default: 2024 2025)",
    )
    parser.add_argument(
        "--pace",
        type=float,
        default=DEFAULT_PACE_SECONDS,
        help=f"seconds to wait between ESPN calls (default {DEFAULT_PACE_SECONDS})",
    )
    parser.add_argument("--espn-limit", type=int, default=None, help="fetch at most N new events (smoke runs)")
    parser.add_argument("--skip-espn", action="store_true", help="rebuild only the CSV")
    parser.add_argument("--refresh", action="store_true", help="re-fetch ids already in the JSON")
    parser.add_argument(
        "--games-csv",
        type=Path,
        default=None,
        help="read a local copy of the nflverse games.csv instead of downloading it",
    )
    parser.add_argument("--lines-csv", type=Path, default=LINES_CSV, help=f"output CSV (default {LINES_CSV})")
    parser.add_argument(
        "--open-close-json", type=Path, default=OPEN_CLOSE_JSON, help=f"output JSON (default {OPEN_CLOSE_JSON})"
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    seasons = parse_seasons(args.seasons)
    espn_seasons = parse_seasons(args.espn_seasons)

    with _http_client() as client:
        if args.games_csv is not None:
            games_text = args.games_csv.read_text(encoding="utf-8")
        else:
            print(f"downloading {NFLVERSE_GAMES_URL}")
            games_text = download_nflverse_games(client)
        rows = build_lines_rows(read_nflverse_csv(games_text), seasons)
        write_lines_csv(rows, args.lines_csv)
        print(f"wrote {len(rows)} regular-season games to {args.lines_csv}")
        print(format_season_coverage(season_coverage(rows)))

        if args.skip_espn:
            return 0

        existing = load_open_close(args.open_close_json)
        targets = [row for row in rows if int(row["season"]) in set(espn_seasons) and row["espn_id"]]
        pending = [row for row in targets if args.refresh or row["espn_id"] not in existing]
        print(
            f"ESPN odds: {len(targets)} events in seasons {espn_seasons}, "
            f"{len(existing)} already stored, {len(pending)} to fetch"
            + (f" (limit {args.espn_limit})" if args.espn_limit is not None else "")
        )
        result = fetch_open_close(
            targets,
            existing,
            fetch=lambda event_id: fetch_odds_payload(client, event_id),
            refresh=args.refresh,
            limit=args.espn_limit,
            pace=args.pace,
            checkpoint=lambda entries: write_open_close(entries, args.open_close_json),
        )

    print(
        f"fetched {result.fetched}, skipped {result.skipped} already stored, "
        f"{len(result.no_provider)} without a usable provider, {len(result.failed)} failed; "
        f"{len(result.entries)} entries in {args.open_close_json}"
    )
    if result.no_provider:
        print("  no provider: " + " ".join(result.no_provider))
    for event_id, error in result.failed:
        print(f"  failed {event_id}: {error}")
    print(
        f"top-level spread vs per-team current: {result.top_spread_agree} agree, "
        f"{len(result.top_spread_disagree)} disagree"
    )
    for event_id, top, current in result.top_spread_disagree[:8]:
        print(f"    {event_id}: top-level {format_number(top)} vs current {format_number(current)}")
    print(format_cross_check(cross_check(rows, result.entries)))
    return 1 if result.failed else 0


if __name__ == "__main__":
    sys.exit(main())
