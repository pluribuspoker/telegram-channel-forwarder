"""Durable exact-line grades for canonical celebrity NFL picks."""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from moe_celebrity import (
    _enriched_rows,
    _grade,
    _latest_revisions,
    _matching_game,
    _parse_time,
    celebrity_grade_source_sha256,
)


CELEBRITY_GRADE_HEADERS = [
    "grade_id",
    "pick_id",
    "source_sha256",
    "submission_id",
    "event_id",
    "season",
    "week",
    "celebrity_name",
    "canonical_key",
    "submitted_at_utc",
    "commence_time_utc",
    "away_team",
    "home_team",
    "period",
    "market_family",
    "market",
    "subject",
    "stat",
    "direction",
    "line",
    "price",
    "final_away_score",
    "final_home_score",
    "result",
    "graded_at_utc",
]


def _canonical_json(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def _text(value: Any) -> str:
    return "" if value is None else str(value)


def _sha256(value: Any) -> str:
    return hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def _grade_row(
    row: dict[str, Any],
    final: dict[str, Any],
    *,
    graded_at_utc: str,
) -> dict[str, str] | None:
    result = _grade(row, final)
    if result is None:
        return None
    source_sha256 = celebrity_grade_source_sha256(row)
    values = {
        "pick_id": _text(row.get("pick_id")),
        "source_sha256": source_sha256,
        "submission_id": str(row.get("submission_id") or ""),
        "event_id": str(row.get("event_id") or ""),
        "season": str(row.get("season") or ""),
        "week": str(row.get("week") or ""),
        "celebrity_name": str(row.get("celebrity_name") or ""),
        "canonical_key": str(row.get("canonical_key") or ""),
        "submitted_at_utc": str(row.get("submitted_at_utc") or ""),
        "commence_time_utc": str(row.get("commence_time_utc") or ""),
        "away_team": str(row.get("away_team") or ""),
        "home_team": str(row.get("home_team") or ""),
        "period": str(row.get("period") or ""),
        "market_family": str(row.get("market_family") or ""),
        "market": str(row.get("market") or ""),
        "subject": str(row.get("subject") or ""),
        "stat": str(row.get("stat") or ""),
        "direction": str(row.get("direction") or row.get("side") or ""),
        "line": _text(row.get("line")),
        "price": _text(row.get("price")),
        "final_away_score": str(final["away_score"]),
        "final_home_score": str(final["home_score"]),
        "result": result,
        "graded_at_utc": graded_at_utc,
    }
    values["grade_id"] = _sha256(
        {
            key: value
            for key, value in values.items()
            if key not in {"grade_id", "graded_at_utc"}
        }
    )
    return values


def _final_key(final: dict[str, Any]) -> tuple[str, str, str]:
    kickoff = _parse_time(final["kickoff_utc"]).isoformat()
    return (
        str(final.get("away_team") or ""),
        str(final.get("home_team") or ""),
        kickoff,
    )


def _reconciled_history(
    history: Iterable[dict[str, Any]],
    preferred_finals: Iterable[dict[str, Any]],
) -> list[dict[str, Any]]:
    reconciled: dict[tuple[str, str, str], dict[str, Any]] = {}
    for final in history:
        key = _final_key(final)
        previous = reconciled.get(key)
        if previous is not None and (
            str(previous.get("away_score")) != str(final.get("away_score"))
            or str(previous.get("home_score")) != str(final.get("home_score"))
        ):
            raise RuntimeError(
                "Conflicting historical finals for "
                f"{key[0]} at {key[1]} on {key[2]}"
            )
        reconciled[key] = final
    for final in preferred_finals:
        reconciled[_final_key(final)] = final
    return list(reconciled.values())


def build_celebrity_grade_rows(
    celebrity_rows: Iterable[dict[str, Any]],
    leans: Iterable[dict[str, Any]],
    history: Iterable[dict[str, Any]],
    *,
    preferred_finals: Iterable[dict[str, Any]] = (),
    season: int | None = None,
    event_ids: set[str] | None = None,
    graded_at_utc: str | None = None,
) -> list[dict[str, str]]:
    """Grade latest pre-kickoff canonical picks at their stated terms."""
    history = _reconciled_history(history, preferred_finals)
    rows = _latest_revisions(
        row
        for row in _enriched_rows(celebrity_rows, leans)
        if _parse_time(row["submitted_at_utc"])
        < _parse_time(row["commence_time_utc"])
    )
    graded_at_utc = graded_at_utc or datetime.now(timezone.utc).isoformat()
    grades = []
    for row in rows:
        if season is not None and str(row.get("season") or "") != str(season):
            continue
        event_id = str(row.get("event_id") or "")
        if event_ids is not None and event_id not in event_ids:
            continue
        final = _matching_game(row, history)
        if final is None:
            continue
        grade = _grade_row(row, final, graded_at_utc=graded_at_utc)
        if grade is not None:
            grades.append(grade)
    return sorted(
        grades,
        key=lambda row: (
            row["commence_time_utc"],
            row["celebrity_name"],
            row["canonical_key"],
            row["pick_id"],
        ),
    )


class CelebrityPickGradeStore:
    """Append-only grade revisions in the authoritative MOE SQLite database."""

    def __init__(
        self,
        path: str | Path,
        *,
        writable: bool = True,
        initialize: bool = False,
    ) -> None:
        self._path = Path(path).expanduser()
        if not self._path.is_absolute():
            raise ValueError("MOE_SQLITE_PATH must be absolute")
        if not self._path.is_file():
            raise FileNotFoundError(self._path)
        self._writable = writable
        self._validate_moe_database()
        if initialize:
            self._require_writable()
            self._initialize()
        self._initialized = self._validate_schema()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self._path, timeout=30)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA busy_timeout=30000")
        connection.execute("PRAGMA synchronous=FULL")
        return connection

    def _validate_moe_database(self) -> None:
        with self._connect() as connection:
            tables = {
                str(row["name"])
                for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                ).fetchall()
            }
        if "moe_opinions" not in tables or "moe_schema_metadata" not in tables:
            raise RuntimeError("Invalid MOE SQLite database")

    def _initialize(self) -> None:
        columns = ", ".join(
            f'"{header}" TEXT NOT NULL' for header in CELEBRITY_GRADE_HEADERS
        )
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.executescript(
                f"""
                CREATE TABLE IF NOT EXISTS celebrity_pick_grades (
                    _row_order INTEGER PRIMARY KEY AUTOINCREMENT,
                    {columns}
                );
                CREATE INDEX IF NOT EXISTS celebrity_pick_grades_grade
                    ON celebrity_pick_grades(grade_id);
                CREATE INDEX IF NOT EXISTS celebrity_pick_grades_pick
                    ON celebrity_pick_grades(pick_id, _row_order);
                CREATE INDEX IF NOT EXISTS celebrity_pick_grades_event
                    ON celebrity_pick_grades(event_id, _row_order);
                """
            )

    def _validate_schema(self) -> bool:
        with self._connect() as connection:
            columns = [
                str(row["name"])
                for row in connection.execute(
                    "PRAGMA table_info(celebrity_pick_grades)"
                ).fetchall()
            ]
        if not columns:
            return False
        if columns != ["_row_order", *CELEBRITY_GRADE_HEADERS]:
            raise RuntimeError("Invalid celebrity pick grade schema")
        return True

    def _require_writable(self) -> None:
        if not self._writable:
            raise RuntimeError("Celebrity pick grade store is readonly")

    def append_rows(self, rows: Iterable[dict[str, Any]]) -> int:
        self._require_writable()
        if not self._initialized:
            raise RuntimeError("Celebrity pick grade schema is not initialized")
        normalized = [
            {header: _text(row.get(header)) for header in CELEBRITY_GRADE_HEADERS}
            for row in rows
        ]
        if not normalized:
            return 0
        columns = ", ".join(f'"{header}"' for header in CELEBRITY_GRADE_HEADERS)
        placeholders = ", ".join("?" for _ in CELEBRITY_GRADE_HEADERS)
        inserted = 0
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            for row in normalized:
                latest = connection.execute(
                    """
                    SELECT * FROM celebrity_pick_grades
                    WHERE pick_id = ?
                    ORDER BY _row_order DESC
                    LIMIT 1
                    """,
                    (row["pick_id"],),
                ).fetchone()
                if latest is not None and str(latest["grade_id"]) == row["grade_id"]:
                    stored = {
                        header: str(latest[header])
                        for header in CELEBRITY_GRADE_HEADERS
                        if header != "graded_at_utc"
                    }
                    candidate = {
                        header: value
                        for header, value in row.items()
                        if header != "graded_at_utc"
                    }
                    if stored != candidate:
                        raise RuntimeError(
                            f"Conflicting celebrity grade {row['grade_id']}"
                        )
                    continue
                connection.execute(
                    f"INSERT INTO celebrity_pick_grades ({columns})"
                    f" VALUES ({placeholders})",
                    [row[header] for header in CELEBRITY_GRADE_HEADERS],
                )
                inserted += 1
        return inserted

    def list_latest(
        self,
        *,
        event_id: str | None = None,
    ) -> list[dict[str, str]]:
        if not self._initialized:
            return []
        where = "WHERE event_id = ?" if event_id is not None else ""
        params = (event_id,) if event_id is not None else ()
        with self._connect() as connection:
            rows = connection.execute(
                f"""
                SELECT g.*
                FROM celebrity_pick_grades g
                JOIN (
                    SELECT pick_id, MAX(_row_order) AS latest_order
                    FROM celebrity_pick_grades
                    {where}
                    GROUP BY pick_id
                ) latest ON latest.latest_order = g._row_order
                ORDER BY g._row_order
                """,
                params,
            ).fetchall()
        return [
            {
                header: str(row[header])
                for header in CELEBRITY_GRADE_HEADERS
            }
            for row in rows
        ]


def configured_celebrity_grade_store(
    *,
    writable: bool | None = None,
    initialize: bool = False,
) -> CelebrityPickGradeStore:
    backend = os.getenv("MOE_STORAGE_BACKEND", "sheets").strip().lower()
    if backend != "sqlite":
        raise RuntimeError("Celebrity pick grades require SQLite MOE storage")
    path = os.getenv("MOE_SQLITE_PATH", "").strip()
    if not path:
        raise ValueError("MOE_SQLITE_PATH is required for celebrity pick grades")
    role = os.getenv("MOE_STORAGE_ROLE", "primary").strip().lower()
    if role not in {"primary", "readonly"}:
        raise ValueError(f"Unsupported MOE_STORAGE_ROLE: {role}")
    role_writable = role == "primary"
    if writable is None:
        writable = role_writable
    elif writable and not role_writable:
        raise RuntimeError("Celebrity pick grade store is readonly")
    return CelebrityPickGradeStore(
        path,
        writable=writable,
        initialize=initialize,
    )
