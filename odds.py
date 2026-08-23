"""
odds.py — Production odds lookup for the pick tracker.

Entry point:
    result = await fetch_odds(sport, game_date, pick)

Returns an OddsResult. Always call validate_for_display() before using
odds in any Telegram message — it catches sanity failures and logs them.

Failure taxonomy (result.match_type):
  Structural misses (expected, silent):
    sport_unsupported, team_total_unavailable, prop_stat_unsupported,
    no_h2h_*_data, missing_line_or_direction, no_line_in_pick

  Unexpected misses (worth flagging):
    no_game          — event not found in Odds API for this sport+date
    prop_not_found   — player not in prop outcomes for event
    alt_line_gap_*   — closest line too far away (>MAX_LINE_GAP pts)
    api_error        — Odds API returned an error

  Hits:
    exact            — exact match on main-line market
    exact_alt        — exact match on alternate-line market
    proximity_*pts   — closest line within MAX_LINE_GAP, odds adjusted
"""

from __future__ import annotations

import json
import os
import re
import sqlite3
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo
from pathlib import Path

import httpx
from dotenv import load_dotenv

from common import is_regulation_ml
from scores import _team_matches, fetch_espn, espn_bookmakers_for_teams, ESPN_LEAGUES

ROOT = Path(__file__).resolve().parent
DB_PATH = str(ROOT / "picks.db")

load_dotenv(ROOT / ".env")
load_dotenv(ROOT / ".env.local", override=True)

ODDS_API_KEY  = os.getenv("ODDS_API_KEY", "")
ODDS_API_BASE = "https://api.the-odds-api.com/v4"

# ── Sport / market config ─────────────────────────────────────────────────────

SPORT_KEYS: dict[str, str] = {
    "NBA":   "basketball_nba",
    "WNBA":  "basketball_wnba",
    "NCAAB": "basketball_ncaab",
    "NFL":   "americanfootball_nfl",
    "NCAAF": "americanfootball_ncaaf",
    "MLB":   "baseball_mlb",
    "NHL":   "icehockey_nhl",
    "UFC":   "mma_mixed_martial_arts",
    "UFL":   "americanfootball_ufl",
    "CFL":   "americanfootball_cfl",
    "KBO":    "baseball_kbo",
    "Soccer": "soccer_fifa_world_cup",
    "Lacrosse": "lacrosse_pll",
}

# Extra Odds API sport keys carrying the SAME league in a different phase of the
# season. The Odds API files NFL preseason under its own key, so a preseason
# pick finds no event in americanfootball_nfl — and _find_event_id does not fail
# closed: it scores every event that shares a team name and returns the nearest
# one, which in August is a REGULAR-season game five weeks out. A Panthers /
# Cardinals preseason under 35.5 was priced off Bears @ Panthers in Week 1
# (total 46.5) at +320 instead of the real ~-150.
#
# Event lists from every candidate key are merged into one list and scored
# together, so the exact game wins on team-match count and proximity. Each event
# carries its own "sport_key", which is what the odds call must use.
#
# The other three are listed for the same reason before their season comes round
# (NBA/NHL preseason in late September, MLB spring training in February): out of
# season the endpoint returns HTTP 200 with an empty list, and /events costs no
# API quota at all (x-requests-last: 0), so an unused key is one cached HTTP
# round-trip per 30 minutes and nothing else.
_EXTRA_SPORT_KEYS: dict[str, list[str]] = {
    "NFL": ["americanfootball_nfl_preseason"],
    "NBA": ["basketball_nba_preseason"],
    "NHL": ["icehockey_nhl_preseason"],
    "MLB": ["baseball_mlb_preseason"],
}


def _sport_key_candidates(sport: str) -> list[str]:
    """Every Odds API sport key that may carry this sport's games, primary first."""
    primary = SPORT_KEYS.get(sport)
    if not primary:
        return []
    return [primary] + _EXTRA_SPORT_KEYS.get(sport, [])


def _event_sport_key(event_list: list[dict], event_id: str, default: str) -> str:
    """The sport key the matched event actually came from (lists may be merged)."""
    event = next((e for e in event_list if e.get("id") == event_id), None)
    return (event or {}).get("sport_key") or default


# Outright tournament-winner sports. The Odds API prices "to lift the trophy" /
# "to win the tournament" in a SEPARATE '{sport}_winner' sport (2-way outcomes,
# includes ET/penalties) rather than on the game event. Maps a game sport_key to
# its winner sport_key; used as a fallback for final-round / outright moneylines.
_WINNER_SPORT_KEYS: dict[str, str] = {
    "soccer_fifa_world_cup": "soccer_fifa_world_cup_winner",
}

PROP_STAT_MARKETS: dict[str, dict[str, str]] = {
    "MLB": {
        "HITS":       "batter_hits",
        "HR":         "batter_home_runs",
        "RBI":        "batter_rbis",
        "K":          "pitcher_strikeouts",
        "SO":         "pitcher_strikeouts",
        "STRIKEOUTS": "pitcher_strikeouts",
    },
    "NBA": {
        "PTS":         "player_points",
        "REB":         "player_rebounds",
        "AST":         "player_assists",
        "PTS+REB":     "player_points_rebounds",
        "PTS+AST":     "player_points_assists",
        "PTS+REB+AST": "player_points_rebounds_assists",
        "3PM":         "player_threes",
        "BLK":         "player_blocks",
        "STL":         "player_steals",
    },
    "WNBA": {
        "PTS":         "player_points",
        "REB":         "player_rebounds",
        "AST":         "player_assists",
        "PTS+REB":     "player_points_rebounds",
        "PTS+AST":     "player_points_assists",
        "PTS+REB+AST": "player_points_rebounds_assists",
        "3PM":         "player_threes",
        "BLK":         "player_blocks",
        "STL":         "player_steals",
    },
    "NHL": {
        "GOALS":  "player_goal_scorer_anytime",
        "SHOTS":  "player_shots_on_goal",
        "SAVES":  "goalie_saves",
    },
    "NFL": {
        "PASSING_YDS":   "player_pass_yds",
        "RUSHING_YDS":   "player_rush_yds",
        "RECEIVING_YDS": "player_reception_yds",
        "RECEPTIONS":    "player_receptions",
        "TDS":           "player_anytime_td",
    },
}

MARKETS_FULL = (
    "h2h,h2h_3_way,spreads,totals,"
    "alternate_spreads,alternate_totals,"
    "team_totals,alternate_team_totals,"
    "h2h_h1,spreads_h1,totals_h1,"
    "h2h_h2,spreads_h2,totals_h2,"
    "h2h_q1,spreads_q1,totals_q1,"
    "h2h_p1,spreads_p1,totals_p1,alternate_totals_p1,"
    "h2h_p2,spreads_p2,totals_p2,alternate_totals_p2,"
    "h2h_p3,spreads_p3,totals_p3,alternate_totals_p3,"
    "h2h_1st_5_innings,spreads_1st_5_innings,totals_1st_5_innings,"
    "alternate_spreads_1st_5_innings,alternate_totals_1st_5_innings"
)

MARKETS_BY_TYPE: dict[str, str] = {
    "moneyline":    "h2h,h2h_3_way,h2h_h1,h2h_h2,h2h_q1,h2h_p1,h2h_p2,h2h_p3",
    "to_advance":   "to_qualify",
    "spread":       "spreads,alternate_spreads,spreads_h1,spreads_h2,spreads_q1,spreads_p1,spreads_p2,spreads_p3",
    "total":        "totals,alternate_totals,totals_h1,totals_h2,totals_q1,totals_p1,totals_p2,totals_p3,alternate_totals_p1,alternate_totals_p2,alternate_totals_p3",
    "team_total":   "team_totals,alternate_team_totals,team_totals_h1,alternate_team_totals_h1,team_totals_h2,alternate_team_totals_h2",
}

MAX_LINE_GAP = 5

# How long after kickoff an event still counts as "current" when picking which
# event a pick refers to (see _find_event_id). Longer than any single event we
# price — a UFC card runs about six hours — and shorter than the gap to the
# next same-matchup game.
_STARTED_GRACE_H = 8

HALF_POINT_COST: dict[str, float] = {
    "NFL":   0.022,
    "NCAAF": 0.020,
    "NBA":   0.022,
    "WNBA":  0.022,
    "NCAAB": 0.020,
    "MLB":   0.020,
    "NHL":   0.020,
    "UFC":   0.000,
    "UFL":   0.022,
    "CFL":   0.022,
    "Soccer": 0.020,
}

_PERIOD_RE = re.compile(
    r'\b(1h|2h|1st half|2nd half|first half|second half|'
    r'1q|2q|3q|4q|1st quarter|2nd quarter|3rd quarter|4th quarter|'
    r'1p|2p|3p|1st period|2nd period|3rd period)\b',
    re.IGNORECASE,
)

# "To advance" / "to qualify" — knockout-stage soccer bets. The Odds API h2h
# market is the 90-min line (draw is a separate outcome), not the advancement
# price, so a moneyline described as an advancement bet must route to the
# to_qualify market to avoid the wrong price (e.g. England +172 on the 90-min
# line vs -126 to advance). We match the canonical "advance"/"qualify" stems the
# parser emits — from the message text, or from the bet slip image when the text
# is slang like "it's coming home" (claude_parse's image path resolves the slang
# to explicit wording, e.g. "England to advance (Game Winner)"). Matching the
# vocabulary rather than a hand-maintained slang list keeps this robust. Only
# consulted for moneyline bet_type, so "advance"/"qualify" in a description is
# reliably an advancement bet.
_ADVANCE_RE = re.compile(r'\b(advanc\w*|qualif\w*)\b|\bto\s+the\s+final\b', re.IGNORECASE)

# "To lift the trophy" / "to win the tournament" — an OUTRIGHT (futures) winner
# bet. For the FINAL specifically, the knockout to_qualify market is absent and
# the game's h2h is the 90-min 3-way line (wrong price, e.g. Spain +125 vs the
# -158 winner price), so these route to the '{sport}_winner' outright market. The
# parser often rewrites "lift the trophy" into an "advance as Game Winner" phrase
# (caught by _ADVANCE_RE), but match the trophy vocabulary too so the raw wording
# is handled if it survives the parse.
_OUTRIGHT_RE = re.compile(
    r'\blift(?:ing|s)?\s+the\s+(?:trophy|cup|title)\b'
    r'|\bwin(?:s|ning)?\s+(?:the\s+)?(?:world\s+cup|tournament|title|championship|trophy)\b'
    r'|\bto\s+be\s+champions?\b|\btournament\s+winner\b|\bwin\s+it\s+all\b',
    re.IGNORECASE,
)


def _is_advance_or_outright(desc: str) -> bool:
    """A knockout-advancement or outright-winner soccer moneyline. Both must
    avoid the 90-min h2h market; both resolve on the full result (ET/penalties)."""
    return bool(_ADVANCE_RE.search(desc) or _OUTRIGHT_RE.search(desc))

