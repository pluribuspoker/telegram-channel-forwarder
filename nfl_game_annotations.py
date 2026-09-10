"""Append-only annotations for unusual events affecting completed NFL games."""

from __future__ import annotations

import json
from collections import defaultdict
from typing import Any, Iterable

from gspread.exceptions import WorksheetNotFound


GAME_ANNOTATIONS_TAB = "nfl_game_annotations"
GAME_ANNOTATION_HEADERS = [
    "annotation_id",
    "event_id",
    "annotation_type",
    "severity",
    "period",
    "game_clock",
    "team",
    "subject",
    "summary",
    "affected_scopes_json",
    "default_treatment",
    "source",
    "source_reference",
    "created_at_utc",
    "created_by",
    "review_status",
    "reviewed_at_utc",
    "reviewed_by",
    "supersedes_annotation_id",
]
SEVERITIES = {"minor", "major", "critical"}
TREATMENTS = {"flag_only", "include", "exclude", "downweight", "report_both"}
REVIEW_STATUSES = {"pending", "approved", "rejected"}


def _string(value: Any) -> str:
    return str(value or "").strip()


def _affected_scopes(value: Any) -> list[str]:
    if isinstance(value, list):
        raw = value
    else:
        text = _string(value)
        if not text:
            return []
        try:
            raw = json.loads(text)
        except json.JSONDecodeError as exc:
            raise ValueError("affected_scopes_json must be a JSON list") from exc
    if not isinstance(raw, list) or not all(
        isinstance(item, str) and item.strip() for item in raw
    ):
        raise ValueError("affected_scopes_json must contain non-empty strings")
    return list(dict.fromkeys(item.strip() for item in raw))


def normalize_game_annotation(row: dict[str, Any]) -> dict[str, Any]:
    annotation = {
        header: _string(row.get(header)) for header in GAME_ANNOTATION_HEADERS
    }
    required = (
        "annotation_id",
        "event_id",
        "annotation_type",
        "severity",
        "summary",
        "default_treatment",
        "source",
        "created_at_utc",
        "created_by",
        "review_status",
    )
    missing = [field for field in required if not annotation[field]]
    if missing:
        raise ValueError(
            "Game annotation is missing required fields: " + ", ".join(missing)
        )
    if annotation["severity"] not in SEVERITIES:
        raise ValueError(
            f"Unsupported game annotation severity: {annotation['severity']}"
        )
    if annotation["default_treatment"] not in TREATMENTS:
        raise ValueError(
            "Unsupported game annotation treatment: "
            f"{annotation['default_treatment']}"
        )
    if annotation["review_status"] not in REVIEW_STATUSES:
        raise ValueError(
            "Unsupported game annotation review status: "
            f"{annotation['review_status']}"
        )
    annotation["affected_scopes"] = _affected_scopes(
        row.get("affected_scopes_json")
    )
    annotation["affected_scopes_json"] = json.dumps(
        annotation["affected_scopes"],
        separators=(",", ":"),
    )
    return annotation


def active_approved_annotations(
    rows: Iterable[dict[str, Any]],
) -> list[dict[str, Any]]:
    normalized = [normalize_game_annotation(row) for row in rows]
    ids = [row["annotation_id"] for row in normalized]
    if len(ids) != len(set(ids)):
        raise ValueError("Duplicate game annotation_id")
    by_id = {row["annotation_id"]: row for row in normalized}
    approved = [row for row in normalized if row["review_status"] == "approved"]
    superseded: set[str] = set()
    for row in approved:
        supersedes = row["supersedes_annotation_id"]
        if not supersedes:
            continue
        target = by_id.get(supersedes)
        if target is None:
            raise ValueError(
                f"Unknown supersedes_annotation_id: {supersedes}"
            )
        if target["event_id"] != row["event_id"]:
            raise ValueError(
                "A game annotation can only supersede an annotation for the "
                "same event_id"
            )
        superseded.add(supersedes)
    return [row for row in approved if row["annotation_id"] not in superseded]


def public_game_annotation(row: dict[str, Any]) -> dict[str, Any]:
    normalized = normalize_game_annotation(row)
    return {
        "annotation_id": normalized["annotation_id"],
        "annotation_type": normalized["annotation_type"],
        "severity": normalized["severity"],
        "period": normalized["period"],
        "game_clock": normalized["game_clock"],
        "team": normalized["team"],
        "subject": normalized["subject"],
        "summary": normalized["summary"],
        "affected_scopes": normalized["affected_scopes"],
        "default_treatment": normalized["default_treatment"],
        "source": normalized["source"],
        "source_reference": normalized["source_reference"],
    }


