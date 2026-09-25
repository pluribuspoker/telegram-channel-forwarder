#!/usr/bin/env python3
"""Tests for NFL Pikkit snapshot validation, matching, and scheduling."""

from __future__ import annotations

import sys
import unittest
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from nfl_pikkit import (
    CaptureTask,
    analyze_snapshot,
    build_snapshot_row,
    capture_tasks,
    collect_due_snapshots,
    complete_markets,
    latest_line_snapshot_at_or_before,
    latest_snapshot_at_or_before,
    match_nfl_game_to_pikkit_event,
    normalize_full_splits,
    snapshot_movement,
    sportsbook_net_scenarios,
    validate_stored_snapshot,
)


def game() -> dict:
    return {
        "event_id": "nfl-1",
        "season": 2026,
        "week": 1,
        "status": "upcoming",
        "commence_time_utc": "2026-09-13T17:00:00+00:00",
        "away_team": "Chicago Bears",
        "home_team": "Carolina Panthers",
    }


def event() -> dict:
    return {
        "event_id": "pikkit-1",
        "away_full": "Chicago Bears",
        "home_full": "Carolina Panthers",
        "start_time": "2026-09-13T17:00:00.000Z",
        "status": "not_started",
    }


def splits() -> dict:
    return {
        "num_picks": 1000,
        "total_wagered": 125000.5,
        "moneyline": {
            "home": {
                "bet_pct": 0.25,
                "handle_pct": 0.2,
                "label": "CAR",
                "bets": 100,
            },
            "away": {
                "bet_pct": 0.75,
                "handle_pct": 0.8,
                "label": "CHI",
                "bets": 300,
            },
        },
        "spread": {
            "home": {
                "bet_pct": 0.6,
                "handle_pct": 0.55,
                "label": "CAR",
                "bets": 240,
            },
            "away": {
                "bet_pct": 0.4,
                "handle_pct": 0.45,
                "label": "CHI",
                "bets": 160,
            },
        },
        "total": {
            "over": {
                "bet_pct": 0.45,
                "handle_pct": 0.35,
                "label": "OVER",
                "bets": 90,
            },
            "under": {
                "bet_pct": 0.55,
                "handle_pct": 0.65,
                "label": "UNDER",
                "bets": 110,
            },
        },
    }


class SplitValidationTests(unittest.TestCase):
    def test_normalizes_complete_markets(self):
        normalized = normalize_full_splits(splits())

        self.assertEqual(
            complete_markets(normalized), ["moneyline", "spread", "total"]
        )
        self.assertEqual(normalized["moneyline"]["away"]["bets"], 300)
        self.assertEqual(normalized["total_wagered"], 125000.5)

    def test_rejects_one_sided_market(self):
        value = splits()
        del value["spread"]["away"]

        with self.assertRaisesRegex(ValueError, "both opposing sides"):
            normalize_full_splits(value)

    def test_rejects_percentages_that_do_not_sum_to_one(self):
        value = splits()
        value["total"]["under"]["bet_pct"] = 0.4

        with self.assertRaisesRegex(ValueError, "do not sum to 1"):
            normalize_full_splits(value)


class EventMatchingTests(unittest.TestCase):
    def test_requires_exact_oriented_pair_and_eastern_date(self):
        self.assertEqual(
            match_nfl_game_to_pikkit_event(game(), [event()])["event_id"],
            "pikkit-1",
        )

        reversed_event = {
            **event(),
            "away_full": "Carolina Panthers",
            "home_full": "Chicago Bears",
        }
        self.assertIsNone(
            match_nfl_game_to_pikkit_event(game(), [reversed_event])
        )

        next_day = {**event(), "start_time": "2026-09-14T17:00:00Z"}
        self.assertIsNone(match_nfl_game_to_pikkit_event(game(), [next_day]))


