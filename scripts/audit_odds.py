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
from scores import fetch_espn, espn_bookmakers_for_teams, ESPN_LEAGUES
from odds import (
    ODDS_API_KEY, ODDS_API_BASE,
    SPORT_KEYS, PROP_STAT_MARKETS, MARKETS_FULL, PREFERRED_BOOKS,
    MAX_LINE_GAP, HALF_POINT_COST, _PERIOD_RE,
    _pick_best, _collect_outcomes, _find_event_id,
    _lookup_moneyline, _lookup_spread, _lookup_total, _lookup_prop,
    lookup_pick_odds,
)

load_dotenv(ROOT / ".env")
load_dotenv(ROOT / ".env.local", override=True)

# ── Paths ─────────────────────────────────────────────────────────────────────

DATA_DIR         = ROOT / "data"
ODDS_CACHE_FILE  = DATA_DIR / "odds_api_cache.json"
PARSE_CACHE_FILE = DATA_DIR / "audit_parse_cache.json"

# ── Text helpers ──────────────────────────────────────────────────────────────

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
    m = re.search(r'([+-]\d{3,4})(?:\s|$|\n|✅|❌)', text)
    return m.group(1) if m else None


def _text_hash(text: str) -> str:
    return hashlib.md5(text.encode()).hexdigest()[:12]


# ── Odds API (JSON file cache for backtest) ───────────────────────────────────

_odds_cache: dict = {}
_quota_remaining: str | None = None


def _load_odds_cache() -> None:
    global _odds_cache
    if ODDS_CACHE_FILE.exists():
        _odds_cache = json.loads(ODDS_CACHE_FILE.read_text(encoding="utf-8"))


def _save_odds_cache() -> None:
    ODDS_CACHE_FILE.write_text(json.dumps(_odds_cache, indent=2), encoding="utf-8")


async def _api_get(http: httpx.AsyncClient, url: str, params: dict) -> dict | list | None:
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
    cache_key = f"event_odds:{event_id}:{date}"
    if cache_key in _odds_cache:
        return _odds_cache[cache_key]
    if not ODDS_API_KEY:
        return []
    async with httpx.AsyncClient(timeout=20) as http:
        data = await _api_get(http,
            f"{ODDS_API_BASE}/historical/sports/{sport_key}/events/{event_id}/odds",
            {"apiKey": ODDS_API_KEY, "regions": "us", "markets": MARKETS_FULL,
             "date": f"{date}T18:00:00Z", "oddsFormat": "american"},
        )
    bookmakers: list[dict] = []
    if isinstance(data, dict):
        bookmakers = data.get("data", {}).get("bookmakers", []) if "data" in data else data.get("bookmakers", [])
    _odds_cache[cache_key] = bookmakers
    _save_odds_cache()
    mkt_keys = {m["key"] for bk in bookmakers for m in bk.get("markets", [])}
    print(f"  [event_odds] {event_id[:8]}.. -> {len(bookmakers)} books, markets: {sorted(mkt_keys)}  (quota: {_quota_remaining})", file=sys.stderr)
    return bookmakers


