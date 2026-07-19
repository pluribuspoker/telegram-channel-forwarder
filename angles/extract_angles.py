#!/usr/bin/env python3
"""
Extract angle performance data from Telegram channel picks.

Scrapes messages from the destination channel, extracts blockquoted angle
records, parses them into structured data, enriches with grading data from
picks.db, and outputs JSON for the web analyzer.

Usage:
    python scripts/extract_angles.py                  # full extraction
    python scripts/extract_angles.py --limit 100      # test with 100 msgs
    python scripts/extract_angles.py --output path    # custom output path
"""

import asyncio
import argparse
import datetime
import json
import os
import re
import sqlite3
import sys

from dotenv import load_dotenv
from telethon import TelegramClient
from telethon.sessions import StringSession
from telethon.tl.types import MessageEntityBlockquote

load_dotenv(override=True)
load_dotenv(".env.local", override=True)

CHANNEL_ID = -1002486251914
REPO_CREATED = datetime.date(2026, 3, 23)

# ── Sport / bet-type / side / day maps ────────────────────────────────────

SPORTS_MAP = {
    "mlb": "MLB", "nba": "NBA", "nfl": "NFL", "nhl": "NHL",
    "ncaab": "NCAAB", "ncaaf": "NCAAF", "wnba": "WNBA",
    "cfl": "CFL", "ufl": "UFL", "soccer": "Soccer",
    "mls": "Soccer", "ufc": "UFC", "tennis": "Tennis",
    "kbo": "KBO", "boxing": "Boxing", "lacrosse": "Lacrosse",
    "cbb": "NCAAB", "cfb": "NCAAF", "wc": "Soccer",
    "world cup": "Soccer",
}

BET_TYPES_MAP = {
    "moneyline": "ML", "money line": "ML", "ml": "ML",
    "spreads": "Spread", "spread": "Spread",
    "run line": "Spread", "runline": "Spread",
    "overs": "Over", "over": "Over",
    "unders": "Under", "under": "Under",
    "totals": "Total", "total": "Total",
    "first 5": "F5", "f5": "F5",
    "props": "Prop", "prop": "Prop",
    "btts": "BTTS", "both to score": "BTTS",
    "team total": "Team Total", "tt": "Team Total",
    "parlays": "Parlay", "parlay": "Parlay",
    "3-way ml": "3-Way ML", "3-way": "3-Way ML",
    "60-min ml": "3-Way ML", "3way": "3-Way ML",
    "1h": "1H", "2h": "2H", "1st half": "1H", "2nd half": "2H",
}

SIDES_MAP = {
    "dogs": "Dog", "dog": "Dog", "underdog": "Dog", "underdogs": "Dog",
    "favs": "Fav", "fav": "Fav", "favorite": "Fav",
    "favourites": "Fav", "favorites": "Fav",
}

DAYS_MAP = {
    "mondays": "Monday", "monday": "Monday", "mon": "Monday", "mnf": "Monday",
    "tuesdays": "Tuesday", "tuesday": "Tuesday", "tue": "Tuesday",
    "wednesdays": "Wednesday", "wednesday": "Wednesday", "wed": "Wednesday",
    "thursdays": "Thursday", "thursday": "Thursday", "thu": "Thursday", "tnf": "Thursday",
    "fridays": "Friday", "friday": "Friday", "fri": "Friday",
    "saturdays": "Saturday", "saturday": "Saturday", "sat": "Saturday",
    "sundays": "Sunday", "sunday": "Sunday", "sun": "Sunday", "snf": "Sunday",
}

# ── Angle field extractors ────────────────────────────────────────────────

_OFF_RE = re.compile(
    r"(?:coming\s+|after\s+)?off\s+(?:a\s+)?(\d+)"
    r"(?:\s+\w+){0,3}"  # up to 3 intervening words (e.g. "off 2 MLB losses")
    r"\s+(loss(?:es)?|wins?|[lw])\b",
    re.I,
)
_OFF_ABBR_RE = re.compile(
    r"(?:coming\s+|after\s+)?off\s+(\d+)\s*([lw])\b",
    re.I,
)


