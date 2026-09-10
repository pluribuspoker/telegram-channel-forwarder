#!/usr/bin/env python3
"""Tests for append-only NFL game annotations."""

from __future__ import annotations

import unittest

from nfl_game_annotations import (
    active_approved_annotations,
    attach_game_annotations,
    game_annotation_context,
    normalize_game_annotation,
)
from scripts.moe_grade import notification_text


def _annotation(**overrides) -> dict:
    row = {
        "annotation_id": "annotation-1",
        "event_id": "event-1",
        "annotation_type": "major_in_game_injury",
        "severity": "major",
        "period": "first_quarter",
        "game_clock": "",
        "team": "Seattle Seahawks",
        "subject": "starting quarterback",
        "summary": (
            "Seahawks starting quarterback injury in first quarter. "
            "Seahawks offense tarnished, constantly punting"
        ),
        "affected_scopes_json": '["side","total","team_performance"]',
        "default_treatment": "flag_only",
        "source": "operator",
        "source_reference": "",
        "created_at_utc": "2026-09-10T01:20:00+00:00",
        "created_by": "6780239459",
        "review_status": "approved",
        "reviewed_at_utc": "2026-09-10T01:20:00+00:00",
        "reviewed_by": "6780239459",
        "supersedes_annotation_id": "",
    }
    row.update(overrides)
    return row


class GameAnnotationTest(unittest.TestCase):
    def test_normalizes_affected_scopes(self) -> None:
        annotation = normalize_game_annotation(_annotation())

        self.assertEqual(
            annotation["affected_scopes"],
            ["side", "total", "team_performance"],
        )

    def test_approved_correction_supersedes_original(self) -> None:
        correction = _annotation(
            annotation_id="annotation-2",
            summary="Corrected summary",
            supersedes_annotation_id="annotation-1",
        )

        active = active_approved_annotations([_annotation(), correction])

        self.assertEqual(
            [row["annotation_id"] for row in active],
            ["annotation-2"],
        )

    def test_pending_correction_does_not_supersede_approved_original(self) -> None:
        correction = _annotation(
            annotation_id="annotation-2",
            review_status="pending",
            reviewed_at_utc="",
            reviewed_by="",
            supersedes_annotation_id="annotation-1",
        )

        active = active_approved_annotations([_annotation(), correction])

        self.assertEqual(
            [row["annotation_id"] for row in active],
            ["annotation-1"],
        )

    def test_correction_cannot_supersede_another_game(self) -> None:
        correction = _annotation(
            annotation_id="annotation-2",
            event_id="event-2",
            supersedes_annotation_id="annotation-1",
        )

        with self.assertRaisesRegex(ValueError, "same event_id"):
            active_approved_annotations([_annotation(), correction])

    def test_attaches_annotation_and_builds_consumer_context(self) -> None:
        games = attach_game_annotations(
            [
                {
                    "event_id": "event-1",
                    "season": 2026,
                    "week": 1,
                    "away_team": "New England Patriots",
                    "home_team": "Seattle Seahawks",
                    "away_score": 20,
                    "home_score": 23,
                }
            ],
            [_annotation()],
        )

        self.assertTrue(games[0]["has_major_annotation"])
        context = game_annotation_context(
            games,
            deterministic_treatment="include",
        )
        self.assertEqual(context["deterministic_treatment"], "include")
        self.assertEqual(
            context["annotated_games"][0]["annotations"][0]["summary"],
            _annotation()["summary"],
        )

    def test_rejects_unreviewed_schema_values(self) -> None:
        with self.assertRaisesRegex(ValueError, "severity"):
            normalize_game_annotation(_annotation(severity="huge"))

    def test_grade_notification_marks_annotated_final(self) -> None:
        finals = attach_game_annotations(
            [
                {
                    "event_id": "event-1",
                    "away_team": "New England Patriots",
                    "home_team": "Seattle Seahawks",
                }
            ],
            [_annotation()],
        )
        text = notification_text(
            season=2026,
            scoreboard={
                "resolved_games": 1,
                "graded_opinions": 1,
                "by_expert": {},
            },
            new_rows=[
                {
                    "expert_id": "cee",
                    "event_id": "event-1",
                    "away_team": "New England Patriots",
                    "home_team": "Seattle Seahawks",
                    "final": "20-23",
                }
            ],
            finals=finals,
        )

        self.assertIn(
            "🏈 Patriots 20 @ Seahawks 23*",
            text,
        )
        self.assertIn(f"* {_annotation()['summary']}", text)


if __name__ == "__main__":
    unittest.main()
