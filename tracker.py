#!/usr/bin/env python3
"""
tracker.py — Grade sports picks from Telegram channel exports.

Usage:
  python tracker.py --backtest result_df.json
  python tracker.py --backtest result.json
"""

import asyncio
import hashlib
import json
import os
import re
import argparse

from datetime import date as _date, timedelta

import anthropic
import httpx
from dotenv import load_dotenv

from common import VERDICT_EMOJI
from scores import ESPN_LEAGUES, fetch_espn, scoreboard_text, odds_requests_used
from odds import fetch_odds_current, quota_used as odds_quota_used
from ai import (
    claude,
    claude_parse,
    claude_grade,
    build_context,
    CONTEXT_SKIP,
    CONTEXT_PENDING,
    CONTEXT_ESPN_ERROR,
    usage_cost,
    fmt_cost,
)

load_dotenv()
load_dotenv(".env.local", override=True)  # VPS-specific overrides (never synced)

# Cache of parsed-but-pending messages so we don't re-call Claude on every run
_PENDING_CACHE_PATH = os.path.join(os.path.dirname(__file__), "parse_cache.json")


def _norm_desc(d: str) -> str:
    """Normalise a pick description for duplicate comparison."""
    d = d.lower().strip()
    d = re.sub(r'\([+-]?\d+\)', '', d)               # strip parenthesized odds e.g. (-170)
    d = re.sub(r'(?<=\s)[+-]?\d{3,}(?=\s|$)', '', d) # strip bare American odds e.g. -170, +110
    d = re.sub(r'\d+(\.\d+)?u\b', '', d)              # strip units e.g. 1.5u
    return re.sub(r'\s+', ' ', d).strip()


def _pending_entry(capper: str, parsed: dict, leg_verdicts: dict, existing: dict, odds_by_pick: dict | None = None) -> dict:
    """Build a pending-cache entry, preserving linked_message_ids and odds from the existing entry."""
    entry = {
        "capper_name":        capper,
        "parsed":             parsed,
        "leg_verdicts":       leg_verdicts,
        "linked_message_ids": existing.get("linked_message_ids", []),
        # Preserve fetched odds — once set, never overwritten with None
        "odds_by_pick":       odds_by_pick if odds_by_pick is not None else existing.get("odds_by_pick", {}),
    }
    if existing.get("_unknown_notified"):
        entry["_unknown_notified"] = True
    return entry


def _find_duplicate_cache_key(
    pending_cache: dict,
    channel_id: int,
    capper_name: str,
    new_picks: list[dict],
    exclude_key: str | None = None,
) -> str | None:
    """Return the cache key of a pending entry that matches this capper+picks, else None."""
    norm_new = sorted(_norm_desc(p.get("description", "")) for p in new_picks)
    capper_lower = capper_name.lower()
    for key, entry in pending_cache.items():
        if key == exclude_key:
            continue
        if int(key.split(':')[0]) != channel_id:
            continue
        if entry.get("capper_name", "").lower() != capper_lower:
            continue
        existing_picks = entry.get("parsed", {}).get("picks", [])
        if not existing_picks:
            continue
        norm_existing = sorted(_norm_desc(p.get("description", "")) for p in existing_picks)
        if norm_existing == norm_new:
            return key
    return None


def _load_pending_cache() -> dict:
    try:
        with open(_PENDING_CACHE_PATH) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def _save_pending_cache(cache: dict) -> None:
    with open(_PENDING_CACHE_PATH, "w") as f:
        json.dump(cache, f)


# ─── Message parsing ──────────────────────────────────────────────────────────

def msg_plain_text(msg: dict) -> str:
    text = msg.get("text", "")
    if isinstance(text, list):
        parts = []
        for chunk in text:
            if isinstance(chunk, str):
                parts.append(chunk)
            elif isinstance(chunk, dict) and chunk.get("type") != "blockquote":
                parts.append(chunk.get("text", ""))
        return "".join(parts)
    return text


def extract_label(text: str) -> str | None:
    if "\u2705" in text:
        return "win"
    if "\u274c" in text:
        return "loss"
    return None


def strip_label(text: str) -> str:
    return re.sub(r"[\u2705\u274c]", "", text).strip()


def grade_matches_label(grade: str, label: str) -> bool:
    """Check if a graded verdict matches the expected label (win/loss)."""
    return (grade == "WIN" and label == "win") or (grade == "LOSS" and label == "loss")


# ─── Emoji insertion ─────────────────────────────────────────────────────────

_PICK_EMOJI = {k: v for k, v in VERDICT_EMOJI.items() if k in ("WIN", "LOSS", "PUSH")}


def _insert_emojis(text: str, verdicts: list[tuple]) -> str:
    """
    Insert verdict emoji(s) into the message text.

    Parlay messages: add a single overall verdict emoji on the "Parlay:" header
    line (or after the last leg if no header found).  Per-leg emojis are NOT
    inserted — the parlay is a single bet.

    Non-parlay messages: insert per-pick verdict emojis inline after each
    pick's line, matched by team/player name.

    Lines that can't be matched are left unchanged.
    Returns the modified text (or original if nothing could be matched).
    """
    lines = text.rstrip().split("\n")

    is_parlay = any(v[0].get("is_parlay_leg") for v in verdicts)

    if is_parlay:
        overall = _overall_verdict(verdicts)
        emoji = _PICK_EMOJI.get(overall)
        if not emoji:
            return text  # PENDING / UNKNOWN — nothing to insert yet

        # Prefer appending to the "Parlay:" header line
        for i, line in enumerate(lines):
            if "parlay" in line.lower() and not any(ch in line for ch in _PICK_EMOJI.values()):
                lines[i] = f"{line.rstrip()}{emoji}"
                return "\n".join(lines)

        # Fallback: find the last leg line and append there
        last_idx = -1
        for pick, _verdict, _calc, _sport, *_ in verdicts:
            if not pick.get("is_parlay_leg"):
                continue
            teams  = pick.get("teams") or []
            player = pick.get("player") or ""
            identifiers = [player] if player else teams
            search_terms: list[str] = []
            for t in identifiers:
                tl = t.lower().strip()
                if tl:
                    search_terms.append(tl)
                    search_terms.extend(w for w in tl.split() if len(w) > 3)
            for i, line in enumerate(lines):
                if any(term in line.lower() for term in search_terms):
                    last_idx = max(last_idx, i)

        if last_idx >= 0 and not any(ch in lines[last_idx] for ch in _PICK_EMOJI.values()):
            lines[last_idx] = f"{lines[last_idx].rstrip()}{emoji}"
        else:
            lines.append(emoji)
        return "\n".join(lines)

    # ── Non-parlay: per-pick emoji ────────────────────────────────────────────
    for pick, verdict, _calc, _sport, *_ in verdicts:
        emoji = _PICK_EMOJI.get(verdict)
        if not emoji:
            continue  # UNKNOWN / PENDING — leave line alone

        teams  = pick.get("teams") or []
        player = pick.get("player") or ""
        # For player props, search by player name only — team names appear as game
        # headers (e.g. "Pirates / Mets:") and would match the wrong line.
        # For team bets, search by team names.
        identifiers = [player] if player else teams
        search_terms: list[str] = []
        for t in identifiers:
            tl = t.lower().strip()
            if tl:
                search_terms.append(tl)
                search_terms.extend(w for w in tl.split() if len(w) > 3)

        for i, line in enumerate(lines):
            if any(ch in line for ch in _PICK_EMOJI.values()):
                continue  # already has an emoji — skip
            line_lower = line.lower()
            if any(term in line_lower for term in search_terms):
                lines[i] = f"{line.rstrip()}{emoji}"
                break  # one match per pick

    return "\n".join(lines)