_AFTER_RE = re.compile(
    r"\bafter\s+(\d+)\s+(loss(?:es)?|wins?)\b", re.I,
)
_OF_TYPO_RE = re.compile(
    r"\bof\s+(\d+)\s+(loss(?:es)?|wins?)\b", re.I,
)
_OFF_BARE_RE = re.compile(
    r"\boff\s+(\d+)\s*$", re.I,
)


def _extract_off(text: str):
    m = _OFF_RE.search(text)
    if m:
        return ("losses" if m.group(2).lower().startswith("l") else "wins"), int(m.group(1))
    m = _OFF_ABBR_RE.search(text)
    if m:
        return ("losses" if m.group(2).lower() == "l" else "wins"), int(m.group(1))
    # "after N losses/wins" (without "off")
    m = _AFTER_RE.search(text)
    if m:
        return ("losses" if m.group(2).lower().startswith("l") else "wins"), int(m.group(1))
    # "of N losses/wins" (common typo for "off")
    m = _OF_TYPO_RE.search(text)
    if m:
        return ("losses" if m.group(2).lower().startswith("l") else "wins"), int(m.group(1))
    # "off a loss" / "off a win" without number
    m = re.search(r"(?:coming\s+|after\s+)?off\s+a\s+(loss|win)\b", text, re.I)
    if m:
        return ("losses" if "l" in m.group(1).lower() else "wins"), 1
    # Bare "off N" at end of string (default to losses)
    m = _OFF_BARE_RE.search(text)
    if m:
        return "losses", int(m.group(1))
    return None, None


def _extract_sport(text: str):
    tl = text.lower()
    for pat, sport in SPORTS_MAP.items():
        if re.search(r"\b" + re.escape(pat) + r"\b", tl):
            return sport
    return None


def _extract_bet_type(text: str):
    tl = text.lower()
    for pat, bt in BET_TYPES_MAP.items():
        if re.search(r"\b" + re.escape(pat) + r"\b", tl):
            return bt
    return None


def _extract_side(text: str):
    tl = text.lower()
    for pat, side in SIDES_MAP.items():
        if re.search(r"\b" + re.escape(pat) + r"\b", tl):
            return side
    return None


def _extract_day(text: str):
    tl = text.lower()
    for pat, day in DAYS_MAP.items():
        if re.search(r"\b" + re.escape(pat) + r"\b", tl):
            return day
    return None


def _extract_scope(text: str):
    tl = text.lower()
    if re.search(r"\boverall\b", tl):
        return "Overall"
    if re.search(r"\btoday\b", tl):
        return "Today"
    # "after his/the/a" is too vague for a meaningful time window — skip
    if re.search(r"\bytd\b", tl):
        return "YTD"
    m = re.search(r"l\s*(\d+)\s*days?|last\s+(\d+)\s+days?", tl)
    if m:
        n = m.group(1) or m.group(2)
        return f"L{n}"
    m = re.search(r"\blast\s+(\d+)\b", tl)
    if m:
        return f"L{m.group(1)}"
    m = re.search(r"\bl(\d+)\b", tl)
    if m:
        return f"L{m.group(1)}"
    if re.search(r"\bthis\s+month\b", tl):
        return "This Month"
    if re.search(r"\bthis\s+(?:season|szn|year)\b", tl):
        return "This Season"
    # "since" + date (4/21, 1/1/25)
    m = re.search(r"\bsince\s+(\d{1,2}/\d{1,2}(?:/\d{2,4})?)", tl)
    if m:
        return f"Since {m.group(1)}"
    # "since" + year (2024, '25)
    m = re.search(r"\bsince\s+'?(\d{2,4})\b", tl)
    if m and (len(m.group(1)) == 4 or int(m.group(1)) > 20):
        return f"Since {m.group(1)}"
    # "since" + month name
    _months_re = "january|february|march|april|may|june|july|august|september|october|november|december"
    m = re.search(r"\bsince\s+(" + _months_re + r")\b", tl)
    if m:
        return f"Since {m.group(1).capitalize()}"
    m = re.search(r"\bin\s+(20\d{2})\b", tl)
    if m:
        return f"In {m.group(1)}"
    months = (
        "january|february|march|april|may|june|"
        "july|august|september|october|november|december"
    )
    m = re.search(r"\bin\s+(" + months + r")\b", tl)
    if m:
        return f"In {m.group(1).capitalize()}"
    # Standalone month as scope (e.g. "13-3 August")
    m = re.search(r"\b(" + months + r")\b", tl)
    if m and not re.search(r"since|in\s+", tl[: m.start()]):
        return f"In {m.group(1).capitalize()}"
    return None


