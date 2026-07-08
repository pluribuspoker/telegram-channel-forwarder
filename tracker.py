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
import argparse
import re

from datetime import date as _date, timedelta
from zoneinfo import ZoneInfo

from dotenv import load_dotenv

from common import VERDICT_EMOJI, parlay_combined_odds
from scores import fetch_espn, odds_requests_used, try_early_grade_math, build_early_context, validate_sport
from odds import fetch_odds_current, quota_used as odds_quota_used
from ai import (
    claude_parse,
    claude_grade,
    build_context,
    CONTEXT_SKIP,
    CONTEXT_PENDING,
    CONTEXT_ESPN_ERROR,
    usage_cost,
    fmt_cost,
)
from tracker_cache import (
    _load_pending_cache,
    _save_pending_cache,
    _pending_entry,
    _find_duplicate_cache_key,
    _find_mirror_entry,
)
from tracker_grading import _overall_verdict, grade_matches_label
from tracker_format import (
    extract_label,
    strip_label,
    _insert_emojis,
    _insert_odds,
    _fmt_odds_audit,
    _bot_edit_message,
    _user_edit_message,
    _PICK_EMOJI,
)
from tracker_backtest import run_backtest
from sheets import append_pick_rows

load_dotenv()
load_dotenv(".env.local", override=True)  # VPS-specific overrides (never synced)

# ─── Live mode ────────────────────────────────────────────────────────────────

# Column widths for tabular pick output
_ID_W    = 5   # message ID
_CAP_W   = 15  # capper name
_DESC_W  = 28  # pick description
_ODDS_W  = 7   # odds e.g. [-115]
_TBL_W   = _ID_W + 1 + _CAP_W + 2 + _DESC_W + 1 + _ODDS_W + 1 + 4  # total table width

_TAG_ICON = {"WAIT": "⏳", "EDIT": "✏", "DRY ": "🧪", "SKIP": "⚠", "ESPN": "📡"}

_DAY_NAMES = {
    "monday": 0, "tuesday": 1, "wednesday": 2, "thursday": 3,
    "friday": 4, "saturday": 5, "sunday": 6,
}
_DAY_RE = re.compile(
    r'\b(' + '|'.join(_DAY_NAMES) + r')\b', re.IGNORECASE,
)


def _day_hint_date(text: str, msg_date: _date) -> str | None:
    """Extract a day-of-week from message text and return the nearest future date.

    When a capper posts "SATURDAY BEST BET" on Friday evening, the message
    date (ET) is Friday but the game is Saturday.  This uses the explicit
    day mention to compute the correct game date so we don't accidentally
    match a same-matchup game from a consecutive-day series that already
    completed on the message date.

    Returns YYYY-MM-DD if the hint day differs from msg_date, else None.
    """
    m = _DAY_RE.search(text)
    if not m:
        return None
    target_dow = _DAY_NAMES[m.group(1).lower()]
    msg_dow = msg_date.weekday()
    delta = (target_dow - msg_dow) % 7
    if delta == 0:
        return None  # same day — no override needed
    return (msg_date + timedelta(days=delta)).isoformat()


def _trunc(s: str, w: int) -> str:
    """Truncate string to width w, appending … if trimmed."""
    return s if len(s) <= w else s[:w - 1] + "…"

def _annotate_blockquotes(text: str, entities) -> str:
    """Prefix blockquoted portions with '> ' so the AI parser can distinguish them."""
    if not entities:
        return text
    bq_ranges = [(e.offset, e.offset + e.length) for e in entities
                 if type(e).__name__ == "MessageEntityBlockquote"]
    if not bq_ranges:
        return text
    bq_ranges.sort()
    parts: list[str] = []
    pos = 0
    for start, end in bq_ranges:
        parts.append(text[pos:start])
        bq = text[start:end]
        parts.append("\n".join("> " + line for line in bq.split("\n")))
        pos = end
    parts.append(text[pos:])
    return "".join(parts)


