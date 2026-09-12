"""NFL Pikkit split snapshots, scheduling, storage, and deterministic features."""

from __future__ import annotations

import hashlib
import json
import math
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Awaitable, Callable, Iterable

import gspread

from nfl_lines import (
    ET,
    LATEST_AWAY_COLUMN,
    LATEST_HOME_COLUMN,
    LATEST_TOTALS_COLUMN,
    SNAPSHOT_HEADERS as LINE_SNAPSHOT_HEADERS,
    _call_with_retry,
    decode_packed_markets,
    get_gspread_client,
)

PIKKIT_SNAPSHOTS_TAB = "nfl_pikkit_snapshots"
BASELINE_INTERVAL = timedelta(hours=12)
FINAL_LEAD = timedelta(hours=2)
PERCENT_TOLERANCE = 0.002

PIKKIT_SNAPSHOT_HEADERS = [
    "snapshot_id",
    "capture_kind",
    "scheduled_for_utc",
    "captured_at_utc",
    "nfl_event_id",
    "pikkit_event_id",
    "season",
    "week",
    "commence_time_utc",
    "away_team",
    "home_team",
    "pikkit_status",
    "num_picks",
    "total_wagered",
    "home_ml_label",
    "home_ml_bet_pct",
    "home_ml_handle_pct",
    "home_ml_bets",
    "away_ml_label",
    "away_ml_bet_pct",
    "away_ml_handle_pct",
    "away_ml_bets",
    "home_spread_label",
    "home_spread_bet_pct",
    "home_spread_handle_pct",
    "home_spread_bets",
    "away_spread_label",
    "away_spread_bet_pct",
    "away_spread_handle_pct",
    "away_spread_bets",
    "over_label",
    "over_bet_pct",
    "over_handle_pct",
    "over_bets",
    "under_label",
    "under_bet_pct",
    "under_handle_pct",
    "under_bets",
    "source",
    "source_json",
    "payload_sha256",
]

MARKET_SIDES = {
    "moneyline": ("home", "away"),
    "spread": ("home", "away"),
    "total": ("over", "under"),
}


