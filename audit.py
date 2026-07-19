"""
audit.py — Audit log for the pick-tracker bot.

Every time the bot grades a pick (or would grade one in dry-run mode) it:
  1. Writes a row to a local SQLite database  (picks.db)
  2. Posts a summary to a private Telegram audit channel  (AUDIT_CHANNEL_ID)

The DB is the source of truth for "what has been graded" so the nightly cron
can skip already-processed messages.  The Telegram channel gives a human-readable
real-time stream of bot actions that can be reviewed on any device.

Usage (from other modules):
    from audit import AuditLog
    audit = AuditLog()          # opens/creates picks.db, reads env vars
    await audit.record(...)     # write DB row + post to Telegram
"""

import asyncio
import os
import re
import sqlite3
from datetime import datetime, timezone

import httpx

from common import VERDICT_EMOJI, is_regulation_ml, parlay_combined_odds


def _clean_desc(desc: str) -> str:
    """Fallback: strip odds and normalize wording from a raw description string."""
    desc = re.sub(r'\s*\([+-]?\d+\)', '', desc)          # (-125), (+110)
    desc = re.sub(r'\s+[+-]\d{3,4}$', '', desc)           # trailing +113 / -138
    desc = re.sub(r'\bMoneyline\b', 'ML', desc, re.IGNORECASE)
    desc = re.sub(r'\s+vs\s+\S.*$', '', desc)             # " vs Arkansas" on spread lines
    return desc.strip()


def _format_pick(pick: dict) -> str:
    """Build a standardized, odds-free pick description from structured Claude parse fields."""
    bet_type  = pick.get("bet_type", "")
    teams     = pick.get("teams") or []
    line      = pick.get("line")
    direction = pick.get("direction") or ""
    period    = pick.get("period") or "game"
    player    = pick.get("player") or ""
    prop_stat = pick.get("prop_stat") or ""

    sport     = pick.get("sport", "")
    # Baseball "1H" is conventionally called "F5" (first 5 innings)
    if period == "1h" and sport in ("MLB", "KBO"):
        period_tag = " F5"
    else:
        period_tag = f" {period.upper()}" if period and period != "game" else ""
    team = teams[0] if teams else ""

    if bet_type == "spread" and team and line is not None:
        sign = "+" if line > 0 else ""
        return f"{team}{period_tag} {sign}{line:g}"

    if bet_type == "double_chance" and team:
        return f"{team}{period_tag} DC"

    if bet_type == "draw_no_bet" and team:
        return f"{team}{period_tag} DNB"

    if bet_type == "moneyline" and team:
        desc_raw = pick.get("description", "")
        advance_m = re.search(r'\bto\s+(advance|qualify)\b', desc_raw, re.IGNORECASE)
        if advance_m:
            suffix = f" to {advance_m.group(1).title()}"
        elif is_regulation_ml(desc_raw):
            suffix = " 3-way ML"
        else:
            suffix = " ML"
        return f"{team}{period_tag}{suffix}"

    if bet_type in ("total", "team_total") and line is not None:
        d = "O" if direction == "over" else "U" if direction == "under" else ""
        if bet_type == "team_total" and team:
            prefix = f"{team} "
        elif len(teams) >= 2:
            prefix = f"{teams[0]}/{teams[1]} "
        elif team:
            prefix = f"{team} "
        else:
            prefix = ""
        stat = f" {prop_stat}" if prop_stat else ""
        return f"{prefix}{d}{line:g}{stat}"

    if bet_type == "prop" and player:
        if line is not None and direction:
            d = "O" if direction == "over" else "U"
            stat = f" {prop_stat}" if prop_stat else ""
            return f"{player} {d}{line:g}{stat}"
        return player

    # Team-level props (BTTS, clean sheet, etc.) — no player, but has teams + prop_stat
    if bet_type == "prop" and prop_stat and teams:
        matchup = " vs ".join(teams) if len(teams) >= 2 else teams[0]
        stat = prop_stat
        if prop_stat == "BTTS" and direction:
            stat += " Yes" if direction == "over" else " No"
        return f"{matchup}{period_tag} {stat}"

    # Fallback to cleaned description string
    return _clean_desc(pick.get("description", ""))

