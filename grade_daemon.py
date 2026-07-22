#!/usr/bin/env python3
"""
grade_daemon.py — Lightweight persistent daemon that grades pending picks.

Runs as a long-lived process alongside the 5-min tracker timer.  Every 10
seconds it:
  1. Reloads parse_cache.json (only when mtime changes)
  2. Finds picks with unresolved legs
  3. Checks ESPN scoreboards for finished games
  4. Grades via Claude when a game completes
  5. Edits emoji onto the Telegram message via Bot API (no Telethon)
  6. Broadcasts results via Bot API
  7. Logs to Google Sheets

Zero Telethon dependency — all Telegram writes use the Bot API (plain HTTP).
This avoids session/flood-wait risk entirely.
"""

import asyncio
import json
import os
import socket
import sys
import time
import traceback

from datetime import date as _date, timedelta
from dotenv import load_dotenv

load_dotenv()
load_dotenv(".env.local", override=True)

from common import VERDICT_EMOJI, parlay_combined_odds
from scores import fetch_espn, try_early_grade_math, build_early_context
from ai import (
    claude_grade,
    build_context,
    CONTEXT_SKIP,
    CONTEXT_PENDING,
    CONTEXT_ESPN_ERROR,
    usage_cost,
    fmt_cost,
)
from tracker_cache import _load_pending_cache, _save_pending_cache
from tracker_grading import _overall_verdict
from tracker_format import _insert_emojis, _bot_edit_message, _PICK_EMOJI
from audit import AuditLog, log_api_costs
from sheets import append_pick_rows

# ─── Config ──────────────────────────────────────────────────────────────────

LOOP_INTERVAL = int(os.getenv("GRADE_DAEMON_INTERVAL", "10"))
ESPN_CACHE_TTL = 30  # seconds — don't re-fetch same sport/date faster than this
# Backstop: if a single grade cycle runs longer than this it is aborted and
# retried next cycle, so no hung network call can freeze the daemon (see the
# ~35-min silent hang caused by an untimed Claude request). Should comfortably
# exceed a normal cycle (seconds) and the worst-case per-request time.
CYCLE_TIMEOUT = int(os.getenv("GRADE_DAEMON_CYCLE_TIMEOUT", "300"))

_CACHE_PATH = os.path.join(os.path.dirname(__file__), "parse_cache.json")


def _sd_notify(state: str) -> None:
    """Best-effort systemd notification (e.g. WATCHDOG=1). No-op when not run
    under systemd. Pure stdlib — avoids a python-systemd dependency."""
    addr = os.environ.get("NOTIFY_SOCKET")
    if not addr:
        return
    if addr[0] == "@":  # abstract-namespace socket
        addr = "\0" + addr[1:]
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as sock:
            sock.connect(addr)
            sock.sendall(state.encode())
    except OSError:
        pass


def _build_broadcast_map() -> dict[int, int]:
    """dest_channel → broadcast_results_channel from MAPPINGS_CONFIG."""
    result: dict[int, int] = {}
    for m in json.loads(os.getenv("MAPPINGS_CONFIG", "[]")):
        dest = m.get("dest_channel")
        bc = m.get("broadcast_results_channel")
        if dest and bc:
            result[dest] = bc
    return result


def _build_sheets_map() -> dict[int, str]:
    """dest_channel → sheets_id from MAPPINGS_CONFIG."""
    result: dict[int, str] = {}
    for m in json.loads(os.getenv("MAPPINGS_CONFIG", "[]")):
        dest = m.get("dest_channel")
        sid = m.get("sheets_id")
        if dest and sid:
            result[dest] = sid
    return result