def _extract_unit(text: str):
    tl = text.lower()
    m = re.search(r">=?\s*(\d+(?:\.\d+)?)\s*u\b", tl)
    if m:
        return f">={m.group(1)}U"
    if re.search(r"\bsuper\s*max\b", tl):
        return "Super Max"
    if re.search(r"\bmax\b", tl):
        return "Max"
    if re.search(r"\bpremium", tl):
        return "Premium"
    m = re.search(r"\b(\d+(?:\.\d+)?)\s*u(?:nits?)?\b", tl)
    if m:
        return f"{m.group(1)}U"
    return None


# ── Angle type classification ─────────────────────────────────────────────


def _classify_type(text: str, wins: int, losses: int) -> str:
    tl = text.lower()
    off_type, _ = _extract_off(text)
    if off_type:
        return f"off_{off_type}"
    if re.search(r"\b(?:run|stretch|cooling|regression|skid|slide|streak|surge|tear|heater|cold\s*spell)\b", tl):
        return "run"
    if _extract_unit(text):
        return "unit_record"
    if _extract_day(text) and not _extract_sport(text) and not _extract_bet_type(text):
        return "day_record"
    # Separate sport / bet-type / side into distinct types.
    # Bet-type is most specific, then side, then sport.
    if _extract_bet_type(text):
        return "bet_type_record"
    if _extract_sport(text):
        return "sport_record"
    # Time-scoped: includes "last N", "L30 days", "this month", "YTD", etc.
    if _extract_scope(text) or re.search(r"\blast\s+\d+\b|\bl\d+\b|\bl\s+\d+\b", tl):
        return "time_scoped"
    return "commentary"


# ── Parse one angle segment ───────────────────────────────────────────────


_PROSE_STARTERS = re.compile(
    r"^(?:the|this|he|she|but|if|from|that|it|so|also|according)\s",
    re.I,
)
_ANGLE_KEYWORDS = re.compile(
    r"\b(?:run|stretch|cooling|off\s+\d|after\s+\d|last\s+\d|l\d+)\b",
    re.I,
)


