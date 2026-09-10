#!/usr/bin/env python3
"""Tests for durable celebrity source-pick grades."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from celebrity_grades import (
    CelebrityPickGradeStore,
    build_celebrity_grade_rows,
)
from celebrity_picks import build_celebrity_rows
from moe import SQLiteMoeOpinionStore


EVENT_ID = "patriots-seahawks"
AWAY = "New England Patriots"
HOME = "Seattle Seahawks"
KICKOFF = "2026-09-10T00:20:00+00:00"


def _pick(
    *,
    submission_id: str,
    celebrity: str,
    submitted_at: str,
    market: str,
    side: str,
    line: float | str,
) -> dict:
    return build_celebrity_rows(
        submission={
            "submission_id": submission_id,
            "submitted_at_utc": submitted_at,
            "event_id": EVENT_ID,
            "season": 2026,
            "week": 1,
            "commence_time_utc": KICKOFF,
            "away_team": AWAY,
            "home_team": HOME,
            "period": "game",
            "market": market,
            "side": side,
            "latest_selected_line": line,
            "latest_selected_price": -110,
            "raw_pick_text": "Exact source pick.",
        },
        names=[celebrity],
    )[0]


def _final() -> dict:
    return {
        "event_id": EVENT_ID,
        "kickoff_utc": KICKOFF,
        "away_team": AWAY,
        "home_team": HOME,
        "away_score": 10,
        "home_score": 13,
        "completed": True,
    }


class GradeRowsTest(unittest.TestCase):
    def test_grades_each_pick_at_its_exact_stated_terms(self) -> None:
        rows = [
            _pick(
                submission_id="anthony",
                celebrity="Anthony Dabbundo",
                submitted_at="2026-09-09T18:00:00+00:00",
                market="spread",
                side=AWAY,
                line=3.5,
            ),
            _pick(
                submission_id="cblez",
                celebrity="Cblez",
                submitted_at="2026-09-09T18:01:00+00:00",
                market="spread",
                side=HOME,
                line=-3,
            ),
            _pick(
                submission_id="bill",
                celebrity="Bill Simmons",
                submitted_at="2026-09-09T18:02:00+00:00",
                market="moneyline",
                side=HOME,
                line="",
            ),
        ]

        grades = build_celebrity_grade_rows(
            rows,
            [],
            [_final()],
            season=2026,
            graded_at_utc="2026-09-10T05:00:00+00:00",
        )

        by_name = {row["celebrity_name"]: row for row in grades}
        self.assertEqual(by_name["Anthony Dabbundo"]["line"], "3.5")
        self.assertEqual(by_name["Anthony Dabbundo"]["result"], "W")
        self.assertEqual(by_name["Cblez"]["line"], "-3")
        self.assertEqual(by_name["Cblez"]["result"], "P")
        self.assertEqual(by_name["Bill Simmons"]["result"], "W")

    def test_only_latest_prekickoff_revision_is_graded(self) -> None:
        rows = [
            _pick(
                submission_id="old",
                celebrity="Bill Simmons",
                submitted_at="2026-09-09T17:00:00+00:00",
                market="spread",
                side=HOME,
                line=-3,
            ),
            _pick(
                submission_id="new",
                celebrity="Bill Simmons",
                submitted_at="2026-09-09T19:00:00+00:00",
                market="spread",
                side=AWAY,
                line=3.5,
            ),
            _pick(
                submission_id="post",
                celebrity="Cblez",
                submitted_at="2026-09-10T01:00:00+00:00",
                market="spread",
                side=AWAY,
                line=3.5,
            ),
        ]

        grades = build_celebrity_grade_rows(rows, [], [_final()])

        self.assertEqual(len(grades), 1)
        self.assertEqual(grades[0]["submission_id"], "new")
        self.assertEqual(grades[0]["result"], "W")

    def test_preferred_final_replaces_duplicate_history_final(self) -> None:
        row = _pick(
            submission_id="anthony",
            celebrity="Anthony Dabbundo",
            submitted_at="2026-09-09T18:00:00+00:00",
            market="spread",
            side=AWAY,
            line=3.5,
        )
        stale = {**_final(), "home_score": 14}

        grades = build_celebrity_grade_rows(
            [row],
            [],
            [stale],
            preferred_finals=[_final()],
        )

        self.assertEqual(len(grades), 1)
        self.assertEqual(grades[0]["final_home_score"], "13")
        self.assertEqual(grades[0]["result"], "W")

    def test_conflicting_history_finals_fail_closed(self) -> None:
        with self.assertRaisesRegex(RuntimeError, "Conflicting historical finals"):
            build_celebrity_grade_rows(
                [],
                [],
                [_final(), {**_final(), "home_score": 14}],
            )

    def test_unsupported_moneyline_and_team_stat_remain_ungraded(self) -> None:
        invalid_moneyline = _pick(
            submission_id="draw",
            celebrity="Bill Simmons",
            submitted_at="2026-09-09T18:00:00+00:00",
            market="moneyline",
            side="Draw",
            line="",
        )
        turnovers = build_celebrity_rows(
            submission={
                "submission_id": "turnovers",
                "submitted_at_utc": "2026-09-09T18:01:00+00:00",
                "event_id": EVENT_ID,
                "season": 2026,
                "week": 1,
                "commence_time_utc": KICKOFF,
                "away_team": AWAY,
                "home_team": HOME,
                "period": "game",
                "market_family": "team_prop",
                "market": "prop",
                "subject": HOME,
                "stat": "Turnovers",
                "direction": "Over",
                "line": 1.5,
                "price": -110,
            },
            names=["Cblez"],
        )[0]

        grades = build_celebrity_grade_rows(
            [invalid_moneyline, turnovers],
            [],
            [_final()],
        )

        self.assertEqual(grades, [])


class GradeStoreTest(unittest.TestCase):
    def test_append_is_idempotent_and_latest_revision_wins(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "moe.sqlite3"
            SQLiteMoeOpinionStore(path, create=True)
            store = CelebrityPickGradeStore(path, initialize=True)
            first = build_celebrity_grade_rows(
                [
                    _pick(
                        submission_id="anthony",
                        celebrity="Anthony Dabbundo",
                        submitted_at="2026-09-09T18:00:00+00:00",
                        market="spread",
                        side=AWAY,
                        line=3.5,
                    )
                ],
                [],
                [_final()],
                graded_at_utc="2026-09-10T05:00:00+00:00",
            )[0]

            self.assertEqual(store.append_rows([first]), 1)
            self.assertEqual(store.append_rows([first]), 0)
            self.assertEqual(store.list_latest(), [first])

            corrected = dict(first)
            corrected["line"] = "3"
            corrected["result"] = "P"
            corrected["grade_id"] = "corrected-grade"
            corrected["source_sha256"] = "corrected-source"
            self.assertEqual(store.append_rows([corrected]), 1)
            self.assertEqual(store.list_latest(), [corrected])
            reverted = {**first, "graded_at_utc": "2026-09-10T07:00:00+00:00"}
            self.assertEqual(store.append_rows([reverted]), 1)
            self.assertEqual(store.list_latest(), [reverted])

    def test_refuses_unrelated_database_and_readonly_write(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            unrelated = Path(directory) / "unrelated.sqlite3"
            unrelated.touch()
            with self.assertRaisesRegex(RuntimeError, "Invalid MOE SQLite"):
                CelebrityPickGradeStore(unrelated, initialize=True)

            path = Path(directory) / "moe.sqlite3"
            SQLiteMoeOpinionStore(path, create=True)
            CelebrityPickGradeStore(path, initialize=True)
            readonly = CelebrityPickGradeStore(path, writable=False)
            with self.assertRaisesRegex(RuntimeError, "readonly"):
                readonly.append_rows([])


if __name__ == "__main__":
    unittest.main()
