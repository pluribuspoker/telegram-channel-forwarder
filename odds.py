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
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

import httpx
from dotenv import load_dotenv

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
    "NCAAB": "basketball_ncaab",
    "NFL":   "americanfootball_nfl",
    "NCAAF": "americanfootball_ncaaf",
    "MLB":   "baseball_mlb",
    "NHL":   "icehockey_nhl",
    "UFC":   "mma_mixed_martial_arts",
    "UFL":   "americanfootball_ufl",
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
    "h2h,spreads,totals,"
    "alternate_spreads,alternate_totals,"
    "team_totals,alternate_team_totals,"
    "h2h_h1,spreads_h1,totals_h1,"
    "h2h_h2,spreads_h2,totals_h2,"
    "h2h_q1,spreads_q1,totals_q1"
)

PREFERRED_BOOKS = [
    "draftkings", "fanduel", "betmgm", "caesars",
    "pointsbet", "williamhill_us", "barstool",
]

MAX_LINE_GAP = 1.5

HALF_POINT_COST: dict[str, float] = {
    "NFL":   0.022,
    "NCAAF": 0.020,
    "NBA":   0.022,
    "NCAAB": 0.020,
    "MLB":   0.020,
    "NHL":   0.020,
    "UFC":   0.000,
    "UFL":   0.022,
}

_PERIOD_RE = re.compile(
    r'\b(1h|2h|1st half|2nd half|first half|second half|'
    r'1q|2q|3q|4q|1st quarter|2nd quarter|3rd quarter|4th quarter)\b',
    re.IGNORECASE,
)

_PERIOD_SUFFIX: dict[str, str] = {
    "1h": "_h1", "2h": "_h2",
    "1q": "_q1", "2q": "_q2", "3q": "_q3", "4q": "_q4",
}

# ── OddsResult ────────────────────────────────────────────────────────────────

