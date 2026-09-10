#!/usr/bin/env python3
"""Set up and check the desk group (moe_desk.py) from any shell — the VPS
Claude session included, so every step works from a phone.

Order of operations, once, when the group is created:

1. In Telegram: create a private group, convert it to a supergroup with
   Topics enabled, add the intake bot (@nflguesser_bot) as an admin with
   "Manage topics" and "Pin messages".
2. Send ``/desk`` inside the group: the bot replies with the chat id (and,
   inside a topic, that topic's id) and whether Topics are on. Set
   ``MOE_DESK_CHAT_ID=-100…`` in ``.env`` (both machines; it is synced).
3. ``python scripts/desk_setup.py --create-topics`` — creates the Picks
   and Scores topics and prints the ``MOE_DESK_*_TOPIC`` lines. (Review is
   automatic since 2026-09-09; the Review topic was removed 2026-09-10.)
4. ``python scripts/desk_setup.py --grant-reviewer <telegram_id>`` — adds
   ``reviewer`` to that ``allowed_users`` row (VPS, needs the sheet
   credentials). The role only opens the DM views of rows that are not
   approved (legacy pending and rejected audit rows).
5. ``python scripts/desk_setup.py --check`` — chat type, the bot's rights,
   topics, reviewers. ``--post-test`` also posts and deletes one message per
   configured topic.
6. Restart ``telegram-intake.service``; the bot logs "Desk group enabled".

Env: INTAKE_BOT_TOKEN, MOE_DESK_CHAT_ID, MOE_DESK_PICKS_TOPIC,
MOE_DESK_SCORES_TOPIC (optional);
GOOGLE_CREDENTIALS + NFL_INTAKE_SHEET_ID for the reviewer commands.
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
load_dotenv(ROOT / ".env")
load_dotenv(ROOT / ".env.local", override=True)

from moe_desk import BotApi, DeskApiError, desk_config_from_env  # noqa: E402
from moe_identity import (  # noqa: E402
    ALLOWED_USERS_TAB,
    ALLOWED_USER_HEADERS,
    MOE_EXPERT_IDS_COLUMN,
    REVIEWER_ROLE,
    resolve_role_user_ids,
)

TOPICS = (
    ("MOE_DESK_PICKS_TOPIC", "🏈 Picks"),
    ("MOE_DESK_SCORES_TOPIC", "📊 Scores"),
)


def _require(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise SystemExit(f"{name} is not set")
    return value


def _allowed_users_worksheet():
    from nfl_lines import get_gspread_client

    credentials = _require("GOOGLE_CREDENTIALS")
    sheet_id = _require("NFL_INTAKE_SHEET_ID")
    spreadsheet = get_gspread_client(credentials).open_by_key(sheet_id)
    return spreadsheet.worksheet(ALLOWED_USERS_TAB)


def create_topics(api: BotApi, chat_id: str) -> int:
    print("creating topics …")
    for env_name, title in TOPICS:
        existing = os.environ.get(env_name, "").strip()
        if existing:
            print(f"{env_name}={existing}  (already set; not created again)")
            continue
        thread_id = api.create_topic(chat_id, title)
        print(f"{env_name}={thread_id}")
    print(
        "Put the lines above in .env on both machines (syncenv deletes server "
        "keys that are absent locally), then restart telegram-intake."
    )
    return 0


def check(api: BotApi, chat_id: str, *, post_test: bool) -> int:
    problems = 0
    chat = api.get_chat(chat_id)
    kind = chat.get("type")
    forum = bool(chat.get("is_forum"))
    print(f"chat: {chat.get('title')!r} type={kind} topics={'on' if forum else 'OFF'}")
    if kind != "supergroup" or not forum:
        print("  ✗ needs a supergroup with Topics enabled")
        problems += 1
    me = api.get_me()
    member = api.get_member(chat_id, int(me["id"]))
    status = member.get("status")
    rights = {
        key: bool(member.get(key))
        for key in ("can_manage_topics", "can_pin_messages", "can_delete_messages")
    }
    print(f"bot: @{me.get('username')} status={status} rights={rights}")
    if status not in {"administrator", "creator"}:
        print("  ✗ the bot must be an admin")
        problems += 1
    for key in ("can_manage_topics", "can_pin_messages"):
        if not rights[key]:
            print(f"  ✗ missing admin right: {key}")
            problems += 1
    config = desk_config_from_env()
    if config is None:
        print("topics: MOE_DESK_PICKS_TOPIC unset — desk disabled")
        problems += 1
    else:
        print(
            f"topics: picks={config.picks_topic} "
            f"scores={config.scores_topic or 'unset'} · sync every {config.sync_seconds}s"
        )
        if post_test:
            for label, topic in (
                ("picks", config.picks_topic),
                ("scores", config.scores_topic),
            ):
                if not topic:
                    continue
                try:
                    message_id = api.send(
                        chat_id, topic, f"desk_setup: {label} topic test", silent=True
                    )
                    api.delete(chat_id, message_id)
                    print(f"  ✓ {label} topic {topic}: posted and deleted")
                except DeskApiError as exc:
                    print(f"  ✗ {label} topic {topic}: {exc}")
                    problems += 1
    try:
        rows = _allowed_users_worksheet().get_all_records(
            expected_headers=ALLOWED_USER_HEADERS
        )
    except SystemExit as exc:
        print(f"reviewers: skipped ({exc})")
    else:
        reviewers = resolve_role_user_ids(rows, REVIEWER_ROLE)
        names = ", ".join(sorted(reviewers.values())) or "none"
        # Informational only: review is automatic; the role just opens the
        # DM views of unapproved rows.
        print(f"reviewers: {len(reviewers)} ({names})")
    print("OK" if not problems else f"{problems} problem(s)")
    return 1 if problems else 0


def grant_reviewer(telegram_id: int) -> int:
    worksheet = _allowed_users_worksheet()
    rows = worksheet.get_all_records(expected_headers=ALLOWED_USER_HEADERS)
    matches = [
        index
        for index, row in enumerate(rows)
        if str(row.get("telegram_id") or "").strip() == str(telegram_id)
    ]
    if len(matches) != 1:
        raise SystemExit(
            f"expected one {ALLOWED_USERS_TAB} row with telegram_id {telegram_id}, "
            f"found {len(matches)} — add the person to the tab first"
        )
    index = matches[0]
    row = rows[index]
    roles = [
        item.strip()
        for item in str(row.get(MOE_EXPERT_IDS_COLUMN) or "").split(",")
        if item.strip()
    ]
    if REVIEWER_ROLE in {role.lower() for role in roles}:
        print(f"{row.get('display_name')} already holds {REVIEWER_ROLE}")
        return 0
    roles.append(REVIEWER_ROLE)
    column = ALLOWED_USER_HEADERS.index(MOE_EXPERT_IDS_COLUMN) + 1
    worksheet.update_cell(index + 2, column, ", ".join(roles))
    print(f"granted {REVIEWER_ROLE} to {row.get('display_name')} ({telegram_id}): {', '.join(roles)}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--check", action="store_true", help="verify the group, the bot's rights, topics, reviewers")
    group.add_argument("--create-topics", action="store_true", help="create the three topics and print the env lines")
    group.add_argument("--grant-reviewer", type=int, metavar="TELEGRAM_ID", help="add the reviewer role to that allowed_users row")
    parser.add_argument("--post-test", action="store_true", help="with --check: post and delete a message in every configured topic")
    args = parser.parse_args(argv)
    if args.grant_reviewer is not None:
        return grant_reviewer(args.grant_reviewer)
    api = BotApi(_require("INTAKE_BOT_TOKEN"))
    chat_id = _require("MOE_DESK_CHAT_ID")
    try:
        if args.create_topics:
            return create_topics(api, chat_id)
        return check(api, chat_id, post_test=args.post_test)
    except DeskApiError as exc:
        raise SystemExit(f"Bot API: {exc}") from exc


if __name__ == "__main__":
    raise SystemExit(main())
