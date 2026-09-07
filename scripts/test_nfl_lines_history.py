#!/usr/bin/env python3
"""Offline tests for the historical NFL lines pull (WP4).

Fixtures are real rows and a real ESPN payload trimmed to the parsed fields:
``scripts/fixtures/nflverse_games_excerpt.csv`` (16 rows from nflverse's
games.csv, deliberately unsorted) and
``scripts/fixtures/espn_odds_401671789_2024_bal_kc.json`` /
``espn_odds_401772831_2025_sf_sea.json`` (ESPN core odds, one home
favorite and one away favorite). Nothing here touches the network.
"""

from __future__ import annotations

import contextlib
import copy
import csv
import io
import json
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from nfl_win_predictions import TEAM_ABBREVIATIONS
from scripts.fetch_nfl_lines_history import (
    BLOCK_FIELDS,
    BLOCKS,
    CHECKPOINT_EVERY,
    LINES_COLUMNS,
    NFLVERSE_TEAMS,
    FetchError,
    build_entry,
    build_lines_rows,
    build_parser,
    cross_check,
    extract_block,
    extract_open_close,
    fetch_open_close,
    format_cross_check,
    format_number,
    home_spread_from_nflverse,
    lines_csv_text,
    load_open_close,
    main,
    parse_american,
    parse_line,
    parse_seasons,
    read_lines_csv,
    read_nflverse_csv,
    season_coverage,
    select_provider,
    team_name,
    top_level_home_spread,
    write_lines_csv,
    write_open_close,
)

FIXTURES = Path(__file__).resolve().parent / "fixtures"
GAMES_FIXTURE = FIXTURES / "nflverse_games_excerpt.csv"
BAL_KC_FIXTURE = FIXTURES / "espn_odds_401671789_2024_bal_kc.json"
SF_SEA_FIXTURE = FIXTURES / "espn_odds_401772831_2025_sf_sea.json"


def _games() -> list[dict[str, str]]:
    return read_nflverse_csv(GAMES_FIXTURE.read_text(encoding="utf-8"))