_ODDS_TAG_RE = re.compile(r'\s*\[[+-]\d{3,4}[^\]]*\]')


def _fmt_odds_audit(pick: dict, sport: str, capper: str, result) -> str:
    fmt  = result.format() or "?"
    desc = pick.get("description", "")
    bk   = result.bookmaker or "?"
    lines = [
        f"📊 <b>odds</b>: {desc} → [{fmt}]",
        f"{result.match_type} · {bk}",
    ]
    if result.api_line is not None and result.pick_line is not None and result.api_line != result.pick_line:
        lines.append(f"api_line: {result.api_line} | pick_line: {result.pick_line}")
    lines.append(f"{sport} · {capper}")
    return "\n".join(lines)


def _insert_odds(text: str, picks: list[dict], odds_by_pick: dict) -> str:
    """
    Insert odds tags directly after each pick line, e.g. 'Duke -4.5 (-153)'.

    For parlays: inserts combined parlay price on the header line (the line
    containing "parlay" that isn't a leg bullet). Individual leg prices are
    not shown — only the combined payout odds.
    Idempotent — skips lines that already carry an odds tag.
    Uses same search-term logic as _insert_emojis.
    """
    if any(p.get("is_parlay_leg") for p in picks):
        _leg_odds = [odds_by_pick.get(str(i), {}).get("odds") for i in range(len(picks))]
        _valid = [o for o in _leg_odds if o is not None]
        if len(_valid) != len(_leg_odds):
            return text  # partial odds — don't show misleading combined price
        _dec = 1.0
        for _o in _valid:
            _dec *= (_o / 100 + 1) if _o > 0 else (100 / abs(_o) + 1)
        _comb = round((_dec - 1) * 100) if _dec >= 2.0 else round(-100 / (_dec - 1))
        combined_tag = f" [{'+' if _comb > 0 else ''}{_comb}]"
        lines = text.rstrip().split("\n")
        for j, line in enumerate(lines):
            ll = line.lower().lstrip()
            if ll.startswith("•") or ll.startswith("-"):
                continue  # skip leg bullet lines
            if "parlay" not in ll:
                continue
            if _ODDS_TAG_RE.search(line):
                return text  # already tagged — idempotent
            lines[j] = f"{line.rstrip()}{combined_tag}"
            return "\n".join(lines)
        return text

    lines = text.rstrip().split("\n")

    def _fmt(v: int) -> str:
        return f"+{v}" if v > 0 else str(v)

    for idx, pick in enumerate(picks):
        odds_val = odds_by_pick.get(str(idx), {}).get("odds")
        if odds_val is None:
            continue
        match_type  = odds_by_pick.get(str(idx), {}).get("match_type", "")
        pregame_val = odds_by_pick.get(str(idx), {}).get("pregame_odds")
        if match_type.startswith("live_"):
            if pregame_val is not None:
                odds_tag = f" [{_fmt(odds_val)} live · {_fmt(pregame_val)} pre]"
            else:
                odds_tag = f" [{_fmt(odds_val)} live]"
        elif match_type.startswith("pregame_"):
            odds_tag = f" [{_fmt(odds_val)} pre]"
        else:
            odds_tag = f" [{_fmt(odds_val)}]"

        teams  = pick.get("teams") or []
        player = pick.get("player") or ""
        identifiers = [player] if player else teams
        search_terms: list[str] = []
        for t in identifiers:
            tl = t.lower().strip()
            if tl:
                search_terms.append(tl)
                search_terms.extend(w for w in tl.split() if len(w) > 3)

        # Try description first: more specific than team/player fragments and avoids
        # false matches on game-info header lines (e.g. "Defenders @ Aviators / 8:00 PM").
        # Normalise "moneyline" → "ml" so AI-expanded descriptions match message abbreviations.
        desc = (pick.get("description") or "").lower().strip().replace("moneyline", "ml")
        desc_matched = False
        if desc:
            for j, line in enumerate(lines):
                if desc in line.lower():
                    if not _ODDS_TAG_RE.search(line):
                        lines[j] = f"{line.rstrip()}{odds_tag}"
                    desc_matched = True
                    break

        if not desc_matched:
            for j, line in enumerate(lines):
                if " @ " in line:  # skip game-info header (e.g. "Team A @ Team B / 8:00 PM")
                    continue
                if any(term in line.lower() for term in search_terms):
                    if _ODDS_TAG_RE.search(line):
                        break  # already tagged — idempotent
                    lines[j] = f"{line.rstrip()}{odds_tag}"
                    desc_matched = True
                    break

        # Third fallback: strip team/player names from desc and search for the remainder.
        # Catches abbreviations like "Dbacks ML (2 units)" when AI parsed "Arizona Diamondbacks ML".
        if not desc_matched and desc:
            team_words = set()
            for t in identifiers:
                for w in t.lower().split():
                    if len(w) > 3:
                        team_words.add(w)
            desc_stripped = desc
            for w in team_words:
                desc_stripped = desc_stripped.replace(w, "")
            desc_stripped = " ".join(desc_stripped.split())  # collapse whitespace
            if len(desc_stripped) >= 4:
                for j, line in enumerate(lines):
                    if " @ " in line:
                        continue
                    if desc_stripped in line.lower():
                        if not _ODDS_TAG_RE.search(line):
                            lines[j] = f"{line.rstrip()}{odds_tag}"
                        break

    return "\n".join(lines)


