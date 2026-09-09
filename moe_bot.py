"""Dedicated Telegram runtime for the NFL MOE review group."""

from __future__ import annotations

import asyncio
import logging
import os
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from dotenv import load_dotenv
from gspread.exceptions import APIError
from telethon import TelegramClient, events
from telethon.sessions import StringSession

from moe import (
    approved_opinions,
    configured_opinion_store,
    opinion_output_sha256,
)
from moe_desk import (
    BotApi,
    build_desks,
    desk_config_from_env,
    desk_ids_report,
    load_state,
    parse_callback,
    review_targets,
    save_state,
    sync_desk,
    topic_id_from_reply,
)
from moe_god import load_registry
from moe_identity import (
    REVIEWER_ROLE,
    resolve_role_user_ids_from_spreadsheet,
)
from nfl_lines import get_gspread_client

ROOT = Path(__file__).resolve().parent
load_dotenv(ROOT / ".env")
load_dotenv(ROOT / ".env.local", override=True)

log = logging.getLogger(__name__)

GAMES_CACHE_TTL_SECONDS = 60
TEAM_CACHE_TTL_SECONDS = 600
MOE_CACHE_TTL_SECONDS = 30
REVIEWERS_CACHE_TTL_SECONDS = 300

_CACHE_LOCK = threading.RLock()
_SYNC_LOCK = threading.Lock()
_REVIEW_LOCK = threading.Lock()
_CACHE: dict[str, tuple[float, Any]] = {}
_SPREADSHEET: Any | None = None
_MOE_STORE: Any | None = None


def _cached_value(key: str, ttl: int, loader: Any) -> Any:
    now = time.monotonic()
    with _CACHE_LOCK:
        cached = _CACHE.get(key)
        if cached is not None and now - cached[0] < ttl:
            return cached[1]
        try:
            value = loader()
        except APIError as exc:
            status = getattr(exc.response, "status_code", None)
            if cached is not None and (
                status == 429 or (isinstance(status, int) and status >= 500)
            ):
                log.warning(
                    "Sheets %s refreshing %s; serving stale cache", status, key
                )
                _CACHE[key] = (now, cached[1])
                return cached[1]
            raise
        _CACHE[key] = (now, value)
        return value


def _set_cache(key: str, value: Any) -> None:
    with _CACHE_LOCK:
        _CACHE[key] = (time.monotonic(), value)


def _spreadsheet() -> Any:
    global _SPREADSHEET
    with _CACHE_LOCK:
        if _SPREADSHEET is None:
            _SPREADSHEET = get_gspread_client(
                os.environ["GOOGLE_CREDENTIALS"]
            ).open_by_key(os.environ["NFL_INTAKE_SHEET_ID"])
        return _SPREADSHEET


def _opinion_store() -> Any:
    global _MOE_STORE
    with _CACHE_LOCK:
        if _MOE_STORE is None:
            _MOE_STORE = configured_opinion_store()
        return _MOE_STORE


def load_opinions() -> list[dict[str, Any]]:
    return _cached_value(
        "moe_opinions",
        MOE_CACHE_TTL_SECONDS,
        lambda: _opinion_store().list(),
    )


def load_reviewers() -> dict[int, str]:
    return _cached_value(
        "desk_reviewers",
        REVIEWERS_CACHE_TTL_SECONDS,
        lambda: resolve_role_user_ids_from_spreadsheet(
            _spreadsheet(), REVIEWER_ROLE
        ),
    )


def load_games() -> tuple[list[dict[str, Any]], dict[str, str]]:
    spreadsheet = _spreadsheet()
    games = _cached_value(
        "nfl_games",
        GAMES_CACHE_TTL_SECONDS,
        lambda: spreadsheet.worksheet("nfl_games").get_all_records(),
    )
    team_rows = _cached_value(
        "team_emojis",
        TEAM_CACHE_TTL_SECONDS,
        lambda: spreadsheet.worksheet("team_emojis").get_all_records(),
    )
    abbreviations = {
        str(row.get("team_name") or "").strip(): str(
            row.get("abbreviation") or ""
        ).strip()
        for row in team_rows
        if str(row.get("team_name") or "").strip()
        and str(row.get("abbreviation") or "").strip()
    }
    return games, abbreviations


