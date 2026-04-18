"""
sheets.py — Append graded pick results to a Google Sheet.
"""

import asyncio
import os
import re
from datetime import datetime

import gspread
from google.oauth2.service_account import Credentials

from audit import _format_pick
from common import VERDICT_EMOJI, parlay_combined_odds

_SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]
_gc: gspread.Client | None = None
_UNITS_RE = re.compile(r'(?:^|[(, ])(\d*\.?\d+)\s*u(?:nits?)?\b', re.IGNORECASE | re.MULTILINE)


def _get_client() -> gspread.Client | None:
    global _gc
    if _gc is not None:
        return _gc
    sa_path = os.getenv("GOOGLE_SERVICE_ACCOUNT_JSON", "")
    if not sa_path or not os.path.isfile(sa_path):
        return None
    creds = Credentials.from_service_account_file(sa_path, scopes=_SCOPES)
    _gc = gspread.authorize(creds)
    return _gc


def _extract_units(raw_text: str) -> float:
    m = _UNITS_RE.search(raw_text)
    return float(m.group(1)) if m else 1.0


def _map_bet_type(pick: dict, odds: int | None) -> str:
    bt = pick.get("bet_type", "")
    direction = pick.get("direction", "")
    line = pick.get("line")

    if pick.get("is_parlay_leg"):
        return "PARLAY"
    if bt in ("total", "team_total"):
        return "OVER" if direction == "over" else "UNDER"
    if bt == "spread":
        if line is not None:
            return "DOG SPREAD" if line > 0 else "FAV SPREAD"
    if bt == "moneyline":
        if odds is not None:
            return "DOG ML" if odds > 0 else "FAV ML"
        return "FAV ML"
    if bt == "prop":
        if direction == "over":
            return "OVER"
        if direction == "under":
            return "UNDER"
    return bt.upper()


def _build_description(pick: dict, verdict: str, odds: int | None) -> str:
    desc = _format_pick(pick).upper().replace("/", " ")
    if odds is not None:
        sign = "+" if odds > 0 else ""
        desc += f" [{sign}{odds}]"
    emoji = VERDICT_EMOJI.get(verdict, "")
    return f"{desc}{emoji}"


def _fmt_odds(o: int | None) -> str:
    if o is None:
        return ""
    return f"{o:+d}.00" if o != 0 else "0.00"


def _copy_formulas(prev_formulas: list[str], prev_row: int, new_row: int) -> list[str]:
    """Adjust row references in formulas from prev_row to new_row."""
    result = []
    for f in prev_formulas:
        if not f or not f.startswith("="):
            result.append(f)
            continue
        adjusted = re.sub(
            r'([A-Z])(\d+)',
            lambda m: m.group(1) + str(new_row if int(m.group(2)) == prev_row else new_row - 1 if int(m.group(2)) == prev_row - 1 else int(m.group(2))),
            f,
        )
        result.append(adjusted)
    return result


