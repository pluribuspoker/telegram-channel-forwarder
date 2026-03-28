#!/usr/bin/env python3
"""
scripts/audit_odds.py — Backtest odds lookup against graded picks.

Reads result.json and/or result_df.json, parses picks via Claude,
fetches closing odds from the Odds API, and outputs a CSV for analysis.

Uses a two-step approach that unlocks alternate lines and period markets
on all paid Odds API plans:
  Step 1: GET /historical/sports/{sport}/events  →  list of event IDs (cheap)
  Step 2: GET /historical/sports/{sport}/events/{id}/odds  →  full markets per event

Markets fetched per event: h2h, spreads, totals, alternate_spreads, alternate_totals,
  h2h_h1, spreads_h1, totals_h1, h2h_h2, spreads_h2, totals_h2,
  h2h_q1, spreads_q1, totals_q1

Proximity matching (gap <= MAX_LINE_GAP) applies a half-point price adjustment.

Usage:
  python scripts/audit_odds.py
  python scripts/audit_odds.py --days-back 30        # only picks from last 30 days
  python scripts/audit_odds.py --sport NBA            # filter to one sport
  python scripts/audit_odds.py --dry-run              # parse only, no Odds API calls
  python scripts/audit_odds.py --out data/my.csv
"""

import argparse
import asyncio
import csv
import hashlib
import json
import os
import re
import sys
from datetime import date as _date, timedelta
from pathlib import Path

import httpx
from dotenv import load_dotenv

# ── Project root on path ─────────────────────────────────────────────────────
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from ai import claude_parse
from scores import _team_matches, fetch_espn, espn_bookmakers_for_teams, ESPN_LEAGUES

load_dotenv(ROOT / ".env")
load_dotenv(ROOT / ".env.local", override=True)

# ── Config ───────────────────────────────────────────────────────────────────

ODDS_API_KEY  = os.getenv("ODDS_API_KEY", "")
ODDS_API_BASE = "https://api.the-odds-api.com/v4"

# Our sport names → Odds API sport keys
SPORT_KEYS: dict[str, str] = {
    "NBA":   "basketball_nba",
    "NCAAB": "basketball_ncaab",
    "NFL":   "americanfootball_nfl",
    "NCAAF": "americanfootball_ncaaf",
    "MLB":   "baseball_mlb",
    "NHL":   "icehockey_nhl",
    "UFC":   "mma_mixed_martial_arts",
}

# Markets to fetch per event (per-event endpoint supports all of these)
MARKETS_FULL = (
    "h2h,spreads,totals,"
    "alternate_spreads,alternate_totals,"
    "h2h_h1,spreads_h1,totals_h1,"
    "h2h_h2,spreads_h2,totals_h2,"
    "h2h_q1,spreads_q1,totals_q1"
)

# Preferred bookmakers in priority order
PREFERRED_BOOKS = [
    "draftkings", "fanduel", "betmgm", "caesars",
    "pointsbet", "williamhill_us", "barstool",
]

# Proximity: max gap in pts before we give up
MAX_LINE_GAP = 1.5

# Implied probability cost per half-point of line movement.
# Calibrated so that 1 half-point ≈ 10 cents of juice at standard -110 pricing.
# (-110 implied prob = 52.38%; -120 = 54.55%; delta ≈ 0.022 per half pt)
# NFL key numbers (3, 7) cost 2-3x more to cross but we use the flat rate here.
HALF_POINT_COST: dict[str, float] = {
    "NFL":   0.022,
    "NCAAF": 0.020,
    "NBA":   0.022,
    "NCAAB": 0.020,
    "MLB":   0.020,
    "NHL":   0.020,
    "UFC":   0.000,  # moneyline only, no line gap
}

# Regex to detect period-specific bets (1H, 2H, Q1, etc.) in description
_PERIOD_RE = re.compile(
    r'\b(1h|2h|1st half|2nd half|first half|second half|'
    r'1q|2q|3q|4q|1st quarter|2nd quarter|3rd quarter|4th quarter)\b',
    re.IGNORECASE,
)

# ── Paths ────────────────────────────────────────────────────────────────────

