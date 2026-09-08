"""Backtest harness for the God Expert rules arm (roadmap WP8) and the
ledger refit (WP10).

Everything here replays the production arithmetic, never a copy of it: a
market goes through :func:`moe_god.market_block_from_lines`, the rating voice
through :func:`moe_rating.rating_estimate` and :func:`moe_god.voice_from_row`,
the pool and shrink through :func:`moe_god.build_feature_block` and
:func:`moe_god.rules_arm_response`, the legs through
:func:`moe_god.apply_policy`, and the grading through
:func:`moe_god._leg_result`. A change to any of those changes the backtest
with it, which is the point.

Historical replay (``grid``, ``clv``)
    Games come from ``data/nfl_lines_history.csv`` (nflverse closing lines
    with juice; complete from 2016) and, for the open-to-close work, from
    ``data/nfl_open_close.json`` (ESPN open and close per event, 2024-2025).
    The committee is the rating voice alone -- the model voices have no
    history -- replayed from 1999 with the committed prior's Elo parameters,
    so every pregame rating is leak-free (the update follows the
    prediction). The voice's projected total for a season is the previous
    season's league scoring rate, as the live voice uses the last completed
    season's mean. The rating voice informs the side pool only (registry
    ``markets``), so the total pool is empty and the blend total is the
    market line: under the normal model a total never fires, and under the
    empirical model only the bin's own asymmetry at the line can clear a
    low threshold (a market-line-only signal, scored like any other).
    ``sigma_total`` and the total veto are therefore not identifiable from
    this committee and keep their values.

    In ``grid`` mode the market is the nflverse close and there is no
    opening, so the market-move veto never fires. In ``clv`` mode the
    market is the ESPN open, the ESPN close is the closing line, so every
    fired leg carries closing-line value.

    The empirical margin model is evaluated without leakage: the table for a
    season is built from 2016 up to the season before it
    (:func:`moe_god.margin_table_override`), never from the committed file,
    which holds the seasons under test.

Scores
    ML Brier of the arm's ``home_win_probability`` against the winner (ties
    excluded, as ``grade_opinion_row`` grades), beside the de-vigged market's
    and the raw Elo's; log loss as a secondary. Cover Brier of the arm's
    ``p_cover_home`` against the ATS result at the close (pushes excluded),
    beside the fair cover probability's. Fired legs: W-L-P, flat one unit at
    the posted price, ROI, mean CLV where a closing line exists, and the
    record per star bucket.

Selection (``select_policy``)
    ``shrink_lambda`` by the lowest fit-season ML Brier; then
    ``sigma_margin`` and ``margin_model`` by the lowest cover Brier at that
    lambda; ``edge_threshold`` and ``min_ev_per_unit`` keep the registry
    values unless a candidate beats them on ROI (and on mean CLV where CLV
    exists) with at least ``MIN_BETS_TO_PREFER`` fit-season bets; the star
    ladder keeps the default unless the alternative is monotone in ROI
    where the default is not. The check seasons are then scored for the
    chosen policy and the registry policy side by side, untouched by the
    selection. Nothing here writes ``moe/experts.yaml``.

Veto calibration (``veto``)
    For every event with an ESPN open and close, the legs whose own side
    got cheaper since open by at least a threshold (the directions
    ``_adverse_move_note`` vetoes) and how they did at the close, flat one
    unit at the close price. Negative units on the adverse side mean the
    veto helps.

Ledger refit (``ledger``, WP10)
    The same grid over persisted ``god_rules`` rows: each row's voices are
    re-pooled under every candidate policy on the row's own market (opening
    and latest are both persisted, so the veto can fire here), graded
    against ESPN finals and the last snapshot before kickoff. Below
    ``MIN_REFIT_GAMES`` graded games the output is informational.

Standard library only; no ``moe`` import (this module runs on Windows).
"""

from __future__ import annotations

import copy
import json
import math
import time
from collections import Counter
from pathlib import Path
from typing import Any, Iterable

from moe_ak import _matching_history_game, _parse_time
from moe_god import (
    ADVERSE_MOVE_REASON,
    DEFAULT_POLICY,
    MARKET_FIELDS,
    _empty_record,
    _leg_label,
    _leg_result,
    _stars_for_edge,
    aggregator_policy,
    american_to_decimal,
    apply_policy,
    build_feature_block,
    closing_market,
    load_registry,
    margin_table_override,
    market_block_from_lines,
    movement_since_open,
    parse_margin_table,
    rules_arm_response,
    voice_from_row,
    voice_markets,
)
from moe_rating import (
    PARAMETER_KEYS,
    RATING_EXPERT_ID,
    league_scoring_rate,
    load_prior,
    rating_estimate,
    read_lines_csv,
    replay_games,
)
from scripts.build_nfl_margins import build_table, game_from_row
from scripts.fetch_nfl_lines_history import load_open_close

ROOT = Path(__file__).resolve().parent
LINES_CSV = ROOT / "data" / "nfl_lines_history.csv"
OPEN_CLOSE_JSON = ROOT / "data" / "nfl_open_close.json"

RULES_EXPERT_ID = "god_rules"
DEFAULT_FIT_SEASONS = (2023, 2024)
DEFAULT_CHECK_SEASONS = (2025,)
DEFAULT_CLV_SEASONS = (2024, 2025)
# The empirical table for a season is built from these seasons up to the
# season before it (the committed table's first season and support bar).
TABLE_FIRST_SEASON = 2016
TABLE_MIN_GAMES = 30
# The roadmap's bar for the ledger refit (WP10).
MIN_REFIT_GAMES = 50
# A leg-knob candidate needs this many fit-season bets to displace a default.
MIN_BETS_TO_PREFER = 50
# Star buckets with fewer bets do not enter the monotonicity check.
MIN_BETS_PER_STAR = 10
# A veto threshold displaces the current one only when the adverse side's
# ROI is at least this much worse there, over at least MIN_VETO_LEGS legs.
VETO_CLEAR_MARGIN = 0.05
MIN_VETO_LEGS = 40
LOG_CLIP = 1e-6

GRID: dict[str, tuple[Any, ...]] = {
    "shrink_lambda": (0.0, 0.25, 0.5, 0.75, 1.0),
    "sigma_margin": (12.0, 13.0, 13.5, 14.0, 15.0),
    "margin_model": ("normal", "empirical"),
    "edge_threshold": (0.02, 0.03, 0.04, 0.05),
    "min_ev_per_unit": (0.0, 0.01, 0.02, 0.03),
}
# Star ladders as offsets from the edge threshold (policy validation
# requires star_edges[0] == edge_threshold). "default" reproduces the
# registry's 0.03/0.05/0.08/0.12/0.16 at a 3% threshold.
STAR_LADDERS: dict[str, tuple[float, ...]] = {
    "default": (0.0, 0.02, 0.05, 0.09, 0.13),
    "alternative": (0.0, 0.03, 0.06, 0.09, 0.12),
}
VETO_GRID: dict[str, tuple[float, ...]] = {
    "spread": (0.5, 1.0, 1.5, 2.0),
    "total": (0.5, 1.0, 1.5, 2.0),
    "price": (5.0, 10.0, 15.0, 20.0),
}
VETO_KNOBS = {
    "spread": "veto_adverse_spread_points",
    "total": "veto_adverse_total_points",
    "price": "veto_adverse_price_cents",
}
# Knobs that switch the veto off for a "what would have been bet" replay.
NO_VETO = {
    "veto_adverse_spread_points": 1e9,
    "veto_adverse_total_points": 1e9,
    "veto_adverse_price_cents": 1e9,
}
LEG_KINDS = ("side", "total")
POLICY_KEY_FIELDS = (
    "shrink_lambda",
    "sigma_margin",
    "margin_model",
    "edge_threshold",
    "min_ev_per_unit",
    "star_ladder",
)


# --------------------------------------------------------------------------
# Seasons and data


def parse_seasons(tokens: Iterable[Any]) -> list[int]:
    """``2023-2024``, ``2025``, or a mix; sorted and deduplicated."""
    seasons: set[int] = set()
    for token in tokens:
        for piece in str(token).replace(",", " ").split():
            if "-" in piece:
                start, end = piece.split("-", 1)
                first, last = int(start), int(end)
                if last < first:
                    raise ValueError(f"Bad season range: {piece}")
                seasons.update(range(first, last + 1))
            else:
                seasons.add(int(piece))
    if not seasons:
        raise ValueError("No seasons given")
    return sorted(seasons)