def _parse_segment(text: str, parent_ctx: str = "") -> list[dict]:
    text = text.strip()
    if not text or not re.search(r"\d+-\d+", text):
        return []

    # Handle "X-Y vs X-Y" — extract both records as separate angles
    # Only when vs is the primary structure (not embedded in a longer sentence)
    vs_match = re.match(r"^\s*(\d+-\d+)\s+vs\.?\s+(\d+-\d+)\s*$", text)
    if vs_match:
        results: list[dict] = []
        for part in re.split(r"\s+vs\.?\s+", text):
            results.extend(_parse_segment(part.strip(), parent_ctx=parent_ctx))
        return results

    # Extract parenthetical sub-records first, pass main text as parent context
    sub_records: list[dict] = []
    for pm in re.finditer(r"\((\d+-\d+[^)]*)\)", text):
        main_for_ctx = re.sub(r"\([^)]*\)", "", text).strip()
        sub_records.extend(_parse_segment(pm.group(1), parent_ctx=main_for_ctx))

    main_text = re.sub(r"\([^)]*\)", "", text).strip()
    if not re.search(r"\d+-\d+", main_text):
        return sub_records  # record was only inside parens

    # Skip prose lines where the record is buried deep in a sentence,
    # UNLESS the line contains angle keywords (run, stretch, off N, etc.)
    rm = re.search(r"\d+-\d+", main_text)
    if rm and rm.start() > 20 and _PROSE_STARTERS.match(main_text):
        if not _ANGLE_KEYWORDS.search(main_text):
            return sub_records

    m = re.search(r"(\d+)-(\d+)", main_text)
    wins, losses = int(m.group(1)), int(m.group(2))
    if wins > 500 or losses > 500:
        return sub_records

    off_type, off_count = _extract_off(main_text)
    # If main text has no off-signal, check full text (parens may hold context)
    if not off_type:
        off_type, off_count = _extract_off(text)
    atype = _classify_type(main_text, wins, losses)
    # If commentary, try full text with parens for better type
    if atype == "commentary":
        full_type = _classify_type(text, wins, losses)
        if full_type != "commentary":
            atype = full_type
    # If still commentary, try parent context (for sub-records from parens)
    if atype == "commentary" and parent_ctx:
        parent_type = _classify_type(parent_ctx, wins, losses)
        if parent_type != "commentary":
            atype = parent_type

    sport = _extract_sport(main_text)
    bet_type = _extract_bet_type(main_text)
    side = None
    scope = _extract_scope(main_text)
    # Inherit missing fields from parent context (sub-records from parens)
    if parent_ctx:
        if not sport:
            sport = _extract_sport(parent_ctx)
        if not bet_type:
            bet_type = _extract_bet_type(parent_ctx)

    # Undefeated/winless only meaningful for deeper angles (records in a
    # specific context), not raw streaks like "6-0 run" or "7-3 last 10".
    _deeper = atype not in ("run", "time_scoped")
    angle = {
        "raw": text,
        "wins": wins,
        "losses": losses,
        "type": atype,
        "sport": sport,
        "bet_type": bet_type,
        "side": side,
        "day": _extract_day(main_text),
        "scope": scope,
        "unit": _extract_unit(main_text),
        "off_type": off_type,
        "off_count": off_count,
        "is_undefeated": _deeper and losses == 0 and wins > 0,
        "is_winless": _deeper and wins == 0 and losses > 0,
    }
    return [angle] + sub_records


def parse_blockquote(bq_text: str) -> list[dict]:
    """Parse all angle records from a blockquote.

    Tracks context from header lines (e.g. "L30 days:", "This month:") and
    applies their scope to subsequent bare-record lines.
    """
    angles: list[dict] = []
    ctx_scope: str | None = None

    for line in bq_text.split("\n"):
        line = line.strip()
        if not line:
            ctx_scope = None  # blank line resets header context
            continue

        # Header line (ends with ":") with no record — sets scope context
        if re.search(r":\s*$", line) and not re.search(r"\d+-\d+", line):
            ctx_scope = _extract_scope(line)
            continue

        if not re.search(r"\d+-\d+", line):
            continue

        for seg in re.split(r"\s*;\s*", line):
            parsed = _parse_segment(seg)
            # Apply header scope context to bare records
            if ctx_scope:
                for a in parsed:
                    if a["type"] == "commentary" and not a["scope"]:
                        a["scope"] = ctx_scope
                        a["type"] = "time_scoped"
            angles.extend(parsed)

    return angles


# ── Message-level helpers ─────────────────────────────────────────────────

_ODDS_BRACKET_RE = re.compile(r"\[([+-]\d{3,4})(?:\s|])")
_ODDS_PAREN_RE = re.compile(r"\(([+-]\d{3,4})\)")

_VERDICT_MAP = {
    "\u2705": "WIN",
    "\u274c": "LOSS",
    "\u267b\ufe0f": "PUSH",
    "\u267b": "PUSH",
    "\u2753": "UNKNOWN",
    "\u23f3": "PENDING",
}


