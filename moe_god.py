"""God Expert aggregator for the NFL mixture of experts.

Two registered experts share this module and one input:

- ``god_rules`` (mode ``aggregator``) is a deterministic gate. Every approved
  expert opinion for the game becomes one voice; voices are pooled with the
  registry weights (discounted for evidence they share with voices ranked
  before them, and only into the markets their expert informs), the pool is
  shrunk toward the de-vigged BetOnline market, and the shared policy turns
  the blended probabilities into side and total legs. No model is involved
  anywhere.
- ``god_judge`` (mode ``aggregator_judge``) receives the same arithmetic plus a
  masked, seeded-shuffled view of the voices and returns only probabilities.
  The identical policy turns those probabilities into legs.

Everything with a right answer is computed here. A model only estimates.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import math
import random
import re
from pathlib import Path
from typing import Any, Iterable, Iterator

import yaml

from moe_ak import (
    _grade_side,
    _grade_total,
    _market_from_packed,
    _market_from_snapshot,
    _matching_history_game,
    _parse_time,
)

ROOT = Path(__file__).resolve().parent
EXPERTS_PATH = ROOT / "moe" / "experts.yaml"
# The empirical margin table (scripts/build_nfl_margins.py); consulted only
# when aggregator_policy.margin_model is "empirical".
MARGINS_TABLE_PATH = ROOT / "moe" / "priors" / "nfl_margins_v1.json"
MARGIN_MODELS = ("normal", "empirical")

AGGREGATOR_PROFILE = "aggregator"
JUDGE_REQUEST_PROFILE = "aggregator_judge_request"
RULES_MODE = "aggregator"
JUDGE_MODE = "aggregator_judge"
AGGREGATOR_MODES = {RULES_MODE, JUDGE_MODE}
DETERMINISTIC_MODEL = "deterministic"
DETERMINISTIC_BACKEND = "deterministic"
RULES_EXPERT_ID = "god_rules"
JUDGE_EXPERT_ID = "god_judge"
# Ledger-only row for the bake-off: the mean of the two arms. Never an expert.
MEAN_OF_ARMS_ID = "mean_of_arms"

JUDGE_LABELS = tuple(f"Voice {letter}" for letter in "ABCDEFGHIJKL")
MARKET_LABEL = "market"
POOL_LABEL = "pool"
SCOREBOARD_LABEL = "scoreboard"
EXTRA_REASON_LABELS = {MARKET_LABEL, POOL_LABEL, SCOREBOARD_LABEL}

GRADES_TAB = "moe_grades"
GRADE_HEADERS = [
    "graded_at_utc",
    "opinion_id",
    "expert_id",
    "event_id",
    "season",
    "week",
    "away_team",
    "home_team",
    "final",
    "home_won",
    "home_win_probability",
    "brier",
    "ats_at_close",
    "ou_at_close",
    "side_selection",
    "side_line",
    "side_result",
    "side_clv_points",
    "total_selection",
    "total_line",
    "total_result",
    "total_clv_points",
    "closing_available",
]

DEFAULT_POLICY: dict[str, Any] = {
    "version": 1,
    "sigma_margin": 13.5,
    "sigma_total": 13.5,
    "shrink_lambda": 0.5,
    "edge_threshold": 0.03,
    "star_edges": [0.03, 0.05, 0.08, 0.12, 0.16],
    "kelly_fraction": 0.25,
    "max_stake_fraction": 0.05,
    "veto_adverse_spread_points": 0.5,
    "veto_adverse_total_points": 1.0,
    "veto_adverse_price_cents": 10,
    "min_ev_per_unit": 0.02,
    "margin_model": "normal",
    "voice_rule": "default_model",
    "voice_fallback": "latest_any_model",
    "hedge_eta": 2.0,
    "weights_min_resolved": 3,
    "weight_floor": 0.5,
    "weight_cap": 2.0,
    "factor_limit": 5,
    "factor_chars": 200,
    "reason_limit": 6,
    "reason_chars": 280,
}

# What each voice sees, phrased without naming any person. The judge reads
# these; the human-facing rendering shows the expert names instead.
VOICE_LENSES: dict[str, str] = {
    "schedule": (
        "Sees three seasons of schedule cohorts for both teams: month, "
        "weekday, week number, venue splits, head-to-head. No lines, no news."
    ),
    "divisional": (
        "Sees division and conference matchup history and matchup tags. "
        "No lines, no news."
    ),
    "win_total": (
        "Sees BetOnline season win totals and several human season "
        "projections for both teams. No game lines."
    ),
    "ak": (
        "Calibrates one human forecaster's exact score projection against "
        "the submission-time market using that forecaster's graded history "
        "and a capped cross-sport prior. May pass either leg."
    ),
    "cee": (
        "Interprets one human forecaster's full-game moneyline rationale "
        "alongside that forecaster's season-win ordering and resolved NFL "
        "pick history. Side only."
    ),
    "celebrity": (
        "Compares the celebrities who actually picked this game, including "
        "individual records, each person's results when a pair disagrees, "
        "and exact identity-specific home/away or Over/Under permutations. "
        "Props and other markets are tracked but do not directly inform the "
        "game side or total pools."
    ),
    "rating_elo": (
        "Sees one Elo rating per team built from every regular-season final "
        "since 1999 with home advantage and margin of victory, updated "
        "through this season's finals before kickoff. Its total is the "
        "league scoring rate. No lines, no news."
    ),
}

MARKET_FIELDS = (
    "away_spread",
    "away_spread_price",
    "away_moneyline",
    "home_spread",
    "home_spread_price",
    "home_moneyline",
    "total",
    "over_price",
    "under_price",
)

# Price fields whose movement since open is reported in cents. Their absence
# from a persisted movement block marks an input built before they existed.
MOVEMENT_PRICE_FIELDS = (
    "home_spread_price",
    "away_spread_price",
    "over_price",
    "under_price",
)

# Policy knobs behind the market-move veto and the expected-value floor.
VETO_FLOOR_KEYS = (
    "veto_adverse_spread_points",
    "veto_adverse_total_points",
    "veto_adverse_price_cents",
    "min_ev_per_unit",
)

# ``pass_reason`` values; the first two open the matching policy note.
ADVERSE_MOVE_REASON = "adverse move"
EV_FLOOR_REASON = "ev floor"
NO_EXPECTATION_REASON = "no positive expectation at the posted price"

# The judge runner's ensemble (roadmap WP9): one judge row per trigger whose
# numbers are the means of the valid sampled responses and whose reasons come
# from one sample; the sampled rows persist as generation_status "sample".
ENSEMBLE_RULE = (
    "mean of the valid samples; reasons from the sample closest to the mean"
)
ENSEMBLE_KEYS = ("size", "valid", "samples", "estimates", "reasons_from", "rule")
# Coherence notes, shared by the rules arm and the ensemble.
FENCE_NOTE = "Blend sat exactly on the fence; the market favorite breaks the tie."
SIGN_NOTE = (
    "Pooled probability and pooled margin disagreed in sign; the "
    "margin was clamped to follow the probability."
)

# The markets a voice can inform (registry ``markets``); the side pool
# averages side-informed voices, the total pool total-informed ones.
MARKETS = ("side", "total")

# The record style of moe._complete_unique_record_paths: W-L or W-L-T. Shared
# by the evidence extractor (what two voices cite in common) and the judge's
# reason guard (what a reason may cite at all). A digit-dot before or a
# dot-digit after is a decimal fragment, not a record: "24.0-20.5" (implied
# totals), "13.5-14" and "0.5-1.0" once read as 0-20, 5-14 and 5-1 and had the
# guard reject a live judge response (2026-09-07).
_RECORD_PATTERN = re.compile(r"(?<!\d\.)\b(\d+)-(\d+)(?:-(\d+))?\b(?!\.\d)")
_GAME_COUNT_PATTERN = re.compile(r"\b(\d+)[\s-]games?\b")
_COUNT_KEY_WORDS = ("games", "count", "sample", "resolved")


# --------------------------------------------------------------------------
# Registry and policy


def load_registry() -> dict[str, Any]:
    config = yaml.safe_load(EXPERTS_PATH.read_text(encoding="utf-8"))
    if not isinstance(config, dict) or not isinstance(
        config.get("experts"), dict
    ):
        raise ValueError("moe/experts.yaml must define an experts map")
    for expert_id, expert in config["experts"].items():
        if not isinstance(expert, dict):
            continue
        try:
            review_policy(expert)
            if str(expert.get("mode") or "") not in AGGREGATOR_MODES:
                voice_markets(expert)
        except ValueError as exc:
            raise ValueError(
                f"moe/experts.yaml expert {expert_id}: {exc}"
            ) from exc
    return config


# How a persisted row reaches ``approved``. ``validation`` is the default:
# every structurally valid generated opinion is approved at generation and
# hash-bound exactly like a human approval. ``human`` remains available as an
# explicit registry opt-out. Invalid and sample rows are never approved.
# Decided 2026-09-08, replacing the manual gate so committee and God Expert
# updates do not wait on a reviewer.
REVIEW_POLICIES = ("human", "validation")
VALIDATION_REVIEWER = "validation"
VALIDATION_REVIEW_NOTE = (
    "approved automatically after input-bound schema validation"
)


def review_policy(config: dict[str, Any]) -> str:
    """The registry's ``review`` policy; validation is the default."""
    raw = config.get("review")
    if raw is None:
        return "validation"
    policy = str(raw)
    if policy not in REVIEW_POLICIES:
        raise ValueError(
            f"review must be one of {list(REVIEW_POLICIES)}: {raw!r}"
        )
    return policy


def voice_markets(config: dict[str, Any]) -> list[str]:
    """The markets a registered expert informs, in canonical order.

    ``markets`` in the registry is a non-empty subset of ``side`` and
    ``total``; an entry without it informs both. A voice enters only the
    pools of the markets its expert informs.
    """
    raw = config.get("markets")
    if raw is None:
        return list(MARKETS)
    if (
        not isinstance(raw, list)
        or not raw
        or len(set(raw)) != len(raw)
        or any(item not in MARKETS for item in raw)
    ):
        raise ValueError(
            f"markets must be a non-empty subset of {list(MARKETS)}: {raw!r}"
        )
    return [market for market in MARKETS if market in raw]


def aggregator_policy(config: dict[str, Any] | None = None) -> dict[str, Any]:
    config = config if config is not None else load_registry()
    raw = config.get("aggregator_policy") or {}
    if not isinstance(raw, dict):
        raise ValueError("aggregator_policy must be a map")
    unknown = set(raw) - set(DEFAULT_POLICY)
    if unknown:
        raise ValueError(f"Unknown aggregator_policy keys: {sorted(unknown)}")
    policy = {**DEFAULT_POLICY, **raw}
    for key in (
        "sigma_margin",
        "sigma_total",
        "shrink_lambda",
        "edge_threshold",
        "kelly_fraction",
        "max_stake_fraction",
        "hedge_eta",
        "weight_floor",
        "weight_cap",
        *VETO_FLOOR_KEYS,
    ):
        value = policy[key]
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ValueError(f"aggregator_policy.{key} must be numeric")
        policy[key] = float(value)
    for key in VETO_FLOOR_KEYS:
        if policy[key] < 0:
            raise ValueError(f"aggregator_policy.{key} must be non-negative")
    if policy["min_ev_per_unit"] > 1.0:
        raise ValueError("aggregator_policy.min_ev_per_unit must be within 0..1")
    if not 0.0 <= policy["shrink_lambda"] <= 1.0:
        raise ValueError("aggregator_policy.shrink_lambda must be within 0..1")
    if policy["sigma_margin"] <= 0 or policy["sigma_total"] <= 0:
        raise ValueError("aggregator_policy sigmas must be positive")
    if not 0.0 < policy["kelly_fraction"] <= 1.0:
        raise ValueError("aggregator_policy.kelly_fraction must be within 0..1")
    if policy["weight_floor"] <= 0 or policy["weight_cap"] < policy["weight_floor"]:
        raise ValueError("aggregator_policy weight bounds are inconsistent")
    stars = policy["star_edges"]
    if (
        not isinstance(stars, list)
        or len(stars) != 5
        or any(
            isinstance(value, bool) or not isinstance(value, (int, float))
            for value in stars
        )
        or any(stars[i] >= stars[i + 1] for i in range(4))
        or float(stars[0]) != policy["edge_threshold"]
    ):
        raise ValueError(
            "aggregator_policy.star_edges must be five increasing edges "
            "starting at edge_threshold"
        )
    policy["star_edges"] = [float(value) for value in stars]
    for key in (
        "weights_min_resolved",
        "factor_limit",
        "factor_chars",
        "reason_limit",
        "reason_chars",
    ):
        value = policy[key]
        if isinstance(value, bool) or not isinstance(value, int) or value < 1:
            raise ValueError(
                f"aggregator_policy.{key} must be a positive integer"
            )
    if policy["margin_model"] not in MARGIN_MODELS:
        raise ValueError(
            "aggregator_policy.margin_model must be normal or empirical"
        )
    if policy["voice_rule"] != "default_model":
        raise ValueError("aggregator_policy.voice_rule must be default_model")
    if policy["voice_fallback"] not in {"latest_any_model", "skip"}:
        raise ValueError(
            "aggregator_policy.voice_fallback must be latest_any_model or skip"
        )
    return policy


# --------------------------------------------------------------------------
# Arithmetic


def canonical_json(value: Any) -> str:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )


def sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def american_to_implied(price: Any) -> float:
    """Vig-inclusive implied probability of an American price."""
    value = float(price)
    if not math.isfinite(value) or value == 0 or -100 < value < 100:
        raise ValueError(f"Invalid American price: {price}")
    if value > 0:
        return 100.0 / (value + 100.0)
    return -value / (-value + 100.0)


def american_to_decimal(price: Any) -> float:
    value = float(price)
    if not math.isfinite(value) or value == 0 or -100 < value < 100:
        raise ValueError(f"Invalid American price: {price}")
    return 1.0 + value / 100.0 if value > 0 else 1.0 + 100.0 / -value


def fair_pair(price_a: Any, price_b: Any) -> tuple[float, float, float]:
    """De-vig a two-way market. Returns (fair_a, fair_b, hold)."""
    implied_a = american_to_implied(price_a)
    implied_b = american_to_implied(price_b)
    book = implied_a + implied_b
    return implied_a / book, implied_b / book, book - 1.0


