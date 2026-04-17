"""
sauce_daily.py — Daily scrape, grade, and forward Kyle Kirms (Sauce) open bets.

Flow:
  1. Scrape SAUCE tab from published Google Sheet
  2. Classify sports + parse bets via Claude
  3. Validate sports against ESPN schedules
  4. Upsert into sauce_picks DB table
  5. Grade PENDING picks where games are complete
  6. Write results to Google Sheet
  7. Render screenshot (upcoming + past with result emoji)
  8. Send image to Telegram channel

Usage:
    python scripts/sauce_daily.py                  # full run -> test channel
    python scripts/sauce_daily.py --channel ID     # send to specific channel
    python scripts/sauce_daily.py --grade-only     # grade pending, no screenshot
    python scripts/sauce_daily.py --no-send        # scrape+grade+sheet, skip Telegram
"""

import asyncio
import io
import json
import os
import sqlite3
import sys
from datetime import date as _date, datetime, timedelta, timezone
from pathlib import Path

# Add project root to path
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv
load_dotenv(ROOT / ".env")
load_dotenv(ROOT / ".env.local", override=True)

from telethon import TelegramClient
from telethon.sessions import StringSession

from scripts.scrape_kirms import fetch_tab, parse_picks
from ai import (
    build_context, claude, claude_grade, fmt_cost, usage_cost,
    CONTEXT_PENDING, CONTEXT_SKIP, CONTEXT_ESPN_ERROR,
)
from common import VERDICT_EMOJI
from scores import ESPN_LEAGUES, fetch_espn, find_event_ids, validate_sport
from sheets import _get_client as get_sheets_client, _map_bet_type

# ─── Config ───────────────────────────────────────────────────────────────────

DB_PATH = str(ROOT / "picks.db")
SAUCE_SHEET_ID = "1yozWEoQ5m6rqNC8-E5UGwg0ySjYbAybNHwPmtNTYIzM"
TEST_CHANNEL = -1003713809799

API_ID = int(os.environ["TELEGRAM_API_ID"])
API_HASH = os.environ["TELEGRAM_API_HASH"]
SESSION = os.environ["TELEGRAM_SESSION"]

# ─── DB schema ────────────────────────────────────────────────────────────────

_SCHEMA = """
CREATE TABLE IF NOT EXISTS sauce_picks (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    date        TEXT    NOT NULL,
    bet         TEXT    NOT NULL,
    odds        TEXT,
    unit        TEXT,
    sport       TEXT,
    bet_type    TEXT,
    teams       TEXT,
    line        REAL,
    direction   TEXT,
    player      TEXT,
    prop_stat   TEXT,
    period      TEXT    DEFAULT 'game',
    verdict     TEXT    DEFAULT 'PENDING',
    calc        TEXT,
    graded_at   TEXT,
    UNIQUE(date, bet)
);
"""


def _init_db():
    with sqlite3.connect(DB_PATH) as conn:
        conn.executescript(_SCHEMA)


def _connect():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


# ─── Claude: classify + parse ─────────────────────────────────────────────────

async def classify_and_parse(picks: list[dict]) -> list[dict]:
    """Classify sport and parse bet structure for each pick via Claude."""
    lines = []
    for i, p in enumerate(picks):
        lines.append(f"{i+1}. [{p['date']}] {p['name']}: {p['bet']}  ({p['odds']}  {p['unit']})")

    prompt = (
        "You are analyzing sports bets from a US capper's open-bets sheet.\n\n"
        "For each bet below, return a JSON list of objects with:\n"
        '  "sport": "NBA|NCAAB|MLB|NFL|NHL|UFC|Tennis|Boxing|KBO|Other",\n'
        '  "bet_type": "spread|moneyline|total|team_total|prop",\n'
        '  "teams": ["Full canonical team name(s)"],\n'
        '  "line": <number or null>,\n'
        '  "direction": "over|under|null",\n'
        '  "player": "player name or null",\n'
        '  "prop_stat": "stat abbreviation or null",\n'
        '  "period": "game|1h|2h|1q|2q|3q|4q"\n\n'
        "Rules:\n"
        '- Use full canonical team names (e.g. "Cleveland Guardians" not "Guardians")\n'
        '- For totals like "WSH/SF Over 7.5", teams should list both teams\n'
        '- For moneyline like "Suns ML", teams should list the team\n'
        '- For spreads like "Hornets -2.5", line=-2.5\n'
        "- Return JSON only (no markdown fences)\n\n"
        "Bets:\n" + "\n".join(lines)
    )

    resp = await claude().messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=2048,
        messages=[{"role": "user", "content": prompt}],
    )
    raw = resp.content[0].text.strip()
    if raw.startswith("```"):
        raw = raw.split("\n", 1)[1].rsplit("```", 1)[0].strip()
    parsed_list = json.loads(raw)

    for i, p in enumerate(picks):
        if i < len(parsed_list):
            parsed = parsed_list[i]
            p["sport"] = parsed.get("sport", "Other")
            p["bet_type"] = parsed.get("bet_type", "")
            p["_teams_list"] = parsed.get("teams", [])
            p["teams"] = json.dumps(p["_teams_list"])
            p["line"] = parsed.get("line")
            p["direction"] = parsed.get("direction")
            p["player"] = parsed.get("player")
            p["prop_stat"] = parsed.get("prop_stat")
            p["period"] = parsed.get("period", "game")
    return picks


