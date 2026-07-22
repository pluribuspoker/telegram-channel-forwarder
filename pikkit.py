"""
pikkit.py — Fetch betting splits from Pikkit's API.

Provides event discovery (by date/sport) and community splits
(bet %, handle %) for matched events.  Token stored in PIKKIT_TOKEN env var.
"""

import os
import re
import logging
import time
import urllib.parse
import urllib.request
from datetime import date as _date
from typing import Any

import httpx

log = logging.getLogger(__name__)

BASE = "https://prod-website.pikkit.app"
_TIMEOUT = 15  # seconds

# ── In-memory cache (per-process) ────────────────────────────────────────

_events_cache: dict[str, dict[str, list[dict]]] = {}   # date → sport → [event]
_splits_cache: dict[str, dict] = {}                     # event_id → splits dict
_last_401_alert: float = 0  # epoch — rate-limit alerts to once per hour


def _alert_token_expired() -> None:
    """Send a one-time-per-hour Telegram alert when the Pikkit token expires."""
    global _last_401_alert
    now = time.time()
    if now - _last_401_alert < 3600:
        return
    _last_401_alert = now
    token = os.getenv("WATCHDOG_BOT_TOKEN", "")
    uid = os.getenv("WATCHDOG_USER_ID", "")
    if not token or not uid:
        return
    text = "\U0001f6a8 Pikkit token expired (401). Update PIKKIT_TOKEN in .env and restart."
    data = urllib.parse.urlencode({"chat_id": uid, "text": text}).encode()
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    try:
        urllib.request.urlopen(urllib.request.Request(url, data=data), timeout=10)
    except Exception:
        pass


# ── Sport mapping ────────────────────────────────────────────────────────
# Maps our internal sport names → Pikkit league header values
_SPORT_TO_LEAGUES: dict[str, list[str]] = {
    "MLB":    ["MLB"],
    "NBA":    ["NBA"],
    "NFL":    ["NFL"],
    "NHL":    ["NHL"],
    "WNBA":  ["WNBA"],
    "MLS":   ["MLS"],
    "Soccer": ["MLS", "EPL", "La Liga", "Serie A", "Bundesliga", "Ligue 1",
               "Champions League", "UEFA", "Copa America", "World Cup"],
    "CFB":   ["NCAAF", "CFB"],
    "CBB":   ["NCAAB", "CBB"],
}


def _token() -> str | None:
    return os.getenv("PIKKIT_TOKEN")


def _headers() -> dict[str, str]:
    tok = _token()
    if not tok:
        return {}
    return {
        "Authorization": tok,
        "Accept": "application/json, text/plain, */*",
        "Origin": "https://app.pikkit.com",
    }


# ── Fetch events for a date ─────────────────────────────────────────────


async def fetch_events_for_date(dt: str) -> dict[str, list[dict]]:
    """Fetch all events for a date, paginating through league offsets.

    Returns { "MLB": [event, ...], "NBA": [...], ... }
    """
    if dt in _events_cache:
        return _events_cache[dt]

    tok = _token()
    if not tok:
        log.warning("[pikkit] PIKKIT_TOKEN not set, skipping")
        return {}

    result: dict[str, list[dict]] = {}
    offset = 0
    max_offsets = 20  # safety cap

    async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
        while offset < max_offsets:
            url = f"{BASE}/events/all?query_date={dt}&league_offset={offset}"
            try:
                resp = await client.get(url, headers=_headers())
            except httpx.HTTPError as e:
                log.warning("[pikkit] events fetch error: %s", e)
                break

            if resp.status_code == 401:
                log.warning("[pikkit] 401 — token expired or invalid")
                _alert_token_expired()
                return {}
            if resp.status_code != 200:
                log.warning("[pikkit] events status %d", resp.status_code)
                break

            data = resp.json()
            leagues = data.get("leagues", [])
            if not leagues or not leagues[0]:
                break  # no more leagues

            for league_items in leagues:
                if not league_items:
                    continue
                # First item is the header
                header = league_items[0] if isinstance(league_items, list) else None
                if not header or header.get("type") != "header":
                    continue
                league_name = header.get("value", "")
                events = []
                for item in league_items[1:]:
                    if not isinstance(item, dict):
                        continue
                    val = item.get("value", item)
                    if isinstance(val, dict) and val.get("_id"):
                        events.append(_parse_event_listing(val))
                if events:
                    result[league_name] = events

            next_offset = data.get("league_offset")
            if next_offset is None or next_offset <= offset:
                break
            offset = next_offset

    _events_cache[dt] = result
    log.info("[pikkit] loaded %d leagues for %s: %s",
             len(result), dt, list(result.keys()))
    return result