def _build_user_send_channels() -> set[int]:
    """dest_channels forwarded as the user (send_as_user=True). Their messages
    are sent by the Telethon userbot, NOT the bot, so the Bot API cannot edit
    them ("message can't be edited"). This daemon is Zero-Telethon, so it leaves
    these channels entirely to the tracker, which has a Telethon edit fallback
    (`_user_edit_message`). Grading/editing/marking them here only fails the edit
    and — worse — marks them broadcasted, which blocks the tracker from applying
    the emoji. (All current send_as_user channels have no broadcast target.)"""
    result: set[int] = set()
    for m in json.loads(os.getenv("MAPPINGS_CONFIG", "[]")):
        if m.get("send_as_user") and m.get("dest_channel"):
            result.add(m["dest_channel"])
    return result


# ─── ESPN cache with TTL ─────────────────────────────────────────────────────

class _ESPNCache:
    """In-memory ESPN scoreboard cache with per-key TTL."""

    def __init__(self, ttl: int = ESPN_CACHE_TTL):
        self._ttl = ttl
        self._data: dict[tuple, tuple[float, list]] = {}  # key → (fetched_at, data)

    async def get(self, sport: str, date_str: str) -> list:
        key = (sport, date_str)
        cached = self._data.get(key)
        if cached and (time.monotonic() - cached[0]) < self._ttl:
            return cached[1]
        data = await fetch_espn(sport, date_str)
        self._data[key] = (time.monotonic(), data)
        return data

    def clear(self):
        self._data.clear()


# ─── Main loop ───────────────────────────────────────────────────────────────

def _parlay_broadcast_legs(picks, leg_verdicts, odds_by_pick, default_sport):
    """Every leg of a parlay with its current verdict + odds, in message order.

    A parlay is one ticket, so its broadcast must show all legs and the combined
    price — even legs that aren't individually resolved (e.g. a leg voided when a
    sibling already lost). Resolved legs carry their WIN/LOSS/PUSH verdict; the
    rest carry PENDING so broadcast_results lists + prices them without counting
    them toward the settled result.
    """
    out = []
    for i, pick in enumerate(picks):
        lv = leg_verdicts.get(str(i)) or {}
        if not pick.get("sport"):
            pick["sport"] = lv.get("sport", default_sport)
        out.append((pick, lv.get("verdict", "PENDING"), odds_by_pick.get(str(i), {}).get("odds")))
    return out