def sync_once(config: Any, api: Any, *, now: datetime | None = None) -> Any:
    now = now or datetime.now(timezone.utc)
    with _SYNC_LOCK:
        rows = load_opinions()
        games, abbreviations = load_games()
        desks = build_desks(
            games,
            rows,
            approved_opinions(rows),
            load_registry(),
            now=now,
        )
        state = load_state(config.state_path)
        try:
            summary = sync_desk(
                config=config,
                api=api,
                state=state,
                desks=desks,
                now=now,
                team_abbrevs=abbreviations,
            )
        finally:
            save_state(config.state_path, state)
    if any(
        (
            summary.posted,
            summary.edited,
            summary.alerts,
            summary.deleted,
            summary.deferred,
            summary.errors,
        )
    ):
        print(
            f"desk: posted {summary.posted} edited {summary.edited} "
            f"alerts {summary.alerts} deleted {summary.deleted} "
            f"deferred {summary.deferred} errors {summary.errors}"
        )
    return summary


def review(action: str, target: str, *, reviewer: str) -> tuple[str, bool]:
    with _REVIEW_LOCK:
        rows = load_opinions()
        targets, error = review_targets(action, target, rows)
        if error:
            return error, False
        status = "rejected" if action == "no" else "approved"
        store = _opinion_store()
        fetch = getattr(store, "fetch", None)
        done: list[str] = []
        reviewed_at = datetime.now(timezone.utc).isoformat()
        for row in targets:
            opinion_id = str(row["opinion_id"])
            if fetch is not None:
                live = fetch(opinion_id)
                if live is None:
                    return "That row is no longer in the sheet.", False
                live_status = str(
                    live.get("review_status") or "pending"
                ).strip().lower()
                if live_status != "pending":
                    by = str(live.get("reviewed_by") or "someone").strip()
                    row.update(
                        review_status=live_status,
                        reviewed_by=by,
                        reviewed_at_utc=str(
                            live.get("reviewed_at_utc") or ""
                        ),
                        approved_output_sha256=str(
                            live.get("approved_output_sha256") or ""
                        ),
                    )
                    return f"Already {live_status} by {by}.", False
            store.review(
                opinion_id,
                status=status,
                reviewed_by=reviewer,
                note="",
            )
            row["review_status"] = status
            row["reviewed_by"] = reviewer
            row["reviewed_at_utc"] = reviewed_at
            row["review_note"] = ""
            row["approved_output_sha256"] = (
                opinion_output_sha256(row) if status == "approved" else ""
            )
            done.append(str(row.get("expert_name") or row.get("expert_id")))
        _set_cache("moe_opinions", rows)
        verb = "Rejected" if status == "rejected" else "Approved"
        return f"{verb} {', '.join(done)} as {reviewer}.", True