def _to_bot_html(text: str, entities) -> str:
    """Convert Telethon message text+entities to Bot API-compatible HTML."""
    from telethon.extensions import html as tl_html
    ht = tl_html.unparse(text, entities or [])
    return ht.replace("<spoiler>", "<tg-spoiler>").replace("</spoiler>", "</tg-spoiler>")


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
    sheets_map: dict[int, str] = {}  # dest_channel → "sheet_id:gid"
    results_filter_map: dict[int, str] = {}  # dest_channel → regex pattern
    for m in json.loads(os.getenv("MAPPINGS_CONFIG", "[]")):
        dest = m.get("dest_channel")
        bc_key = "test_broadcast_results_channel" if dry_run else "broadcast_results_channel"
        bc = m.get(bc_key)
        if dest and bc:
            broadcast_results_map[dest] = bc
        if dest and m.get("sheets_id"):
            sheets_map[dest] = m["sheets_id"]
        if dest and m.get("results_filter"):
            results_filter_map[dest] = m["results_filter"]

    # Channels where messages are sent via user account and need user-account editing
    user_edit_channels: set[int] = set()
    # Channels where only listener-forwarded messages should be graded
    forwarded_only_channels: set[int] = set()
    for m in json.loads(os.getenv("MAPPINGS_CONFIG", "[]")):
        if m.get("send_as_user") and m.get("dest_channel"):
            user_edit_channels.add(m["dest_channel"])
        if m.get("grade_forwarded_only") and m.get("dest_channel"):
            forwarded_only_channels.add(m["dest_channel"])

    audit         = AuditLog(broadcast_results_mappings=broadcast_results_map)
    pending_cache = _load_pending_cache()
    cutoff = dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=days)
    mode   = "DRY RUN" if dry_run else "LIVE"

    async with TelegramClient(StringSession(session), api_id, api_hash) as client:

        async def _edit_msg(ch_id: int, msg_id: int, new_text: str, has_media: bool) -> bool:
            """Edit via bot, falling back to user account for send_as_user channels."""
            ok = await _bot_edit_message(bot_token, ch_id, msg_id, new_text, has_media)
            if not ok and ch_id in user_edit_channels:
                ok = await _user_edit_message(client, ch_id, msg_id, new_text)
            return ok

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
                    for sid, m in zip(stale_ids, fetched if isinstance(fetched, list) else [fetched]):
                        if m:
                            yield m
                        else:
                            # Primary message was deleted — promote a surviving linked dupe
                            dead_key = f"{channel_id}:{sid}"
                            dead_entry = pending_cache.get(dead_key, {})
                            linked = dead_entry.get("linked_message_ids", [])
                            if linked and isinstance(dead_entry, dict) and "parsed" in dead_entry:
                                # Try to find a surviving linked message
                                alive = await client.get_messages(channel_id, ids=linked)
                                alive_list = alive if isinstance(alive, list) else [alive]
                                for lmsg in alive_list:
                                    if lmsg and lmsg.id:
                                        new_key = f"{channel_id}:{lmsg.id}"
                                        # Promote: copy parsed data from dead primary
                                        promoted = {
                                            "capper_name":        dead_entry.get("capper_name", ""),
                                            "parsed":             dead_entry["parsed"],
                                            "leg_verdicts":       dead_entry.get("leg_verdicts", {}),
                                            "linked_message_ids": [i for i in linked if i != lmsg.id],
                                            "odds_by_pick":       dead_entry.get("odds_by_pick", {}),
                                        }
                                        pending_cache[new_key] = promoted
                                        # Update remaining dupes to point to new primary
                                        for other_id in linked:
                                            if other_id != lmsg.id:
                                                ok = f"{channel_id}:{other_id}"
                                                if ok in pending_cache and isinstance(pending_cache[ok], dict):
                                                    pending_cache[ok]["primary_id"] = lmsg.id
                                        # Remove the dead primary
                                        pending_cache.pop(dead_key, None)
                                        print(f"  [dupe] promoted {lmsg.id} (primary {sid} deleted)")
                                        yield lmsg
                                        break

            async for msg in _iter_with_catchup():
                msg_date = msg.date
                if msg_date.tzinfo is None:
                    msg_date = msg_date.replace(tzinfo=dt.timezone.utc)

                text = msg.text or ""
                # Convert to US Eastern so late-night picks (e.g. 9:38 PM ET =
                # next day UTC) map to the correct ESPN game date.
                date_str = msg_date.astimezone(ZoneInfo("America/New_York")).strftime("%Y-%m-%d")

                cache_key = f"{channel_id}:{msg.id}"
                visited_keys.add(cache_key)

                if not text.strip():
                    continue
                # Skip messages whose first line contains "__" (manually excluded)
                if "__" in text.splitlines()[0]:
                    continue
                # In forwarded-only channels, skip messages not seeded by the listener.
                # Accept any existing cache entry (parsed/_dupe/_failed) as proof the
                # message already passed the _forwarded gate on a previous run.
                if channel_id in forwarded_only_channels:
                    cached_fwd = pending_cache.get(cache_key)
                    if not (isinstance(cached_fwd, dict) and (
                        cached_fwd.get("_forwarded") or "parsed" in cached_fwd
                        or cached_fwd.get("_dupe") or cached_fwd.get("_failed")
                    )):
                        continue
                # Skip messages that don't match results_filter (e.g. leans without
                # units in DF channel).  Already-cached messages are allowed through
                # so partially-graded picks can finish.
                rf = results_filter_map.get(channel_id)
                if rf and cache_key not in pending_cache:
                    if not re.search(rf, text):
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
                skip_odds = channel_id in forwarded_only_channels
                if isinstance(cached, dict) and cached.get("_dupe"):
                    # Edit odds onto the duplicate if the primary has them but this msg doesn't yet
                    primary_key = f"{channel_id}:{cached.get('primary_id')}"
                    primary_entry = pending_cache.get(primary_key, {})
                    if not dry_run and not skip_odds and isinstance(primary_entry, dict):
                        dup_odds = primary_entry.get("odds_by_pick", {})
                        if any(v.get("odds") is not None for v in dup_odds.values()):
                            dup_picks = primary_entry.get("parsed", {}).get("picks", [])
                            _ht = _to_bot_html(text, msg.entities)
                            _odds_text = _insert_odds(_ht, dup_picks, dup_odds)
                            if _odds_text != _ht:
                                await _edit_msg(channel_id, msg.id, _odds_text, msg.media is not None)
                                await asyncio.sleep(0.5)
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
                # Mirror: reuse parse/odds from same source message forwarded to another channel
                mirror_source_key = cached.get("_source_key") if isinstance(cached, dict) else None
                if not cached_parse and mirror_source_key:
                    mirror = _find_mirror_entry(pending_cache, mirror_source_key, cache_key)
                    if mirror:
                        cached_parse = mirror["parsed"]
                        cached_leg_verdicts = mirror.get("leg_verdicts", {})
                        capper = mirror.get("capper_name", capper)
                        # Inject mirror data into cache so downstream odds_by_pick lookup finds it
                        entry_update = dict(cached) if isinstance(cached, dict) else {}
                        entry_update["parsed"] = cached_parse
                        entry_update["leg_verdicts"] = cached_leg_verdicts
                        entry_update["capper_name"] = capper
                        if not skip_odds and mirror.get("odds_by_pick"):
                            entry_update["odds_by_pick"] = mirror["odds_by_pick"]
                        pending_cache[cache_key] = entry_update
                        print(f"  [mirror] reusing parse+odds from {mirror_source_key}")
                # Leg indices that were already broadcast in a previous partial edit
                already_broadcast_indices = {
                    int(k) for k, v in cached_leg_verdicts.items()
                    if isinstance(v, dict) and v.get("broadcasted")
                }
                # Day-of-week hint from message text (e.g. "SATURDAY BEST BET"
                # sent Friday evening).  Invalidate any cached verdicts whose
                # game_date no longer matches the hint so they get re-graded
                # against the correct game.
                day_hint = _day_hint_date(text, _date.fromisoformat(date_str))
                if day_hint:
                    for k, v in cached_leg_verdicts.items():
                        if isinstance(v, dict) and v.get("game_date") and v["game_date"] != day_hint:
                            already_broadcast_indices.discard(int(k))
                # Skip fully-resolved messages where every leg has been broadcast
                if cached_parse and already_broadcast_indices:
                    n_picks = len(cached_parse.get("picks", []))
                    if n_picks > 0 and len(already_broadcast_indices) >= n_picks:
                        continue
                parsed = cached_parse or await claude_parse(
                    _annotate_blockquotes(text, msg.entities), date_str,
                )
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

                # Validate sport classification against ESPN schedules
                # (only on fresh parses, not cached ones — cached entries
                # were already validated on their first run)
                if not cached_parse and picks:
                    teams = picks[0].get("teams", [])
                    bet_desc = picks[0].get("description", "")
                    new_sport, new_teams = await validate_sport(
                        sport, teams, bet_desc, date_str, scoreboard_cache,
                    )
                    if new_sport != sport:
                        print(f"  ESPN sport override: {sport} -> {new_sport}")
                        sport = new_sport
                        parsed["sport"] = sport
                    if new_teams != teams:
                        picks[0]["teams"] = new_teams
                        # Team was corrected to match top-level sport —
                        # clear any conflicting pick-level sport override
                        # (e.g., AI said KBO "KIA Tigers" but ESPN confirmed MLB "Detroit Tigers")
                        ps0 = picks[0].get("sport")
                        if ps0 and ps0 != sport:
                            print(f"  Clearing pick[0] sport override: {ps0} -> inherit {sport}")
                            del picks[0]["sport"]

                    # Also validate per-pick sport overrides (cross-sport parlays)
                    for pick in picks[1:]:
                        ps = pick.get("sport")
                        if ps and ps in ("NBA", "NCAAB", "MLB", "NFL", "NHL"):
                            pt = pick.get("teams", [])
                            pd = pick.get("description", "")
                            new_ps, new_pt = await validate_sport(
                                ps, pt, pd, date_str, scoreboard_cache,
                            )
                            if new_ps != ps:
                                print(f"  ESPN pick sport override: {ps} -> {new_ps}")
                                pick["sport"] = new_ps
                            if new_pt != pt:
                                pick["teams"] = new_pt

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
                    newly_linked = msg.id not in linked
                    if newly_linked:
                        linked.append(msg.id)
                    # Edit odds onto the duplicate if the primary already has them
                    primary_entry = pending_cache[dup_key]
                    dup_odds = primary_entry.get("odds_by_pick", {})
                    if not dry_run and newly_linked and any(v.get("odds") is not None for v in dup_odds.values()):
                        dup_picks = primary_entry.get("parsed", {}).get("picks", [])
                        _ht = _to_bot_html(text, msg.entities)
                        _odds_text = _insert_odds(_ht, dup_picks, dup_odds)
                        if _odds_text != _ht:
                            await _edit_msg(channel_id, msg.id, _odds_text, msg.media is not None)
                            await asyncio.sleep(0.5)
                    # Cache the dupe marker so we skip claude_parse on future runs
                    pending_cache[cache_key] = {"_dupe": True, "primary_id": dup_id}
                    continue  # primary row carries the +N dup annotation

                # ── Fetch odds at first encounter ─────────────────────────────
                # Only fetch once — if odds_by_pick is already in the cache, reuse it.
                # Skip odds entirely for forwarded-only channels (results only).
                cached_entry = pending_cache.get(cache_key) if isinstance(pending_cache.get(cache_key), dict) else {}
                odds_by_pick: dict = {} if skip_odds else cached_entry.get("odds_by_pick", {})
                odds_were_empty = not odds_by_pick
                if odds_were_empty and not skip_odds:
                    print(f"  [odds] fetching fresh (no cache) for {cache_key}")
                if not odds_by_pick and not skip_odds:
                    for i, pick in enumerate(picks):
                        pick_sport = pick.get("sport") or sport
                        result = await fetch_odds_current(pick_sport, pick)
                        display_odds, warn = result.validate_for_display()
                        pick_desc = pick.get("description", "")
                        if display_odds is None:
                            reason = warn or result.match_type
                            print(f"  [odds] miss({result.match_type}) {pick_desc[:60]}")
                            if not result.is_structural_miss:
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
                            "game_date":          result.game_date,
                        }
                        odds_total += 1
                        if display_odds is not None:
                            odds_found += 1

                # Edit odds into the message so they appear while PENDING.
                # Idempotent — _insert_odds won't re-add if tag already present.
                if not dry_run and not skip_odds and any(v.get("odds") is not None for v in odds_by_pick.values()):
                    _ht = _to_bot_html(text, msg.entities)
                    _odds_text = _insert_odds(_ht, picks, odds_by_pick)
                    if _odds_text != _ht:
                        await _edit_msg(channel_id, msg.id, _odds_text, msg.media is not None)
                        await asyncio.sleep(0.5)
                        for linked_id in cached_entry.get("linked_message_ids", []):
                            await _edit_msg(channel_id, linked_id, _odds_text, msg.media is not None)
                            await asyncio.sleep(0.5)

                base_date = day_hint or date_str
                sb_key = (sport, base_date)
                if sb_key not in scoreboard_cache:
                    scoreboard_cache[sb_key] = await fetch_espn(sport, base_date)

                verdicts = []
                has_espn_error = False
                for i, pick in enumerate(picks):
                    pick_sport = pick.get("sport") or sport
                    odds_gd = odds_by_pick.get(str(i), {}).get("game_date")
                    # Prefer explicit day-of-week mention in the message
                    # text (e.g. "SATURDAY BEST BET" sent Friday evening),
                    # then Odds API game_date, then message date.
                    if day_hint:
                        eff_date = day_hint
                    elif odds_gd and odds_gd != date_str and abs((_date.fromisoformat(odds_gd) - _date.fromisoformat(date_str)).days) <= 2:
                        eff_date = odds_gd
                    else:
                        eff_date = date_str

                    cached_leg = cached_leg_verdicts.get(str(i))
                    # Invalidate stale cached verdict if the game date
                    # changed (day hint now points to the correct game).
                    if cached_leg and day_hint and cached_leg.get("game_date") and cached_leg["game_date"] != day_hint:
                        cached_leg = None
                    if cached_leg and cached_leg.get("verdict") in ("WIN", "LOSS", "PUSH"):
                        # Resolved leg — use cached verdict, skip ESPN + Claude calls
                        verdict   = cached_leg["verdict"]
                        calc      = cached_leg["calc"]
                        pick_sport = cached_leg.get("sport", pick_sport)
                        game_date  = cached_leg.get("game_date", eff_date)
                    else:
                        ps_key = (pick_sport, eff_date)
                        if ps_key not in scoreboard_cache:
                            scoreboard_cache[ps_key] = await fetch_espn(pick_sport, eff_date)
                        sb = scoreboard_cache[ps_key]

                        # Early grade: totals where score already exceeds the line
                        early = try_early_grade_math(pick_sport, pick, sb)
                        if early:
                            verdict, calc = early
                            game_date = eff_date
                        else:
                            # Early context: period bets where the period is complete
                            early_ctx = build_early_context(pick_sport, pick, sb)
                            if early_ctx:
                                context, game_date = early_ctx, eff_date
                            else:
                                context, game_date = await build_context(
                                    pick_sport, eff_date, pick, sb, summary_cache,
                                    odds_game_date=odds_gd,
                                    msg_date=date_str,
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
                                    pick.get("prop_stat") or "",
                                )
                    verdicts.append((pick, verdict, calc, pick_sport, game_date))

                # Build edited text — odds then emoji inserted inline after each pick's line
                html_text = _to_bot_html(text, msg.entities)
                if not skip_odds:
                    html_text = _insert_odds(html_text, picks, odds_by_pick)
                # Only insert emojis for picks not already broadcast — their emojis
                # are already in the text.  Re-inserting them causes _match_pick_line
                # to fall through to heuristic matching and place emojis on wrong lines.
                emoji_verdicts = [
                    v for j, v in enumerate(verdicts)
                    if j not in already_broadcast_indices
                ]
                new_text = _insert_emojis(html_text, emoji_verdicts)
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
                # But if the message also contains standalone (non-parlay) picks
                # that are newly resolved, we must still edit for those.
                parlay_pending = is_parlay and overall == "PENDING"
                newly_resolved_non_parlay = [
                    v for _, v in newly_resolved_indexed
                    if not v[0].get("is_parlay_leg")
                ]
                parlay_blocks_edit = parlay_pending and not newly_resolved_non_parlay

                # Print all picks with their individual verdicts
                has_pending = any(v[1] == "PENDING" for v in verdicts)
                if not newly_resolved or parlay_blocks_edit:
                    if has_espn_error and (has_pending or parlay_blocks_edit):
                        tag = "ESPN"
                    elif has_pending or parlay_blocks_edit:
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
                    _leg_odds = [odds_by_pick.get(str(i), {}).get("odds")
                                 for i, v in enumerate(verdicts) if v[0].get("is_parlay_leg")]
                    _comb = parlay_combined_odds(_leg_odds)
                    if _comb is not None:
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
                if not graded or parlay_blocks_edit:
                    new_leg_verdicts = dict(cached_leg_verdicts)  # preserve previously cached
                    for j, (lpick, lverdict, lcalc, lps, lgd, *_) in enumerate(verdicts):
                        if lverdict in ("WIN", "LOSS", "PUSH"):
                            entry = {
                                "verdict": lverdict, "calc": lcalc,
                                "sport": lps, "game_date": lgd or date_str,
                            }
                            if cached_leg_verdicts.get(str(j), {}).get("broadcasted"):
                                entry["broadcasted"] = True
                            new_leg_verdicts[str(j)] = entry
                    pending_cache[cache_key] = _pending_entry(capper, parsed, new_leg_verdicts, pending_cache.get(cache_key, {}), odds_by_pick)

                # Nothing new to grade this run — log and skip
                if not newly_resolved or parlay_blocks_edit:
                    if overall == "PENDING" or parlay_blocks_edit:
                        pending += 1
                    else:
                        failed += 1
                    all_descs = "\n".join(
                        f"{v[1]}: {v[0].get('description', '')}|{v[3]}|{v[4]}|{v[2]}" for v in verdicts
                    )
                    first_pick, _, first_calc, first_sport, first_game_date = verdicts[0]
                    first_odds = odds_by_pick.get("0", {})
                    already_unknown_notified = cached_entry.get("_unknown_notified")
                    all_already_broadcast = (
                        already_broadcast_indices
                        and all(j in already_broadcast_indices for j in range(len(verdicts)))
                    )
                    if not has_espn_error and not all_already_broadcast and not (overall == "UNKNOWN" and already_unknown_notified):
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
                    # If odds were freshly fetched this run, write them to the cache now
                    # so subsequent runs don't re-fetch and re-warn. Only touch odds_by_pick
                    # to avoid disturbing leg_verdicts (e.g. broadcasted flags).
                    if odds_were_empty and odds_by_pick:
                        existing = pending_cache.get(cache_key, {})
                        if isinstance(existing, dict):
                            pending_cache[cache_key] = {**existing, "odds_by_pick": odds_by_pick}
                    continue

                first_pick, _, first_calc, first_sport, first_game_date = verdicts[0]

                edit_failed = False
                text_unchanged = (new_text == html_text)
                if not dry_run and not text_unchanged:
                    ok = await _edit_msg(
                        channel_id, msg.id, new_text, msg.media is not None,
                    )
                    if not ok:
                        errors += 1
                        edit_failed = True
                    else:
                        await asyncio.sleep(0.5)   # stay under Telegram flood limit
                        for linked_id in pending_cache.get(cache_key, {}).get("linked_message_ids", []):
                            await _edit_msg(
                                channel_id, linked_id, new_text, msg.media is not None,
                            )
                            await asyncio.sleep(0.5)

                if not edit_failed:
                    edited += 1
                # If some picks are still pending, keep the cache entry (with broadcasted
                # markers) so we can re-enter this message next run; otherwise evict.
                # Cache resolved legs with broadcasted=True to prevent double-broadcast.
                # Keep full entry if some picks are still pending; minimal entry otherwise.
                new_leg_verdicts = dict(cached_leg_verdicts)
                for j, (lpick, lverdict, lcalc, lps, lgd, *_) in enumerate(verdicts):
                    if lverdict in ("WIN", "LOSS", "PUSH"):
                        already_bc = cached_leg_verdicts.get(str(j), {}).get("broadcasted", False)
                        new_leg_verdicts[str(j)] = {
                            "verdict": lverdict, "calc": lcalc,
                            "sport": lps, "game_date": lgd or date_str,
                            "broadcasted": already_bc or (not dry_run and not edit_failed),
                        }
                pending_cache[cache_key] = _pending_entry(capper, parsed, new_leg_verdicts, pending_cache.get(cache_key, {}), odds_by_pick)
                all_descs = "\n".join(
                    f"{v[1]}: {v[0].get('description', '')}|{v[3]}|{v[4]}|{v[2]}" for v in verdicts if v[1] in _PICK_EMOJI
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
                    edit_failed=edit_failed,
                )
                if newly_resolved:
                    _nr_pick_results = []
                    for j, v in newly_resolved_indexed:
                        pick_dict = v[0]
                        if not pick_dict.get("sport"):
                            pick_dict["sport"] = v[3]  # pick_sport from verdicts
                        _nr_pick_results.append((pick_dict, v[1], odds_by_pick.get(str(j), {}).get("odds")))
                    await audit.broadcast_results(
                        channel_id=channel_id,
                        message_id=msg.id,
                        pick_results=_nr_pick_results,
                        capper_name=capper,
                        client=client,
                    )
                    if not dry_run and channel_id in sheets_map:
                        try:
                            await append_pick_rows(
                                pick_results=_nr_pick_results,
                                date_str=date_str,
                                raw_text=text,
                                sheets_id=sheets_map[channel_id],
                            )
                        except Exception as exc:
                            print(f"[sheets] warn: {exc}")

        print(f"  ─ edit:{edited} pend:{pending} fail:{failed} err:{errors}" +
              (f" odds:{odds_found}/{odds_total}" if odds_total else ""))

    if not dry_run:
        _save_pending_cache(pending_cache)

    run_type = "dry_run" if dry_run else "live"
    total_odds_quota = odds_requests_used() + odds_quota_used()
    print(f"\n[Claude total] {fmt_cost(usage_cost())}  |  [Odds API] {total_odds_quota} requests used")
    from audit import log_api_costs
    log_api_costs(run_type, usage_cost(), total_odds_quota)