async def append_pick_rows(
    *,
    pick_results: list[tuple[dict, str, int | None]],
    date_str: str,
    raw_text: str,
    sheets_id: str,
) -> None:
    """Append one row per resolved pick (or one row per parlay) to the Google Sheet.

    sheets_id: "spreadsheet_id:gid" string from the mapping config.
    """
    if not sheets_id:
        return

    gc = _get_client()
    if gc is None:
        return

    parts = sheets_id.split(":", 1)
    sheet_id = parts[0]
    gid = int(parts[1]) if len(parts) > 1 else 0
    spreadsheet = gc.open_by_key(sheet_id)
    worksheet = next(
        (ws for ws in spreadsheet.worksheets() if ws.id == gid),
        spreadsheet.sheet1,
    )

    d = datetime.strptime(date_str, "%Y-%m-%d")
    date_formatted = f"{d.month}/{d.day}/{d.year}"
    units = f"{_extract_units(raw_text):.2f}"

    _DEFAULT_ODDS = -150
    resolved = [(p, v, o if o is not None else _DEFAULT_ODDS) for p, v, o in pick_results if v in ("WIN", "LOSS", "PUSH")]
    if not resolved:
        return

    is_parlay = any(p.get("is_parlay_leg") for p, _, _ in resolved)

    rows_to_append: list[list] = []
    if is_parlay:
        sport = (resolved[0][0].get("sport") or "").upper()
        legs_desc = " / ".join(_format_pick(p).upper() for p, _, _ in resolved)
        combined = parlay_combined_odds([o for p, _, o in resolved if p.get("is_parlay_leg")])
        overall_verdict = "LOSS" if any(v == "LOSS" for _, v, _ in resolved) else \
                          "WIN" if all(v == "WIN" for _, v, _ in resolved) else \
                          "PUSH"
        emoji = VERDICT_EMOJI.get(overall_verdict, "")
        if combined is not None:
            sign = "+" if combined > 0 else ""
            desc = f"{legs_desc} [{sign}{combined}]{emoji}"
        else:
            desc = f"{legs_desc}{emoji}"
        result = "win" if overall_verdict == "WIN" else "lose" if overall_verdict == "LOSS" else "push"
        odds_val = f"{combined:.2f}" if combined is not None else f"{_DEFAULT_ODDS:.2f}"
        rows_to_append.append(["", "", date_formatted, sport, desc, units, "PARLAY", odds_val, result])
    else:
        for pick, verdict, odds in resolved:
            sport = (pick.get("sport") or "").upper()
            description = _build_description(pick, verdict, odds)
            bet_type = _map_bet_type(pick, odds)
            odds_val = f"{odds:.2f}"
            result = "win" if verdict == "WIN" else "lose" if verdict == "LOSS" else "push"
            rows_to_append.append(["", "", date_formatted, sport, description, units, bet_type, odds_val, result])

    # Find the last data row and read its formulas to copy forward
    col_c_vals = worksheet.col_values(3)  # column C (date)
    last_data_row = len(col_c_vals)
    next_row = last_data_row + 1

    # Number of data columns we write (A-I = 9)
    data_cols = len(rows_to_append[0])
    # Read the full last row to discover formula columns beyond our data
    last_row_vals = await asyncio.to_thread(
        worksheet.row_values, last_data_row, value_render_option="FORMULA"
    )
    formula_cols = last_row_vals[data_cols:] if len(last_row_vals) > data_cols else []
    total_cols = data_cols + len(formula_cols)
    last_col_letter = chr(ord('A') + total_cols - 1) if total_cols <= 26 else 'Z'

    for i, data_row in enumerate(rows_to_append):
        row_num = next_row + i
        if formula_cols:
            formulas = _copy_formulas(formula_cols, last_data_row + i, row_num)
            full_row = data_row + formulas
        else:
            full_row = data_row
        await asyncio.to_thread(
            worksheet.update,
            f"A{row_num}:{last_col_letter}{row_num}",
            [full_row],
            value_input_option="USER_ENTERED",
        )

    # Copy formatting (borders, font, alignment, colors) from the last
    # existing row to all newly appended rows in a single batch request.
    fmt_requests = [{
        "copyPaste": {
            "source": {
                "sheetId": worksheet.id,
                "startRowIndex": last_data_row - 1,
                "endRowIndex": last_data_row,
                "startColumnIndex": 0,
                "endColumnIndex": total_cols,
            },
            "destination": {
                "sheetId": worksheet.id,
                "startRowIndex": next_row - 1,
                "endRowIndex": next_row - 1 + len(rows_to_append),
                "startColumnIndex": 0,
                "endColumnIndex": total_cols,
            },
            "pasteType": "PASTE_FORMAT",
        }
    }]
    # Set result column (I) background color based on verdict
    result_col_index = 8  # column I (0-indexed)
    _COLORS = {
        "win":  {"red": 0, "green": 1, "blue": 0},  # pure green
        "lose": {"red": 1, "green": 0, "blue": 0},  # pure red
        "push": {"red": 1, "green": 1, "blue": 0},  # yellow
    }
    for i, data_row in enumerate(rows_to_append):
        result_val = data_row[result_col_index]
        color = _COLORS.get(result_val)
        if color:
            fmt_requests.append({
                "repeatCell": {
                    "range": {
                        "sheetId": worksheet.id,
                        "startRowIndex": next_row - 1 + i,
                        "endRowIndex": next_row + i,
                        "startColumnIndex": result_col_index,
                        "endColumnIndex": result_col_index + 1,
                    },
                    "cell": {
                        "userEnteredFormat": {
                            "backgroundColor": color,
                        }
                    },
                    "fields": "userEnteredFormat.backgroundColor",
                }
            })

    await asyncio.to_thread(
        spreadsheet.batch_update, {"requests": fmt_requests}
    )
