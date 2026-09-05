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
import re
import socket
import sys
import time
import traceback

from datetime import date as _date, timedelta
from dotenv import load_dotenv

load_dotenv()
load_dotenv(".env.local", override=True)

from common import (
    VERDICT_EMOJI,
    UNKNOWN_MAX_ATTEMPTS,
    parlay_combined_odds,
    record_unknown_attempt,
    should_skip_unknown,
)
from scores import (
    fetch_espn,
    try_early_grade_math,
    fetch_cfl_scoreboard,
    build_early_context,
    _find_event_for_pick,
)
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
from tracker_format import (
    _insert_emojis,
    _bot_edit_message,
    _bot_edit_message_status,
    _PICK_EMOJI,
)
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
# How many results on the same game must land in one cycle before they are merged
# into a single broadcast. 2 = merge as soon as there is anything to merge; raise it
# to keep the per-capper look until the spam is worse. The floor stays 2 because this
# knob is about *merging* — a lone result still renders in the same score-header
# format whenever its final score resolved (see _flush_broadcasts).
GROUP_MIN = max(2, int(os.getenv("GRADE_DAEMON_GROUP_MIN", "2")))
# An entry only settles once EVERY leg has a verdict, so one leg that can never
# resolve (a garbled parse with no findable game — it context-skips for free and
# so never even trips the ungradeable-attempt cap) pins the whole entry open:
# its resolved siblings stay broadcasted=False, and the tracker re-records them
# to the audit channel every single run until the 14-day eviction. Bound it —
# once every date the entry could still be waiting on is this far past, nothing
# is going to resolve and the entry is retired instead of churning.
STALE_UNRESOLVED_DAYS = int(os.getenv("GRADE_STALE_UNRESOLVED_DAYS", "3"))

_CACHE_PATH = os.path.join(os.path.dirname(__file__), "parse_cache.json")

_SPORT_EMOJI = {
    "MLB": "⚾️", "KBO": "⚾️",
    "NBA": "🏀", "WNBA": "🏀", "NCAAB": "🏀", "CBB": "🏀",
    "NFL": "🏈", "NCAAF": "🏈", "CFB": "🏈", "CFL": "🏈", "UFL": "🏈",
    "NHL": "🏒",
    "Soccer": "⚽️",
    "UFC": "🥊", "Boxing": "🥊",
    "Tennis": "🎾", "Golf": "⛳️", "Lacrosse": "🥍",
}


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


def _retire_deleted(entry: dict, cache: dict, cache_key: str) -> None:
    """Permanently retire a cache entry whose pick message has been deleted.

    `_failed` is the existing terminal "stop touching this entry" flag honoured
    by both this daemon and the tracker, and `_pending_entry` already preserves
    it across the cache rebuild — so the stop survives the next tracker pass
    instead of lasting one write cycle.

    Persists immediately: the reason we are here is that a result was about to
    be broadcast, and a broadcast is not idempotent, so the flag has to outlive
    a mid-cycle abort or restart.
    """
    entry["_failed"] = True
    entry["_failed_reason"] = "message deleted"
    cache[cache_key] = entry
    _save_pending_cache(cache)
    print(f"  ⏹ {cache_key} retired — pick message deleted (no broadcast)")


def _stale_reference_date(leg_verdicts: dict, odds_by_pick: dict, msg_date: str) -> str:
    """Latest date this entry could still legitimately be waiting on.

    Ages off the GAME, not the post: a pick posted Monday for Saturday's game is
    unresolved for five days by design, and aging it off the post date would
    retire it before it ever had a chance to grade. Any game date we know about —
    from a graded sibling leg or from the odds match — pushes the horizon out.
    A leg we could never even find a game for contributes nothing, which is the
    point: it falls back to the post date and is exactly the leg worth retiring.
    """
    dates = [d for d in [msg_date] if d]
    for src in (leg_verdicts, odds_by_pick):
        for v in (src or {}).values():
            if isinstance(v, dict):
                gd = v.get("game_date")
                if isinstance(gd, str) and len(gd) == 10:
                    dates.append(gd)
    return max(dates) if dates else msg_date