def attach_game_annotations(
    games: Iterable[dict[str, Any]],
    annotation_rows: Iterable[dict[str, Any]],
) -> list[dict[str, Any]]:
    by_event: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for annotation in active_approved_annotations(annotation_rows):
        by_event[annotation["event_id"]].append(
            public_game_annotation(annotation)
        )
    attached: list[dict[str, Any]] = []
    for original in games:
        game = dict(original)
        annotations = sorted(
            by_event.get(_string(game.get("event_id")), []),
            key=lambda row: row["annotation_id"],
        )
        game["game_annotations"] = annotations
        game["has_major_annotation"] = any(
            row["severity"] in {"major", "critical"} for row in annotations
        )
        attached.append(game)
    return attached


def game_annotation_context(
    games: Iterable[dict[str, Any]],
    *,
    deterministic_treatment: str,
) -> dict[str, Any]:
    annotated_by_event: dict[str, dict[str, Any]] = {}
    for game in games:
        annotations = list(game.get("game_annotations") or [])
        if not annotations:
            continue
        event_id = _string(game.get("event_id"))
        annotated_by_event[event_id] = {
            "event_id": event_id,
            "season": game.get("season"),
            "week": game.get("week"),
            "away_team": _string(game.get("away_team")),
            "home_team": _string(game.get("home_team")),
            "away_score": game.get("away_score"),
            "home_score": game.get("home_score"),
            "has_major_annotation": bool(game.get("has_major_annotation")),
            "annotations": annotations,
        }
    return {
        "official_result_policy": (
            "Scores and prediction grading remain official and unchanged."
        ),
        "calibration_policy": (
            "Consumers may include, exclude, downweight, or report both when "
            "an annotation is relevant to their stated lens, but must state "
            "the treatment used."
        ),
        "agent_treatment": "consumer_decides",
        "deterministic_treatment": deterministic_treatment,
        "annotated_games": sorted(
            annotated_by_event.values(),
            key=lambda row: (
                int(row.get("season") or 0),
                int(row.get("week") or 0),
                row["event_id"],
            ),
        ),
    }


def with_game_annotation_context(
    payload: dict[str, Any],
    games: Iterable[dict[str, Any]],
    *,
    deterministic_treatment: str,
) -> dict[str, Any]:
    enriched = dict(payload)
    enriched["game_annotation_context"] = game_annotation_context(
        games,
        deterministic_treatment=deterministic_treatment,
    )
    return enriched


def ensure_game_annotations_worksheet(spreadsheet: Any) -> Any:
    try:
        worksheet = spreadsheet.worksheet(GAME_ANNOTATIONS_TAB)
    except WorksheetNotFound:
        worksheet = spreadsheet.add_worksheet(
            title=GAME_ANNOTATIONS_TAB,
            rows=1000,
            cols=len(GAME_ANNOTATION_HEADERS),
        )
        worksheet.update([GAME_ANNOTATION_HEADERS])
        return worksheet
    header = worksheet.row_values(1)
    if not header:
        worksheet.resize(cols=len(GAME_ANNOTATION_HEADERS))
        worksheet.update([GAME_ANNOTATION_HEADERS])
    elif header != GAME_ANNOTATION_HEADERS:
        raise RuntimeError(
            "nfl_game_annotations headers do not match the finalized schema"
        )
    return worksheet


def load_game_annotations(spreadsheet: Any) -> list[dict[str, Any]]:
    worksheet = ensure_game_annotations_worksheet(spreadsheet)
    rows = worksheet.get_all_records(
        expected_headers=GAME_ANNOTATION_HEADERS
    )
    active_approved_annotations(rows)
    return rows


def append_game_annotation(spreadsheet: Any, row: dict[str, Any]) -> bool:
    normalized = normalize_game_annotation(row)
    worksheet = ensure_game_annotations_worksheet(spreadsheet)
    existing = set(
        worksheet.col_values(GAME_ANNOTATION_HEADERS.index("annotation_id") + 1)[
            1:
        ]
    )
    if normalized["annotation_id"] in existing:
        return False
    worksheet.append_row(
        [normalized.get(header, "") for header in GAME_ANNOTATION_HEADERS],
        value_input_option="RAW",
    )
    return True