class SchedulingTests(unittest.TestCase):
    def test_first_capture_is_current_baseline_bucket(self):
        tasks = capture_tasks(
            [game()],
            [],
            datetime(2026, 9, 11, 17, 5, tzinfo=timezone.utc),
        )

        self.assertEqual(len(tasks), 1)
        self.assertEqual(tasks[0].capture_kind, "baseline")
        self.assertEqual(
            tasks[0].scheduled_for_utc,
            datetime(2026, 9, 11, 12, 0, tzinfo=timezone.utc),
        )

    def test_same_baseline_bucket_is_idempotent(self):
        first = capture_tasks(
            [game()],
            [],
            datetime(2026, 9, 11, 17, 5, tzinfo=timezone.utc),
        )[0]
        tasks = capture_tasks(
            [game()],
            [
                {
                    "nfl_event_id": "nfl-1",
                    "snapshot_id": first.snapshot_id,
                    "capture_kind": "baseline",
                }
            ],
            datetime(2026, 9, 11, 18, 0, tzinfo=timezone.utc),
        )

        self.assertEqual(tasks, [])

    def test_final_capture_replaces_baseline_when_due(self):
        tasks = capture_tasks(
            [game()],
            [],
            datetime(2026, 9, 13, 15, 1, tzinfo=timezone.utc),
        )

        self.assertEqual(tasks[0].capture_kind, "final_t_minus_2h")
        self.assertEqual(
            tasks[0].scheduled_for_utc,
            datetime(2026, 9, 13, 15, 0, tzinfo=timezone.utc),
        )

    def test_successful_final_stops_collection(self):
        tasks = capture_tasks(
            [game()],
            [
                {
                    "nfl_event_id": "nfl-1",
                    "snapshot_id": "final",
                    "capture_kind": "final_t_minus_2h",
                }
            ],
            datetime(2026, 9, 13, 15, 30, tzinfo=timezone.utc),
        )

        self.assertEqual(tasks, [])


class SnapshotRowTests(unittest.TestCase):
    def test_builds_hash_verified_row(self):
        task = CaptureTask(
            game=game(),
            capture_kind="baseline",
            scheduled_for_utc=datetime(
                2026, 9, 11, 12, 0, tzinfo=timezone.utc
            ),
            snapshot_id="",
        )
        task = CaptureTask(
            game=task.game,
            capture_kind=task.capture_kind,
            scheduled_for_utc=task.scheduled_for_utc,
            snapshot_id=__import__("nfl_pikkit").snapshot_identity(
                "nfl-1", "baseline", task.scheduled_for_utc
            ),
        )

        row = build_snapshot_row(
            task,
            event(),
            splits(),
            datetime(2026, 9, 11, 17, 6, tzinfo=timezone.utc),
        )

        normalized = validate_stored_snapshot(row)
        self.assertEqual(normalized["num_picks"], 1000)
        self.assertEqual(row["away_ml_handle_pct"], 0.8)

    def test_rejects_changed_payload(self):
        task = capture_tasks(
            [game()],
            [],
            datetime(2026, 9, 11, 17, 5, tzinfo=timezone.utc),
        )[0]
        row = build_snapshot_row(
            task,
            event(),
            splits(),
            datetime(2026, 9, 11, 17, 6, tzinfo=timezone.utc),
        )
        row["source_json"] += " "

        with self.assertRaisesRegex(ValueError, "hash mismatch"):
            validate_stored_snapshot(row)


class CollectionTests(unittest.IsolatedAsyncioTestCase):
    async def test_collects_due_snapshot(self):
        async def events(_date):
            return {"NFL": [event()]}

        async def event_splits(_event_id):
            return splits()

        rows, outcomes = await collect_due_snapshots(
            [game()],
            [],
            datetime(2026, 9, 11, 17, 5, tzinfo=timezone.utc),
            event_loader=events,
            split_loader=event_splits,
        )

        self.assertEqual(len(rows), 1)
        self.assertEqual(outcomes[0]["status"], "captured")

    async def test_incomplete_data_remains_retryable(self):
        async def events(_date):
            return {"NFL": [event()]}

        async def empty_splits(_event_id):
            return {"num_picks": 0, "total_wagered": 0}

        rows, outcomes = await collect_due_snapshots(
            [game()],
            [],
            datetime(2026, 9, 13, 15, 5, tzinfo=timezone.utc),
            event_loader=events,
            split_loader=empty_splits,
        )

        self.assertEqual(rows, [])
        self.assertEqual(outcomes[0]["status"], "data_incomplete")