async def _grade_cycle(
    bot_token: str,
    audit: AuditLog,
    espn_cache: _ESPNCache,
    broadcast_map: dict[int, int],
    sheets_map: dict[int, str],
    user_send_channels: set[int],
) -> tuple[int, int]:
    """Run one grading cycle.  Returns (graded_count, pending_count)."""
    cache = _load_pending_cache()
    graded_count = 0
    pending_count = 0
    dirty = False

    for cache_key, entry in list(cache.items()):
        if not isinstance(entry, dict) or "parsed" not in entry:
            continue
        # Skip dupes, failures
        if entry.get("_dupe") or entry.get("_failed"):
            continue
        # Skip send_as_user channels — their messages aren't bot-editable, so
        # the tracker (with its Telethon fallback) owns them. See
        # _build_user_send_channels for why touching them here breaks grading.
        try:
            _ch = int(cache_key.split(":")[0])
        except (ValueError, IndexError):
            _ch = 0
        if _ch in user_send_channels:
            continue

        parsed = entry["parsed"]
        picks = parsed.get("picks", [])
        if not picks:
            continue
        sport = parsed.get("sport", "Other")
        leg_verdicts = entry.get("leg_verdicts", {})
        odds_by_pick = entry.get("odds_by_pick", {})
        html_text = entry.get("html_text")
        has_media = entry.get("has_media", False)
        msg_date = entry.get("msg_date", "")
        if not msg_date:
            continue  # pre-daemon cache entry — tracker will handle it
        capper = entry.get("capper_name", "")
        reply_to_id = entry.get("reply_to_id")  # pre-cached by tracker for threaded broadcasts

        # A parlay is decided the instant any leg loses — the remaining legs
        # are moot. Treat them as needing no grading so the parlay settles now
        # (routes through the all-resolved broadcast path below) instead of
        # waiting forever on a pending sibling. Also stops a later-resolving
        # pending leg from broadcasting a second, redundant result.
        is_parlay_entry = any(p.get("is_parlay_leg") for p in picks)
        parlay_lost = is_parlay_entry and any(
            leg_verdicts.get(str(i), {}).get("verdict") == "LOSS"
            for i in range(len(picks))
        )

        # Figure out which legs still need grading
        unresolved_indices = []
        for i in range(len(picks)):
            leg = leg_verdicts.get(str(i))
            if not leg or leg.get("verdict") not in ("WIN", "LOSS", "PUSH"):
                if parlay_lost:
                    continue  # parlay already lost — pending leg is moot
                unresolved_indices.append(i)

        if not unresolved_indices:
            # A parlay settled on a lost leg: its still-pending legs are moot and
            # will never resolve or broadcast. Mark them VOID + broadcasted so
            # every leg counts as broadcast — otherwise the tracker's
            # fully-broadcast skip never fires and it re-records the dead parlay
            # to the audit channel every run.
            if parlay_lost:
                for i in range(len(picks)):
                    lv = leg_verdicts.get(str(i))
                    if (not lv or lv.get("verdict") not in ("WIN", "LOSS", "PUSH")) \
                            and not (lv and lv.get("broadcasted")):
                        leg_verdicts[str(i)] = {
                            "verdict": "VOID", "calc": "",
                            "sport": picks[i].get("sport") or sport,
                            "game_date": msg_date, "broadcasted": True,
                        }
                        entry["leg_verdicts"] = leg_verdicts
                        dirty = True

            # ── Broadcast picks graded by tracker but not yet broadcast ────
            unbroadcast = [
                i for i in range(len(picks))
                if leg_verdicts.get(str(i), {}).get("verdict") in ("WIN", "LOSS", "PUSH")
                and not leg_verdicts.get(str(i), {}).get("broadcasted")
            ]
            if unbroadcast:
                channel_id = int(cache_key.split(":")[0])
                msg_id = int(cache_key.split(":")[1])

                # Retry emoji edit if not already on the message
                if html_text and not any(ch in html_text for ch in _PICK_EMOJI.values()):
                    all_v = []
                    for i in range(len(picks)):
                        lv2 = leg_verdicts.get(str(i))
                        if lv2 and lv2.get("verdict") in ("WIN", "LOSS", "PUSH"):
                            all_v.append((picks[i], lv2["verdict"], lv2.get("calc", ""), lv2.get("sport", sport)))
                        else:
                            all_v.append((picks[i], "PENDING", "", picks[i].get("sport") or sport))
                    new_text = _insert_emojis(html_text, all_v)
                    if new_text != html_text:
                        ok = await _bot_edit_message(bot_token, channel_id, msg_id, new_text, has_media)
                        if ok:
                            entry["html_text"] = new_text
                            await asyncio.sleep(0.5)
                            for linked_id in entry.get("linked_message_ids", []):
                                await _bot_edit_message(bot_token, channel_id, linked_id, new_text, has_media)
                                await asyncio.sleep(0.5)

                nr_pick_results = []
                for i in unbroadcast:
                    pick = picks[i]
                    lv = leg_verdicts[str(i)]
                    if not pick.get("sport"):
                        pick["sport"] = lv.get("sport", sport)
                    nr_pick_results.append((pick, lv["verdict"], odds_by_pick.get(str(i), {}).get("odds")))
                # A parlay broadcasts as one ticket — send all legs so the result
                # shows every leg and the combined price, not just the settled one.
                bc_results = (
                    _parlay_broadcast_legs(picks, leg_verdicts, odds_by_pick, sport)
                    if any(p.get("is_parlay_leg") for p in picks) else nr_pick_results
                )
                await audit.broadcast_results(
                    channel_id=channel_id,
                    message_id=msg_id,
                    pick_results=bc_results,
                    capper_name=capper,
                    reply_to_id=reply_to_id,
                )
                if channel_id in sheets_map:
                    try:
                        await append_pick_rows(
                            pick_results=nr_pick_results,
                            date_str=msg_date,
                            raw_text=html_text or "",
                            sheets_id=sheets_map[channel_id],
                        )
                    except Exception as exc:
                        print(f"  [sheets] warn: {exc}")
                for i in unbroadcast:
                    leg_verdicts[str(i)]["broadcasted"] = True
                entry["leg_verdicts"] = leg_verdicts
                dirty = True
                # Persist immediately so a mid-cycle abort/restart can never
                # re-broadcast this result (broadcast is not idempotent).
                _save_pending_cache(cache)
                graded_count += len(unbroadcast)
                for i in unbroadcast:
                    pick = picks[i]
                    emoji = VERDICT_EMOJI.get(leg_verdicts[str(i)]["verdict"], "")
                    desc = pick.get("description", "")[:40]
                    print(f"  {emoji} {cache_key} {capper[:15]:<15} {desc} (broadcast-only)")
            continue

        # Check if all resolved legs are already broadcast
        already_broadcast = {
            int(k) for k, v in leg_verdicts.items()
            if isinstance(v, dict) and v.get("broadcasted")
        }

        # Grade unresolved legs
        summary_cache: dict = {}
        newly_resolved = []  # (index, pick, verdict, calc, pick_sport, game_date)
        has_espn_error = False

        for i in unresolved_indices:
            pick = picks[i]
            pick_sport = pick.get("sport") or sport
            odds_gd = odds_by_pick.get(str(i), {}).get("game_date")
            eff_date = odds_gd if (odds_gd and odds_gd != msg_date and
                                   abs((_date.fromisoformat(odds_gd) - _date.fromisoformat(msg_date)).days) <= 2) else msg_date

            sb = await espn_cache.get(pick_sport, eff_date)

            # Early grade: totals where score already exceeds the line
            early = try_early_grade_math(pick_sport, pick, sb)
            if early:
                verdict, calc = early
                game_date = eff_date
            else:
                early_ctx = build_early_context(pick_sport, pick, sb)
                if early_ctx:
                    context, game_date = early_ctx, eff_date
                else:
                    context, game_date = await build_context(
                        pick_sport, eff_date, pick, sb, summary_cache,
                        odds_game_date=odds_gd,
                        msg_date=msg_date,
                    )

                if context in (CONTEXT_ESPN_ERROR, CONTEXT_PENDING):
                    if context == CONTEXT_ESPN_ERROR:
                        has_espn_error = True
                    verdict, calc = "PENDING", ""
                elif context == CONTEXT_SKIP:
                    verdict, calc = "UNKNOWN", ""
                else:
                    verdict, calc = await claude_grade(
                        pick.get("description", ""), msg_date, context,
                        pick.get("bet_type", ""),
                        pick.get("prop_stat") or "",
                    )

            if verdict in ("WIN", "LOSS", "PUSH"):
                leg_verdicts[str(i)] = {
                    "verdict": verdict, "calc": calc,
                    "sport": pick_sport, "game_date": game_date or eff_date,
                }
                newly_resolved.append((i, pick, verdict, calc, pick_sport, game_date))

        if not newly_resolved:
            if unresolved_indices:
                pending_count += 1
            continue

        # ── Determine if we should edit now ────────────────────────────────
        all_verdicts = []
        for i, pick in enumerate(picks):
            leg = leg_verdicts.get(str(i))
            if leg and leg.get("verdict") in ("WIN", "LOSS", "PUSH"):
                all_verdicts.append((pick, leg["verdict"], leg["calc"], leg.get("sport", sport), leg.get("game_date", msg_date)))
            else:
                all_verdicts.append((pick, "PENDING", "", pick.get("sport") or sport, msg_date))

        is_parlay = any(p.get("is_parlay_leg") for p in picks)
        overall = _overall_verdict(all_verdicts)
        parlay_pending = is_parlay and overall == "PENDING"

        # For parlays, only edit when all legs resolved (or a LOSS settles it)
        newly_resolved_non_parlay = [
            (i, p, v, c, ps, gd) for i, p, v, c, ps, gd in newly_resolved
            if not p.get("is_parlay_leg")
        ]
        parlay_blocks_edit = parlay_pending and not newly_resolved_non_parlay

        if parlay_blocks_edit:
            # Save resolved legs but don't edit/broadcast yet
            entry["leg_verdicts"] = leg_verdicts
            dirty = True
            pending_count += 1
            continue

        # ── Edit message via Bot API ──────────────────────────────────────
        channel_id = int(cache_key.split(":")[0])
        msg_id = int(cache_key.split(":")[1])

        edit_failed = False
        if html_text:
            # Build emoji verdicts for picks not already broadcast
            emoji_verdicts = [
                v for j, v in enumerate(all_verdicts)
                if j not in already_broadcast
            ]
            new_text = _insert_emojis(html_text, emoji_verdicts)

            if new_text != html_text:
                ok = await _bot_edit_message(bot_token, channel_id, msg_id, new_text, has_media)
                if ok:
                    await asyncio.sleep(0.5)
                    # Edit linked duplicates
                    for linked_id in entry.get("linked_message_ids", []):
                        await _bot_edit_message(bot_token, channel_id, linked_id, new_text, has_media)
                        await asyncio.sleep(0.5)
                    entry["html_text"] = new_text
                else:
                    edit_failed = True
                    print(f"  [grade_daemon] edit failed {cache_key}")
            elif not any(ch in html_text for ch in _PICK_EMOJI.values()):
                # Unchanged AND no emoji anywhere: _insert_emojis genuinely found
                # no line to mark — a real failure. Unchanged WITH an emoji already
                # present just means someone (the tracker) got here first, which is
                # the desired end state, not a failure.
                edit_failed = True
                print(f"  [grade_daemon] emoji insert failed (no line match) {cache_key}")

        # ── Mark legs as broadcasted ──────────────────────────────────────
        for i, pick, verdict, calc, ps, gd in newly_resolved:
            leg_verdicts[str(i)] = {
                "verdict": verdict, "calc": calc,
                "sport": ps, "game_date": gd or msg_date,
                "broadcasted": not edit_failed,
            }
        entry["leg_verdicts"] = leg_verdicts

        graded_count += len(newly_resolved)
        dirty = True
        # Persist immediately so a mid-cycle abort/restart can never re-broadcast
        # this result (broadcast is not idempotent).
        _save_pending_cache(cache)

        # Pretty print
        for i, pick, verdict, calc, ps, gd in newly_resolved:
            emoji = VERDICT_EMOJI.get(verdict, "")
            desc = pick.get("description", "")[:40]
            print(f"  {emoji} {cache_key} {capper[:15]:<15} {desc} ({calc})")

        # ── Broadcast results ─────────────────────────────────────────────
        if not edit_failed:
            nr_pick_results = []
            for i, pick, verdict, calc, ps, gd in newly_resolved:
                if not pick.get("sport"):
                    pick["sport"] = ps
                nr_pick_results.append((pick, verdict, odds_by_pick.get(str(i), {}).get("odds")))

            # A parlay broadcasts as one ticket — send all legs so the result
            # shows every leg and the combined price, not just the settled one.
            bc_results = (
                _parlay_broadcast_legs(picks, leg_verdicts, odds_by_pick, sport)
                if any(p.get("is_parlay_leg") for p in picks) else nr_pick_results
            )
            await audit.broadcast_results(
                channel_id=channel_id,
                message_id=msg_id,
                pick_results=bc_results,
                capper_name=capper,
                reply_to_id=reply_to_id,
            )

            # ── Google Sheets ─────────────────────────────────────────────
            if channel_id in sheets_map:
                try:
                    await append_pick_rows(
                        pick_results=nr_pick_results,
                        date_str=msg_date,
                        raw_text=html_text or "",
                        sheets_id=sheets_map[channel_id],
                    )
                except Exception as exc:
                    print(f"  [sheets] warn: {exc}")

        # ── Audit DB ─────────────────────────────────────────────────────
        all_descs = "\n".join(
            f"{v}: {pick.get('description', '')}|{ps}|{gd}|{calc}"
            for i, pick, v, calc, ps, gd in newly_resolved
        )
        first_pick, first_v, first_calc, first_sport, first_gd = newly_resolved[0][1:]
        first_odds = odds_by_pick.get("0", {})
        await audit.record(
            channel_id=channel_id,
            message_id=msg_id,
            date=msg_date,
            sport=first_sport,
            pick_desc=all_descs or first_pick.get("description", ""),
            bet_type=first_pick.get("bet_type", ""),
            verdict=overall if not parlay_pending else first_v,
            calc=first_calc,
            prev_caption="",
            new_caption="",
            dry_run=False,
            channel_name="",
            capper_name=capper,
            odds=first_odds.get("odds"),
            odds_bookmaker=first_odds.get("bookmaker"),
            odds_match_type=first_odds.get("match_type"),
        )

    if dirty:
        _save_pending_cache(cache)

    return graded_count, pending_count