_PERIOD_SUFFIX: dict[str, str] = {
    "1h": "_h1", "2h": "_h2",
    "1q": "_q1", "2q": "_q2", "3q": "_q3", "4q": "_q4",
    "1p": "_p1", "2p": "_p2", "3p": "_p3",
}

# MLB uses inning-based market keys instead of _h1/_h2.
_MLB_PERIOD_SUFFIX: dict[str, str] = {
    "1h": "_1st_5_innings",
}


def _get_period_suffix(period: str, sport: str = "") -> str:
    """Return the Odds API market suffix for a period, sport-aware for MLB innings."""
    if sport == "MLB" and period in _MLB_PERIOD_SUFFIX:
        return _MLB_PERIOD_SUFFIX[period]
    return _PERIOD_SUFFIX.get(period, "")

# ── OddsResult ────────────────────────────────────────────────────────────────

# Match types that are known structural gaps — expected and not worth flagging.
_STRUCTURAL_MISS_TYPES = {
    "team_total_unavailable",
    "player_prop_unavailable",
    "no_h2h_data", "no_h2h_3_way_data",
    "no_h2h_h1_data", "no_h2h_h2_data", "no_h2h_q1_data",
    "no_total_data", "no_spread_data",
    "missing_line_or_direction",
    "no_line_in_pick",
    "game_in_progress",
    "dry_run",
}


@dataclass
class OddsResult:
    match_type:  str
    odds:        int | None   = None   # American odds (adjusted for proximity matches)
    bookmaker:   str | None   = None
    api_line:    float | None = None
    pick_line:   float | None = None
    # Populated for in-progress games when historical pre-game odds are also available:
    pregame_odds:       int | None = None
    pregame_bookmaker:  str | None = None
    pregame_match_type: str | None = None
    game_date:          str | None = None   # YYYY-MM-DD from commence_time
    betonline_sides:    dict | None = None  # {pick_odds, opp_odds, pick_label, opp_label}
    commence_time:      str | None = None   # raw ISO from the /events match; bounds miss retries

    @property
    def found(self) -> bool:
        return self.odds is not None

    @property
    def is_structural_miss(self) -> bool:
        """Known, expected gaps — sport unsupported, team total, etc. Don't flag."""
        if self.match_type in _STRUCTURAL_MISS_TYPES:
            return True
        return (
            self.match_type.startswith("sport_unsupported")
            or self.match_type.startswith("prop_stat_unsupported")
            or self.match_type.startswith("unsupported_bet_type")
        )

    @property
    def is_unexpected_miss(self) -> bool:
        """Game found but odds couldn't be matched for a non-structural reason. Flag these."""
        return not self.found and not self.is_structural_miss

    def validate_for_display(self) -> tuple[int | None, str | None]:
        """
        Sanity-check odds before including in a Telegram message.

        Returns (odds_to_show, warning_string_or_None).
        Returns (None, warning) if odds fail a hard check and should be suppressed.
        Returns (odds, warning) for soft warnings where odds can still be shown.
        Returns (odds, None) if everything looks clean.
        """
        if self.odds is None:
            return None, None

        odds = self.odds

        # Hard checks — suppress the value
        if -99 <= odds <= 99:
            return None, f"invalid American odds {odds} (must be ≤-100 or ≥+100)"

        if odds < -10000 or odds > 10000:
            return None, f"odds out of sane range: {odds}"

        # Soft checks — show but warn
        if odds > 3000:
            return odds, f"unusually long odds +{odds} from {self.bookmaker}"

        if odds < -3000:
            return odds, f"unusually short odds {odds} from {self.bookmaker}"

        if self.match_type.startswith("proximity_") and self.api_line is not None and self.pick_line is not None:
            gap = abs((self.api_line or 0) - (self.pick_line or 0))
            if gap >= MAX_LINE_GAP:
                return odds, f"odds from {gap:.1f}pt adjacent line ({self.api_line} vs pick {self.pick_line})"

        return odds, None

    def format(self) -> str | None:
        """Format as '+110' / '-150'. Returns None if no odds."""
        if self.odds is None:
            return None
        return f"+{self.odds}" if self.odds > 0 else str(self.odds)


# ── Retryable misses ──────────────────────────────────────────────────────────
# A pick is priced once at first encounter and the result stored for good — but
# Bovada's free period markets (MLB F5, CFL quarters/halves) only appear on the
# coupon in a pregame window the first attempt can post hours ahead of, and an
# event posted days early may not be listed anywhere yet. The tracker re-tries
# these misses with FREE sources only (fetch_odds_current(free_only=True)) every
# ≥30 min until game start. Priced results, live_/pregame_ results and
# structural misses stay final; the bound comes from commence_time when the
# /events match provided one, else the eastern game date (a post-start attempt
# then resolves through the started branch and stores a final verdict).

_RETRYABLE_MISS_RE = re.compile(r"no_\w+_data")
_ODDS_RETRY_SPACING_S = 30 * 60
_ODDS_RETRY_MAX = 300           # safety valve; the 30-min spacing is the real limiter


def should_retry_odds(stored: dict, now: datetime | None = None) -> bool:
    """True if a stored odds_by_pick result is a miss worth re-fetching now."""
    if not isinstance(stored, dict) or stored.get("odds") is not None:
        return False
    mt = stored.get("match_type") or ""
    if not (mt == "no_game" or _RETRYABLE_MISS_RE.fullmatch(mt)):
        return False
    if stored.get("_retry_n", 0) >= _ODDS_RETRY_MAX:
        return False
    now = now or datetime.now(timezone.utc)
    if now.timestamp() - (stored.get("_retry_ts") or 0) < _ODDS_RETRY_SPACING_S:
        return False
    ct = stored.get("commence_time")
    if ct:
        try:
            return now < datetime.fromisoformat(str(ct).replace("Z", "+00:00"))
        except ValueError:
            pass
    gd = stored.get("game_date")
    if gd:
        try:
            return now.astimezone(_ET).date().isoformat() <= str(gd)
        except (ValueError, TypeError):
            return False
    return False


# ── SQLite cache ──────────────────────────────────────────────────────────────