async def fetch_event_prop_odds(sport_key: str, event_id: str, date: str, prop_market: str) -> list[dict]:
    cache_key = f"event_prop_odds:{event_id}:{date}:{prop_market}"
    if cache_key in _odds_cache:
        return _odds_cache[cache_key]
    if not ODDS_API_KEY:
        return []
    async with httpx.AsyncClient(timeout=20) as http:
        data = await _api_get(http,
            f"{ODDS_API_BASE}/historical/sports/{sport_key}/events/{event_id}/odds",
            {"apiKey": ODDS_API_KEY, "regions": "us", "markets": prop_market,
             "date": f"{date}T18:00:00Z", "oddsFormat": "american"},
        )
    bookmakers: list[dict] = []
    if isinstance(data, dict):
        bookmakers = data.get("data", {}).get("bookmakers", []) if "data" in data else data.get("bookmakers", [])
    _odds_cache[cache_key] = bookmakers
    _save_odds_cache()
    print(f"  [prop_odds] {event_id[:8]}.. {prop_market} -> {len(bookmakers)} books  (quota: {_quota_remaining})", file=sys.stderr)
    return bookmakers


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
            espn_used = False
            event_id: str | None = None
            bet_type = pick.get("bet_type", "")

            if not dry_run and sport_key:
                # Props: skip ESPN (no prop data), go straight to Odds API for event_id
                if bet_type == "prop":
                    event_list = await fetch_event_list(sport_key, msg["date"])
                    event_id = _find_event_id(event_list, pick.get("teams") or [])
                else:
                    # Step 1: ESPN first (free, works pre-game; odds cleared after completion)
                    if pick_sport in ESPN_LEAGUES:
                        espn_data = await fetch_espn(pick_sport, msg["date"])
                        if espn_data:
                            bookmakers = espn_bookmakers_for_teams(espn_data, pick.get("teams") or [])
                            espn_used = bool(bookmakers)

                    # Step 2: Odds API fallback (unlocks alternate lines + period markets)
                    if not bookmakers:
                        event_list = await fetch_event_list(sport_key, msg["date"])
                        event_id = _find_event_id(event_list, pick.get("teams") or [])
                        if event_id:
                            bookmakers = await fetch_event_odds(sport_key, event_id, msg["date"])

            # Player props: separate fetch + lookup by player name
            if bet_type == "prop" and not dry_run and sport_key:
                prop_stat   = (pick.get("prop_stat") or "").upper()
                prop_market = PROP_STAT_MARKETS.get(pick_sport, {}).get(prop_stat, "")
                if prop_market and event_id:
                    prop_bookmakers = await fetch_event_prop_odds(sport_key, event_id, msg["date"], prop_market)
                    result = _lookup_prop(
                        prop_bookmakers,
                        pick.get("player") or "",
                        prop_market,
                        pick.get("direction") or "over",
                        float(pick.get("line") or 0.5),
                    )
                elif not prop_market:
                    result = {"game_found": True, "match_type": f"prop_stat_unsupported({prop_stat})",
                              "pick_line": pick.get("line"), "api_line": None,
                              "computed_odds": None, "adjusted_odds": None, "bookmaker": None}
                else:
                    result = {"game_found": False, "match_type": "no_game",
                              "pick_line": pick.get("line"), "api_line": None,
                              "computed_odds": None, "adjusted_odds": None, "bookmaker": None}
            else:
                result = (
                    lookup_pick_odds(pick_sport, pick, bookmakers)
                    if (bookmakers or (sport_key and not dry_run and bet_type != "prop"))
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
                "notes":         ("espn_odds" if espn_used else "") or notes,
            })

    # Output CSV
    with open(out_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=CSV_FIELDS)
        writer.writeheader()
        writer.writerows(rows)

    # Summary
    total      = len(rows)
    found      = sum(1 for r in rows if r.get("computed_odds") not in ("", None))
    exact_main = sum(1 for r in rows if r.get("match_type") == "exact")
    exact_alt  = sum(1 for r in rows if r.get("match_type") == "exact_alt")
    prox       = sum(1 for r in rows if str(r.get("match_type", "")).startswith("proximity"))
    alt_gap    = sum(1 for r in rows if str(r.get("match_type", "")).startswith("alt_line_gap"))
    prop_unavail = sum(1 for r in rows if "prop_unavailable" in str(r.get("match_type", "")))

    print(file=sys.stderr)
    print("--- Coverage summary -----------------------------------------", file=sys.stderr)
    print(f"  Total picks:                {total}", file=sys.stderr)
    print(f"  Odds found:                 {found}  ({100*found//total if total else 0}%)", file=sys.stderr)
    print(f"    exact (main line):        {exact_main}", file=sys.stderr)
    print(f"    exact (alt line):         {exact_alt}", file=sys.stderr)
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
