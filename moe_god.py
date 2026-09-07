"""God Expert aggregator for the NFL mixture of experts.

Two registered experts share this module and one input:

- ``god_rules`` (mode ``aggregator``) is a deterministic gate. Every approved
  expert opinion for the game becomes one voice; voices are pooled with the
  registry weights, the pool is shrunk toward the de-vigged BetOnline market,
  and the shared policy turns the blended probabilities into side and total
  legs. No model is involved anywhere.
- ``god_judge`` (mode ``aggregator_judge``) receives the same arithmetic plus a
  masked, seeded-shuffled view of the voices and returns only probabilities.
  The identical policy turns those probabilities into legs.

Everything with a right answer is computed here. A model only estimates.
"""

from __future__ import annotations

import hashlib
import json
import math
import random
from pathlib import Path
from typing import Any, Iterable

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

AGGREGATOR_PROFILE = "aggregator"
JUDGE_REQUEST_PROFILE = "aggregator_judge_request"
RULES_MODE = "aggregator"
JUDGE_MODE = "aggregator_judge"
AGGREGATOR_MODES = {RULES_MODE, JUDGE_MODE}
DETERMINISTIC_MODEL = "deterministic"
DETERMINISTIC_BACKEND = "deterministic"

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


# --------------------------------------------------------------------------
# Registry and policy


def load_registry() -> dict[str, Any]:
    config = yaml.safe_load(EXPERTS_PATH.read_text(encoding="utf-8"))
    if not isinstance(config, dict) or not isinstance(
        config.get("experts"), dict
    ):
        raise ValueError("moe/experts.yaml must define an experts map")
    return config


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
    expected_home_margin: float, home_spread: float, sigma: float
) -> float:
    """P(home covers): the home side wins when actual margin + spread > 0."""
    return normal_cdf(
        (float(expected_home_margin) + float(home_spread)) / sigma
    )