def _overall_verdict(verdicts: list[tuple]) -> str:
    """
    Collapse per-pick verdicts into a single message verdict.

    Parlay legs: ALL must WIN → WIN; any LOSS → LOSS; any UNKNOWN → UNKNOWN.
    Non-parlay:  all must agree (all WIN or all LOSS); mixed or any UNKNOWN → UNKNOWN.
    """
    if not verdicts:
        return "UNKNOWN"
    all_v = [v[1] for v in verdicts]
    is_parlay = any(v[0].get("is_parlay_leg") for v in verdicts)
    if is_parlay:
        if "PENDING" in all_v:
            return "PENDING"
        if "UNKNOWN" in all_v:
            return "UNKNOWN"
        if "LOSS" in all_v:
            return "LOSS"
        if all(v == "WIN" for v in all_v):
            return "WIN"
        if "PUSH" in all_v:
            return "PUSH"
        return "UNKNOWN"
    else:
        unique = set(all_v) - {"PUSH"}
        if "PENDING" in unique:
            return "PENDING"
        if "UNKNOWN" in unique or len(unique) > 1:
            return "UNKNOWN"
        return unique.pop() if unique else "PUSH"


async def _bot_edit_message(
    bot_token: str,
    channel_id: int,
    message_id: int,
    new_text: str,
    has_media: bool,
) -> bool:
    """Edit a message via Bot API. Returns True on success."""
    method = "editMessageCaption" if has_media else "editMessageText"
    field  = "caption"            if has_media else "text"
    try:
        async with httpx.AsyncClient(timeout=10) as http:
            r = await http.post(
                f"https://api.telegram.org/bot{bot_token}/{method}",
                json={"chat_id": channel_id, "message_id": message_id,
                      field: new_text, "parse_mode": "HTML"},
            )
            if not r.is_success:
                print(f"    [bot edit error] {r.status_code}: {r.text[:120]}")
                return False
            return True
    except Exception as exc:
        print(f"    [bot edit error] {exc}")
        return False


# ─── Backtest ─────────────────────────────────────────────────────────────────

def _skip_reason(r: dict) -> str:
    if r.get("is_parlay_leg") and r["sport"] not in ESPN_LEAGUES and r["sport"] != "Tennis":
        return f"parlay (no data: {r['sport']})"
    if r["sport"] not in ESPN_LEAGUES and r["sport"] != "Tennis":
        return f"no data ({r['sport']})"
    if r["bet_type"] == "prop":
        return "prop"
    return "unknown"


def _write_detail_file(path: str, source: str, results: list, graded: list,
                       correct_list: list, skipped_list: list, wrong_list: list,
                       cost: float) -> None:
    sep = "=" * 80
    thin = "-" * 80

    with open(path, "w", encoding="utf-8") as f:
        def w(line: str = "") -> None:
            f.write(line + "\n")

        # Header
        w(sep)
        w(f"BACKTEST DETAIL REPORT")
        w(f"Source : {source}")
        w(f"Total  : {len(results)}  |  Graded: {len(graded)}  |  Skipped: {len(skipped_list)}")
        if graded:
            pct = round(100 * len(correct_list) / len(graded))
            w(f"Accuracy: {len(correct_list)}/{len(graded)} ({pct}%)")
        w(f"[Claude total] {fmt_cost(cost)}")
        w(sep)

        for r in results:
            mark = "OK" if r["correct"] else ("--" if r["skipped"] else "XX")
            w()
            w(sep)
            w(f"[{mark}] MSG {r['msg_id']}  |  {r['date']}  |  {r['sport']}  |  label={r['label'].upper()}  grade={r['grade']}")
            w(sep)

            # Raw message text
            w("RAW TEXT:")
            for line in r["raw_text"].splitlines():
                w(f"  {line}")
            w()

            # Parsed pick fields
            w("PARSED PICK:")
            p = r["parsed"]
            w(f"  description : {p.get('description', '')}")
            w(f"  bet_type    : {p.get('bet_type', '')}")
            w(f"  period      : {p.get('period', 'game')}")
            w(f"  teams       : {p.get('teams', [])}")
            w(f"  player      : {p.get('player', '')}")
            w(f"  prop_stat   : {p.get('prop_stat', '')}")
            w(f"  line        : {p.get('line', '')}")
            w(f"  direction   : {p.get('direction', '')}")
            w()

            # Context passed to grader
            w("CONTEXT (sent to grader):")
            ctx = r["context"]
            if ctx == CONTEXT_SKIP:
                w("  [skipped — no grader call]")
            else:
                for line in ctx.splitlines():
                    w(f"  {line}")
            w()

            # Grader output
            w(f"GRADE: {r['grade']}")
            if r["calc"]:
                w(f"CALC : {r['calc']}")
            if r["skipped"]:
                w(f"SKIP REASON: {_skip_reason(r)}")
            w(thin)

        # Incorrect summary
        w()
        w(sep)
        w("INCORRECT GRADES:")
        w(sep)
        if wrong_list:
            for r in wrong_list:
                w(f"  msg {r['msg_id']:3d}  {r['date']}  {r['sport']:6s}  got={r['grade']}  expected={r['label'].upper()}")
                w(f"    pick   : {r['pick']}")
                w(f"    calc   : {r['calc']}")
                w()
        else:
            w("  (none)")