def normal_cdf(z: float) -> float:
    return 0.5 * (1.0 + math.erf(z / math.sqrt(2.0)))


def cover_probability(
    expected_home_margin: float,
    home_spread: float,
    sigma: float,
    *,
    table: dict[str, Any] | None = None,
) -> float:
    """P(home covers): the home side wins when actual margin + spread > 0.

    With ``table`` (a parsed empirical table, see :func:`load_margin_table`)
    the actual margin is the expectation plus a residual drawn from the bin
    of ``home_spread``: P(cover) = P(r > t) + P(r = t) / 2 with
    t = -(expected_home_margin + home_spread). Off the table's support, and
    without a table, the residual is normal with ``sigma``.
    """
    margin, spread = float(expected_home_margin), float(home_spread)
    if table is not None:
        value = empirical_survival(table["spread"], spread, -(margin + spread))
        if value is not None:
            return value
    return normal_cdf((margin + spread) / sigma)


def over_probability(
    projected_total: float,
    total_line: float,
    sigma: float,
    *,
    table: dict[str, Any] | None = None,
) -> float:
    """P(over): the total residual, from the bin of ``total_line``, exceeds
    ``total_line - projected_total``; otherwise the normal model."""
    projected, line = float(projected_total), float(total_line)
    if table is not None:
        value = empirical_survival(table["total"], line, line - projected)
        if value is not None:
            return value
    return normal_cdf((projected - line) / sigma)


# --------------------------------------------------------------------------
# Empirical margin table (aggregator_policy.margin_model = "empirical")

_MARGIN_TABLE_CACHE: dict[str, dict[str, Any]] = {}


def parse_margin_table(
    raw: dict[str, Any], *, sha256: str = "", path: str = ""
) -> dict[str, Any]:
    """Turn the committed table document into lookup-ready bins.

    Per market only bins with at least ``min_games`` games survive. Each
    keeps a regular half-point lattice from one step below its smallest
    residual to one step above its largest, and the survival value at every
    lattice point, ``H(u) = (count(r > u) + count(r = u) / 2) / n``: 1 at
    the bottom, 0 at the top. :func:`empirical_survival` interpolates
    linearly between lattice points.
    """
    if not isinstance(raw, dict) or int(raw.get("schema_version") or 0) != 1:
        raise ValueError("Unsupported margin table schema")
    min_games = int(raw["min_games"])
    step = float(raw.get("lattice_step") or 0.5)
    if step <= 0:
        raise ValueError("margin table lattice_step must be positive")

    def market(key: str) -> dict[int, dict[str, Any]]:
        bins: dict[int, dict[str, Any]] = {}
        for bin_label, entry in raw[key]["bins"].items():
            n = int(entry["n"])
            if n < min_games:
                continue
            pairs = sorted(
                (float(value), int(count)) for value, count in entry["residuals"]
            )
            if not pairs or sum(count for _value, count in pairs) != n:
                raise ValueError(
                    f"margin table bin {key}/{bin_label} is inconsistent"
                )
            start = pairs[0][0] - step
            slots = int(round((pairs[-1][0] + step - start) / step)) + 1
            counts = [0] * slots
            for value, count in pairs:
                index = (value - start) / step
                if abs(index - round(index)) > 1e-9:
                    raise ValueError(
                        f"margin table residual {value} is off the lattice"
                    )
                counts[int(round(index))] += count
            survival = []
            at_or_above = n
            for count in counts:
                survival.append((at_or_above - count + count / 2) / n)
                at_or_above -= count
            bins[int(bin_label)] = {
                "n": n,
                "lattice_start": start,
                "step": step,
                "survival": survival,
            }
        return bins

    return {
        "path": path,
        "sha256": sha256,
        "schema_version": 1,
        "version": str(raw.get("version") or ""),
        "seasons": list(raw.get("seasons") or []),
        "games": int(raw.get("games") or 0),
        "min_games": min_games,
        "bin_width": raw.get("bin_width"),
        "spread": market("spread"),
        "total": market("total"),
    }


def _repo_relative(path: str | Path) -> str:
    """``moe/priors/nfl_margins_v1.json`` for a file under the repo, else as given."""
    resolved = Path(path).resolve()
    if ROOT in resolved.parents:
        return resolved.relative_to(ROOT).as_posix()
    return str(path)


def load_margin_table(path: str | Path = MARGINS_TABLE_PATH) -> dict[str, Any]:
    """The committed table, parsed once per path; its sha256 rides in inputs."""
    key = str(path)
    table = _MARGIN_TABLE_CACHE.get(key)
    if table is None:
        raw_bytes = Path(path).read_bytes()
        table = parse_margin_table(
            json.loads(raw_bytes.decode("utf-8")),
            sha256=hashlib.sha256(raw_bytes).hexdigest(),
            path=_repo_relative(path),
        )
        _MARGIN_TABLE_CACHE[key] = table
    return table


def empirical_survival(
    bins: dict[int, dict[str, Any]], line: float, t: float
) -> float | None:
    """P(r > t) + P(r = t) / 2 from the bin of ``line``; None off support.

    Exact at lattice points, linear in between, 1 below the bin's lattice
    and 0 above it.
    """
    entry = bins.get(int(math.floor(float(line))))
    if entry is None:
        return None
    survival = entry["survival"]
    position = round((float(t) - entry["lattice_start"]) / entry["step"], 9)
    if position <= 0:
        return 1.0
    if position >= len(survival) - 1:
        return 0.0
    index = int(math.floor(position))
    fraction = position - index
    return survival[index] + fraction * (survival[index + 1] - survival[index])


# For backtests only (moe_backtest): the parsed table that margin_table_for
# answers with instead of the committed file while the context is active.
_MARGIN_TABLE_OVERRIDE: dict[str, Any] | None = None


@contextlib.contextmanager
def margin_table_override(table: dict[str, Any]) -> Iterator[dict[str, Any]]:
    """Evaluate ``empirical`` policies against ``table`` (a parsed table).

    For backtests only: a table built from the seasons before the one under
    test replaces the committed file inside the block, so a replay never
    reads a distribution that contains the games it is scoring. Live rows
    are untouched: :func:`check_margin_table` still compares a persisted
    input against the committed file, never the override.
    """
    global _MARGIN_TABLE_OVERRIDE
    previous = _MARGIN_TABLE_OVERRIDE
    _MARGIN_TABLE_OVERRIDE = table
    try:
        yield table
    finally:
        _MARGIN_TABLE_OVERRIDE = previous


def margin_table_for(policy: dict[str, Any]) -> dict[str, Any] | None:
    """The table a policy asks for: loaded when ``empirical``, else None.

    A policy persisted before the switch existed reads as ``normal``. Inside
    :func:`margin_table_override` the override answers instead of the file.
    """
    if str(policy.get("margin_model") or "normal") == "empirical":
        if _MARGIN_TABLE_OVERRIDE is not None:
            return _MARGIN_TABLE_OVERRIDE
        return load_margin_table()
    return None


def margin_table_descriptor(
    table: dict[str, Any] | None,
) -> dict[str, Any] | None:
    """What an input records about the table it was built with."""
    if table is None:
        return None
    return {
        key: table[key]
        for key in (
            "path",
            "sha256",
            "schema_version",
            "version",
            "seasons",
            "games",
            "min_games",
            "bin_width",
        )
    }


def check_margin_table(input_payload: dict[str, Any]) -> None:
    """Refuse an input built against a table other than the committed one."""
    recorded = input_payload.get("margin_table")
    if not recorded:
        return
    current = load_margin_table()
    if str(recorded.get("sha256") or "") != current["sha256"]:
        raise ValueError(
            "The input was built with margin table "
            f"{str(recorded.get('sha256') or '')[:12]}, not the committed "
            f"{current['sha256'][:12]}"
        )


def _round(value: float | None, digits: int = 4) -> float | None:
    return None if value is None else round(float(value), digits)