def _number(value: Any) -> float | None:
    if value in (None, ""):
        return None
    number = float(value)
    return number if math.isfinite(number) else None


def _price(value: Any) -> int | None:
    number = _number(value)
    return None if number is None else int(round(number))


def market_from_history_row(row: dict[str, Any]) -> dict[str, Any] | None:
    """The nflverse close of one CSV row as a decoded full-game market.

    None when a line or a price is missing (the CSV is complete from 2016;
    earlier seasons lack moneylines and juice).
    """
    home_spread = _number(row.get("home_spread"))
    total = _number(row.get("total"))
    prices = {
        field: _price(row.get(field))
        for field in (
            "away_moneyline",
            "home_moneyline",
            "away_spread_price",
            "home_spread_price",
            "over_price",
            "under_price",
        )
    }
    if home_spread is None or total is None or any(
        value is None for value in prices.values()
    ):
        return None
    market = {
        "away_spread": -home_spread,
        "home_spread": home_spread,
        "total": total,
        **prices,
    }
    return {field: market[field] for field in MARKET_FIELDS}


def market_from_espn_block(block: dict[str, Any] | None) -> dict[str, Any] | None:
    """An ESPN ``open``/``close`` block as a decoded market; None when a field
    is missing or the spreads are not mirror images."""
    if not isinstance(block, dict):
        return None
    market: dict[str, Any] = {}
    for field in MARKET_FIELDS:
        value = block.get(field)
        if field in ("home_spread", "away_spread", "total"):
            number = _number(value)
        else:
            number = _price(value)
        if number is None:
            return None
        market[field] = number
    if float(market["away_spread"]) != -float(market["home_spread"]):
        return None
    return market


def history_games(
    rows: Iterable[dict[str, Any]], seasons: Iterable[int]
) -> list[dict[str, Any]]:
    """Played games of ``seasons`` with a complete closing market, in season,
    week, day order. Each carries ``final`` and ``close`` (the market)."""
    wanted = set(int(season) for season in seasons)
    games = []
    for row in rows:
        if int(row["season"]) not in wanted:
            continue
        if row.get("home_score") in ("", None) or row.get("away_score") in ("", None):
            continue
        close = market_from_history_row(row)
        if close is None:
            continue
        games.append(
            {
                "season": int(row["season"]),
                "week": int(row["week"]),
                "gameday": str(row.get("gameday") or ""),
                "gametime": str(row.get("gametime") or ""),
                "event_id": str(row.get("espn_id") or ""),
                "away_team": str(row["away_team"]),
                "home_team": str(row["home_team"]),
                "final": {
                    "away_score": int(float(row["away_score"])),
                    "home_score": int(float(row["home_score"])),
                },
                "close": close,
            }
        )
    games.sort(
        key=lambda game: (
            game["season"],
            game["week"],
            game["gameday"],
            game["gametime"],
            game["home_team"],
        )
    )
    return games


def attach_open_close(
    games: list[dict[str, Any]], open_close: dict[str, dict[str, Any]]
) -> list[dict[str, Any]]:
    """Add ``espn_open`` / ``espn_close`` markets to the games that have them
    (joined on the ESPN event id); games without stay as they are."""
    for game in games:
        entry = open_close.get(str(game["event_id"]))
        if not entry:
            continue
        game["espn_provider"] = entry.get("provider")
        game["espn_open"] = market_from_espn_block(entry.get("open"))
        game["espn_close"] = market_from_espn_block(entry.get("close"))
    return games


# --------------------------------------------------------------------------
# The rating voice, replayed


def elo_parameters(prior: dict[str, Any] | None = None) -> dict[str, float]:
    prior = prior if prior is not None else load_prior()[0]
    return {key: float(prior["parameters"][key]) for key in PARAMETER_KEYS}


def rating_inputs(
    rows: list[dict[str, Any]],
    *,
    seasons: Iterable[int],
    params: dict[str, float],
) -> dict[tuple[int, int, str, str], dict[str, Any]]:
    """Pregame ratings and the season's projected total for every played
    game of ``seasons``, from a replay of the whole file (warm-up from its
    first season). Keyed by (season, week, away, home)."""
    seasons = sorted(int(season) for season in seasons)
    replay = replay_games(rows, params, collect_from=min(seasons))
    totals = {
        season: float(league_scoring_rate(rows, season - 1)["mean_total"])
        for season in seasons
    }
    inputs = {}
    for prediction in replay["predictions"]:
        season = int(prediction["season"])
        if season not in totals:
            continue
        key = (
            season,
            int(prediction["week"]),
            str(prediction["away_team"]),
            str(prediction["home_team"]),
        )
        inputs[key] = {
            "home_rating": float(prediction["home_rating"]),
            "away_rating": float(prediction["away_rating"]),
            "total": totals[season],
            "elo_probability": float(prediction["home_win_probability"]),
        }
    return inputs


def rating_opinion_row(
    game: dict[str, Any], estimate: dict[str, Any], config: dict[str, Any]
) -> dict[str, Any]:
    """A synthetic ``rating_elo`` opinion row shaped like a persisted one,
    so :func:`moe_god.voice_from_row` builds the voice exactly as live."""
    return {
        "opinion_id": f"backtest:{RATING_EXPERT_ID}:{game['season']}:{game['week']}:{game['event_id']}",
        "generated_at_utc": "",
        "expert_id": RATING_EXPERT_ID,
        "expert_name": str(config.get("name") or RATING_EXPERT_ID),
        "expert_version": config.get("version", ""),
        "prompt_version": config.get("prompt_version", ""),
        "model": "deterministic",
        "generation_backend": "deterministic",
        "generation_effort": "",
        "predicted_winner": estimate["predicted_winner"],
        "predicted_away_score": estimate["predicted_away_score"],
        "predicted_home_score": estimate["predicted_home_score"],
        "home_win_probability": estimate["home_win_probability"],
        "expected_home_margin": estimate["expected_home_margin"],
        "confidence_stars": estimate["confidence_stars"],
        "thesis": "",
        "supporting_factors_json": "[]",
        "counterarguments_json": "[]",
        "no_signal_factors_json": "[]",
        "discarded_considerations_json": "[]",
        "side_pick_json": "",
        "total_pick_json": "",
    }


def rating_voice(
    game: dict[str, Any],
    inputs: dict[str, Any],
    *,
    params: dict[str, float],
    registry: dict[str, Any],
    market: dict[str, Any],
    policy: dict[str, Any],
) -> dict[str, Any]:
    """The committee's one voice for ``game``, built through the live path."""
    config = registry["experts"][RATING_EXPERT_ID]
    estimate = rating_estimate(
        away_team=game["away_team"],
        home_team=game["home_team"],
        away_rating=inputs["away_rating"],
        home_rating=inputs["home_rating"],
        params=params,
        total=inputs["total"],
    )
    return voice_from_row(
        RATING_EXPERT_ID,
        config,
        rating_opinion_row(game, estimate, config),
        selection_rule="default_model",
        market=market,
        policy=policy,
        track_record=_empty_record(),
    )


# --------------------------------------------------------------------------
# Policies


def policy_key(policy: dict[str, Any], ladder: str) -> tuple[Any, ...]:
    return (
        float(policy["shrink_lambda"]),
        float(policy["sigma_margin"]),
        str(policy["margin_model"]),
        float(policy["edge_threshold"]),
        float(policy["min_ev_per_unit"]),
        ladder,
    )


def key_label(key: tuple[Any, ...]) -> str:
    return "|".join(
        f"{field}={value:g}" if isinstance(value, float) else f"{field}={value}"
        for field, value in zip(POLICY_KEY_FIELDS, key)
    )


def star_edges_for(edge_threshold: float, ladder: str) -> list[float]:
    return [round(float(edge_threshold) + offset, 4) for offset in STAR_LADDERS[ladder]]


def make_policy(
    base: dict[str, Any], *, ladder: str = "default", **overrides: Any
) -> dict[str, Any]:
    """A validated policy: the registry block (or a persisted policy) with
    overrides, the star ladder rebuilt on the edge threshold."""
    block = {**base, **overrides}
    block["star_edges"] = star_edges_for(block["edge_threshold"], ladder)
    return aggregator_policy({"aggregator_policy": block})