DATA_DIR         = ROOT / "data"
ODDS_CACHE_FILE  = DATA_DIR / "odds_api_cache.json"
PARSE_CACHE_FILE = DATA_DIR / "audit_parse_cache.json"

# ── Text helpers ─────────────────────────────────────────────────────────────

EMOJI_VERDICT = {"✅": "WIN", "❌": "LOSS"}


def _flat_text(msg: dict) -> str:
    t = msg.get("text", "")
    if isinstance(t, list):
        return "".join(e.get("text", "") if isinstance(e, dict) else e for e in t)
    return str(t)


def _extract_verdict(text: str) -> str | None:
    for emoji, verdict in EMOJI_VERDICT.items():
        if emoji in text:
            return verdict
    return None


def _extract_stated_odds(text: str) -> str | None:
    """Extract first American odds string from text, e.g. -145, +220."""
    m = re.search(r'([+-]\d{3,4})(?:\s|$|\n|✅|❌)', text)
    return m.group(1) if m else None


def _text_hash(text: str) -> str:
    return hashlib.md5(text.encode()).hexdigest()[:12]


# ── Price adjustment ──────────────────────────────────────────────────────────

def _american_to_prob(odds: int) -> float:
    """Convert American odds to implied probability (no vig)."""
    if odds > 0:
        return 100 / (odds + 100)
    return abs(odds) / (abs(odds) + 100)


def _prob_to_american(prob: float) -> int:
    """Convert implied probability to American odds."""
    prob = max(0.01, min(0.99, prob))
    if prob >= 0.5:
        return round(-(prob / (1 - prob)) * 100)
    return round((1 - prob) / prob * 100)


def _adjust_for_gap(sport: str, base_odds: int, pick_line: float, api_line: float, gap: float) -> int:
    """
    Estimate what the capper likely paid for their line given the main-line price.

    Higher numerical line is always better for the bettor regardless of sign:
      - Underdog: +3 > +2.5  (easier to cover)
      - Favorite: -9.5 > -11.5  (less points to cover; -9.5 is the higher number)

    If capper has the better number → they bought points → pay more juice (prob goes up).
    If capper has the worse number  → they sold points → pay less juice (prob goes down).

    gap: absolute pts difference between pick_line and api_line.
    """
    cost = HALF_POINT_COST.get(sport, 0.022)
    n_half_pts = gap / 0.5
    prob = _american_to_prob(base_odds)

    capper_got_better = pick_line > api_line
    if capper_got_better:
        adjusted = prob + n_half_pts * cost   # paid more juice for better number
    else:
        adjusted = prob - n_half_pts * cost   # got better juice for worse number

    return _prob_to_american(adjusted)


# ── Odds API ─────────────────────────────────────────────────────────────────

_odds_cache: dict = {}
_quota_remaining: str | None = None


def _load_odds_cache() -> None:
    global _odds_cache
    if ODDS_CACHE_FILE.exists():
        _odds_cache = json.loads(ODDS_CACHE_FILE.read_text(encoding="utf-8"))


def _save_odds_cache() -> None:
    ODDS_CACHE_FILE.write_text(json.dumps(_odds_cache, indent=2), encoding="utf-8")


async def _api_get(http: httpx.AsyncClient, url: str, params: dict) -> dict | list | None:
    """Single GET with error handling; updates quota counter."""
    global _quota_remaining
    try:
        r = await http.get(url, params=params)
        r.raise_for_status()
        _quota_remaining = r.headers.get("x-requests-remaining", _quota_remaining)
        return r.json()
    except httpx.HTTPStatusError as exc:
        print(f"  [Odds API {exc.response.status_code}] {url.split('/')[-1]}: {exc.response.text[:120]}", file=sys.stderr)
        return None
    except Exception as exc:
        print(f"  [Odds API error] {url}: {exc}", file=sys.stderr)
        return None