async def _retire_stale(
    entry: dict, cache: dict, cache_key: str, audit: AuditLog,
    unresolved: list[int], picks: list[dict], days: int,
) -> None:
    """Retire an entry whose unresolved legs are never going to resolve.

    Same terminal flag as `_retire_deleted` (honoured by this daemon and the
    tracker, preserved by `_pending_entry` across the cache rebuild) and the same
    no-broadcast rule: we could not grade every leg, so publishing a result off
    the legs that did grade would be asserting an outcome we never verified.
    Notifies audit ONCE — a pick dying quietly is exactly what nobody notices.
    """
    descs = ", ".join((picks[i].get("description") or f"leg {i}")[:40] for i in unresolved)
    reason = f"unresolvable after {days}d — {descs}"
    entry["_failed"] = True
    entry["_failed_reason"] = reason
    cache[cache_key] = entry
    _save_pending_cache(cache)
    print(f"  ⏹ {cache_key} retired — {reason} (no broadcast)")
    await audit.warn(f"⏹ <code>{cache_key}</code> retired — {reason} (no broadcast)")


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


# ─── Same-game broadcast grouping ────────────────────────────────────────────

def _norm_team(s: str) -> str:
    return re.sub(r"[^a-z0-9]", "", (s or "").lower())


# Market sides that name a direction rather than a team. betonline_sides holds these
# for totals/props, so they must never be mistaken for a matchup — "Over/Under" would
# key every total in a sport on one date as if it were the same game.
_DIRECTIONAL_LABELS = {"over", "under", "yes", "no", "draw", "tie"}


def _matchup_labels(pick: dict, odds_entry: dict) -> list[str]:
    """Team names identifying this pick's game, best-effort.

    `betonline_sides` names both sides of the market, and for a moneyline or spread
    that is the only place the *opponent* appears — the parse records just the side
    that was bet. For a total those same fields hold Over/Under, so directional labels
    are discarded in favour of the parsed teams.
    """
    sides = odds_entry.get("betonline_sides") or {}
    side_labels = [s for s in (sides.get("pick_label"), sides.get("opp_label")) if s]
    if side_labels and not any(_norm_team(s) in _DIRECTIONAL_LABELS for s in side_labels):
        return side_labels
    return [t for t in (pick.get("teams") or []) if t]


def _game_key(sport: str, game_date: str, labels: list[str]) -> tuple | None:
    """Identity of the game a pick is on, or None when it can't be determined.

    None is the safe answer: it means "never group this", i.e. exactly the
    one-message-per-pick behaviour that predates grouping.
    """
    if not sport or not game_date:
        return None
    norm = sorted({_norm_team(t) for t in labels if _norm_team(t)})
    if not norm:
        return None
    return (sport, game_date, tuple(norm))


def _final_score_text(event: dict) -> str | None:
    """'Yankees 2–5 Cubs' for a COMPLETED game, else None.

    Gated on `completed` because a pick can settle before the game does (an over that
    already cleared, a lost parlay leg) — printing the running score as the result
    line would be wrong.
    """
    comp = (event.get("competitions") or [{}])[0]
    if not ((comp.get("status") or {}).get("type") or {}).get("completed"):
        return None
    sides = {c.get("homeAway"): c for c in comp.get("competitors", [])}
    away, home = sides.get("away"), sides.get("home")
    if not away or not home:
        return None

    def _name(c: dict) -> str:
        t = c.get("team") or {}
        return t.get("shortDisplayName") or t.get("displayName") or t.get("abbreviation") or ""

    try:
        return f"{_name(away)} {int(away['score'])}–{int(home['score'])} {_name(home)}"
    except (KeyError, TypeError, ValueError):
        return None