def _payload(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _row(rows: list[dict[str, str]], espn_id: str) -> dict[str, str]:
    return next(row for row in rows if row["espn_id"] == espn_id)


class TeamMapTest(unittest.TestCase):
    def test_map_values_are_exactly_the_32_canonical_names(self):
        self.assertEqual(set(NFLVERSE_TEAMS.values()), set(TEAM_ABBREVIATIONS))
        self.assertEqual(len(set(NFLVERSE_TEAMS.values())), 32)

    def test_current_codes_cover_every_franchise_once(self):
        current = [
            "ARI", "ATL", "BAL", "BUF", "CAR", "CHI", "CIN", "CLE", "DAL", "DEN",
            "DET", "GB", "HOU", "IND", "JAX", "KC", "LA", "LAC", "LV", "MIA", "MIN",
            "NE", "NO", "NYG", "NYJ", "PHI", "PIT", "SEA", "SF", "TB", "TEN", "WAS",
        ]
        self.assertEqual(len(current), 32)
        self.assertEqual({NFLVERSE_TEAMS[code] for code in current}, set(TEAM_ABBREVIATIONS))

    def test_historical_codes_map_to_the_current_franchise(self):
        self.assertEqual(team_name("OAK"), "Las Vegas Raiders")
        self.assertEqual(team_name("SD"), "Los Angeles Chargers")
        self.assertEqual(team_name("STL"), "Los Angeles Rams")
        self.assertEqual(team_name("LAR"), "Los Angeles Rams")
        self.assertEqual(team_name("LA"), "Los Angeles Rams")

    def test_unknown_code_is_an_error_not_a_silent_blank(self):
        with self.assertRaises(ValueError):
            team_name("XYZ")


class SeasonParsingTest(unittest.TestCase):
    def test_range_token(self):
        self.assertEqual(parse_seasons(["2016-2025"]), list(range(2016, 2026)))

    def test_space_separated_list(self):
        self.assertEqual(parse_seasons(["2024", "2025"]), [2024, 2025])

    def test_mixed_ranges_and_years_dedupe_and_sort(self):
        self.assertEqual(parse_seasons(["2024", "2016-2018", "2017"]), [2016, 2017, 2018, 2024])

    def test_bad_tokens_fail(self):
        with self.assertRaises(ValueError):
            parse_seasons(["2025-2016"])
        with self.assertRaises(ValueError):
            parse_seasons(["twenty"])
        with self.assertRaises(ValueError):
            parse_seasons([])


class SignConventionTest(unittest.TestCase):
    """nflverse spread_line is positive when the home team is favored; the
    repo's home_spread is negative when the home team is favored."""

    def setUp(self):
        self.rows = build_lines_rows(_games(), range(2016, 2026))

    def test_2024_week1_ravens_at_chiefs_chiefs_favored_by_3_and_won(self):
        row = _row(self.rows, "401671789")
        self.assertEqual((row["away_team"], row["home_team"]), ("Baltimore Ravens", "Kansas City Chiefs"))
        self.assertEqual(row["nflverse_spread_line"], "3")
        self.assertEqual(row["home_spread"], "-3")
        self.assertEqual((row["home_moneyline"], row["away_moneyline"]), ("-148", "124"))
        self.assertEqual((row["away_score"], row["home_score"]), ("20", "27"))
        self.assertEqual(row["total"], "46")

    def test_2023_week1_lions_at_chiefs_chiefs_favored_by_4_and_lost(self):
        row = _row(self.rows, "401547353")
        self.assertEqual((row["away_team"], row["home_team"]), ("Detroit Lions", "Kansas City Chiefs"))
        self.assertEqual(row["nflverse_spread_line"], "4")
        self.assertEqual(row["home_spread"], "-4")
        self.assertEqual((row["home_moneyline"], row["away_moneyline"]), ("-198", "164"))
        self.assertEqual((row["away_score"], row["home_score"]), ("21", "20"))

    def test_2025_week1_49ers_at_seahawks_away_favorite_gives_positive_home_spread(self):
        row = _row(self.rows, "401772831")
        self.assertEqual((row["away_team"], row["home_team"]), ("San Francisco 49ers", "Seattle Seahawks"))
        self.assertEqual(row["nflverse_spread_line"], "-2.5")
        self.assertEqual(row["home_spread"], "2.5")
        self.assertEqual((row["away_moneyline"], row["home_moneyline"]), ("-135", "114"))

    def test_conversion_helper(self):
        self.assertEqual(home_spread_from_nflverse("3"), -3.0)
        self.assertEqual(home_spread_from_nflverse("-2.5"), 2.5)
        self.assertEqual(home_spread_from_nflverse("0"), 0.0)
        self.assertIsNone(home_spread_from_nflverse(""))
        self.assertIsNone(home_spread_from_nflverse(None))

    def test_number_formatting_has_no_trailing_zero_or_negative_zero(self):
        self.assertEqual(format_number(3.0), "3")
        self.assertEqual(format_number(-2.5), "-2.5")
        self.assertEqual(format_number(-0.0), "0")
        self.assertEqual(format_number(None), "")


class LinesCsvTest(unittest.TestCase):
    def setUp(self):
        self.games = _games()
        self.rows = build_lines_rows(self.games, range(2016, 2026))

    def test_column_order_is_pinned(self):
        self.assertEqual(
            LINES_COLUMNS,
            (
                "season", "week", "gameday", "weekday", "gametime", "espn_id",
                "away_team", "home_team", "away_score", "home_score", "home_spread",
                "total", "away_moneyline", "home_moneyline", "away_spread_price",
                "home_spread_price", "over_price", "under_price", "nflverse_spread_line",
            ),
        )
        for row in self.rows:
            self.assertEqual(tuple(row), LINES_COLUMNS)

    def test_only_regular_season_games_of_the_requested_seasons(self):
        self.assertEqual(len(self.games), 16)
        self.assertEqual(len(self.rows), 14)  # drops the 2024 Super Bowl and the 2026 row
        self.assertFalse(any(row["espn_id"] == "401671889" for row in self.rows))
        only_2024 = build_lines_rows(self.games, [2024])
        self.assertEqual({row["season"] for row in only_2024}, {"2024"})
        self.assertEqual(len(only_2024), 5)
        with_2026 = build_lines_rows(self.games, range(2016, 2027))
        self.assertEqual(len(with_2026), 15)

    def test_sorted_by_season_week_gameday_home_team(self):
        order = [(row["season"], row["week"], row["gameday"], row["home_team"]) for row in self.rows]
        self.assertEqual(order, sorted(order, key=lambda key: (int(key[0]), int(key[1]), key[2], key[3])))
        week1_2024 = [(row["gameday"], row["home_team"]) for row in self.rows if row["season"] == "2024"]
        self.assertEqual(
            week1_2024,
            [
                ("2024-09-05", "Kansas City Chiefs"),
                ("2024-09-06", "Philadelphia Eagles"),
                ("2024-09-08", "Atlanta Falcons"),
                ("2024-09-08", "Buffalo Bills"),
                ("2024-09-09", "San Francisco 49ers"),
            ],
        )
        self.assertEqual(
            [row["home_team"] for row in self.rows if row["season"] == "2016"],
            ["Kansas City Chiefs", "New Orleans Saints", "San Francisco 49ers"],
        )
        self.assertEqual([row["week"] for row in self.rows if row["season"] == "2025"], ["1", "1", "18"])

    def test_historical_codes_in_real_rows(self):
        rams_2016 = _row(self.rows, "400874532")
        self.assertEqual((rams_2016["away_team"], rams_2016["home_team"]), ("Los Angeles Rams", "San Francisco 49ers"))
        chargers_2016 = _row(self.rows, "400874570")
        self.assertEqual(chargers_2016["away_team"], "Los Angeles Chargers")
        raiders_2016 = _row(self.rows, "400874543")
        self.assertEqual(raiders_2016["away_team"], "Las Vegas Raiders")
        raiders_2020 = _row(self.rows, "401220370")
        self.assertEqual((raiders_2020["away_team"], raiders_2020["home_team"]), ("Las Vegas Raiders", "Carolina Panthers"))
        self.assertEqual(raiders_2020["home_spread"], "3")

    def test_empty_odds_cells_stay_empty(self):
        row = _row(self.rows, "400951678")  # 2017 wk4 CHI @ GB: no prices in nflverse
        self.assertEqual(row["home_spread"], "-7.5")
        self.assertEqual(row["total"], "44")
        for column in ("away_moneyline", "home_moneyline", "away_spread_price", "home_spread_price", "over_price", "under_price"):
            self.assertEqual(row[column], "", column)

    def test_unplayed_game_has_empty_scores(self):
        row = _row(build_lines_rows(self.games, [2026]), "401872656")
        self.assertEqual((row["away_score"], row["home_score"]), ("", ""))
        self.assertEqual(row["home_spread"], "-3.5")

    def test_schedule_fields_and_ids_carry_over(self):
        row = _row(self.rows, "401772510")
        self.assertEqual(row["gameday"], "2025-09-04")
        self.assertEqual(row["weekday"], "Thursday")
        self.assertEqual(row["gametime"], "20:20")
        self.assertEqual(row["home_spread"], "-8.5")
        self.assertEqual(row["total"], "47.5")

    def test_csv_text_uses_lf_and_round_trips(self):
        text = lines_csv_text(self.rows)
        self.assertTrue(text.startswith(",".join(LINES_COLUMNS) + "\n"))
        self.assertNotIn("\r", text)
        self.assertEqual(list(csv.DictReader(io.StringIO(text))), self.rows)

    def test_write_is_atomic_and_readable(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "nested" / "nfl_lines_history.csv"
            write_lines_csv(self.rows, path)
            self.assertEqual(read_lines_csv(path), self.rows)
            self.assertEqual(sorted(p.name for p in path.parent.iterdir()), ["nfl_lines_history.csv"])

    def test_season_coverage_counts_priced_games(self):
        coverage = season_coverage(self.rows)
        self.assertEqual(coverage[2017], {"games": 1, "spread": 1, "total": 1, "moneyline": 0})
        self.assertEqual(coverage[2024], {"games": 5, "spread": 5, "total": 5, "moneyline": 5})


class EspnExtractionTest(unittest.TestCase):
    def test_prefers_espn_bet(self):
        payload = _payload(BAL_KC_FIXTURE)
        self.assertEqual([item["provider"]["name"] for item in payload["items"]], ["Bet 365", "ESPN BET", "ESPN Bet - Live Odds"])
        name, item = select_provider(payload["items"])
        self.assertEqual(name, "ESPN BET")
        self.assertEqual(item["details"], "KC -2.5")

    def test_home_favorite_blocks_use_the_repo_sign_convention(self):
        provider, blocks = extract_open_close(_payload(BAL_KC_FIXTURE))
        self.assertEqual(provider, "ESPN BET")
        self.assertEqual(
            blocks["open"],
            {
                "home_spread": -3.0, "away_spread": 3.0,
                "home_spread_price": -110, "away_spread_price": -110,
                "home_moneyline": -150, "away_moneyline": 130,
                "total": 46.5, "over_price": -115, "under_price": -105,
            },
        )
        self.assertEqual(
            blocks["close"],
            {
                "home_spread": -2.5, "away_spread": 2.5,
                "home_spread_price": -120, "away_spread_price": 100,
                "home_moneyline": -140, "away_moneyline": 120,
                "total": 45.5, "over_price": -115, "under_price": -105,
            },
        )
        self.assertEqual(blocks["current"], blocks["close"])
        # Same sign as nflverse's converted close for this game (-3): both say
        # Chiefs favored, and the books differ by half a point.
        nflverse_close = home_spread_from_nflverse("3")
        self.assertGreater(blocks["close"]["home_spread"] * nflverse_close, 0)
        self.assertAlmostEqual(abs(blocks["close"]["home_spread"] - nflverse_close), 0.5)

    def test_away_favorite_blocks_and_top_level_spread_are_home_relative(self):
        payload = _payload(SF_SEA_FIXTURE)
        provider, blocks = extract_open_close(payload)
        self.assertEqual(provider, "ESPN BET")
        self.assertEqual(blocks["open"]["home_spread"], 2.5)
        self.assertEqual(blocks["open"]["away_spread"], -2.5)
        self.assertEqual((blocks["open"]["home_moneyline"], blocks["open"]["away_moneyline"]), (120, -140))
        self.assertEqual(blocks["close"]["home_spread"], 1.5)
        self.assertEqual(blocks["close"]["away_spread"], -1.5)
        self.assertEqual((blocks["close"]["home_spread_price"], blocks["close"]["away_spread_price"]), (-105, -115))
        self.assertEqual((blocks["close"]["home_moneyline"], blocks["close"]["away_moneyline"]), (105, -125))
        self.assertEqual((blocks["close"]["total"], blocks["close"]["over_price"], blocks["close"]["under_price"]), (44.5, 100, -120))
        name, item = select_provider(payload["items"])
        # ESPN's top-level ``spread`` is home-relative (+1.5 = home underdog) while
        # ``details`` names the favorite ("SF -1.5"): that is the roadmap's "disagreement".
        self.assertEqual(top_level_home_spread(item), 1.5)
        self.assertEqual(item["details"], "SF -1.5")
        self.assertEqual(top_level_home_spread(item), blocks["current"]["home_spread"])

    def test_falls_back_to_the_first_provider_with_open_and_close(self):
        payload = _payload(BAL_KC_FIXTURE)
        espn_bet = next(item for item in payload["items"] if item["provider"]["name"] == "ESPN BET")
        renamed = copy.deepcopy(espn_bet)
        renamed["provider"] = {"id": "40", "name": "DraftKings", "priority": 0}
        payload["items"] = [item for item in payload["items"] if item["provider"]["name"] != "ESPN BET"] + [renamed]
        provider, blocks = extract_open_close(payload)
        self.assertEqual(provider, "DraftKings")
        self.assertEqual(blocks["close"]["home_spread"], -2.5)

    def test_partial_provider_is_recorded_with_empty_blocks(self):
        payload = _payload(BAL_KC_FIXTURE)
        payload["items"] = [item for item in payload["items"] if item["provider"]["name"] != "ESPN BET"]
        provider, blocks = extract_open_close(payload)
        self.assertEqual(provider, "ESPN Bet - Live Odds")  # has open + current, no close
        self.assertEqual(blocks["open"]["home_spread"], -3.0)
        self.assertEqual(blocks["close"], {name: None for name in BLOCK_FIELDS})

    def test_no_usable_provider(self):
        payload = _payload(BAL_KC_FIXTURE)
        payload["items"] = [item for item in payload["items"] if item["provider"]["name"] == "Bet 365"]
        provider, blocks = extract_open_close(payload)
        self.assertIsNone(provider)
        for block in BLOCKS:
            self.assertEqual(blocks[block], {name: None for name in BLOCK_FIELDS})
        self.assertEqual(extract_open_close({})[0], None)

    def test_price_and_line_parsing(self):
        self.assertEqual(parse_american("+130"), 130)
        self.assertEqual(parse_american("-110"), -110)
        self.assertEqual(parse_american("EVEN"), 100)
        self.assertEqual(parse_american(-140), -140)
        self.assertIsNone(parse_american(""))
        self.assertIsNone(parse_american(None))
        self.assertIsNone(parse_american("n/a"))
        self.assertEqual(parse_line("-2.5"), -2.5)
        self.assertEqual(parse_line("+3"), 3.0)
        self.assertEqual(parse_line("PK"), 0.0)
        self.assertEqual(parse_line("45.5"), 45.5)
        self.assertIsNone(parse_line(None))
        self.assertIsNone(parse_line("OFF"))

    def test_2023_style_contaminated_lines_are_dropped_but_prices_kept(self):
        # For 2023 events ESPN puts the price where the line belongs
        # (observed: close.total.american == "-110", pointSpread.american == "-115").
        item = {
            "provider": {"name": "ESPN BET"},
            "homeTeamOdds": {"close": {"pointSpread": {"american": "-115"}, "spread": {"american": "-115"}, "moneyLine": {"american": "-210"}}},
            "awayTeamOdds": {"close": {"pointSpread": {"american": "-115"}, "spread": {"american": "-115"}, "moneyLine": {"american": "+175"}}},
            "close": {"over": {"american": "-110"}, "under": {"american": "-110"}, "total": {"american": "-110"}},
        }
        block = extract_block(item, "close")
        self.assertIsNone(block["home_spread"])
        self.assertIsNone(block["away_spread"])
        self.assertIsNone(block["total"])
        self.assertEqual((block["home_spread_price"], block["away_spread_price"]), (-115, -115))
        self.assertEqual((block["home_moneyline"], block["away_moneyline"]), (-210, 175))
        self.assertEqual((block["over_price"], block["under_price"]), (-110, -110))

    def test_entry_shape(self):
        row = {"season": "2024", "week": "1", "away_team": "Baltimore Ravens", "home_team": "Kansas City Chiefs", "espn_id": "401671789"}
        entry = build_entry(row, _payload(BAL_KC_FIXTURE), "2026-09-07T12:00:00Z")
        self.assertEqual(
            sorted(entry),
            ["away_team", "close", "current", "fetched_at", "home_team", "open", "provider", "season", "week"],
        )
        self.assertEqual((entry["season"], entry["week"]), (2024, 1))
        self.assertEqual(entry["fetched_at"], "2026-09-07T12:00:00Z")
        for block in BLOCKS:
            self.assertEqual(tuple(entry[block]), BLOCK_FIELDS)


def _stub_rows(count: int) -> list[dict[str, str]]:
    return [
        {"season": "2024", "week": str(1 + index // 16), "away_team": "Baltimore Ravens", "home_team": "Kansas City Chiefs", "espn_id": str(400000 + index)}
        for index in range(count)
    ]


class MergeTest(unittest.TestCase):
    def setUp(self):
        self.payload = _payload(BAL_KC_FIXTURE)
        self.calls: list[str] = []
        self.sleeps: list[float] = []
        self.clock = datetime(2026, 9, 7, 15, 0, tzinfo=timezone.utc)

    def _fetch(self, event_id: str) -> dict:
        self.calls.append(event_id)
        if event_id.endswith("13"):
            raise FetchError(f"{event_id}: HTTP 503 after 5 attempts")
        return self.payload

    def _run(self, rows, existing, **kwargs):
        return fetch_open_close(
            rows,
            existing,
            fetch=self._fetch,
            sleep=self.sleeps.append,
            now=lambda: self.clock,
            log=lambda message: None,
            **kwargs,
        )

    def test_existing_entries_are_kept_and_not_refetched(self):
        rows = _stub_rows(3)
        old = build_entry(rows[0], self.payload, "2026-09-01T00:00:00Z")
        result = self._run(rows, {"400000": old}, pace=0.75)
        self.assertEqual(self.calls, ["400001", "400002"])
        self.assertEqual(result.entries["400000"], old)
        self.assertEqual(result.entries["400000"]["fetched_at"], "2026-09-01T00:00:00Z")
        self.assertEqual(sorted(result.entries), ["400000", "400001", "400002"])
        self.assertEqual((result.fetched, result.skipped, result.failed), (2, 1, []))
        self.assertEqual(result.entries["400001"]["fetched_at"], "2026-09-07T15:00:00Z")
        self.assertEqual(self.sleeps, [0.75, 0.75])  # paced once per network call, never for skips

    def test_refresh_replaces_existing_entries(self):
        rows = _stub_rows(2)
        old = build_entry(rows[0], self.payload, "2026-09-01T00:00:00Z")
        old["provider"] = "stale"
        result = self._run(rows, {"400000": old}, refresh=True, pace=0)
        self.assertEqual(self.calls, ["400000", "400001"])
        self.assertEqual(result.entries["400000"]["provider"], "ESPN BET")
        self.assertEqual(result.entries["400000"]["fetched_at"], "2026-09-07T15:00:00Z")
        self.assertEqual(self.sleeps, [])

    def test_limit_bounds_new_fetches_only(self):
        rows = _stub_rows(5)
        existing = {"400000": build_entry(rows[0], self.payload, "2026-09-01T00:00:00Z")}
        result = self._run(rows, existing, limit=2, pace=0)
        self.assertEqual(self.calls, ["400001", "400002"])
        self.assertEqual(len(result.entries), 3)

    def test_failures_are_reported_not_persisted(self):
        rows = _stub_rows(15)  # 400013 fails
        result = self._run(rows, {}, pace=0)
        self.assertEqual([event_id for event_id, _ in result.failed], ["400013"])
        self.assertIn("HTTP 503", result.failed[0][1])
        self.assertNotIn("400013", result.entries)
        self.assertEqual(result.fetched, 14)
        self.assertEqual(len(self.calls), 15)

    def test_checkpoints_every_25_and_at_the_end(self):
        rows = _stub_rows(30)
        rows = [row for row in rows if row["espn_id"] != "400013"] + [_stub_rows(31)[30]]
        seen: list[int] = []
        result = self._run(rows, {}, pace=0, checkpoint=lambda entries: seen.append(len(entries)))
        self.assertEqual(CHECKPOINT_EVERY, 25)
        self.assertEqual(seen, [25, 30])
        self.assertEqual(len(result.entries), 30)

    def test_rows_without_an_espn_id_are_ignored(self):
        rows = [{"season": "2016", "week": "1", "away_team": "a", "home_team": "b", "espn_id": ""}]
        result = self._run(rows, {}, pace=0)
        self.assertEqual(self.calls, [])
        self.assertEqual(result.entries, {})

    def test_unusable_provider_is_persisted_and_listed(self):
        self.payload["items"] = [item for item in self.payload["items"] if item["provider"]["name"] == "Bet 365"]
        result = self._run(_stub_rows(1), {}, pace=0)
        self.assertEqual(result.no_provider, ["400000"])
        self.assertIsNone(result.entries["400000"]["provider"])


class JsonRoundTripTest(unittest.TestCase):
    def test_sorted_keys_indent_one_and_round_trip(self):
        row = {"season": "2024", "week": "1", "away_team": "Baltimore Ravens", "home_team": "Kansas City Chiefs", "espn_id": "401671789"}
        entries = {"401671789": build_entry(row, _payload(BAL_KC_FIXTURE), "2026-09-07T12:00:00Z")}
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "nfl_open_close.json"
            self.assertEqual(load_open_close(path), {})
            write_open_close(entries, path)
            text = path.read_text(encoding="utf-8")
            self.assertTrue(text.startswith('{\n "401671789": {\n  "away_team": "Baltimore Ravens",\n  "close": {\n   "away_moneyline": 120,'))
            self.assertTrue(text.endswith("}\n"))
            self.assertEqual(load_open_close(path), entries)
            self.assertLess(len(text) * 544, 1_000_000)


class CrossCheckTest(unittest.TestCase):
    def _entry(self, home_spread, total):
        blocks = {name: None for name in BLOCK_FIELDS}
        close = dict(blocks, home_spread=home_spread, total=total)
        return {"open": blocks, "close": close, "current": blocks}

    def test_buckets_and_sign_flips(self):
        rows = [
            {"season": "2024", "week": "1", "away_team": "A", "home_team": "B", "espn_id": "1", "home_spread": "-3", "total": "46"},
            {"season": "2024", "week": "1", "away_team": "C", "home_team": "D", "espn_id": "2", "home_spread": "-8.5", "total": "47.5"},
            {"season": "2024", "week": "2", "away_team": "E", "home_team": "F", "espn_id": "3", "home_spread": "1", "total": "40"},
            {"season": "2024", "week": "2", "away_team": "G", "home_team": "H", "espn_id": "4", "home_spread": "", "total": ""},
            {"season": "2024", "week": "2", "away_team": "I", "home_team": "J", "espn_id": "5", "home_spread": "-3", "total": "44"},
        ]
        entries = {
            "1": self._entry(-2.5, 45.5),   # near / near
            "2": self._entry(-7.5, 47.5),   # far / exact
            "3": self._entry(-1.0, 40.0),   # far + sign flip / exact
            "4": self._entry(-3.0, 44.0),   # no nflverse line: skipped
            "5": self._entry(None, 44.0),   # no ESPN close: skipped
        }
        stats = cross_check(rows, entries)
        self.assertEqual(stats["compared"], 3)
        self.assertEqual((stats["spread"]["exact"], stats["spread"]["near"], len(stats["spread"]["far"])), (0, 1, 2))
        self.assertEqual((stats["total"]["exact"], stats["total"]["near"], len(stats["total"]["far"])), (2, 1, 0))
        self.assertEqual([label for label, _, _ in stats["spread"]["sign_flips"]], ["2024 wk2 E @ F"])
        report = format_cross_check(stats)
        self.assertIn("over 3 games", report)
        self.assertIn("sign flips (different favorite): 1", report)
        self.assertIn("2024 wk1 C @ D: espn -7.5 vs nflverse -8.5", report)


class CliTest(unittest.TestCase):
    def test_defaults_and_append_instruction(self):
        parser = build_parser()
        args = parser.parse_args([])
        self.assertEqual(parse_seasons(args.seasons), list(range(2016, 2026)))
        self.assertEqual(parse_seasons(args.espn_seasons), [2024, 2025])
        self.assertEqual(args.pace, 0.75)
        self.assertFalse(args.skip_espn or args.refresh)
        self.assertIsNone(args.espn_limit)
        later = parser.parse_args(["--seasons", "2016-2026", "--espn-seasons", "2024", "2025", "2026", "--espn-limit", "5"])
        self.assertEqual(parse_seasons(later.seasons)[-1], 2026)
        self.assertEqual(parse_seasons(later.espn_seasons), [2024, 2025, 2026])
        self.assertEqual(later.espn_limit, 5)
        self.assertIn("--seasons 2016-2026 --espn-seasons 2024 2025 2026", parser.format_help())

    def test_csv_only_run_is_offline(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "lines.csv"
            with contextlib.redirect_stdout(io.StringIO()) as captured:
                code = main(["--games-csv", str(GAMES_FIXTURE), "--skip-espn", "--lines-csv", str(out), "--seasons", "2023-2025"])
            self.assertEqual(code, 0)
            rows = read_lines_csv(out)
            self.assertEqual([row["espn_id"] for row in rows], ["401547353", "401671789", "401671805", "401671744", "401671617", "401671696", "401772510", "401772831", "401772969"])
            self.assertIn("wrote 9 regular-season games", captured.getvalue())


if __name__ == "__main__":
    unittest.main()