def base_policy_block(registry: dict[str, Any] | None = None) -> dict[str, Any]:
    """The registry's ``aggregator_policy`` block as written (defaults for
    keys it omits), the base every grid point overrides."""
    registry = registry if registry is not None else load_registry()
    return {**DEFAULT_POLICY, **(registry.get("aggregator_policy") or {})}


def registry_ladder(base: dict[str, Any]) -> str:
    """Which ladder the base block's ``star_edges`` follow (default when
    neither matches exactly)."""
    edges = [round(float(value), 4) for value in base["star_edges"]]
    for name in STAR_LADDERS:
        if star_edges_for(base["edge_threshold"], name) == edges:
            return name
    return "default"


def grid_points(
    base: dict[str, Any], grid: dict[str, tuple[Any, ...]] | None = None
) -> list[tuple[dict[str, Any], str]]:
    """Every (policy, ladder) of the grid, validated, in a fixed order."""
    grid = grid if grid is not None else GRID

    def axis(field: str) -> list[Any]:
        # The base's own value always sits on the axis, so the registry
        # policy is one of the points and the selection can fall back to it.
        values = list(grid[field])
        own = base[field]
        if field != "margin_model":
            own = float(own)
            values = [float(value) for value in values]
        if own not in values:
            values.append(own)
        return sorted(values)

    points = []
    for lam in axis("shrink_lambda"):
        for sigma in axis("sigma_margin"):
            for model in axis("margin_model"):
                for edge in axis("edge_threshold"):
                    for floor in axis("min_ev_per_unit"):
                        for ladder in STAR_LADDERS:
                            points.append(
                                (
                                    make_policy(
                                        base,
                                        ladder=ladder,
                                        shrink_lambda=lam,
                                        sigma_margin=sigma,
                                        margin_model=model,
                                        edge_threshold=edge,
                                        min_ev_per_unit=floor,
                                    ),
                                    ladder,
                                )
                            )
    return points


# --------------------------------------------------------------------------
# The rules arm on one game