# ─── grade_one ────────────────────────────────────────────────────────────────

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

    # Day-of-week hint overrides the supplied date
    day_hint = _day_hint_date(clean, _date.fromisoformat(date))
    eff_date = day_hint or date
    if day_hint:
        print(f"Day hint: {day_hint} (message date: {date})")

    sb_key = (sport, eff_date)
    scoreboard_cache[sb_key] = await fetch_espn(sport, eff_date)

    for i, pick in enumerate(picks, 1):
        pick_sport = pick.get("sport") or sport
        if pick_sport != sport:
            ps_key = (pick_sport, eff_date)
            if ps_key not in scoreboard_cache:
                scoreboard_cache[ps_key] = await fetch_espn(pick_sport, eff_date)
            scoreboard = scoreboard_cache[ps_key]
        else:
            scoreboard = scoreboard_cache[sb_key]

        pick_desc = pick.get("description", clean[:80])
        print(f"Pick {i}: {pick_desc}")
        print(f"  sport={pick_sport}  bet_type={pick.get('bet_type')}  period={pick.get('period','game')}"
              f"  teams={pick.get('teams')}  player={pick.get('player')}"
              f"  line={pick.get('line')}  dir={pick.get('direction')}"
              f"  parlay_leg={pick.get('is_parlay_leg', False)}")

        # Try pure-math early grade first
        early = try_early_grade_math(pick_sport, pick, scoreboard)
        if early:
            grade, calc = early
            print()
            print(f"  EARLY : {grade}")
            print(f"  CALC  : {calc}")
        else:
            # Try period-complete early context
            early_ctx = build_early_context(pick_sport, pick, scoreboard)
            if early_ctx:
                context, _game_date = early_ctx, eff_date
            else:
                context, _game_date = await build_context(pick_sport, eff_date, pick, scoreboard, summary_cache)
            print()
            print("  CONTEXT:")
            if context == CONTEXT_SKIP:
                print("    [skipped]")
            else:
                for ln in context.splitlines():
                    print(f"    {ln}")
            print()

            if context != CONTEXT_SKIP:
                grade, calc = await claude_grade(pick_desc, date, context, pick.get("bet_type", ""), pick.get("prop_stat") or "")
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