async def run_backtest(filepath: str) -> None:
    with open(filepath, encoding="utf-8") as f:
        data = json.load(f)

    channel_name = data.get("name", filepath)
    messages = [m for m in data["messages"] if m.get("type") == "message"]

    print(f"\nBacktest: {channel_name}  ({len(messages)} messages)")
    print("=" * 72)

    results = []
    scoreboard_cache: dict[tuple[str, str], dict | None] = {}
    summary_cache: dict[tuple[str, str], dict | None] = {}

    for msg in messages:
        plain = msg_plain_text(msg)
        if not plain.strip():
            continue

        label = extract_label(plain)
        if label is None:
            continue

        clean = strip_label(plain)
        date = msg["date"][:10]

        parsed = await claude_parse(clean, date)
        if not parsed:
            print(f"  [parse fail] msg {msg['id']}")
            continue

        sport = parsed.get("sport", "Other")
        picks = parsed.get("picks", [])
        if not picks:
            continue

        # Fetch scoreboard once per (sport, date)
        sb_key = (sport, date)
        if sb_key not in scoreboard_cache:
            scoreboard_cache[sb_key] = await fetch_espn(sport, date)
        scoreboard = scoreboard_cache[sb_key]

        for pick in picks:
            pick_desc = pick.get("description", clean[:80])
            bet_type = pick.get("bet_type", "")
            period = pick.get("period", "game")
            is_parlay_leg = pick.get("is_parlay_leg", False)
            # Per-pick sport override (used for cross-sport parlays)
            pick_sport = pick.get("sport") or sport

            # Fetch scoreboard for per-pick sport if different from message sport
            if pick_sport != sport:
                pick_sb_key = (pick_sport, date)
                if pick_sb_key not in scoreboard_cache:
                    scoreboard_cache[pick_sb_key] = await fetch_espn(pick_sport, date)
                pick_scoreboard = scoreboard_cache[pick_sb_key]
            else:
                pick_scoreboard = scoreboard

            context, _game_date = await build_context(pick_sport, date, pick, pick_scoreboard, summary_cache)

            if context in (CONTEXT_SKIP, CONTEXT_ESPN_ERROR):
                grade, calc = "UNKNOWN", ""
            elif context == CONTEXT_PENDING:
                grade, calc = "PENDING", ""
            else:
                grade, calc = await claude_grade(pick_desc, date, context, bet_type)

            correct = grade_matches_label(grade, label)
            skipped = grade in ("PUSH", "UNKNOWN")
            mark = "OK" if correct else ("--" if skipped else "XX")

            print(
                f"  {mark}  msg {msg['id']:3d}  {date}  {pick_sport:6s}  "
                f"{grade:7s}  label={label.upper():<4s}  {pick_desc[:48]}"
            )
            results.append({
                "msg_id": msg["id"],
                "date": date,
                "sport": pick_sport,
                "label": label,
                "grade": grade,
                "calc": calc,
                "correct": correct,
                "skipped": skipped,
                "pick": pick_desc,
                "bet_type": bet_type,
                "period": period,
                "is_parlay_leg": is_parlay_leg,
                "parsed": pick,
                "context": context,
                "raw_text": msg_plain_text(msg),
            })

    # ── Summary ──
    graded = [r for r in results if not r["skipped"]]
    correct_list = [r for r in graded if r["correct"]]
    skipped_list = [r for r in results if r["skipped"]]
    wrong_list = [r for r in graded if not r["correct"]]

    print(f"\n{'=' * 72}")
    total = len(results)
    if graded:
        pct = round(100 * len(correct_list) / len(graded))
        print(f"Accuracy : {len(correct_list)}/{len(graded)} ({pct}%)  |  skipped: {len(skipped_list)}/{total}")
    else:
        print(f"Accuracy : N/A  |  skipped: {len(skipped_list)}/{total}")

    cost = usage_cost()
    print(f"[Claude total] {fmt_cost(cost)}")

    if wrong_list:
        print("\nIncorrect grades:")
        for r in wrong_list:
            print(f"  msg {r['msg_id']:3d}  {r['date']}  {r['sport']:6s}  got={r['grade']}  expected={r['label'].upper()}")
            print(f"         {r['pick'][:65]}")

    # Skip breakdown
    if skipped_list:
        skip_by_reason: dict[str, int] = {}
        for r in skipped_list:
            reason = _skip_reason(r)
            skip_by_reason[reason] = skip_by_reason.get(reason, 0) + 1

        print("\nSkipped breakdown:")
        for reason, count in sorted(skip_by_reason.items(), key=lambda x: -x[1]):
            print(f"  {reason}: {count}")

    # ── Write detail file ──
    data_dir = os.path.join(os.path.dirname(__file__), "data")
    os.makedirs(data_dir, exist_ok=True)
    base = os.path.splitext(os.path.basename(filepath))[0]
    out_path = os.path.join(data_dir, f"backtest_{base}.txt")
    _write_detail_file(out_path, filepath, results, graded, correct_list, skipped_list, wrong_list, cost)
    print(f"\nDetail file: {out_path}")
    from audit import log_api_costs
    log_api_costs("backtest", cost, odds_requests_used())


async def grade_one(text: str, date: str) -> None:
    """Parse and grade a single pick message, printing full detail."""
    import sys
    sys.stdout.reconfigure(encoding="utf-8", line_buffering=True)

    label = extract_label(text)
    clean = strip_label(text)

    parsed = await claude_parse(clean, date)
    if not parsed:
        print("[parse fail]")
        return

    sport = parsed.get("sport", "Other")
    picks = parsed.get("picks", [])
    print(f"Sport: {sport}  |  {len(picks)} pick(s)")
    print()

    scoreboard_cache: dict = {}
    summary_cache: dict = {}

    sb_key = (sport, date)
    scoreboard_cache[sb_key] = await fetch_espn(sport, date)

    for i, pick in enumerate(picks, 1):
        pick_sport = pick.get("sport") or sport
        if pick_sport != sport:
            ps_key = (pick_sport, date)
            if ps_key not in scoreboard_cache:
                scoreboard_cache[ps_key] = await fetch_espn(pick_sport, date)
            scoreboard = scoreboard_cache[ps_key]
        else:
            scoreboard = scoreboard_cache[sb_key]

        pick_desc = pick.get("description", clean[:80])
        print(f"Pick {i}: {pick_desc}")
        print(f"  sport={pick_sport}  bet_type={pick.get('bet_type')}  period={pick.get('period','game')}"
              f"  teams={pick.get('teams')}  player={pick.get('player')}"
              f"  line={pick.get('line')}  dir={pick.get('direction')}"
              f"  parlay_leg={pick.get('is_parlay_leg', False)}")

        context, _game_date = await build_context(pick_sport, date, pick, scoreboard, summary_cache)
        print()
        print("  CONTEXT:")
        if context == CONTEXT_SKIP:
            print("    [skipped]")
        else:
            for ln in context.splitlines():
                print(f"    {ln}")
        print()

        if context != CONTEXT_SKIP:
            grade, calc = await claude_grade(pick_desc, date, context, pick.get("bet_type", ""))
            print(f"  GRADE : {grade}")
            print(f"  CALC  : {calc}")
        else:
            grade = "UNKNOWN"
            print(f"  GRADE : UNKNOWN (skipped)")

        if label:
            correct = grade_matches_label(grade, label)
            print(f"  LABEL : {label.upper()}  →  {'OK' if correct else ('--' if grade in ('PUSH','UNKNOWN') else 'XX')}")
        print()

    print()
    print(f"[Claude total] {fmt_cost(usage_cost())}  |  [Odds API] {odds_requests_used()} requests used")
    from audit import log_api_costs
    log_api_costs("debug", usage_cost(), odds_requests_used())