async def validate_sports(picks: list[dict], scoreboard_cache: dict) -> list[dict]:
    """Validate each pick's sport against ESPN schedules."""
    for p in picks:
        sport = p.get("sport", "Other")
        teams = p.get("_teams_list", [])
        date_str = _date_to_iso(p["date"])

        new_sport, new_teams = await validate_sport(
            sport, teams, p["bet"], date_str, scoreboard_cache,
        )
        if new_sport != sport:
            print(f"  ESPN override: {p['bet']} -- {sport} -> {new_sport}")
            p["sport"] = new_sport
        if new_teams != teams:
            print(f"  ESPN teams fix: {teams} -> {new_teams}")
            p["_teams_list"] = new_teams
            p["teams"] = json.dumps(new_teams)
    return picks


# ─── DB operations ────────────────────────────────────────────────────────────

def _date_to_iso(date_str: str) -> str:
    """Convert '4/17' format to 'YYYY-MM-DD' using current year."""
    parts = date_str.split("/")
    month, day = int(parts[0]), int(parts[1])
    year = _date.today().year
    return f"{year}-{month:02d}-{day:02d}"


def _iso_to_display(iso_date: str) -> str:
    """Convert 'YYYY-MM-DD' to '4/17' display format."""
    dt = datetime.strptime(iso_date, "%Y-%m-%d")
    return f"{dt.month}/{dt.day}"


