#!/usr/bin/env python3
"""Tests for Sheet-backed human MOE expert identity resolution."""

from __future__ import annotations

import unittest

from moe_identity import resolve_moe_expert_user_id


class MoeIdentityTests(unittest.TestCase):
    def test_resolves_unique_expert_role(self) -> None:
        rows = [
            {
                "display_name": "A K",
                "telegram_id": 123,
                "telegram_username": "@ak",
                "moe_expert_ids": "ak, other_role",
            },
            {
                "display_name": "Cee",
                "telegram_id": 456,
                "telegram_username": "",
                "moe_expert_ids": "cee",
            },
        ]

        self.assertEqual(resolve_moe_expert_user_id(rows, "AK"), "123")
        self.assertEqual(resolve_moe_expert_user_id(rows, "cee"), "456")

    def test_rejects_missing_or_duplicate_role(self) -> None:
        with self.assertRaisesRegex(RuntimeError, "found 0"):
            resolve_moe_expert_user_id([], "ak")
        with self.assertRaisesRegex(RuntimeError, "found 2"):
            resolve_moe_expert_user_id(
                [
                    {"telegram_id": 123, "moe_expert_ids": "ak"},
                    {"telegram_id": 456, "moe_expert_ids": "ak"},
                ],
                "ak",
            )

    def test_rejects_invalid_telegram_id(self) -> None:
        for value in ("", "not-a-number", 0, -1):
            with self.subTest(value=value):
                with self.assertRaisesRegex(RuntimeError, "invalid Telegram ID"):
                    resolve_moe_expert_user_id(
                        [{"telegram_id": value, "moe_expert_ids": "ak"}],
                        "ak",
                    )


if __name__ == "__main__":
    unittest.main()