def over_probability(
    projected_total: float, total_line: float, sigma: float
) -> float:
    return normal_cdf((float(projected_total) - float(total_line)) / sigma)


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
    opening = _market_from_packed(game, prefix="opening")
    latest = _market_from_packed(game, prefix="latest")
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
        "bookmaker": str(game.get("bookmaker") or ""),
        "opening": {
            **{field: opening.get(field) for field in MARKET_FIELDS},
            "captured_at": str(game.get("opening_captured_at") or ""),
        },
        "latest": {
            **{field: latest[field] for field in MARKET_FIELDS},
            "captured_at": str(game.get("latest_captured_at") or ""),
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


def _latest_row(rows: list[dict[str, Any]]) -> dict[str, Any]:
    return max(
        rows,
        key=lambda row: (
            str(row.get("generated_at_utc") or ""),
            str(row.get("opinion_id") or ""),
        ),
    )


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
                    margin, latest["home_spread"], policy["sigma_margin"]
                )
            ),
            "p_over": _round(
                over_probability(
                    projected_total, latest["total"], policy["sigma_total"]
                )
            ),
        },
        "legs": {
            "side": _leg_from_json(row.get("side_pick_json")),
            "total": _leg_from_json(row.get("total_pick_json")),
        },
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
    if not voices:
        raise ValueError("No approved voices exist for this game")
    weights = weighting["weights"]
    total_weight = sum(weights[voice["voice_id"]] for voice in voices)

    def pooled(key: str) -> float:
        return (
            sum(weights[voice["voice_id"]] * float(voice[key]) for voice in voices)
            / total_weight
        )

    def pooled_derived(key: str) -> float:
        return (
            sum(
                weights[voice["voice_id"]] * float(voice["derived"][key])
                for voice in voices
            )
            / total_weight
        )

    def spread(key: str) -> dict[str, float]:
        values = [float(voice[key]) for voice in voices]
        return {
            "min": round(min(values), 4),
            "max": round(max(values), 4),
            "range": round(max(values) - min(values), 4),
        }

    fair = market["fair"]
    latest = market["latest"]
    lam = policy["shrink_lambda"]
    pool_p = pooled("home_win_probability")
    pool_margin = pooled("expected_home_margin")
    pool_total = pooled("projected_total")
    shrunk_p = lam * pool_p + (1 - lam) * float(fair["home_ml"])
    shrunk_margin = lam * pool_margin + (1 - lam) * float(
        market["market_expectation"]["home_margin"]
    )
    shrunk_total = lam * pool_total + (1 - lam) * float(
        market["market_expectation"]["total"]
    )
    home_votes = sum(
        1 for voice in voices if voice["predicted_winner"] == home_team
    )
    return {
        "n_voices": len(voices),
        "weights": {
            voice["voice_id"]: weights[voice["voice_id"]] for voice in voices
        },
        "weights_active": bool(weighting["active"]),
        "pool": {
            "home_win_probability": _round(pool_p),
            "expected_home_margin": _round(pool_margin, 2),
            "projected_total": _round(pool_total, 2),
            "p_cover_home": _round(pooled_derived("p_cover_home")),
            "p_over": _round(pooled_derived("p_over")),
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
                    shrunk_margin, latest["home_spread"], policy["sigma_margin"]
                )
                - float(fair["home_cover"])
            ),
            "over": _round(
                over_probability(
                    shrunk_total, latest["total"], policy["sigma_total"]
                )
                - float(fair["over"])
            ),
        },
        "dispersion": {
            "home_win_probability": spread("home_win_probability"),
            "expected_home_margin": spread("expected_home_margin"),
            "projected_total": spread("projected_total"),
            "home_winner_votes": home_votes,
            "away_winner_votes": len(voices) - home_votes,
        },
    }


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
    }
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
    by_voice = {voice["voice_id"]: voice for voice in input_payload["voices"]}
    policy = input_payload["policy"]
    weights = input_payload["feature_block"]["weights"]
    masked_voices = []
    for label in sorted(labels, key=JUDGE_LABELS.index):
        voice = by_voice[labels[label]]
        masked_voices.append(
            {
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
    return {
        "input_profile": JUDGE_REQUEST_PROFILE,
        "aggregator_input_sha256": sha256_text(canonical_json(input_payload)),
        "seed": input_payload["judge_view"]["seed"],
        "policy": {
            "sigma_margin": policy["sigma_margin"],
            "sigma_total": policy["sigma_total"],
            "shrink_lambda": policy["shrink_lambda"],
            "edge_threshold": policy["edge_threshold"],
        },
        "game": input_payload["game"],
        "market": input_payload["market"],
        "feature_block": feature_block,
        "scoreboard": {
            "as_of": input_payload["scoreboard"]["as_of"],
            "resolved_games": input_payload["scoreboard"]["resolved_games"],
        },
        "voices": masked_voices,
    }


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
    p_cover_home = cover_probability(
        expected_home_margin, latest["home_spread"], policy["sigma_margin"]
    )
    p_over = over_probability(
        projected_total, latest["total"], policy["sigma_total"]
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


def rules_arm_response(input_payload: dict[str, Any]) -> dict[str, Any]:
    feature = input_payload["feature_block"]
    market = input_payload["market"]
    shrunk = feature["shrunk"]
    probability = float(shrunk["home_win_probability"])
    margin = float(shrunk["expected_home_margin"])
    total = float(shrunk["projected_total"])
    notes: list[str] = []
    if abs(probability - 0.5) < 1e-9 or abs(margin) < 1e-9:
        lean_home = float(market["fair"]["home_ml"]) >= 0.5
        probability = 0.505 if lean_home else 0.495
        margin = 0.5 if lean_home else -0.5
        notes.append(
            "Blend sat exactly on the fence; the market favorite breaks the tie."
        )
    elif (probability > 0.5) != (margin > 0):
        margin = 0.5 if probability > 0.5 else -0.5
        notes.append(
            "Pooled probability and pooled margin disagreed in sign; the "
            "margin was clamped to follow the probability."
        )
    pool = feature["pool"]
    weighted = "Hedge-weighted" if feature["weights_active"] else "Equal-weight"
    reasons = [
        {
            "voice": POOL_LABEL,
            "text": (
                f"{weighted} pool of {feature['n_voices']} voices: p(home) "
                f"{float(pool['home_win_probability']):.3f}, margin "
                f"{float(pool['expected_home_margin']):+.1f}, total "
                f"{float(pool['projected_total']):.1f}."
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
    return {
        "home_win_probability": round(probability, 4),
        "expected_home_margin": round(margin, 2),
        "projected_total": round(total, 2),
        "key_reasons": reasons[: input_payload["policy"]["reason_limit"]],
        "counterpoints": [
            {
                "voice": MARKET_LABEL,
                "text": (
                    f"Voices span p(home) {float(dispersion['min']):.2f} to "
                    f"{float(dispersion['max']):.2f}; the blend averages a "
                    "disagreement rather than reporting a consensus."
                ),
            }
        ],
        "discarded_considerations": notes,
    }


# --------------------------------------------------------------------------
# Normalization shared by both arms


def _validate_reasons(
    value: Any,
    *,
    field: str,
    allowed_labels: set[str],
    policy: dict[str, Any],
    minimum: int,
) -> list[dict[str, str]]:
    if not isinstance(value, list):
        raise ValueError(f"{field} must be a list")
    if len(value) < minimum:
        raise ValueError(f"{field} needs at least {minimum} item(s)")
    if len(value) > policy["reason_limit"]:
        raise ValueError(
            f"{field} may hold at most {policy['reason_limit']} items"
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
        normalized.append({"voice": label, "text": text})
    return normalized


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
    key_reasons = _validate_reasons(
        response.get("key_reasons"),
        field="key_reasons",
        allowed_labels=allowed_labels,
        policy=policy,
        minimum=2,
    )
    counterpoints = _validate_reasons(
        response.get("counterpoints", []),
        field="counterpoints",
        allowed_labels=allowed_labels,
        policy=policy,
        minimum=0,
    )
    discarded = response.get("discarded_considerations", [])
    if not isinstance(discarded, list) or not all(
        isinstance(value, str) and value.strip() for value in discarded
    ):
        raise ValueError(
            "discarded_considerations must contain non-empty strings"
        )

    home_score = round((projected_total + margin) / 2)
    away_score = round((projected_total - margin) / 2)
    if home_score == away_score:
        if probability > 0.5:
            home_score += 1
        else:
            away_score += 1
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
) -> str:
    game = input_payload["game"]
    market = input_payload["market"]
    latest = market["latest"]
    fair = market["fair"]
    feature = input_payload["feature_block"]
    home = game["home_team"]
    away = game["away_team"]
    voice_lines = []
    for voice in input_payload["voices"]:
        legs_note = ""
        if voice["legs"]["side"] or voice["legs"]["total"]:
            side_leg = voice["legs"]["side"] or {"selection": "PASS", "line": None}
            total_leg = voice["legs"]["total"] or {"selection": "PASS", "line": None}
            legs_note = f"; legs {_leg_label(side_leg)} | {_leg_label(total_leg)}"
        voice_lines.append(
            f"{voice['expert_name']} v{voice['expert_version']} · "
            f"{voice['model']}: {voice['predicted_winner']} "
            f"{voice['predicted_away_score']}-{voice['predicted_home_score']}, "
            f"p(home) {float(voice['home_win_probability']):.2f}, margin "
            f"{float(voice['expected_home_margin']):+g}, "
            f"{'★' * int(voice['confidence_stars'])} (cover "
            f"{float(voice['derived']['p_cover_home']):.2f}, over "
            f"{float(voice['derived']['p_over']):.2f}; weight "
            f"{feature['weights'][voice['voice_id']]:g}{legs_note})"
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
        + f"\n- Pool before shrink: p({home}) "
        f"{float(feature['pool']['home_win_probability']):.3f} · margin "
        f"{float(feature['pool']['expected_home_margin']):+.1f} · total "
        f"{float(feature['pool']['projected_total']):.1f}"
        f"\n- Edges vs fair: {home} ML {float(legs['edges']['home_ml']):+.1%}, "
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