def _init_db(conn: sqlite3.Connection) -> None:
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS odds_cache (
            sport       TEXT NOT NULL,
            game_date   TEXT NOT NULL,
            event_id    TEXT NOT NULL,
            markets     TEXT NOT NULL,
            bookmakers  TEXT NOT NULL,
            fetched_at  TEXT NOT NULL,
            PRIMARY KEY (event_id, game_date, markets)
        );
        CREATE TABLE IF NOT EXISTS events_cache (
            sport_key  TEXT NOT NULL,
            game_date  TEXT NOT NULL,
            events     TEXT NOT NULL,
            fetched_at TEXT NOT NULL,
            PRIMARY KEY (sport_key, game_date)
        );
    """)
    conn.commit()


def _evict_old(conn: sqlite3.Connection) -> None:
    # fetched_at is stored as ISO 8601 ('2026-04-09T01:14:58.447536+00:00') while
    # datetime('now', ...) returns 'YYYY-MM-DD HH:MM:SS'. A raw string compare puts
    # same-day ISO rows *after* the datetime() value because 'T' (0x54) > ' ' (0x20),
    # so TTL eviction silently fails within a UTC day. Wrap both sides in datetime()
    # to force a normalized comparison.
    conn.execute("DELETE FROM odds_cache   WHERE game_date != 'current' AND game_date != 'live' AND game_date < date('now', '-60 days')")
    conn.execute("DELETE FROM odds_cache   WHERE game_date  = 'current' AND datetime(fetched_at) < datetime('now', '-2 days')")
    conn.execute("DELETE FROM odds_cache   WHERE game_date  = 'live'    AND datetime(fetched_at) < datetime('now', '-5 minutes')")
    conn.execute("DELETE FROM events_cache WHERE game_date != 'current' AND game_date < date('now', '-60 days')")
    conn.execute("DELETE FROM events_cache WHERE game_date  = 'current' AND datetime(fetched_at) < datetime('now', '-30 minutes')")
    conn.commit()


def _get_events(conn: sqlite3.Connection, sport_key: str, game_date: str) -> list[dict] | None:
    row = conn.execute(
        "SELECT events FROM events_cache WHERE sport_key = ? AND game_date = ?",
        (sport_key, game_date),
    ).fetchone()
    return json.loads(row[0]) if row else None


def _save_events(conn: sqlite3.Connection, sport_key: str, game_date: str, events: list[dict]) -> None:
    conn.execute(
        "INSERT OR REPLACE INTO events_cache (sport_key, game_date, events, fetched_at) VALUES (?,?,?,?)",
        (sport_key, game_date, json.dumps(events), datetime.now(timezone.utc).isoformat()),
    )
    conn.commit()


def _get_bookmakers(conn: sqlite3.Connection, event_id: str, game_date: str, markets: str) -> list[dict] | None:
    row = conn.execute(
        "SELECT bookmakers FROM odds_cache WHERE event_id = ? AND game_date = ? AND markets = ?",
        (event_id, game_date, markets),
    ).fetchone()
    return json.loads(row[0]) if row else None


def _save_bookmakers(
    conn: sqlite3.Connection, sport: str, event_id: str, game_date: str, markets: str, bookmakers: list[dict]
) -> None:
    conn.execute(
        "INSERT OR REPLACE INTO odds_cache (sport, game_date, event_id, markets, bookmakers, fetched_at) VALUES (?,?,?,?,?,?)",
        (sport, game_date, event_id, markets, json.dumps(bookmakers), datetime.now(timezone.utc).isoformat()),
    )
    conn.commit()


# ── Odds API ──────────────────────────────────────────────────────────────────

_quota_remaining: str | None = None
_quota_used: int = 0
_QUOTA_EXHAUSTED = False

# ── Quota cost control ────────────────────────────────────────────────────────
# Cost is [unique markets RETURNED] x [regions SPECIFIED], x10 on the historical
# endpoints. Two independent multipliers, both measured against the live cache
# before being cut (2026-08-09, after the month's 20,000 credits ran out):
#
#  * regions — us2 (ballybet, betanysports, betparx, espnbet, fliff, hardrockbet*,
#    rebet) is the ONLY source for 2.1% of priced outcomes and beats every us book
#    on 3.0% of them, by a median 0.82 points of implied probability. Dropping it
#    is a straight 50% cut for that 2.1%. Set ODDS_API_REGIONS=us,us2 to restore.
#  * markets — the lists above ask for every period variant of a bet type, and we
#    are billed for each one that happens to exist (2.59 came back per request when
#    the pick reads 1-2). Narrowing to the pick's OWN period stops buying the rest.
#
# What is deliberately NOT cut: the `alternate_*` market. 22 of 208 priced picks
# (10.6%) matched via exact_alt — five times the loss from dropping us2 — so
# alternates stay for live pricing and are dropped only in economy mode, where a
# miss falls back to the -110 default and nobody reads the line.
REGIONS = os.getenv("ODDS_API_REGIONS", "us")

_ECONOMY = False


def set_economy(on: bool = True) -> None:
    """Backfill mode: drop alternate lines too (one market per pick)."""
    global _ECONOMY
    _ECONOMY = on


def economy() -> bool:
    return _ECONOMY


def _cache_markets(markets: str) -> str:
    """Cache key for a markets string, namespaced by the regions that produced it.

    A response fetched with fewer books must never be served later to a caller
    expecting full coverage. Rows written before this existed were all us,us2, so
    that value keeps the bare key and stays readable.
    """
    return markets if REGIONS == "us,us2" else f"{markets}@{REGIONS}"


async def _api_get(http: httpx.AsyncClient, url: str, params: dict) -> dict | list | None:
    global _quota_remaining, _quota_used
    try:
        r = await http.get(url, params=params)
        r.raise_for_status()
        # `x-requests-last` is the exact cost of THIS request, straight from the
        # vendor. The previous version differenced `x-requests-remaining` across
        # calls, which has no previous value on the first request of a process —
        # so every run's first (and usually only) paid call was never counted.
        # That is why a month that really spent 20,000 credits logged ~300, and
        # the quota hit zero with no warning.
        last = r.headers.get("x-requests-last", "")
        if last.strip().lstrip("-").isdigit():
            _quota_used += int(last)
        _quota_remaining = r.headers.get("x-requests-remaining", _quota_remaining)
        return r.json()
    except httpx.HTTPStatusError as exc:
        # Quota exhaustion has to be distinguishable from a genuine miss. It used
        # to fall through as a plain failure and the caller recorded no_game —
        # the same value a missing event produces — so an outage looked exactly
        # like "this pick has no market" and nothing downstream could tell.
        body = exc.response.text or ""
        if exc.response.status_code == 401 and "OUT_OF_USAGE_CREDIT" in body:
            global _QUOTA_EXHAUSTED
            if not _QUOTA_EXHAUSTED:
                print("[odds] quota exhausted — falling back to free sources for this run")
            _QUOTA_EXHAUSTED = True
        print(f"[odds] API {exc.response.status_code} {url.split('/')[-1]}: {body[:120]}")
        return None
    except Exception as exc:
        print(f"[odds] API error {url}: {exc}")
        return None


async def _fetch_event_list(sport_key: str, date: str, conn: sqlite3.Connection) -> list[dict]:
    cached = _get_events(conn, sport_key, date)
    if cached is not None:
        return cached
    if not ODDS_API_KEY:
        return []
    async with httpx.AsyncClient(timeout=20) as http:
        data = await _api_get(
            http,
            f"{ODDS_API_BASE}/historical/sports/{sport_key}/events",
            {"apiKey": ODDS_API_KEY, "date": f"{date}T18:00:00Z"},
        )
    events: list[dict] = (data or {}).get("data", []) if isinstance(data, dict) else []
    _save_events(conn, sport_key, date, events)
    return events


def _snapshot_time(date: str) -> datetime | None:
    """The instant the historical event list was snapshotted, for _find_event_id."""
    try:
        return datetime.fromisoformat(f"{date}T18:00:00+00:00")
    except ValueError:
        return None


async def _fetch_event_list_all(sport: str, date: str, conn: sqlite3.Connection) -> list[dict]:
    """Historical event lists for every candidate sport key, merged (see _EXTRA_SPORT_KEYS)."""
    events: list[dict] = []
    for key in _sport_key_candidates(sport):
        events.extend(await _fetch_event_list(key, date, conn))
    return events


async def _fetch_bookmakers(
    sport_key: str, event_id: str, date: str, markets: str, conn: sqlite3.Connection
) -> list[dict]:
    cached = _get_bookmakers(conn, event_id, date, _cache_markets(markets))
    if cached is not None:
        return cached
    if not ODDS_API_KEY:
        return []
    async with httpx.AsyncClient(timeout=20) as http:
        data = await _api_get(
            http,
            f"{ODDS_API_BASE}/historical/sports/{sport_key}/events/{event_id}/odds",
            {"apiKey": ODDS_API_KEY, "regions": REGIONS, "markets": markets,
             "date": f"{date}T18:00:00Z", "oddsFormat": "american"},
        )
    bookmakers: list[dict] = []
    if isinstance(data, dict):
        bookmakers = data.get("data", {}).get("bookmakers", []) if "data" in data else data.get("bookmakers", [])
    _save_bookmakers(conn, sport_key, event_id, date, _cache_markets(markets), bookmakers)
    return bookmakers


# ── Matching helpers ──────────────────────────────────────────────────────────

def _american_to_prob(odds: int) -> float:
    if odds > 0:
        return 100 / (odds + 100)
    return abs(odds) / (abs(odds) + 100)


def _prob_to_american(prob: float) -> int:
    prob = max(0.01, min(0.99, prob))
    if prob >= 0.5:
        return round(-(prob / (1 - prob)) * 100)
    return round((1 - prob) / prob * 100)


def _adjust_for_gap(sport: str, base_odds: int, pick_line: float, api_line: float, gap: float) -> int:
    cost = HALF_POINT_COST.get(sport, 0.022)
    n_half_pts = gap / 0.5
    prob = _american_to_prob(base_odds)
    if pick_line > api_line:
        adjusted = prob + n_half_pts * cost
    else:
        adjusted = prob - n_half_pts * cost
    return _prob_to_american(adjusted)


def _pick_best(candidates: list[tuple[int, str]]) -> tuple[int | None, str | None]:
    if not candidates:
        return None, None
    return max(candidates, key=lambda x: x[0])


def _collect_outcomes(
    bookmakers: list[dict],
    market_key: str,
    name_filter: str | None = None,
    line_filter: float | None = None,
) -> list[tuple[float | None, int, str]]:
    results: list[tuple[float | None, int, str]] = []
    for bk in bookmakers:
        bk_key = bk.get("key", "")
        for mkt in bk.get("markets", []):
            if mkt.get("key") != market_key:
                continue
            for outcome in mkt.get("outcomes", []):
                name  = outcome.get("name", "")
                price = outcome.get("price")
                pt    = outcome.get("point")
                if name_filter and not _team_matches(name_filter.lower(), name.lower()):
                    continue
                if line_filter is not None and pt is not None:
                    if abs(float(pt) - line_filter) > 0.01:
                        continue
                if price is not None:
                    results.append((float(pt) if pt is not None else None, int(price), bk_key))
    return results


_BOOK_PRIORITY = ["betonlineag", "bovada", "mybookieag"]
_BOOK_DISPLAY = {"betonlineag": "BetOnline", "bovada": "Bovada", "mybookieag": "MyBookie"}


def _betonline_both_sides(
    bookmakers: list[dict],
    market_key: str,
    pick_team: str | None = None,
    pick_direction: str | None = None,
    pick_line: float | None = None,
) -> dict | None:
    """Extract offshore book odds for both sides of a market.

    Tries BetOnline first, then Bovada, then MyBookie.
    Returns {pick_odds, opp_odds, pick_label, opp_label, book} or None.
    """
    bk_map = {bk.get("key"): bk for bk in bookmakers}
    target = None
    for key in _BOOK_PRIORITY:
        if key in bk_map:
            target = bk_map[key]
            break
    if not target:
        return None
    book_name = _BOOK_DISPLAY.get(target.get("key", ""), target.get("key", ""))

    for mkt in target.get("markets", []):
        if mkt.get("key") != market_key:
            continue
        outcomes = mkt.get("outcomes", [])
        if len(outcomes) < 2:
            continue

        # For totals: match by direction (Over/Under)
        if pick_direction:
            pick_name = "Over" if pick_direction == "over" else "Under"
            opp_name = "Under" if pick_direction == "over" else "Over"
            pick_o = next((o for o in outcomes if o.get("name") == pick_name
                           and (pick_line is None or o.get("point") is None
                                or abs(float(o["point"]) - pick_line) < 0.01)), None)
            opp_o = next((o for o in outcomes if o.get("name") == opp_name
                          and (pick_line is None or o.get("point") is None
                               or abs(float(o["point"]) - pick_line) < 0.01)), None)
            if pick_o and opp_o and pick_o.get("price") and opp_o.get("price"):
                return {"pick_odds": int(pick_o["price"]), "opp_odds": int(opp_o["price"]),
                        "pick_label": pick_name, "opp_label": opp_name, "book": book_name}
            continue

        # For ML/spread: match by team name
        if not pick_team:
            continue
        pick_o = opp_o = None
        for o in outcomes:
            name = o.get("name", "")
            price = o.get("price")
            pt = o.get("point")
            if price is None:
                continue
            if pick_line is not None and pt is not None and abs(float(pt) - pick_line) > 0.01:
                continue
            if _team_matches(pick_team.lower(), name.lower()):
                pick_o = {"price": int(price), "name": name}
            elif opp_o is None:
                opp_o = {"price": int(price), "name": name}
        if pick_o and opp_o:
            return {"pick_odds": pick_o["price"], "opp_odds": opp_o["price"],
                    "pick_label": pick_o["name"], "opp_label": opp_o["name"], "book": book_name}

    return None


def _find_event_id(
    event_list: list[dict], teams: list[str], *, as_of: datetime | None = None,
) -> str | None:
    """Pick the event that best matches the pick's teams list.

    Scoring (lower is better):
      1. -(# of distinct pick teams that match this event)  — an event matching
         both teams always beats an event matching only one. This prevents a
         mis-parsed opponent from hijacking the lookup to a different game.
      2. first_team_miss (0 if the first team in the pick matches, else 1) —
         the first team is typically the subject of the bet, so we prefer it
         when no event matches both teams.
      3. long_finished (1 if the event began more than _STARTED_GRACE_H ago) —
         prefer an upcoming game over a finished one at the same proximity.
         Bounded on purpose: an event that has merely KICKED OFF is still the
         likeliest subject of the pick, and a plain started/not-started flag
         outranks proximity, so a game in its second quarter would lose to the
         same team's game five weeks out. That is also the documented trap
         where a re-fetch mid-series binds to tomorrow's game in the same
         matchup. The grace covers the longest event we price (a fight card).
      4. proximity to the reference time.

    `as_of` is that reference, defaulting to now. Callers that know the date the
    pick is ABOUT must pass it: steps 3 and 4 otherwise measure from now, so any
    same-team game still in the future outranks the pick's own game the moment
    it kicks off — which is how a Cardinals preseason moneyline resolved to
    their Week 1 game five weeks later and priced it at +455 instead of -102.
    Ranking against the pick's date rather than clamping to a window keeps a
    fight card posted five days early (routine for UFC) matching correctly.

    Fails closed when the pick names several teams that must be opponents (a
    game total, "Panthers / Cardinals under 35.5") and the winning event holds
    only ONE of them while the other is demonstrably on the schedule somewhere
    else: that event is provably not the game the pick is about, and pricing it
    is worse than reporting no game. The "demonstrably" half matters — a name
    variant _team_matches can't resolve must still fall through to the partial
    match, or this would drop odds we get correctly today.
    """
    now = as_of or datetime.now(timezone.utc)
    teams_lower = [t.lower() for t in teams]
    seen_anywhere: set[int] = set()
    scored: list[tuple[tuple[int, int, int, float], str]] = []
    for event in event_list:
        home = event.get("home_team", "")
        away = event.get("away_team", "")
        sides = [home.lower(), away.lower()]
        matched_idxs = [
            i for i, t in enumerate(teams_lower)
            if any(_team_matches(t, s) for s in sides)
        ]
        if not matched_idxs:
            continue
        seen_anywhere.update(matched_idxs)
        ct = event.get("commence_time", "")
        try:
            commence = datetime.fromisoformat(ct.replace("Z", "+00:00"))
            elapsed = (now - commence).total_seconds()
            started = 1 if elapsed > _STARTED_GRACE_H * 3600 else 0
            proximity = abs(elapsed)
        except (ValueError, AttributeError):
            started = 1
            proximity = float("inf")
        match_count = len(matched_idxs)
        first_team_miss = 0 if 0 in matched_idxs else 1
        scored.append(((-match_count, first_team_miss, started, proximity),
                       event["id"], frozenset(matched_idxs)))
    if not scored:
        return None
    scored.sort(key=lambda x: x[0])
    _, best_id, best_idxs = scored[0]

    # Partial match on a multi-team pick: only reject when every team the pick
    # names IS on the schedule, just not together in this event.
    if len(teams_lower) > 1 and len(best_idxs) < len(teams_lower):
        if seen_anywhere == set(range(len(teams_lower))):
            return None
    return best_id


def _lookup_moneyline(bookmakers: list[dict], team: str, period: str = "game", market: str = "h2h", sport: str = "") -> dict:
    mkt = market + _get_period_suffix(period, sport)
    candidates = [(price, bk) for _, price, bk in _collect_outcomes(bookmakers, mkt, name_filter=team)]
    odds, book = _pick_best(candidates)
    return {
        "match_type":    "exact" if odds is not None else f"no_{mkt}_data",
        "pick_line":     None,
        "api_line":      None,
        "computed_odds": odds,
        "adjusted_odds": odds,
        "bookmaker":     book,
    }


def _lookup_spread(sport: str, bookmakers: list[dict], team: str, pick_line: float, period: str = "game") -> dict:
    suffix   = _get_period_suffix(period, sport)
    main_mkt = "spreads" + suffix
    alt_mkt  = ("alternate_spreads" + suffix) if (not suffix or suffix.startswith("_1st_")) else None

    _empty = {"match_type": "no_spread_data", "pick_line": pick_line,
              "api_line": None, "computed_odds": None, "adjusted_odds": None, "bookmaker": None}

    for mkt in filter(None, [main_mkt, alt_mkt]):
        hits = _collect_outcomes(bookmakers, mkt, name_filter=team, line_filter=pick_line)
        if hits:
            odds, book = _pick_best([(price, bk) for _, price, bk in hits])
            label = "exact" if mkt == main_mkt else "exact_alt"
            return {"match_type": label, "pick_line": pick_line,
                    "api_line": pick_line, "computed_odds": odds, "adjusted_odds": odds, "bookmaker": book}

    all_lines: list[tuple[float, int, str]] = []
    for mkt in filter(None, [alt_mkt, main_mkt]):
        for pt, price, bk in _collect_outcomes(bookmakers, mkt, name_filter=team):
            if pt is None:
                continue
            if pick_line != 0 and (pick_line < 0) != (pt < 0):
                continue
            all_lines.append((pt, price, bk))

    if not all_lines:
        return _empty

    min_gap = min(abs(x[0] - pick_line) for x in all_lines)
    at_closest = [(pt, price, bk) for pt, price, bk in all_lines if abs(pt - pick_line) <= min_gap + 0.01]
    best_pt, best_price, best_bk = max(at_closest, key=lambda x: x[1])
    gap = abs(best_pt - pick_line)

    if gap > MAX_LINE_GAP:
        return {"match_type": f"alt_line_gap_{gap:.1f}pts", "pick_line": pick_line,
                "api_line": best_pt, "computed_odds": None, "adjusted_odds": None, "bookmaker": None}

    adjusted = _adjust_for_gap(sport, best_price, pick_line, best_pt, gap)
    return {"match_type": f"proximity_{gap:.1f}pts", "pick_line": pick_line,
            "api_line": best_pt, "computed_odds": best_price, "adjusted_odds": adjusted, "bookmaker": best_bk}


def _lookup_total(sport: str, bookmakers: list[dict], direction: str, pick_line: float, period: str = "game") -> dict:
    suffix       = _get_period_suffix(period, sport)
    main_mkt     = "totals" + suffix
    alt_mkt      = ("alternate_totals" + suffix) if (not suffix or suffix.startswith("_1st_") or suffix.startswith("_p")) else None
    outcome_name = "Over" if direction == "over" else "Under"

    _empty = {"match_type": "no_total_data", "pick_line": pick_line,
              "api_line": None, "computed_odds": None, "adjusted_odds": None, "bookmaker": None}

    for mkt in filter(None, [main_mkt, alt_mkt]):
        hits = _collect_outcomes(bookmakers, mkt, name_filter=outcome_name, line_filter=pick_line)
        if hits:
            odds, book = _pick_best([(price, bk) for _, price, bk in hits])
            label = "exact" if mkt == main_mkt else "exact_alt"
            return {"match_type": label, "pick_line": pick_line,
                    "api_line": pick_line, "computed_odds": odds, "adjusted_odds": odds, "bookmaker": book}

    all_lines: list[tuple[float, int, str]] = []
    for mkt in filter(None, [alt_mkt, main_mkt]):
        for pt, price, bk in _collect_outcomes(bookmakers, mkt, name_filter=outcome_name):
            if pt is not None:
                all_lines.append((pt, price, bk))

    if not all_lines:
        return _empty

    min_gap = min(abs(x[0] - pick_line) for x in all_lines)
    at_closest = [(pt, price, bk) for pt, price, bk in all_lines if abs(pt - pick_line) <= min_gap + 0.01]
    best_pt, best_price, best_bk = max(at_closest, key=lambda x: x[1])
    gap = abs(best_pt - pick_line)

    if gap > MAX_LINE_GAP:
        return {"match_type": f"alt_line_gap_{gap:.1f}pts", "pick_line": pick_line,
                "api_line": best_pt, "computed_odds": None, "adjusted_odds": None, "bookmaker": None}

    signed_pick = -pick_line if direction == "over" else pick_line
    signed_api  = -best_pt if direction == "over" else best_pt
    adjusted = _adjust_for_gap(sport, best_price, signed_pick, signed_api, gap)
    return {"match_type": f"proximity_{gap:.1f}pts", "pick_line": pick_line,
            "api_line": best_pt, "computed_odds": best_price, "adjusted_odds": adjusted, "bookmaker": best_bk}


def _lookup_team_total(sport: str, bookmakers: list[dict], team: str, direction: str, pick_line: float, period: str = "game") -> dict:
    """Look up team total odds (team_totals / alternate_team_totals markets).

    These markets use `description` for the team name and `name` for Over/Under,
    unlike game totals which use `name` for the outcome label only.
    """
    outcome_name = "Over" if direction == "over" else "Under"
    suffix = _get_period_suffix(period, sport)
    _empty = {"match_type": "team_total_unavailable", "pick_line": pick_line,
              "api_line": None, "computed_odds": None, "adjusted_odds": None, "bookmaker": None}

    def _collect_team_total(mkt_key: str, line_filter: float | None = None):
        results = []
        for bk in bookmakers:
            bk_key = bk.get("key", "")
            for mkt in bk.get("markets", []):
                if mkt.get("key") != mkt_key:
                    continue
                for outcome in mkt.get("outcomes", []):
                    desc  = outcome.get("description", "")
                    name  = outcome.get("name", "")
                    pt    = outcome.get("point")
                    price = outcome.get("price")
                    if not _team_matches(team.lower(), desc.lower()):
                        continue
                    if name != outcome_name:
                        continue
                    if line_filter is not None and pt is not None:
                        if abs(float(pt) - line_filter) > 0.01:
                            continue
                    if price is not None:
                        results.append((float(pt) if pt is not None else None, int(price), bk_key))
        return results

    main_mkt = "team_totals" + suffix
    alt_mkt  = "alternate_team_totals" + suffix

    # Exact match on main market, then alternate
    for mkt_key, label in [(main_mkt, "exact"), (alt_mkt, "exact_alt")]:
        hits = _collect_team_total(mkt_key, line_filter=pick_line)
        if hits:
            odds, book = _pick_best([(price, bk) for _, price, bk in hits])
            return {"match_type": label, "pick_line": pick_line,
                    "api_line": pick_line, "computed_odds": odds, "adjusted_odds": odds, "bookmaker": book}

    # Proximity: gather all lines from period-specific markets only.
    # Do NOT fall back to full-game markets for period picks — the line scales are incompatible.
    all_lines = []
    for mkt_key in (alt_mkt, main_mkt):
        for pt, price, bk in _collect_team_total(mkt_key):
            if pt is not None:
                all_lines.append((pt, price, bk))

    if not all_lines:
        return _empty

    min_gap = min(abs(x[0] - pick_line) for x in all_lines)
    at_closest = [(pt, price, bk) for pt, price, bk in all_lines if abs(pt - pick_line) <= min_gap + 0.01]
    best_pt, best_price, best_bk = max(at_closest, key=lambda x: x[1])
    gap = abs(best_pt - pick_line)

    if gap > MAX_LINE_GAP:
        return {"match_type": f"alt_line_gap_{gap:.1f}pts", "pick_line": pick_line,
                "api_line": best_pt, "computed_odds": None, "adjusted_odds": None, "bookmaker": None}

    signed_pick = -pick_line if direction == "over" else pick_line
    signed_api  = -best_pt if direction == "over" else best_pt
    adjusted = _adjust_for_gap(sport, best_price, signed_pick, signed_api, gap)
    return {"match_type": f"proximity_{gap:.1f}pts", "pick_line": pick_line,
            "api_line": best_pt, "computed_odds": best_price, "adjusted_odds": adjusted, "bookmaker": best_bk}


def _lookup_prop(bookmakers: list[dict], player: str, prop_market: str, direction: str, line: float) -> dict:
    outcome_name = "Over" if direction == "over" else "Under"
    candidates: list[tuple[int, str]] = []
    for bk in bookmakers:
        bk_key = bk.get("key", "")
        for mkt in bk.get("markets", []):
            if mkt.get("key") != prop_market:
                continue
            for outcome in mkt.get("outcomes", []):
                desc  = outcome.get("description", "")
                name  = outcome.get("name", "")
                pt    = outcome.get("point")
                price = outcome.get("price")
                if not _team_matches(player.lower(), desc.lower()):
                    continue
                if name != outcome_name:
                    continue
                if pt is not None and abs(float(pt) - line) > 0.01:
                    continue
                if price is not None:
                    candidates.append((int(price), bk_key))
    odds, book = _pick_best(candidates)
    return {
        "match_type":    "exact" if odds is not None else "prop_not_found",
        "pick_line":     line,
        "api_line":      line if odds is not None else None,
        "computed_odds": odds,
        "adjusted_odds": odds,
        "bookmaker":     book,
    }


def lookup_pick_odds(sport: str, pick: dict, bookmakers: list[dict]) -> dict:
    """Given a parsed pick and a bookmakers list, find the best odds. Returns a raw result dict."""
    teams     = pick.get("teams") or []
    bet_type  = pick.get("bet_type", "")
    line      = pick.get("line")
    direction = pick.get("direction")
    period    = pick.get("period", "game")
    desc      = pick.get("description", "")

    if period == "game" and _PERIOD_RE.search(desc):
        m = _PERIOD_RE.search(desc)
        raw = m.group(1).lower().replace(" ", "").replace("st", "").replace("nd", "").replace("rd", "").replace("th", "")
        period = {"half": "1h", "1half": "1h", "2half": "2h",
                  "firsthalf": "1h", "secondhalf": "2h",
                  "quarter": "1q", "1quarter": "1q",
                  "1period": "1p", "2period": "2p", "3period": "3p"}.get(raw, raw)

    if bet_type == "prop":
        return {"match_type": "player_prop_unavailable", "pick_line": line,
                "api_line": None, "computed_odds": None, "adjusted_odds": None, "bookmaker": None}

    if bet_type == "team_total":
        prop_stat = (pick.get("prop_stat") or "").upper()
        if prop_stat and prop_stat != "GOALS":
            return {"match_type": f"prop_stat_unsupported({prop_stat})", "pick_line": line,
                    "api_line": None, "computed_odds": None, "adjusted_odds": None, "bookmaker": None}
        if line is None or not direction:
            return {"match_type": "missing_line_or_direction", "pick_line": line,
                    "api_line": None, "computed_odds": None, "adjusted_odds": None, "bookmaker": None}
        return _lookup_team_total(sport, bookmakers, teams[0] if teams else "", direction, float(line), period)

    if not bookmakers:
        return {"match_type": "no_game", "pick_line": line,
                "api_line": None, "computed_odds": None, "adjusted_odds": None, "bookmaker": None}

    if bet_type == "moneyline":
        # "To advance" / "to qualify" / "to lift the trophy" — use the to_qualify
        # market; the h2h market returns 90-min ML which is the wrong price. The
        # outright winner fallback (fetch_odds*) covers the final, where to_qualify
        # is absent.
        if _is_advance_or_outright(desc):
            return _lookup_moneyline(bookmakers, teams[0] if teams else "", period, "to_qualify", sport)
        market = "h2h_3_way" if is_regulation_ml(desc) else "h2h"
        return _lookup_moneyline(bookmakers, teams[0] if teams else "", period, market, sport)

    if bet_type == "spread":
        if line is None:
            return {"match_type": "no_line_in_pick", "pick_line": None,
                    "api_line": None, "computed_odds": None, "adjusted_odds": None, "bookmaker": None}
        return _lookup_spread(sport, bookmakers, teams[0] if teams else "", float(line), period)

    if bet_type == "total":
        prop_stat = (pick.get("prop_stat") or "").upper()
        if prop_stat and prop_stat != "GOALS":
            return {"match_type": f"prop_stat_unsupported({prop_stat})", "pick_line": line,
                    "api_line": None, "computed_odds": None, "adjusted_odds": None, "bookmaker": None}
        if line is None or not direction:
            return {"match_type": "missing_line_or_direction", "pick_line": line,
                    "api_line": None, "computed_odds": None, "adjusted_odds": None, "bookmaker": None}
        return _lookup_total(sport, bookmakers, direction, float(line), period)

    return {"match_type": f"unsupported_bet_type({bet_type})", "pick_line": line,
            "api_line": None, "computed_odds": None, "adjusted_odds": None, "bookmaker": None}


_MLB_INNINGS_MARKETS: dict[str, str] = {
    "moneyline": "h2h_1st_5_innings",
    "spread":    "spreads_1st_5_innings,alternate_spreads_1st_5_innings",
    "total":     "totals_1st_5_innings,alternate_totals_1st_5_innings",
}


_NARROW_BASE: dict[str, str] = {
    "moneyline": "h2h", "spread": "spreads", "total": "totals", "team_total": "team_totals",
}


def _alt_market_for(base: str, suffix: str) -> str | None:
    """The alternate market the matching `_lookup_*` will actually read, or None.

    These conditions are NOT uniform across bet types — spreads consult an
    alternate only at game level and MLB innings, totals also at hockey periods,
    team totals always, and h2h has no alternate at all. Mirrored exactly from the
    lookups below, because requesting one they never read is billed for nothing
    while omitting one they do read silently costs an exact_alt match.
    `scripts/test_market_narrowing.py` pins the two in sync.
    """
    if base == "spreads":
        return f"alternate_spreads{suffix}" if (not suffix or suffix.startswith("_1st_")) else None
    if base == "totals":
        return (f"alternate_totals{suffix}"
                if (not suffix or suffix.startswith("_1st_") or suffix.startswith("_p")) else None)
    if base == "team_totals":
        return f"alternate_team_totals{suffix}"
    return None


def _narrow_markets_for_pick(pick: dict, sport: str = "") -> str | None:
    """Just the markets this pick's lookup actually reads, at its own period.

    Mirrors what `_lookup_*` builds (`base + _get_period_suffix(...)`), so the
    narrowed request answers the same question — a 1H total asks for totals_h1,
    not totals, because the period decides the line. Everything else in the old
    list was billed for being available, never read.

    The `alternate_*` sibling is included: 10.6% of priced picks match through it
    (exact_alt), so dropping it costs five times what dropping us2 does. Economy
    mode drops it anyway — backfills fall back to -110 and nobody reads the line.

    Returns None for anything not confidently reducible (props, to_qualify,
    unknown bet types), which falls through to the existing wider list.
    """
    base = _NARROW_BASE.get(pick.get("bet_type", ""))
    if not base:
        return None
    suffix = _get_period_suffix(pick.get("period") or "game", sport)
    mkts = [base + suffix]
    if not _ECONOMY:
        alt = _alt_market_for(base, suffix)
        if alt:
            mkts.append(alt)
    # 3-way/regulation moneylines are priced in a different market entirely.
    if base == "h2h" and is_regulation_ml(pick.get("description", "")):
        mkts.append("h2h_3_way" + suffix)
    return ",".join(mkts)


def _markets_for_pick(pick: dict, sport: str = "") -> str:
    """Minimal markets string for this pick's bet_type. Falls back to MARKETS_FULL."""
    desc = pick.get("description", "")
    # "To advance" / "to qualify" / "to lift the trophy" needs the to_qualify
    # market (or the outright winner fallback), never the 90-min h2h.
    if pick.get("bet_type") == "moneyline" and _is_advance_or_outright(desc):
        return MARKETS_BY_TYPE["to_advance"]
    narrowed = _narrow_markets_for_pick(pick, sport)
    if narrowed:
        return narrowed
    base = MARKETS_BY_TYPE.get(pick.get("bet_type", ""), MARKETS_FULL)
    if sport == "MLB" and pick.get("period") in _MLB_PERIOD_SUFFIX:
        extra = _MLB_INNINGS_MARKETS.get(pick.get("bet_type", ""), "")
        if extra:
            return base + "," + extra
    return base


# ── Outright winner fallback ──────────────────────────────────────────────────

async def _fetch_outright_winner(
    sport_key: str, team: str, conn: sqlite3.Connection,
    *, game_date: str | None = None, current: bool = False,
) -> dict | None:
    """Best outright tournament-winner price for `team` (e.g. 'Spain to lift the
    trophy'). The Odds API prices this in a separate '{sport}_winner' sport with
    an 'outrights' market keyed by team-named outcomes — no game event needed.
    Returns a lookup_pick_odds-style dict (match_type 'outright'), or None."""
    winner_key = _WINNER_SPORT_KEYS.get(sport_key)
    if not winner_key or not team:
        return None
    if current:
        events = await _fetch_current_event_list(winner_key, conn)
    else:
        events = await _fetch_event_list(winner_key, game_date or "", conn)
    for event in events:
        eid = event.get("id")
        if not eid:
            continue
        if current:
            bks = await _fetch_current_bookmakers(winner_key, eid, "outrights", conn)
        else:
            bks = await _fetch_bookmakers(winner_key, eid, game_date or "", "outrights", conn)
        r = _lookup_moneyline(bks, team, "game", "outrights")
        if r.get("adjusted_odds") is not None:
            return {**r, "match_type": "outright"}
    return None


# ── Main entry point ──────────────────────────────────────────────────────────

async def fetch_odds(sport: str, game_date: str, pick: dict, db_path: str = DB_PATH) -> OddsResult:
    """
    Look up closing odds for a single pick.

    Args:
        sport:     Our internal sport name, e.g. "NBA", "MLB".
        game_date: ISO date string, e.g. "2026-03-27".
        pick:      Parsed pick dict with keys: bet_type, teams, line, direction,
                   period, player, prop_stat, description.
        db_path:   Path to picks.db (default: project root).

    Returns an OddsResult. Check result.is_unexpected_miss to decide whether
    to flag the failure in the run summary.
    """
    sport_key = SPORT_KEYS.get(sport)
    if not sport_key:
        return OddsResult(match_type=f"sport_unsupported({sport})", pick_line=pick.get("line"))

    conn = sqlite3.connect(db_path)
    try:
        _init_db(conn)
        _evict_old(conn)

        teams    = pick.get("teams") or []
        bet_type = pick.get("bet_type", "")

        # Defensive: for moneylines, if teams is empty but player is set (parse
        # misclassified a UFC/Boxing fighter into player), treat player as the team.
        if bet_type == "moneyline" and not teams and pick.get("player"):
            teams = [pick["player"]]

        # ── Player props: separate endpoint ───────────────────────────────────
        if bet_type == "prop":
            prop_stat   = (pick.get("prop_stat") or "").upper()
            prop_market = PROP_STAT_MARKETS.get(sport, {}).get(prop_stat)
            if not prop_market:
                return OddsResult(match_type=f"prop_stat_unsupported({prop_stat})", pick_line=pick.get("line"))

            event_list = await _fetch_event_list_all(sport, game_date, conn)
            event_id   = _find_event_id(event_list, teams, as_of=_snapshot_time(game_date))
            if not event_id:
                return OddsResult(match_type="no_game", pick_line=pick.get("line"))

            ev_key     = _event_sport_key(event_list, event_id, sport_key)
            bookmakers = await _fetch_bookmakers(ev_key, event_id, game_date, prop_market, conn)
            r = _lookup_prop(bookmakers, pick.get("player") or "", prop_market,
                             pick.get("direction") or "over", float(pick.get("line") or 0.5))
            return OddsResult(
                match_type  = r["match_type"],
                odds        = r["adjusted_odds"],
                bookmaker   = r["bookmaker"],
                api_line    = r["api_line"],
                pick_line   = r["pick_line"],
            )

        # ── Non-goals team/game totals (corners, etc.): Odds API has no market ─
        if bet_type in ("team_total", "total"):
            prop_stat = (pick.get("prop_stat") or "").upper()
            if prop_stat and prop_stat != "GOALS":
                return OddsResult(match_type=f"prop_stat_unsupported({prop_stat})", pick_line=pick.get("line"))

        # ── All other bet types ───────────────────────────────────────────────
        bookmakers: list[dict] = []

        # ESPN first (free; pre-game only — odds cleared after game completion).
        # Historical API calls bill ~10x, so a free price short-circuits them.
        # Adopted only when it actually prices: for completed games ESPN has
        # nothing and the API tier below must still own the verdict.
        r: dict | None = None
        if sport in ESPN_LEAGUES:
            espn_data = await fetch_espn(sport, game_date)
            if espn_data:
                espn_bk = espn_bookmakers_for_teams(espn_data, teams)
                if espn_bk:
                    r_espn = lookup_pick_odds(sport, pick, espn_bk)
                    if r_espn.get("adjusted_odds") is not None:
                        r = r_espn

        # Odds API last resort (historical closing odds + alternate lines)
        if r is None:
            event_list = await _fetch_event_list_all(sport, game_date, conn)
            event_id   = _find_event_id(event_list, teams, as_of=_snapshot_time(game_date))
            if event_id:
                ev_key     = _event_sport_key(event_list, event_id, sport_key)
                bookmakers = await _fetch_bookmakers(ev_key, event_id, game_date, _markets_for_pick(pick, sport), conn)

            r = lookup_pick_odds(sport, pick, bookmakers)

            # Outright winner fallback: "to lift the trophy" / final-round advance —
            # the game's to_qualify/h2h markets don't carry the 2-way winner price.
            if (r.get("adjusted_odds") is None and bet_type == "moneyline"
                    and _is_advance_or_outright(pick.get("description", ""))):
                ow = await _fetch_outright_winner(
                    sport_key, teams[0] if teams else "", conn, game_date=game_date)
                if ow:
                    r = ow

        return OddsResult(
            match_type  = r["match_type"],
            odds        = r["adjusted_odds"],
            bookmaker   = r["bookmaker"],
            api_line    = r["api_line"],
            pick_line   = r["pick_line"],
        )

    finally:
        conn.close()


def _event_already_started(event_list: list[dict], event_id: str) -> bool:
    """Return True if the event's commence_time is in the past."""
    event = next((e for e in event_list if e.get("id") == event_id), None)
    if not event or not event.get("commence_time"):
        return False
    try:
        commence = datetime.fromisoformat(event["commence_time"].replace("Z", "+00:00"))
        return commence < datetime.now(timezone.utc)
    except ValueError:
        return False


_ET = ZoneInfo("America/New_York")


def _utc_to_eastern_date(commence_time: str) -> str | None:
    """Convert a UTC ISO timestamp to a YYYY-MM-DD date in US Eastern time."""
    if not commence_time or len(commence_time) < 10:
        return None
    try:
        dt = datetime.fromisoformat(commence_time.replace("Z", "+00:00"))
        return dt.astimezone(_ET).strftime("%Y-%m-%d")
    except ValueError:
        return commence_time[:10]


def _get_event_date(event_list: list[dict], event_id: str) -> str | None:
    """Return YYYY-MM-DD (US Eastern) from an event's commence_time, or None."""
    event = next((e for e in event_list if e.get("id") == event_id), None)
    ct = (event or {}).get("commence_time", "")
    return _utc_to_eastern_date(ct)


def _get_event_commence(event_list: list[dict], event_id: str) -> str | None:
    """Return the event's raw commence_time ISO string, or None."""
    event = next((e for e in event_list if e.get("id") == event_id), None)
    return (event or {}).get("commence_time") or None


async def _try_pregame(
    sport: str, sport_key: str, event_list: list[dict], event_id: str,
    pick: dict, db_path: str,
) -> "OddsResult | None":
    """
    Fetch historical closing odds (at game start) for an in-progress game.

    Uses commence_time as the API snapshot so we get the closing line, not a
    mid-day snapshot. Cached under 'pregame_YYYY-MM-DD' to avoid conflicting
    with the regular T18:00:00Z historical cache.
    """
    event = next((e for e in event_list if e.get("id") == event_id), None)
    if not event:
        return None
    commence_time = event.get("commence_time", "")
    if not commence_time or len(commence_time) < 10:
        return None
    game_date  = _utc_to_eastern_date(commence_time) or commence_time[:10]
    cache_date = f"pregame_{game_date}"

    bet_type = pick.get("bet_type", "")
    if bet_type == "prop":
        prop_stat   = (pick.get("prop_stat") or "").upper()
        prop_market = PROP_STAT_MARKETS.get(sport, {}).get(prop_stat)
        if not prop_market:
            return None
        markets = prop_market
    elif bet_type in ("team_total", "total"):
        prop_stat = (pick.get("prop_stat") or "").upper()
        if prop_stat and prop_stat != "GOALS":
            return None
        markets = _markets_for_pick(pick, sport)
    else:
        markets = _markets_for_pick(pick, sport)

    conn = sqlite3.connect(db_path)
    try:
        bookmakers = _get_bookmakers(conn, event_id, cache_date, _cache_markets(markets))
        if bookmakers is None:
            if not ODDS_API_KEY:
                return None
            async with httpx.AsyncClient(timeout=20) as http:
                data = await _api_get(http,
                    f"{ODDS_API_BASE}/historical/sports/{sport_key}/events/{event_id}/odds",
                    {"apiKey": ODDS_API_KEY, "regions": REGIONS, "markets": markets,
                     "date": commence_time, "oddsFormat": "american"},
                )
            if isinstance(data, dict):
                bookmakers = data.get("data", {}).get("bookmakers", []) if "data" in data else data.get("bookmakers", [])
            else:
                bookmakers = []
            _save_bookmakers(conn, sport_key, event_id, cache_date, _cache_markets(markets), bookmakers)
    finally:
        conn.close()

    if not bookmakers:
        return None

    if bet_type == "prop":
        r = _lookup_prop(bookmakers, pick.get("player") or "", prop_market,
                         pick.get("direction") or "over", float(pick.get("line") or 0.5))
    else:
        r = lookup_pick_odds(sport, pick, bookmakers)

    if r.get("adjusted_odds") is None:
        return None
    return OddsResult(
        match_type=r["match_type"], odds=r["adjusted_odds"],
        bookmaker=r["bookmaker"], api_line=r["api_line"], pick_line=r["pick_line"],
    )


async def fetch_odds_current(sport: str, pick: dict, db_path: str = DB_PATH,
                             *, free_only: bool = False) -> OddsResult:
    """
    Look up current (live pre-game) odds for a pick.

    Source order: ESPN (free, its leagues) → Bovada (free) → Odds API (paid,
    LAST resort — the subscription ended 2026-08-22, the key reverts to the
    free 500-credits/month tier, so every market call must earn its place; the
    /events match stays first because it costs nothing and anchors the Bovada
    same-game guard). free_only=True skips every paid call — the tracker's
    miss-retry loop uses it (see should_retry_odds).

    Intended to be called at pick-receive time (first tracker encounter).
    Results cached in picks.db under game_date='current' with a short TTL.
    """
    sport_key = SPORT_KEYS.get(sport)
    if not sport_key:
        return OddsResult(match_type=f"sport_unsupported({sport})", pick_line=pick.get("line"))

    conn = sqlite3.connect(db_path)
    try:
        _init_db(conn)
        _evict_old(conn)

        teams    = pick.get("teams") or []
        bet_type = pick.get("bet_type", "")

        # Defensive: for moneylines, if teams is empty but player is set (parse
        # misclassified a UFC/Boxing fighter into player), treat player as the team.
        if bet_type == "moneyline" and not teams and pick.get("player"):
            teams = [pick["player"]]

        # ── Player props: separate endpoint ───────────────────────────────────
        if bet_type == "prop":
            prop_stat   = (pick.get("prop_stat") or "").upper()
            prop_market = PROP_STAT_MARKETS.get(sport, {}).get(prop_stat)
            if not prop_market:
                return OddsResult(match_type=f"prop_stat_unsupported({prop_stat})", pick_line=pick.get("line"))
            if free_only:
                # Props are paid-API-only today (Bovada's prop groups exist but
                # are unmapped — ~2% of picks; see docs/odds.md).
                return OddsResult(match_type="player_prop_unavailable", pick_line=pick.get("line"))
            event_list = await _fetch_current_event_list_all(sport, conn)
            event_id   = _find_event_id(event_list, teams)
            if not event_id:
                return OddsResult(match_type="no_game", pick_line=pick.get("line"))
            gd     = _get_event_date(event_list, event_id)
            ev_key = _event_sport_key(event_list, event_id, sport_key)
            if _event_already_started(event_list, event_id):
                live_bk = await _fetch_current_bookmakers(ev_key, event_id, prop_market, conn, live=True)
                pregame = await _try_pregame(sport, ev_key, event_list, event_id, pick, db_path)
                if live_bk:
                    r = _lookup_prop(live_bk, pick.get("player") or "", prop_market,
                                     pick.get("direction") or "over", float(pick.get("line") or 0.5))
                    if r.get("adjusted_odds") is not None:
                        return OddsResult(
                            match_type=f"live_{r['match_type']}", odds=r["adjusted_odds"],
                            bookmaker=r["bookmaker"], api_line=r["api_line"], pick_line=r["pick_line"],
                            pregame_odds=pregame.odds if pregame else None,
                            pregame_bookmaker=pregame.bookmaker if pregame else None,
                            pregame_match_type=f"pregame_{pregame.match_type}" if pregame else None,
                            game_date=gd,
                        )
                if pregame:
                    return OddsResult(match_type=f"pregame_{pregame.match_type}", odds=pregame.odds,
                                      bookmaker=pregame.bookmaker, api_line=pregame.api_line,
                                      pick_line=pregame.pick_line, game_date=gd)
                return OddsResult(match_type="game_in_progress", pick_line=pick.get("line"), game_date=gd)
            bookmakers = await _fetch_current_bookmakers(ev_key, event_id, prop_market, conn)
            r = _lookup_prop(bookmakers, pick.get("player") or "", prop_market,
                             pick.get("direction") or "over", float(pick.get("line") or 0.5))
            return OddsResult(
                match_type = r["match_type"],
                odds       = r["adjusted_odds"],
                bookmaker  = r["bookmaker"],
                api_line   = r["api_line"],
                pick_line  = r["pick_line"],
                game_date  = gd,
            )

        # ── Non-goals team/game totals (corners, etc.): Odds API has no market ─
        if bet_type in ("team_total", "total"):
            prop_stat = (pick.get("prop_stat") or "").upper()
            if prop_stat and prop_stat != "GOALS":
                return OddsResult(match_type=f"prop_stat_unsupported({prop_stat})", pick_line=pick.get("line"))

        # ── All other bet types ───────────────────────────────────────────────
        bookmakers: list[dict] = []

        # Event binding via the API /events endpoint stays FIRST even though the
        # API prices last: /events is free (x-requests-last: 0) and its match
        # anchors game_date, commence_time and the Bovada same-game guard.
        event_list = await _fetch_current_event_list_all(sport, conn)
        event_id   = _find_event_id(event_list, teams)
        gd       = _get_event_date(event_list, event_id) if event_id else None
        commence = _get_event_commence(event_list, event_id) if event_id else None
        ev_key = _event_sport_key(event_list, event_id, sport_key) if event_id else sport_key
        if event_id:
            if _event_already_started(event_list, event_id):
                # Bovada live first (free) — the paid live call only runs when
                # Bovada can't price the pick (e.g. its near-game coupon trims
                # period markets). Same-game guard applies: the API matched
                # this event, so Bovada must agree on the date.
                r_live: dict | None = None
                if sport in _BOVADA_PATHS:
                    bov_live, bov_live_gd = await _fetch_bovada_bookmakers(sport, teams, allow_started=True)
                    if bov_live and _bovada_result_acceptable(gd, bov_live_gd):
                        r_try = lookup_pick_odds(sport, pick, bov_live)
                        if r_try.get("adjusted_odds") is not None:
                            r_live = r_try
                if r_live is None and not free_only:
                    live_bk = await _fetch_current_bookmakers(ev_key, event_id, _markets_for_pick(pick, sport), conn, live=True)
                    if live_bk:
                        r_try = lookup_pick_odds(sport, pick, live_bk)
                        if r_try.get("adjusted_odds") is not None:
                            r_live = r_try
                # Closing line: the paid historical endpoint is the only source
                # (Bovada keeps no history). Skipped on free-only retries; a
                # no-historical-access plan just returns nothing here.
                pregame = None if free_only else await _try_pregame(sport, ev_key, event_list, event_id, pick, db_path)
                if r_live is not None:
                    return OddsResult(
                        match_type=f"live_{r_live['match_type']}", odds=r_live["adjusted_odds"],
                        bookmaker=r_live["bookmaker"], api_line=r_live["api_line"], pick_line=r_live["pick_line"],
                        pregame_odds=pregame.odds if pregame else None,
                        pregame_bookmaker=pregame.bookmaker if pregame else None,
                        pregame_match_type=f"pregame_{pregame.match_type}" if pregame else None,
                        game_date=gd,
                    )
                if pregame:
                    return OddsResult(match_type=f"pregame_{pregame.match_type}", odds=pregame.odds,
                                      bookmaker=pregame.bookmaker, api_line=pregame.api_line,
                                      pick_line=pregame.pick_line, game_date=gd)
                return OddsResult(match_type="game_in_progress", pick_line=pick.get("line"), game_date=gd)
            # Pre-game: no immediate paid call — free sources below get the
            # first shot, the paid market call is the last resort.

        r = lookup_pick_odds(sport, pick, bookmakers)

        # ESPN first (free; pre-game only, skip for team_total — ESPN lacks that market)
        if r.get("adjusted_odds") is None and sport in ESPN_LEAGUES and bet_type != "team_total":
            from datetime import date as _d
            espn_data = await fetch_espn(sport, _d.today().isoformat())
            if espn_data:
                espn_bk = espn_bookmakers_for_teams(espn_data, teams)
                if espn_bk:
                    r = lookup_pick_odds(sport, pick, espn_bk)
                    if r.get("adjusted_odds") is not None:
                        bookmakers = espn_bk  # feeds the both-sides extraction below

        # Bovada second (free; pre-game only — _bovada_pick_event refuses a
        # started game here, so a live price can't be recorded as a closing
        # one). CFL has no ESPN odds; Bovada also carries period and alternate
        # markets ESPN never lists.
        if r.get("adjusted_odds") is None and sport in _BOVADA_PATHS:
            bov_bk, bov_gd = await _fetch_bovada_bookmakers(sport, teams)
            if bov_bk and _bovada_result_acceptable(gd, bov_gd):
                r2 = lookup_pick_odds(sport, pick, bov_bk)
                # A Bovada miss verdict is adopted too when nothing else had the
                # game at all: "alt_line_gap_6.0pts" from a market Bovada
                # actually served beats a blanket no_game.
                if r2.get("adjusted_odds") is not None or r.get("match_type") == "no_game":
                    r = r2
                    bookmakers = bov_bk  # feeds the both-sides extraction below
                    if gd is None:
                        gd = bov_gd

        # Odds API LAST RESORT (paid) — only when the free sources left no
        # price. Same adoption rule as Bovada: a price always wins; a miss
        # verdict only replaces an uninformative no_game.
        if r.get("adjusted_odds") is None and event_id and not free_only:
            api_bk = await _fetch_current_bookmakers(ev_key, event_id, _markets_for_pick(pick, sport), conn)
            if api_bk:
                r_api = lookup_pick_odds(sport, pick, api_bk)
                if r_api.get("adjusted_odds") is not None or r.get("match_type") == "no_game":
                    r = r_api
                    bookmakers = api_bk

        # Outright winner fallback (see fetch_odds) — pre-game only; the
        # started-game branch above returns before reaching here. Paid: no
        # free source carries outright/advance prices.
        if (r.get("adjusted_odds") is None and bet_type == "moneyline" and not free_only
                and _is_advance_or_outright(pick.get("description", ""))):
            ow = await _fetch_outright_winner(
                sport_key, teams[0] if teams else "", conn, current=True)
            if ow:
                r = ow

        # Extract BetOnline odds for both sides (for dashboard liability card)
        bol = None
        if bookmakers and r.get("adjusted_odds") is not None:
            team0 = teams[0] if teams else None
            direction = (pick.get("direction") or "").lower() or None
            pick_line_val = pick.get("line")
            if bet_type == "moneyline":
                bol = _betonline_both_sides(bookmakers, "h2h", pick_team=team0)
            elif bet_type == "spread":
                bol = _betonline_both_sides(bookmakers, "spreads", pick_team=team0,
                                            pick_line=float(pick_line_val) if pick_line_val is not None else None)
            elif bet_type in ("total", "over", "under"):
                bol = _betonline_both_sides(bookmakers, "totals", pick_direction=direction,
                                            pick_line=float(pick_line_val) if pick_line_val is not None else None)

        return OddsResult(
            match_type = r["match_type"],
            odds       = r["adjusted_odds"],
            bookmaker  = r["bookmaker"],
            api_line   = r["api_line"],
            pick_line  = r["pick_line"],
            game_date  = gd,
            betonline_sides = bol,
            commence_time = commence,
        )

    finally:
        conn.close()


async def _fetch_current_event_list(sport_key: str, conn: sqlite3.Connection) -> list[dict]:
    """Fetch upcoming events (live endpoint, no date param). Cached for 30 min."""
    cached = _get_events(conn, sport_key, "current")
    if cached is not None:
        return cached
    if not ODDS_API_KEY:
        return []
    async with httpx.AsyncClient(timeout=20) as http:
        data = await _api_get(http,
            f"{ODDS_API_BASE}/sports/{sport_key}/events",
            {"apiKey": ODDS_API_KEY},
        )
    events: list[dict] = data if isinstance(data, list) else []
    _save_events(conn, sport_key, "current", events)
    return events


async def _fetch_current_event_list_all(sport: str, conn: sqlite3.Connection) -> list[dict]:
    """Current event lists for every candidate sport key, merged (see _EXTRA_SPORT_KEYS)."""
    events: list[dict] = []
    for key in _sport_key_candidates(sport):
        events.extend(await _fetch_current_event_list(key, conn))
    return events


async def _fetch_current_bookmakers(
    sport_key: str, event_id: str, markets: str, conn: sqlite3.Connection,
    *, live: bool = False,
) -> list[dict]:
    """Fetch current odds for one event. live=True uses 5-min cache for in-progress games."""
    cache_key = "live" if live else "current"
    cached = _get_bookmakers(conn, event_id, cache_key, _cache_markets(markets))
    if cached is not None:
        return cached
    if not ODDS_API_KEY:
        return []
    async with httpx.AsyncClient(timeout=20) as http:
        data = await _api_get(http,
            f"{ODDS_API_BASE}/sports/{sport_key}/events/{event_id}/odds",
            {"apiKey": ODDS_API_KEY, "regions": REGIONS, "markets": markets, "oddsFormat": "american"},
        )
    bookmakers: list[dict] = data.get("bookmakers", []) if isinstance(data, dict) else []
    _save_bookmakers(conn, sport_key, event_id, cache_key, _cache_markets(markets), bookmakers)
    return bookmakers


# ── Bovada free fallback ──────────────────────────────────────────────────────
# The free second source when the Odds API produces no usable price and ESPN
# has nothing either (no CFL at all; no period or alternate markets anywhere).
# Born during the quota outage that started 2026-08-08: every CFL pick recorded
# no_game — the market call 401s while the free /events still matches the game.
# Bovada's public coupon JSON (no auth, no quota) carries game/period spreads,
# totals and moneylines including alternate lines; shaped like an Odds API
# bookmakers list, the existing lookups read it unchanged. Fallback only.
#
# Lacrosse (PLL) and KBO are not on Bovada (404/empty — probed 2026-08-22);
# those sports still have no free odds source.

_BOVADA_PATHS: dict[str, str] = {
    "CFL":   "football/cfl",
    "MLB":   "baseball/mlb",
    "NFL":   "football/nfl",            # includes preseason
    "NCAAF": "football/college-football",
    "NBA":   "basketball/nba",
    "WNBA":  "basketball/wnba",
    "NCAAB": "basketball/college-basketball",
    "NHL":   "hockey/nhl",
    "UFC":   "ufc-mma",                 # all MMA orgs, like the Odds API key
    "UFL":   "football/ufl",
}
_BOVADA_BASE = "https://www.bovada.lv/services/sports/event/v2/events/A/description"
_BOVADA_UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
_BOVADA_TTL, _BOVADA_FAIL_TTL = 60.0, 15.0
_bovada_cache: dict[str, tuple[float, list[dict]]] = {}

# Only these displayGroups hold line markets. The allowlist is the structural
# guard that keeps player props out: "Total Strikeouts - Logan Henderson (MIL)"
# reads exactly like a team total to any description rule, but it lives in
# "Pitcher Props" — and Game Props' "3-Way Moneyline" would land in h2h.
_BOVADA_LINE_GROUPS = {"game lines", "alternate lines", "fight odds"}

# Bovada name → the canonical name the parse and the Odds API use. Only pairs
# _team_matches cannot bridge belong here.
_BOVADA_TEAM_ALIASES = {"British Columbia Lions": "BC Lions"}

# Bovada period abbreviation → Odds API market suffix. B = bout (UFC's whole
# fight). Unmapped periods (RT = regulation time — a 3-way market the scoreline
# rules refuse anyway; 1I = 1st inning) are skipped entirely rather than
# mislabeled. NHL per-period abbreviations are deliberately absent: they could
# not be observed pregame (period markets appear close to game time), and a
# wrong mapping would mislabel a market where an absent one just skips it —
# add them from a real payload when the season starts.
_BOVADA_PERIOD_TO_SUFFIX = {"G": "", "B": "", "1H": "_h1", "2H": "_h2",
                            "Q1": "_q1", "Q2": "_q2", "Q3": "_q3", "Q4": "_q4"}

_BOVADA_PERIOD_TAG_RE = re.compile(r"\s*-\s*(?:1H|2H|1I|Q?[1-4]Q?|RT)$")


def _bovada_suffix(abbr: str | None, sport: str) -> str | None:
    """Market suffix for a Bovada period abbreviation, sport-aware for MLB.

    Bovada reuses "1H" for MLB's First 5 Innings, but the Odds API market the
    lookups read is spreads/totals/h2h_1st_5_innings (_MLB_PERIOD_SUFFIX), not
    *_h1 — mislabeling it _h1 would price an F5 pick off a market key the
    lookup never reads for MLB, or worse, collide with a real half if one
    existed. Mirrors _get_period_suffix's sport override.
    """
    if sport == "MLB" and abbr == "1H":
        return "_1st_5_innings"
    return _BOVADA_PERIOD_TO_SUFFIX.get(abbr)


def _bovada_team(name: str) -> str:
    """Strip Bovada's ' - 1H' style outcome suffix and canonicalize the name."""
    name = _BOVADA_PERIOD_TAG_RE.sub("", name or "").strip()
    return _BOVADA_TEAM_ALIASES.get(name, name)


def _bovada_american(a) -> int | None:
    if a is None:
        return None
    s = str(a).strip().upper()
    if s == "EVEN":
        return 100
    try:
        return int(s.lstrip("+"))
    except ValueError:
        return None


def _bovada_minimal_event(ev: dict) -> dict | None:
    """One Bovada event → the minimal Odds-API event shape _find_event_id reads."""
    comps = ev.get("competitors") or []
    home = next((c.get("name") for c in comps if c.get("home")), None)
    away = next((c.get("name") for c in comps if not c.get("home")), None)
    if not (home and away):
        return None
    try:
        commence = datetime.fromtimestamp(int(ev["startTime"]) / 1000, tz=timezone.utc)
    except (KeyError, TypeError, ValueError):
        return None
    return {"id": str(ev.get("id")),
            "home_team": _bovada_team(home), "away_team": _bovada_team(away),
            "commence_time": commence.strftime("%Y-%m-%dT%H:%M:%SZ")}


def _bovada_bookmakers(ev: dict, sport: str) -> list[dict]:
    """One Bovada event → an Odds-API-shaped bookmakers list.

    Only _BOVADA_LINE_GROUPS are read (the structural guard against player
    props and exotics — see the allowlist note). Market titles vary by sport:
    the spread is "Point Spread" (football/basketball), "Runline" (MLB) or
    "Puckline" (NHL); totals are "Total", "Total Runs O/U" or UFC's "Main
    Total Rounds Over/Under"; a " - <Team>" suffix on a total marks a TEAM
    total (outcomes carry the team in `description`, Over/Under in `name`,
    matching the Odds API's team_totals shape); UFC's moneyline is "Fight
    Winner". Alternate lines fold into the SAME key as the main market
    rather than an API-style alternate_* sibling: the lookups only read
    alternate_* where the Odds API sells it (alternate_spreads_h1 is not a
    thing there), so a 1H alt line shaped under alternate_spreads_h1 would
    be invisible — and for a single-book scrape "main vs alternate" is a
    request artifact, not a fact about the price. Suspended markets/outcomes
    (status != "O") are dropped — a frozen live price is worse than none.
    """
    markets: dict[str, list[dict]] = {}
    for grp in ev.get("displayGroups") or []:
        if (grp.get("description") or "").lower() not in _BOVADA_LINE_GROUPS:
            continue
        for mkt in grp.get("markets") or []:
            if mkt.get("status") not in (None, "O"):
                continue
            sfx = _bovada_suffix((mkt.get("period") or {}).get("abbreviation"), sport)
            if sfx is None:
                continue
            desc = mkt.get("description") or ""
            dlow = desc.lower()
            team_total_team = None
            is_total = dlow.startswith("total") or "total rounds" in dlow
            if dlow.startswith("moneyline") or dlow == "fight winner":
                key = "h2h" + sfx
            elif "spread" in dlow or dlow in ("runline", "puckline"):
                key = "spreads" + sfx
            elif is_total and " - " in desc:
                team_total_team = _bovada_team(desc.split(" - ", 1)[1])
                key = "team_totals" + sfx
            elif is_total:
                key = "totals" + sfx
            else:
                continue
            outs = []
            for o in mkt.get("outcomes") or []:
                if o.get("status") not in (None, "O"):
                    continue
                price = (o.get("price") or {})
                american = _bovada_american(price.get("american"))
                if american is None:
                    continue
                out = {"name": _bovada_team(o.get("description") or ""), "price": american}
                if price.get("handicap") is not None:
                    try:
                        out["point"] = float(price["handicap"])
                    except (TypeError, ValueError):
                        pass
                if team_total_team:
                    out["description"] = team_total_team
                outs.append(out)
            if outs:
                markets.setdefault(key, []).extend(outs)
    if not markets:
        return []
    return [{"key": "bovada", "title": "Bovada",
             "markets": [{"key": k, "outcomes": v} for k, v in markets.items()]}]


def _bovada_pick_event(
    events: list[dict], teams: list[str], *, allow_started: bool,
    now: datetime | None = None,
) -> tuple[dict | None, str | None]:
    """Match the pick's teams against Bovada's coupon; (event, eastern date).

    allow_started=False refuses an event past its start time: post-kickoff
    Bovada serves live prices, and the pregame caller would record one under a
    pregame-looking match_type — a mislabel nothing downstream could detect.
    The started/live caller passes True.
    """
    pairs = [(m, e) for e in events if (m := _bovada_minimal_event(e))]
    minimal = [m for m, _ in pairs]
    eid = _find_event_id(minimal, teams)
    if not eid:
        return None, None
    gd = _get_event_date(minimal, eid)
    match = next((m, e) for m, e in pairs if m["id"] == eid)
    if not allow_started:
        commence = datetime.fromisoformat(match[0]["commence_time"].replace("Z", "+00:00"))
        if commence <= (now or datetime.now(timezone.utc)):
            return None, gd
    return match[1], gd


async def _fetch_bovada_events(sport: str) -> list[dict]:
    """Bovada coupon events for a league, cached briefly. [] on any failure.

    Full period markets are only present pregame — near kickoff Bovada trims
    the coupon to game lines, and after it the prices are live.
    """
    path = _BOVADA_PATHS.get(sport)
    if not path:
        return []
    now = time.time()
    cached = _bovada_cache.get(sport)
    if cached is not None:
        ttl = _BOVADA_TTL if cached[1] else _BOVADA_FAIL_TTL
        if now - cached[0] < ttl:
            return cached[1]
    events: list[dict] = []
    try:
        async with httpx.AsyncClient(
            timeout=15, headers={"User-Agent": _BOVADA_UA, "Accept": "application/json"},
        ) as http:
            r = await http.get(f"{_BOVADA_BASE}/{path}", params={"lang": "en"})
            r.raise_for_status()
            for blk in r.json():
                events.extend(blk.get("events") or [])
    except Exception as exc:
        print(f"[odds] bovada fetch failed ({sport}): {exc}")
        events = []
    _bovada_cache[sport] = (now, events)
    return events


async def _fetch_bovada_bookmakers(
    sport: str, teams: list[str], *, allow_started: bool = False,
) -> tuple[list[dict], str | None]:
    """Shaped Bovada bookmakers for the pick's game, plus its eastern date."""
    events = await _fetch_bovada_events(sport)
    ev, gd = _bovada_pick_event(events, teams, allow_started=allow_started)
    return (_bovada_bookmakers(ev, sport) if ev else []), gd


# A Bovada match with no Odds API date to check it against is only trusted
# this many days out. Cappers post 0–5 days ahead (UFC cards routinely 5).
_BOVADA_MAX_DAYS_AHEAD = 7


def _bovada_result_acceptable(
    api_gd: str | None, bov_gd: str | None, now: datetime | None = None,
) -> bool:
    """Cross-source guard: the Bovada price must describe the API's game.

    Bovada and the Odds API match events independently, so their answers can
    be DIFFERENT games — the class this pipeline fears most, because a real
    price on the wrong game verifies clean everywhere downstream. Seen live
    while testing: a Patriots pick whose actual game (PHI @ NE preseason,
    tomorrow) is not on Bovada's coupon matched Bovada's only listed Patriots
    event — Week 1, 18 days out — and priced it, while game_date said
    tomorrow. When the API matched a game, Bovada must agree on the eastern
    date; when the API matched nothing, Bovada's game must at least be near
    (within _BOVADA_MAX_DAYS_AHEAD), which keeps the genuine coverage win —
    a card the API doesn't list this week — while refusing a far-future
    stand-in for a game the coupon lacks.
    """
    if api_gd is not None:
        return bov_gd == api_gd
    if bov_gd is None:
        return False
    cutoff = ((now or datetime.now(timezone.utc)).astimezone(ZoneInfo("America/New_York"))
              + timedelta(days=_BOVADA_MAX_DAYS_AHEAD)).strftime("%Y-%m-%d")
    return bov_gd <= cutoff


def quota_used() -> int:
    """Return the number of Odds API quota units consumed this process."""
    return _quota_used


def quota_exhausted() -> bool:
    """True once a call this process came back OUT_OF_USAGE_CREDIT.

    Latched rather than probed: the 401 is free and definitive, so the first
    failure tells us for the rest of the run without spending another request.
    """
    return _QUOTA_EXHAUSTED


async def quota_remaining() -> int | None:
    """Credits left in the current billing period, or None if unknown.

    Free to call: /v4/sports/ returns the usage headers at x-requests-last: 0.
    """
    if not ODDS_API_KEY:
        return None
    async with httpx.AsyncClient(timeout=20) as http:
        try:
            r = await http.get(f"{ODDS_API_BASE}/sports/", params={"apiKey": ODDS_API_KEY})
            rem = r.headers.get("x-requests-remaining", "")
            return int(rem) if rem.strip().lstrip("-").isdigit() else None
        except Exception:  # noqa: BLE001 - a failed probe must not block the caller
            return None