async def _resolve_game_keys(pending: list[dict], espn_cache: _ESPNCache) -> None:
    """Upgrade each queued result's game key to the ESPN event id where one matches.

    A team-name key only merges picks that *name* the same teams, so a moneyline
    ("Chicago Cubs") and a total ("Cubs / Yankees o8.5") on one game would never meet
    — the parse records only the bet side for the first and both teams for the second.
    The event id is the game's real identity: it merges those two and cannot merge two
    different games. Falls back to the name key whenever ESPN has no event for the pick
    (sport it doesn't cover, offseason, no match), which is conservative rather than
    wrong. The scoreboard is the same one grading just fetched, so this is normally a
    cache hit, and the event is kept for the header.
    """
    for item in pending:
        if not item["game_key"] or not item["matchup"]:
            continue
        sport, game_date, _ = item["game_key"]
        try:
            sb = await espn_cache.get(sport, game_date)
            event = _find_event_for_pick(sb, item["matchup"]) if sb else None
        except Exception as exc:
            print(f"  [group] event lookup failed ({sport} {game_date}): {exc}")
            continue
        if event and event.get("id"):
            item["game_key"] = (sport, game_date, f"espn:{event['id']}")
            item["event"] = event


def _group_header(group: list[dict]) -> str:
    """Game label for a merged broadcast — '⚾️ Yankees 2–5 Cubs', '… · 7/31' if stale.

    Degrades to the ESPN matchup, then the pick's own team labels, then the sport,
    when there is no final score: the header is context, never a reason to hold up
    or drop a result.
    """
    sport, game_date, _ = group[0]["game_key"]
    # The date earns its place only when the game isn't today's — a pick graded late,
    # or one game of a series against the same opponent where the matchup alone would
    # be ambiguous. Results post within hours of the final almost every time, and
    # those don't need to be told what day it is.
    date_tag = ""
    if game_date and game_date != _date.today().isoformat():
        try:
            d = _date.fromisoformat(game_date)
            date_tag = f"· {d.month}/{d.day}"
        except ValueError:
            date_tag = f"· {game_date}"

    event = next((it["event"] for it in group if it.get("event")), None)
    matchup = ""
    if event:
        matchup = _final_score_text(event) or event.get("shortName") or ""
    if not matchup:
        labels = group[0]["matchup"]
        matchup = " vs ".join(labels) if len(labels) == 2 else (labels[0] if labels else sport)

    return " ".join(p for p in (_SPORT_EMOJI.get(sport, ""), matchup, date_tag) if p)


def _queue_broadcast(
    pending: list[dict], *, cache_key: str, channel_id: int, message_id: int,
    capper: str, reply_to_id: int | None, bc_results: list, sheets_results: list,
    leg_indices: list[int], mark_all_resolved: bool, html_text: str | None,
    msg_date: str, sport: str, picks: list, leg_verdicts: dict, odds_by_pick: dict,
) -> None:
    """Queue one message's result for the end-of-cycle flush.

    Only a lone straight pick gets a game key: a parlay spans several games, and a
    multi-pick message would have to be split across group messages, so both stay on
    the one-message-per-pick path.
    """
    game_key = None
    matchup: list[str] = []
    if (len(bc_results) == 1 and len(leg_indices) == 1
            and not any(p.get("is_parlay_leg") for p in picks)):
        i = leg_indices[0]
        pick = picks[i] if i < len(picks) else bc_results[0][0]
        lv = leg_verdicts.get(str(i)) or {}
        matchup = _matchup_labels(pick, odds_by_pick.get(str(i)) or {})
        game_key = _game_key(
            lv.get("sport") or pick.get("sport") or sport,
            lv.get("game_date") or msg_date,
            matchup,
        )

    pending.append({
        "cache_key": cache_key, "channel_id": channel_id, "message_id": message_id,
        "capper": capper, "reply_to_id": reply_to_id,
        "bc_results": bc_results, "sheets_results": sheets_results,
        "leg_indices": leg_indices, "mark_all_resolved": mark_all_resolved,
        "html_text": html_text, "msg_date": msg_date,
        "game_key": game_key, "matchup": matchup, "event": None,
    })