def _extract_verdict(text: str) -> str:
    for char, v in _VERDICT_MAP.items():
        if char in text:
            return v
    return "PENDING"


def _extract_odds(text: str) -> int | None:
    m = _ODDS_BRACKET_RE.search(text)
    if m:
        return int(m.group(1))
    m = _ODDS_PAREN_RE.search(text)
    if m:
        return int(m.group(1))
    return None


def _pick_text(text: str, bq_ranges: list[tuple[int, int]]) -> str:
    """Non-blockquote text minus capper-name line."""
    parts, prev = [], 0
    for s, e in sorted(bq_ranges):
        parts.append(text[prev:s])
        prev = e
    parts.append(text[prev:])
    lines = "".join(parts).strip().split("\n")
    return "\n".join(l.strip() for l in lines[1:] if l.strip())


def _profit(odds: int | None, verdict: str) -> float:
    if verdict == "WIN":
        if odds is None:
            return 0.909  # -110 default
        return odds / 100 if odds > 0 else 100 / abs(odds)
    if verdict == "LOSS":
        return -1.0
    return 0.0


def _progress(stage, **kw):
    """Emit structured progress for the dashboard server (SSE)."""
    print("PROGRESS:" + json.dumps({"stage": stage, **kw}), flush=True)


# ── Main extraction ───────────────────────────────────────────────────────