def _parse_event_listing(val: dict) -> dict:
    """Extract key fields from an events-list item."""
    ei = val.get("event_info", {})
    ctx = val.get("event_context", {})
    home = ei.get("home", {})
    away = ei.get("away", {})
    return {
        "event_id":   val["_id"],
        "home_full":  home.get("full", ""),
        "away_full":  away.get("full", ""),
        "home_abbr":  ctx.get("home", {}).get("abbr", ""),
        "away_abbr":  ctx.get("away", {}).get("abbr", ""),
        "full_name":  ei.get("full_name", ""),
        "start_time": val.get("start_time", ""),
        "status":     val.get("status", ""),
        "league":     ei.get("league", {}).get("short", ""),
    }


# ── Fetch splits for a single event ────────────────────────────────────


async def fetch_splits(event_id: str) -> dict | None:
    """Fetch community splits for one event.

    Returns dict with keys: moneyline, spread, total — each having
    home/away (or over/under) with bet_pct, handle_pct, label.
    Returns None on failure.
    """
    if event_id in _splits_cache:
        return _splits_cache[event_id]

    tok = _token()
    if not tok:
        return None

    url = f"{BASE}/event/foryou/{event_id}"
    async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
        try:
            resp = await client.get(url, headers=_headers())
        except httpx.HTTPError as e:
            log.warning("[pikkit] splits fetch error for %s: %s", event_id, e)
            return None

        if resp.status_code == 401:
            log.warning("[pikkit] 401 — token expired")
            _alert_token_expired()
            return None
        if resp.status_code != 200:
            log.warning("[pikkit] splits status %d for %s", resp.status_code, event_id)
            return None

        data = resp.json()

    community = data.get("community", {})
    breakdowns = community.get("breakdowns", {})
    if not breakdowns:
        return None

    splits = {
        "num_picks": community.get("num_picks", 0),
        "total_wagered": community.get("total_wagered", 0),
    }
    for market in ("moneyline", "spread", "total"):
        mkt = breakdowns.get(market, {})
        if not mkt:
            continue
        # moneyline/spread: home/away.  total: over/under.
        sides = {}
        for side_key, side_data in mkt.items():
            if isinstance(side_data, dict) and "bet_pct" in side_data:
                sides[side_key] = {
                    "bet_pct":    side_data.get("bet_pct", 0),
                    "handle_pct": side_data.get("handle_pct", 0),
                    "label":      side_data.get("label", ""),
                    "bets":       side_data.get("bets", 0),
                }
        splits[market] = sides

    _splits_cache[event_id] = splits
    return splits


# ── Match a parsed pick to a Pikkit event ───────────────────────────────


def _normalize(name: str) -> str:
    """Lowercase, strip common prefixes/suffixes for fuzzy matching."""
    return re.sub(r"[^a-z0-9 ]", "", name.lower()).strip()


def _team_match(pick_team: str, event_team_full: str) -> bool:
    """Check if a pick's team name matches a Pikkit event team."""
    pn = _normalize(pick_team)
    en = _normalize(event_team_full)
    if not pn or not en:
        return False
    # Exact match
    if pn == en:
        return True
    # Substring: "angels" in "los angeles angels"
    # Use word-boundary-aware matching for short names
    en_words = en.split()
    if pn in en_words:
        return True
    # Multi-word substring: "san diego" in "san diego padres"
    if len(pn) >= 4 and pn in en:
        return True
    return False


