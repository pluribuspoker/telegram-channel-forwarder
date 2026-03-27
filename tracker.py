#!/usr/bin/env python3
"""
tracker.py — Grade sports picks from Telegram channel exports.

Usage:
  python tracker.py --backtest result_df.json
  python tracker.py --backtest result.json
"""

import asyncio
import json
import os
import re
import argparse

from datetime import date as _date, timedelta

import anthropic
import httpx
from dotenv import load_dotenv

from common import VERDICT_EMOJI
from scores import ESPN_LEAGUES, fetch_espn, scoreboard_text
from ai import (
    claude,
    claude_parse,
    claude_grade,
    build_context,
    CONTEXT_SKIP,
    CONTEXT_PENDING,
    usage_cost,
    _usage,
)

load_dotenv()

# Cache of parsed-but-pending messages so we don't re-call Claude on every run
_PENDING_CACHE_PATH = os.path.join(os.path.dirname(__file__), "parse_cache.json")


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
    Insert per-pick verdict emojis inline after each pick's line in the message.
    Matches each pick to its line using team/player names, then appends the emoji.
    Lines that can't be matched are left unchanged.
    Returns the modified text (or original if nothing could be matched).
    """
    lines = text.rstrip().split("\n")

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
    if r["period"] != "game":
        return f"period ({r['period']})"
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
        w(f"Cost   : ${cost:.4f}  ({_usage['input_tokens']:,} in / {_usage['output_tokens']:,} out tokens)")
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

        parsed = await claude_parse(clean)
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

            if context == CONTEXT_SKIP:
                grade, calc = "UNKNOWN", ""
            else:
                grade, calc = await claude_grade(pick_desc, date, context)

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
    print(f"Cost     : ${cost:.4f}  ({_usage['input_tokens']:,} in / {_usage['output_tokens']:,} out tokens, Sonnet 4.6)")

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
    base = os.path.splitext(os.path.basename(filepath))[0]
    out_path = f"backtest_{base}.txt"
    _write_detail_file(out_path, filepath, results, graded, correct_list, skipped_list, wrong_list, cost)
    print(f"\nDetail file: {out_path}")


async def grade_one(text: str, date: str) -> None:
    """Parse and grade a single pick message, printing full detail."""
    import sys
    sys.stdout.reconfigure(encoding="utf-8")

    label = extract_label(text)
    clean = strip_label(text)

    parsed = await claude_parse(clean)
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
            grade, calc = await claude_grade(pick_desc, date, context)
            print(f"  GRADE : {grade}")
            print(f"  CALC  : {calc}")
        else:
            grade = "UNKNOWN"
            print(f"  GRADE : UNKNOWN (skipped)")

        if label:
            correct = grade_matches_label(grade, label)
            print(f"  LABEL : {label.upper()}  →  {'OK' if correct else ('--' if grade in ('PUSH','UNKNOWN') else 'XX')}")
        print()

    cost = usage_cost()
    print(f"Cost: ${cost:.4f}  ({_usage['input_tokens']:,} in / {_usage['output_tokens']:,} out tokens)")


# ─── Live mode ────────────────────────────────────────────────────────────────

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

    audit         = AuditLog()
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

        print(f"\nLive grader — {mode}  |  last {days} days  |  channels: {[channel_names[c] for c in channel_ids]}")
        print("=" * 72)

        for channel_id in channel_ids:
            ch_name = channel_names[channel_id]
            print(f"\n{ch_name}  ({channel_id}):")
            scoreboard_cache: dict = {}
            summary_cache:   dict = {}
            edited = skipped = errors = 0

            async for msg in client.iter_messages(channel_id):
                msg_date = msg.date
                if msg_date.tzinfo is None:
                    msg_date = msg_date.replace(tzinfo=dt.timezone.utc)
                if msg_date < cutoff:
                    break

                text = msg.text or ""
                date_str = msg_date.strftime("%Y-%m-%d")

                if not text.strip():
                    continue
                # Skip already graded (check plain text)
                if any(ch in text for ch in ("\u2705", "\u274c", "\u21a9\ufe0f")):
                    continue

                capper  = next((l.strip() for l in text.splitlines() if l.strip()), "")
                snippet = " ".join(text.split())[:80]
                cache_key = f"{channel_id}:{msg.id}"
                parsed = pending_cache.get(cache_key) or await claude_parse(text)
                if not parsed:
                    skipped += 1
                    print(f"\n  [SKIP] msg {msg.id}  {date_str}  parse failed")
                    print(f"         {snippet}")
                    await audit.record(
                        channel_id=channel_id, message_id=msg.id, date=date_str,
                        sport="Other", pick_desc=snippet, bet_type="",
                        verdict="UNKNOWN", calc="parse failed",
                        prev_caption=text, dry_run=dry_run, channel_name=ch_name, capper_name=capper,
                    )
                    continue
                sport = parsed.get("sport", "Other")
                picks = parsed.get("picks", [])
                if not picks:
                    skipped += 1
                    print(f"\n  [SKIP] msg {msg.id}  {date_str}  no picks extracted  ({sport})")
                    print(f"         {snippet}")
                    await audit.record(
                        channel_id=channel_id, message_id=msg.id, date=date_str,
                        sport=sport, pick_desc=snippet, bet_type="",
                        verdict="UNKNOWN", calc="no picks extracted",
                        prev_caption=text, dry_run=dry_run, channel_name=ch_name, capper_name=capper,
                    )
                    continue

                sb_key = (sport, date_str)
                if sb_key not in scoreboard_cache:
                    scoreboard_cache[sb_key] = await fetch_espn(sport, date_str)

                verdicts = []
                for pick in picks:
                    pick_sport = pick.get("sport") or sport
                    ps_key = (pick_sport, date_str)
                    if ps_key not in scoreboard_cache:
                        scoreboard_cache[ps_key] = await fetch_espn(pick_sport, date_str)
                    context, game_date = await build_context(
                        pick_sport, date_str, pick,
                        scoreboard_cache[ps_key], summary_cache,
                    )
                    if context == CONTEXT_PENDING:
                        verdict, calc = "PENDING", ""
                    elif context == CONTEXT_SKIP:
                        verdict, calc = "UNKNOWN", ""
                    else:
                        verdict, calc = await claude_grade(
                            pick.get("description", text[:80]), date_str, context,
                        )
                    verdicts.append((pick, verdict, calc, pick_sport, game_date))

                # Build edited text — per-pick emoji inserted inline after each pick's line
                # Convert to HTML to preserve original formatting entities
                from telethon.extensions import html as tl_html
                import html as _html
                html_text = tl_html.unparse(text, msg.entities or [])
                # Escape any HTML special chars that Telethon may have left as plain text
                # (unparse already handles this, but sanitise spoiler tag for Bot API)
                html_text = html_text.replace("<spoiler>", "<tg-spoiler>").replace("</spoiler>", "</tg-spoiler>")
                new_text = _insert_emojis(html_text, verdicts)
                graded = [v for v in verdicts if v[1] in _PICK_EMOJI]
                overall = _overall_verdict(verdicts)

                # Print all picks with their individual verdicts
                has_pending = any(v[1] == "PENDING" for v in verdicts)
                if not graded:
                    tag = "WAIT" if has_pending else "SKIP"
                else:
                    tag = "DRY " if dry_run else "EDIT"
                print(f"\n  [{tag}] msg {msg.id}")
                for pick, verdict, calc, ps, gd, *_ in verdicts:
                    desc = pick.get("description", "")[:60]
                    calc_str = f"  ({calc})" if calc else ""
                    gd_short = f"{_date.fromisoformat(gd).month}/{_date.fromisoformat(gd).day}" if gd else date_str
                    print(f"         {verdict:<7}  {desc}{calc_str}  [{ps} · {gd_short}]")

                # Cache the parse result for pending messages to avoid re-parsing on next run
                if not graded:
                    pending_cache[cache_key] = parsed

                # Nothing gradeable — log and skip
                if not graded:
                    skipped += 1
                    all_descs = "\n".join(
                        f"{v[1]}: {v[0].get('description', '')}|{v[3]}|{v[4]}|{v[2]}" for v in verdicts
                    )
                    first_pick, _, first_calc, first_sport, first_game_date = verdicts[0]
                    await audit.record(
                        channel_id=channel_id, message_id=msg.id, date=date_str,
                        sport=first_sport,
                        pick_desc=all_descs,
                        bet_type=first_pick.get("bet_type", ""),
                        verdict=overall, calc=first_calc,
                        prev_caption=text, dry_run=dry_run, channel_name=ch_name, capper_name=capper,
                    )
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

                edited += 1
                pending_cache.pop(cache_key, None)  # graded — evict from pending cache
                all_descs = "\n".join(
                    f"{v[0].get('description', '')}|{v[3]}|{v[4]}|{v[2]}" for v in verdicts if v[1] in _PICK_EMOJI
                )
                all_calcs = "  ·  ".join(
                    v[2] for v in verdicts if v[1] in _PICK_EMOJI and v[2]
                )
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
                )

            print(f"\n  => edited: {edited}  skipped: {skipped}  errors: {errors}")

    if not dry_run:
        _save_pending_cache(pending_cache)

    cost = usage_cost()
    print(f"\nCost: ${cost:.4f}  ({_usage['input_tokens']:,} in / {_usage['output_tokens']:,} out)")


# ─── Entry point ──────────────────────────────────────────────────────────────

async def main() -> None:
    import sys
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    parser = argparse.ArgumentParser(description="Grade sports betting picks")
    parser.add_argument("--backtest", metavar="FILE", help="JSON export file to backtest")
    parser.add_argument("--grade",    metavar="TEXT", help="Grade a single pick message")
    parser.add_argument("--live",     action="store_true", help="Grade live Telegram channels")
    parser.add_argument("--date",     metavar="YYYY-MM-DD", help="Date for --grade (default: today)")
    parser.add_argument("--days",     type=int, default=7,
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