# ─── Live mode ────────────────────────────────────────────────────────────────

# Column widths for tabular pick output
_ID_W    = 5   # message ID
_CAP_W   = 15  # capper name
_DESC_W  = 28  # pick description
_ODDS_W  = 7   # odds e.g. [-115]
_TBL_W   = _ID_W + 1 + _CAP_W + 2 + _DESC_W + 1 + _ODDS_W + 1 + 4  # total table width

_TAG_ICON = {"WAIT": "⏳", "EDIT": "✏", "DRY ": "🧪", "SKIP": "⚠", "ESPN": "📡"}


def _trunc(s: str, w: int) -> str:
    """Truncate string to width w, appending … if trimmed."""
    return s if len(s) <= w else s[:w - 1] + "…"

async def run_live(dry_run: bool = False, days: int = 7, channel: int | None = None) -> None:
    import datetime as dt
    from telethon import TelegramClient
    from telethon.sessions import StringSession
    from audit import AuditLog

    api_id    = int(os.getenv("TELEGRAM_API_ID", "0"))
    api_hash  = os.getenv("TELEGRAM_API_HASH", "")
    session   = os.getenv("TELEGRAM_SESSION", "")
    bot_token = os.getenv("BOT_TOKEN", "")

    channels_raw = os.getenv("GRADE_CHANNELS", "[]")
    try:
        channel_ids = json.loads(channels_raw)
    except json.JSONDecodeError:
        print("ERROR: GRADE_CHANNELS must be a JSON array, e.g. [-1001234567890]")
        return
    if not channel_ids:
        print("ERROR: GRADE_CHANNELS not set in .env")
        return
    if channel is not None:
        channel_ids = [channel]

    # Build broadcast results map from MAPPINGS_CONFIG: graded dest_channel → broadcast_results_channel.
    # In dry-run, route to test_broadcast_results_channel so results can be previewed safely.
    broadcast_results_map: dict[int, int] = {}
    for m in json.loads(os.getenv("MAPPINGS_CONFIG", "[]")):
        dest = m.get("dest_channel")
        bc_key = "test_broadcast_results_channel" if dry_run else "broadcast_results_channel"
        bc = m.get(bc_key)
        if dest and bc:
            broadcast_results_map[dest] = bc

    audit         = AuditLog(broadcast_results_mappings=broadcast_results_map)
    pending_cache = _load_pending_cache()
    cutoff = dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=days)
    mode   = "DRY RUN" if dry_run else "LIVE"

    async with TelegramClient(StringSession(session), api_id, api_hash) as client:
        # Resolve channel names once up front
        channel_names: dict[int, str] = {}
        for cid in channel_ids:
            try:
                entity = await client.get_entity(cid)
                channel_names[cid] = getattr(entity, "title", str(cid))
            except Exception:
                channel_names[cid] = str(cid)

        channels_str = ', '.join(channel_names[c] for c in channel_ids)
        print(f"{mode} | {days}d | {channels_str}")
        print("=" * 40)

        edited = pending = failed = errors = 0
        odds_found = odds_total = 0

        for channel_id in channel_ids:
            ch_name = channel_names[channel_id]
            _hdr = f"{ch_name}  ({channel_id}):"
            print(f"\n{_hdr:^{_TBL_W}}")
            print(f"{'ID':<{_ID_W}} {'Capper':<{_CAP_W}}  {'Pick':<{_DESC_W}} {'Odds':<{_ODDS_W}} Date")
            print(f"{'─'*_ID_W} {'─'*_CAP_W}  {'─'*_DESC_W} {'─'*_ODDS_W} ────")
            scoreboard_cache: dict = {}
            summary_cache:   dict = {}

            visited_keys: set[str] = set()

            async def _iter_with_catchup():
                async for m in client.iter_messages(channel_id):
                    mdate = m.date
                    if mdate.tzinfo is None:
                        mdate = mdate.replace(tzinfo=dt.timezone.utc)
                    if mdate < cutoff:
                        break       # regular scan done — break so stale catchup runs
                    yield m
                # After regular scan: yield any pending messages that weren't reached
                stale_ids = [
                    int(k.split(':')[1]) for k in pending_cache
                    if k.startswith(f"{channel_id}:")
                    and k not in visited_keys
                    and isinstance(pending_cache.get(k), dict)
                    and "parsed" in pending_cache.get(k, {})
                ]
                if stale_ids:
                    fetched = await client.get_messages(channel_id, ids=stale_ids)
                    for m in (fetched if isinstance(fetched, list) else [fetched]):
                        if m:
                            yield m

            async for msg in _iter_with_catchup():
                msg_date = msg.date
                if msg_date.tzinfo is None:
                    msg_date = msg_date.replace(tzinfo=dt.timezone.utc)

                text = msg.text or ""
                date_str = msg_date.strftime("%Y-%m-%d")

                cache_key = f"{channel_id}:{msg.id}"
                visited_keys.add(cache_key)

                if not text.strip():
                    continue
                # Skip already graded (check plain text) — but allow re-entry for
                # partially-graded messages that still have a pending-cache entry.
                if any(ch in text for ch in ("\u2705", "\u274c", "\u21a9\ufe0f")):
                    if cache_key not in pending_cache:
                        continue

                capper  = next((l.strip() for l in text.splitlines() if l.strip()), "")
                snippet = " ".join(text.split())[:80]
                cached = pending_cache.get(cache_key)
                # {"_dupe": True} is stored when this message was identified as a duplicate
                # so we can skip claude_parse on subsequent runs without re-paying.
                if isinstance(cached, dict) and cached.get("_dupe"):
                    continue  # primary row carries the +N dup annotation
                # {"_failed": True} is stored after the first audit notification so we
                # don't spam the audit channel. We only re-try parsing if the message
                # text has changed (i.e. the capper edited it) — otherwise skip Claude.
                already_notified = isinstance(cached, dict) and cached.get("_failed")
                if already_notified:
                    _cur_hash = hashlib.md5(text.encode()).hexdigest()
                    if cached.get("text_hash") == _cur_hash:
                        # Text unchanged — skip Claude, just show the warning
                        failed += 1
                        print(f"\n{msg.id:<{_ID_W}} {_trunc(capper, _CAP_W):<{_CAP_W}}  {'no picks':<{_DESC_W}} {'':<{_ODDS_W}} {int(date_str[5:7])}/{int(date_str[8:10])} ⚠")
                        continue
                    else:
                        # Message was edited — retry fresh (re-fire audit notification too)
                        already_notified = False
                # Support both old format (bare parsed dict) and new format (with leg_verdicts)
                if cached and not already_notified:
                    if isinstance(cached, dict) and "parsed" in cached:  # new format
                        cached_parse = cached["parsed"]
                        cached_leg_verdicts = cached.get("leg_verdicts", {})
                    elif isinstance(cached, dict) and "picks" in cached:  # old format
                        cached_parse = cached
                        cached_leg_verdicts = {}
                    else:
                        cached_parse = None
                        cached_leg_verdicts = {}
                else:
                    cached_parse = None
                    cached_leg_verdicts = {}
                # Leg indices that were already broadcast in a previous partial edit
                already_broadcast_indices = {
                    int(k) for k, v in cached_leg_verdicts.items()
                    if isinstance(v, dict) and v.get("broadcasted")
                }
                parsed = cached_parse or await claude_parse(text, date_str)
                if not parsed:
                    failed += 1
                    print(f"\n{msg.id:<{_ID_W}} {_trunc(capper, _CAP_W):<{_CAP_W}}  {'parse failed':<{_DESC_W}} {'':<{_ODDS_W}} {int(date_str[5:7])}/{int(date_str[8:10])} ⚠")
                    if not already_notified:
                        await audit.record(
                            channel_id=channel_id, message_id=msg.id, date=date_str,
                            sport="Other", pick_desc=snippet, bet_type="",
                            verdict="UNKNOWN", calc="parse failed",
                            prev_caption=text, dry_run=dry_run, channel_name=ch_name, capper_name=capper,
                        )
                        pending_cache[cache_key] = {"_failed": True, "text_hash": hashlib.md5(text.encode()).hexdigest()}
                    continue
                sport = parsed.get("sport", "Other")
                picks = parsed.get("picks", [])
                if not picks:
                    failed += 1
                    print(f"\n{msg.id:<{_ID_W}} {_trunc(capper, _CAP_W):<{_CAP_W}}  {'no picks':<{_DESC_W}} {'':<{_ODDS_W}} {int(date_str[5:7])}/{int(date_str[8:10])} ⚠")
                    if not already_notified:
                        await audit.record(
                            channel_id=channel_id, message_id=msg.id, date=date_str,
                            sport=sport, pick_desc=snippet, bet_type="",
                            verdict="UNKNOWN", calc="no picks extracted",
                            prev_caption=text, dry_run=dry_run, channel_name=ch_name, capper_name=capper,
                        )
                        pending_cache[cache_key] = {"_failed": True, "text_hash": hashlib.md5(text.encode()).hexdigest()}
                    continue

                dup_key = _find_duplicate_cache_key(
                    pending_cache, channel_id, capper, picks, exclude_key=cache_key
                )
                if dup_key:
                    dup_id = int(dup_key.split(':')[1])
                    linked = pending_cache[dup_key].setdefault("linked_message_ids", [])
                    if msg.id not in linked:
                        linked.append(msg.id)
                    # Cache the dupe marker so we skip claude_parse on future runs
                    pending_cache[cache_key] = {"_dupe": True, "primary_id": dup_id}
                    continue  # primary row carries the +N dup annotation

                # ── Fetch odds at first encounter ─────────────────────────────
                # Only fetch once — if odds_by_pick is already in the cache, reuse it.
                cached_entry = pending_cache.get(cache_key) if isinstance(pending_cache.get(cache_key), dict) else {}
                odds_by_pick: dict = cached_entry.get("odds_by_pick", {})
                odds_were_empty = not odds_by_pick
                if not odds_by_pick:
                    for i, pick in enumerate(picks):
                        pick_sport = pick.get("sport") or sport
                        result = await fetch_odds_current(pick_sport, pick)
                        display_odds, warn = result.validate_for_display()
                        pick_desc = pick.get("description", "")
                        if display_odds is None:
                            # Any failure to show odds → one audit warning per pick, never repeated
                            reason = warn or result.match_type
                            print(f"  [odds] miss({result.match_type}) {pick_desc[:60]}")
                            await audit.warn(f"⚠️ <b>odds miss</b>: {reason}\n{pick_desc} · {pick_sport} · {capper}")
                        elif warn:
                            # Odds found but soft sanity flag — log + one audit warning
                            print(f"  [odds] sanity: {warn}")
                            await audit.warn(f"⚠️ <b>odds sanity</b>: {warn}\n" + _fmt_odds_audit(pick, pick_sport, capper, result))
                        else:
                            if result.match_type.startswith("live_"):
                                prefix = "🟢 "
                            elif result.match_type.startswith("pregame_"):
                                prefix = "📅 "
                            else:
                                prefix = ""
                            await audit.warn(prefix + _fmt_odds_audit(pick, pick_sport, capper, result))
                        odds_by_pick[str(i)] = {
                            "odds":               display_odds,
                            "bookmaker":          result.bookmaker,
                            "match_type":         result.match_type,
                            "pregame_odds":       result.pregame_odds,
                            "pregame_bookmaker":  result.pregame_bookmaker,
                            "pregame_match_type": result.pregame_match_type,
                        }
                        odds_total += 1
                        if display_odds is not None:
                            odds_found += 1

                # Edit odds into the message so they appear while PENDING.
                # Idempotent — _insert_odds won't re-add if tag already present.
                if not dry_run and any(v.get("odds") is not None for v in odds_by_pick.values()):
                    from telethon.extensions import html as tl_html
                    _ht = tl_html.unparse(text, msg.entities or [])
                    _ht = _ht.replace("<spoiler>", "<tg-spoiler>").replace("</spoiler>", "</tg-spoiler>")
                    _odds_text = _insert_odds(_ht, picks, odds_by_pick)
                    if _odds_text != _ht:
                        await _bot_edit_message(bot_token, channel_id, msg.id, _odds_text, msg.media is not None)
                        await asyncio.sleep(0.5)
                        for linked_id in cached_entry.get("linked_message_ids", []):
                            await _bot_edit_message(bot_token, channel_id, linked_id, _odds_text, msg.media is not None)
                            await asyncio.sleep(0.5)

                sb_key = (sport, date_str)
                if sb_key not in scoreboard_cache:
                    scoreboard_cache[sb_key] = await fetch_espn(sport, date_str)

                verdicts = []
                has_espn_error = False
                for i, pick in enumerate(picks):
                    cached_leg = cached_leg_verdicts.get(str(i))
                    if cached_leg and cached_leg.get("verdict") in ("WIN", "LOSS", "PUSH"):
                        # Resolved leg — use cached verdict, skip ESPN + Claude calls
                        verdict   = cached_leg["verdict"]
                        calc      = cached_leg["calc"]
                        pick_sport = cached_leg.get("sport", pick.get("sport") or sport)
                        game_date  = cached_leg.get("game_date", date_str)
                    else:
                        pick_sport = pick.get("sport") or sport
                        ps_key = (pick_sport, date_str)
                        if ps_key not in scoreboard_cache:
                            scoreboard_cache[ps_key] = await fetch_espn(pick_sport, date_str)
                        context, game_date = await build_context(
                            pick_sport, date_str, pick,
                            scoreboard_cache[ps_key], summary_cache,
                        )
                        if context in (CONTEXT_ESPN_ERROR, CONTEXT_PENDING):
                            if context == CONTEXT_ESPN_ERROR:
                                has_espn_error = True
                            verdict, calc = "PENDING", ""
                        elif context == CONTEXT_SKIP:
                            verdict, calc = "UNKNOWN", ""
                        else:
                            verdict, calc = await claude_grade(
                                pick.get("description", text[:80]), date_str, context,
                                pick.get("bet_type", ""),
                            )
                    verdicts.append((pick, verdict, calc, pick_sport, game_date))

                # Build edited text — odds then emoji inserted inline after each pick's line
                # Convert to HTML to preserve original formatting entities
                from telethon.extensions import html as tl_html
                import html as _html
                html_text = tl_html.unparse(text, msg.entities or [])
                # Escape any HTML special chars that Telethon may have left as plain text
                # (unparse already handles this, but sanitise spoiler tag for Bot API)
                html_text = html_text.replace("<spoiler>", "<tg-spoiler>").replace("</spoiler>", "</tg-spoiler>")
                html_text = _insert_odds(html_text, picks, odds_by_pick)
                new_text = _insert_emojis(html_text, verdicts)
                graded = [v for v in verdicts if v[1] in _PICK_EMOJI]
                # Picks resolved this run that haven't been broadcast yet (keep index for odds lookup)
                newly_resolved_indexed = [
                    (j, v) for j, v in enumerate(verdicts)
                    if v[1] in _PICK_EMOJI and j not in already_broadcast_indices
                ]
                newly_resolved = [v for _, v in newly_resolved_indexed]
                overall = _overall_verdict(verdicts)
                is_parlay = any(v[0].get("is_parlay_leg") for v in verdicts)
                # For parlays, don't edit until ALL legs are resolved — a LOSS
                # resolves the parlay immediately, but PENDING means we must wait.
                parlay_pending = is_parlay and overall == "PENDING"

                # Print all picks with their individual verdicts
                has_pending = any(v[1] == "PENDING" for v in verdicts)
                if not newly_resolved or parlay_pending:
                    if has_espn_error and (has_pending or parlay_pending):
                        tag = "ESPN"
                    elif has_pending or parlay_pending:
                        tag = "WAIT"
                    else:
                        tag = "SKIP"
                else:
                    tag = "DRY " if dry_run else "EDIT"
                dupe_ids = pending_cache.get(cache_key, {}).get("linked_message_ids", [])
                dupe_note = f" {'🔁' if len(dupe_ids) == 1 else str(len(dupe_ids)) + '🔁'}" if dupe_ids else ""
                # Compute combined parlay odds for log display
                parlay_combined_str = ""
                if is_parlay:
                    _leg_odds = [odds_by_pick.get(str(i), {}).get("odds") for i in range(len(verdicts))]
                    _valid = [o for o in _leg_odds if o is not None]
                    if _valid and len(_valid) == len(_leg_odds):
                        _dec = 1.0
                        for _o in _valid:
                            _dec *= (_o / 100 + 1) if _o > 0 else (100 / abs(_o) + 1)
                        _comb = round((_dec - 1) * 100) if _dec >= 2.0 else round(-100 / (_dec - 1))
                        parlay_combined_str = f"[{'+' if _comb > 0 else ''}{_comb}]"
                first_active = True
                for i, (pick, verdict, calc, ps, gd, *_) in enumerate(verdicts):
                    if i in already_broadcast_indices:
                        continue          # already done — don't reprint every cycle
                    pick_odds = odds_by_pick.get(str(i), {}).get("odds")
                    odds_col  = f"[{'+' if pick_odds > 0 else ''}{pick_odds}]" if pick_odds is not None else ""
                    desc     = _trunc(pick.get("description", ""), _DESC_W)
                    emoji    = VERDICT_EMOJI.get(verdict, "")
                    d        = _date.fromisoformat(gd) if gd else _date.fromisoformat(date_str)
                    gd_short = f"{d.month}/{d.day}"
                    id_col   = str(msg.id) if first_active else ""
                    cap_col  = _trunc(capper, _CAP_W) if first_active else ""
                    prefix   = "\n" if first_active else ""
                    suffix   = dupe_note if first_active else ""
                    first_active = False
                    print(f"{prefix}{id_col:<{_ID_W}} {cap_col:<{_CAP_W}}  {desc:<{_DESC_W}} {odds_col:<{_ODDS_W}} {gd_short} {emoji}{suffix}")
                    if calc:
                        print(f"{'':>{_ID_W}} {'':>{_CAP_W}}  {calc[:_DESC_W + 8]}")
                if parlay_combined_str:
                    print(f"{'':>{_ID_W}} {'':>{_CAP_W}}  {'→ parlay':<{_DESC_W}} {parlay_combined_str}")

                # Cache the parse result and any resolved leg verdicts to avoid re-calling
                # Claude on subsequent runs for legs that are already graded.
                if not graded or parlay_pending:
                    new_leg_verdicts = dict(cached_leg_verdicts)  # preserve previously cached
                    for j, (lpick, lverdict, lcalc, lps, lgd, *_) in enumerate(verdicts):
                        if lverdict in ("WIN", "LOSS", "PUSH"):
                            new_leg_verdicts[str(j)] = {
                                "verdict": lverdict, "calc": lcalc,
                                "sport": lps, "game_date": lgd or date_str,
                            }
                    pending_cache[cache_key] = _pending_entry(capper, parsed, new_leg_verdicts, pending_cache.get(cache_key, {}), odds_by_pick)

                # Nothing new to grade this run — log and skip
                if not newly_resolved or parlay_pending:
                    if overall == "PENDING" or parlay_pending:
                        pending += 1
                    else:
                        failed += 1
                    all_descs = "\n".join(
                        f"{v[1]}: {v[0].get('description', '')}|{v[3]}|{v[4]}|{v[2]}" for v in verdicts
                    )
                    first_pick, _, first_calc, first_sport, first_game_date = verdicts[0]
                    first_odds = odds_by_pick.get("0", {})
                    already_unknown_notified = cached_entry.get("_unknown_notified")
                    if not has_espn_error and not (overall == "UNKNOWN" and already_unknown_notified):
                        await audit.record(
                            channel_id=channel_id, message_id=msg.id, date=date_str,
                            sport=first_sport,
                            pick_desc=all_descs,
                            bet_type=first_pick.get("bet_type", ""),
                            verdict=overall, calc=first_calc,
                            prev_caption=text, dry_run=dry_run, channel_name=ch_name, capper_name=capper,
                            odds=first_odds.get("odds"), odds_bookmaker=first_odds.get("bookmaker"),
                            odds_match_type=first_odds.get("match_type"),
                        )
                        if overall == "UNKNOWN":
                            pending_cache[cache_key] = {**cached_entry, "_unknown_notified": True}
                            _save_pending_cache(pending_cache)
                    continue

                first_pick, _, first_calc, first_sport, first_game_date = verdicts[0]

                if not dry_run:
                    ok = await _bot_edit_message(
                        bot_token, channel_id, msg.id, new_text, msg.media is not None,
                    )
                    if not ok:
                        errors += 1
                        continue
                    await asyncio.sleep(0.5)   # stay under Telegram flood limit
                    for linked_id in pending_cache.get(cache_key, {}).get("linked_message_ids", []):
                        await _bot_edit_message(
                            bot_token, channel_id, linked_id, new_text, msg.media is not None,
                        )
                        await asyncio.sleep(0.5)

                edited += 1
                # If some picks are still pending, keep the cache entry (with broadcasted
                # markers) so we can re-enter this message next run; otherwise evict.
                still_pending = any(v[1] == "PENDING" for v in verdicts)
                if still_pending:
                    new_leg_verdicts = dict(cached_leg_verdicts)
                    for j, (lpick, lverdict, lcalc, lps, lgd, *_) in enumerate(verdicts):
                        if lverdict in ("WIN", "LOSS", "PUSH"):
                            new_leg_verdicts[str(j)] = {
                                "verdict": lverdict, "calc": lcalc,
                                "sport": lps, "game_date": lgd or date_str,
                                "broadcasted": True,   # prevent double-broadcast next run
                            }
                    pending_cache[cache_key] = _pending_entry(capper, parsed, new_leg_verdicts, pending_cache.get(cache_key, {}), odds_by_pick)
                else:
                    pending_cache.pop(cache_key, None)  # fully graded — evict from pending cache
                all_descs = "\n".join(
                    f"{v[0].get('description', '')}|{v[3]}|{v[4]}|{v[2]}" for v in verdicts if v[1] in _PICK_EMOJI
                )
                all_calcs = "  ·  ".join(
                    v[2] for v in verdicts if v[1] in _PICK_EMOJI and v[2]
                )
                first_odds = odds_by_pick.get("0", {})
                await audit.record(
                    channel_id=channel_id,
                    message_id=msg.id,
                    date=date_str,
                    sport=first_sport,
                    pick_desc=all_descs or first_pick.get("description", ""),
                    bet_type=first_pick.get("bet_type", ""),
                    verdict=overall,
                    calc=all_calcs or first_calc,
                    prev_caption=text,
                    new_caption=new_text if not dry_run else "",
                    dry_run=dry_run,
                    channel_name=ch_name,
                    capper_name=capper,
                    odds=first_odds.get("odds"), odds_bookmaker=first_odds.get("bookmaker"),
                    odds_match_type=first_odds.get("match_type"),
                )
                if newly_resolved:
                    await audit.broadcast_results(
                        channel_id=channel_id,
                        message_id=msg.id,
                        pick_results=[
                            (v[0], v[1], odds_by_pick.get(str(j), {}).get("odds"))
                            for j, v in newly_resolved_indexed
                        ],
                        capper_name=capper,
                    )

        print(f"  ─ edit:{edited} pend:{pending} fail:{failed} err:{errors}" +
              (f" odds:{odds_found}/{odds_total}" if odds_total else ""))

    if not dry_run:
        _save_pending_cache(pending_cache)

    run_type = "dry_run" if dry_run else "live"
    total_odds_quota = odds_requests_used() + odds_quota_used()
    print(f"\n[Claude total] {fmt_cost(usage_cost())}  |  [Odds API] {total_odds_quota} requests used")
    from audit import log_api_costs
    log_api_costs(run_type, usage_cost(), total_odds_quota)


