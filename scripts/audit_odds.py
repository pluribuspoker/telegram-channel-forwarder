#!/usr/bin/env python3
"""
scripts/audit_odds.py — Backtest odds lookup against graded picks.

Reads result.json and/or result_df.json, parses picks via Claude,
fetches closing odds from the Odds API, and outputs a CSV for analysis.

API plan supports: h2h, spreads, totals (no alternate markets, no period markets).
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
from scores import _team_matches

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

# Free-tier plan supports only these markets
MARKETS = "h2h,spreads,totals"

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


async def fetch_historical_odds(sport_key: str, date: str) -> list[dict]:
    """Fetch historical odds snapshot for sport_key on date.

    Snapshot time is 18:00 UTC (1 pm ET) — pre-game for most evening events.
    Cached to disk to avoid re-fetching.
    """
    global _quota_remaining
    cache_key = f"{sport_key}:{date}"
    if cache_key in _odds_cache:
        return _odds_cache[cache_key]

    if not ODDS_API_KEY:
        print("  [warn] ODDS_API_KEY not set", file=sys.stderr)
        return []

    async with httpx.AsyncClient(timeout=20) as http:
        try:
            r = await http.get(
                f"{ODDS_API_BASE}/historical/sports/{sport_key}/odds",
                params={
                    "apiKey":     ODDS_API_KEY,
                    "regions":    "us",
                    "markets":    MARKETS,
                    "date":       f"{date}T18:00:00Z",
                    "oddsFormat": "american",
                },
            )
            r.raise_for_status()
            _quota_remaining = r.headers.get("x-requests-remaining")
            events: list[dict] = r.json().get("data", [])
            _odds_cache[cache_key] = events
            _save_odds_cache()
            print(f"  [API] {sport_key} {date} -> {len(events)} events  (quota: {_quota_remaining})", file=sys.stderr)
            return events
        except httpx.HTTPStatusError as exc:
            print(f"  [Odds API {exc.response.status_code}] {sport_key} {date}: {exc.response.text[:120]}", file=sys.stderr)
            _odds_cache[cache_key] = []
            _save_odds_cache()
            return []
        except Exception as exc:
            print(f"  [Odds API error] {sport_key} {date}: {exc}", file=sys.stderr)
            return []


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


def _find_event(events: list[dict], teams: list[str]) -> dict | None:
    # Collect all matching events with a score (more specific = higher score)
    scored: list[tuple[int, dict]] = []
    for term in teams:
        t_lower = term.lower()
        for event in events:
            home = event.get("home_team", "")
            away = event.get("away_team", "")
            for side in (home, away):
                if _team_matches(t_lower, side.lower()):
                    # Score: prefer shorter team names (more specific match)
                    score = -len(side)
                    scored.append((score, event))
                    break
    if not scored:
        return None
    # Return the event with highest score (least ambiguous match)
    scored.sort(key=lambda x: x[0], reverse=True)
    return scored[0][1]


def _lookup_moneyline(event: dict, team: str) -> dict:
    bks = event.get("bookmakers", [])
    candidates = [(price, bk) for _, price, bk in _collect_outcomes(bks, "h2h", name_filter=team)]
    odds, book = _pick_best(candidates)
    return {
        "game_found":    True,
        "match_type":    "exact" if odds is not None else "no_h2h_data",
        "pick_line":     None,
        "api_line":      None,
        "computed_odds": odds,
        "adjusted_odds": odds,  # no adjustment for moneyline
        "bookmaker":     book,
    }


def _lookup_spread(sport: str, event: dict, team: str, pick_line: float) -> dict:
    bks = event.get("bookmakers", [])

    # Exact match
    hits = _collect_outcomes(bks, "spreads", name_filter=team, line_filter=pick_line)
    if hits:
        odds, book = _pick_best([(price, bk) for _, price, bk in hits])
        return {"game_found": True, "match_type": "exact", "pick_line": pick_line,
                "api_line": pick_line, "computed_odds": odds, "adjusted_odds": odds, "bookmaker": book}

    # Proximity: only consider outcomes with the same sign as pick_line to avoid
    # matching the opponent's line (e.g. Tennessee -9.5 vs Tennessee St +24.5)
    all_lines: list[tuple[float, int, str]] = []
    for pt, price, bk in _collect_outcomes(bks, "spreads", name_filter=team):
        if pt is None:
            continue
        # Skip opposite-sign outcomes (would be the opponent's side)
        if pick_line != 0 and (pick_line < 0) != (pt < 0):
            continue
        all_lines.append((pt, price, bk))

    if not all_lines:
        return {"game_found": True, "match_type": "no_spread_data", "pick_line": pick_line,
                "api_line": None, "computed_odds": None, "adjusted_odds": None, "bookmaker": None}

    closest = min(all_lines, key=lambda x: abs(x[0] - pick_line))
    gap = abs(closest[0] - pick_line)

    if gap > MAX_LINE_GAP:
        return {"game_found": True, "match_type": f"alt_line_gap_{gap:.1f}pts", "pick_line": pick_line,
                "api_line": closest[0], "computed_odds": None, "adjusted_odds": None, "bookmaker": None}

    adjusted = _adjust_for_gap(sport, closest[1], pick_line, closest[0], gap)
    return {"game_found": True, "match_type": f"proximity_{gap:.1f}pts", "pick_line": pick_line,
            "api_line": closest[0], "computed_odds": closest[1], "adjusted_odds": adjusted, "bookmaker": closest[2]}


def _lookup_total(sport: str, event: dict, direction: str, pick_line: float) -> dict:
    bks = event.get("bookmakers", [])
    outcome_name = "Over" if direction == "over" else "Under"

    # Exact match
    hits = _collect_outcomes(bks, "totals", name_filter=outcome_name, line_filter=pick_line)
    if hits:
        odds, book = _pick_best([(price, bk) for _, price, bk in hits])
        return {"game_found": True, "match_type": "exact", "pick_line": pick_line,
                "api_line": pick_line, "computed_odds": odds, "adjusted_odds": odds, "bookmaker": book}

    # Proximity
    all_lines: list[tuple[float, int, str]] = []
    for pt, price, bk in _collect_outcomes(bks, "totals", name_filter=outcome_name):
        if pt is not None:
            all_lines.append((pt, price, bk))

    if not all_lines:
        return {"game_found": True, "match_type": "no_total_data", "pick_line": pick_line,
                "api_line": None, "computed_odds": None, "adjusted_odds": None, "bookmaker": None}

    closest = min(all_lines, key=lambda x: abs(x[0] - pick_line))
    gap = abs(closest[0] - pick_line)

    if gap > MAX_LINE_GAP:
        return {"game_found": True, "match_type": f"alt_line_gap_{gap:.1f}pts", "pick_line": pick_line,
                "api_line": closest[0], "computed_odds": None, "adjusted_odds": None, "bookmaker": None}

    # For totals: treat the line as a directional number — over wants lower line, under wants higher.
    # Express as a signed value from the bettor's perspective for the universal formula:
    # Over bettor: lower line = better → negate so higher-is-better rule still applies
    # Under bettor: higher line = better → use as-is
    signed_pick = -pick_line if direction == "over" else pick_line
    signed_api  = -closest[0] if direction == "over" else closest[0]
    adjusted = _adjust_for_gap(sport, closest[1], signed_pick, signed_api, gap)
    return {"game_found": True, "match_type": f"proximity_{gap:.1f}pts", "pick_line": pick_line,
            "api_line": closest[0], "computed_odds": closest[1], "adjusted_odds": adjusted, "bookmaker": closest[2]}


def lookup_pick_odds(sport: str, pick: dict, events: list[dict]) -> dict:
    """Given a parsed pick and Odds API events, find the best odds match."""
    teams     = pick.get("teams") or []
    bet_type  = pick.get("bet_type", "")
    line      = pick.get("line")
    direction = pick.get("direction")
    period    = pick.get("period", "game")
    desc      = pick.get("description", "")

    _no_game = {"game_found": False, "match_type": "no_game",
                "pick_line": line, "api_line": None,
                "computed_odds": None, "adjusted_odds": None, "bookmaker": None}

    # Props: separate player_props endpoint required (not in our plan)
    if bet_type == "prop":
        return {**_no_game, "game_found": True, "match_type": "player_prop_unavailable"}

    # Team totals: not in standard totals market
    if bet_type == "team_total":
        return {**_no_game, "game_found": True, "match_type": "team_total_unavailable"}

    # Period bets: period markets not in our API plan — also guard with regex on description
    if period != "game" or _PERIOD_RE.search(desc):
        return {**_no_game, "game_found": True, "match_type": "period_market_unavailable"}

    event = _find_event(events, teams)
    if not event:
        return _no_game

    if bet_type == "moneyline":
        return _lookup_moneyline(event, teams[0] if teams else "")

    if bet_type == "spread":
        if line is None:
            return {**_no_game, "game_found": True, "match_type": "no_line_in_pick"}
        return _lookup_spread(sport, event, teams[0] if teams else "", float(line))

    if bet_type == "total":
        if line is None or not direction:
            return {**_no_game, "game_found": True, "match_type": "missing_line_or_direction"}
        return _lookup_total(sport, event, direction, float(line))

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

            leg_events: list[dict] = []
            if not dry_run and sport_key:
                leg_events = await fetch_historical_odds(sport_key, msg["date"])

            result = (
                lookup_pick_odds(pick_sport, pick, leg_events)
                if leg_events
                else {
                    "game_found":    False,
                    "match_type":    "dry_run" if dry_run else ("sport_unsupported" if not sport_key else "api_error"),
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
                "notes":         notes,
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
    print("--- Coverage summary -----------------------------------------", file=sys.stderr)
    print(f"  Total picks:                {total}", file=sys.stderr)
    print(f"  Odds found:                 {found}  ({100*found//total if total else 0}%)", file=sys.stderr)
    print(f"    exact match:              {exact}", file=sys.stderr)
    print(f"    proximity w/ adjustment:  {prox}", file=sys.stderr)
    print(f"  Alt line (gap > {MAX_LINE_GAP}pts):       {alt_gap}  (genuine alt lines)", file=sys.stderr)
    print(f"  Period/prop/team_total:     {unavail}  (separate endpoint needed)", file=sys.stderr)
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
