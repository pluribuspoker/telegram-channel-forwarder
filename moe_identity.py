"""Resolve human MOE expert identities from the intake workbook."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any


ALLOWED_USERS_TAB = "allowed_users"
MOE_EXPERT_IDS_COLUMN = "moe_expert_ids"
ALLOWED_USER_HEADERS = [
    "display_name",
    "telegram_id",
    "telegram_username",
    MOE_EXPERT_IDS_COLUMN,
]


def _expert_ids(value: Any) -> set[str]:
    return {
        item.strip().lower()
        for item in str(value or "").split(",")
        if item.strip()
    }


def resolve_moe_expert_user_id(
    rows: Iterable[Mapping[str, Any]],
    expert_id: str,
) -> str:
    """Return the unique positive Telegram user ID assigned to an expert."""
    normalized_expert_id = expert_id.strip().lower()
    if not normalized_expert_id:
        raise ValueError("MOE expert ID cannot be empty")

    matches = [
        row
        for row in rows
        if normalized_expert_id in _expert_ids(row.get(MOE_EXPERT_IDS_COLUMN))
    ]
    if len(matches) != 1:
        raise RuntimeError(
            f"Expected exactly one {ALLOWED_USERS_TAB} row for MOE expert "
            f"{normalized_expert_id!r}; found {len(matches)}"
        )

    raw_user_id = str(matches[0].get("telegram_id") or "").strip()
    try:
        user_id = int(raw_user_id)
    except ValueError as exc:
        raise RuntimeError(
            f"MOE expert {normalized_expert_id!r} has an invalid Telegram ID"
        ) from exc
    if user_id <= 0:
        raise RuntimeError(
            f"MOE expert {normalized_expert_id!r} has an invalid Telegram ID"
        )
    return str(user_id)


REVIEWER_ROLE = "reviewer"


def resolve_role_user_ids(
    rows: Iterable[Mapping[str, Any]],
    role: str,
) -> dict[int, str]:
    """Every ``allowed_users`` row carrying ``role`` in ``moe_expert_ids``, as
    ``{telegram_id: display_name}``. Unlike an expert role, several people may
    hold one (the desk group's reviewers); a row with the role but an invalid
    Telegram ID fails closed."""
    normalized_role = role.strip().lower()
    if not normalized_role:
        raise ValueError("role cannot be empty")
    holders: dict[int, str] = {}
    for row in rows:
        if normalized_role not in _expert_ids(row.get(MOE_EXPERT_IDS_COLUMN)):
            continue
        raw_user_id = str(row.get("telegram_id") or "").strip()
        try:
            user_id = int(raw_user_id)
        except ValueError as exc:
            raise RuntimeError(
                f"{ALLOWED_USERS_TAB} row with role {normalized_role!r} has an "
                "invalid Telegram ID"
            ) from exc
        if user_id <= 0:
            raise RuntimeError(
                f"{ALLOWED_USERS_TAB} row with role {normalized_role!r} has an "
                "invalid Telegram ID"
            )
        display_name = str(row.get("display_name") or "").strip()
        holders[user_id] = display_name or str(user_id)
    return holders


def resolve_role_user_ids_from_spreadsheet(
    spreadsheet: Any,
    role: str,
) -> dict[int, str]:
    rows = spreadsheet.worksheet(ALLOWED_USERS_TAB).get_all_records(
        expected_headers=ALLOWED_USER_HEADERS
    )
    return resolve_role_user_ids(rows, role)


def resolve_moe_expert_user_id_from_spreadsheet(
    spreadsheet: Any,
    expert_id: str,
) -> str:
    """Load the authoritative identity table and resolve one expert role."""
    rows = spreadsheet.worksheet(ALLOWED_USERS_TAB).get_all_records(
        expected_headers=ALLOWED_USER_HEADERS
    )
    return resolve_moe_expert_user_id(rows, expert_id)