def canonical_json(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def parse_time(value: Any) -> datetime:
    parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _normalized_team(value: Any) -> str:
    return re.sub(r"[^a-z0-9]", "", str(value or "").lower())


def match_nfl_game_to_pikkit_event(
    game: dict[str, Any],
    events: Iterable[dict[str, Any]],
) -> dict[str, Any] | None:
    """Require exact oriented teams and the same Eastern calendar date."""
    away = _normalized_team(game.get("away_team"))
    home = _normalized_team(game.get("home_team"))
    kickoff_date = parse_time(game["commence_time_utc"]).astimezone(ET).date()
    matches = []
    for event in events:
        if (
            _normalized_team(event.get("away_full")) != away
            or _normalized_team(event.get("home_full")) != home
        ):
            continue
        start = event.get("start_time")
        if not start or parse_time(start).astimezone(ET).date() != kickoff_date:
            continue
        matches.append(event)
    if len(matches) > 1:
        raise ValueError(
            f"Multiple Pikkit events match NFL event {game.get('event_id')}"
        )
    return matches[0] if matches else None


def _finite_fraction(value: Any, field: str) -> float:
    if isinstance(value, bool):
        raise ValueError(f"{field} must be numeric")
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{field} must be numeric") from exc
    if not math.isfinite(number) or not 0.0 <= number <= 1.0:
        raise ValueError(f"{field} must be within 0..1")
    return number


def _nonnegative_int(value: Any, field: str) -> int:
    if isinstance(value, bool):
        raise ValueError(f"{field} must be a non-negative integer")
    try:
        number = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{field} must be a non-negative integer") from exc
    if number < 0 or float(value) != number:
        raise ValueError(f"{field} must be a non-negative integer")
    return number


def _nonnegative_number(value: Any, field: str) -> float:
    if isinstance(value, bool):
        raise ValueError(f"{field} must be non-negative")
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{field} must be non-negative") from exc
    if not math.isfinite(number) or number < 0:
        raise ValueError(f"{field} must be non-negative")
    return number


def normalize_full_splits(splits: dict[str, Any]) -> dict[str, Any]:
    """Return the complete validated Pikkit event split object."""
    if not isinstance(splits, dict):
        raise ValueError("Pikkit splits must be an object")
    normalized: dict[str, Any] = {
        "num_picks": _nonnegative_int(splits.get("num_picks", 0), "num_picks"),
        "total_wagered": _nonnegative_number(
            splits.get("total_wagered", 0), "total_wagered"
        ),
    }
    for market, side_names in MARKET_SIDES.items():
        raw_market = splits.get(market)
        if raw_market in (None, {}):
            continue
        if not isinstance(raw_market, dict):
            raise ValueError(f"{market} must be an object")
        present: dict[str, dict[str, Any]] = {}
        for side in side_names:
            raw_side = raw_market.get(side)
            if raw_side is None:
                continue
            if not isinstance(raw_side, dict):
                raise ValueError(f"{market}.{side} must be an object")
            present[side] = {
                "bet_pct": _finite_fraction(
                    raw_side.get("bet_pct"), f"{market}.{side}.bet_pct"
                ),
                "handle_pct": _finite_fraction(
                    raw_side.get("handle_pct"), f"{market}.{side}.handle_pct"
                ),
                "label": str(raw_side.get("label") or "").strip(),
                "bets": _nonnegative_int(
                    raw_side.get("bets", 0), f"{market}.{side}.bets"
                ),
            }
        if present and set(present) != set(side_names):
            raise ValueError(f"{market} must contain both opposing sides")
        if not present:
            continue
        for metric in ("bet_pct", "handle_pct"):
            total = sum(float(present[side][metric]) for side in side_names)
            if abs(total - 1.0) > PERCENT_TOLERANCE:
                raise ValueError(f"{market} {metric} values do not sum to 1")
        normalized[market] = present
    return normalized


def complete_markets(splits: dict[str, Any]) -> list[str]:
    return [market for market in MARKET_SIDES if market in splits]


def _baseline_bucket(moment: datetime) -> datetime:
    moment = moment.astimezone(timezone.utc)
    seconds = int(moment.timestamp())
    bucket = seconds - seconds % int(BASELINE_INTERVAL.total_seconds())
    return datetime.fromtimestamp(bucket, timezone.utc)


def snapshot_identity(
    nfl_event_id: str,
    capture_kind: str,
    scheduled_for: datetime,
) -> str:
    if capture_kind not in {"baseline", "final_t_minus_2h"}:
        raise ValueError(f"Unsupported capture kind: {capture_kind}")
    return sha256_text(
        canonical_json(
            {
                "nfl_event_id": str(nfl_event_id),
                "capture_kind": capture_kind,
                "scheduled_for_utc": scheduled_for.astimezone(
                    timezone.utc
                ).isoformat(),
            }
        )
    )


@dataclass(frozen=True)
class CaptureTask:
    game: dict[str, Any]
    capture_kind: str
    scheduled_for_utc: datetime
    snapshot_id: str


EventLoader = Callable[[str], Awaitable[dict[str, list[dict[str, Any]]]]]
SplitLoader = Callable[[str], Awaitable[dict[str, Any] | None]]


def capture_tasks(
    games: Iterable[dict[str, Any]],
    snapshots: Iterable[dict[str, Any]],
    now: datetime,
) -> list[CaptureTask]:
    """Return the baseline or final capture currently due for each game."""
    now = now.astimezone(timezone.utc)
    rows = list(snapshots)
    by_event: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        by_event.setdefault(str(row.get("nfl_event_id") or ""), []).append(row)
    tasks = []
    for game in games:
        if str(game.get("status") or "") != "upcoming":
            continue
        event_id = str(game.get("event_id") or "")
        kickoff = parse_time(game["commence_time_utc"])
        if not event_id or now >= kickoff:
            continue
        existing = by_event.get(event_id, [])
        if any(
            str(row.get("capture_kind")) == "final_t_minus_2h"
            for row in existing
        ):
            continue
        final_at = kickoff - FINAL_LEAD
        if now >= final_at:
            capture_kind = "final_t_minus_2h"
            scheduled_for = final_at
        else:
            capture_kind = "baseline"
            scheduled_for = _baseline_bucket(now)
        identity = snapshot_identity(event_id, capture_kind, scheduled_for)
        if any(str(row.get("snapshot_id")) == identity for row in existing):
            continue
        tasks.append(
            CaptureTask(
                game=dict(game),
                capture_kind=capture_kind,
                scheduled_for_utc=scheduled_for,
                snapshot_id=identity,
            )
        )
    return sorted(
        tasks, key=lambda task: parse_time(task.game["commence_time_utc"])
    )


async def collect_due_snapshots(
    games: Iterable[dict[str, Any]],
    snapshots: Iterable[dict[str, Any]],
    now: datetime,
    *,
    event_loader: EventLoader,
    split_loader: SplitLoader,
    target_event_id: str | None = None,
) -> tuple[list[dict[str, Any]], list[dict[str, str]]]:
    """Fetch every due capture while keeping per-game failures isolated."""
    tasks = capture_tasks(games, snapshots, now)
    if target_event_id is not None:
        tasks = [
            task
            for task in tasks
            if str(task.game.get("event_id")) == str(target_event_id)
        ]
    rows: list[dict[str, Any]] = []
    outcomes: list[dict[str, str]] = []
    events_by_date: dict[str, list[dict[str, Any]]] = {}
    for task in tasks:
        event_id = str(task.game["event_id"])
        date = (
            parse_time(task.game["commence_time_utc"])
            .astimezone(ET)
            .date()
            .isoformat()
        )
        try:
            if date not in events_by_date:
                leagues = await event_loader(date)
                events_by_date[date] = list(leagues.get("NFL", []))
            event = match_nfl_game_to_pikkit_event(
                task.game, events_by_date[date]
            )
            if event is None:
                outcomes.append(
                    {
                        "event_id": event_id,
                        "capture_kind": task.capture_kind,
                        "status": "event_not_found",
                    }
                )
                continue
            splits = await split_loader(str(event["event_id"]))
            if not splits:
                outcomes.append(
                    {
                        "event_id": event_id,
                        "capture_kind": task.capture_kind,
                        "status": "splits_unavailable",
                    }
                )
                continue
            normalized = normalize_full_splits(splits)
            if not complete_markets(normalized):
                outcomes.append(
                    {
                        "event_id": event_id,
                        "capture_kind": task.capture_kind,
                        "status": "data_incomplete",
                    }
                )
                continue
            row = build_snapshot_row(task, event, normalized, now)
        except Exception as exc:
            outcomes.append(
                {
                    "event_id": event_id,
                    "capture_kind": task.capture_kind,
                    "status": "error",
                    "error": f"{type(exc).__name__}: {exc}",
                }
            )
            continue
        rows.append(row)
        outcomes.append(
            {
                "event_id": event_id,
                "capture_kind": task.capture_kind,
                "status": "captured",
                "snapshot_id": str(row["snapshot_id"]),
            }
        )
    return rows, outcomes


def _side_fields(
    normalized: dict[str, Any],
    market: str,
    side: str,
) -> dict[str, Any]:
    data = normalized.get(market, {}).get(side, {})
    return {
        "label": data.get("label", ""),
        "bet_pct": data.get("bet_pct", ""),
        "handle_pct": data.get("handle_pct", ""),
        "bets": data.get("bets", ""),
    }


def build_snapshot_row(
    task: CaptureTask,
    event: dict[str, Any],
    splits: dict[str, Any],
    captured_at: datetime,
) -> dict[str, Any]:
    game = task.game
    matched = match_nfl_game_to_pikkit_event(game, [event])
    if matched is None:
        raise ValueError("Pikkit event does not match the NFL game")
    status = str(event.get("status") or "")
    if status not in {"not_started", "scheduled", "pregame"}:
        raise ValueError(f"Pikkit event is not pregame: {status or '<blank>'}")
    normalized = normalize_full_splits(splits)
    source_json = canonical_json(normalized)
    fields = {
        (market, side): _side_fields(normalized, market, side)
        for market, sides in MARKET_SIDES.items()
        for side in sides
    }
    row = {
        "snapshot_id": task.snapshot_id,
        "capture_kind": task.capture_kind,
        "scheduled_for_utc": task.scheduled_for_utc.astimezone(
            timezone.utc
        ).isoformat(),
        "captured_at_utc": captured_at.astimezone(timezone.utc).isoformat(),
        "nfl_event_id": str(game["event_id"]),
        "pikkit_event_id": str(event["event_id"]),
        "season": int(game["season"]),
        "week": int(game["week"]) if str(game.get("week") or "").strip() else "",
        "commence_time_utc": parse_time(game["commence_time_utc"]).isoformat(),
        "away_team": str(game["away_team"]),
        "home_team": str(game["home_team"]),
        "pikkit_status": status,
        "num_picks": normalized["num_picks"],
        "total_wagered": normalized["total_wagered"],
        "source": "pikkit",
        "source_json": source_json,
        "payload_sha256": sha256_text(source_json),
    }
    prefixes = {
        ("moneyline", "home"): "home_ml",
        ("moneyline", "away"): "away_ml",
        ("spread", "home"): "home_spread",
        ("spread", "away"): "away_spread",
        ("total", "over"): "over",
        ("total", "under"): "under",
    }
    for key, prefix in prefixes.items():
        for field, value in fields[key].items():
            row[f"{prefix}_{field}"] = value
    return {header: row.get(header, "") for header in PIKKIT_SNAPSHOT_HEADERS}


def validate_stored_snapshot(row: dict[str, Any]) -> dict[str, Any]:
    source_json = str(row.get("source_json") or "")
    if not source_json:
        raise ValueError("Pikkit snapshot source_json is required")
    if sha256_text(source_json) != str(row.get("payload_sha256") or ""):
        raise ValueError("Pikkit snapshot payload hash mismatch")
    try:
        normalized = normalize_full_splits(json.loads(source_json))
    except json.JSONDecodeError as exc:
        raise ValueError("Invalid Pikkit snapshot source_json") from exc
    if canonical_json(normalized) != source_json:
        raise ValueError("Pikkit snapshot source_json is not canonical")
    expected = snapshot_identity(
        str(row.get("nfl_event_id") or ""),
        str(row.get("capture_kind") or ""),
        parse_time(row.get("scheduled_for_utc")),
    )
    if expected != str(row.get("snapshot_id") or ""):
        raise ValueError("Pikkit snapshot identity mismatch")
    return normalized


def ensure_snapshot_worksheet(
    spreadsheet: gspread.Spreadsheet,
) -> gspread.Worksheet:
    try:
        worksheet = spreadsheet.worksheet(PIKKIT_SNAPSHOTS_TAB)
    except gspread.WorksheetNotFound:
        worksheet = spreadsheet.add_worksheet(
            title=PIKKIT_SNAPSHOTS_TAB,
            rows=2000,
            cols=len(PIKKIT_SNAPSHOT_HEADERS),
        )
        _call_with_retry(
            worksheet.update,
            [PIKKIT_SNAPSHOT_HEADERS],
            "A1",
            value_input_option="RAW",
        )
        return worksheet
    headers = _call_with_retry(worksheet.row_values, 1)
    if headers != PIKKIT_SNAPSHOT_HEADERS:
        raise RuntimeError(
            f"{PIKKIT_SNAPSHOTS_TAB} headers do not match expected schema"
        )
    return worksheet


def load_snapshot_rows(worksheet: gspread.Worksheet) -> list[dict[str, Any]]:
    rows = _call_with_retry(
        worksheet.get_all_records,
        expected_headers=PIKKIT_SNAPSHOT_HEADERS,
        numericise_ignore=["all"],
    )
    for row in rows:
        validate_stored_snapshot(row)
    return rows


def append_snapshot_rows(
    worksheet: gspread.Worksheet,
    rows: Iterable[dict[str, Any]],
) -> int:
    rows = [dict(row) for row in rows]
    if not rows:
        return 0
    existing_ids = set(
        _call_with_retry(
            worksheet.col_values,
            PIKKIT_SNAPSHOT_HEADERS.index("snapshot_id") + 1,
        )[1:]
    )
    pending = []
    for row in rows:
        validate_stored_snapshot(row)
        if str(row["snapshot_id"]) in existing_ids:
            continue
        pending.append([row.get(header, "") for header in PIKKIT_SNAPSHOT_HEADERS])
        existing_ids.add(str(row["snapshot_id"]))
    if pending:
        _call_with_retry(
            worksheet.append_rows,
            pending,
            value_input_option="RAW",
        )
    return len(pending)


def open_snapshot_worksheet(
    credentials_b64: str,
    sheet_id: str,
    *,
    create: bool = False,
) -> gspread.Worksheet:
    spreadsheet = get_gspread_client(credentials_b64).open_by_key(sheet_id)
    if create:
        return ensure_snapshot_worksheet(spreadsheet)
    worksheet = spreadsheet.worksheet(PIKKIT_SNAPSHOTS_TAB)
    headers = _call_with_retry(worksheet.row_values, 1)
    if headers != PIKKIT_SNAPSHOT_HEADERS:
        raise RuntimeError(
            f"{PIKKIT_SNAPSHOTS_TAB} headers do not match expected schema"
        )
    return worksheet


def line_snapshot_market(row: dict[str, Any]) -> dict[str, Any]:
    if list(row) and all(header in row for header in LINE_SNAPSHOT_HEADERS):
        return decode_packed_markets(
            str(row.get(LINE_SNAPSHOT_HEADERS[7]) or ""),
            str(row.get(LINE_SNAPSHOT_HEADERS[8]) or ""),
            str(row.get(LINE_SNAPSHOT_HEADERS[9]) or ""),
        )["game"]
    return decode_packed_markets(
        str(row.get(LATEST_AWAY_COLUMN) or ""),
        str(row.get(LATEST_HOME_COLUMN) or ""),
        str(row.get(LATEST_TOTALS_COLUMN) or ""),
    )["game"]


def snapshots_for_event(
    rows: Iterable[dict[str, Any]],
    event_id: str,
) -> list[dict[str, Any]]:
    selected = [
        dict(row)
        for row in rows
        if str(row.get("nfl_event_id") or "") == str(event_id)
    ]
    for row in selected:
        validate_stored_snapshot(row)
    return sorted(
        selected,
        key=lambda row: (
            parse_time(row["captured_at_utc"]),
            str(row["snapshot_id"]),
        ),
    )


def first_snapshot(
    rows: Iterable[dict[str, Any]], event_id: str
) -> dict[str, Any] | None:
    selected = snapshots_for_event(rows, event_id)
    return selected[0] if selected else None


def latest_snapshot_at_or_before(
    rows: Iterable[dict[str, Any]],
    event_id: str,
    as_of: datetime,
) -> dict[str, Any] | None:
    as_of = as_of.astimezone(timezone.utc)
    selected = [
        row
        for row in snapshots_for_event(rows, event_id)
        if parse_time(row["captured_at_utc"]) <= as_of
    ]
    return selected[-1] if selected else None


def final_snapshot(
    rows: Iterable[dict[str, Any]], event_id: str
) -> dict[str, Any] | None:
    selected = [
        row
        for row in snapshots_for_event(rows, event_id)
        if str(row.get("capture_kind")) == "final_t_minus_2h"
    ]
    if len(selected) > 1:
        raise ValueError(f"Multiple final Pikkit snapshots for event {event_id}")
    return selected[0] if selected else None


def latest_line_snapshot_at_or_before(
    rows: Iterable[dict[str, Any]],
    event_id: str,
    as_of: datetime,
) -> dict[str, Any] | None:
    as_of = as_of.astimezone(timezone.utc)
    selected = []
    for row in rows:
        if str(row.get("event_id") or "") != str(event_id):
            continue
        captured = row.get("captured_at")
        if captured and parse_time(captured) <= as_of:
            selected.append(dict(row))
    return max(
        selected,
        key=lambda row: parse_time(row["captured_at"]),
        default=None,
    )


def american_to_decimal(price: Any) -> float:
    value = float(price)
    if value == 0:
        raise ValueError("American price cannot be zero")
    return 1.0 + (100.0 / abs(value) if value < 0 else value / 100.0)


def american_to_implied(price: Any) -> float:
    value = float(price)
    if value == 0:
        raise ValueError("American price cannot be zero")
    return abs(value) / (abs(value) + 100.0) if value < 0 else 100.0 / (
        value + 100.0
    )


def fair_pair(price_a: Any, price_b: Any) -> dict[str, float]:
    raw_a = american_to_implied(price_a)
    raw_b = american_to_implied(price_b)
    total = raw_a + raw_b
    return {
        "a": round(raw_a / total, 6),
        "b": round(raw_b / total, 6),
        "hold": round(total - 1.0, 6),
    }


def sportsbook_net_scenarios(
    share_a: float,
    share_b: float,
    price_a: Any,
    price_b: Any,
    *,
    outcome_a: str,
    outcome_b: str,
    include_push: bool,
) -> dict[str, Any]:
    decimal_a = american_to_decimal(price_a)
    decimal_b = american_to_decimal(price_b)
    net_a = share_b - share_a * (decimal_a - 1.0)
    net_b = share_a - share_b * (decimal_b - 1.0)
    outcomes = {
        outcome_a: round(net_a, 6),
        outcome_b: round(net_b, 6),
    }
    if include_push:
        outcomes["push"] = 0.0
    best = max(outcomes, key=outcomes.get)
    worst = min(outcomes, key=outcomes.get)
    lower_handle = outcome_a if share_a < share_b else outcome_b
    return {
        "assumption": (
            "Pikkit handle shares are treated as BetOnline handle at the "
            "representative BetOnline prices"
        ),
        "net_per_unit_handle": outcomes,
        "best_outcome": best,
        "worst_outcome": worst,
        "net_gap": round(outcomes[best] - outcomes[worst], 6),
        "lower_handle_proxy": lower_handle,
        "proxy_agrees_with_priced_net": lower_handle == best,
    }


def _representative_score(total: float, home_margin: float) -> dict[str, int]:
    home = max(0, int(round((total + home_margin) / 2.0)))
    away = max(0, int(round(total - home)))
    if away == home:
        if home_margin >= 0:
            home += 1
        else:
            away += 1
    return {"away": away, "home": home}


def _market_analysis(
    splits: dict[str, Any],
    line_market: dict[str, Any],
    market: str,
) -> dict[str, Any] | None:
    if market not in splits:
        return None
    side_a, side_b = MARKET_SIDES[market]
    a = splits[market][side_a]
    b = splits[market][side_b]
    if market == "moneyline":
        price_a = line_market.get("home_moneyline")
        price_b = line_market.get("away_moneyline")
        outcome_a, outcome_b = "home_win", "away_win"
        include_push = False
    elif market == "spread":
        price_a = line_market.get("home_spread_price")
        price_b = line_market.get("away_spread_price")
        outcome_a, outcome_b = "home_cover", "away_cover"
        include_push = True
    else:
        price_a = line_market.get("over_price")
        price_b = line_market.get("under_price")
        outcome_a, outcome_b = "over", "under"
        include_push = True
    result = {
        "sides": {
            side_a: {
                **a,
                "handle_minus_bet_pct": round(
                    float(a["handle_pct"]) - float(a["bet_pct"]), 6
                ),
                "average_ticket_index": (
                    round(float(a["handle_pct"]) / float(a["bet_pct"]), 6)
                    if float(a["bet_pct"]) > 0
                    else None
                ),
            },
            side_b: {
                **b,
                "handle_minus_bet_pct": round(
                    float(b["handle_pct"]) - float(b["bet_pct"]), 6
                ),
                "average_ticket_index": (
                    round(float(b["handle_pct"]) / float(b["bet_pct"]), 6)
                    if float(b["bet_pct"]) > 0
                    else None
                ),
            },
        },
        "majority_bet_side": (
            side_a if float(a["bet_pct"]) > float(b["bet_pct"]) else side_b
        ),
        "majority_handle_side": (
            side_a
            if float(a["handle_pct"]) > float(b["handle_pct"])
            else side_b
        ),
        "market_bets": int(a["bets"]) + int(b["bets"]),
    }
    result["majorities_agree"] = (
        result["majority_bet_side"] == result["majority_handle_side"]
    )
    if price_a not in (None, "") and price_b not in (None, ""):
        result["sportsbook"] = sportsbook_net_scenarios(
            float(a["handle_pct"]),
            float(b["handle_pct"]),
            price_a,
            price_b,
            outcome_a=outcome_a,
            outcome_b=outcome_b,
            include_push=include_push,
        )
    else:
        result["sportsbook"] = None
    return result


def analyze_snapshot(
    snapshot: dict[str, Any],
    line_snapshot: dict[str, Any],
) -> dict[str, Any]:
    splits = validate_stored_snapshot(snapshot)
    if str(line_snapshot.get("event_id") or "") != str(
        snapshot.get("nfl_event_id") or ""
    ):
        raise ValueError("BetOnline snapshot describes a different NFL event")
    line_captured = parse_time(line_snapshot["captured_at"])
    pikkit_captured = parse_time(snapshot["captured_at_utc"])
    if line_captured > pikkit_captured:
        raise ValueError("BetOnline snapshot is later than the Pikkit snapshot")
    market = line_snapshot_market(line_snapshot)
    missing_baseline = [
        field
        for field in (
            "home_moneyline",
            "away_moneyline",
            "home_spread",
            "total",
        )
        if market.get(field) in (None, "")
    ]
    if missing_baseline:
        raise ValueError(
            "BetOnline snapshot is missing baseline fields: "
            + ", ".join(missing_baseline)
        )
    fair_ml = fair_pair(
        market["home_moneyline"], market["away_moneyline"]
    )
    home_margin = -float(market["home_spread"])
    total = float(market["total"])
    return {
        "snapshot_id": str(snapshot["snapshot_id"]),
        "capture_kind": str(snapshot["capture_kind"]),
        "captured_at_utc": pikkit_captured.isoformat(),
        "snapshot_age_seconds": int(
            (pikkit_captured - line_captured).total_seconds()
        ),
        "community": {
            "num_picks": int(splits["num_picks"]),
            "total_wagered": float(splits["total_wagered"]),
        },
        "betonline": {
            "captured_at_utc": line_captured.isoformat(),
            "home_moneyline": market["home_moneyline"],
            "away_moneyline": market["away_moneyline"],
            "home_spread": market["home_spread"],
            "away_spread": market["away_spread"],
            "home_spread_price": market["home_spread_price"],
            "away_spread_price": market["away_spread_price"],
            "total": market["total"],
            "over_price": market["over_price"],
            "under_price": market["under_price"],
        },
        "market_baseline": {
            "home_win_probability": fair_ml["a"],
            "away_win_probability": fair_ml["b"],
            "moneyline_hold": fair_ml["hold"],
            "expected_home_margin": round(home_margin, 2),
            "projected_total": round(total, 2),
            "representative_score": _representative_score(total, home_margin),
        },
        "markets": {
            market_name: analysis
            for market_name in MARKET_SIDES
            if (
                analysis := _market_analysis(splits, market, market_name)
            )
            is not None
        },
    }


def snapshot_movement(
    first: dict[str, Any],
    selected: dict[str, Any],
) -> dict[str, Any]:
    first_splits = validate_stored_snapshot(first)
    selected_splits = validate_stored_snapshot(selected)
    result: dict[str, Any] = {
        "elapsed_seconds": int(
            (
                parse_time(selected["captured_at_utc"])
                - parse_time(first["captured_at_utc"])
            ).total_seconds()
        ),
        "num_picks_change": int(selected_splits["num_picks"])
        - int(first_splits["num_picks"]),
        "total_wagered_change": round(
            float(selected_splits["total_wagered"])
            - float(first_splits["total_wagered"]),
            2,
        ),
        "markets": {},
    }
    for market, sides in MARKET_SIDES.items():
        if market not in first_splits or market not in selected_splits:
            continue
        side_changes = {}
        for side in sides:
            before = first_splits[market][side]
            after = selected_splits[market][side]
            side_changes[side] = {
                "bet_pct_change": round(
                    float(after["bet_pct"]) - float(before["bet_pct"]), 6
                ),
                "handle_pct_change": round(
                    float(after["handle_pct"])
                    - float(before["handle_pct"]),
                    6,
                ),
                "bets_change": int(after["bets"]) - int(before["bets"]),
                "divergence_change": round(
                    (
                        float(after["handle_pct"])
                        - float(after["bet_pct"])
                    )
                    - (
                        float(before["handle_pct"])
                        - float(before["bet_pct"])
                    ),
                    6,
                ),
            }
        before_bet = max(sides, key=lambda side: first_splits[market][side]["bet_pct"])
        after_bet = max(
            sides, key=lambda side: selected_splits[market][side]["bet_pct"]
        )
        before_handle = max(
            sides, key=lambda side: first_splits[market][side]["handle_pct"]
        )
        after_handle = max(
            sides, key=lambda side: selected_splits[market][side]["handle_pct"]
        )
        result["markets"][market] = {
            "sides": side_changes,
            "bet_majority_before": before_bet,
            "bet_majority_after": after_bet,
            "bet_majority_flipped": before_bet != after_bet,
            "handle_majority_before": before_handle,
            "handle_majority_after": after_handle,
            "handle_majority_flipped": before_handle != after_handle,
        }
    return result