def _clip(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


# --------------------------------------------------------------------------
# Market


def price_cents(price: Any) -> float:
    """An American price on the bettor's cents scale.

    ``-110`` is -10, ``+110`` is +10, and both ``-100`` and ``+100`` are 0, so
    the difference of two values is the move in cents: ``-110`` to ``+100`` is
    +10, as is ``-105`` to ``+105``.
    """
    value = float(price)
    if not math.isfinite(value) or value == 0 or -100 < value < 100:
        raise ValueError(f"Invalid American price: {price}")
    return value - 100.0 if value > 0 else value + 100.0


def movement_since_open(
    opening: dict[str, Any], latest: dict[str, Any]
) -> dict[str, float | None]:
    """Latest minus opening: lines in points, prices in cents, None when unset.

    Accepts the decoded market dicts as well as a persisted block's
    ``market["opening"]`` and ``market["latest"]``, so ``apply_policy`` can
    recompute the price deltas for inputs stored before they were recorded.
    """

    def delta(field: str) -> float | None:
        if opening.get(field) is None or latest.get(field) is None:
            return None
        return round(float(latest[field]) - float(opening[field]), 2)

    def cents_delta(field: str) -> float | None:
        if opening.get(field) is None or latest.get(field) is None:
            return None
        return round(
            price_cents(latest[field]) - price_cents(opening[field]), 2
        )

    fair_home_ml = None
    if all(
        block.get(field) is not None
        for block in (opening, latest)
        for field in ("home_moneyline", "away_moneyline")
    ):
        fair_home_ml = _round(
            fair_pair(latest["home_moneyline"], latest["away_moneyline"])[0]
            - fair_pair(opening["home_moneyline"], opening["away_moneyline"])[0]
        )
    return {
        "home_spread": delta("home_spread"),
        "total": delta("total"),
        "home_moneyline": delta("home_moneyline"),
        "fair_home_ml": fair_home_ml,
        "away_spread": delta("away_spread"),
        **{field: cents_delta(field) for field in MOVEMENT_PRICE_FIELDS},
    }


def build_market_block(game: dict[str, Any]) -> dict[str, Any]:
    """The market block of an ``nfl_games`` row (packed BetOnline columns)."""
    return market_block_from_lines(
        _market_from_packed(game, prefix="opening"),
        _market_from_packed(game, prefix="latest"),
        bookmaker=str(game.get("bookmaker") or ""),
        opening_captured_at=str(game.get("opening_captured_at") or ""),
        latest_captured_at=str(game.get("latest_captured_at") or ""),
    )


def market_block_from_lines(
    opening: dict[str, Any] | None,
    latest: dict[str, Any],
    *,
    bookmaker: str = "",
    opening_captured_at: str = "",
    latest_captured_at: str = "",
) -> dict[str, Any]:
    """The market block from decoded full-game markets (``MARKET_FIELDS``).

    ``latest`` must carry every field; ``opening`` may be missing fields, or
    be None altogether, in which case every movement delta is None and
    nothing is ever vetoed (a missing opening never vetoes). The backtest
    builds its markets from historical closes through this function, so a
    replayed market is shaped exactly like a live one.
    """
    opening = dict(opening or {})
    missing = [field for field in MARKET_FIELDS if latest.get(field) is None]
    if missing:
        raise ValueError(f"Latest full-game market is missing {missing}")
    if float(latest["away_spread"]) != -float(latest["home_spread"]):
        raise ValueError("Latest spreads are not mirror images")
    fair_home_ml, fair_away_ml, hold_ml = fair_pair(
        latest["home_moneyline"], latest["away_moneyline"]
    )
    fair_home_cover, fair_away_cover, hold_spread = fair_pair(
        latest["home_spread_price"], latest["away_spread_price"]
    )
    fair_over, fair_under, hold_total = fair_pair(
        latest["over_price"], latest["under_price"]
    )
    home_spread = float(latest["home_spread"])
    total_line = float(latest["total"])
    return {
        "bookmaker": bookmaker,
        "opening": {
            **{field: opening.get(field) for field in MARKET_FIELDS},
            "captured_at": opening_captured_at,
        },
        "latest": {
            **{field: latest[field] for field in MARKET_FIELDS},
            "captured_at": latest_captured_at,
        },
        "fair": {
            "home_ml": _round(fair_home_ml),
            "away_ml": _round(fair_away_ml),
            "home_cover": _round(fair_home_cover),
            "away_cover": _round(fair_away_cover),
            "over": _round(fair_over),
            "under": _round(fair_under),
            "hold_ml": _round(hold_ml),
            "hold_spread": _round(hold_spread),
            "hold_total": _round(hold_total),
        },
        "market_expectation": {
            "home_margin": -home_spread,
            "total": total_line,
        },
        "implied_totals": {
            "away": round((total_line + home_spread) / 2, 2),
            "home": round((total_line - home_spread) / 2, 2),
        },
        "movement_since_open": movement_since_open(opening, latest),
    }


def closing_market(
    event_id: str,
    commence_time_utc: str,
    snapshots: Iterable[dict[str, Any]],
) -> dict[str, Any] | None:
    """Latest full-game snapshot strictly before kickoff for one event.

    Unlike ``moe_ak._closing_market`` this does not filter on bookmaker,
    because opinion rows carry no bookmaker column; every snapshot in this
    system comes from the one configured book.
    """
    kickoff = _parse_time(commence_time_utc)
    eligible = [
        row
        for row in snapshots
        if str(row.get("event_id")) == str(event_id)
        and _parse_time(row["captured_at"]) < kickoff
    ]
    if not eligible:
        return None
    return _market_from_snapshot(
        max(eligible, key=lambda row: _parse_time(row["captured_at"]))
    )


# --------------------------------------------------------------------------
# Voices


def _text_items(value: Any) -> list[str]:
    """Flatten a persisted factor column into plain claims."""
    if value in (None, ""):
        return []
    parsed = value
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError:
            return [value.strip()] if value.strip() else []
    if isinstance(parsed, dict):
        parsed = parsed.get("items", [])
    if not isinstance(parsed, list):
        return []
    items: list[str] = []
    for item in parsed:
        if isinstance(item, str):
            text = item.strip()
        elif isinstance(item, dict):
            text = str(item.get("claim") or item.get("text") or "").strip()
        else:
            text = ""
        if text:
            items.append(text)
    return items


def _capped(items: list[str], limit: int, chars: int) -> dict[str, Any]:
    kept = [
        item if len(item) <= chars else item[: chars - 1] + "…"
        for item in items[:limit]
    ]
    return {
        "items": kept,
        "truncated": len(items) > limit
        or any(len(item) > chars for item in items[:limit]),
        "total": len(items),
    }


def extract_evidence(items: Iterable[str]) -> dict[str, Any]:
    """Record tuples and cohort labels cited in a voice's factor text.

    A heuristic, pinned on the Week 1 fixture texts by the tests. Every
    ``W-L`` or ``W-L-T`` token (``_RECORD_PATTERN``, the reason guard's
    record shape) is a record. Its cohort size is an explicit "N games" count
    in the same item: each count is claimed by the record nearest to it (the
    earlier record on a tie), and a record takes the nearest count it
    claimed, else W+L+T, the cohort a bare record implies. The cohort label is
    the last four non-numeric words before the record and after the previous
    one; it is informational and never enters the overlap.

    Returns ``{"tuples": [[W, L, T, games], ...], "cohorts": [label, ...]}``
    aligned by index, deduplicated on the tuple in order of first
    appearance, so the persisted form is deterministic.
    """
    tuples: list[list[int]] = []
    cohorts: list[str] = []
    seen: set[tuple[int, int, int, int]] = set()

    def gap(a: Any, b: Any) -> int:
        return max(0, b.start() - a.end(), a.start() - b.end())

    for item in items:
        text = str(item)
        records = list(_RECORD_PATTERN.finditer(text))
        if not records:
            continue
        claimed: dict[int, list[Any]] = {}
        for count in _GAME_COUNT_PATTERN.finditer(text):
            nearest = min(
                range(len(records)),
                key=lambda index: (gap(records[index], count), index),
            )
            claimed.setdefault(nearest, []).append(count)
        previous_end = 0
        for index, record in enumerate(records):
            wins, losses, ties = (
                int(group) if group else 0 for group in record.groups()
            )
            mine = claimed.get(index)
            if mine:
                games = int(
                    min(
                        mine, key=lambda count: (gap(record, count), count.start())
                    ).group(1)
                )
            else:
                games = wins + losses + ties
            words = [
                word
                for word in re.split(
                    r"[^a-z0-9]+", text[previous_end : record.start()].lower()
                )
                if word and not word.isdigit()
            ]
            previous_end = record.end()
            key = (wins, losses, ties, games)
            if key in seen:
                continue
            seen.add(key)
            tuples.append(list(key))
            cohorts.append(" ".join(words[-4:]))
    return {"tuples": tuples, "cohorts": cohorts}


def voice_evidence(voice: dict[str, Any]) -> dict[str, Any]:
    """A voice's evidence block.

    Voices persisted before the block existed are re-extracted from their
    capped factor text, so a replayed input still gets an overlap matrix.
    """
    evidence = voice.get("evidence")
    if isinstance(evidence, dict) and isinstance(evidence.get("tuples"), list):
        return evidence
    return extract_evidence(
        list((voice.get("supporting_factors") or {}).get("items") or [])
        + list((voice.get("counterarguments") or {}).get("items") or [])
    )


def _voice_markets(voice: dict[str, Any]) -> list[str]:
    """The markets a voice informs; a voice persisted without the field
    informs both, which is what every registry entry did before it existed."""
    markets = voice.get("markets")
    if markets is None:
        return list(MARKETS)
    return [market for market in MARKETS if market in markets]


def _jaccard(a: set[Any], b: set[Any]) -> float:
    if not a and not b:
        return 0.0
    return round(len(a & b) / len(a | b), 4)


def evidence_overlap(
    voices: list[dict[str, Any]],
) -> dict[str, dict[str, float]]:
    """Pairwise Jaccard overlap of the record tuples the voices cite.

    Keyed by voice id both ways with the diagonal omitted; two voices that
    cite no records at all overlap 0.
    """
    sets = {
        voice["voice_id"]: {
            tuple(int(value) for value in item)
            for item in voice_evidence(voice)["tuples"]
        }
        for voice in voices
    }
    return {
        a: {b: _jaccard(sets[a], sets[b]) for b in sets if b != a} for a in sets
    }


def overlap_adjusted_weights(
    hedge: dict[str, float], overlap: dict[str, dict[str, float]]
) -> dict[str, float]:
    """Hedge weights discounted for evidence shared with higher-ranked voices.

    Voices rank by id (alphabetical); a voice's weight is divided by one plus
    the sum of its overlap with every voice ranked before it, so the first
    voice to cite a table keeps its full weight and a later voice reciting
    the same table counts for half of its own.
    """
    weights: dict[str, float] = {}
    order = sorted(hedge)
    for index, voice_id in enumerate(order):
        divisor = 1.0 + sum(
            float((overlap.get(voice_id) or {}).get(other, 0.0))
            for other in order[:index]
        )
        weights[voice_id] = round(float(hedge[voice_id]) / divisor, 4)
    return weights


def _leg_from_json(value: Any) -> dict[str, Any] | None:
    if value in (None, ""):
        return None
    parsed = json.loads(value) if isinstance(value, str) else value
    if not isinstance(parsed, dict) or "selection" not in parsed:
        return None
    line = parsed.get("line")
    return {
        "selection": str(parsed.get("selection")),
        "line": None if line in (None, "") else float(line),
        "confidence_stars": int(parsed.get("confidence_stars") or 1),
    }


def _number(value: Any, field: str) -> float:
    if value in (None, ""):
        raise ValueError(f"Opinion row is missing {field}")
    number = float(value)
    if not math.isfinite(number):
        raise ValueError(f"Opinion row has a non-finite {field}")
    return number


def _row_order(row: dict[str, Any]) -> tuple[str, str]:
    return (
        str(row.get("generated_at_utc") or ""),
        str(row.get("opinion_id") or ""),
    )


def _latest_row(rows: list[dict[str, Any]]) -> dict[str, Any]:
    return max(rows, key=_row_order)


def select_voice_rows(
    approved_rows: Iterable[dict[str, Any]],
    *,
    event_id: str,
    registry: dict[str, Any],
    policy: dict[str, Any],
) -> list[tuple[str, dict[str, Any], dict[str, Any], str]]:
    """One approved row per non-aggregator expert, by the pinned rule.

    Returns ``(expert_id, expert_config, row, selection_rule)`` tuples sorted
    by expert id. Rows must already be approval-verified by the caller.
    """
    experts = registry["experts"]
    rows = [
        row
        for row in approved_rows
        if str(row.get("event_id")) == str(event_id)
        and str(row.get("review_status") or "") == "approved"
        and str(row.get("generation_status") or "valid") == "valid"
    ]
    selected = []
    for expert_id in sorted(experts):
        config = experts[expert_id]
        if not isinstance(config, dict) or not config.get("enabled"):
            continue
        if str(config.get("mode") or "") in AGGREGATOR_MODES:
            continue
        candidates = [
            row for row in rows if str(row.get("expert_id")) == expert_id
        ]
        if not candidates:
            continue
        default_model = str(config.get("default_model") or "")
        on_default = [
            row for row in candidates if str(row.get("model")) == default_model
        ]
        if on_default:
            selected.append(
                (expert_id, config, _latest_row(on_default), "default_model")
            )
        elif policy["voice_fallback"] == "latest_any_model":
            selected.append(
                (expert_id, config, _latest_row(candidates), "latest_any_model")
            )
    return selected


def voice_from_row(
    expert_id: str,
    config: dict[str, Any],
    row: dict[str, Any],
    *,
    selection_rule: str,
    market: dict[str, Any],
    policy: dict[str, Any],
    track_record: dict[str, Any],
) -> dict[str, Any]:
    probability = _number(
        row.get("home_win_probability"), "home_win_probability"
    )
    margin = _number(row.get("expected_home_margin"), "expected_home_margin")
    away_score = int(
        _number(row.get("predicted_away_score"), "predicted_away_score")
    )
    home_score = int(
        _number(row.get("predicted_home_score"), "predicted_home_score")
    )
    stars = int(_number(row.get("confidence_stars"), "confidence_stars"))
    latest = market["latest"]
    projected_total = away_score + home_score
    limit, chars = policy["factor_limit"], policy["factor_chars"]
    table = margin_table_for(policy)
    legs = {
        "side": _leg_from_json(row.get("side_pick_json")),
        "total": _leg_from_json(row.get("total_pick_json")),
    }
    markets = voice_markets(config)
    if expert_id == "celebrity":
        markets = [
            market_name
            for market_name in markets
            if (
                legs[market_name] is not None
                and legs[market_name].get("selection") != "PASS"
            )
        ]
    return {
        "voice_id": expert_id,
        "expert_id": expert_id,
        "expert_name": str(
            row.get("expert_name") or config.get("name") or expert_id
        ),
        "lens": VOICE_LENSES.get(
            expert_id, str(config.get("input_profile") or "")
        ),
        "expert_version": str(row.get("expert_version") or ""),
        "prompt_version": str(row.get("prompt_version") or ""),
        "model": str(row.get("model") or ""),
        "generation_backend": str(row.get("generation_backend") or ""),
        "generation_effort": str(row.get("generation_effort") or ""),
        "opinion_id": str(row.get("opinion_id") or ""),
        "generated_at_utc": str(row.get("generated_at_utc") or ""),
        "selection_rule": selection_rule,
        "predicted_winner": str(row.get("predicted_winner") or ""),
        "predicted_away_score": away_score,
        "predicted_home_score": home_score,
        "projected_total": projected_total,
        "home_win_probability": probability,
        "expected_home_margin": margin,
        "confidence_stars": stars,
        "derived": {
            "p_cover_home": _round(
                cover_probability(
                    margin,
                    latest["home_spread"],
                    policy["sigma_margin"],
                    table=table,
                )
            ),
            "p_over": _round(
                over_probability(
                    projected_total,
                    latest["total"],
                    policy["sigma_total"],
                    table=table,
                )
            ),
        },
        "legs": legs,
        "markets": markets,
        # Extracted from the full factor lists, before the caps below.
        "evidence": extract_evidence(
            _text_items(row.get("supporting_factors_json"))
            + _text_items(row.get("counterarguments_json"))
        ),
        "thesis": str(row.get("thesis") or "").strip(),
        "supporting_factors": _capped(
            _text_items(row.get("supporting_factors_json")), limit, chars
        ),
        "counterarguments": _capped(
            _text_items(row.get("counterarguments_json")), limit, chars
        ),
        "no_signal_factors": _capped(
            _text_items(row.get("no_signal_factors_json")), limit, chars
        ),
        "discarded_considerations": _capped(
            _text_items(row.get("discarded_considerations_json")), limit, chars
        ),
        "track_record": track_record,
    }


# --------------------------------------------------------------------------
# Scoreboard: grading resolved opinions


def _leg_result(
    leg: dict[str, Any] | None,
    *,
    kind: str,
    away_team: str,
    home_team: str,
    final: dict[str, Any],
    closing: dict[str, Any] | None,
) -> dict[str, Any] | None:
    if (
        not leg
        or leg.get("selection") in (None, "", "PASS")
        or leg.get("line") is None
    ):
        return None
    selection = str(leg["selection"])
    line = float(leg["line"])
    away_score = int(final["away_score"])
    home_score = int(final["home_score"])
    clv = None
    if kind == "side":
        if selection not in {away_team, home_team}:
            return None
        team_margin = (
            home_score - away_score
            if selection == home_team
            else away_score - home_score
        )
        settled = team_margin + line
        result = "W" if settled > 0 else "L" if settled < 0 else "P"
        if closing is not None:
            closing_line = (
                closing.get("home_spread")
                if selection == home_team
                else closing.get("away_spread")
            )
            if closing_line is not None:
                clv = round(line - float(closing_line), 2)
    else:
        if selection not in {"Over", "Under"}:
            return None
        actual = away_score + home_score
        if actual == line:
            result = "P"
        elif (actual > line) == (selection == "Over"):
            result = "W"
        else:
            result = "L"
        if closing is not None and closing.get("total") is not None:
            closing_total = float(closing["total"])
            clv = round(
                closing_total - line
                if selection == "Over"
                else line - closing_total,
                2,
            )
    return {
        "kind": kind,
        "selection": selection,
        "line": line,
        "result": result,
        "clv_points": clv,
    }


def grade_opinion_row(
    row: dict[str, Any],
    *,
    finals: Iterable[dict[str, Any]],
    snapshots: Iterable[dict[str, Any]],
) -> dict[str, Any] | None:
    """Grade one approved opinion against a final. None when unresolved."""
    final = _matching_history_game(row, finals)
    if final is None:
        return None
    away_team = str(row.get("away_team"))
    home_team = str(row.get("home_team"))
    away_score = int(final["away_score"])
    home_score = int(final["home_score"])
    outcome = (
        1.0 if home_score > away_score else 0.0 if home_score < away_score else None
    )
    probability = _number(
        row.get("home_win_probability"), "home_win_probability"
    )
    closing = closing_market(
        str(row.get("event_id")), str(row["commence_time_utc"]), snapshots
    )
    ats = ou = None
    if closing is not None and closing.get("home_spread") is not None:
        ats = _grade_side(
            projected_winner=str(row.get("predicted_winner")),
            away_team=away_team,
            home_team=home_team,
            market=closing,
            result=final,
        )
    if closing is not None and closing.get("total") is not None:
        projected_total = int(
            _number(row.get("predicted_away_score"), "predicted_away_score")
        ) + int(_number(row.get("predicted_home_score"), "predicted_home_score"))
        settled = _grade_total(closing, final)
        if projected_total != float(closing["total"]) and settled is not None:
            if settled == "P":
                ou = "P"
            else:
                lean_over = projected_total > float(closing["total"])
                ou = "W" if (settled == "O") == lean_over else "L"
    legs = [
        leg
        for leg in (
            _leg_result(
                _leg_from_json(row.get("side_pick_json")),
                kind="side",
                away_team=away_team,
                home_team=home_team,
                final=final,
                closing=closing,
            ),
            _leg_result(
                _leg_from_json(row.get("total_pick_json")),
                kind="total",
                away_team=away_team,
                home_team=home_team,
                final=final,
                closing=closing,
            ),
        )
        if leg is not None
    ]
    return {
        "opinion_id": str(row.get("opinion_id") or ""),
        "expert_id": str(row.get("expert_id") or ""),
        "event_id": str(row.get("event_id") or ""),
        "season": row.get("season"),
        "week": row.get("week"),
        "away_team": away_team,
        "home_team": home_team,
        "final": f"{away_score}-{home_score}",
        "home_won": outcome,
        "home_win_probability": round(probability, 4),
        "brier": (
            None if outcome is None else round((probability - outcome) ** 2, 4)
        ),
        "ats_at_close": ats,
        "ou_at_close": ou,
        "legs": legs,
        "closing_available": closing is not None,
    }


def grade_all(
    approved_rows: Iterable[dict[str, Any]],
    *,
    finals: Iterable[dict[str, Any]],
    snapshots: Iterable[dict[str, Any]],
    registry: dict[str, Any],
    policy: dict[str, Any],
) -> list[dict[str, Any]]:
    """Grade every resolvable approved opinion.

    Voices are graded under the same one-row-per-expert-per-game rule the
    live selection uses, so a model comparison run never counts twice.
    Aggregator rows are graded for the ledger but never as voices.
    """
    finals = list(finals)
    snapshots = list(snapshots)
    rows = list(approved_rows)
    experts = registry["experts"]
    graded: list[dict[str, Any]] = []
    for event_id in sorted({str(row.get("event_id")) for row in rows}):
        selected = select_voice_rows(
            rows, event_id=event_id, registry=registry, policy=policy
        )
        aggregator_rows = [
            row
            for row in rows
            if str(row.get("event_id")) == event_id
            and str(row.get("review_status") or "") == "approved"
            and str(
                (experts.get(str(row.get("expert_id") or "")) or {}).get("mode")
                or ""
            )
            in AGGREGATOR_MODES
        ]
        for row in [item[2] for item in selected] + aggregator_rows:
            result = grade_opinion_row(row, finals=finals, snapshots=snapshots)
            if result is not None:
                graded.append(result)
    return graded


def _empty_record() -> dict[str, Any]:
    return {
        "resolved": 0,
        "brier": None,
        "ats": {"w": 0, "l": 0, "p": 0},
        "ou": {"w": 0, "l": 0, "p": 0},
        "legs": {"w": 0, "l": 0, "p": 0},
        "clv_points_mean": None,
        "clv_legs": 0,
    }


def build_scoreboard(
    approved_rows: Iterable[dict[str, Any]],
    *,
    finals: Iterable[dict[str, Any]],
    snapshots: Iterable[dict[str, Any]],
    registry: dict[str, Any],
    policy: dict[str, Any],
    as_of: str,
) -> dict[str, Any]:
    """Per-expert track record from every resolved approved opinion."""
    graded = grade_all(
        approved_rows,
        finals=finals,
        snapshots=snapshots,
        registry=registry,
        policy=policy,
    )
    board: dict[str, dict[str, Any]] = {
        expert_id: _empty_record()
        for expert_id, config in registry["experts"].items()
        if isinstance(config, dict) and config.get("enabled")
    }
    sums: dict[str, dict[str, float]] = {}
    for result in graded:
        expert_id = result["expert_id"]
        record = board.setdefault(expert_id, _empty_record())
        totals = sums.setdefault(
            expert_id, {"brier": 0.0, "brier_n": 0, "clv": 0.0, "clv_n": 0}
        )
        record["resolved"] += 1
        if result["brier"] is not None:
            totals["brier"] += result["brier"]
            totals["brier_n"] += 1
        for key, outcome in (
            ("ats", result["ats_at_close"]),
            ("ou", result["ou_at_close"]),
        ):
            if outcome in {"W", "L", "P"}:
                record[key][outcome.lower()] += 1
        for leg in result["legs"]:
            record["legs"][leg["result"].lower()] += 1
            if leg["clv_points"] is not None:
                totals["clv"] += leg["clv_points"]
                totals["clv_n"] += 1
    for expert_id, totals in sums.items():
        record = board[expert_id]
        if totals["brier_n"]:
            record["brier"] = round(totals["brier"] / totals["brier_n"], 4)
        if totals["clv_n"]:
            record["clv_points_mean"] = round(totals["clv"] / totals["clv_n"], 2)
            record["clv_legs"] = int(totals["clv_n"])
    return {
        "as_of": as_of,
        "resolved_games": len({result["event_id"] for result in graded}),
        "graded_opinions": len(graded),
        "by_expert": board,
    }


def ledger_row(result: dict[str, Any], *, graded_at_utc: str) -> dict[str, Any]:
    """Flatten one graded opinion into the ``moe_grades`` tab shape."""
    legs = {leg["kind"]: leg for leg in result["legs"]}
    side = legs.get("side") or {}
    total = legs.get("total") or {}
    return {
        "graded_at_utc": graded_at_utc,
        "opinion_id": result["opinion_id"],
        "expert_id": result["expert_id"],
        "event_id": result["event_id"],
        "season": result.get("season") if result.get("season") not in (None, "") else "",
        "week": result.get("week") if result.get("week") not in (None, "") else "",
        "away_team": result["away_team"],
        "home_team": result["home_team"],
        "final": result["final"],
        "home_won": "" if result["home_won"] is None else int(result["home_won"]),
        "home_win_probability": result["home_win_probability"],
        "brier": "" if result["brier"] is None else result["brier"],
        "ats_at_close": result["ats_at_close"] or "",
        "ou_at_close": result["ou_at_close"] or "",
        "side_selection": side.get("selection", ""),
        "side_line": "" if side.get("line") is None else side["line"],
        "side_result": side.get("result", ""),
        "side_clv_points": "" if side.get("clv_points") is None else side["clv_points"],
        "total_selection": total.get("selection", ""),
        "total_line": "" if total.get("line") is None else total["line"],
        "total_result": total.get("result", ""),
        "total_clv_points": (
            "" if total.get("clv_points") is None else total["clv_points"]
        ),
        "closing_available": bool(result["closing_available"]),
    }


# --------------------------------------------------------------------------
# Disagreement report: the two arms on one game, and the mean-of-arms row


def _parsed_json(value: Any) -> dict[str, Any] | None:
    """A persisted JSON column as a dict; None when empty or not an object."""
    if isinstance(value, dict):
        return value
    if value in (None, ""):
        return None
    try:
        parsed = json.loads(str(value))
    except json.JSONDecodeError:
        return None
    return parsed if isinstance(parsed, dict) else None


def _latest_item(
    items: list[tuple[dict[str, Any], dict[str, Any]]],
) -> tuple[dict[str, Any], dict[str, Any]]:
    return max(items, key=lambda item: _row_order(item[0]))


def arm_pairs(
    approved_rows: Iterable[dict[str, Any]],
    graded: Iterable[dict[str, Any]],
) -> list[dict[str, Any]]:
    """One (rules, judge) pair per event graded for both arms.

    ``graded`` is :func:`grade_all` over ``approved_rows``. The judge row
    whose masked request names a rules row's ``input_sha256`` wins (both
    arms on one sheet state, ``linked``); otherwise the latest row per arm
    pairs up. Sorted by kickoff, then event id.
    """
    rows_by_id = {
        str(row.get("opinion_id") or ""): row for row in approved_rows
    }
    by_event: dict[
        str, dict[str, list[tuple[dict[str, Any], dict[str, Any]]]]
    ] = {}
    for result in graded:
        expert_id = str(result.get("expert_id") or "")
        if expert_id not in {RULES_EXPERT_ID, JUDGE_EXPERT_ID}:
            continue
        row = rows_by_id.get(str(result.get("opinion_id") or ""))
        if row is None:
            continue
        by_event.setdefault(str(result["event_id"]), {}).setdefault(
            expert_id, []
        ).append((row, result))
    pairs = []
    for event_id, arms in by_event.items():
        rules_items = arms.get(RULES_EXPERT_ID) or []
        judge_items = arms.get(JUDGE_EXPERT_ID) or []
        if not rules_items or not judge_items:
            continue
        rules_by_hash: dict[
            str, list[tuple[dict[str, Any], dict[str, Any]]]
        ] = {}
        for item in rules_items:
            rules_by_hash.setdefault(
                str(item[0].get("input_sha256") or ""), []
            ).append(item)
        linked = []
        for judge_item in judge_items:
            request = _parsed_json(judge_item[0].get("input_json")) or {}
            digest = str(request.get("aggregator_input_sha256") or "")
            if digest and digest in rules_by_hash:
                linked.append(
                    (_latest_item(rules_by_hash[digest]), judge_item)
                )
        if linked:
            rules_item, judge_item = max(
                linked, key=lambda item: _row_order(item[1][0])
            )
        else:
            rules_item = _latest_item(rules_items)
            judge_item = _latest_item(judge_items)
        rules_row, rules_result = rules_item
        judge_row, judge_result = judge_item
        pairs.append(
            {
                "event_id": event_id,
                "season": rules_result.get("season"),
                "week": rules_result.get("week"),
                "away_team": rules_result["away_team"],
                "home_team": rules_result["home_team"],
                "commence_time_utc": str(
                    rules_row.get("commence_time_utc") or ""
                ),
                "final": rules_result["final"],
                "rules": rules_result,
                "judge": judge_result,
                "rules_row": rules_row,
                "judge_row": judge_row,
                "linked": bool(linked),
            }
        )
    pairs.sort(
        key=lambda pair: (
            _parse_time(pair["commence_time_utc"]),
            pair["event_id"],
        )
    )
    return pairs


def _graded_leg(result: dict[str, Any], kind: str) -> dict[str, Any] | None:
    return next((leg for leg in result["legs"] if leg["kind"] == kind), None)


def _graded_leg_label(leg: dict[str, Any] | None) -> str:
    if leg is None:
        return "PASS"
    return f"{_leg_label(leg)} ({leg['result']})"


def compare_legs(
    rules_leg: dict[str, Any] | None,
    judge_leg: dict[str, Any] | None,
    *,
    kind: str | None = None,
) -> dict[str, Any]:
    """Compare one graded leg per arm; ``None`` is a PASS.

    Agreement is the same selection and line (two passes agree). Where the
    arms differ, the arm that bet and won is right; against a lost bet, the
    arm that passed; two winners, two losers, or a push leave ``neither``.
    """
    if kind is None:
        kind = str((rules_leg or judge_leg or {}).get("kind") or "")
    agreed = (rules_leg is None and judge_leg is None) or (
        rules_leg is not None
        and judge_leg is not None
        and rules_leg["selection"] == judge_leg["selection"]
        and rules_leg["line"] == judge_leg["line"]
    )
    right = None
    if not agreed:
        results = {
            "rules": None if rules_leg is None else rules_leg["result"],
            "judge": None if judge_leg is None else judge_leg["result"],
        }
        winners = [arm for arm, result in results.items() if result == "W"]
        losers = [arm for arm, result in results.items() if result == "L"]
        passes = [arm for arm, result in results.items() if result is None]
        if len(winners) == 1:
            right = winners[0]
        elif len(losers) == 1 and len(passes) == 1:
            right = passes[0]
        else:
            right = "neither"
    return {
        "kind": kind,
        "agreed": agreed,
        "rules": _graded_leg_label(rules_leg),
        "judge": _graded_leg_label(judge_leg),
        "right": right,
    }


def disagreement_report(pairs: list[dict[str, Any]]) -> dict[str, Any]:
    """Per-game Briers and leg agreement, with season totals.

    ``brier_diff_mean`` is rules minus judge over games where both Briers
    exist; ``brier_diff_se`` is the sample standard deviation (ddof=1) over
    the square root of that count, None below two games.
    """
    games = []
    diffs: list[float] = []
    agreed = 0
    record = {"rules": 0, "judge": 0, "neither": 0}
    for pair in pairs:
        legs = [
            compare_legs(
                _graded_leg(pair["rules"], kind),
                _graded_leg(pair["judge"], kind),
                kind=kind,
            )
            for kind in ("side", "total")
        ]
        for leg in legs:
            if leg["agreed"]:
                agreed += 1
            else:
                record[leg["right"]] += 1
        brier_rules = pair["rules"]["brier"]
        brier_judge = pair["judge"]["brier"]
        if brier_rules is not None and brier_judge is not None:
            diffs.append(float(brier_rules) - float(brier_judge))
        games.append(
            {
                "event": pair["event_id"],
                "week": pair["week"],
                "teams": f"{pair['away_team']} @ {pair['home_team']}",
                "kickoff": pair["commence_time_utc"],
                "linked": pair["linked"],
                "final": pair["final"],
                "brier_rules": brier_rules,
                "brier_judge": brier_judge,
                "legs": legs,
            }
        )
    legs_total = 2 * len(games)
    mean = se = None
    if diffs:
        mean = sum(diffs) / len(diffs)
        if len(diffs) >= 2:
            variance = sum((diff - mean) ** 2 for diff in diffs) / (
                len(diffs) - 1
            )
            se = math.sqrt(variance) / math.sqrt(len(diffs))
    return {
        "games": games,
        "totals": {
            "n_games": len(games),
            "legs_total": legs_total,
            "legs_agreed": agreed,
            "agreement_rate": (
                None if not legs_total else round(agreed / legs_total, 4)
            ),
            "brier_diff_n": len(diffs),
            "brier_diff_mean": _round(mean),
            "brier_diff_se": _round(se),
            "disagreement_record": record,
        },
    }


def _fmt(value: Any, spec: str = ".4f") -> str:
    return "—" if value is None else format(float(value), spec)


def format_disagreement_report(report: dict[str, Any]) -> list[str]:
    """Plain aligned lines for a terminal; ``—`` marks a missing number."""
    games = report["games"]
    if not games:
        return ["no games graded for both arms"]
    totals = report["totals"]
    width = max(len(game["teams"]) for game in games)
    count = int(totals["n_games"])
    lines = [
        f"Disagreement report: {count} game{'s' if count != 1 else ''} "
        "graded for both arms"
    ]
    for game in games:
        week = "wk—" if game["week"] in (None, "") else f"wk{game['week']}"
        diff = (
            None
            if game["brier_rules"] is None or game["brier_judge"] is None
            else float(game["brier_rules"]) - float(game["brier_judge"])
        )
        lines.append(
            f"{week:<5} {game['teams']:<{width}}  final={game['final']:<7} "
            f"brier rules={_fmt(game['brier_rules'])} "
            f"judge={_fmt(game['brier_judge'])} diff={_fmt(diff, '+.4f')}  "
            f"linked={'yes' if game['linked'] else 'no'}"
        )
        for leg in game["legs"]:
            if leg["agreed"]:
                lines.append(f"      {leg['kind']:<6} agree   {leg['rules']}")
            else:
                lines.append(
                    f"      {leg['kind']:<6} differ  rules={leg['rules']}  "
                    f"judge={leg['judge']}  right={leg['right']}"
                )
    rate = totals["agreement_rate"]
    rate_text = "—" if rate is None else format(float(rate), ".1%")
    lines.append(
        f"games={totals['n_games']} legs={totals['legs_total']} "
        f"agreed={totals['legs_agreed']} agreement_rate={rate_text}"
    )
    lines.append(
        "brier diff (rules - judge): "
        f"mean={_fmt(totals['brier_diff_mean'], '+.4f')} "
        f"se={_fmt(totals['brier_diff_se'])} n={totals['brier_diff_n']}"
    )
    record = totals["disagreement_record"]
    lines.append(
        f"disagreement record: rules={record['rules']} "
        f"judge={record['judge']} neither={record['neither']}"
    )
    return lines


def mean_of_arms_results(
    pairs: list[dict[str, Any]],
    *,
    finals: Iterable[dict[str, Any]],
    snapshots: Iterable[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Grade the mean of the two arms' estimates per pair, for the ledger only.

    The bake-off scores the mean as a free third row. It is not an expert:
    it never enters the registry, the scoreboard, Hedge weights, or voice
    selection. Legs come from the shared policy on the rules row's persisted
    market; policy keys added since that row was persisted take their
    defaults. Results have the :func:`grade_opinion_row` shape plus
    ``estimate`` and ``arms``, so :func:`ledger_row` flattens them.
    """
    finals = list(finals)
    snapshots = list(snapshots)
    results = []
    for pair in pairs:
        rules_row, judge_row = pair["rules_row"], pair["judge_row"]
        rules_input = _parsed_json(rules_row.get("input_json"))
        if not rules_input or not isinstance(rules_input.get("market"), dict):
            raise ValueError(
                f"Rules opinion {rules_row.get('opinion_id')} has no "
                "persisted aggregator input"
            )
        policy = {**DEFAULT_POLICY, **(rules_input.get("policy") or {})}
        probability = (
            _number(rules_row.get("home_win_probability"), "home_win_probability")
            + _number(judge_row.get("home_win_probability"), "home_win_probability")
        ) / 2
        margin = (
            _number(rules_row.get("expected_home_margin"), "expected_home_margin")
            + _number(judge_row.get("expected_home_margin"), "expected_home_margin")
        ) / 2
        projected_total = sum(
            int(_number(row.get("predicted_away_score"), "predicted_away_score"))
            + int(_number(row.get("predicted_home_score"), "predicted_home_score"))
            for row in (rules_row, judge_row)
        ) / 2
        away_team = str(pair["away_team"])
        home_team = str(pair["home_team"])
        legs = apply_policy(
            home_win_probability=probability,
            expected_home_margin=margin,
            projected_total=projected_total,
            market=rules_input["market"],
            policy=policy,
            away_team=away_team,
            home_team=home_team,
        )
        away_score, home_score = _scores_from_estimate(
            probability, margin, projected_total
        )
        rules_id = str(rules_row.get("opinion_id") or "")
        judge_id = str(judge_row.get("opinion_id") or "")
        row = {
            "opinion_id": f"mean:{rules_id}:{judge_id}",
            "expert_id": MEAN_OF_ARMS_ID,
            "generated_at_utc": max(
                str(rules_row.get("generated_at_utc") or ""),
                str(judge_row.get("generated_at_utc") or ""),
            ),
            "event_id": pair["event_id"],
            "season": pair["season"],
            "week": pair["week"],
            "commence_time_utc": pair["commence_time_utc"],
            "away_team": away_team,
            "home_team": home_team,
            "predicted_winner": home_team if probability > 0.5 else away_team,
            "home_win_probability": round(probability, 4),
            "expected_home_margin": round(margin, 2),
            "predicted_away_score": away_score,
            "predicted_home_score": home_score,
            "confidence_stars": max(
                legs["side"]["confidence_stars"],
                legs["total"]["confidence_stars"],
            ),
            "pick_market": "side_and_total",
            "side_pick_json": canonical_json(legs["side"]),
            "total_pick_json": canonical_json(legs["total"]),
        }
        result = grade_opinion_row(row, finals=finals, snapshots=snapshots)
        if result is None:
            continue
        result["estimate"] = {
            "home_win_probability": row["home_win_probability"],
            "expected_home_margin": row["expected_home_margin"],
            "projected_total": round(projected_total, 2),
            "predicted_away_score": away_score,
            "predicted_home_score": home_score,
        }
        result["arms"] = {"rules": rules_id, "judge": judge_id}
        results.append(result)
    return results


def hedge_weights(
    scoreboard: dict[str, Any],
    voice_ids: Iterable[str],
    policy: dict[str, Any],
) -> dict[str, Any]:
    """Multiplicative weights from relative Brier, floored and capped.

    A voice keeps weight 1.0 until it has ``weights_min_resolved`` graded
    games; abstainers are untouched. Cumulative regret is approximated by
    resolved count times the Brier gap to the eligible mean.
    """
    board = scoreboard.get("by_expert", {})
    voice_ids = list(voice_ids)
    eligible = {
        voice_id: board[voice_id]
        for voice_id in voice_ids
        if voice_id in board
        and board[voice_id]["brier"] is not None
        and int(board[voice_id]["resolved"]) >= policy["weights_min_resolved"]
    }
    weights = {voice_id: 1.0 for voice_id in voice_ids}
    if len(eligible) < 2:
        return {"weights": weights, "active": False, "mean_brier": None}
    mean_brier = sum(record["brier"] for record in eligible.values()) / len(
        eligible
    )
    for voice_id, record in eligible.items():
        exponent = -policy["hedge_eta"] * int(record["resolved"]) * (
            float(record["brier"]) - mean_brier
        )
        weights[voice_id] = round(
            _clip(math.exp(exponent), policy["weight_floor"], policy["weight_cap"]),
            4,
        )
    return {"weights": weights, "active": True, "mean_brier": round(mean_brier, 4)}


# --------------------------------------------------------------------------
# Feature block and input


def build_feature_block(
    voices: list[dict[str, Any]],
    market: dict[str, Any],
    policy: dict[str, Any],
    weighting: dict[str, Any],
    *,
    home_team: str,
) -> dict[str, Any]:
    """Pool, shrink, and edges for both arms.

    ``hedge_weights`` are the track-record weights; ``weights`` divide them
    by one plus each voice's evidence overlap with the voices ranked before
    it (``overlap_adjusted_weights``) and are what every pool uses. The side
    pool (win probability, margin, cover probability, winner votes) averages
    the side-informed voices, the total pool (projected total, over
    probability) the total-informed ones; ``markets`` records both lists. An
    empty pool has nothing to shrink: its pooled values are None and the
    blend is the market expectation, so the only edge left is the asymmetry
    of the posted prices.
    """
    if not voices:
        raise ValueError("No approved voices exist for this game")
    hedge = {
        voice["voice_id"]: float(weighting["weights"][voice["voice_id"]])
        for voice in voices
    }
    overlap = evidence_overlap(voices)
    weights = overlap_adjusted_weights(hedge, overlap)
    side_voices = [voice for voice in voices if "side" in _voice_markets(voice)]
    total_voices = [
        voice for voice in voices if "total" in _voice_markets(voice)
    ]

    def pooled(
        key: str, group: list[dict[str, Any]], *, derived: bool = False
    ) -> float | None:
        total_weight = sum(weights[voice["voice_id"]] for voice in group)
        if not group or total_weight <= 0:
            return None
        return (
            sum(
                weights[voice["voice_id"]]
                * float(voice["derived"][key] if derived else voice[key])
                for voice in group
            )
            / total_weight
        )

    def spread(key: str, group: list[dict[str, Any]]) -> dict[str, float] | None:
        if not group:
            return None
        values = [float(voice[key]) for voice in group]
        return {
            "min": round(min(values), 4),
            "max": round(max(values), 4),
            "range": round(max(values) - min(values), 4),
        }

    fair = market["fair"]
    latest = market["latest"]
    lam = policy["shrink_lambda"]
    table = margin_table_for(policy)
    fair_home = float(fair["home_ml"])
    market_margin = float(market["market_expectation"]["home_margin"])
    market_total = float(market["market_expectation"]["total"])
    pool_p = pooled("home_win_probability", side_voices)
    pool_margin = pooled("expected_home_margin", side_voices)
    pool_total = pooled("projected_total", total_voices)
    shrunk_p = (
        fair_home if pool_p is None else lam * pool_p + (1 - lam) * fair_home
    )
    shrunk_margin = (
        market_margin
        if pool_margin is None
        else lam * pool_margin + (1 - lam) * market_margin
    )
    shrunk_total = (
        market_total
        if pool_total is None
        else lam * pool_total + (1 - lam) * market_total
    )
    home_votes = sum(
        1 for voice in side_voices if voice["predicted_winner"] == home_team
    )
    return {
        "n_voices": len(voices),
        "markets": {
            "side": [voice["voice_id"] for voice in side_voices],
            "total": [voice["voice_id"] for voice in total_voices],
        },
        "hedge_weights": {
            voice["voice_id"]: hedge[voice["voice_id"]] for voice in voices
        },
        "overlap": overlap,
        "weights": {
            voice["voice_id"]: weights[voice["voice_id"]] for voice in voices
        },
        "weights_active": bool(weighting["active"]),
        "pool": {
            "home_win_probability": _round(pool_p),
            "expected_home_margin": _round(pool_margin, 2),
            "projected_total": _round(pool_total, 2),
            "p_cover_home": _round(
                pooled("p_cover_home", side_voices, derived=True)
            ),
            "p_over": _round(pooled("p_over", total_voices, derived=True)),
        },
        "shrunk": {
            "lambda": lam,
            "home_win_probability": _round(shrunk_p),
            "expected_home_margin": _round(shrunk_margin, 2),
            "projected_total": _round(shrunk_total, 2),
        },
        "edges_if_shrunk": {
            "home_ml": _round(shrunk_p - float(fair["home_ml"])),
            "home_cover": _round(
                cover_probability(
                    shrunk_margin,
                    latest["home_spread"],
                    policy["sigma_margin"],
                    table=table,
                )
                - float(fair["home_cover"])
            ),
            "over": _round(
                over_probability(
                    shrunk_total,
                    latest["total"],
                    policy["sigma_total"],
                    table=table,
                )
                - float(fair["over"])
            ),
        },
        "dispersion": {
            "home_win_probability": spread("home_win_probability", side_voices),
            "expected_home_margin": spread("expected_home_margin", side_voices),
            "projected_total": spread("projected_total", total_voices),
            "home_winner_votes": home_votes,
            "away_winner_votes": len(side_voices) - home_votes,
        },
    }


def committee_key(input_payload: dict[str, Any]) -> str:
    """Identity of the committee state an aggregator input was built from.

    The sorted voice opinion ids plus the latest full-game lines and prices,
    and deliberately not the capture timestamp: every fetch rewrites the
    timestamp, but the judge only needs to run again when a voice or a
    number changed. The runner dedupes its subscription calls on this key.
    """
    latest = input_payload["market"]["latest"]
    return sha256_text(
        canonical_json(
            {
                "opinion_ids": sorted(
                    str(voice["opinion_id"]) for voice in input_payload["voices"]
                ),
                "latest": {field: latest[field] for field in MARKET_FIELDS},
            }
        )
    )


def build_aggregator_input(
    game: dict[str, Any],
    *,
    approved_opinions: Iterable[dict[str, Any]],
    finals: Iterable[dict[str, Any]],
    snapshots: Iterable[dict[str, Any]],
    registry: dict[str, Any] | None = None,
    policy: dict[str, Any] | None = None,
    as_of: str | None = None,
) -> dict[str, Any]:
    """The one input both arms consume. Deterministic for fixed inputs."""
    registry = registry if registry is not None else load_registry()
    policy = policy if policy is not None else aggregator_policy(registry)
    rows = list(approved_opinions)
    finals = list(finals)
    snapshots = list(snapshots)
    event_id = str(game["event_id"])
    kickoff = _parse_time(game["commence_time_utc"])
    as_of = as_of or str(
        game.get("latest_captured_at") or game["commence_time_utc"]
    )
    market = build_market_block(game)
    scoreboard = build_scoreboard(
        rows,
        finals=finals,
        snapshots=snapshots,
        registry=registry,
        policy=policy,
        as_of=as_of,
    )
    selected = select_voice_rows(
        rows, event_id=event_id, registry=registry, policy=policy
    )
    voices = []
    for expert_id, config, row, rule in selected:
        if str(row.get("away_team")) != str(game["away_team"]) or str(
            row.get("home_team")
        ) != str(game["home_team"]):
            raise ValueError(
                f"Opinion {row.get('opinion_id')} teams do not match the game"
            )
        voices.append(
            voice_from_row(
                expert_id,
                config,
                row,
                selection_rule=rule,
                market=market,
                policy=policy,
                track_record=scoreboard["by_expert"].get(
                    expert_id, _empty_record()
                ),
            )
        )
    weighting = hedge_weights(
        scoreboard, [voice["voice_id"] for voice in voices], policy
    )
    feature_block = build_feature_block(
        voices, market, policy, weighting, home_team=str(game["home_team"])
    )
    week = game.get("week")
    payload = {
        "input_profile": AGGREGATOR_PROFILE,
        "policy": policy,
        "game": {
            "event_id": event_id,
            "season": int(game["season"]),
            "week": int(week) if str(week or "").strip() else None,
            "away_team": str(game["away_team"]),
            "home_team": str(game["home_team"]),
            "commence_time_utc": kickoff.isoformat(),
            "commence_time_et": str(game.get("commence_time_et") or ""),
        },
        "market": market,
        "voices": voices,
        "feature_block": feature_block,
        "scoreboard": scoreboard,
        # None under the normal model; the table's identity when empirical,
        # so the input hash changes with the table the way it does with the
        # policy knobs.
        "margin_table": margin_table_descriptor(margin_table_for(policy)),
    }
    payload["committee_key"] = committee_key(payload)
    seed = sha256_text(canonical_json(payload))[:16]
    order = [voice["voice_id"] for voice in voices]
    random.Random(seed).shuffle(order)
    if len(order) > len(JUDGE_LABELS):
        raise ValueError("Too many voices to label")
    payload["judge_view"] = {
        "seed": seed,
        "labels": {
            JUDGE_LABELS[index]: voice_id for index, voice_id in enumerate(order)
        },
    }
    return payload


def build_judge_request(input_payload: dict[str, Any]) -> dict[str, Any]:
    """The masked, shuffled document the judge model actually reads."""
    labels: dict[str, str] = input_payload["judge_view"]["labels"]
    label_of = {voice_id: label for label, voice_id in labels.items()}
    by_voice = {voice["voice_id"]: voice for voice in input_payload["voices"]}
    policy = input_payload["policy"]
    weights = input_payload["feature_block"]["weights"]
    # Present only on inputs built with the overlap discount and the market
    # masks; a request derived from an older input stays byte-identical.
    hedge_weights = input_payload["feature_block"].get("hedge_weights")
    masked_voices = []
    for label in sorted(labels, key=JUDGE_LABELS.index):
        voice = by_voice[labels[label]]
        extra: dict[str, Any] = {}
        if "markets" in voice:
            extra["markets"] = list(voice["markets"])
        if hedge_weights is not None:
            extra["hedge_weight"] = hedge_weights[voice["voice_id"]]
        masked_voices.append(
            {
                **extra,
                "label": label,
                "lens": voice["lens"],
                "selection_rule": voice["selection_rule"],
                "predicted_winner": voice["predicted_winner"],
                "predicted_away_score": voice["predicted_away_score"],
                "predicted_home_score": voice["predicted_home_score"],
                "projected_total": voice["projected_total"],
                "home_win_probability": voice["home_win_probability"],
                "expected_home_margin": voice["expected_home_margin"],
                "confidence_stars": voice["confidence_stars"],
                "derived": voice["derived"],
                "legs": voice["legs"],
                "thesis": voice["thesis"],
                "supporting_factors": voice["supporting_factors"],
                "counterarguments": voice["counterarguments"],
                "no_signal_factors": voice["no_signal_factors"],
                "discarded_considerations": voice["discarded_considerations"],
                "track_record": voice["track_record"],
                "pool_weight": weights[voice["voice_id"]],
            }
        )
    feature_block = dict(input_payload["feature_block"])
    feature_block["weights"] = {
        label: weights[voice_id] for label, voice_id in labels.items()
    }
    if hedge_weights is not None:
        feature_block["hedge_weights"] = {
            label_of[voice_id]: value for voice_id, value in hedge_weights.items()
        }
    if "overlap" in feature_block:
        feature_block["overlap"] = {
            label_of[a]: {label_of[b]: value for b, value in row.items()}
            for a, row in feature_block["overlap"].items()
        }
    if "markets" in feature_block:
        # Label order, never id order: the ids sort alphabetically and an
        # id-ordered list would reveal which label is which expert.
        feature_block["markets"] = {
            market: sorted(
                (label_of[voice_id] for voice_id in voice_ids),
                key=JUDGE_LABELS.index,
            )
            for market, voice_ids in feature_block["markets"].items()
        }
    policy_view = {
        "sigma_margin": policy["sigma_margin"],
        "sigma_total": policy["sigma_total"],
        "shrink_lambda": policy["shrink_lambda"],
        "edge_threshold": policy["edge_threshold"],
    }
    if "margin_model" in policy:
        policy_view["margin_model"] = policy["margin_model"]
    request = {
        "input_profile": JUDGE_REQUEST_PROFILE,
        "aggregator_input_sha256": sha256_text(canonical_json(input_payload)),
        # A hash of opinion ids and prices leaks nothing; the runner reads it
        # back from persisted judge rows to dedupe. Inputs persisted before
        # the key existed derive it here and never match a live key anyway.
        "committee_key": (
            input_payload.get("committee_key") or committee_key(input_payload)
        ),
        "seed": input_payload["judge_view"]["seed"],
        "policy": policy_view,
        "game": input_payload["game"],
        "market": input_payload["market"],
        "feature_block": feature_block,
        "scoreboard": {
            "as_of": input_payload["scoreboard"]["as_of"],
            "resolved_games": input_payload["scoreboard"]["resolved_games"],
        },
        "voices": masked_voices,
    }
    if input_payload.get("margin_table"):
        request["margin_table"] = input_payload["margin_table"]
    return request


# --------------------------------------------------------------------------
# Policy: probabilities -> legs


def _stars_for_edge(edge: float, policy: dict[str, Any]) -> int:
    return max(
        1, sum(1 for threshold in policy["star_edges"] if edge >= threshold)
    )


def _kelly(probability: float, price: Any, policy: dict[str, Any]) -> dict[str, float]:
    b = american_to_decimal(price) - 1.0
    ev_per_unit = probability * b - (1.0 - probability)
    full = ev_per_unit / b if b > 0 else 0.0
    fraction = _clip(
        full * policy["kelly_fraction"], 0.0, policy["max_stake_fraction"]
    )
    return {
        "ev_per_unit": round(ev_per_unit, 4),
        "stake_fraction": round(fraction, 4),
        "stake_units": round(fraction * 100, 1),
    }


def _pass_leg(
    edge: float, probability: float, fair: float, reason: str | None = None
) -> dict[str, Any]:
    return {
        "selection": "PASS",
        "line": None,
        "price": None,
        "probability": _round(probability),
        "fair_probability": _round(fair),
        "edge": _round(edge),
        "confidence_stars": 1,
        "ev_per_unit": None,
        "stake_fraction": 0.0,
        "stake_units": 0.0,
        "pass_reason": reason,
    }


def _knob(policy: dict[str, Any], key: str) -> float:
    """A veto/floor knob; a policy persisted before the knob existed reads
    the module default when its row is replayed."""
    return float(policy.get(key, DEFAULT_POLICY[key]))


def _policy_movement(market: dict[str, Any]) -> dict[str, Any]:
    """The movement block, recomputed when it predates the price deltas."""
    movement = market.get("movement_since_open")
    if isinstance(movement, dict) and all(
        field in movement for field in MOVEMENT_PRICE_FIELDS
    ):
        return movement
    opening, latest = market.get("opening"), market.get("latest")
    if isinstance(opening, dict) and isinstance(latest, dict):
        return movement_since_open(opening, latest)
    return dict(movement or {})


def _move_text(
    opening: Any, latest: Any, delta: float, *, unit: str, signed: bool = True
) -> str:
    change = f"{float(delta):+g} {unit}"
    if opening is None or latest is None:
        return change
    if unit == "cents":
        return f"{int(opening):+d} → {int(latest):+d} ({change})"
    spec = "+g" if signed else "g"
    return f"{float(opening):{spec}} → {float(latest):{spec}} ({change})"


def _adverse_move_note(
    selection: str,
    *,
    kind: str,
    market: dict[str, Any],
    movement: dict[str, Any],
    policy: dict[str, Any],
    home_team: str,
) -> str | None:
    """The policy note when the market moved away from this leg since open.

    A leg is vetoed when its own side got cheaper: the team's spread rose by
    the points threshold (home bet: home spread up; away bet: home spread
    down), the total fell against an Over or rose against an Under, or the
    leg's price lengthened by the cents threshold. Missing opening data never
    vetoes. Returns None when the leg stands.
    """
    tolerance = 1e-9
    opening = market.get("opening") or {}
    latest = market.get("latest") or {}
    if kind == "side":
        is_home = selection == home_team
        home_delta = movement.get("home_spread")
        if home_delta is not None:
            team_delta = float(home_delta) if is_home else -float(home_delta)
            if team_delta >= _knob(policy, "veto_adverse_spread_points") - tolerance:
                field = "home_spread" if is_home else "away_spread"
                return (
                    f"{ADVERSE_MOVE_REASON}: {selection} spread "
                    + _move_text(
                        opening.get(field), latest.get(field), team_delta, unit="points"
                    )
                    + " since open"
                )
        field = "home_spread_price" if is_home else "away_spread_price"
        label = f"{selection} spread price"
    else:
        is_over = selection == "Over"
        total_delta = movement.get("total")
        if total_delta is not None:
            against = -float(total_delta) if is_over else float(total_delta)
            if against >= _knob(policy, "veto_adverse_total_points") - tolerance:
                return (
                    f"{ADVERSE_MOVE_REASON}: total "
                    + _move_text(
                        opening.get("total"),
                        latest.get("total"),
                        float(total_delta),
                        unit="points",
                        signed=False,
                    )
                    + f" against the {selection} since open"
                )
        field = "over_price" if is_over else "under_price"
        label = f"{selection} price"
    price_delta = movement.get(field)
    if price_delta is not None and float(price_delta) >= _knob(
        policy, "veto_adverse_price_cents"
    ) - tolerance:
        return (
            f"{ADVERSE_MOVE_REASON}: {label} "
            + _move_text(
                opening.get(field), latest.get(field), float(price_delta), unit="cents"
            )
            + " since open"
        )
    return None


def apply_policy(
    *,
    home_win_probability: float,
    expected_home_margin: float,
    projected_total: float,
    market: dict[str, Any],
    policy: dict[str, Any],
    away_team: str,
    home_team: str,
) -> dict[str, Any]:
    latest = market["latest"]
    fair = market["fair"]
    movement = _policy_movement(market)
    table = margin_table_for(policy)
    p_cover_home = cover_probability(
        expected_home_margin,
        latest["home_spread"],
        policy["sigma_margin"],
        table=table,
    )
    p_over = over_probability(
        projected_total, latest["total"], policy["sigma_total"], table=table
    )
    edges = {
        "home_ml": home_win_probability - float(fair["home_ml"]),
        "away_ml": (1 - home_win_probability) - float(fair["away_ml"]),
        "home_cover": p_cover_home - float(fair["home_cover"]),
        "away_cover": (1 - p_cover_home) - float(fair["away_cover"]),
        "over": p_over - float(fair["over"]),
        "under": (1 - p_over) - float(fair["under"]),
    }

    def choose(
        candidates: list[tuple[str, float, float, float, Any, float]],
        *,
        kind: str,
    ) -> tuple[dict[str, Any], str | None]:
        selection, edge, probability, line, price, fair_probability = max(
            candidates, key=lambda item: item[1]
        )
        if edge < policy["edge_threshold"]:
            return _pass_leg(edge, probability, fair_probability), None
        kelly = _kelly(probability, price, policy)
        if kelly["ev_per_unit"] <= 0:
            return (
                _pass_leg(
                    edge, probability, fair_probability, NO_EXPECTATION_REASON
                ),
                NO_EXPECTATION_REASON,
            )
        veto = _adverse_move_note(
            selection,
            kind=kind,
            market=market,
            movement=movement,
            policy=policy,
            home_team=home_team,
        )
        if veto:
            return (
                _pass_leg(edge, probability, fair_probability, ADVERSE_MOVE_REASON),
                veto,
            )
        floor = _knob(policy, "min_ev_per_unit")
        if kelly["ev_per_unit"] < floor - 1e-9:
            label = _leg_label({"selection": selection, "line": line})
            return (
                _pass_leg(edge, probability, fair_probability, EV_FLOOR_REASON),
                f"{EV_FLOOR_REASON}: {label} ({int(price):+d}) ev "
                f"{kelly['ev_per_unit']:+.3f} under {floor:.3f}",
            )
        return (
            {
                "selection": selection,
                "line": float(line),
                "price": int(price),
                "probability": _round(probability),
                "fair_probability": _round(fair_probability),
                "edge": _round(edge),
                "confidence_stars": _stars_for_edge(edge, policy),
                **kelly,
                "pass_reason": None,
            },
            None,
        )

    side, side_note = choose(
        [
            (
                home_team,
                edges["home_cover"],
                p_cover_home,
                latest["home_spread"],
                latest["home_spread_price"],
                float(fair["home_cover"]),
            ),
            (
                away_team,
                edges["away_cover"],
                1 - p_cover_home,
                latest["away_spread"],
                latest["away_spread_price"],
                float(fair["away_cover"]),
            ),
        ],
        kind="side",
    )
    total, total_note = choose(
        [
            (
                "Over",
                edges["over"],
                p_over,
                latest["total"],
                latest["over_price"],
                float(fair["over"]),
            ),
            (
                "Under",
                edges["under"],
                1 - p_over,
                latest["total"],
                latest["under_price"],
                float(fair["under"]),
            ),
        ],
        kind="total",
    )
    return {
        "p_cover_home": _round(p_cover_home),
        "p_over": _round(p_over),
        "edges": {key: _round(value) for key, value in edges.items()},
        "side": side,
        "total": total,
        "notes": [note for note in (side_note, total_note) if note],
    }


# --------------------------------------------------------------------------
# The rules arm's response (same shape as the judge's)


def coherent_estimate(
    probability: float, margin: float, *, fair_home: float
) -> tuple[float, float, list[str]]:
    """The coherence step shared by the rules arm and the judge ensemble.

    An estimate exactly on the fence (probability 0.5 or margin 0) leans the
    market favorite's way (0.505 and +0.5, or 0.495 and -0.5); a probability
    and a margin that disagree in sign keep the probability and clamp the
    margin to +/-0.5. Either case adds one note for the discarded
    considerations. A coherent estimate passes through untouched.
    """
    probability, margin = float(probability), float(margin)
    notes: list[str] = []
    if abs(probability - 0.5) < 1e-9 or abs(margin) < 1e-9:
        lean_home = float(fair_home) >= 0.5
        probability = 0.505 if lean_home else 0.495
        margin = 0.5 if lean_home else -0.5
        notes.append(FENCE_NOTE)
    elif (probability > 0.5) != (margin > 0):
        margin = 0.5 if probability > 0.5 else -0.5
        notes.append(SIGN_NOTE)
    return probability, margin, notes


def rules_arm_response(input_payload: dict[str, Any]) -> dict[str, Any]:
    feature = input_payload["feature_block"]
    market = input_payload["market"]
    shrunk = feature["shrunk"]
    total = float(shrunk["projected_total"])
    probability, margin, notes = coherent_estimate(
        float(shrunk["home_win_probability"]),
        float(shrunk["expected_home_margin"]),
        fair_home=float(market["fair"]["home_ml"]),
    )
    pool = feature["pool"]
    weighted = "Hedge-weighted" if feature["weights_active"] else "Equal-weight"
    reasons = [
        {
            "voice": POOL_LABEL,
            "text": (
                f"{weighted} pool of {feature['n_voices']} voices: p(home) "
                f"{_fmt(pool['home_win_probability'], '.3f')}, margin "
                f"{_fmt(pool['expected_home_margin'], '+.1f')}, total "
                f"{_fmt(pool['projected_total'], '.1f')}."
            ),
        },
        {
            "voice": MARKET_LABEL,
            "text": (
                f"Market fair p(home) {float(market['fair']['home_ml']):.3f}; "
                f"shrink lambda {shrunk['lambda']:g} moves the pool toward the "
                "market before any edge is measured."
            ),
        },
    ]
    for voice in input_payload["voices"]:
        reasons.append(
            {
                "voice": voice["voice_id"],
                "text": (
                    f"{voice['predicted_winner']} "
                    f"{voice['predicted_away_score']}-"
                    f"{voice['predicted_home_score']}, p(home) "
                    f"{float(voice['home_win_probability']):.2f}, "
                    f"{'★' * int(voice['confidence_stars'])}, weight "
                    f"{feature['weights'][voice['voice_id']]:g}."
                ),
            }
        )
    dispersion = feature["dispersion"]["home_win_probability"]
    counterpoints = []
    if dispersion:
        counterpoints.append(
            {
                "voice": MARKET_LABEL,
                "text": (
                    f"Voices span p(home) {float(dispersion['min']):.2f} to "
                    f"{float(dispersion['max']):.2f}; the blend averages a "
                    "disagreement rather than reporting a consensus."
                ),
            }
        )
    overlap_note = _largest_overlap_text(input_payload)
    if overlap_note:
        counterpoints.append({"voice": POOL_LABEL, "text": overlap_note})
    return {
        "home_win_probability": round(probability, 4),
        "expected_home_margin": round(margin, 2),
        "projected_total": round(total, 2),
        "key_reasons": reasons[: input_payload["policy"]["reason_limit"]],
        "counterpoints": counterpoints[: input_payload["policy"]["reason_limit"]],
        "discarded_considerations": notes,
    }


def _largest_overlap_text(input_payload: dict[str, Any]) -> str | None:
    """The pair sharing the most evidence and the weight the discount left.

    None when the input predates the overlap matrix or no pair overlaps.
    """
    overlap = input_payload["feature_block"].get("overlap") or {}
    pairs = [
        (a, b, float(value))
        for a, row in overlap.items()
        for b, value in row.items()
        if a < b and float(value) > 0
    ]
    if not pairs:
        return None
    a, b, value = max(pairs, key=lambda item: (item[2], item[0], item[1]))
    names = {
        voice["voice_id"]: voice["expert_name"]
        for voice in input_payload["voices"]
    }
    weight = input_payload["feature_block"]["weights"][b]
    return (
        f"{names.get(a, a)} and {names.get(b, b)} cite the same records "
        f"(overlap {value:.2f}); {names.get(b, b)} pools at weight "
        f"{float(weight):g} after the discount."
    )


# --------------------------------------------------------------------------
# The judge ensemble: one row from several sampled responses (WP9)


def ensemble_response(
    samples: list[dict[str, Any]], *, fair_home: float, size: int
) -> dict[str, Any]:
    """One judge response from the valid sampled responses of one trigger.

    ``samples`` holds ``{"opinion_id", "response"}`` for every sample that
    validated, in call order; ``size`` is how many calls the trigger made.
    The three numbers are the means over the valid samples, rounded like a
    response (4, 2, 2 decimals) and passed through :func:`coherent_estimate`;
    the reasons are copied from the sample closest to the mean (the smallest
    |dp|, then |dmargin|, then |dtotal|, then call order), with the coherence
    notes appended to its discarded considerations. The ``ensemble`` block
    records how the row was made; :func:`normalize_aggregator_opinion`
    validates it with the rest of the response and copies it into the
    calibration summary.
    """
    if not samples:
        raise ValueError("An ensemble needs at least one valid sample")
    size = int(size)
    if size < len(samples):
        raise ValueError("An ensemble cannot hold more samples than calls")
    estimates = [
        [
            float(sample["response"]["home_win_probability"]),
            float(sample["response"]["expected_home_margin"]),
            float(sample["response"]["projected_total"]),
        ]
        for sample in samples
    ]
    count = len(estimates)
    mean_probability = round(sum(item[0] for item in estimates) / count, 4)
    mean_margin = round(sum(item[1] for item in estimates) / count, 2)
    mean_total = round(sum(item[2] for item in estimates) / count, 2)
    probability, margin, notes = coherent_estimate(
        mean_probability, mean_margin, fair_home=fair_home
    )
    # Distances are rounded so float noise never decides a tie; call order does.
    closest = min(
        range(count),
        key=lambda index: (
            round(abs(estimates[index][0] - mean_probability), 6),
            round(abs(estimates[index][1] - mean_margin), 6),
            round(abs(estimates[index][2] - mean_total), 6),
            index,
        ),
    )
    chosen = samples[closest]
    response = chosen["response"]
    return {
        "home_win_probability": round(probability, 4),
        "expected_home_margin": round(margin, 2),
        "projected_total": mean_total,
        "key_reasons": [dict(item) for item in response.get("key_reasons", [])],
        "counterpoints": [
            dict(item) for item in response.get("counterpoints", [])
        ],
        "discarded_considerations": list(
            response.get("discarded_considerations", [])
        )
        + notes,
        "ensemble": {
            "size": size,
            "valid": count,
            "samples": [str(sample["opinion_id"]) for sample in samples],
            "estimates": estimates,
            "reasons_from": str(chosen["opinion_id"]),
            "rule": ENSEMBLE_RULE,
        },
    }


# --------------------------------------------------------------------------
# Normalization shared by both arms

def _reference_form(text: str) -> str:
    """Dash and case normalization applied to a reason and the reference alike."""
    return text.replace("−", "-").replace("–", "-").lower()


def reason_reference_text(request: dict[str, Any]) -> str:
    """The judge request plus the numbers it carries in structured form.

    The reason guard rejects invented numbers, never numbers the request
    holds somewhere: a voice's projected score ("21-27", and "27-21" with the
    home team first), the winner-vote split of the pool ("2-2"), a
    track-record tally, a count stored under a numeric key, or the cohort
    size a cited record implies ("17-8" is 25 games). Those are rendered the
    way a reason would write them and appended to the request text.
    """
    text = canonical_json(request)
    derived: list[str] = []
    for voice in request.get("voices", []):
        # Both orders: "21-27" as the request writes it and "27-21" as a
        # reason may write the home team first (a live response did).
        derived.append(
            f"{voice['predicted_away_score']}-{voice['predicted_home_score']}"
        )
        derived.append(
            f"{voice['predicted_home_score']}-{voice['predicted_away_score']}"
        )
        record = voice.get("track_record") or {}
        for kind in ("legs", "ats", "ou"):
            tally = record.get(kind) or {}
            if all(key in tally for key in ("w", "l", "p")):
                derived.append(f"{tally['w']}-{tally['l']}-{tally['p']}")
                derived.append(f"{tally['w']}-{tally['l']}")
    dispersion = (request.get("feature_block") or {}).get("dispersion") or {}
    home_votes = dispersion.get("home_winner_votes")
    away_votes = dispersion.get("away_winner_votes")
    if home_votes is not None and away_votes is not None:
        derived.append(f"{home_votes}-{away_votes}")
        derived.append(f"{away_votes}-{home_votes}")

    def counts(value: Any) -> None:
        if isinstance(value, dict):
            for key, item in value.items():
                name = str(key).lower()
                if (
                    isinstance(item, int)
                    and not isinstance(item, bool)
                    and (
                        name == "n"
                        or any(word in name for word in _COUNT_KEY_WORDS)
                    )
                ):
                    derived.append(f"{item} games")
                counts(item)
        elif isinstance(value, list):
            for item in value:
                counts(item)

    counts(request)
    for match in _RECORD_PATTERN.finditer(_reference_form(text)):
        derived.append(
            f"{sum(int(group) for group in match.groups() if group)} games"
        )
    return text + "\n" + " ".join(derived)


def _check_reason_citations(text: str, reference: str, *, field: str) -> None:
    """Reject a record or game count the request does not carry.

    ``reference`` is already in ``_reference_form``. A record must appear
    verbatim; a count must sit within twelve non-word characters of the
    word "game".
    """
    claim = _reference_form(text)
    for match in _RECORD_PATTERN.finditer(claim):
        if match.group(0) not in reference:
            raise ValueError(
                f"{field} cites a record the request does not carry: "
                f"{match.group(0)!r}"
            )
    for match in _GAME_COUNT_PATTERN.finditer(claim):
        count = match.group(1)
        if (
            re.search(
                rf"game\w*\W{{0,12}}\b{count}\b|\b{count}\b\W{{0,12}}game",
                reference,
            )
            is None
        ):
            raise ValueError(
                f"{field} cites a game count the request does not carry: "
                f"{match.group(0)!r}"
            )


def _validate_reasons(
    value: Any,
    *,
    field: str,
    allowed_labels: set[str],
    policy: dict[str, Any],
    minimum: int,
    reference_text: str | None = None,
) -> list[dict[str, str]]:
    if not isinstance(value, list):
        raise ValueError(f"{field} must be a list")
    if len(value) < minimum:
        raise ValueError(f"{field} needs at least {minimum} item(s)")
    if len(value) > policy["reason_limit"]:
        raise ValueError(
            f"{field} may hold at most {policy['reason_limit']} items"
        )
    reference = (
        None if reference_text is None else _reference_form(reference_text)
    )
    normalized = []
    for item in value:
        if not isinstance(item, dict):
            raise ValueError(f"{field} items must be objects")
        label = str(item.get("voice") or "")
        text = str(item.get("text") or "").strip()
        if label not in allowed_labels:
            raise ValueError(f"{field} cites an unknown voice: {label!r}")
        if not text:
            raise ValueError(f"{field} items need text")
        if len(text) > policy["reason_chars"]:
            raise ValueError(
                f"{field} text longer than {policy['reason_chars']} characters"
            )
        if reference is not None:
            _check_reason_citations(text, reference, field=field)
        normalized.append({"voice": label, "text": text})
    return normalized


def _validate_ensemble(value: Any) -> dict[str, Any] | None:
    """The ensemble block of a judge row built from sampled responses.

    None when absent. ``size`` calls were made and ``valid`` of them
    validated; ``samples`` names those rows in call order, ``estimates``
    holds each one's three numbers, ``reasons_from`` names the sample whose
    reasons the row carries, and ``rule`` says how the numbers were combined.
    Anything else is a malformed block and fails validation.
    """
    if value is None:
        return None
    if not isinstance(value, dict):
        raise ValueError("ensemble must be an object")
    if set(value) != set(ENSEMBLE_KEYS):
        raise ValueError(f"ensemble keys must be {list(ENSEMBLE_KEYS)}")
    size, valid = value["size"], value["valid"]
    for name, item in (("size", size), ("valid", valid)):
        if isinstance(item, bool) or not isinstance(item, int) or item < 1:
            raise ValueError(f"ensemble.{name} must be a positive integer")
    if valid > size:
        raise ValueError("ensemble.valid cannot exceed ensemble.size")
    samples = value["samples"]
    if (
        not isinstance(samples, list)
        or len(samples) != valid
        or any(
            not isinstance(item, str) or not item.strip() for item in samples
        )
        or len(set(samples)) != len(samples)
    ):
        raise ValueError("ensemble.samples must name each valid sample once")
    estimates = value["estimates"]
    if not isinstance(estimates, list) or len(estimates) != valid:
        raise ValueError(
            "ensemble.estimates must hold one triple per valid sample"
        )
    normalized_estimates = []
    for triple in estimates:
        if (
            not isinstance(triple, (list, tuple))
            or len(triple) != 3
            or any(
                isinstance(item, bool)
                or not isinstance(item, (int, float))
                or not math.isfinite(float(item))
                for item in triple
            )
        ):
            raise ValueError(
                "ensemble.estimates items must be [probability, margin, total]"
            )
        normalized_estimates.append([float(item) for item in triple])
    if value["reasons_from"] not in samples:
        raise ValueError("ensemble.reasons_from must name one of the samples")
    rule = value["rule"]
    if not isinstance(rule, str) or not rule.strip():
        raise ValueError("ensemble.rule must be a non-empty string")
    return {
        "size": int(size),
        "valid": int(valid),
        "samples": [str(item) for item in samples],
        "estimates": normalized_estimates,
        "reasons_from": str(value["reasons_from"]),
        "rule": rule.strip(),
    }


def _display_label(
    label: str, input_payload: dict[str, Any], *, judge: bool
) -> str:
    names = {
        voice["voice_id"]: voice["expert_name"]
        for voice in input_payload["voices"]
    }
    if label in EXTRA_REASON_LABELS:
        return label.capitalize()
    if judge:
        voice_id = input_payload["judge_view"]["labels"].get(label)
        return f"{label} ({names.get(voice_id, voice_id)})"
    return names.get(label, label)


def _leg_label(leg: dict[str, Any]) -> str:
    if leg["selection"] == "PASS":
        return "PASS"
    if leg["selection"] in {"Over", "Under"}:
        return f"{leg['selection']} {float(leg['line']):g}"
    return f"{leg['selection']} {float(leg['line']):+g}"


def _price_move_texts(
    movement: dict[str, Any], *, away_team: str, home_team: str
) -> list[str]:
    """Non-zero price moves since open, in cents, for the renderings."""
    labels = (
        ("home_spread_price", f"{home_team} spread price"),
        ("away_spread_price", f"{away_team} spread price"),
        ("over_price", "over price"),
        ("under_price", "under price"),
    )
    return [
        f"{label} {float(movement[field]):+g} cents"
        for field, label in labels
        if movement.get(field) not in (None, 0)
    ]


def _scores_from_estimate(
    probability: float, margin: float, projected_total: float
) -> tuple[int, int]:
    """Predicted (away, home) scores from a total and margin, never tied."""
    home_score = round((projected_total + margin) / 2)
    away_score = round((projected_total - margin) / 2)
    if home_score == away_score:
        if probability > 0.5:
            home_score += 1
        else:
            away_score += 1
    return int(away_score), int(home_score)


def normalize_aggregator_opinion(
    response: dict[str, Any],
    input_payload: dict[str, Any],
    *,
    expert: dict[str, Any],
    model: str = "",
) -> dict[str, Any]:
    """Turn either arm's probability response into a full opinion."""
    mode = str(expert.get("mode") or "")
    if mode not in AGGREGATOR_MODES:
        raise ValueError(f"Not an aggregator expert mode: {mode}")
    judge = mode == JUDGE_MODE
    check_margin_table(input_payload)
    policy = input_payload["policy"]
    game = input_payload["game"]
    market = input_payload["market"]
    away_team = str(game["away_team"])
    home_team = str(game["home_team"])
    allowed_labels = set(EXTRA_REASON_LABELS) | (
        set(input_payload["judge_view"]["labels"])
        if judge
        else {voice["voice_id"] for voice in input_payload["voices"]}
    )
    unknown = set(response) - {
        "home_win_probability",
        "expected_home_margin",
        "projected_total",
        "key_reasons",
        "counterpoints",
        "discarded_considerations",
    }
    if judge:
        # Only a judge row built by the runner's ensemble carries the block.
        unknown.discard("ensemble")
    if unknown:
        raise ValueError(f"Response has unexpected fields: {sorted(unknown)}")
    for field in ("home_win_probability", "expected_home_margin", "projected_total"):
        value = response.get(field)
        if (
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or not math.isfinite(float(value))
        ):
            raise ValueError(f"{field} must be a finite number")
    probability = float(response["home_win_probability"])
    margin = float(response["expected_home_margin"])
    projected_total = float(response["projected_total"])
    if not 0.01 <= probability <= 0.99:
        raise ValueError("home_win_probability must be between 0.01 and 0.99")
    if probability == 0.5 or margin == 0:
        raise ValueError(
            "The aggregate must lean: probability 0.5 or margin 0 is not allowed"
        )
    if (probability > 0.5) != (margin > 0):
        raise ValueError(
            "home_win_probability and expected_home_margin disagree in sign"
        )
    if abs(margin) > 40:
        raise ValueError("expected_home_margin is implausible")
    if not 20 <= projected_total <= 90:
        raise ValueError("projected_total is implausible")
    # The judge's reasons may cite only records and counts the request
    # carries. The rules arm's reasons are generated arithmetic (they quote
    # projected scores) and go unguarded.
    reference = (
        reason_reference_text(build_judge_request(input_payload))
        if judge
        else None
    )
    key_reasons = _validate_reasons(
        response.get("key_reasons"),
        field="key_reasons",
        allowed_labels=allowed_labels,
        policy=policy,
        minimum=2,
        reference_text=reference,
    )
    counterpoints = _validate_reasons(
        response.get("counterpoints", []),
        field="counterpoints",
        allowed_labels=allowed_labels,
        policy=policy,
        minimum=0,
        reference_text=reference,
    )
    discarded = response.get("discarded_considerations", [])
    if not isinstance(discarded, list) or not all(
        isinstance(value, str) and value.strip() for value in discarded
    ):
        raise ValueError(
            "discarded_considerations must contain non-empty strings"
        )
    ensemble = _validate_ensemble(response.get("ensemble")) if judge else None

    away_score, home_score = _scores_from_estimate(
        probability, margin, projected_total
    )
    winner = home_team if probability > 0.5 else away_team
    legs = apply_policy(
        home_win_probability=probability,
        expected_home_margin=margin,
        projected_total=projected_total,
        market=market,
        policy=policy,
        away_team=away_team,
        home_team=home_team,
    )
    side, total = legs["side"], legs["total"]
    side_label, total_label = _leg_label(side), _leg_label(total)
    arm = "judge" if judge else "rules"
    fair = market["fair"]
    thesis = (
        f"God Expert ({arm}): side {side_label} "
        f"{'★' * side['confidence_stars']}; total {total_label} "
        f"{'★' * total['confidence_stars']}. p(home) {probability:.2f} vs "
        f"market {float(fair['home_ml']):.2f}; edges home-cover "
        f"{legs['edges']['home_cover']:+.1%}, over {legs['edges']['over']:+.1%}."
    )
    supporting = [
        f"{_display_label(item['voice'], input_payload, judge=judge)}: "
        f"{item['text']}"
        for item in key_reasons
    ]
    counter = [
        f"{_display_label(item['voice'], input_payload, judge=judge)}: "
        f"{item['text']}"
        for item in counterpoints
    ]
    counter.append(
        f"Market: fair p(home) {float(fair['home_ml']):.3f}, fair p(home "
        f"covers) {float(fair['home_cover']):.3f}, fair p(over) "
        f"{float(fair['over']):.3f}; the book holds "
        f"{float(fair['hold_spread']):.1%} on the spread and "
        f"{float(fair['hold_total']):.1%} on the total."
    )
    movement = market["movement_since_open"]
    price_moves = _price_move_texts(
        movement, away_team=away_team, home_team=home_team
    )
    if (
        movement.get("home_spread") not in (None, 0)
        or movement.get("total") not in (None, 0)
        or price_moves
    ):
        counter.append(
            "Line movement since open: home spread "
            f"{float(movement.get('home_spread') or 0):+g} points, total "
            f"{float(movement.get('total') or 0):+g} points"
            + (f"; {', '.join(price_moves)}" if price_moves else "")
            + "."
        )
    counter.extend(f"Policy: {note}." for note in legs["notes"])
    scoreboard = input_payload["scoreboard"]
    no_signal = []
    if int(scoreboard.get("resolved_games") or 0) == 0:
        no_signal.append(
            "Scoreboard has 0 resolved games; every voice carries weight 1.0 "
            "and no track record."
        )
    elif not input_payload["feature_block"]["weights_active"]:
        no_signal.append(
            f"Scoreboard has {scoreboard['resolved_games']} resolved games, "
            f"below the {policy['weights_min_resolved']}-per-voice bar for "
            "Hedge weights; weights stay 1.0."
        )
    pool_markets = input_payload["feature_block"].get("markets")
    if isinstance(pool_markets, dict):
        for market_name, expectation in (
            ("side", "the market's expected margin and fair probability"),
            ("total", "the market total"),
        ):
            if not pool_markets.get(market_name):
                no_signal.append(
                    f"No voice informs the {market_name} market; that pool is "
                    f"empty and the blend takes {expectation}."
                )
    full_opinion = _render_full_opinion(
        input_payload,
        arm=arm,
        probability=probability,
        margin=margin,
        projected_total=projected_total,
        legs=legs,
        supporting=supporting,
        counter=counter,
        no_signal=no_signal,
        discarded=list(discarded),
        thesis=thesis,
        model=model,
        ensemble=ensemble,
    )
    voice_key = {
        voice["voice_id"]: {
            "expert_name": voice["expert_name"],
            "opinion_id": voice["opinion_id"],
            "model": voice["model"],
            "expert_version": voice["expert_version"],
            "selection_rule": voice["selection_rule"],
        }
        for voice in input_payload["voices"]
    }
    summary = {
        "arm": arm,
        "policy_version": policy["version"],
        "fair": fair,
        "pool": input_payload["feature_block"]["pool"],
        "shrunk": input_payload["feature_block"]["shrunk"],
        "weights": input_payload["feature_block"]["weights"],
        "weights_active": input_payload["feature_block"]["weights_active"],
        "hedge_weights": input_payload["feature_block"].get("hedge_weights"),
        "overlap": input_payload["feature_block"].get("overlap"),
        "markets": input_payload["feature_block"].get("markets"),
        "estimate": {
            "home_win_probability": round(probability, 4),
            "expected_home_margin": round(margin, 2),
            "projected_total": round(projected_total, 2),
            "p_cover_home": legs["p_cover_home"],
            "p_over": legs["p_over"],
        },
        "edges": legs["edges"],
        "voices": voice_key,
        "judge_labels": input_payload["judge_view"]["labels"] if judge else None,
        "scoreboard_resolved_games": scoreboard.get("resolved_games"),
        # The runner's ensemble block for a judge row built from samples.
        "ensemble": ensemble,
    }
    return {
        "predicted_winner": winner,
        "predicted_away_score": int(away_score),
        "predicted_home_score": int(home_score),
        "home_win_probability": round(probability, 4),
        "expected_home_margin": round(margin, 2),
        "confidence_stars": max(
            side["confidence_stars"], total["confidence_stars"]
        ),
        "pick_market": "side_and_total",
        "pick_side": f"{side_label} | {total_label}",
        "thesis": thesis,
        "supporting_factors": supporting,
        "counterarguments": counter,
        "no_signal_factors": no_signal,
        "discarded_considerations": list(discarded),
        "full_opinion": full_opinion,
        "side_pick_json": canonical_json(side),
        "total_pick_json": canonical_json(total),
        "calibration_summary_json": canonical_json(summary),
    }


def _bullets(values: list[str]) -> str:
    return "\n".join(f"- {value}" for value in values) or "- None"


def _render_full_opinion(
    input_payload: dict[str, Any],
    *,
    arm: str,
    probability: float,
    margin: float,
    projected_total: float,
    legs: dict[str, Any],
    supporting: list[str],
    counter: list[str],
    no_signal: list[str],
    discarded: list[str],
    thesis: str,
    model: str,
    ensemble: dict[str, Any] | None = None,
) -> str:
    game = input_payload["game"]
    market = input_payload["market"]
    latest = market["latest"]
    fair = market["fair"]
    feature = input_payload["feature_block"]
    home = game["home_team"]
    away = game["away_team"]
    voice_lines = []
    overlap = feature.get("overlap") or {}
    for voice in input_payload["voices"]:
        legs_note = ""
        if voice["legs"]["side"] or voice["legs"]["total"]:
            side_leg = voice["legs"]["side"] or {"selection": "PASS", "line": None}
            total_leg = voice["legs"]["total"] or {"selection": "PASS", "line": None}
            legs_note = f"; legs {_leg_label(side_leg)} | {_leg_label(total_leg)}"
        pool_note = ""
        if voice.get("markets"):
            pool_note += f"; markets {'+'.join(voice['markets'])}"
        top_overlap = max(
            (float(value) for value in (overlap.get(voice["voice_id"]) or {}).values()),
            default=0.0,
        )
        if top_overlap > 0:
            pool_note += f"; overlap {top_overlap:.2f}"
        voice_lines.append(
            f"{voice['expert_name']} v{voice['expert_version']} · "
            f"{voice['model']}: {voice['predicted_winner']} "
            f"{voice['predicted_away_score']}-{voice['predicted_home_score']}, "
            f"p(home) {float(voice['home_win_probability']):.2f}, margin "
            f"{float(voice['expected_home_margin']):+g}, "
            f"{'★' * int(voice['confidence_stars'])} (cover "
            f"{float(voice['derived']['p_cover_home']):.2f}, over "
            f"{float(voice['derived']['p_over']):.2f}; weight "
            f"{feature['weights'][voice['voice_id']]:g}{pool_note}{legs_note})"
        )
    blend_title = (
        "Blend (rules: pool, then shrink toward the market)"
        if arm == "rules"
        else "Blend (judge estimate)"
    )
    side, total = legs["side"], legs["total"]

    def leg_line(leg: dict[str, Any]) -> str:
        if leg["selection"] == "PASS":
            return f"PASS ★ (best edge {float(leg['edge']):+.1%})"
        return (
            f"{_leg_label(leg)} ({int(leg['price']):+d}) "
            f"{'★' * int(leg['confidence_stars'])} · edge "
            f"{float(leg['edge']):+.1%} · p {float(leg['probability']):.3f} vs "
            f"fair {float(leg['fair_probability']):.3f} · quarter-Kelly "
            f"{float(leg['stake_units']):g}u"
        )

    movement = market["movement_since_open"]
    price_moves = _price_move_texts(movement, away_team=away, home_team=home)
    sections = [
        "Market\n"
        f"- BetOnline latest: {home} {float(latest['home_spread']):+g} "
        f"({int(latest['home_spread_price']):+d}), {away} "
        f"{float(latest['away_spread']):+g} "
        f"({int(latest['away_spread_price']):+d}); ML {away} "
        f"{int(latest['away_moneyline']):+d} / {home} "
        f"{int(latest['home_moneyline']):+d}; total {float(latest['total']):g} "
        f"({int(latest['over_price']):+d}/{int(latest['under_price']):+d})\n"
        f"- Fair after de-vig: p({home}) {float(fair['home_ml']):.3f} · "
        f"p({home} covers) {float(fair['home_cover']):.3f} · p(over) "
        f"{float(fair['over']):.3f}\n"
        f"- Movement since open: home spread {movement.get('home_spread')}, "
        f"total {movement.get('total')}"
        + (f"; {', '.join(price_moves)}" if price_moves else ""),
        f"Voices\n{_bullets(voice_lines)}",
        f"{blend_title}\n- p({home}) {probability:.3f} · margin {margin:+.1f} "
        f"· total {projected_total:.1f}"
        + (f" · model {model}" if model else "")
        + (
            f" · mean of {int(ensemble['valid'])} of {int(ensemble['size'])} "
            "samples"
            if ensemble
            else ""
        )
        + f"\n- Pool before shrink: p({home}) "
        f"{_fmt(feature['pool']['home_win_probability'], '.3f')} · margin "
        f"{_fmt(feature['pool']['expected_home_margin'], '+.1f')} · total "
        f"{_fmt(feature['pool']['projected_total'], '.1f')}"
        + (
            f" (side pool {len(feature['markets']['side'])} voices, total "
            f"pool {len(feature['markets']['total'])})"
            if isinstance(feature.get("markets"), dict)
            else ""
        )
        + f"\n- Edges vs fair: {home} ML {float(legs['edges']['home_ml']):+.1%}, "
        f"{home} cover {float(legs['edges']['home_cover']):+.1%}, over "
        f"{float(legs['edges']['over']):+.1%}",
        f"Side pick\n- {leg_line(side)}",
        f"Total pick\n- {leg_line(total)}",
        f"Why\n{_bullets(supporting)}",
        f"Why it may be wrong\n{_bullets(counter)}",
        f"No signal\n{_bullets(no_signal)}",
        f"Discarded considerations\n{_bullets(discarded)}",
        f"Conclusion\n{thesis}",
    ]
    return "\n\n".join(sections)
