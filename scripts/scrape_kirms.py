"""
Scrape Kyle Kirms open-bets from the published Google Sheet.

The open-bets page embeds a public Google Sheet with one tab per handicapper.
We fetch each tab's published HTML directly — no login or browser needed.

Usage:
    python scripts/scrape_kirms.py                # all tabs with data
    python scripts/scrape_kirms.py --tab SAUCE    # specific tab only
    python scripts/scrape_kirms.py --upcoming     # only upcoming picks
"""

import argparse
import json
from html.parser import HTMLParser

import httpx

SHEET_ID = "1yjaN85i-WRhRrBcozOG70vTX6cTNpJzFmuNJ8KgL-14"
BASE_URL = f"https://docs.google.com/spreadsheets/d/{SHEET_ID}/pubhtml"

# Each tab name -> gid
TABS = {
    "SAUCE":     "1868949549",
    "TOAST":     "1416285506",
    "ANDY":      "1336957950",
    "SCOOP":     "1200214817",
    "BEAVER":    "197765988",
    "JESS":      "1240718664",
    "COMMUNITY": "1368364799",
}



class TableParser(HTMLParser):
    """Simple HTML parser to extract table rows from Google Sheets pubhtml."""

    def __init__(self):
        super().__init__()
        self.rows: list[list[str]] = []
        self._in_row = False
        self._in_cell = False
        self._current_row: list[str] = []
        self._current_cell = ""

    def handle_starttag(self, tag, attrs):
        if tag == "tr":
            self._in_row = True
            self._current_row = []
        elif tag in ("td", "th") and self._in_row:
            self._in_cell = True
            self._current_cell = ""

    def handle_endtag(self, tag):
        if tag in ("td", "th") and self._in_cell:
            self._in_cell = False
            self._current_row.append(self._current_cell.strip())
        elif tag == "tr" and self._in_row:
            self._in_row = False
            if self._current_row:
                self.rows.append(self._current_row)

    def handle_data(self, data):
        if self._in_cell:
            self._current_cell += data


def fetch_tab(tab_name: str) -> list[list[str]]:
    """Fetch a single tab's data as a list of rows."""
    gid = TABS.get(tab_name.upper())
    if not gid:
        print(f"Unknown tab: {tab_name}")
        return []

    url = (f"{BASE_URL}?gid={gid}&single=true&widget=false"
           f"&range=a1:f180&chrome=false&headers=false")

    try:
        resp = httpx.get(url, timeout=15, follow_redirects=True)
        resp.raise_for_status()
    except httpx.HTTPStatusError:
        return []

    parser = TableParser()
    parser.feed(resp.text)
    return parser.rows


def parse_picks(rows: list[list[str]], upcoming_only: bool = False) -> list[dict]:
    """Parse raw table rows into structured pick dicts."""
    picks = []
    section = "Upcoming"

    for row in rows:
        # Skip empty rows and header row
        if not any(row):
            continue

        # The first column is a row number from the sheet — skip it
        # Columns: [row_num, DATE, NAME, BET, ODDS, UNIT, ...]
        cells = row[1:] if len(row) > 1 else row  # strip row number col

        # Detect header
        if len(cells) >= 4 and cells[0] == "DATE" and cells[2] == "BET":
            continue

        # Detect section markers
        bet_col = cells[2] if len(cells) > 2 else cells[0] if cells else ""
        if bet_col in ("Upcoming", "Past", "No Upcoming"):
            section = bet_col
            continue

        # Skip rows with no date (empty filler rows)
        if len(cells) < 4 or not cells[0]:
            continue

        if upcoming_only and section != "Upcoming":
            continue

        pick = {
            "section": section,
            "date": cells[0],
            "name": cells[1],
            "bet": cells[2],
            "odds": cells[3] if len(cells) > 3 else "",
            "unit": cells[4] if len(cells) > 4 else "",
        }
        # Clean up odds — remove parens if present
        pick["odds"] = pick["odds"].strip("()")
        picks.append(pick)

    return picks


def scrape_all(tabs: list[str] | None = None,
               upcoming_only: bool = False) -> dict[str, list[dict]]:
    """Scrape one or all tabs, return {tab_name: [picks]}."""
    target_tabs = tabs or list(TABS.keys())
    results = {}

    for tab in target_tabs:
        tab = tab.upper()
        rows = fetch_tab(tab)
        if not rows:
            continue
        picks = parse_picks(rows, upcoming_only)
        if picks:
            results[tab] = picks

    return results


def format_picks(all_picks: dict[str, list[dict]]) -> str:
    """Format picks into a readable text summary."""
    lines = []
    for tab, picks in all_picks.items():
        upcoming = [p for p in picks if p["section"] == "Upcoming"]
        past = [p for p in picks if p["section"] == "Past"]

        if upcoming:
            lines.append(f"\n=== {tab} — Upcoming ===")
            for p in upcoming:
                lines.append(f"  {p['date']}  {p['bet']:30s}  {p['odds']:>6s}  {p['unit']}")

        if past:
            lines.append(f"\n=== {tab} — Past ===")
            for p in past:
                lines.append(f"  {p['date']}  {p['bet']:30s}  {p['odds']:>6s}  {p['unit']}")

    return "\n".join(lines)


def main():
    parser = argparse.ArgumentParser(description="Scrape Kyle Kirms open-bets")
    parser.add_argument("--tab", help="Specific tab (SAUCE, TOAST, etc.)")
    parser.add_argument("--upcoming", action="store_true", help="Only upcoming picks")
    parser.add_argument("--json", action="store_true", help="Output as JSON")
    args = parser.parse_args()

    tabs = [args.tab] if args.tab else None
    results = scrape_all(tabs=tabs, upcoming_only=args.upcoming)

    if args.json:
        print(json.dumps(results, indent=2))
    else:
        print(format_picks(results))
        total = sum(len(v) for v in results.values())
        print(f"\nTotal: {total} picks across {len(results)} tabs")


if __name__ == "__main__":
    main()