async def fetch_event_list(sport_key: str, date: str) -> list[dict]:
    """Fetch the list of events for sport_key on date (step 1, cheap).

    Returns list of {id, home_team, away_team, commence_time}.
    Cached by (sport_key, date).
    """
    cache_key = f"events:{sport_key}:{date}"
    if cache_key in _odds_cache:
        return _odds_cache[cache_key]

    if not ODDS_API_KEY:
        return []

    async with httpx.AsyncClient(timeout=20) as http:
        data = await _api_get(http,
            f"{ODDS_API_BASE}/historical/sports/{sport_key}/events",
            {"apiKey": ODDS_API_KEY, "date": f"{date}T18:00:00Z"},
        )

    events: list[dict] = (data or {}).get("data", []) if isinstance(data, dict) else []
    _odds_cache[cache_key] = events
    _save_odds_cache()
    print(f"  [events] {sport_key} {date} -> {len(events)}  (quota: {_quota_remaining})", file=sys.stderr)
    return events


async def fetch_event_odds(sport_key: str, event_id: str, date: str) -> list[dict]:
    """Fetch full odds (including alternate lines + period markets) for one event (step 2).

    Returns the bookmakers list for that event.
    Cached by event_id.
    """
    cache_key = f"event_odds:{event_id}:{date}"
    if cache_key in _odds_cache:
        return _odds_cache[cache_key]

    if not ODDS_API_KEY:
        return []

    async with httpx.AsyncClient(timeout=20) as http:
        data = await _api_get(http,
            f"{ODDS_API_BASE}/historical/sports/{sport_key}/events/{event_id}/odds",
            {
                "apiKey":     ODDS_API_KEY,
                "regions":    "us",
                "markets":    MARKETS_FULL,
                "date":       f"{date}T18:00:00Z",
                "oddsFormat": "american",
            },
        )

    bookmakers: list[dict] = []
    if isinstance(data, dict):
        bookmakers = data.get("data", {}).get("bookmakers", []) if "data" in data else data.get("bookmakers", [])

    _odds_cache[cache_key] = bookmakers
    _save_odds_cache()
    mkt_keys = {m["key"] for bk in bookmakers for m in bk.get("markets", [])}
    print(f"  [event_odds] {event_id[:8]}.. -> {len(bookmakers)} books, markets: {sorted(mkt_keys)}  (quota: {_quota_remaining})", file=sys.stderr)
    return bookmakers


# ── Odds matching helpers ─────────────────────────────────────────────────────

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
    """Collect (point, price, bookmaker) from a market across all bookmakers."""
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
    """Find the best-matching event ID from an event list for the given team names.

    Prefers shorter team names (more specific match) to avoid 'Tennessee' matching
    'Tennessee St Tigers' before 'Tennessee Volunteers'.
    """
    scored: list[tuple[int, str]] = []  # (score, event_id)
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


# Period → market key suffix mapping
_PERIOD_SUFFIX: dict[str, str] = {
    "1h": "_h1", "2h": "_h2",
    "1q": "_q1", "2q": "_q2", "3q": "_q3", "4q": "_q4",
}


def _lookup_moneyline(bookmakers: list[dict], team: str, period: str = "game") -> dict:
    mkt = "h2h" + _PERIOD_SUFFIX.get(period, "")
    candidates = [(price, bk) for _, price, bk in _collect_outcomes(bookmakers, mkt, name_filter=team)]
    odds, book = _pick_best(candidates)
    return {
        "game_found":    True,
        "match_type":    "exact" if odds is not None else f"no_{mkt}_data",
        "pick_line":     None,
        "api_line":      None,
        "computed_odds": odds,
        "adjusted_odds": odds,
        "bookmaker":     book,
    }


def _lookup_spread(sport: str, bookmakers: list[dict], team: str, pick_line: float, period: str = "game") -> dict:
    suffix = _PERIOD_SUFFIX.get(period, "")
    main_mkt = "spreads" + suffix
    alt_mkt  = "alternate_spreads" if not suffix else None  # alt markets only for full game

    _empty = {"game_found": True, "match_type": f"no_spread_data", "pick_line": pick_line,
              "api_line": None, "computed_odds": None, "adjusted_odds": None, "bookmaker": None}

    # Exact match in main, then alternate
    for mkt in filter(None, [main_mkt, alt_mkt]):
        hits = _collect_outcomes(bookmakers, mkt, name_filter=team, line_filter=pick_line)
        if hits:
            odds, book = _pick_best([(price, bk) for _, price, bk in hits])
            label = "exact" if mkt == main_mkt else "exact_alt"
            return {"game_found": True, "match_type": label, "pick_line": pick_line,
                    "api_line": pick_line, "computed_odds": odds, "adjusted_odds": odds, "bookmaker": book}

    # Gather all available lines (same-sign filter to avoid matching opponent)
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
        return {"game_found": True, "match_type": f"alt_line_gap_{gap:.1f}pts", "pick_line": pick_line,
                "api_line": closest[0], "computed_odds": None, "adjusted_odds": None, "bookmaker": None}

    adjusted = _adjust_for_gap(sport, closest[1], pick_line, closest[0], gap)
    return {"game_found": True, "match_type": f"proximity_{gap:.1f}pts", "pick_line": pick_line,
            "api_line": closest[0], "computed_odds": closest[1], "adjusted_odds": adjusted, "bookmaker": closest[2]}


