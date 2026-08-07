"""
sheets_export.py — Push a formatted pick CSV into a Google Sheets tab.

Step 5 of the capper backfill pipeline:
    fetch_x_posts -> parse_posts_csv -> grade_csv -> format_graded_csv -> sheets_export

Writes <account>_sheet.csv into a worksheet named after the account, inside one
shared workbook. One tab per capper keeps every backfill in a single place.

    python scripts/sheets_export.py --account boyerBets_

Setup (once): create a Google Sheet, share it with the service account as Editor,
and put its id in BACKFILL_SHEETS_ID in .env.

    forwarder@api-project-349700129720.iam.gserviceaccount.com

Why a pre-made workbook rather than creating one per run: the service account has
Sheets API access but the project's Drive API is disabled, so gc.create() fails
with a 403. Writing into a shared workbook needs only the Sheets scope. (Enabling
the Drive API would also allow create+share, but that is console work and this
avoids it.)
"""

import argparse
import csv
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import gspread
from google.oauth2.service_account import Credentials
from dotenv import load_dotenv

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
load_dotenv(os.path.join(ROOT, ".env"))
load_dotenv(os.path.join(ROOT, ".env.local"), override=True)

OUT_DIR = os.path.join(os.path.dirname(__file__), "output")
_SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]

# Column G holds "win"/"lose"/"push" in the format_graded_csv layout.
_WL_COL = 6
_COLORS = {
    "win":  {"red": 0.78, "green": 0.94, "blue": 0.78},
    "lose": {"red": 0.98, "green": 0.80, "blue": 0.80},
    "push": {"red": 1.00, "green": 0.95, "blue": 0.75},
}


class SheetsExportError(RuntimeError):
    pass


def _client() -> gspread.Client:
    sa = os.getenv("GOOGLE_SERVICE_ACCOUNT_JSON", "")
    if not sa:
        raise SheetsExportError("GOOGLE_SERVICE_ACCOUNT_JSON is not set")
    if not os.path.isabs(sa):
        sa = os.path.join(ROOT, sa)
    if not os.path.isfile(sa):
        raise SheetsExportError(f"service account json not found: {sa}")
    return gspread.authorize(Credentials.from_service_account_file(sa, scopes=_SCOPES))


def _coerce(value: str):
    """Send numbers as numbers so the sheet can sum Return without cleanup."""
    try:
        return int(value)
    except ValueError:
        pass
    try:
        return float(value)
    except ValueError:
        return value


def export(account: str, sheet_id: str | None = None,
           tab: str | None = None, csv_path: str | None = None) -> str:
    sheet_id = sheet_id or os.getenv("BACKFILL_SHEETS_ID", "")
    if not sheet_id:
        raise SheetsExportError(
            "No target workbook. Set BACKFILL_SHEETS_ID in .env (or pass --sheet-id).\n"
            "Create a Google Sheet and share it as Editor with:\n"
            "  forwarder@api-project-349700129720.iam.gserviceaccount.com"
        )
    # Accept a full URL as well as a bare id — pasting the URL is the natural thing to do.
    if "/spreadsheets/d/" in sheet_id:
        sheet_id = sheet_id.split("/spreadsheets/d/")[1].split("/")[0]

    path = csv_path or os.path.join(OUT_DIR, f"{account}_sheet.csv")
    with open(path, newline="", encoding="utf-8") as f:
        rows = list(csv.reader(f))
    if not rows:
        raise SheetsExportError(f"{path} is empty")

    header, data = rows[0], rows[1:]
    values = [header] + [[_coerce(c) for c in r] for r in data]

    gc = _client()
    try:
        book = gc.open_by_key(sheet_id)
    except (gspread.exceptions.APIError, PermissionError) as e:
        # gspread turns a 403 from open_by_key into a bare builtin PermissionError,
        # so catching only APIError lets the unshared-workbook case escape as a
        # traceback instead of the one-line fix below.
        raise SheetsExportError(
            f"Cannot open workbook {sheet_id} ({type(e).__name__}).\n"
            "Share it as Editor with:\n"
            "  forwarder@api-project-349700129720.iam.gserviceaccount.com"
        ) from e

    title = tab or account.rstrip("_") or account
    try:
        ws = book.worksheet(title)
        ws.clear()
        ws.resize(rows=max(len(values) + 10, 20), cols=len(header))
    except gspread.exceptions.WorksheetNotFound:
        ws = book.add_worksheet(title=title,
                                rows=max(len(values) + 10, 20), cols=len(header))

    ws.update(range_name="A1", values=values, value_input_option="USER_ENTERED")

    last_col = chr(ord("A") + len(header) - 1)
    reqs = [
        {"repeatCell": {
            "range": {"sheetId": ws.id, "startRowIndex": 0, "endRowIndex": 1},
            "cell": {"userEnteredFormat": {
                "textFormat": {"bold": True},
                "backgroundColor": {"red": 0.87, "green": 0.87, "blue": 0.87},
            }},
            "fields": "userEnteredFormat(textFormat,backgroundColor)",
        }},
        {"updateSheetProperties": {
            "properties": {"sheetId": ws.id,
                           "gridProperties": {"frozenRowCount": 1}},
            "fields": "gridProperties.frozenRowCount",
        }},
        {"setBasicFilter": {"filter": {"range": {
            "sheetId": ws.id, "startRowIndex": 0,
            "endRowIndex": len(values), "startColumnIndex": 0,
            "endColumnIndex": len(header)}}}},
        {"autoResizeDimensions": {"dimensions": {
            "sheetId": ws.id, "dimension": "COLUMNS",
            "startIndex": 0, "endIndex": len(header)}}},
    ]
    # Colour each row's W/L cell. Batched into one request per verdict class
    # rather than one per row, so a 200-row backfill stays a single API call.
    for verdict, colour in _COLORS.items():
        for i, row in enumerate(data):
            if len(row) > _WL_COL and row[_WL_COL] == verdict:
                reqs.append({"repeatCell": {
                    "range": {"sheetId": ws.id,
                              "startRowIndex": i + 1, "endRowIndex": i + 2,
                              "startColumnIndex": _WL_COL,
                              "endColumnIndex": _WL_COL + 1},
                    "cell": {"userEnteredFormat": {"backgroundColor": colour}},
                    "fields": "userEnteredFormat.backgroundColor",
                }})
    book.batch_update({"requests": reqs})

    url = f"https://docs.google.com/spreadsheets/d/{sheet_id}/edit#gid={ws.id}"
    print(f"Wrote {len(data)} rows to tab '{title}' ({last_col}{len(values)})")
    print(f"Sheet: {url}")
    return url


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--account", required=True, help="X handle, e.g. boyerBets_")
    p.add_argument("--sheet-id", default=None,
                   help="Target workbook id or URL (default: BACKFILL_SHEETS_ID)")
    p.add_argument("--tab", default=None, help="Worksheet name (default: account)")
    p.add_argument("--csv", default=None, help="Override input CSV path")
    a = p.parse_args()
    try:
        export(a.account, a.sheet_id, a.tab, a.csv)
    except SheetsExportError as e:
        print(f"ERROR: {e}")
        sys.exit(1)