def rules_estimate(
    voices: list[dict[str, Any]],
    market: dict[str, Any],
    policy: dict[str, Any],
    *,
    home_team: str,
    weighting: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Pool, shrink, coherence: the rules arm's estimate for one game.

    Returns the response of :func:`moe_god.rules_arm_response` plus the
    feature block it read. Depends on the voices, the market, and
    ``shrink_lambda``; the margin model and sigmas do not enter it.
    """
    if weighting is None:
        weighting = {
            "weights": {voice["voice_id"]: 1.0 for voice in voices},
            "active": False,
            "mean_brier": None,
        }
    feature = build_feature_block(
        voices, market, policy, weighting, home_team=home_team
    )
    response = rules_arm_response(
        {
            "feature_block": feature,
            "market": market,
            "voices": voices,
            "policy": policy,
        }
    )
    return {**response, "feature_block": feature}


def _leg_units(leg: dict[str, Any], result: str) -> float:
    if result == "W":
        return american_to_decimal(leg["price"]) - 1.0
    if result == "L":
        return -1.0
    return 0.0


def grade_legs(
    legs: dict[str, Any],
    *,
    game: dict[str, Any],
    closing: dict[str, Any] | None,
) -> list[dict[str, Any]]:
    """Every fired leg of an ``apply_policy`` result, graded: result, flat
    units at the posted price, CLV against ``closing`` when given."""
    graded = []
    for kind in LEG_KINDS:
        leg = legs[kind]
        if leg["selection"] == "PASS":
            continue
        result = _leg_result(
            leg,
            kind=kind,
            away_team=game["away_team"],
            home_team=game["home_team"],
            final=game["final"],
            closing=closing,
        )
        if result is None:
            continue
        graded.append(
            {
                "kind": kind,
                "label": _leg_label(leg),
                "price": int(leg["price"]),
                "edge": float(leg["edge"]),
                "stars": int(leg["confidence_stars"]),
                "result": result["result"],
                "units": round(_leg_units(leg, result["result"]), 4),
                "clv_points": result["clv_points"],
            }
        )
    return graded


def cover_outcome(game: dict[str, Any], home_spread: float) -> float | None:
    """1 when the home side covers ``home_spread``, 0 when not, None on a push."""
    settled = (
        game["final"]["home_score"] - game["final"]["away_score"]
    ) + float(home_spread)
    if settled == 0:
        return None
    return 1.0 if settled > 0 else 0.0


def win_outcome(game: dict[str, Any]) -> float | None:
    margin = game["final"]["home_score"] - game["final"]["away_score"]
    if margin == 0:
        return None
    return 1.0 if margin > 0 else 0.0


# --------------------------------------------------------------------------
# Tallies


class Tally:
    """Running scores for one policy over many games."""

    def __init__(self) -> None:
        self.games = 0
        self.ml_n = 0
        self.ml_brier = 0.0
        self.ml_log_loss = 0.0
        self.market_brier = 0.0
        self.elo_brier = 0.0
        self.elo_n = 0
        self.cover_n = 0
        self.cover_brier = 0.0
        self.fair_cover_brier = 0.0
        self.legs: list[dict[str, Any]] = []
        self.pass_reasons: Counter = Counter()

    def add_game(
        self,
        *,
        outcome: float | None,
        probability: float,
        market_probability: float,
        elo_probability: float | None,
        cover: float | None,
        p_cover_home: float,
        fair_cover: float,
    ) -> None:
        self.games += 1
        if outcome is not None:
            self.ml_n += 1
            self.ml_brier += (probability - outcome) ** 2
            clipped = min(max(probability, LOG_CLIP), 1.0 - LOG_CLIP)
            self.ml_log_loss += -math.log(clipped if outcome else 1.0 - clipped)
            self.market_brier += (market_probability - outcome) ** 2
            if elo_probability is not None:
                self.elo_n += 1
                self.elo_brier += (elo_probability - outcome) ** 2
        if cover is not None:
            self.cover_n += 1
            self.cover_brier += (p_cover_home - cover) ** 2
            self.fair_cover_brier += (fair_cover - cover) ** 2

    def add_legs(self, legs: dict[str, Any], graded: list[dict[str, Any]]) -> None:
        for kind in LEG_KINDS:
            leg = legs[kind]
            if leg["selection"] == "PASS":
                self.pass_reasons[leg.get("pass_reason") or "under threshold"] += 1
        self.legs.extend(graded)

    def summary(self) -> dict[str, Any]:
        def mean(total: float, count: int, digits: int = 5) -> float | None:
            return None if not count else round(total / count, digits)

        by_kind = {}
        for kind in LEG_KINDS:
            by_kind[kind] = _legs_summary(
                [leg for leg in self.legs if leg["kind"] == kind]
            )
        stars: dict[str, Any] = {}
        for value in sorted({leg["stars"] for leg in self.legs}):
            stars[str(value)] = _legs_summary(
                [leg for leg in self.legs if leg["stars"] == value]
            )
        return {
            "games": self.games,
            "ml": {
                "n": self.ml_n,
                "brier": mean(self.ml_brier, self.ml_n),
                "log_loss": mean(self.ml_log_loss, self.ml_n),
                "market_brier": mean(self.market_brier, self.ml_n),
                "elo_brier": mean(self.elo_brier, self.elo_n),
            },
            "cover": {
                "n": self.cover_n,
                "brier": mean(self.cover_brier, self.cover_n),
                "fair_brier": mean(self.fair_cover_brier, self.cover_n),
            },
            "legs": _legs_summary(self.legs),
            "by_kind": by_kind,
            "by_stars": stars,
            "pass_reasons": dict(sorted(self.pass_reasons.items())),
        }


def _legs_summary(legs: list[dict[str, Any]]) -> dict[str, Any]:
    wins = sum(1 for leg in legs if leg["result"] == "W")
    losses = sum(1 for leg in legs if leg["result"] == "L")
    pushes = sum(1 for leg in legs if leg["result"] == "P")
    total = sum(leg["units"] for leg in legs)
    clv = [leg["clv_points"] for leg in legs if leg["clv_points"] is not None]
    return {
        "bets": len(legs),
        "record": f"{wins}-{losses}-{pushes}",
        "wins": wins,
        "losses": losses,
        "pushes": pushes,
        "units": round(total, 2),
        "roi": None if not legs else round(total / len(legs), 4),
        "clv_n": len(clv),
        "clv_mean": None if not clv else round(sum(clv) / len(clv), 3),
    }


# --------------------------------------------------------------------------
# Historical replay


def _as_of_table(
    rows: list[dict[str, Any]], season: int, *, first: int = TABLE_FIRST_SEASON
) -> tuple[dict[str, Any] | None, list[int]]:
    """The empirical table for ``season`` from the seasons before it."""
    seasons = [year for year in range(first, season) if year < season]
    if not seasons:
        return None, []
    games = read_games_from_rows(rows, seasons)
    raw = build_table(games, seasons=seasons, min_games=TABLE_MIN_GAMES)
    return parse_margin_table(raw, path=f"as-of-{season}"), seasons


def read_games_from_rows(
    rows: Iterable[dict[str, Any]], seasons: Iterable[int]
) -> list[dict[str, Any]]:
    """``scripts.build_nfl_margins.read_games`` over rows already in memory."""
    wanted = set(int(season) for season in seasons)
    games = []
    for row in rows:
        if int(row["season"]) not in wanted:
            continue
        game = game_from_row(row)
        if game is not None:
            games.append(game)
    return games


def replay_seasons(
    rows: list[dict[str, Any]],
    *,
    seasons: Iterable[int],
    points: list[tuple[dict[str, Any], str]],
    registry: dict[str, Any] | None = None,
    params: dict[str, float] | None = None,
    market_source: str = "close",
    open_close: dict[str, dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Score every grid point over ``seasons``.

    ``market_source`` is ``close`` (nflverse close, no opening, no CLV) or
    ``open`` (ESPN open as the market, ESPN close as the closing line; games
    without both are skipped). Returns ``{"seasons", "games", "skipped",
    "tables", "results": {key label: summary}}``.
    """
    registry = registry if registry is not None else load_registry()
    params = params if params is not None else elo_parameters()
    seasons = sorted(int(season) for season in seasons)
    games = history_games(rows, seasons)
    if market_source == "open":
        games = attach_open_close(games, open_close if open_close is not None else load_open_close(OPEN_CLOSE_JSON))
    inputs = rating_inputs(rows, seasons=seasons, params=params)
    unique: dict[tuple[Any, ...], tuple[dict[str, Any], str]] = {}
    for policy, ladder in points:
        unique.setdefault(policy_key(policy, ladder), (policy, ladder))
    points = list(unique.values())
    tallies: dict[tuple[Any, ...], Tally] = {}
    scored = 0
    lambdas = sorted({float(policy["shrink_lambda"]) for policy, _ladder in points})
    base = base_policy_block(registry)
    tables: dict[str, list[int]] = {}
    skipped = Counter()
    started = time.monotonic()
    for season in seasons:
        table, table_seasons = _as_of_table(rows, season)
        tables[str(season)] = table_seasons
        context = margin_table_override(table) if table is not None else _NullContext()
        with context:
            for game in games:
                if game["season"] != season:
                    continue
                key = (game["season"], game["week"], game["away_team"], game["home_team"])
                if key not in inputs:
                    skipped["no rating"] += 1
                    continue
                if market_source == "open":
                    latest = game.get("espn_open")
                    closing = game.get("espn_close")
                    if latest is None or closing is None:
                        skipped["no ESPN open and close"] += 1
                        continue
                    market = market_block_from_lines(None, latest, bookmaker=str(game.get("espn_provider") or ""))
                else:
                    market = market_block_from_lines(None, game["close"], bookmaker="nflverse")
                    closing = None
                voice = rating_voice(
                    game, inputs[key], params=params, registry=registry, market=market, policy=make_policy(base)
                )
                estimates = {
                    lam: rules_estimate([voice], market, make_policy(base, shrink_lambda=lam), home_team=game["home_team"])
                    for lam in lambdas
                }
                _score_game(
                    game,
                    market=market,
                    closing=closing,
                    estimates=estimates,
                    points=points,
                    tallies=tallies,
                    elo_probability=inputs[key]["elo_probability"],
                )
                scored += 1
    return {
        "seasons": seasons,
        "market_source": market_source,
        "games": scored,
        "skipped": dict(skipped),
        "tables": tables,
        "elapsed_seconds": round(time.monotonic() - started, 1),
        "results": {key_label(key): tally.summary() for key, tally in sorted(tallies.items(), key=lambda item: item[0])},
    }


class _NullContext:
    def __enter__(self) -> None:
        return None

    def __exit__(self, *_exc: Any) -> None:
        return None


def _score_game(
    game: dict[str, Any],
    *,
    market: dict[str, Any],
    closing: dict[str, Any] | None,
    estimates: dict[float, dict[str, Any]],
    points: list[tuple[dict[str, Any], str]],
    tallies: dict[tuple[Any, ...], Tally],
    elo_probability: float | None,
) -> None:
    """Apply every grid point to one game's estimates and update the tallies."""
    outcome = win_outcome(game)
    cover = cover_outcome(game, market["latest"]["home_spread"])
    fair_home = float(market["fair"]["home_ml"])
    fair_cover = float(market["fair"]["home_cover"])
    for policy, ladder in points:
        estimate = estimates[float(policy["shrink_lambda"])]
        legs = apply_policy(
            home_win_probability=float(estimate["home_win_probability"]),
            expected_home_margin=float(estimate["expected_home_margin"]),
            projected_total=float(estimate["projected_total"]),
            market=market,
            policy=policy,
            away_team=game["away_team"],
            home_team=game["home_team"],
        )
        key = policy_key(policy, ladder)
        tally = tallies.setdefault(key, Tally())
        tally.add_game(
            outcome=outcome,
            probability=float(estimate["home_win_probability"]),
            market_probability=fair_home,
            elo_probability=elo_probability,
            cover=cover,
            p_cover_home=float(legs["p_cover_home"]),
            fair_cover=fair_cover,
        )
        tally.add_legs(legs, grade_legs(legs, game=game, closing=closing))


# --------------------------------------------------------------------------
# Selection


def _is_default(key: tuple[Any, ...], base_key: tuple[Any, ...], fields: Iterable[int]) -> bool:
    return all(key[index] == base_key[index] for index in fields)


def _monotone_roi(by_stars: dict[str, Any]) -> bool | None:
    """True when ROI never falls as stars rise over buckets with enough bets;
    None when fewer than two buckets qualify."""
    rois = [
        block["roi"]
        for _stars, block in sorted(by_stars.items(), key=lambda item: int(item[0]))
        if block["bets"] >= MIN_BETS_PER_STAR
    ]
    if len(rois) < 2:
        return None
    return all(later >= earlier for earlier, later in zip(rois, rois[1:]))


def select_policy(
    results: dict[str, dict[str, Any]],
    *,
    base: dict[str, Any],
    grid: dict[str, tuple[Any, ...]] | None = None,
    require_clv: bool = False,
) -> dict[str, Any]:
    """Pick a policy from fit-season results by the documented rule.

    ``results`` maps a key label to a tally summary. Returns the chosen
    overrides, the ladder, the steps taken, and the registry values.
    """
    grid = grid if grid is not None else GRID
    keyed = {_parse_label(label): summary for label, summary in results.items()}
    base_ladder = registry_ladder(base)
    base_key = policy_key(make_policy(base, ladder=base_ladder), base_ladder)
    steps: list[dict[str, Any]] = []

    def closest_to_default(candidates: list[tuple[Any, ...]], index: int) -> Any:
        return min(candidates, key=lambda key: (abs(float(key[index]) - float(base_key[index])), key[index]))

    # 1. lambda on the ML Brier (identical across the leg knobs).
    by_lambda: dict[float, float] = {}
    for key, summary in keyed.items():
        brier = summary["ml"]["brier"]
        if brier is not None:
            by_lambda.setdefault(key[0], brier)
    best_brier = min(by_lambda.values())
    lambda_keys = [(lam,) for lam, brier in by_lambda.items() if brier == best_brier]
    chosen_lambda = closest_to_default(lambda_keys, 0)[0]
    steps.append({"knob": "shrink_lambda", "rule": "lowest fit-season ML Brier", "chosen": chosen_lambda, "registry": base_key[0], "table": {f"{lam:g}": brier for lam, brier in sorted(by_lambda.items())}})

    # 2. sigma_margin and margin_model on the cover Brier at that lambda.
    by_sigma_model: dict[tuple[float, str], float] = {}
    for key, summary in keyed.items():
        if key[0] != chosen_lambda or summary["cover"]["brier"] is None:
            continue
        by_sigma_model.setdefault((key[1], key[2]), summary["cover"]["brier"])
    best_cover = min(by_sigma_model.values())
    ties = [pair for pair, brier in by_sigma_model.items() if brier == best_cover]
    ties.sort(key=lambda pair: (pair[1] != base_key[2], abs(pair[0] - base_key[1]), pair[0]))
    chosen_sigma, chosen_model = ties[0]
    steps.append({"knob": "sigma_margin, margin_model", "rule": "lowest fit-season cover Brier at the chosen lambda", "chosen": [chosen_sigma, chosen_model], "registry": [base_key[1], base_key[2]], "table": {f"{sigma:g}|{model}": brier for (sigma, model), brier in sorted(by_sigma_model.items())}})

    # 3. edge threshold and EV floor: the registry values unless a candidate
    #    beats them on ROI (and CLV) with enough bets, on the default ladder.
    leg_table: dict[str, dict[str, Any]] = {}
    default_leg_key = (chosen_lambda, chosen_sigma, chosen_model, base_key[3], base_key[4], base_ladder)
    default_legs = keyed[default_leg_key]["legs"]
    chosen_edge, chosen_floor = base_key[3], base_key[4]
    best: tuple[Any, ...] | None = None
    for key, summary in keyed.items():
        if key[:3] != (chosen_lambda, chosen_sigma, chosen_model) or key[5] != base_ladder:
            continue
        legs = summary["legs"]
        leg_table[f"{key[3]:g}|{key[4]:g}"] = legs
        if (key[3], key[4]) == (base_key[3], base_key[4]):
            continue
        if legs["bets"] < MIN_BETS_TO_PREFER:
            continue
        if legs["roi"] is None or default_legs["roi"] is None or legs["roi"] <= default_legs["roi"]:
            continue
        if require_clv:
            if legs["clv_mean"] is None or default_legs["clv_mean"] is None or legs["clv_mean"] <= default_legs["clv_mean"]:
                continue
        if best is None or legs["roi"] > keyed[best]["legs"]["roi"]:
            best = key
    if best is not None:
        chosen_edge, chosen_floor = best[3], best[4]
    steps.append({"knob": "edge_threshold, min_ev_per_unit", "rule": f"registry values unless a candidate beats them on ROI{' and mean CLV' if require_clv else ''} with at least {MIN_BETS_TO_PREFER} fit-season bets", "chosen": [chosen_edge, chosen_floor], "registry": [base_key[3], base_key[4]], "table": leg_table})

    # 4. the star ladder: default unless the alternative is monotone where
    #    the default is not.
    chosen_ladder = base_ladder
    ladder_table = {}
    for ladder in STAR_LADDERS:
        key = (chosen_lambda, chosen_sigma, chosen_model, chosen_edge, chosen_floor, ladder)
        by_stars = keyed[key]["by_stars"]
        ladder_table[ladder] = {"by_stars": by_stars, "monotone": _monotone_roi(by_stars)}
    other = next(name for name in STAR_LADDERS if name != base_ladder)
    if ladder_table[base_ladder]["monotone"] is False and ladder_table[other]["monotone"] is True:
        chosen_ladder = other
    steps.append({"knob": "star_edges", "rule": "the registry ladder unless the alternative is monotone in ROI where it is not", "chosen": chosen_ladder, "registry": base_ladder, "table": ladder_table})

    # For reading only: the same two tables at the registry's own lambda,
    # sigma and margin model, where the pool carries the voices' weight and
    # legs actually fire. No selection happens here.
    at_registry = {
        "shrink_lambda": base_key[0],
        "cover_brier_by_sigma_model": {
            f"{key[1]:g}|{key[2]}": summary["cover"]["brier"]
            for key, summary in sorted(keyed.items())
            if key[0] == base_key[0] and key[3] == base_key[3] and key[4] == base_key[4] and key[5] == base_ladder
        },
        "legs_by_edge_floor": {
            f"{key[3]:g}|{key[4]:g}": summary["legs"]
            for key, summary in sorted(keyed.items())
            if key[:3] == base_key[:3] and key[5] == base_ladder
        },
        "by_stars": {
            ladder: keyed[(base_key[0], base_key[1], base_key[2], base_key[3], base_key[4], ladder)]["by_stars"]
            for ladder in STAR_LADDERS
        },
    }
    overrides = {
        "shrink_lambda": chosen_lambda,
        "sigma_margin": chosen_sigma,
        "margin_model": chosen_model,
        "edge_threshold": chosen_edge,
        "min_ev_per_unit": chosen_floor,
    }
    chosen = make_policy(base, ladder=chosen_ladder, **overrides)
    return {
        "overrides": overrides,
        "star_ladder": chosen_ladder,
        "star_edges": chosen["star_edges"],
        "key": key_label(policy_key(chosen, chosen_ladder)),
        "registry_key": key_label(base_key),
        "changed": {
            field: {"registry": base_key[index], "chosen": value}
            for index, (field, value) in enumerate(zip(POLICY_KEY_FIELDS, policy_key(chosen, chosen_ladder)))
            if value != base_key[index]
        },
        "steps": steps,
        "at_registry_lambda": at_registry,
    }


def _parse_label(label: str) -> tuple[Any, ...]:
    values: list[Any] = []
    for piece in label.split("|"):
        field, raw = piece.split("=", 1)
        if field in ("margin_model", "star_ladder"):
            values.append(raw)
        else:
            values.append(float(raw))
    return tuple(values)


def policy_block_text(base: dict[str, Any], selection: dict[str, Any]) -> str:
    """The chosen policy as the ``aggregator_policy`` block would read."""
    chosen = make_policy(base, ladder=selection["star_ladder"], **selection["overrides"])
    lines = ["aggregator_policy:"]
    for key in DEFAULT_POLICY:
        value = chosen[key]
        if isinstance(value, list):
            rendered = "[" + ", ".join(f"{item:g}" for item in value) + "]"
        elif isinstance(value, float):
            rendered = f"{value:g}"
        else:
            rendered = str(value)
        marker = "   # changed" if key in selection["changed"] or (key == "star_edges" and "star_ladder" in selection["changed"]) else ""
        lines.append(f"  {key}: {rendered}{marker}")
    return "\n".join(lines)


# --------------------------------------------------------------------------
# The grid run end to end


def run_grid(
    rows: list[dict[str, Any]],
    *,
    fit_seasons: Iterable[int] = DEFAULT_FIT_SEASONS,
    check_seasons: Iterable[int] = DEFAULT_CHECK_SEASONS,
    registry: dict[str, Any] | None = None,
    grid: dict[str, tuple[Any, ...]] | None = None,
) -> dict[str, Any]:
    """Fit on ``fit_seasons``, select, confirm on ``check_seasons``."""
    registry = registry if registry is not None else load_registry()
    base = base_policy_block(registry)
    params = elo_parameters()
    points = grid_points(base, grid)
    fit = replay_seasons(rows, seasons=fit_seasons, points=points, registry=registry, params=params)
    selection = select_policy(fit["results"], base=base, grid=grid)
    base_ladder = registry_ladder(base)
    chosen = make_policy(base, ladder=selection["star_ladder"], **selection["overrides"])
    registry_policy = make_policy(base, ladder=base_ladder)
    check = replay_seasons(
        rows,
        seasons=check_seasons,
        points=[(chosen, selection["star_ladder"]), (registry_policy, base_ladder)],
        registry=registry,
        params=params,
    )
    chosen_label = key_label(policy_key(chosen, selection["star_ladder"]))
    registry_label = key_label(policy_key(registry_policy, base_ladder))
    return {
        "elo_parameters": params,
        "fit_seasons": fit["seasons"],
        "check_seasons": check["seasons"],
        "grid": {key: list(values) for key, values in (grid or GRID).items()},
        "star_ladders": {name: list(offsets) for name, offsets in STAR_LADDERS.items()},
        "fit": fit,
        "selection": selection,
        "policy_block": policy_block_text(base, selection),
        "check": {
            "seasons": check["seasons"],
            "games": check["games"],
            "tables": check["tables"],
            "chosen": {"key": chosen_label, **check["results"][chosen_label]},
            "registry": {"key": registry_label, **check["results"][registry_label]},
        },
    }


# --------------------------------------------------------------------------
# CLV: bets at the ESPN open


def run_clv(
    rows: list[dict[str, Any]],
    *,
    seasons: Iterable[int] = DEFAULT_CLV_SEASONS,
    selection: dict[str, Any] | None = None,
    registry: dict[str, Any] | None = None,
    open_close: dict[str, dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Bets placed at the ESPN open, graded and CLV'd against the ESPN close,
    for the chosen policy (``selection`` from :func:`run_grid`, else the
    registry policy) and the registry policy, plus the edge/floor
    candidates at the chosen lambda, sigma and margin model."""
    registry = registry if registry is not None else load_registry()
    base = base_policy_block(registry)
    base_ladder = registry_ladder(base)
    registry_policy = make_policy(base, ladder=base_ladder)
    if selection is None:
        chosen, chosen_ladder = registry_policy, base_ladder
    else:
        chosen_ladder = selection["star_ladder"]
        chosen = make_policy(base, ladder=chosen_ladder, **selection["overrides"])
    points = [(chosen, chosen_ladder), (registry_policy, base_ladder)]
    for edge in GRID["edge_threshold"]:
        for floor in GRID["min_ev_per_unit"]:
            points.append(
                (
                    make_policy(
                        base,
                        ladder=chosen_ladder,
                        shrink_lambda=chosen["shrink_lambda"],
                        sigma_margin=chosen["sigma_margin"],
                        margin_model=chosen["margin_model"],
                        edge_threshold=edge,
                        min_ev_per_unit=floor,
                    ),
                    chosen_ladder,
                )
            )
    seen: dict[str, tuple[dict[str, Any], str]] = {}
    for policy, ladder in points:
        seen.setdefault(key_label(policy_key(policy, ladder)), (policy, ladder))
    replay = replay_seasons(
        rows,
        seasons=seasons,
        points=list(seen.values()),
        registry=registry,
        market_source="open",
        open_close=open_close,
    )
    chosen_label = key_label(policy_key(chosen, chosen_ladder))
    registry_label = key_label(policy_key(registry_policy, base_ladder))
    return {
        "seasons": replay["seasons"],
        "games": replay["games"],
        "skipped": replay["skipped"],
        "chosen": {"key": chosen_label, **replay["results"][chosen_label]},
        "registry": {"key": registry_label, **replay["results"][registry_label]},
        "edge_floor_table": {
            label: summary["legs"]
            for label, summary in replay["results"].items()
        },
    }


# --------------------------------------------------------------------------
# Veto calibration on ESPN open -> close


def adverse_legs(
    game: dict[str, Any], *, kind: str, threshold: float
) -> list[dict[str, Any]]:
    """The legs of ``game`` whose own side got cheaper since open by at least
    ``threshold`` (points for ``spread``/``total``, cents for ``price``),
    graded at the ESPN close with flat one unit at the close price."""
    opening, latest = game["espn_open"], game["espn_close"]
    movement = movement_since_open(opening, latest)
    tolerance = 1e-9
    candidates: list[tuple[str, str, str]] = []  # (leg kind, selection, price field)
    away, home = game["away_team"], game["home_team"]
    if kind == "spread":
        delta = movement.get("home_spread")
        if delta is not None:
            if float(delta) >= threshold - tolerance:
                candidates.append(("side", home, "home_spread_price"))
            if -float(delta) >= threshold - tolerance:
                candidates.append(("side", away, "away_spread_price"))
    elif kind == "total":
        delta = movement.get("total")
        if delta is not None:
            if -float(delta) >= threshold - tolerance:
                candidates.append(("total", "Over", "over_price"))
            if float(delta) >= threshold - tolerance:
                candidates.append(("total", "Under", "under_price"))
    elif kind == "price":
        for field, leg_kind, selection in (
            ("home_spread_price", "side", home),
            ("away_spread_price", "side", away),
            ("over_price", "total", "Over"),
            ("under_price", "total", "Under"),
        ):
            delta = movement.get(field)
            if delta is not None and float(delta) >= threshold - tolerance:
                candidates.append((leg_kind, selection, field))
    else:
        raise ValueError(f"Unknown veto kind: {kind}")
    legs = []
    for leg_kind, selection, price_field in candidates:
        if leg_kind == "side":
            line = latest["home_spread"] if selection == home else latest["away_spread"]
        else:
            line = latest["total"]
        leg = {"selection": selection, "line": float(line), "price": int(latest[price_field]), "edge": 0.0, "confidence_stars": 1}
        result = _leg_result(leg, kind=leg_kind, away_team=away, home_team=home, final=game["final"], closing=None)
        if result is None:
            continue
        legs.append(
            {
                "event_id": game["event_id"],
                "season": game["season"],
                "kind": leg_kind,
                "label": _leg_label(leg),
                "price": leg["price"],
                "move": movement.get("home_spread" if kind == "spread" else "total" if kind == "total" else price_field),
                "result": result["result"],
                "units": round(_leg_units(leg, result["result"]), 4),
            }
        )
    return legs


def run_veto(
    rows: list[dict[str, Any]],
    *,
    seasons: Iterable[int] = DEFAULT_CLV_SEASONS,
    open_close: dict[str, dict[str, Any]] | None = None,
    grid: dict[str, tuple[float, ...]] | None = None,
    registry: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """The veto calibration table over every event with ESPN open and close."""
    grid = grid if grid is not None else VETO_GRID
    base = base_policy_block(registry)
    games = attach_open_close(
        history_games(rows, seasons),
        open_close if open_close is not None else load_open_close(OPEN_CLOSE_JSON),
    )
    usable = [game for game in games if game.get("espn_open") and game.get("espn_close")]
    moved = 0
    for game in usable:
        movement = movement_since_open(game["espn_open"], game["espn_close"])
        if any(value not in (None, 0) for value in movement.values()):
            moved += 1
    table: dict[str, dict[str, Any]] = {}
    for kind, thresholds in grid.items():
        table[kind] = {}
        for threshold in thresholds:
            legs = [leg for game in usable for leg in adverse_legs(game, kind=kind, threshold=threshold)]
            summary = _legs_summary([{**leg, "clv_points": None} for leg in legs])
            table[kind][f"{threshold:g}"] = {
                **summary,
                "events": len({leg["event_id"] for leg in legs}),
                "by_kind": {leg_kind: _legs_summary([{**leg, "clv_points": None} for leg in legs if leg["kind"] == leg_kind]) for leg_kind in LEG_KINDS},
            }
    recommendation = {}
    for kind, knob in VETO_KNOBS.items():
        current = float(base[knob])
        current_label = f"{current:g}"
        current_roi = table[kind].get(current_label, {}).get("roi")
        best_label, best_roi = current_label, current_roi
        for label, block in table[kind].items():
            if label == current_label or block["bets"] < MIN_VETO_LEGS or block["roi"] is None:
                continue
            # A lower adverse-side ROI means the veto removes worse legs;
            # it displaces the current knob only by a clear margin.
            if current_roi is None or block["roi"] <= current_roi - VETO_CLEAR_MARGIN:
                if best_roi is None or block["roi"] < best_roi:
                    best_label, best_roi = label, block["roi"]
        recommendation[knob] = {
            "current": current,
            "current_roi": current_roi,
            "recommended": float(best_label),
            "recommended_roi": best_roi,
            "keep": best_label == current_label,
        }
    return {
        "seasons": sorted(int(season) for season in seasons),
        "events": len(usable),
        "events_with_movement": moved,
        "events_without_open_close": len(games) - len(usable),
        "table": table,
        "recommendation": recommendation,
    }


# --------------------------------------------------------------------------
# Ledger refit (WP10)


def _parsed(value: Any) -> dict[str, Any] | None:
    if isinstance(value, dict):
        return value
    if value in (None, ""):
        return None
    try:
        parsed = json.loads(str(value))
    except json.JSONDecodeError:
        return None
    return parsed if isinstance(parsed, dict) else None


def ledger_games(
    rows: Iterable[dict[str, Any]],
    *,
    finals: Iterable[dict[str, Any]],
    snapshots: Iterable[dict[str, Any]],
    registry: dict[str, Any] | None = None,
    approved_only: bool = True,
) -> dict[str, Any]:
    """Persisted ``god_rules`` rows that resolve against a final, with their
    voices, market, policy, and closing line; one entry per row."""
    registry = registry if registry is not None else load_registry()
    finals = list(finals)
    snapshots = list(snapshots)
    games = []
    skipped: Counter = Counter()
    for row in rows:
        if str(row.get("expert_id") or "") != RULES_EXPERT_ID:
            continue
        if str(row.get("generation_status") or "") != "valid":
            skipped["not valid"] += 1
            continue
        if approved_only and str(row.get("review_status") or "") != "approved":
            skipped["not approved"] += 1
            continue
        payload = _parsed(row.get("input_json"))
        if not payload or not isinstance(payload.get("market"), dict):
            skipped["no persisted input"] += 1
            continue
        game = payload["game"]
        final = _matching_history_game(
            {
                "commence_time_utc": game["commence_time_utc"],
                "away_team": game["away_team"],
                "home_team": game["home_team"],
            },
            finals,
        )
        if final is None:
            skipped["unresolved"] += 1
            continue
        voices = copy.deepcopy(payload["voices"])
        for voice in voices:
            if "markets" not in voice:
                config = registry["experts"].get(voice["voice_id"]) or {}
                voice["markets"] = voice_markets(config)
        feature = payload["feature_block"]
        games.append(
            {
                "opinion_id": str(row.get("opinion_id") or ""),
                "event_id": str(game["event_id"]),
                "season": game.get("season"),
                "week": game.get("week"),
                "away_team": str(game["away_team"]),
                "home_team": str(game["home_team"]),
                "kickoff": str(game["commence_time_utc"]),
                "final": {"away_score": int(final["away_score"]), "home_score": int(final["home_score"])},
                "market": payload["market"],
                "closing": closing_market(str(game["event_id"]), str(game["commence_time_utc"]), snapshots),
                "voices": voices,
                "weighting": {
                    "weights": dict(feature.get("hedge_weights") or feature["weights"]),
                    "active": bool(feature.get("weights_active")),
                    "mean_brier": None,
                },
                "policy": {**DEFAULT_POLICY, **(payload.get("policy") or {})},
            }
        )
    games.sort(key=lambda game: (_parse_time(game["kickoff"]), game["event_id"], game["opinion_id"]))
    return {"games": games, "skipped": dict(skipped)}


def _ledger_estimates(game: dict[str, Any], lambdas: Iterable[float]) -> dict[float, dict[str, Any]]:
    return {
        lam: rules_estimate(
            game["voices"],
            game["market"],
            make_policy(game["policy"], ladder=registry_ladder(game["policy"]), shrink_lambda=lam),
            home_team=game["home_team"],
            weighting=game["weighting"],
        )
        for lam in lambdas
    }


def vetoed_legs(
    game: dict[str, Any], estimate: dict[str, Any], policy: dict[str, Any]
) -> list[dict[str, Any]]:
    """Legs the veto removed under ``policy`` and how the un-vetoed leg
    (the same policy with every veto knob off) would have graded.

    The sweep passes a policy with one veto knob set and the other two
    switched off, so every leg listed is attributed to that one knob.
    """
    common = {
        "home_win_probability": float(estimate["home_win_probability"]),
        "expected_home_margin": float(estimate["expected_home_margin"]),
        "projected_total": float(estimate["projected_total"]),
        "market": game["market"],
        "away_team": game["away_team"],
        "home_team": game["home_team"],
    }
    with_veto = apply_policy(policy=policy, **common)
    without = apply_policy(policy={**policy, **NO_VETO}, **common)
    vetoed = []
    graded = {leg["kind"]: leg for leg in grade_legs(without, game=game, closing=game["closing"])}
    for kind in LEG_KINDS:
        if with_veto[kind]["pass_reason"] == ADVERSE_MOVE_REASON and kind in graded:
            vetoed.append({**graded[kind], "event_id": game["event_id"], "opinion_id": game["opinion_id"]})
    return vetoed


def run_ledger(
    rows: Iterable[dict[str, Any]],
    *,
    finals: Iterable[dict[str, Any]],
    snapshots: Iterable[dict[str, Any]],
    registry: dict[str, Any] | None = None,
    approved_only: bool = True,
    grid: dict[str, tuple[Any, ...]] | None = None,
    fitted: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """The grid and the veto sweep over persisted rules rows.

    ``fitted`` is a :func:`run_grid` selection to compare against; without
    it the comparison is against the registry policy.
    """
    registry = registry if registry is not None else load_registry()
    grid = grid if grid is not None else GRID
    prepared = ledger_games(rows, finals=finals, snapshots=snapshots, registry=registry, approved_only=approved_only)
    games = prepared["games"]
    base = base_policy_block(registry)
    base_ladder = registry_ladder(base)
    lambdas = [float(value) for value in grid["shrink_lambda"]]
    tallies: dict[tuple[Any, ...], Tally] = {}
    veto_tallies: dict[str, dict[str, list[dict[str, Any]]]] = {
        kind: {f"{value:g}": [] for value in VETO_GRID[kind]} for kind in VETO_GRID
    }
    for game in games:
        # Each row replays under its own persisted policy as the base, so a
        # knob the row predates takes its default, never a live value.
        points = grid_points(game["policy"], grid)
        estimates = _ledger_estimates(game, lambdas)
        _score_game(
            game,
            market=game["market"],
            closing=game["closing"],
            estimates=estimates,
            points=points,
            tallies=tallies,
            elo_probability=None,
        )
        persisted = make_policy(game["policy"], ladder=registry_ladder(game["policy"]))
        estimate = estimates[float(persisted["shrink_lambda"])]
        for kind, knob in VETO_KNOBS.items():
            for value in VETO_GRID[kind]:
                candidate = make_policy(
                    game["policy"],
                    ladder=registry_ladder(game["policy"]),
                    **{**NO_VETO, knob: value},
                )
                veto_tallies[kind][f"{value:g}"].extend(vetoed_legs(game, estimate, candidate))
    results = {key_label(key): tally.summary() for key, tally in sorted(tallies.items(), key=lambda item: item[0])}
    selection = select_policy(results, base=base, grid=grid) if results else None
    comparison = None
    if selection is not None:
        reference = fitted["overrides"] if fitted else {field: base[field] for field in ("shrink_lambda", "sigma_margin", "margin_model", "edge_threshold", "min_ev_per_unit")}
        comparison = {
            "against": "backtest" if fitted else "registry",
            "knobs": {
                field: {"ledger": selection["overrides"][field], "reference": reference[field], "agree": selection["overrides"][field] == reference[field]}
                for field in selection["overrides"]
            },
        }
    veto_table = {
        kind: {
            label: {**_legs_summary(legs), "legs": [f"{leg['label']} ({leg['price']:+d}) {leg['result']}" for leg in legs]}
            for label, legs in blocks.items()
        }
        for kind, blocks in veto_tallies.items()
    }
    return {
        "graded_games": len(games),
        "informational": len(games) < MIN_REFIT_GAMES,
        "minimum_games": MIN_REFIT_GAMES,
        "skipped": prepared["skipped"],
        "rows": [
            {"opinion_id": game["opinion_id"], "event_id": game["event_id"], "week": game["week"], "teams": f"{game['away_team']} @ {game['home_team']}", "final": f"{game['final']['away_score']}-{game['final']['home_score']}", "closing_available": game["closing"] is not None}
            for game in games
        ],
        "results": results,
        "selection": selection,
        "comparison": comparison,
        "veto": veto_table,
        "registry_ladder": base_ladder,
    }


# --------------------------------------------------------------------------
# Text rendering


def _fmt(value: Any, spec: str = ".4f") -> str:
    return "—" if value is None else format(float(value), spec)


def _legs_line(legs: dict[str, Any]) -> str:
    clv = "" if legs["clv_mean"] is None else f" clv={legs['clv_mean']:+.2f} (n={legs['clv_n']})"
    return f"bets={legs['bets']:<4} {legs['record']:<10} units={legs['units']:+.2f} roi={_fmt(legs['roi'], '+.3f')}{clv}"


def _summary_lines(title: str, summary: dict[str, Any]) -> list[str]:
    ml, cover = summary["ml"], summary["cover"]
    lines = [
        f"{title}: games={summary['games']}",
        f"  ML Brier arm={_fmt(ml['brier'])} market={_fmt(ml['market_brier'])} elo={_fmt(ml['elo_brier'])} log_loss={_fmt(ml['log_loss'])} (n={ml['n']})",
        f"  cover Brier arm={_fmt(cover['brier'])} fair={_fmt(cover['fair_brier'])} (n={cover['n']})",
        f"  legs   {_legs_line(summary['legs'])}",
    ]
    for kind in LEG_KINDS:
        lines.append(f"  {kind:<6} {_legs_line(summary['by_kind'][kind])}")
    for stars, block in summary["by_stars"].items():
        lines.append(f"  {'★' * int(stars):<6} {_legs_line(block)}")
    if summary["pass_reasons"]:
        lines.append("  passes: " + ", ".join(f"{reason} {count}" for reason, count in summary["pass_reasons"].items()))
    return lines


def format_grid_report(result: dict[str, Any]) -> list[str]:
    fit, selection, check = result["fit"], result["selection"], result["check"]
    lines = [
        f"Backtest grid: fit {fit['seasons'][0]}-{fit['seasons'][-1]} ({fit['games']} games, {fit['elapsed_seconds']} s, "
        f"{len(fit['results'])} policies), confirm {', '.join(str(s) for s in check['seasons'])} untouched ({check['games']} games).",
        "Committee: the rating voice alone (Elo K "
        f"{result['elo_parameters']['k']:g}, hfa {result['elo_parameters']['hfa']:g}, regression {result['elo_parameters']['regression']:.4g}; "
        "parameters fitted on 2023-2024, so the fit seasons are in-sample for Elo; 2025 is untouched by both fits). "
        "Market: nflverse close, no opening (the veto never fires). Empirical tables as of each season: "
        + ", ".join(f"{season}: {seasons[0]}-{seasons[-1]}" if seasons else f"{season}: none" for season, seasons in fit["tables"].items())
        + ".",
        "The rating voice informs the side pool only, so the blend total is the market line: under the normal model no total fires, "
        "under the empirical model only the bin's asymmetry at the line can clear a low threshold; "
        "sigma_total and the total veto are not identifiable here and keep their values.",
        "",
        "Selection:",
    ]
    for step in selection["steps"]:
        lines.append(f"  {step['knob']}: {step['rule']} -> chosen {step['chosen']} (registry {step['registry']})")
        table = step["table"]
        if step["knob"] in ("shrink_lambda", "sigma_margin, margin_model"):
            lines.append("    " + "  ".join(f"{label}: {_fmt(value, '.5f')}" for label, value in table.items()))
        elif step["knob"] == "edge_threshold, min_ev_per_unit":
            for label, legs in table.items():
                lines.append(f"    edge|floor {label:<10} {_legs_line(legs)}")
        else:
            for ladder, block in table.items():
                lines.append(f"    {ladder}: monotone={block['monotone']}")
                for stars, legs in block["by_stars"].items():
                    lines.append(f"      {'★' * int(stars):<6} {_legs_line(legs)}")
    view = selection["at_registry_lambda"]
    lines.append("")
    lines.append(f"For reading, at the registry lambda {view['shrink_lambda']:g} (no selection here):")
    lines.append("  cover Brier by sigma|model: " + "  ".join(f"{label}: {_fmt(value, '.5f')}" for label, value in view["cover_brier_by_sigma_model"].items()))
    for label, legs in view["legs_by_edge_floor"].items():
        lines.append(f"  edge|floor {label:<10} {_legs_line(legs)}")
    for ladder, by_stars in view["by_stars"].items():
        lines.append(f"  {ladder} ladder: monotone={_monotone_roi(by_stars)}")
        for stars, legs in by_stars.items():
            lines.append(f"    {'★' * int(stars):<6} {_legs_line(legs)}")
    lines.append("")
    lines.append(f"Chosen: {selection['key']}")
    lines.append(f"Registry: {selection['registry_key']}")
    lines.append("Changed: " + (", ".join(f"{field} {block['registry']} -> {block['chosen']}" for field, block in selection["changed"].items()) or "nothing"))
    lines.append("")
    lines.extend(_summary_lines(f"Fit seasons, chosen ({selection['key']})", fit["results"][selection["key"]]))
    lines.extend(_summary_lines(f"Fit seasons, registry ({selection['registry_key']})", fit["results"][selection["registry_key"]]))
    lines.append("")
    lines.extend(_summary_lines(f"Check seasons, chosen ({check['chosen']['key']})", check["chosen"]))
    lines.extend(_summary_lines(f"Check seasons, registry ({check['registry']['key']})", check["registry"]))
    lines.append("")
    lines.append("Policy block (not written; moe/experts.yaml is the operator's call):")
    lines.extend("  " + line for line in result["policy_block"].splitlines())
    return lines


def format_clv_report(result: dict[str, Any]) -> list[str]:
    lines = [
        f"CLV replay: bets at the ESPN open, closing line = ESPN close, seasons {', '.join(str(s) for s in result['seasons'])}: "
        f"{result['games']} games" + (f"; skipped {result['skipped']}" if result["skipped"] else "") + ".",
    ]
    lines.extend(_summary_lines(f"Chosen ({result['chosen']['key']})", result["chosen"]))
    lines.extend(_summary_lines(f"Registry ({result['registry']['key']})", result["registry"]))
    lines.append("Edge/floor candidates at the chosen lambda, sigma and margin model:")
    for label, legs in result["edge_floor_table"].items():
        lines.append(f"  {label}")
        lines.append(f"    {_legs_line(legs)}")
    return lines


def format_veto_report(result: dict[str, Any]) -> list[str]:
    lines = [
        f"Veto calibration on ESPN open -> close, seasons {', '.join(str(s) for s in result['seasons'])}: "
        f"{result['events']} events with open and close ({result['events_with_movement']} moved; "
        f"{result['events_without_open_close']} without a usable pair). Adverse side graded at the close, flat 1u at the close price; "
        "negative units mean the veto removes losing legs.",
    ]
    for kind, blocks in result["table"].items():
        unit = "cents" if kind == "price" else "points"
        lines.append(f"  {kind} ({unit}):")
        for threshold, block in blocks.items():
            by_kind = "  ".join(f"{leg_kind} {b['record']} {b['units']:+.2f}" for leg_kind, b in block["by_kind"].items() if b["bets"])
            lines.append(f"    >= {threshold:<4} legs={block['bets']:<4} events={block['events']:<4} {block['record']:<10} units={block['units']:+.2f} roi={_fmt(block['roi'], '+.3f')}  [{by_kind}]")
    lines.append("Recommendation: " + "; ".join(
        f"{knob} {block['current']:g} -> {'keep' if block['keep'] else block['recommended']}"
        for knob, block in result["recommendation"].items()
    ))
    return lines


def format_ledger_report(result: dict[str, Any]) -> list[str]:
    lines = [
        f"Ledger refit: {result['graded_games']} graded god_rules row(s)"
        + (f"; skipped {result['skipped']}" if result["skipped"] else "")
        + ".",
    ]
    if result["informational"]:
        lines.append(
            f"  Informational only: the refit needs about {result['minimum_games']} graded games "
            "(the roadmap's bar for WP10)."
        )
    for row in result["rows"]:
        lines.append(f"  wk{row['week']} {row['teams']} final={row['final']} closing={'yes' if row['closing_available'] else 'no'} ({row['opinion_id']})")
    selection = result["selection"]
    if selection is None:
        lines.append("  No graded rows; nothing to refit.")
        return lines
    lines.append("Selection on the ledger:")
    for step in selection["steps"]:
        lines.append(f"  {step['knob']}: chosen {step['chosen']} (registry {step['registry']})")
    comparison = result["comparison"]
    lines.append(f"Against the {comparison['against']} values:")
    for field, block in comparison["knobs"].items():
        lines.append(f"  {field}: ledger {block['ledger']} vs {block['reference']} ({'agree' if block['agree'] else 'differ'})")
    lines.extend(_summary_lines(f"Ledger, chosen ({selection['key']})", result["results"][selection["key"]]))
    lines.extend(_summary_lines(f"Ledger, registry ({selection['registry_key']})", result["results"][selection["registry_key"]]))
    lines.append("Veto sweep (legs the veto removed under each knob and how they would have graded):")
    for kind, blocks in result["veto"].items():
        lines.append(f"  {kind}:")
        for label, block in blocks.items():
            legs = ", ".join(block["legs"]) if block["legs"] else "none"
            lines.append(f"    {label:<4} vetoed={block['bets']} {block['record']} units={block['units']:+.2f}: {legs}")
    return lines


def json_text(value: Any) -> str:
    """Deterministic JSON for the ``--json`` dumps."""
    return json.dumps(value, indent=2, sort_keys=True, ensure_ascii=False) + "\n"


def load_rows(path: Path = LINES_CSV) -> list[dict[str, str]]:
    return read_lines_csv(path)