def line_snapshot(captured_at: str = "2026-09-11T17:00:00+00:00") -> dict:
    return {
        "captured_at": captured_at,
        "event_id": "nfl-1",
        "commence_time_utc": game()["commence_time_utc"],
        "commence_time_et": "",
        "away_team": game()["away_team"],
        "home_team": game()["home_team"],
        "bookmaker": "BetOnline.ag",
        "away_game_spread_spreadprice_moneyline__h1_spread_spreadprice_moneyline__q1_spread_spreadprice_moneyline": "3,-110,130|nodata,nodata,nodata|nodata,nodata,nodata",
        "home_game_spread_spreadprice_moneyline__h1_spread_spreadprice_moneyline__q1_spread_spreadprice_moneyline": "-3,-110,-150|nodata,nodata,nodata|nodata,nodata,nodata",
        "totals_game_total_overprice_underprice__h1_total_overprice_underprice__q1_total_overprice_underprice": "44.5,-105,-115|nodata,nodata,nodata|nodata,nodata,nodata",
        "api_requests_used": "",
        "api_requests_remaining": "",
    }


def snapshot_at(captured_at: datetime, values: dict | None = None) -> dict:
    task = capture_tasks(
        [game()],
        [],
        datetime(2026, 9, 11, 17, 5, tzinfo=timezone.utc),
    )[0]
    return build_snapshot_row(
        task,
        event(),
        values or splits(),
        captured_at,
    )


class ReaderAndAnalysisTests(unittest.TestCase):
    def test_as_of_reader_never_returns_future_snapshot(self):
        earlier = snapshot_at(
            datetime(2026, 9, 11, 17, 6, tzinfo=timezone.utc)
        )
        later = snapshot_at(
            datetime(2026, 9, 12, 17, 6, tzinfo=timezone.utc)
        )

        selected = latest_snapshot_at_or_before(
            [earlier, later],
            "nfl-1",
            datetime(2026, 9, 11, 20, tzinfo=timezone.utc),
        )

        self.assertEqual(selected["captured_at_utc"], earlier["captured_at_utc"])

    def test_line_reader_uses_latest_prior_snapshot(self):
        selected = latest_line_snapshot_at_or_before(
            [
                line_snapshot("2026-09-11T16:00:00+00:00"),
                line_snapshot("2026-09-11T18:00:00+00:00"),
            ],
            "nfl-1",
            datetime(2026, 9, 11, 17, tzinfo=timezone.utc),
        )

        self.assertEqual(selected["captured_at"], "2026-09-11T16:00:00+00:00")

    def test_sportsbook_net_uses_handle_and_price(self):
        scenarios = sportsbook_net_scenarios(
            0.2,
            0.8,
            -150,
            130,
            outcome_a="home_win",
            outcome_b="away_win",
            include_push=False,
        )

        self.assertEqual(scenarios["best_outcome"], "home_win")
        self.assertEqual(scenarios["lower_handle_proxy"], "home_win")
        self.assertAlmostEqual(
            scenarios["net_per_unit_handle"]["home_win"], 0.666667
        )

    def test_analyzes_market_baseline_and_all_markets(self):
        snapshot = snapshot_at(
            datetime(2026, 9, 11, 17, 6, tzinfo=timezone.utc)
        )

        analysis = analyze_snapshot(snapshot, line_snapshot())

        self.assertEqual(
            set(analysis["markets"]), {"moneyline", "spread", "total"}
        )
        self.assertAlmostEqual(
            analysis["market_baseline"]["home_win_probability"], 0.579832
        )
        self.assertEqual(
            analysis["markets"]["moneyline"]["sportsbook"]["best_outcome"],
            "home_win",
        )
        self.assertEqual(
            analysis["market_baseline"]["representative_score"],
            {"away": 20, "home": 24},
        )

    def test_movement_detects_majority_flip(self):
        first = snapshot_at(
            datetime(2026, 9, 11, 17, 6, tzinfo=timezone.utc)
        )
        changed = splits()
        changed["spread"]["home"]["bet_pct"] = 0.35
        changed["spread"]["away"]["bet_pct"] = 0.65
        changed["spread"]["home"]["handle_pct"] = 0.4
        changed["spread"]["away"]["handle_pct"] = 0.6
        final = snapshot_at(
            datetime(2026, 9, 13, 15, 0, tzinfo=timezone.utc),
            changed,
        )

        movement = snapshot_movement(first, final)

        self.assertTrue(movement["markets"]["spread"]["bet_majority_flipped"])
        self.assertTrue(
            movement["markets"]["spread"]["handle_majority_flipped"]
        )