def _mark_broadcasted(cache: dict, item: dict) -> None:
    """Flip this message's resolved legs to broadcasted=True in the live cache dict."""
    entry = cache.get(item["cache_key"])
    if not isinstance(entry, dict):
        return  # evicted mid-cycle — nothing left to mark
    leg_verdicts = entry.get("leg_verdicts") or {}
    for i in item["leg_indices"]:
        leg = leg_verdicts.get(str(i))
        if isinstance(leg, dict):
            leg["broadcasted"] = True
    # A parlay broadcasts as ONE ticket covering every leg, so mark every resolved
    # leg — not just the ones resolved this cycle. Otherwise an already-resolved
    # sibling (e.g. an over that hit mid-game and was saved WIN/broadcasted=False
    # while the parlay waited on its other leg) is left unbroadcast and the next
    # cycle's broadcast-only path re-posts the identical ticket.
    if item["mark_all_resolved"]:
        for leg in leg_verdicts.values():
            if isinstance(leg, dict) and leg.get("verdict") in ("WIN", "LOSS", "PUSH"):
                leg["broadcasted"] = True
    entry["leg_verdicts"] = leg_verdicts


async def _flush_broadcasts(
    pending: list[dict], audit: AuditLog, cache: dict,
    sheets_map: dict[int, str], espn_cache: _ESPNCache,
) -> None:
    """Send every result queued this cycle, merging the ones on the same game.

    Results that land in the same cycle on the same game go out as ONE message
    instead of one per capper — three cappers on Cubs ML used to fire three
    near-identical messages and three notifications. A lone result also renders
    through the group format once its final score is in hand, so every scored
    single carries the "⚾️ Marlins 1–6 Cubs" header; the score is the whole point
    of that header, so a lone result *without* one (mid-game settle, non-ESPN
    sport, no event match) keeps the compact per-pick line. Anything without a
    resolvable game key, plus parlays and multi-pick messages, always falls
    through to the unchanged per-message path.

    Ordering matches the pre-grouping code: mark broadcasted and persist BEFORE
    sending, so a crash mid-flush drops a result rather than double-posting one
    (a broadcast is not idempotent; a missed one is recoverable, a duplicate is not).

    What deferring the send does change is that a graded leg now sits at
    broadcasted=False until the flush instead of flipping to True in the same breath
    as its verdict. A tracker pass landing inside that window sees the message as
    not-yet-broadcast and re-records it to the audit channel — one duplicate line in a
    private ops channel. That is deliberately preferred over the alternative ordering
    (mark at grade time), where a cycle abort between grading and the flush would lose
    the result broadcast outright, which is public and unrecoverable.
    """
    if not pending:
        return

    await _resolve_game_keys(pending, espn_cache)

    buckets: dict[tuple, list[dict]] = {}
    for idx, item in enumerate(pending):
        target = audit.broadcast_results_mappings.get(item["channel_id"])
        # Group per target channel: one source fans out to several dest channels,
        # each with its own broadcast channel, and those must not be merged.
        key = ("game", target, item["game_key"]) if (target and item["game_key"]) else ("solo", idx)
        buckets.setdefault(key, []).append(item)

    for key, group in buckets.items():
        # Message order, so the rendering is deterministic and the reply anchors to
        # the earliest pick of the group.
        group.sort(key=lambda it: (it["channel_id"], it["message_id"]))
        for item in group:
            _mark_broadcasted(cache, item)
        _save_pending_cache(cache)

        # A lone result takes the same header format only when the final score is
        # known — a "Team vs Team" header over a single line adds nothing, so the
        # degraded-header fallbacks stay reserved for actual merges.
        single_with_score = (
            len(group) == 1
            and group[0].get("event") is not None
            and _final_score_text(group[0]["event"]) is not None
        )
        if key[0] == "game" and (len(group) >= GROUP_MIN or single_with_score):
            await audit.broadcast_group(
                target_channel=key[1],
                header=_group_header(group),
                items=[{
                    "channel_id": it["channel_id"], "message_id": it["message_id"],
                    "capper": it["capper"], "pick": it["bc_results"][0][0],
                    "verdict": it["bc_results"][0][1], "odds": it["bc_results"][0][2],
                } for it in group],
                reply_to_id=next((it["reply_to_id"] for it in group if it["reply_to_id"]), None),
            )
            ident = key[2][2]
            ident_s = "/".join(ident) if isinstance(ident, tuple) else ident
            if len(group) > 1:
                print(f"  ⊞ merged {len(group)} results on {key[2][0]} {ident_s} "
                      f"into one broadcast")
            else:
                print(f"  ⊞ broadcast with final-score header on {key[2][0]} {ident_s}")
        else:
            for it in group:
                await audit.broadcast_results(
                    channel_id=it["channel_id"], message_id=it["message_id"],
                    pick_results=it["bc_results"], capper_name=it["capper"],
                    reply_to_id=it["reply_to_id"],
                )

        for it in group:
            if it["channel_id"] in sheets_map and it["sheets_results"]:
                try:
                    await append_pick_rows(
                        pick_results=it["sheets_results"],
                        date_str=it["msg_date"],
                        raw_text=it["html_text"] or "",
                        sheets_id=sheets_map[it["channel_id"]],
                    )
                except Exception as exc:
                    print(f"  [sheets] warn: {exc}")


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
    # Results are queued here instead of being sent inline, so the flush at the end
    # of the cycle can see every result at once and merge the ones on the same game.
    pending_broadcasts: list[dict] = []

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

        # Nothing here will ever resolve — retire instead of re-grading forever.
        if unresolved_indices:
            try:
                stale_days = (_date.today() - _date.fromisoformat(
                    _stale_reference_date(leg_verdicts, odds_by_pick, msg_date))).days
            except ValueError:
                stale_days = 0     # unparseable date — leave the entry alone
            if stale_days > STALE_UNRESOLVED_DAYS:
                await _retire_stale(entry, cache, cache_key, audit,
                                    unresolved_indices, picks, stale_days)
                continue

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
                        ok, gone = await _bot_edit_message_status(
                            bot_token, channel_id, msg_id, new_text, has_media
                        )
                        if ok:
                            entry["html_text"] = new_text
                            await asyncio.sleep(0.5)
                            for linked_id in entry.get("linked_message_ids", []):
                                await _bot_edit_message(bot_token, channel_id, linked_id, new_text, has_media)
                                await asyncio.sleep(0.5)
                        elif gone:
                            # Message deleted — the bet was retracted. Retire it
                            # instead of broadcasting a result for it.
                            _retire_deleted(entry, cache, cache_key)
                            continue

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
                # Queued, not sent: the end-of-cycle flush marks these legs
                # broadcasted and persists BEFORE sending, so the "can never
                # re-broadcast after an abort" guarantee still holds — an abort
                # before the flush leaves them broadcasted=False and this same
                # path re-queues them next cycle.
                _queue_broadcast(
                    pending_broadcasts,
                    cache_key=cache_key, channel_id=channel_id, message_id=msg_id,
                    capper=capper, reply_to_id=reply_to_id,
                    bc_results=bc_results, sheets_results=nr_pick_results,
                    leg_indices=list(unbroadcast), mark_all_resolved=False,
                    html_text=html_text, msg_date=msg_date, sport=sport,
                    picks=picks, leg_verdicts=leg_verdicts, odds_by_pick=odds_by_pick,
                )
                entry["leg_verdicts"] = leg_verdicts
                dirty = True
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

            # Totals are arithmetic: settled outright at final (incl. PUSH), or
            # mid-game once the score has passed the line. CFL is not on the
            # ESPN scoreboard, so the math path needs its scraped card instead —
            # `sb` stays as-is, since validate_sport relies on it being empty.
            math_sb = await fetch_cfl_scoreboard(eff_date) if pick_sport == "CFL" else sb
            early = try_early_grade_math(pick_sport, pick, math_sb)
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
                    # Cap repeat grading of a leg Claude can't resolve. Without
                    # this an ungradeable leg is re-graded every cycle forever.
                    skip_unknown, why = should_skip_unknown(leg_verdicts.get(str(i)))
                    if skip_unknown:
                        verdict, calc = "PENDING", ""
                    else:
                        verdict, calc = await claude_grade(
                            pick.get("description", ""), msg_date, context,
                            pick.get("bet_type", ""),
                            pick.get("prop_stat") or "",
                        )
                        if verdict not in ("WIN", "LOSS", "PUSH"):
                            n = record_unknown_attempt(leg_verdicts, i)
                            if n:
                                entry["leg_verdicts"] = leg_verdicts
                                dirty = True
                                if n >= UNKNOWN_MAX_ATTEMPTS:
                                    print(f"  ⏹ {cache_key} leg {i} ungradeable after {n} "
                                          f"attempts — no longer grading "
                                          f"({pick.get('description', '')[:50]})")

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
                ok, gone = await _bot_edit_message_status(
                    bot_token, channel_id, msg_id, new_text, has_media
                )
                if ok:
                    await asyncio.sleep(0.5)
                    # Edit linked duplicates
                    for linked_id in entry.get("linked_message_ids", []):
                        await _bot_edit_message(bot_token, channel_id, linked_id, new_text, has_media)
                        await asyncio.sleep(0.5)
                    entry["html_text"] = new_text
                elif gone:
                    # The pick message was deleted, so this bet was retracted.
                    # Retire the entry before it can broadcast or record a result
                    # for a bet nobody made. Without this the legs stay
                    # broadcasted=False and the broadcast-only path below re-posts
                    # them on the very next cycle, resurrecting the result the
                    # failed edit was supposed to suppress.
                    _retire_deleted(entry, cache, cache_key)
                    continue
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

        # ── Record verdicts ───────────────────────────────────────────────
        # `broadcasted` stays False until the message actually goes out, which the
        # end-of-cycle flush does (it marks + persists before sending). An abort
        # between here and the flush therefore re-queues the broadcast next cycle
        # rather than losing it — and still can't duplicate one.
        for i, pick, verdict, calc, ps, gd in newly_resolved:
            leg_verdicts[str(i)] = {
                "verdict": verdict, "calc": calc,
                "sport": ps, "game_date": gd or msg_date,
                "broadcasted": False,
            }
        entry["leg_verdicts"] = leg_verdicts

        graded_count += len(newly_resolved)
        dirty = True
        # Persist the verdicts now: grading cost a Claude call and a mid-cycle abort
        # must not throw that away.
        _save_pending_cache(cache)

        # Pretty print
        for i, pick, verdict, calc, ps, gd in newly_resolved:
            emoji = VERDICT_EMOJI.get(verdict, "")
            desc = pick.get("description", "")[:40]
            print(f"  {emoji} {cache_key} {capper[:15]:<15} {desc} ({calc})")

        # ── Queue broadcast + Sheets for the end-of-cycle flush ───────────
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
                if is_parlay else nr_pick_results
            )
            _queue_broadcast(
                pending_broadcasts,
                cache_key=cache_key, channel_id=channel_id, message_id=msg_id,
                capper=capper, reply_to_id=reply_to_id,
                bc_results=bc_results, sheets_results=nr_pick_results,
                leg_indices=[i for i, *_ in newly_resolved],
                # Guard on `not parlay_pending` so a still-undecided parlay (mixed
                # with a straight pick that broadcasts now) isn't prematurely marked
                # and silently suppressed.
                mark_all_resolved=is_parlay and not parlay_pending,
                html_text=html_text, msg_date=msg_date, sport=sport,
                picks=picks, leg_verdicts=leg_verdicts, odds_by_pick=odds_by_pick,
            )

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

    await _flush_broadcasts(pending_broadcasts, audit, cache, sheets_map, espn_cache)

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
                # A network call hung past CYCLE_TIMEOUT. The cycle is cancelled.
                # Verdicts already graded were persisted incrementally, and results
                # are only marked broadcasted once they have actually been sent, so
                # an abort re-queues the unsent ones next cycle without duplicating
                # the sent ones. Force a re-process and carry on.
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