async def main() -> None:
    api_id = int(os.environ["TELEGRAM_API_ID"])
    api_hash = os.environ["TELEGRAM_API_HASH"]
    token = os.environ["MOE_BOT_TOKEN"]
    session = os.getenv("MOE_BOT_SESSION", "")
    config = desk_config_from_env()
    if config is None:
        raise RuntimeError(
            "MOE desk configuration is incomplete; set MOE_BOT_TOKEN, "
            "MOE_DESK_CHAT_ID, MOE_DESK_REVIEW_TOPIC, and "
            "MOE_DESK_PICKS_TOPIC"
        )

    client = TelegramClient(StringSession(session), api_id, api_hash)
    api = BotApi(config.bot_token)
    inflight: set[str] = set()
    tasks: set[asyncio.Task[Any]] = set()

    async def reply(event: Any, text: str) -> None:
        try:
            await event.reply(text, silent=True)
        except Exception as exc:  # noqa: BLE001 - log transport failure
            print(f"desk: reply failed: {type(exc).__name__}: {exc}")

    async def resync() -> str | None:
        try:
            await asyncio.to_thread(sync_once, config, api)
        except Exception as exc:  # noqa: BLE001 - timed loop retries
            print(f"desk: sync after review failed: {type(exc).__name__}: {exc}")
            return f"{type(exc).__name__}: {exc}"[:200]
        return None

    async def finish_action(
        event: Any,
        action: str,
        target: str,
        key: str,
    ) -> None:
        try:
            try:
                reviewers = await asyncio.to_thread(load_reviewers)
            except Exception as exc:  # noqa: BLE001 - surfaced under card
                await reply(
                    event,
                    "Could not read the reviewer list "
                    f"({type(exc).__name__}: {exc}). Tap again in a minute."[
                        :400
                    ],
                )
                return
            reviewer = reviewers.get(event.sender_id)
            if reviewer is None:
                await reply(
                    event,
                    "Reviewers only. Add the reviewer role to your "
                    "allowed_users row.",
                )
                return
            try:
                text, ok = await asyncio.to_thread(
                    review,
                    action,
                    target,
                    reviewer=reviewer,
                )
            except Exception as exc:  # noqa: BLE001 - surfaced under card
                text, ok = f"{type(exc).__name__}: {exc}"[:350], False
            if not ok:
                await reply(event, text)
                return
            print(f"desk: {text}")
            problem = await resync()
            if problem:
                await reply(
                    event,
                    f"{text} The card could not refresh yet ({problem}); "
                    "it will on the next pass.",
                )
        finally:
            inflight.discard(key)

    @client.on(events.CallbackQuery)
    async def handle_callback(event: Any) -> None:
        data = event.data.decode()
        if not data.startswith("desk:"):
            return
        if str(event.chat_id) != str(config.chat_id):
            await event.answer("This action belongs to the MOE group.", alert=True)
            return
        parsed = parse_callback(data)
        if parsed is None:
            await event.answer("Unknown desk action.", alert=True)
            return
        action, target = parsed
        key = f"{action}:{target}"
        if key in inflight:
            await event.answer("Still working on the last tap.")
            return
        inflight.add(key)
        try:
            await event.answer("Working on it...")
        except Exception as exc:  # noqa: BLE001 - work still continues
            print(f"desk: answer failed: {type(exc).__name__}: {exc}")
        task = asyncio.create_task(
            finish_action(event, action, target, key)
        )
        tasks.add(task)
        task.add_done_callback(tasks.discard)

    @client.on(
        events.NewMessage(
            pattern=r"^/desk(?:@\w+)?$",
            incoming=True,
            func=lambda event: not event.is_private,
        )
    )
    async def report_desk_ids(event: Any) -> None:
        if str(event.chat_id) != str(config.chat_id):
            return
        reviewers = await asyncio.to_thread(load_reviewers)
        if event.sender_id not in reviewers:
            return
        chat = await event.get_chat()
        text = desk_ids_report(
            event.chat_id,
            title=str(getattr(chat, "title", "") or ""),
            supergroup=bool(getattr(chat, "megagroup", False)),
            topics=bool(getattr(chat, "forum", False)),
            topic_id=topic_id_from_reply(
                getattr(event.message, "reply_to", None)
            ),
        )
        print("desk: " + text.replace("\n", " | "))
        await event.reply(text)

    await client.start(bot_token=token)
    identity = await client.get_me()
    if str(identity.username or "").lower() != "nfl_moe_bot":
        raise RuntimeError(
            f"MOE_BOT_TOKEN authenticated as @{identity.username}, "
            "expected @nfl_moe_bot"
        )
    print(
        f"MOE bot running as @{identity.username}: chat {config.chat_id}, "
        f"review topic {config.review_topic}, picks topic "
        f"{config.picks_topic}, scores topic "
        f"{config.scores_topic or 'unset'}, sync every "
        f"{config.sync_seconds}s"
    )

    async def sync_loop() -> None:
        while True:
            try:
                await asyncio.to_thread(load_reviewers)
            except Exception as exc:  # noqa: BLE001 - retry next loop
                print(
                    "desk: reviewers refresh failed: "
                    f"{type(exc).__name__}: {exc}"
                )
            try:
                await asyncio.to_thread(sync_once, config, api)
            except Exception as exc:  # noqa: BLE001 - keep loop alive
                print(f"desk: sync failed: {type(exc).__name__}: {exc}")
            await asyncio.sleep(config.sync_seconds)

    sync_task = asyncio.create_task(sync_loop())
    try:
        await client.run_until_disconnected()
    finally:
        sync_task.cancel()
        await asyncio.gather(sync_task, return_exceptions=True)
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)


if __name__ == "__main__":
    asyncio.run(main())