DB_PATH = os.path.join(os.path.dirname(__file__), "picks.db")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS grades (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    graded_at     TEXT    NOT NULL,          -- ISO-8601 UTC timestamp
    channel_id    INTEGER NOT NULL,          -- Telegram channel ID
    message_id    INTEGER NOT NULL,          -- Telegram message ID
    date          TEXT    NOT NULL,          -- Pick date  YYYY-MM-DD
    sport         TEXT,
    capper_name   TEXT,                      -- first line of message (capper handle)
    pick_desc     TEXT,
    bet_type      TEXT,
    verdict       TEXT    NOT NULL,          -- WIN / LOSS / PUSH / UNKNOWN / PENDING
    calc          TEXT,                      -- grader's arithmetic string
    prev_caption  TEXT,                      -- caption before edit (if any)
    new_caption   TEXT,                      -- caption after edit  (if any)
    dry_run       INTEGER NOT NULL DEFAULT 0 -- 1 = logged only, no Telegram edit
);

CREATE UNIQUE INDEX IF NOT EXISTS grades_msg
    ON grades (channel_id, message_id);
"""

_SCHEMA_API_COSTS = """
CREATE TABLE IF NOT EXISTS api_costs (
    id                   INTEGER PRIMARY KEY AUTOINCREMENT,
    logged_at            TEXT    NOT NULL,
    run_type             TEXT,
    claude_cost          REAL,
    odds_requests_used   INTEGER
);
"""

_MIGRATIONS = [
    "ALTER TABLE grades ADD COLUMN capper_name TEXT",
    "ALTER TABLE grades ADD COLUMN odds INTEGER",
    "ALTER TABLE grades ADD COLUMN odds_bookmaker TEXT",
    "ALTER TABLE grades ADD COLUMN odds_match_type TEXT",
]



class AuditLog:
    """
    Thin wrapper around the SQLite audit DB + Telegram audit channel.

    All public methods are async-safe (they offload SQLite to a thread
    via asyncio.to_thread so they won't block the event loop).
    """

    def __init__(
        self,
        db_path: str = DB_PATH,
        bot_token: str | None = None,
        audit_channel_id: str | int | None = None,
        broadcast_results_mappings: dict[int, int] | None = None,
    ):
        self.db_path = db_path
        self.bot_token = bot_token or os.getenv("BOT_TOKEN", "")
        raw_cid = audit_channel_id or os.getenv("AUDIT_CHANNEL_ID", "")
        self.audit_channel_id: int | None = int(raw_cid) if raw_cid else None
        self.broadcast_results_mappings: dict[int, int] = broadcast_results_mappings or {}
        self._init_db()

    # ── DB helpers ─────────────────────────────────────────────────────────────

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def _init_db(self) -> None:
        with self._connect() as conn:
            conn.executescript(_SCHEMA)
            conn.executescript(_SCHEMA_API_COSTS)
            for mig in _MIGRATIONS:
                try:
                    conn.execute(mig)
                    conn.commit()
                except Exception:
                    pass  # column already exists

    def _insert(self, row: dict) -> None:
        """Insert or replace a grade row (idempotent on channel_id+message_id).
        Opens its own connection because this runs in a background thread via asyncio.to_thread."""
        cols = ", ".join(row.keys())
        placeholders = ", ".join("?" for _ in row)
        sql = f"INSERT OR REPLACE INTO grades ({cols}) VALUES ({placeholders})"
        with self._connect() as conn:
            conn.execute(sql, list(row.values()))
            conn.commit()

    # ── Core record method ─────────────────────────────────────────────────────

    async def record(
        self,
        *,
        channel_id: int,
        message_id: int,
        date: str,
        sport: str,
        pick_desc: str,
        bet_type: str,
        verdict: str,
        calc: str,
        prev_caption: str = "",
        new_caption: str = "",
        dry_run: bool = False,
        channel_name: str = "",
        capper_name: str = "",
        odds: int | None = None,
        odds_bookmaker: str | None = None,
        odds_match_type: str | None = None,
        edit_failed: bool = False,
    ) -> None:
        """
        Write to DB and post an audit Telegram message.
        Safe to call from an async context — SQLite work runs in a thread.
        """
        row = {
            "graded_at":       datetime.now(timezone.utc).isoformat(),
            "channel_id":      channel_id,
            "message_id":      message_id,
            "date":            date,
            "sport":           sport,
            "capper_name":     capper_name,
            "pick_desc":       pick_desc,
            "bet_type":        bet_type,
            "verdict":         verdict,
            "calc":            calc or "",
            "prev_caption":    prev_caption,
            "new_caption":     new_caption,
            "dry_run":         1 if dry_run else 0,
            "odds":            odds,
            "odds_bookmaker":  odds_bookmaker,
            "odds_match_type": odds_match_type,
        }
        await asyncio.to_thread(self._insert, row)
        await self._post_telegram(row, channel_name=channel_name, capper_name=capper_name, edit_failed=edit_failed)

    # ── Telegram audit channel ─────────────────────────────────────────────────

    async def _post_telegram(self, row: dict, channel_name: str = "", capper_name: str = "", edit_failed: bool = False) -> None:
        """Post a formatted HTML summary to the audit Telegram channel."""
        if not self.audit_channel_id or not self.bot_token:
            return
        if row["verdict"] == "PENDING":
            return  # game not yet played — don't clutter audit channel

        import html as _html

        def e(s: str) -> str:
            return _html.escape(str(s))

        verdict = row["verdict"]
        sport   = row["sport"]
        dry_tag      = "  <code>[DRY]</code>" if row["dry_run"] else ""
        edit_fail_tag = "  <code>[EDIT FAILED]</code>" if edit_failed else ""

        channel_bare = abs(row["channel_id"])
        link = f"https://t.me/c/{str(channel_bare)[3:]}/{row['message_id']}"

        # Line 1 — capper · channel (linked)  [DRY] [EDIT FAILED]
        ch_linked = f'<a href="{link}">{e(channel_name)}</a>' if channel_name else f'<a href="{link}">view</a>'
        meta_parts = []
        if capper_name:
            meta_parts.append(f"<b>{e(capper_name)}</b>")
        meta_parts.append(ch_linked)
        line1 = "  ·  ".join(meta_parts) + dry_tag + edit_fail_tag

        def _trunc(text: str, limit: int = 120) -> str:
            """Truncate to full sentences up to limit chars."""
            if len(text) <= limit:
                return text
            sub = text[:limit]
            for sep in (". ", "! ", "? "):
                idx = sub.rfind(sep)
                if idx > limit // 3:
                    return text[:idx + 1]
            idx = sub.rfind(" ")
            return (text[:idx] + "…") if idx > 0 else sub + "…"

        # Line 2 — picks with per-pick emoji, tag, and calc below each
        overall_em = VERDICT_EMOJI.get(verdict, "")
        raw_lines = [l for l in (row["pick_desc"] or "").splitlines() if l.strip()]
        pick_blocks = []
        for l in raw_lines:
            # "description|sport|game_date|calc" (calc may contain |)
            parts = l.split("|", 3)
            desc_raw     = parts[0] if len(parts) > 0 else l
            pick_sport   = parts[1] if len(parts) > 1 else sport
            game_date_raw = parts[2] if len(parts) > 2 else row["date"]
            pick_calc    = parts[3] if len(parts) > 3 else ""
            try:
                gd = datetime.strptime(game_date_raw, "%Y-%m-%d")
                date_tag = f"{gd.month}/{gd.day}"
            except ValueError:
                date_tag = game_date_raw
            pick_tag = f" [{e(pick_sport)} · {date_tag}]" if pick_sport else ""
            # Skip records prefix with "VERDICT: "
            matched = next((v for v in VERDICT_EMOJI if desc_raw.startswith(f"{v}: ")), None)
            if matched:
                desc = desc_raw[len(matched) + 2:]
                pick_line = f"{e(desc)}{VERDICT_EMOJI[matched]}{pick_tag}"
            else:
                pick_line = f"{e(desc_raw)}{overall_em}{pick_tag}"
            pick_line = f"• {pick_line}"
            block = pick_line
            if pick_calc:
                block += f"\n<i>{e(_trunc(pick_calc))}</i>"
            pick_blocks.append(block)
        line2 = "\n".join(pick_blocks) if pick_blocks else None

        text = "\n".join(l for l in [line1, line2] if l)

        try:
            async with httpx.AsyncClient(timeout=10) as http:
                await http.post(
                    f"https://api.telegram.org/bot{self.bot_token}/sendMessage",
                    json={
                        "chat_id": self.audit_channel_id,
                        "text": text,
                        "parse_mode": "HTML",
                        "disable_web_page_preview": True,
                    },
                )
        except Exception as exc:
            # Never crash the main flow because of audit failures
            print(f"[audit] Telegram post failed: {exc}")

    async def warn(self, text: str) -> None:
        """Post a plain warning message to the audit channel. Never raises."""
        if not self.audit_channel_id or not self.bot_token:
            return
        try:
            async with httpx.AsyncClient(timeout=10) as http:
                await http.post(
                    f"https://api.telegram.org/bot{self.bot_token}/sendMessage",
                    json={
                        "chat_id": self.audit_channel_id,
                        "text": text,
                        "parse_mode": "HTML",
                        "disable_web_page_preview": True,
                    },
                )
        except Exception as exc:
            print(f"[audit] warn post failed: {exc}")

    # ── Broadcast results channel ──────────────────────────────────────────────

    async def broadcast_results(
        self,
        *,
        channel_id: int,
        message_id: int,
        pick_results: list[tuple[dict, str, int | None]],  # (pick_dict, verdict, odds) per pick
        capper_name: str = "",
        client=None,
        reply_to_id: int | None = None,
    ) -> None:
        """Post a compact result message to the broadcast results channel for this source channel."""
        target = self.broadcast_results_mappings.get(channel_id)
        if not target or not self.bot_token:
            return

        # Keep resolved picks (WIN/LOSS/PUSH) and broadcast if any are present
        resolved = [(p, v, o) for p, v, o in pick_results if v in ("WIN", "LOSS", "PUSH")]
        if not resolved:
            return

        import html as _html

        def e(s: str) -> str:
            return _html.escape(str(s))

        def fmt_odds(o: int | None) -> str:
            if o is None:
                return ""
            return f"+{o}" if o > 0 else str(o)

        channel_bare = str(abs(channel_id))[3:]
        link = f"https://t.me/c/{channel_bare}/{message_id}"

        # The capper label is the message's first line, which for analytics-source
        # channels is often a paragraph of reasoning rather than a short handle.
        # Cap it so the label stays handle-sized and never swallows the result line.
        capper_label = " ".join(capper_name.split())
        if len(capper_label) > 40:
            capper_label = capper_label[:39].rstrip() + "…"

        # Capper name is the link; bold if present, plain link if not
        capper_linked = f'<b><a href="{link}">{e(capper_label)}</a></b>' if capper_label else f'<a href="{link}">view</a>'

        # Detect a parlay from ALL passed legs, not just the resolved ones: a
        # parlay that settled on one lost leg still passes its other (voided /
        # pending) legs so the whole ticket can be shown and priced.
        is_parlay = any(p.get("is_parlay_leg") for p, _, _o in pick_results)
        picks = [(_format_pick(p), v, fmt_odds(o)) for p, v, o in resolved]

        def _pick_line(desc: str, verdict: str, odds_str: str) -> str:
            odds_part = f" [{e(odds_str)}]" if odds_str else ""
            return f"{VERDICT_EMOJI.get(verdict, '')} {e(desc)}{odds_part}"

        _parlay_combined_odds = parlay_combined_odds

        def _overall_emoji(verdicts_only: list[str]) -> str:
            if "LOSS" in verdicts_only:
                return VERDICT_EMOJI["LOSS"]
            elif all(v == "WIN" for v in verdicts_only):
                return VERDICT_EMOJI["WIN"]
            elif any(v == "PENDING" for v in verdicts_only):
                return VERDICT_EMOJI["PENDING"]
            elif any(v == "PUSH" for v in verdicts_only):
                return VERDICT_EMOJI["PUSH"]
            return VERDICT_EMOJI["UNKNOWN"]

        # Check is_parlay BEFORE the single-pick case: a parlay that settled on
        # a single lost leg (siblings still pending/dropped) has len(picks)==1 but
        # must still render as a Parlay, not a lone straight pick.
        if is_parlay:
            # One ticket: list every leg and price the whole parlay from every
            # leg's odds — not just the individually-resolved legs. The verdict
            # emoji still comes from the resolved legs (a LOSS settles it).
            parlay_all = [(p, v, o) for p, v, o in pick_results if p.get("is_parlay_leg")]
            verdicts_only = [v for _, v, _ in parlay_all if v in ("WIN", "LOSS", "PUSH")]
            overall_emoji = _overall_emoji(verdicts_only)
            combined = _parlay_combined_odds([o for _, _, o in parlay_all])
            combined_part = f" [{e(fmt_odds(combined))}]" if combined is not None else ""
            legs = "\n".join(f"• {e(_format_pick(p))}" for p, _, _ in parlay_all)
            text = f"{overall_emoji} {capper_linked} · Parlay{combined_part}\n{legs}"
        elif len(picks) == 1:
            desc, verdict, odds_str = picks[0]
            emoji = VERDICT_EMOJI.get(verdict, "")
            odds_part = f" [{e(odds_str)}]" if odds_str else ""
            text = f"{emoji} {capper_linked} · {e(desc)}{odds_part}"
        else:
            # Non-parlay multi-pick: one emoji per pick
            lines = [_pick_line(d, v, o) for d, v, o in picks]
            text = capper_linked + "\n" + "\n".join(lines)

        # Try to find the auto-forwarded message in the discussion group so we can reply to it.
        # Use pre-cached reply_to_id if provided (from grade daemon); fall back to Telethon lookup.
        if reply_to_id is None and client is not None:
            try:
                from telethon.tl.functions.messages import GetDiscussionMessageRequest
                disc = await client(GetDiscussionMessageRequest(peer=channel_id, msg_id=message_id))
                reply_to_id = disc.messages[0].id
            except Exception:
                pass  # no linked group or message not found — send without reply

        payload: dict = {
            "chat_id": target,
            "text": text,
            "parse_mode": "HTML",
            "disable_web_page_preview": True,
            # If reply_to_id points to a message the bot can't resolve (e.g. the
            # auto-forwarded discussion copy ended up in a state where Bot API
            # returns "message to be replied not found"), Telegram will still
            # post the broadcast — just without the reply thread — instead of
            # dropping the whole request. Without this flag the sendMessage
            # returns 400 and the result silently never posts.
            "allow_sending_without_reply": True,
        }
        if reply_to_id is not None:
            payload["reply_to_message_id"] = reply_to_id

        try:
            async with httpx.AsyncClient(timeout=10) as http:
                r = await http.post(
                    f"https://api.telegram.org/bot{self.bot_token}/sendMessage",
                    json=payload,
                )
            if not r.is_success:
                snippet = r.text[:300]
                print(f"[broadcast_results] Telegram post failed: {r.status_code} {snippet}")
                await self.warn(
                    f"⚠ <b>broadcast_results failed</b> [{r.status_code}]\n"
                    f"{e(snippet)}\n"
                    f'<a href="{link}">view pick</a>'
                )
        except Exception as exc:
            print(f"[broadcast_results] Telegram post failed: {exc}")
            await self.warn(
                f"⚠ <b>broadcast_results exception</b>\n"
                f"{e(str(exc)[:300])}\n"
                f'<a href="{link}">view pick</a>'
            )


def log_api_costs(run_type: str, claude_cost: float, odds_requests: int, db_path: str = DB_PATH) -> None:
    """Write a run-level API cost row to picks.db. Safe to call without an AuditLog instance."""
    import sqlite3 as _sqlite3
    conn = _sqlite3.connect(db_path)
    try:
        conn.execute(
            "INSERT INTO api_costs (logged_at, run_type, claude_cost, odds_requests_used) VALUES (?, ?, ?, ?)",
            (datetime.now(timezone.utc).isoformat(), run_type, claude_cost, odds_requests),
        )
        conn.commit()
    finally:
        conn.close()
