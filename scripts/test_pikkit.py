#!/usr/bin/env python3
"""Tests for Pikkit API failure handling."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import pikkit


class _Response:
    def __init__(self, status_code, payload=None, headers=None):
        self.status_code = status_code
        self._payload = payload
        self.headers = headers or {}

    def json(self):
        return self._payload


class _Client:
    def __init__(self, responses):
        self.get = AsyncMock(side_effect=responses)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return None


class FetchSplitsTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        pikkit._splits_cache.clear()

    async def test_retries_rate_limit_then_returns_splits(self):
        payload = {
            "community": {
                "num_picks": 10,
                "total_wagered": 100,
                "breakdowns": {
                    "moneyline": {
                        "home": {
                            "bet_pct": 0.6,
                            "handle_pct": 0.55,
                            "label": "HOME",
                            "bets": 6,
                        },
                        "away": {
                            "bet_pct": 0.4,
                            "handle_pct": 0.45,
                            "label": "AWAY",
                            "bets": 4,
                        },
                    }
                },
            }
        }
        client = _Client(
            [
                _Response(429, headers={"Retry-After": "1"}),
                _Response(200, payload),
            ]
        )
        with (
            patch.dict(pikkit.os.environ, {"PIKKIT_TOKEN": "token"}),
            patch.object(pikkit.httpx, "AsyncClient", return_value=client),
            patch.object(pikkit.asyncio, "sleep", new=AsyncMock()) as sleep,
        ):
            result = await pikkit.fetch_splits("event-1")
        self.assertEqual(result["num_picks"], 10)
        self.assertEqual(client.get.await_count, 2)
        sleep.assert_awaited_once_with(1.0)

    async def test_null_payload_is_an_unavailable_split(self):
        client = _Client([_Response(200, None)])
        with (
            patch.dict(pikkit.os.environ, {"PIKKIT_TOKEN": "token"}),
            patch.object(pikkit.httpx, "AsyncClient", return_value=client),
        ):
            self.assertIsNone(await pikkit.fetch_splits("event-2"))

    async def test_forbidden_event_does_not_report_expired_token(self):
        client = _Client([_Response(403, {})])
        alert = Mock()
        with (
            patch.dict(pikkit.os.environ, {"PIKKIT_TOKEN": "token"}),
            patch.object(pikkit.httpx, "AsyncClient", return_value=client),
            patch.object(pikkit, "_alert_token_expired", alert),
        ):
            self.assertIsNone(await pikkit.fetch_splits("event-3"))
        alert.assert_not_called()


class FetchEventsTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        pikkit._events_cache.clear()

    async def test_forbidden_date_does_not_report_expired_token(self):
        client = _Client([_Response(403, {})])
        alert = Mock()
        with (
            patch.dict(pikkit.os.environ, {"PIKKIT_TOKEN": "token"}),
            patch.object(pikkit.httpx, "AsyncClient", return_value=client),
            patch.object(pikkit, "_alert_token_expired", alert),
        ):
            self.assertEqual(
                await pikkit.fetch_events_for_date("2026-12-01"), {}
            )
        alert.assert_not_called()


if __name__ == "__main__":
    unittest.main()