async def extract(limit: int | None = None, output_path: str | None = None):
    _progress("connecting")
    client = TelegramClient(
        StringSession(os.environ["TELEGRAM_SESSION"]),
        int(os.environ["TELEGRAM_API_ID"]),
        os.environ["TELEGRAM_API_HASH"],
    )
    await client.start()
    _progress("connected")

    # Load grades from picks.db for enrichment
    db_path = os.path.join(os.path.dirname(os.path.dirname(__file__)), "picks.db")
    grades: dict[int, dict] = {}
    if os.path.exists(db_path):
        db = sqlite3.connect(db_path)
        db.row_factory = sqlite3.Row
        for row in db.execute(
            "SELECT message_id, sport, bet_type, verdict, odds, pick_desc, capper_name "
            "FROM grades WHERE channel_id = ?",
            (CHANNEL_ID,),
        ):
            grades[row["message_id"]] = dict(row)
        db.close()
        print(f"Loaded {len(grades)} grades from picks.db")
        _progress("grades", count=len(grades))

    picks: list[dict] = []
    seen_msg_ids: set[int] = set()
    total_scanned = 0
    total_with_angles = 0

    _NO_ANGLE = [{"raw": "(none)", "wins": 0, "losses": 0, "type": "no_angle",
                   "sport": None, "bet_type": None, "side": None, "day": None,
                   "scope": None, "unit": None, "off_type": None, "off_count": None,
                   "is_undefeated": False, "is_winless": False}]

    async for msg in client.iter_messages(CHANNEL_ID, limit=limit):
        total_scanned += 1
        if msg.date and msg.date.date() < REPO_CREATED:
            continue
        if not msg.text:
            continue

        # Capper name = first line, stripped of emojis
        lines = msg.text.split("\n")
        capper = re.sub(r"[\u2705\u274c\u267b\u2753\u23f3\ufe0f]", "", lines[0]).strip() if lines else ""

        verdict = _extract_verdict(msg.text)
        odds = _extract_odds(msg.text)

        # Enrich from grades table
        gd = grades.get(msg.id, {})
        sport = gd.get("sport")
        bet_type = gd.get("bet_type")
        if gd.get("verdict") and gd["verdict"] not in ("PENDING", "UNKNOWN"):
            verdict = gd["verdict"]
        if gd.get("odds") and not odds:
            odds = gd["odds"]
        if gd.get("capper_name") and not capper:
            capper = gd["capper_name"]

        # Extract blockquotes and parse angles
        bq_ranges = [
            (e.offset, e.offset + e.length)
            for e in (msg.entities or [])
            if isinstance(e, MessageEntityBlockquote)
        ]
        bq_texts = [msg.text[s:e] for s, e in bq_ranges] if bq_ranges else []
        pick_t = _pick_text(msg.text, bq_ranges) if bq_ranges else "\n".join(
            l.strip() for l in lines[1:] if l.strip()
        )

        all_angles: list[dict] = []
        if bq_texts and any(re.search(r"\d+-\d+", bq) for bq in bq_texts):
            for bq in bq_texts:
                all_angles.extend(parse_blockquote(bq))

        has_angles = bool(all_angles)
        if has_angles:
            total_with_angles += 1

        # Skip messages that aren't picks (no grade entry AND no verdict emoji)
        if not gd and verdict == "PENDING":
            continue

        seen_msg_ids.add(msg.id)
        picks.append(
            {
                "msg_id": msg.id,
                "date": msg.date.strftime("%Y-%m-%d") if msg.date else None,
                "capper": capper,
                "pick_text": pick_t[:500],
                "verdict": verdict,
                "odds": odds,
                "sport": sport,
                "bet_type": bet_type,
                "profit": round(_profit(odds, verdict), 4),
                "angles_raw": "\n".join(bq_texts) if bq_texts else "",
                "angles": all_angles if has_angles else _NO_ANGLE,
            }
        )

        if total_scanned % 100 == 0:
            _progress("scan", scanned=total_scanned, angles=total_with_angles)
        if total_scanned % 500 == 0:
            print(f"  scanned {total_scanned}, found {total_with_angles} with angles …")

    _progress("enriching")
    # Also pick up any graded messages we missed (no text / entities edge cases)
    for mid, gd in grades.items():
        if mid in seen_msg_ids:
            continue
        v = gd.get("verdict", "PENDING")
        if v in ("PENDING", "UNKNOWN"):
            continue
        odds = gd.get("odds")
        picks.append(
            {
                "msg_id": mid,
                "date": None,
                "capper": gd.get("capper_name", ""),
                "pick_text": gd.get("pick_desc", ""),
                "verdict": v,
                "odds": odds,
                "sport": gd.get("sport"),
                "bet_type": gd.get("bet_type"),
                "profit": round(_profit(odds, v), 4),
                "angles_raw": "",
                "angles": _NO_ANGLE,
            }
        )

    await client.disconnect()

    picks.sort(key=lambda p: p["date"] or "")

    output = {
        "extracted_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "channel_id": CHANNEL_ID,
        "total_messages_scanned": total_scanned,
        "total_with_angles": total_with_angles,
        "total_graded": sum(1 for p in picks if p["verdict"] in ("WIN", "LOSS", "PUSH")),
        "picks": picks,
    }

    if not output_path:
        output_path = os.path.join(
            os.path.dirname(__file__), "data", "angles.json"
        )
    _progress("writing")
    os.makedirs(os.path.dirname(output_path), exist_ok=True)
    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(output, f, indent=2, ensure_ascii=False)

    n_angles = sum(len(p["angles"]) for p in picks)
    _progress("done", scanned=total_scanned, angles=total_with_angles,
              picks=len(picks), graded=output["total_graded"])
    print(f"\nDone — scanned {total_scanned} messages")
    print(f"  {total_with_angles} messages with angles")
    print(f"  {len(picks)} picks extracted ({n_angles} angle entries)")
    print(f"  {output['total_graded']} graded (WIN/LOSS/PUSH)")
    print(f"  → {output_path}")


def main():
    p = argparse.ArgumentParser(description="Extract angle performance data")
    p.add_argument("--limit", type=int, default=None, help="Limit messages to scan")
    p.add_argument("--output", type=str, default=None, help="Output JSON path")
    asyncio.run(extract(limit=p.parse_args().limit, output_path=p.parse_args().output))


if __name__ == "__main__":
    main()