def match_pick_to_event(
    pick: dict,
    events: list[dict],
    sport: str | None = None,
) -> dict | None:
    """Match a parsed pick to a Pikkit event from the events list.

    pick: parsed pick dict with 'teams', 'description', 'bet_type'
    events: list from fetch_events_for_date
    Returns the matched event dict or None.
    """
    teams = pick.get("teams", [])
    desc = pick.get("description", "")

    if not teams and not desc:
        return None

    for evt in events:
        for team in teams:
            if _team_match(team, evt["home_full"]) or _team_match(team, evt["away_full"]):
                return evt
            if evt["home_abbr"] and _normalize(team) == _normalize(evt["home_abbr"]):
                return evt
            if evt["away_abbr"] and _normalize(team) == _normalize(evt["away_abbr"]):
                return evt

    return None


# ── Determine book interest for a pick ──────────────────────────────────


def classify_pick_side(
    pick: dict,
    splits: dict,
    event: dict,
) -> dict | None:
    """Determine if a pick is on the public or book side.

    Returns dict with: side, public_pct, handle_pct, market, num_picks
    or None if we can't determine.
    """
    bet_type = (pick.get("bet_type") or "").lower()
    teams = pick.get("teams", [])
    direction = (pick.get("direction") or "").lower()
    desc = (pick.get("description") or "").lower()

    # Map bet_type to splits market
    if bet_type in ("moneyline", "ml", "money line"):
        market = "moneyline"
    elif bet_type in ("spread", "run line", "puck line"):
        market = "spread"
    elif bet_type in ("total", "over/under", "over", "under"):
        market = "total"
    else:
        # Try to infer from description
        if "ml" in desc or "moneyline" in desc or "money line" in desc:
            market = "moneyline"
        elif any(x in desc for x in ("spread", "run line", "puck line", "rl")):
            market = "spread"
        elif "over" in desc or "under" in desc or "total" in desc:
            market = "total"
        else:
            # Default to moneyline for team picks without explicit type
            market = "moneyline"

    mkt_data = splits.get(market)
    if not mkt_data:
        return None

    # For totals: use direction (over/under)
    if market == "total":
        if direction == "over" or "over" in desc:
            pick_side_key = "over"
        elif direction == "under" or "under" in desc:
            pick_side_key = "under"
        else:
            return None
    else:
        # For ML/spread: determine if pick is on home or away
        pick_side_key = None
        for team in teams:
            if _team_match(team, event["home_full"]):
                pick_side_key = "home"
                break
            if _team_match(team, event["away_full"]):
                pick_side_key = "away"
                break
            if event["home_abbr"] and _normalize(team) == _normalize(event["home_abbr"]):
                pick_side_key = "home"
                break
            if event["away_abbr"] and _normalize(team) == _normalize(event["away_abbr"]):
                pick_side_key = "away"
                break
        if not pick_side_key:
            return None

    side_data = mkt_data.get(pick_side_key)
    if not side_data:
        return None

    bet_pct = side_data["bet_pct"]
    handle_pct = side_data["handle_pct"]

    # Public side = majority of bets (>50%)
    side = "public" if bet_pct > 0.5 else "book"

    return {
        "event_id":   event["event_id"],
        "side":       side,
        "public_pct": round(bet_pct, 4),
        "handle_pct": round(handle_pct, 4),
        "market":     market,
        "num_picks":  splits.get("num_picks", 0),
    }


# ── High-level: fetch splits for a pick ─────────────────────────────────


async def get_pick_splits(
    pick: dict,
    sport: str,
    dt: str,
) -> dict | None:
    """End-to-end: find the Pikkit event for a pick and return its side classification.

    Returns a dict suitable for storing in pikkit_by_pick, or None.
    """
    if not _token():
        return None

    all_events = await fetch_events_for_date(dt)
    if not all_events:
        return None

    # Collect events for matching leagues
    league_keys = _SPORT_TO_LEAGUES.get(sport, [])
    candidates: list[dict] = []
    if league_keys:
        for lk in league_keys:
            candidates.extend(all_events.get(lk, []))
    if not candidates:
        # Try all leagues as fallback
        for evts in all_events.values():
            candidates.extend(evts)

    event = match_pick_to_event(pick, candidates, sport)
    if not event:
        return None

    splits = await fetch_splits(event["event_id"])
    if not splits:
        return None

    return classify_pick_side(pick, splits, event)
