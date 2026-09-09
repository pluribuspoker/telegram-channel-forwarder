#!/usr/bin/env python3
"""Tests for the dedicated MOE Telegram runtime."""

from __future__ import annotations

import sys
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import moe_bot
from moe import approved_opinions


class FakeOpinionStore:
    def __init__(self, rows):
        self.rows = [dict(row) for row in rows]
        self.reviews: list[tuple[str, str, str]] = []

    def list(self, event_id=None):
        return [dict(row) for row in self.rows]

    def fetch(self, opinion_id):
        for row in self.rows:
            if row["opinion_id"] == opinion_id:
                return dict(row)
        return None

    def review(self, opinion_id, *, status, reviewed_by, note):
        for row in self.rows:
            if row["opinion_id"] == opinion_id:
                row["review_status"] = status
                row["reviewed_by"] = reviewed_by
                self.reviews.append((opinion_id, status, reviewed_by))
                return
        raise ValueError("Expected one opinion_id match, found 0")


def desk_row(opinion_id, expert_id, name, *, status="pending", **extra):
    row = {
        "opinion_id": opinion_id,
        "event_id": "401",
        "expert_id": expert_id,
        "expert_name": name,
        "generated_at_utc": "2026-09-12T12:00:00+00:00",
        "generation_status": "valid",
        "review_status": status,
        "reviewed_by": "SS" if status != "pending" else "",
    }
    row.update(extra)
    return row


class DeskReviewTest(unittest.TestCase):
    def setUp(self) -> None:
        moe_bot._CACHE.clear()
        self.store = FakeOpinionStore(
            [
                desk_row("p1", "win_total", "Win Total Expert"),
                desk_row(
                    "a1",
                    "schedule",
                    "Schedule Expert",
                    status="approved",
                ),
                desk_row("r1", "god_rules", "God Expert (Rules)"),
                desk_row("j1", "god_judge", "God Expert (Judge)"),
                desk_row(
                    "x1",
                    "schedule",
                    "Schedule Expert",
                    generation_status="invalid",
                ),
            ]
        )
        moe_bot._MOE_STORE = self.store

    def tearDown(self) -> None:
        moe_bot._CACHE.clear()
        moe_bot._MOE_STORE = None

    def test_approve_and_reject_sign_with_the_reviewer(self) -> None:
        self.assertEqual(
            moe_bot.review("ok", "p1", reviewer="AK"),
            ("Approved Win Total Expert as AK.", True),
        )
        self.assertEqual(self.store.reviews, [("p1", "approved", "AK")])
        self.assertEqual(
            moe_bot.review("ok", "p1", reviewer="SS"),
            ("Already approved by AK.", False),
        )
        self.assertEqual(
            moe_bot.review("no", "a1", reviewer="SS"),
            ("Already approved by SS.", False),
        )
        self.assertFalse(moe_bot.review("no", "x1", reviewer="SS")[1])
        self.assertEqual(len(self.store.reviews), 1)

    def test_approval_patches_cache_for_immediate_refresh(self) -> None:
        moe_bot.review("ok", "p1", reviewer="AK")
        cached = moe_bot.load_opinions()
        row = next(item for item in cached if item["opinion_id"] == "p1")
        self.assertEqual(
            (row["review_status"], row["reviewed_by"]),
            ("approved", "AK"),
        )
        self.assertTrue(row["reviewed_at_utc"])
        self.assertIn(
            "p1",
            [item["opinion_id"] for item in approved_opinions(cached)],
        )

    def test_other_reviewers_prior_write_wins(self) -> None:
        moe_bot.load_opinions()
        self.store.rows[0]["review_status"] = "approved"
        self.store.rows[0]["reviewed_by"] = "SS"
        self.assertEqual(
            moe_bot.review("ok", "p1", reviewer="AK"),
            ("Already approved by SS.", False),
        )
        self.assertEqual(self.store.reviews, [])
        cached = next(
            item
            for item in moe_bot.load_opinions()
            if item["opinion_id"] == "p1"
        )
        self.assertEqual(cached["reviewed_by"], "SS")

    def test_approve_both_arms_reviews_rules_then_judge(self) -> None:
        text, ok = moe_bot.review("okarms", "401", reviewer="SS")
        self.assertTrue(ok)
        self.assertEqual(
            text,
            "Approved God Expert (Rules), God Expert (Judge) as SS.",
        )
        self.assertEqual(
            [item[0] for item in self.store.reviews],
            ["r1", "j1"],
        )
        self.assertEqual(
            moe_bot.review("okarms", "401", reviewer="SS"),
            ("Both arms are no longer pending.", False),
        )

    def test_store_refusal_propagates(self) -> None:
        self.store.rows.append(
            desk_row("ghost", "schedule", "Schedule Expert")
        )
        original = self.store.review

        def refuse(opinion_id, **kwargs):
            raise ValueError(
                "Opinion content changed after generation; review refused"
            )

        self.store.review = refuse
        with self.assertRaisesRegex(ValueError, "review refused"):
            moe_bot.review("ok", "ghost", reviewer="SS")
        self.store.review = original

    def test_opposite_taps_on_one_row_cannot_overwrite_each_other(self) -> None:
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(
                pool.map(
                    lambda args: moe_bot.review(*args[0], reviewer=args[1]),
                    [(("ok", "p1"), "AK"), (("no", "p1"), "SS")],
                )
            )
        self.assertEqual(len(self.store.reviews), 1)
        self.assertEqual(sum(1 for _, ok in results if ok), 1)
        self.assertEqual(sum(1 for _, ok in results if not ok), 1)


if __name__ == "__main__":
    unittest.main()