def _lookup_total(sport: str, bookmakers: list[dict], direction: str, pick_line: float, period: str = "game") -> dict:
    suffix = _PERIOD_SUFFIX.get(period, "")
    main_mkt = "totals" + suffix
    alt_mkt  = "alternate_totals" if not suffix else None
    outcome_name = "Over" if direction == "over" else "Under"

    _empty = {"game_found": True, "match_type": "no_total_data", "pick_line": pick_line,
              "api_line": None, "computed_odds": None, "adjusted_odds": None, "bookmaker": None}

    for mkt in filter(None, [main_mkt, alt_mkt]):
        hits = _collect_outcomes(bookmakers, mkt, name_filter=outcome_name, line_filter=pick_line)
        if hits:
            odds, book = _pick_best([(price, bk) for _, price, bk in hits])
            label = "exact" if mkt == main_mkt else "exact_alt"
            return {"game_found": True, "match_type": label, "pick_line": pick_line,
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
        return {"game_found": True, "match_type": f"alt_line_gap_{gap:.1f}pts", "pick_line": pick_line,
                "api_line": closest[0], "computed_odds": None, "adjusted_odds": None, "bookmaker": None}

    signed_pick = -pick_line if direction == "over" else pick_line
    signed_api  = -closest[0] if direction == "over" else closest[0]
    adjusted = _adjust_for_gap(sport, closest[1], signed_pick, signed_api, gap)
    return {"game_found": True, "match_type": f"proximity_{gap:.1f}pts", "pick_line": pick_line,
            "api_line": closest[0], "computed_odds": closest[1], "adjusted_odds": adjusted, "bookmaker": closest[2]}


def lookup_pick_odds(sport: str, pick: dict, bookmakers: list[dict]) -> dict:
    """Given a parsed pick and the event's bookmakers list, find the best odds match."""
    teams     = pick.get("teams") or []
    bet_type  = pick.get("bet_type", "")
    line      = pick.get("line")
    direction = pick.get("direction")
    period    = pick.get("period", "game")
    desc      = pick.get("description", "")

    _no_game = {"game_found": False, "match_type": "no_game",
                "pick_line": line, "api_line": None,
                "computed_odds": None, "adjusted_odds": None, "bookmaker": None}

    # Props: player_props endpoint not fetched here
    if bet_type == "prop":
        return {**_no_game, "game_found": True, "match_type": "player_prop_unavailable"}

    # Team totals: not in standard totals market
    if bet_type == "team_total":
        return {**_no_game, "game_found": True, "match_type": "team_total_unavailable"}

    # Normalise period from pick field + description regex
    if period == "game" and _PERIOD_RE.search(desc):
        m = _PERIOD_RE.search(desc)
        raw = m.group(1).lower().replace(" ", "").replace("st", "").replace("nd", "").replace("rd", "").replace("th", "")
        period = {"half": "1h", "1half": "1h", "2half": "2h",
                  "firsthalf": "1h", "secondhalf": "2h",
                  "quarter": "1q", "1quarter": "1q"}.get(raw, raw)

    if not bookmakers:
        return _no_game

    if bet_type == "moneyline":
        return _lookup_moneyline(bookmakers, teams[0] if teams else "", period)

    if bet_type == "spread":
        if line is None:
            return {**_no_game, "game_found": True, "match_type": "no_line_in_pick"}
        return _lookup_spread(sport, bookmakers, teams[0] if teams else "", float(line), period)

    if bet_type == "total":
        if line is None or not direction:
            return {**_no_game, "game_found": True, "match_type": "missing_line_or_direction"}
        return _lookup_total(sport, bookmakers, direction, float(line), period)

    return {**_no_game, "game_found": True, "match_type": f"unsupported_bet_type({bet_type})"}


# ── Parse cache ───────────────────────────────────────────────────────────────

_parse_cache: dict = {}


def _load_parse_cache() -> None:
    global _parse_cache
    if PARSE_CACHE_FILE.exists():
        _parse_cache = json.loads(PARSE_CACHE_FILE.read_text(encoding="utf-8"))


def _save_parse_cache() -> None:
    PARSE_CACHE_FILE.write_text(json.dumps(_parse_cache, indent=2), encoding="utf-8")


async def get_parsed(text: str) -> dict | None:
    key = _text_hash(text)
    if key in _parse_cache:
        return _parse_cache[key]
    result = await claude_parse(text)
    if result:
        _parse_cache[key] = result
        _save_parse_cache()
    return result


# ── Message loading ───────────────────────────────────────────────────────────

def load_graded_messages(paths: list[Path], days_back: int | None) -> list[dict]:
    """Load graded (emoji-containing) messages from Telegram export files."""
    cutoff = (_date.today() - timedelta(days=days_back)).isoformat() if days_back else None
    rows = []
    for path in paths:
        if not path.exists():
            print(f"  [skip] {path} not found", file=sys.stderr)
            continue
        data = json.loads(path.read_text(encoding="utf-8"))
        for msg in data.get("messages", []):
            if msg.get("type") != "message":
                continue
            text    = _flat_text(msg)
            verdict = _extract_verdict(text)
            if not verdict:
                continue
            date = msg["date"][:10]
            if cutoff and date < cutoff:
                continue
            rows.append({
                "source_file": path.name,
                "msg_id":      msg.get("id"),
                "date":        date,
                "capper":      msg.get("from", ""),
                "text":        text,
                "verdict":     verdict,
                "stated_odds": _extract_stated_odds(text),
            })
    return rows


# ── Main ─────────────────────────────────────────────────────────────────────

CSV_FIELDS = [
    "source_file", "date", "capper", "sport", "bet_type", "description",
    "verdict", "stated_odds", "pick_line", "api_line", "computed_odds", "adjusted_odds",
    "match_type", "bookmaker", "game_found", "notes",
]


async def main(
    files: list[Path],
    out_path: Path,
    dry_run: bool,
    sport_filter: str | None,
    days_back: int | None,
) -> None:
    _load_odds_cache()
    _load_parse_cache()

    messages = load_graded_messages(files, days_back)
    print(f"Loaded {len(messages)} graded messages from {len(files)} file(s)", file=sys.stderr)
    if days_back:
        print(f"(filtered to last {days_back} days)", file=sys.stderr)

    rows: list[dict] = []

    for i, msg in enumerate(messages, 1):
        print(f"[{i}/{len(messages)}] {msg['date']}  {msg['capper'][:25]}", file=sys.stderr)

        parsed = await get_parsed(msg["text"])
        if not parsed:
            rows.append({**{f: "" for f in CSV_FIELDS},
                         "source_file": msg["source_file"],
                         "date": msg["date"], "capper": msg["capper"],
                         "verdict": msg["verdict"], "stated_odds": msg["stated_odds"],
                         "notes": "parse_failed"})
            continue

        top_sport = parsed.get("sport", "")
        picks     = parsed.get("picks", [])

        for pick in picks:
            pick_sport = pick.get("sport") or top_sport

            if sport_filter and pick_sport != sport_filter:
                continue

            sport_key = SPORT_KEYS.get(pick_sport)
            notes = "" if sport_key else f"sport_unsupported({pick_sport})"

            bookmakers: list[dict] = []
            espn_fallback = False
            if not dry_run and sport_key:
                # Step 1: get event list for this sport+date (cheap, cached)
                event_list = await fetch_event_list(sport_key, msg["date"])
                # Step 2: find matching event, fetch its full odds (per-event, cached)
                event_id = _find_event_id(event_list, pick.get("teams") or [])
                if event_id:
                    bookmakers = await fetch_event_odds(sport_key, event_id, msg["date"])

                # ESPN fallback: free, works pre-game only (odds cleared after completion)
                if not bookmakers and pick_sport in ESPN_LEAGUES:
                    espn_data = await fetch_espn(pick_sport, msg["date"])
                    if espn_data:
                        bookmakers = espn_bookmakers_for_teams(espn_data, pick.get("teams") or [])
                        espn_fallback = bool(bookmakers)

            result = (
                lookup_pick_odds(pick_sport, pick, bookmakers)
                if (bookmakers or (sport_key and not dry_run))
                else {
                    "game_found":    False,
                    "match_type":    "dry_run" if dry_run else ("sport_unsupported" if not sport_key else "no_game"),
                    "pick_line":     pick.get("line"),
                    "api_line":      None,
                    "computed_odds": None,
                    "adjusted_odds": None,
                    "bookmaker":     None,
                }
            )

            rows.append({
                "source_file":   msg["source_file"],
                "date":          msg["date"],
                "capper":        msg["capper"],
                "sport":         pick_sport,
                "bet_type":      pick.get("bet_type", ""),
                "description":   pick.get("description", ""),
                "verdict":       msg["verdict"],
                "stated_odds":   msg["stated_odds"] if len(picks) == 1 else "",
                "pick_line":     result.get("pick_line", ""),
                "api_line":      result.get("api_line", ""),
                "computed_odds": result.get("computed_odds", ""),
                "adjusted_odds": result.get("adjusted_odds", ""),
                "match_type":    result.get("match_type", ""),
                "bookmaker":     result.get("bookmaker", ""),
                "game_found":    result.get("game_found", False),
                "notes":         ("espn_odds" if espn_fallback else "") or notes,
            })

    # Output CSV
    with open(out_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=CSV_FIELDS)
        writer.writeheader()
        writer.writerows(rows)

    # Summary
    total = len(rows)
    found = sum(1 for r in rows if r.get("computed_odds") not in ("", None))
    exact = sum(1 for r in rows if str(r.get("match_type", "")).startswith("exact"))
    prox  = sum(1 for r in rows if str(r.get("match_type", "")).startswith("proximity"))
    unavail = sum(1 for r in rows if "unavailable" in str(r.get("match_type", "")))
    alt_gap = sum(1 for r in rows if str(r.get("match_type", "")).startswith("alt_line_gap"))

    print(file=sys.stderr)
    prop_unavail = sum(1 for r in rows if "prop_unavailable" in str(r.get("match_type", "")))
    print("--- Coverage summary -----------------------------------------", file=sys.stderr)
    print(f"  Total picks:                {total}", file=sys.stderr)
    print(f"  Odds found:                 {found}  ({100*found//total if total else 0}%)", file=sys.stderr)
    print(f"    exact:                    {exact}", file=sys.stderr)
    print(f"    exact_alt (alt line):     {sum(1 for r in rows if r.get('match_type') == 'exact_alt')}", file=sys.stderr)
    print(f"    proximity w/ adjustment:  {prox}", file=sys.stderr)
    print(f"  Alt line gap > {MAX_LINE_GAP}pts:          {alt_gap}", file=sys.stderr)
    print(f"  Player props (unavailable): {prop_unavail}", file=sys.stderr)
    if _quota_remaining is not None:
        print(f"  API quota remaining:        {_quota_remaining}", file=sys.stderr)
    print(f"\n  Output: {out_path}  ({total} rows)", file=sys.stderr)


if __name__ == "__main__":
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--files", nargs="+", type=Path,
                   default=[DATA_DIR / "result.json", DATA_DIR / "result_df.json"],
                   help="Telegram export JSON files")
    p.add_argument("--out", type=Path, default=DATA_DIR / "odds_audit.csv",
                   help="Output CSV path (default: data/odds_audit.csv)")
    p.add_argument("--dry-run", action="store_true",
                   help="Parse picks only, skip Odds API calls")
    p.add_argument("--sport", default=None, metavar="SPORT",
                   help="Only process picks for this sport (e.g. NBA, UFC)")
    p.add_argument("--days-back", type=int, default=None, metavar="N",
                   help="Only include picks from last N days")
    args = p.parse_args()

    asyncio.run(main(args.files, args.out, args.dry_run, args.sport, args.days_back))