async def run_daemon() -> None:
    bot_token = os.getenv("BOT_TOKEN", "")
    if not bot_token:
        print("ERROR: BOT_TOKEN not set")
        sys.exit(1)

    broadcast_map = _build_broadcast_map()
    sheets_map = _build_sheets_map()
    user_send_channels = _build_user_send_channels()
    audit = AuditLog(broadcast_results_mappings=broadcast_map)
    espn_cache = _ESPNCache(ttl=ESPN_CACHE_TTL)

    print(f"grade_daemon started (interval={LOOP_INTERVAL}s, espn_ttl={ESPN_CACHE_TTL}s, "
          f"cycle_timeout={CYCLE_TIMEOUT}s)")

    cache_mtime: float = 0
    cycle = 0

    while True:
        cycle += 1
        # Feed the systemd watchdog every iteration (~LOOP_INTERVAL). If the
        # process ever wedges so hard the loop stops turning, systemd's
        # WatchdogSec restarts it. No-op when not run under systemd.
        _sd_notify("WATCHDOG=1")
        try:
            # Only run if cache file changed (or every 6th cycle as safety net)
            try:
                new_mtime = os.path.getmtime(_CACHE_PATH)
            except FileNotFoundError:
                await asyncio.sleep(LOOP_INTERVAL)
                continue

            if new_mtime == cache_mtime and cycle % 6 != 0:
                await asyncio.sleep(LOOP_INTERVAL)
                continue
            cache_mtime = new_mtime

            try:
                graded, pending = await asyncio.wait_for(
                    _grade_cycle(bot_token, audit, espn_cache, broadcast_map, sheets_map,
                                 user_send_channels),
                    timeout=CYCLE_TIMEOUT,
                )
            except asyncio.TimeoutError:
                # A network call hung past CYCLE_TIMEOUT. The cycle is cancelled
                # (already-broadcast results were persisted incrementally, so no
                # double-post). Force a re-process next cycle and carry on.
                print(f"[cycle {cycle}] ⚠ grade cycle exceeded {CYCLE_TIMEOUT}s — aborted, retrying next cycle")
                cache_mtime = 0
                await asyncio.sleep(LOOP_INTERVAL)
                continue

            if graded:
                cost = usage_cost()
                print(f"[cycle {cycle}] graded={graded} pending={pending} cost={fmt_cost(cost)}")

        except KeyboardInterrupt:
            print("\ngrade_daemon stopped")
            break
        except Exception:
            traceback.print_exc()
            # Don't exit — sleep and retry next cycle

        await asyncio.sleep(LOOP_INTERVAL)


def main():
    asyncio.run(run_daemon())


if __name__ == "__main__":
    main()