# ─── Entry point ──────────────────────────────────────────────────────────────

async def main() -> None:
    import sys
    sys.stdout.reconfigure(encoding="utf-8", errors="replace", line_buffering=True)
    parser = argparse.ArgumentParser(description="Grade sports betting picks")
    parser.add_argument("--backtest", metavar="FILE", help="JSON export file to backtest")
    parser.add_argument("--grade",    metavar="TEXT", help="Grade a single pick message")
    parser.add_argument("--live",     action="store_true", help="Grade live Telegram channels")
    parser.add_argument("--date",     metavar="YYYY-MM-DD", help="Date for --grade (default: today)")
    parser.add_argument("--days",     type=float, default=7,
                        help="Days back to scan in --live mode (default: 7)")
    parser.add_argument("--channel",  type=int, metavar="ID",
                        help="Limit --live to a single channel ID (overrides GRADE_CHANNELS)")
    parser.add_argument("--dry-run",  action="store_true",
                        help="Log what would be graded/edited without touching Telegram")
    args = parser.parse_args()

    if args.backtest:
        await run_backtest(args.backtest)
    elif args.grade:
        date = args.date or _date.today().isoformat()
        await grade_one(args.grade, date)
    elif args.live:
        await run_live(dry_run=args.dry_run, days=args.days, channel=args.channel)
    else:
        parser.print_help()


if __name__ == "__main__":
    asyncio.run(main())
