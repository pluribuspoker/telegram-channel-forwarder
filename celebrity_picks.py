"""Canonical storage helpers for attributed celebrity NFL picks."""

from __future__ import annotations

import hashlib
import re
from typing import Any


CELEBRITY_TAB = "celebrity_picks"
LEGACY_CELEBRITY_HEADERS = [
    "submission_id",
    "submitted_at_utc",
    "submitted_at_et",
    "telegram_user_id",
    "telegram_username",
    "event_id",
    "season",
    "week",
    "commence_time_et",
    "away_team",
    "home_team",
    "period",
    "market",
    "side",
    "celebrity_name",
]
CELEBRITY_HEADERS = [
    *LEGACY_CELEBRITY_HEADERS,
    "commence_time_utc",
    "pick_id",
    "canonical_key",
    "market_family",
    "subject",
    "stat",
    "direction",
    "line",
    "price",
    "selection_text",
    "raw_pick_text",
]
CUSTOM_MARKET_FAMILIES = {
    "player_prop",
    "team_prop",
    "other",
}


def _normalized(value: Any) -> str:
    return " ".join(str(value or "").split()).casefold()


def canonical_pick_key(
    *,
    period: str,
    market_family: str,
    market: str,
    subject: str,
    stat: str,
) -> str:
    """Identify the bet whose latest attributed revision should win."""
    parts = (
        period,
        market_family,
        market,
        subject,
        stat,
    )
    return "|".join(_normalized(part) for part in parts)


def celebrity_pick_id(submission_id: Any, celebrity_name: Any) -> str:
    value = f"{submission_id}\0{_normalized(celebrity_name)}"
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _standard_pick_fields(submission: dict[str, Any]) -> dict[str, Any]:
    market = str(submission.get("market") or "")
    side = str(submission.get("side") or "")
    period = str(submission.get("period") or "")
    line = submission.get("latest_selected_line", "")
    price = submission.get("latest_selected_price", "")
    if market in {"spread", "moneyline"}:
        market_family = "side"
        subject = "game"
        stat = market
        direction = side
    elif market == "total":
        market_family = "total"
        subject = "game"
        stat = "total"
        direction = side.title()
    else:
        market_family = str(submission.get("market_family") or "other")
        subject = str(submission.get("subject") or "")
        stat = str(submission.get("stat") or market)
        direction = str(submission.get("direction") or side)
    selection = str(submission.get("selection_text") or "").strip()
    if not selection:
        values = [direction]
        if str(line).strip() and str(line) != "nodata":
            values.append(str(line))
        if str(price).strip() and str(price) != "nodata":
            values.append(f"({price})")
        selection = " ".join(values)
    return {
        "market_family": market_family,
        "subject": subject,
        "stat": stat,
        "direction": direction,
        "line": line,
        "price": price,
        "selection_text": selection,
        "raw_pick_text": str(submission.get("raw_pick_text") or ""),
        "canonical_key": canonical_pick_key(
            period=period,
            market_family=market_family,
            market=market,
            subject=subject,
            stat=stat,
        ),
    }


def build_celebrity_rows(
    *, submission: dict[str, Any], names: list[str]
) -> list[dict[str, Any]]:
    """Build one complete attributed row per celebrity name."""
    fields = _standard_pick_fields(submission)
    rows = []
    for name in names:
        row = {
            header: submission.get(header, "")
            for header in CELEBRITY_HEADERS
        }
        row.update(fields)
        row["celebrity_name"] = name
        row["pick_id"] = celebrity_pick_id(
            row["submission_id"],
            name,
        )
        rows.append(row)
    return rows


def parse_custom_pick_text(
    raw_text: str,
    *,
    market_family: str,
) -> dict[str, Any]:
    """Parse the structured custom-pick reply while retaining its exact text."""
    if market_family not in CUSTOM_MARKET_FAMILIES:
        raise ValueError("Unsupported custom market family")
    values: dict[str, str] = {}
    current = ""
    for raw_line in str(raw_text).splitlines():
        match = re.match(
            r"^(Subject|Market|Pick|Odds|Rationale)\s*:\s*(.*)$",
            raw_line,
            re.IGNORECASE,
        )
        if match:
            current = match.group(1).casefold()
            values[current] = match.group(2).strip()
        elif current == "rationale":
            values[current] = (
                values[current] + "\n" + raw_line
            ).strip()
    missing = [
        label
        for label in ("subject", "market", "pick")
        if not values.get(label)
    ]
    if missing:
        raise ValueError(
            "Custom picks require Subject, Market, and Pick fields"
        )
    selection = values["pick"]
    pick_match = re.fullmatch(
        r"(Over|Under)\s+(-?\d+(?:\.\d+)?)",
        selection,
        re.IGNORECASE,
    )
    direction = selection
    line: float | str = ""
    if pick_match:
        direction = pick_match.group(1).title()
        line = float(pick_match.group(2))
    price: int | str = ""
    odds = values.get("odds", "")
    if odds:
        if not re.fullmatch(r"[+-]?\d+", odds):
            raise ValueError("Odds must be an integer American price")
        price = int(odds)
    return {
        "market": market_family,
        "market_family": market_family,
        "subject": values["subject"],
        "stat": values["market"],
        "direction": direction,
        "side": direction,
        "line": line,
        "price": price,
        "selection_text": selection,
        "raw_pick_text": str(raw_text),
        "rationale": values.get("rationale", ""),
    }