def upsert_picks(picks: list[dict]) -> int:
    """Insert or ignore picks into the sauce_picks table."""
    with _connect() as conn:
        inserted = 0
        for p in picks:
            iso_date = _date_to_iso(p["date"])
            cursor = conn.execute(
                """INSERT OR IGNORE INTO sauce_picks
                   (date, bet, odds, unit, sport, bet_type, teams, line, direction, player, prop_stat, period)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (iso_date, p["bet"], p.get("odds", ""), p.get("unit", ""),
                 p.get("sport", ""), p.get("bet_type", ""), p.get("teams", "[]"),
                 p.get("line"), p.get("direction"), p.get("player"),
                 p.get("prop_stat"), p.get("period", "game")),
            )
            if cursor.rowcount > 0:
                inserted += 1
        return inserted


def get_pending_picks(days_back: int = 3) -> list[dict]:
    """Get PENDING picks from the last N days."""
    cutoff = (_date.today() - timedelta(days=days_back)).isoformat()
    with _connect() as conn:
        rows = conn.execute(
            "SELECT * FROM sauce_picks WHERE verdict = 'PENDING' AND date >= ? ORDER BY date, id",
            (cutoff,),
        ).fetchall()
        return [dict(r) for r in rows]


def get_picks_for_dates(start_date: str, end_date: str) -> list[dict]:
    """Get all picks between start_date (inclusive) and end_date (exclusive)."""
    with _connect() as conn:
        rows = conn.execute(
            "SELECT * FROM sauce_picks WHERE date >= ? AND date < ? ORDER BY date DESC, id",
            (start_date, end_date),
        ).fetchall()
        return [dict(r) for r in rows]


def _batch_update_verdicts(updates: list[tuple[str, str, int]]):
    """Batch update verdicts. Each tuple is (verdict, calc, pick_id)."""
    if not updates:
        return
    now = datetime.now(timezone.utc).isoformat()
    with _connect() as conn:
        conn.executemany(
            "UPDATE sauce_picks SET verdict = ?, calc = ?, graded_at = ? WHERE id = ?",
            [(v, c, now, pid) for v, c, pid in updates],
        )


# ─── Grading ──────────────────────────────────────────────────────────────────

async def grade_pending(scoreboard_cache: dict) -> list[dict]:
    """Grade all PENDING picks using ESPN scores + Claude.

    Reuses the provided scoreboard_cache (shared with validation step).
    Returns list of picks that were graded in this call.
    """
    pending = get_pending_picks()
    if not pending:
        print("No pending picks to grade.")
        return []

    print(f"Grading {len(pending)} pending picks...")
    summary_cache: dict = {}
    updates: list[tuple[str, str, int]] = []
    graded: list[dict] = []

    for p in pending:
        sport = p["sport"]
        date_str = p["date"]

        if sport not in ESPN_LEAGUES:
            print(f"  Skipping {p['bet']} -- sport '{sport}' not in ESPN")
            updates.append(("UNKNOWN", "Sport not supported for auto-grading", p["id"]))
            continue

        pick_dict = {
            "description": p["bet"],
            "bet_type": p["bet_type"],
            "teams": json.loads(p["teams"]) if p["teams"] else [],
            "player": p["player"],
            "prop_stat": p["prop_stat"],
            "line": p["line"],
            "direction": p["direction"],
            "period": p["period"] or "game",
            "is_parlay_leg": False,
        }

        sb_key = (sport, date_str)
        if sb_key not in scoreboard_cache:
            scoreboard_cache[sb_key] = await fetch_espn(sport, date_str)
        sb = scoreboard_cache[sb_key]

        context, game_date = await build_context(sport, date_str, pick_dict, sb, summary_cache)

        if context == CONTEXT_PENDING:
            print(f"  {p['bet']} -- game not yet complete")
            continue
        if context == CONTEXT_SKIP:
            print(f"  {p['bet']} -- game not found")
            updates.append(("UNKNOWN", "Game not found in ESPN data", p["id"]))
            continue
        if context == CONTEXT_ESPN_ERROR:
            print(f"  {p['bet']} -- ESPN error, will retry")
            continue

        verdict, calc = await claude_grade(
            p["bet"], date_str, context,
            bet_type=p["bet_type"],
            prop_stat=p["prop_stat"] or "",
        )

        print(f"  {verdict:7s}  {p['bet']:30s}  {calc[:50].encode('ascii', 'replace').decode()}")
        updates.append((verdict, calc, p["id"]))
        p["verdict"] = verdict
        p["calc"] = calc
        graded.append(p)

    _batch_update_verdicts(updates)
    return graded


# ─── Google Sheet ─────────────────────────────────────────────────────────────

async def setup_sheet_headers():
    """Set up column headers on the Sauce results sheet."""
    gc = get_sheets_client()
    if not gc:
        print("Google Sheets client not available")
        return
    sh = gc.open_by_key(SAUCE_SHEET_ID)
    ws = sh.sheet1

    headers = ["Date", "League", "Pick", "Units Wagered", "Bet Type", "Odds", "W/L", "Return"]
    await asyncio.to_thread(
        ws.update, "A1:H1", [headers], value_input_option="USER_ENTERED"
    )

    fmt_requests = [
        {
            "repeatCell": {
                "range": {"sheetId": ws.id, "startRowIndex": 0, "endRowIndex": 1,
                          "startColumnIndex": 0, "endColumnIndex": 8},
                "cell": {
                    "userEnteredFormat": {
                        "textFormat": {"bold": True},
                        "horizontalAlignment": "CENTER",
                        "backgroundColor": {"red": 0.93, "green": 0.93, "blue": 0.93},
                    }
                },
                "fields": "userEnteredFormat(textFormat,horizontalAlignment,backgroundColor)",
            }
        },
        {
            "updateSheetProperties": {
                "properties": {"sheetId": ws.id, "gridProperties": {"frozenRowCount": 1}},
                "fields": "gridProperties.frozenRowCount",
            }
        },
        *[
            {
                "updateDimensionProperties": {
                    "range": {"sheetId": ws.id, "dimension": "COLUMNS",
                              "startIndex": i, "endIndex": i + 1},
                    "properties": {"pixelSize": w},
                    "fields": "pixelSize",
                }
            }
            for i, w in enumerate([100, 60, 180, 110, 80, 70, 50, 70])
        ],
    ]
    await asyncio.to_thread(sh.batch_update, {"requests": fmt_requests})
    print("Sheet headers set up.")


async def write_results_to_sheet(picks: list[dict]):
    """Append graded pick rows to the Google Sheet."""
    resolved = [p for p in picks if p["verdict"] in ("WIN", "LOSS", "PUSH")]
    if not resolved:
        return

    gc = get_sheets_client()
    if not gc:
        return

    sh = gc.open_by_key(SAUCE_SHEET_ID)
    ws = sh.sheet1

    col_a = await asyncio.to_thread(ws.col_values, 1)
    next_row = len(col_a) + 1

    rows = []
    for p in resolved:
        d = datetime.strptime(p["date"], "%Y-%m-%d")
        units = _parse_unit(p.get("unit", "1"))
        try:
            odds_int = int(p.get("odds", "0").replace("+", ""))
        except ValueError:
            odds_int = 0
        odds_fmt = f"{odds_int:+d}.00" if odds_int else ""

        if p["verdict"] == "WIN" and odds_int:
            if odds_int > 0:
                ret = units * (odds_int / 100)
            else:
                ret = units * (100 / abs(odds_int))
        elif p["verdict"] == "LOSS":
            ret = -units
        else:
            ret = 0.0

        pick_dict = {
            "bet_type": p.get("bet_type", ""),
            "direction": p.get("direction", ""),
            "line": p.get("line"),
            "is_parlay_leg": False,
        }
        bet_type = _map_bet_type(pick_dict, odds_int or None)
        wl = "win" if p["verdict"] == "WIN" else "lose" if p["verdict"] == "LOSS" else "push"

        rows.append([
            f"{d.month}/{d.day}/{d.year}",
            p["sport"],
            p["bet"],
            f"{units:.2f}",
            bet_type,
            odds_fmt,
            wl,
            f"{ret:.2f}",
        ])

    end_row = next_row + len(rows) - 1
    await asyncio.to_thread(
        ws.update, f"A{next_row}:H{end_row}", rows, value_input_option="USER_ENTERED"
    )
    print(f"Wrote {len(rows)} rows to sheet (rows {next_row}-{end_row}).")


# ─── Screenshot rendering ────────────────────────────────────────────────────

def _parse_unit(unit_str: str) -> float:
    s = unit_str.lower().replace("unit", "").replace("u", "").strip()
    try:
        return float(s)
    except ValueError:
        return 0.0


def _group_and_sort(pick_list: list[dict]) -> list[tuple[str, list[dict]]]:
    by_sport: dict[str, list[dict]] = {}
    for p in pick_list:
        by_sport.setdefault(p.get("sport", "Other"), []).append(p)
    for sport in by_sport:
        by_sport[sport].sort(key=lambda p: _parse_unit(p.get("unit", "0")), reverse=True)
    sport_order = ["NBA", "MLB", "NHL", "NFL", "NCAAB", "UFC", "Tennis", "Boxing", "KBO", "Other"]
    result = []
    for s in sport_order:
        if s in by_sport:
            result.append((s, by_sport[s]))
    for s in by_sport:
        if s not in sport_order:
            result.append((s, by_sport[s]))
    return result


def _format_date(p: dict) -> str:
    """Get display date from a pick (handles both '4/17' and 'YYYY-MM-DD')."""
    d = p.get("date", "")
    if "/" in d:
        return d
    if "-" in d:
        return _iso_to_display(d)
    return d


def render_html(upcoming: list[dict], past: list[dict]) -> str:
    """Render HTML table with 6 aligned columns throughout."""

    def make_rows(pick_list, include_result=False):
        groups = _group_and_sort(pick_list)
        rows = ""
        for sport, sport_picks in groups:
            rows += f'<tr class="sport"><td colspan="6">{sport}</td></tr>'
            for p in sport_picks:
                emoji = ""
                if include_result:
                    emoji = VERDICT_EMOJI.get(p.get("verdict", ""), "")
                rows += f"""<tr>
                    <td>{_format_date(p)}</td>
                    <td>Sauce</td>
                    <td class="bet">{p['bet']}</td>
                    <td>{p.get('odds', '')}</td>
                    <td>{p.get('unit', '')}</td>
                    <td class="result">{emoji}</td>
                </tr>"""
        return rows

    html = """<!DOCTYPE html>
<html><head><meta charset="utf-8">
<style>
  * { margin: 0; padding: 0; box-sizing: border-box; }
  body {
    font-family: Arial, Helvetica, sans-serif;
    background: white;
    display: inline-block;
  }
  table {
    border-collapse: collapse;
    width: 100%;
  }
  th, td {
    border: 1px solid #ccc;
    padding: 4px 10px;
    font-size: 13px;
    font-weight: 700;
    text-align: center;
    white-space: nowrap;
  }
  th {
    background: #f3f3f3;
    font-size: 11px;
    font-weight: 700;
    color: #333;
    padding: 3px 10px;
  }
  tr.section td {
    font-weight: 700;
    font-size: 13px;
    background: white;
  }
  tr.sport td {
    font-weight: 700;
    font-size: 11px;
    color: #7c4dff;
    background: #f5f0ff;
    letter-spacing: 1px;
    text-align: left;
    padding: 3px 10px;
  }
  td.bet {
    min-width: 180px;
  }
  td.result {
    font-size: 16px;
    min-width: 30px;
  }
</style>
</head><body>
<table>
  <tr><th>DATE</th><th>NAME</th><th>BET</th><th>ODDS</th><th>UNIT</th><th></th></tr>
"""

    if upcoming:
        html += '<tr class="section"><td></td><td></td><td>Upcoming</td><td></td><td></td><td></td></tr>'
        html += make_rows(upcoming)

    if past:
        html += '<tr class="section"><td></td><td></td><td>Past</td><td></td><td></td><td></td></tr>'
        html += make_rows(past, include_result=True)

    html += "</table></body></html>"
    return html


async def render_image(html: str) -> bytes:
    from playwright.async_api import async_playwright
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        page = await browser.new_page(viewport={"width": 600, "height": 100})
        await page.set_content(html)
        img_bytes = await page.locator("body").screenshot(type="png")
        await browser.close()
        return img_bytes


# ─── Main ─────────────────────────────────────────────────────────────────────

async def main():
    import argparse
    parser = argparse.ArgumentParser(description="Sauce daily picks")
    parser.add_argument("--channel", type=int, default=TEST_CHANNEL, help="Telegram channel ID")
    parser.add_argument("--grade-only", action="store_true", help="Only grade pending, no screenshot")
    parser.add_argument("--no-send", action="store_true", help="Skip Telegram send")
    parser.add_argument("--setup-sheet", action="store_true", help="Set up sheet headers")
    parser.add_argument("--days-back", type=int, default=3, help="Days back for past picks")
    args = parser.parse_args()

    _init_db()

    if args.setup_sheet:
        await setup_sheet_headers()
        return

    # Shared scoreboard cache across validation + grading
    scoreboard_cache: dict[tuple[str, str], dict | None] = {}

    # ── 1. Scrape ──
    print("Scraping SAUCE tab...")
    rows = fetch_tab("SAUCE")
    picks = parse_picks(rows)
    if not picks:
        print("No picks found!")
        return
    print(f"Found {len(picks)} picks from sheet.")

    # ── 2. Classify + parse ──
    print("Classifying and parsing via Claude...")
    picks = await classify_and_parse(picks)
    for p in picks:
        print(f"  {p.get('sport', '?'):5s}  {p['bet']:30s}  {p.get('bet_type', '?')}")

    # ── 2b. Validate against ESPN schedules ──
    print("Validating sports against ESPN schedules...")
    picks = await validate_sports(picks, scoreboard_cache)

    # ── 3. Store ──
    inserted = upsert_picks(picks)
    print(f"Upserted picks ({inserted} new).")

    # ── 4. Grade pending ──
    graded = await grade_pending(scoreboard_cache)

    # ── 5. Write to sheet ──
    if graded:
        await write_results_to_sheet(graded)

    if args.grade_only:
        cost = usage_cost()
        print(f"\n[Claude cost] {fmt_cost(cost)}")
        return

    # ── 6. Build screenshot data ──
    upcoming_picks = [p for p in picks if p.get("section") == "Upcoming"]

    today = _date.today().isoformat()
    past_cutoff = (_date.today() - timedelta(days=args.days_back)).isoformat()
    past_picks = get_picks_for_dates(past_cutoff, today)

    print(f"\nScreenshot: {len(upcoming_picks)} upcoming, {len(past_picks)} past")

    # ── 7. Render ──
    html = render_html(upcoming_picks, past_picks)
    img = await render_image(html)
    print(f"Image rendered ({len(img)} bytes)")

    preview_path = ROOT / "data" / "kirms_browser_state" / "preview.png"
    preview_path.parent.mkdir(parents=True, exist_ok=True)
    preview_path.write_bytes(img)

    if args.no_send:
        print(f"Preview saved to {preview_path}")
        cost = usage_cost()
        print(f"\n[Claude cost] {fmt_cost(cost)}")
        return

    # ── 8. Send to Telegram ──
    print(f"Sending to channel {args.channel}...")
    client = TelegramClient(StringSession(SESSION), API_ID, API_HASH)
    await client.start()
    buf = io.BytesIO(img)
    buf.name = "sauce_open_bets.png"
    await client.send_file(args.channel, buf)
    print("Sent!")
    await client.disconnect()

    cost = usage_cost()
    print(f"\n[Claude cost] {fmt_cost(cost)}")


if __name__ == "__main__":
    asyncio.run(main())
