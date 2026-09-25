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
        pikkit._session_state = None

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


class DeadSessionTests(unittest.IsolatedAsyncioTestCase):
    """An events 403 probes /login/validate once and alerts only on a dead session."""

    def setUp(self):
        pikkit._events_cache.clear()
        pikkit._session_state = None

    async def test_dead_session_alerts_once_and_caches_the_miss(self):
        client = _Client([_Response(403, {}), _Response(403, {})])
        alert = Mock()
        with (
            patch.dict(pikkit.os.environ, {"PIKKIT_TOKEN": "token"}),
            patch.object(pikkit.httpx, "AsyncClient", return_value=client),
            patch.object(pikkit, "_alert_session_dead", alert),
        ):
            self.assertEqual(await pikkit.fetch_events_for_date("2026-12-01"), {})
            self.assertEqual(await pikkit.fetch_events_for_date("2026-12-01"), {})
        alert.assert_called_once()
        self.assertEqual(client.get.await_count, 2)  # events + validate, then cache
        self.assertIs(pikkit._session_state, False)

    async def test_live_session_forbidden_date_does_not_alert(self):
        client = _Client([_Response(403, {}), _Response(200, {})])
        alert = Mock()
        with (
            patch.dict(pikkit.os.environ, {"PIKKIT_TOKEN": "token"}),
            patch.object(pikkit.httpx, "AsyncClient", return_value=client),
            patch.object(pikkit, "_alert_session_dead", alert),
        ):
            self.assertEqual(await pikkit.fetch_events_for_date("2026-12-02"), {})
        alert.assert_not_called()
        self.assertIs(pikkit._session_state, True)

    def test_alert_throttle_survives_a_new_process(self):
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            stamp = Path(tmp) / "logs" / "pikkit_token_alert.ts"
            sent = Mock()
            with (
                patch.dict(pikkit.os.environ, {"WATCHDOG_BOT_TOKEN": "t", "WATCHDOG_USER_ID": "1"}),
                patch.object(pikkit, "_ALERT_STAMP", stamp),
                patch.object(pikkit.urllib.request, "urlopen", sent),
            ):
                pikkit._last_401_alert = 0
                self.assertTrue(pikkit._send_token_alert("first"))
                pikkit._last_401_alert = 0  # a fresh process has no in-memory guard
                self.assertFalse(pikkit._send_token_alert("second"))
                stamp.write_text(str(pikkit.time.time() - pikkit._ALERT_INTERVAL - 1))
                pikkit._last_401_alert = 0
                self.assertTrue(pikkit._send_token_alert("third"))
            self.assertEqual(sent.call_count, 2)


if __name__ == "__main__":
    unittest.main()