def games_and_events(count: int, kickoff: str = "2026-09-13T17:00:00+00:00"):
    """Distinct upcoming games with their matching Pikkit listings."""
    pairs = [
        ("Chicago Bears", "Carolina Panthers"),
        ("Green Bay Packers", "Minnesota Vikings"),
        ("Dallas Cowboys", "New York Giants"),
        ("Miami Dolphins", "Buffalo Bills"),
    ]
    games, events = [], []
    for index in range(count):
        away, home = pairs[index]
        games.append({
            **game(),
            "event_id": f"nfl-{index + 1}",
            "away_team": away,
            "home_team": home,
            "commence_time_utc": kickoff,
        })
        events.append({
            **event(),
            "event_id": f"pikkit-{index + 1}",
            "away_full": away,
            "home_full": home,
            "start_time": kickoff.replace("+00:00", ".000Z"),
        })
    return games, events


class RunLimitTests(unittest.IsolatedAsyncioTestCase):
    """The limits that keep a run from looking like the burst Pikkit revoked."""

    NOW = datetime(2026, 9, 11, 17, 5, tzinfo=timezone.utc)

    def test_games_beyond_max_lead_are_not_due(self):
        far = {**game(), "commence_time_utc": "2026-09-21T17:00:00+00:00"}
        near = {**game(), "commence_time_utc": "2026-09-17T17:00:00+00:00"}

        self.assertEqual(capture_tasks([far], [], self.NOW), [])
        self.assertEqual(len(capture_tasks([far], [], self.NOW, max_lead=None)), 1)
        self.assertEqual(len(capture_tasks([near], [], self.NOW)), 1)

    async def test_max_captures_defers_the_rest(self):
        games, events = games_and_events(3)
        split_calls = []

        async def loader(_date):
            return {"NFL": events}

        async def split(event_id):
            split_calls.append(event_id)
            return splits()

        rows, outcomes = await collect_due_snapshots(
            games, [], self.NOW,
            event_loader=loader, split_loader=split, max_captures=2,
        )

        self.assertEqual(
            [item["status"] for item in outcomes],
            ["captured", "captured", "deferred"],
        )
        self.assertEqual(len(rows), 2)
        self.assertEqual(split_calls, ["pikkit-1", "pikkit-2"])
        self.assertIn("cap of 2", outcomes[2]["note"])

    async def test_unavailable_breaker_stops_the_run(self):
        games, events = games_and_events(4)
        split_calls = []

        async def loader(_date):
            return {"NFL": events}

        async def split(event_id):
            split_calls.append(event_id)
            return None

        rows, outcomes = await collect_due_snapshots(
            games, [], self.NOW,
            event_loader=loader, split_loader=split, max_unavailable=2,
        )

        self.assertEqual(rows, [])
        self.assertEqual(
            [item["status"] for item in outcomes],
            ["splits_unavailable", "splits_unavailable", "deferred", "deferred"],
        )
        self.assertEqual(len(split_calls), 2)
        self.assertIn("403", outcomes[3]["note"])

    async def test_requests_are_paced(self):
        games, events = games_and_events(2)
        sleeps = []

        async def loader(_date):
            return {"NFL": events}

        async def split(_event_id):
            return splits()

        async def sleeper(seconds):
            sleeps.append(seconds)

        rows, _ = await collect_due_snapshots(
            games, [], self.NOW,
            event_loader=loader, split_loader=split,
            request_delay=1.5, sleeper=sleeper,
        )

        # one events call, then two splits calls: a pause before each request
        # after the first, none before the first.
        self.assertEqual(len(rows), 2)
        self.assertEqual(sleeps, [1.5, 1.5])

    async def test_unlimited_run_matches_legacy_behaviour(self):
        games, events = games_and_events(4)

        async def loader(_date):
            return {"NFL": events}

        async def split(_event_id):
            return splits()

        rows, outcomes = await collect_due_snapshots(
            games, [], self.NOW, event_loader=loader, split_loader=split,
        )

        self.assertEqual(len(rows), 4)
        self.assertTrue(all(item["status"] == "captured" for item in outcomes))


if __name__ == "__main__":
    unittest.main()
    analyze_snapshot,
    latest_line_snapshot_at_or_before,
    latest_snapshot_at_or_before,
    snapshot_movement,
    sportsbook_net_scenarios,