# Match types that are known structural gaps — expected and not worth flagging.
_STRUCTURAL_MISS_TYPES = {
    "team_total_unavailable",
    "player_prop_unavailable",
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
    conn.execute("DELETE FROM odds_cache   WHERE game_date != 'current' AND game_date < date('now', '-60 days')")
    conn.execute("DELETE FROM odds_cache   WHERE game_date  = 'current' AND fetched_at < datetime('now', '-2 days')")
    conn.execute("DELETE FROM events_cache WHERE game_date != 'current' AND game_date < date('now', '-60 days')")
    conn.execute("DELETE FROM events_cache WHERE game_date  = 'current' AND fetched_at < datetime('now', '-30 minutes')")
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


async def _api_get(http: httpx.AsyncClient, url: str, params: dict) -> dict | list | None:
    global _quota_remaining, _quota_used
    try:
        r = await http.get(url, params=params)
        r.raise_for_status()
        prev = int(_quota_remaining) if _quota_remaining and _quota_remaining.isdigit() else None
        _quota_remaining = r.headers.get("x-requests-remaining", _quota_remaining)
        curr = int(_quota_remaining) if _quota_remaining and _quota_remaining.isdigit() else None
        if prev is not None and curr is not None:
            _quota_used += prev - curr
        return r.json()
    except httpx.HTTPStatusError as exc:
        print(f"[odds] API {exc.response.status_code} {url.split('/')[-1]}: {exc.response.text[:120]}")
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


async def _fetch_bookmakers(
    sport_key: str, event_id: str, date: str, markets: str, conn: sqlite3.Connection
) -> list[dict]:
    cached = _get_bookmakers(conn, event_id, date, markets)
    if cached is not None:
        return cached
    if not ODDS_API_KEY:
        return []
    async with httpx.AsyncClient(timeout=20) as http:
        data = await _api_get(
            http,
            f"{ODDS_API_BASE}/historical/sports/{sport_key}/events/{event_id}/odds",
            {"apiKey": ODDS_API_KEY, "regions": "us", "markets": markets,
             "date": f"{date}T18:00:00Z", "oddsFormat": "american"},
        )
    bookmakers: list[dict] = []
    if isinstance(data, dict):
        bookmakers = data.get("data", {}).get("bookmakers", []) if "data" in data else data.get("bookmakers", [])
    _save_bookmakers(conn, sport_key, event_id, date, markets, bookmakers)
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
    for preferred in PREFERRED_BOOKS:
        for odds, bk in candidates:
            if bk == preferred:
                return odds, bk
    return candidates[0]


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


def _find_event_id(event_list: list[dict], teams: list[str]) -> str | None:
    scored: list[tuple[int, str]] = []
    for term in teams:
        t_lower = term.lower()
        for event in event_list:
            home = event.get("home_team", "")
            away = event.get("away_team", "")
            for side in (home, away):
                if _team_matches(t_lower, side.lower()):
                    scored.append((-len(side), event["id"]))
                    break
    if not scored:
        return None
    scored.sort(key=lambda x: x[0], reverse=True)
    return scored[0][1]


def _lookup_moneyline(bookmakers: list[dict], team: str, period: str = "game") -> dict:
    mkt = "h2h" + _PERIOD_SUFFIX.get(period, "")
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
    suffix   = _PERIOD_SUFFIX.get(period, "")
    main_mkt = "spreads" + suffix
    alt_mkt  = "alternate_spreads" if not suffix else None

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

    closest = min(all_lines, key=lambda x: abs(x[0] - pick_line))
    gap = abs(closest[0] - pick_line)

    if gap > MAX_LINE_GAP:
        return {"match_type": f"alt_line_gap_{gap:.1f}pts", "pick_line": pick_line,
                "api_line": closest[0], "computed_odds": None, "adjusted_odds": None, "bookmaker": None}

    adjusted = _adjust_for_gap(sport, closest[1], pick_line, closest[0], gap)
    return {"match_type": f"proximity_{gap:.1f}pts", "pick_line": pick_line,
            "api_line": closest[0], "computed_odds": closest[1], "adjusted_odds": adjusted, "bookmaker": closest[2]}


def _lookup_total(sport: str, bookmakers: list[dict], direction: str, pick_line: float, period: str = "game") -> dict:
    suffix       = _PERIOD_SUFFIX.get(period, "")
    main_mkt     = "totals" + suffix
    alt_mkt      = "alternate_totals" if not suffix else None
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

    closest = min(all_lines, key=lambda x: abs(x[0] - pick_line))
    gap = abs(closest[0] - pick_line)

    if gap > MAX_LINE_GAP:
        return {"match_type": f"alt_line_gap_{gap:.1f}pts", "pick_line": pick_line,
                "api_line": closest[0], "computed_odds": None, "adjusted_odds": None, "bookmaker": None}

    signed_pick = -pick_line if direction == "over" else pick_line
    signed_api  = -closest[0] if direction == "over" else closest[0]
    adjusted = _adjust_for_gap(sport, closest[1], signed_pick, signed_api, gap)
    return {"match_type": f"proximity_{gap:.1f}pts", "pick_line": pick_line,
            "api_line": closest[0], "computed_odds": closest[1], "adjusted_odds": adjusted, "bookmaker": closest[2]}


def _lookup_team_total(sport: str, bookmakers: list[dict], team: str, direction: str, pick_line: float) -> dict:
    """Look up team total odds (team_totals / alternate_team_totals markets).

    These markets use `description` for the team name and `name` for Over/Under,
    unlike game totals which use `name` for the outcome label only.
    """
    outcome_name = "Over" if direction == "over" else "Under"
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

    # Exact match on main market, then alternate
    for mkt_key, label in [("team_totals", "exact"), ("alternate_team_totals", "exact_alt")]:
        hits = _collect_team_total(mkt_key, line_filter=pick_line)
        if hits:
            odds, book = _pick_best([(price, bk) for _, price, bk in hits])
            return {"match_type": label, "pick_line": pick_line,
                    "api_line": pick_line, "computed_odds": odds, "adjusted_odds": odds, "bookmaker": book}

    # Proximity: gather all lines from both markets
    all_lines = []
    for mkt_key in ("alternate_team_totals", "team_totals"):
        for pt, price, bk in _collect_team_total(mkt_key):
            if pt is not None:
                all_lines.append((pt, price, bk))

    if not all_lines:
        return _empty

    closest = min(all_lines, key=lambda x: abs(x[0] - pick_line))
    gap = abs(closest[0] - pick_line)

    if gap > MAX_LINE_GAP:
        return {"match_type": f"alt_line_gap_{gap:.1f}pts", "pick_line": pick_line,
                "api_line": closest[0], "computed_odds": None, "adjusted_odds": None, "bookmaker": None}

    signed_pick = -pick_line if direction == "over" else pick_line
    signed_api  = -closest[0] if direction == "over" else closest[0]
    adjusted = _adjust_for_gap(sport, closest[1], signed_pick, signed_api, gap)
    return {"match_type": f"proximity_{gap:.1f}pts", "pick_line": pick_line,
            "api_line": closest[0], "computed_odds": closest[1], "adjusted_odds": adjusted, "bookmaker": closest[2]}


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

    if bet_type == "prop":
        return {"match_type": "player_prop_unavailable", "pick_line": line,
                "api_line": None, "computed_odds": None, "adjusted_odds": None, "bookmaker": None}

    if bet_type == "team_total":
        if line is None or not direction:
            return {"match_type": "missing_line_or_direction", "pick_line": line,
                    "api_line": None, "computed_odds": None, "adjusted_odds": None, "bookmaker": None}
        return _lookup_team_total(sport, bookmakers, teams[0] if teams else "", direction, float(line))

    if period == "game" and _PERIOD_RE.search(desc):
        m = _PERIOD_RE.search(desc)
        raw = m.group(1).lower().replace(" ", "").replace("st", "").replace("nd", "").replace("rd", "").replace("th", "")
        period = {"half": "1h", "1half": "1h", "2half": "2h",
                  "firsthalf": "1h", "secondhalf": "2h",
                  "quarter": "1q", "1quarter": "1q"}.get(raw, raw)

    if not bookmakers:
        return {"match_type": "no_game", "pick_line": line,
                "api_line": None, "computed_odds": None, "adjusted_odds": None, "bookmaker": None}

    if bet_type == "moneyline":
        return _lookup_moneyline(bookmakers, teams[0] if teams else "", period)

    if bet_type == "spread":
        if line is None:
            return {"match_type": "no_line_in_pick", "pick_line": None,
                    "api_line": None, "computed_odds": None, "adjusted_odds": None, "bookmaker": None}
        return _lookup_spread(sport, bookmakers, teams[0] if teams else "", float(line), period)

    if bet_type == "total":
        if line is None or not direction:
            return {"match_type": "missing_line_or_direction", "pick_line": line,
                    "api_line": None, "computed_odds": None, "adjusted_odds": None, "bookmaker": None}
        return _lookup_total(sport, bookmakers, direction, float(line), period)

    return {"match_type": f"unsupported_bet_type({bet_type})", "pick_line": line,
            "api_line": None, "computed_odds": None, "adjusted_odds": None, "bookmaker": None}


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

        # ── Player props: separate endpoint ───────────────────────────────────
        if bet_type == "prop":
            prop_stat   = (pick.get("prop_stat") or "").upper()
            prop_market = PROP_STAT_MARKETS.get(sport, {}).get(prop_stat)
            if not prop_market:
                return OddsResult(match_type=f"prop_stat_unsupported({prop_stat})", pick_line=pick.get("line"))

            event_list = await _fetch_event_list(sport_key, game_date, conn)
            event_id   = _find_event_id(event_list, teams)
            if not event_id:
                return OddsResult(match_type="no_game", pick_line=pick.get("line"))

            bookmakers = await _fetch_bookmakers(sport_key, event_id, game_date, prop_market, conn)
            r = _lookup_prop(bookmakers, pick.get("player") or "", prop_market,
                             pick.get("direction") or "over", float(pick.get("line") or 0.5))
            return OddsResult(
                match_type  = r["match_type"],
                odds        = r["adjusted_odds"],
                bookmaker   = r["bookmaker"],
                api_line    = r["api_line"],
                pick_line   = r["pick_line"],
            )

        # ── All other bet types ───────────────────────────────────────────────
        bookmakers: list[dict] = []

        # ESPN first (free, pre-game only — odds cleared after game completion)
        if sport in ESPN_LEAGUES:
            espn_data = await fetch_espn(sport, game_date)
            if espn_data:
                bookmakers = espn_bookmakers_for_teams(espn_data, teams)

        # Odds API fallback (historical closing odds + alternate lines)
        if not bookmakers:
            event_list = await _fetch_event_list(sport_key, game_date, conn)
            event_id   = _find_event_id(event_list, teams)
            if event_id:
                bookmakers = await _fetch_bookmakers(sport_key, event_id, game_date, MARKETS_FULL, conn)

        r = lookup_pick_odds(sport, pick, bookmakers)
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


async def fetch_odds_current(sport: str, pick: dict, db_path: str = DB_PATH) -> OddsResult:
    """
    Look up current (live pre-game) odds for a pick.

    Uses the non-historical Odds API endpoint — no date parameter.
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

        # ── Player props: separate endpoint ───────────────────────────────────
        if bet_type == "prop":
            prop_stat   = (pick.get("prop_stat") or "").upper()
            prop_market = PROP_STAT_MARKETS.get(sport, {}).get(prop_stat)
            if not prop_market:
                return OddsResult(match_type=f"prop_stat_unsupported({prop_stat})", pick_line=pick.get("line"))
            event_list = await _fetch_current_event_list(sport_key, conn)
            event_id   = _find_event_id(event_list, teams)
            if not event_id:
                return OddsResult(match_type="no_game", pick_line=pick.get("line"))
            if _event_already_started(event_list, event_id):
                return OddsResult(match_type="game_in_progress", pick_line=pick.get("line"))
            bookmakers = await _fetch_current_bookmakers(sport_key, event_id, prop_market, conn)
            r = _lookup_prop(bookmakers, pick.get("player") or "", prop_market,
                             pick.get("direction") or "over", float(pick.get("line") or 0.5))
            return OddsResult(
                match_type = r["match_type"],
                odds       = r["adjusted_odds"],
                bookmaker  = r["bookmaker"],
                api_line   = r["api_line"],
                pick_line  = r["pick_line"],
            )

        # ── All other bet types ───────────────────────────────────────────────
        bookmakers: list[dict] = []

        # ESPN first (free, only has pre-game odds)
        if sport in ESPN_LEAGUES:
            from datetime import date as _d
            espn_data = await fetch_espn(sport, _d.today().isoformat())
            if espn_data:
                bookmakers = espn_bookmakers_for_teams(espn_data, teams)

        if not bookmakers:
            event_list = await _fetch_current_event_list(sport_key, conn)
            event_id   = _find_event_id(event_list, teams)
            if event_id:
                if _event_already_started(event_list, event_id):
                    return OddsResult(match_type="game_in_progress", pick_line=pick.get("line"))
                bookmakers = await _fetch_current_bookmakers(sport_key, event_id, MARKETS_FULL, conn)

        r = lookup_pick_odds(sport, pick, bookmakers)
        return OddsResult(
            match_type = r["match_type"],
            odds       = r["adjusted_odds"],
            bookmaker  = r["bookmaker"],
            api_line   = r["api_line"],
            pick_line  = r["pick_line"],
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


async def _fetch_current_bookmakers(
    sport_key: str, event_id: str, markets: str, conn: sqlite3.Connection
) -> list[dict]:
    """Fetch current odds for one event (live endpoint). Cached for 2 days."""
    cached = _get_bookmakers(conn, event_id, "current", markets)
    if cached is not None:
        return cached
    if not ODDS_API_KEY:
        return []
    async with httpx.AsyncClient(timeout=20) as http:
        data = await _api_get(http,
            f"{ODDS_API_BASE}/sports/{sport_key}/events/{event_id}/odds",
            {"apiKey": ODDS_API_KEY, "regions": "us", "markets": markets, "oddsFormat": "american"},
        )
    bookmakers: list[dict] = data.get("bookmakers", []) if isinstance(data, dict) else []
    _save_bookmakers(conn, sport_key, event_id, "current", markets, bookmakers)
    return bookmakers


def quota_used() -> int:
    """Return the number of Odds API quota units consumed this process."""
    return _quota_used
