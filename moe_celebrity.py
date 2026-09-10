"""Deterministic input construction for the Celebrity Expert."""

from __future__ import annotations

import hashlib
import itertools
from collections import Counter, defaultdict
from datetime import datetime, timezone
from typing import Any, Iterable

from celebrity_picks import build_celebrity_rows
from nfl_lines import (
    LATEST_AWAY_COLUMN,
    LATEST_HOME_COLUMN,
    LATEST_TOTALS_COLUMN,
    decode_packed_markets,
)

CALIBRATION_MARKETS = ("side", "spread", "moneyline", "total")


def _parse_time(value: Any) -> datetime:
    return datetime.fromisoformat(str(value).replace("Z", "+00:00")).astimezone(
        timezone.utc
    )


def _reference(*parts: Any) -> str:
    value = "\0".join(str(part or "") for part in parts)
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _name(value: Any) -> str:
    return " ".join(str(value or "").split())


def _record(results: Iterable[str]) -> dict[str, Any]:
    values = list(results)
    counts = Counter(values)
    decisions = counts["W"] + counts["L"]
    return {
        "wins": counts["W"],
        "losses": counts["L"],
        "pushes": counts["P"],
        "games": len(values),
        "decisions": decisions,
        "win_rate": round(counts["W"] / decisions, 4) if decisions else None,
    }


def _matching_game(
    row: dict[str, Any],
    history: Iterable[dict[str, Any]],
) -> dict[str, Any] | None:
    kickoff = _parse_time(row["commence_time_utc"])
    matches = [
        game
        for game in history
        if str(game.get("away_team")) == str(row.get("away_team"))
        and str(game.get("home_team")) == str(row.get("home_team"))
        and abs(
            (_parse_time(game["kickoff_utc"]) - kickoff).total_seconds()
        )
        <= 6 * 3600
    ]
    return matches[0] if len(matches) == 1 else None


def _number(value: Any) -> float | None:
    if value in (None, "", "nodata"):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _grade(
    row: dict[str, Any],
    result: dict[str, Any],
) -> str | None:
    if str(row.get("period") or "") != "game":
        return None
    away = str(row["away_team"])
    home = str(row["home_team"])
    away_score = int(result["away_score"])
    home_score = int(result["home_score"])
    market = str(row.get("market") or "")
    direction = str(row.get("direction") or row.get("side") or "")
    line = _number(row.get("line"))
    if market == "moneyline":
        if away_score == home_score:
            return "P"
        winner = away if away_score > home_score else home
        return "W" if direction == winner else "L"
    if market == "spread" and direction in {away, home} and line is not None:
        margin = (
            away_score - home_score
            if direction == away
            else home_score - away_score
        )
        settled = margin + line
        return "W" if settled > 0 else "L" if settled < 0 else "P"
    if market == "total" and direction in {"Over", "Under"} and line is not None:
        total = away_score + home_score
        if total == line:
            return "P"
        won = total > line if direction == "Over" else total < line
        return "W" if won else "L"
    if (
        str(row.get("market_family") or "") == "team_prop"
        and direction in {"Over", "Under"}
        and line is not None
        and str(row.get("subject") or "") in {away, home}
    ):
        score = away_score if row["subject"] == away else home_score
        if score == line:
            return "P"
        won = score > line if direction == "Over" else score < line
        return "W" if won else "L"
    return None


def _enriched_rows(
    celebrity_rows: Iterable[dict[str, Any]],
    leans: Iterable[dict[str, Any]],
) -> list[dict[str, Any]]:
    lean_by_submission = {
        str(row.get("submission_id") or ""): row for row in leans
    }
    enriched = []
    for original in celebrity_rows:
        row = dict(original)
        submission_id = str(row.get("submission_id") or "")
        lean = lean_by_submission.get(submission_id, {})
        if not row.get("commence_time_utc"):
            row["commence_time_utc"] = lean.get("commence_time_utc", "")
        if not row.get("canonical_key"):
            rebuilt = build_celebrity_rows(
                submission={
                    **row,
                    "latest_selected_line": lean.get(
                        "latest_selected_line",
                        "",
                    ),
                    "latest_selected_price": lean.get(
                        "latest_selected_price",
                        "",
                    ),
                    "user_selected_line": lean.get(
                        "user_selected_line",
                        "",
                    ),
                    "user_selected_price": lean.get(
                        "user_selected_price",
                        "",
                    ),
                    "user_terms_source": lean.get(
                        "user_terms_source",
                        "",
                    ),
                    "raw_pick_text": lean.get("lean_text", ""),
                },
                names=[str(row.get("celebrity_name") or "")],
            )[0]
            row.update(rebuilt)
        if row.get("commence_time_utc"):
            enriched.append(row)
    return enriched


def _latest_revisions(
    rows: Iterable[dict[str, Any]],
) -> list[dict[str, Any]]:
    latest: dict[tuple[str, str, str], dict[str, Any]] = {}
    for row in rows:
        key = (
            str(row.get("event_id") or ""),
            _name(row.get("celebrity_name")).casefold(),
            str(row.get("canonical_key") or ""),
        )
        candidate = (
            str(row.get("submitted_at_utc") or ""),
            str(row.get("pick_id") or row.get("submission_id") or ""),
        )
        previous = latest.get(key)
        if previous is None or candidate > (
            str(previous.get("submitted_at_utc") or ""),
            str(previous.get("pick_id") or previous.get("submission_id") or ""),
        ):
            latest[key] = row
    return list(latest.values())


def _signals(
    rows: Iterable[dict[str, Any]],
    *,
    market: str,
    away_team: str,
    home_team: str,
) -> dict[str, str]:
    by_name: dict[str, set[str]] = defaultdict(set)
    for row in rows:
        if str(row.get("period") or "") != "game":
            continue
        row_market = str(row.get("market") or "")
        if market == "side" and row_market not in {"moneyline", "spread"}:
            continue
        if market in {"spread", "moneyline"} and row_market != market:
            continue
        if market == "total" and row_market != "total":
            continue
        direction = str(row.get("direction") or row.get("side") or "")
        if market in {"side", "spread", "moneyline"} and direction not in {
            away_team,
            home_team,
        }:
            continue
        if market == "total" and direction not in {"Over", "Under"}:
            continue
        by_name[_name(row.get("celebrity_name"))].add(direction)
    return {
        name: next(iter(values))
        for name, values in by_name.items()
        if len(values) == 1
    }


def _market_matches(row: dict[str, Any], market: str) -> bool:
    row_market = str(row.get("market") or "")
    if market == "side":
        return row_market in {"moneyline", "spread"}
    return row_market == market


def _consensus(signals: dict[str, str]) -> dict[str, Any]:
    counts = Counter(signals.values())
    if not counts:
        return {
            "participants": 0,
            "votes": {},
            "label": "no_signal",
            "selection": None,
        }
    top = counts.most_common()
    unique_top = len(top) == 1 or top[0][1] > top[1][1]
    if len(signals) == 1:
        label = "single"
    elif len(counts) == 1:
        label = "unanimous"
    elif unique_top:
        label = "majority"
    else:
        label = "split"
    return {
        "participants": len(signals),
        "votes": dict(sorted(counts.items())),
        "label": label,
        "selection": top[0][0] if unique_top else None,
    }


def _consensus_verdict(
    rows: Iterable[dict[str, Any]],
    *,
    signals: dict[str, str],
    selection: str,
    market: str,
    result: dict[str, Any],
) -> str | None:
    verdicts = {
        verdict
        for row in rows
        if _name(row.get("celebrity_name")) in signals
        and signals[_name(row.get("celebrity_name"))] == selection
        and _market_matches(row, market)
        for verdict in [_grade(row, result)]
        if verdict is not None
    }
    return next(iter(verdicts)) if len(verdicts) == 1 else None


def _participant_verdict(
    rows: Iterable[dict[str, Any]],
    *,
    celebrity: str,
    selection: str,
    market: str,
    result: dict[str, Any],
) -> str | None:
    verdicts = {
        verdict
        for row in rows
        if _name(row.get("celebrity_name")) == celebrity
        and str(row.get("direction") or row.get("side") or "") == selection
        and _market_matches(row, market)
        for verdict in [_grade(row, result)]
        if verdict is not None
    }
    return next(iter(verdicts)) if len(verdicts) == 1 else None


def _relative_signals(
    rows: Iterable[dict[str, Any]],
    signals: dict[str, str],
    *,
    market: str,
    away_team: str,
    home_team: str,
) -> dict[str, str]:
    if market == "total":
        return dict(sorted(signals.items()))
    roles = {away_team: "away", home_team: "home"}
    if market == "spread":
        spread_roles: dict[str, set[str]] = defaultdict(set)
        for row in rows:
            name = _name(row.get("celebrity_name"))
            selection = signals.get(name)
            if (
                selection is None
                or not _market_matches(row, "spread")
                or str(row.get("direction") or row.get("side") or "")
                != selection
            ):
                continue
            line = _number(row.get("line"))
            if line is None:
                continue
            price_role = (
                "favorite" if line < 0 else "underdog" if line > 0 else "pickem"
            )
            spread_roles[name].add(f"{roles[selection]}_{price_role}")
        return {
            name: next(iter(values))
            for name, values in sorted(spread_roles.items())
            if len(values) == 1
        }
    return {
        name: roles[selection]
        for name, selection in sorted(signals.items())
    }


def _conditional_lift(
    conditional: dict[str, Any],
    baseline: dict[str, Any],
) -> dict[str, Any]:
    conditional_rate = conditional["win_rate"]
    baseline_rate = baseline["win_rate"]
    return {
        "conditional_games": conditional["games"],
        "conditional_decisions": conditional["decisions"],
        "conditional_win_rate": conditional_rate,
        "baseline_games": baseline["games"],
        "baseline_decisions": baseline["decisions"],
        "baseline_win_rate": baseline_rate,
        "lift": (
            round(conditional_rate - baseline_rate, 4)
            if conditional_rate is not None and baseline_rate is not None
            else None
        ),
    }


def _record_text(record: dict[str, Any]) -> str:
    return (
        f"{record['wins']}-{record['losses']}-{record['pushes']} across "
        f"{record['games']} games"
    )


def _lift_text(lift: dict[str, Any]) -> str:
    value = lift["lift"]
    rendered = "n/a" if value is None else f"{value:+.4f}"
    return (
        f"lift {rendered} from baseline win rate "
        f"{lift['baseline_win_rate']} ({lift['baseline_decisions']} decisions, "
        f"{lift['baseline_games']} games) to conditional win rate "
        f"{lift['conditional_win_rate']} "
        f"({lift['conditional_decisions']} decisions, "
        f"{lift['conditional_games']} games)"
    )


def _calibration(
    rows: list[dict[str, Any]],
    history: list[dict[str, Any]],
    *,
    current_event_id: str,
    current_kickoff: datetime,
    current_names: list[str],
    current_signals: dict[str, dict[str, str]],
    away_team: str,
    home_team: str,
) -> dict[str, Any]:
    events: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        if str(row.get("event_id") or "") == current_event_id:
            continue
        if _parse_time(row["submitted_at_utc"]) >= _parse_time(
            row["commence_time_utc"]
        ):
            continue
        if _parse_time(row["commence_time_utc"]) >= current_kickoff:
            continue
        events[str(row["event_id"])].append(row)

    individual_results = {
        name: {market: [] for market in CALIBRATION_MARKETS}
        for name in current_names
    }
    for event_rows in events.values():
        event_away = str(event_rows[0]["away_team"])
        event_home = str(event_rows[0]["home_team"])
        result = _matching_game(event_rows[0], history)
        if result is None:
            continue
        for market in CALIBRATION_MARKETS:
            signals = _signals(
                event_rows,
                market=market,
                away_team=event_away,
                home_team=event_home,
            )
            for name in current_names:
                if name not in signals:
                    continue
                verdict = _participant_verdict(
                    event_rows,
                    celebrity=name,
                    selection=signals[name],
                    market=market,
                    result=result,
                )
                if verdict is not None:
                    individual_results[name][market].append(verdict)
    individual = {
        name: {
            market: _record(results)
            for market, results in markets.items()
        }
        for name, markets in individual_results.items()
    }

    pairwise = []
    for first, second in itertools.combinations(current_names, 2):
        summary: dict[str, Any] = {"celebrities": [first, second]}
        for market in CALIBRATION_MARKETS:
            summary[market] = {
                "current_relation": "no_comparison",
                "current_selections": {},
                "agreement_games": 0,
                "agreement_record": _record([]),
                "first_record_when_agreeing": _record([]),
                "second_record_when_agreeing": _record([]),
                "agreement_lift_by_celebrity": {},
                "disagreement_games": 0,
                "first_record_when_disagreeing": _record([]),
                "second_record_when_disagreeing": _record([]),
                "first_lift_when_disagreeing": {},
                "second_lift_when_disagreeing": {},
            }
            market_signals = current_signals[market]
            if first in market_signals and second in market_signals:
                summary[market]["current_selections"] = {
                    first: market_signals[first],
                    second: market_signals[second],
                }
                summary[market]["current_relation"] = (
                    "agreement"
                    if market_signals[first] == market_signals[second]
                    else "disagreement"
                )
        pair_results = {
            market: {
                "agreement": [],
                "agreement_first": [],
                "agreement_second": [],
                "first": [],
                "second": [],
            }
            for market in CALIBRATION_MARKETS
        }
        for event_rows in events.values():
            event_away = str(event_rows[0]["away_team"])
            event_home = str(event_rows[0]["home_team"])
            result = _matching_game(event_rows[0], history)
            if result is None:
                continue
            for market in CALIBRATION_MARKETS:
                signals = _signals(
                    event_rows,
                    market=market,
                    away_team=event_away,
                    home_team=event_home,
                )
                if first not in signals or second not in signals:
                    continue
                if signals[first] == signals[second]:
                    summary[market]["agreement_games"] += 1
                    for name, key in (
                        (first, "agreement_first"),
                        (second, "agreement_second"),
                    ):
                        participant_verdict = _participant_verdict(
                            event_rows,
                            celebrity=name,
                            selection=signals[name],
                            market=market,
                            result=result,
                        )
                        if participant_verdict is not None:
                            pair_results[market][key].append(
                                participant_verdict
                            )
                    verdict = _consensus_verdict(
                        event_rows,
                        signals={
                            first: signals[first],
                            second: signals[second],
                        },
                        selection=signals[first],
                        market=market,
                        result=result,
                    )
                    if verdict is not None:
                        pair_results[market]["agreement"].append(verdict)
                else:
                    summary[market]["disagreement_games"] += 1
                    for name, key in ((first, "first"), (second, "second")):
                        verdict = _participant_verdict(
                            event_rows,
                            celebrity=name,
                            selection=signals[name],
                            market=market,
                            result=result,
                        )
                        if verdict is not None:
                            pair_results[market][key].append(verdict)
        for market in CALIBRATION_MARKETS:
            agreement_record = _record(
                pair_results[market]["agreement"]
            )
            first_record = _record(
                pair_results[market]["first"]
            )
            second_record = _record(
                pair_results[market]["second"]
            )
            first_agreement_record = _record(
                pair_results[market]["agreement_first"]
            )
            second_agreement_record = _record(
                pair_results[market]["agreement_second"]
            )
            summary[market]["agreement_record"] = agreement_record
            summary[market][
                "first_record_when_agreeing"
            ] = first_agreement_record
            summary[market][
                "second_record_when_agreeing"
            ] = second_agreement_record
            summary[market]["agreement_lift_by_celebrity"] = {
                first: _conditional_lift(
                    first_agreement_record,
                    individual[first][market],
                ),
                second: _conditional_lift(
                    second_agreement_record,
                    individual[second][market],
                ),
            }
            summary[market]["first_record_when_disagreeing"] = first_record
            summary[market]["second_record_when_disagreeing"] = second_record
            summary[market]["first_lift_when_disagreeing"] = (
                _conditional_lift(first_record, individual[first][market])
            )
            summary[market]["second_lift_when_disagreeing"] = (
                _conditional_lift(second_record, individual[second][market])
            )
        pairwise.append(summary)

    current_patterns = {
        market: _relative_signals(
            (
                row
                for row in rows
                if str(row.get("event_id") or "") == current_event_id
            ),
            current_signals[market],
            market=market,
            away_team=away_team,
            home_team=home_team,
        )
        for market in CALIBRATION_MARKETS
    }
    permutation_results = {
        market: {name: [] for name in pattern}
        for market, pattern in current_patterns.items()
    }
    permutation_matches = {
        market: 0 for market in CALIBRATION_MARKETS
    }
    for event_rows in events.values():
        result = _matching_game(event_rows[0], history)
        if result is None:
            continue
        event_away = str(event_rows[0]["away_team"])
        event_home = str(event_rows[0]["home_team"])
        for market in CALIBRATION_MARKETS:
            current_pattern = current_patterns[market]
            if not current_pattern:
                continue
            signals = _signals(
                event_rows,
                market=market,
                away_team=event_away,
                home_team=event_home,
            )
            historical_pattern = _relative_signals(
                event_rows,
                signals,
                market=market,
                away_team=event_away,
                home_team=event_home,
            )
            if historical_pattern != current_pattern:
                continue
            permutation_matches[market] += 1
            for name in current_pattern:
                verdict = _participant_verdict(
                    event_rows,
                    celebrity=name,
                    selection=signals[name],
                    market=market,
                    result=result,
                )
                if verdict is not None:
                    permutation_results[market][name].append(verdict)
    return {
        "method": (
            "Resolved pre-kickoff NFL celebrity picks before this game's "
            "kickoff. Composite side records preserve team direction across "
            "spread and moneyline only when the same-game verdicts are "
            "compatible; spread and moneyline records remain separate. "
            "Win rates exclude pushes and retain all sample counts. "
            "Conditional lift compares agreement or disagreement performance "
            "with each celebrity's individual baseline. Exact permutations "
            "match identity plus home/away, favorite/underdog, or Over/Under "
            "roles; arbitrary subsets are not mined."
        ),
        "individual": individual,
        "pairwise": pairwise,
        "exact_current_permutation": {
            market: {
                "pattern": current_patterns[market],
                "matching_games": permutation_matches[market],
                "records_by_celebrity": {
                    name: _record(results)
                    for name, results in permutation_results[market].items()
                },
            }
            for market in CALIBRATION_MARKETS
        },
    }


def _catalog_item(
    item_id: str,
    scope: str,
    text: str,
    *,
    supporting_allowed: bool,
) -> dict[str, Any]:
    return {
        "id": item_id,
        "scope": scope,
        "text": text,
        "supporting_allowed": supporting_allowed,
    }


def build_celebrity_input(
    game: dict[str, Any],
    history: list[dict[str, Any]],
    celebrity_rows: list[dict[str, Any]],
    leans: list[dict[str, Any]],
) -> dict[str, Any]:
    """Build one whitelisted, time-aligned celebrity-pattern input."""
    rows = _latest_revisions(
        row
        for row in _enriched_rows(celebrity_rows, leans)
        if _parse_time(row["submitted_at_utc"])
        < _parse_time(row["commence_time_utc"])
    )
    event_id = str(game["event_id"])
    kickoff = _parse_time(game["commence_time_utc"])
    current = [
        row
        for row in rows
        if str(row.get("event_id") or "") == event_id
    ]
    if not current:
        raise ValueError("No pre-kickoff celebrity picks exist for this event")
    away = str(game["away_team"])
    home = str(game["home_team"])
    names = sorted({_name(row["celebrity_name"]) for row in current})
    current_signals = {
        market: _signals(
            current,
            market=market,
            away_team=away,
            home_team=home,
        )
        for market in CALIBRATION_MARKETS
    }
    side = _consensus(current_signals["side"])
    total = _consensus(current_signals["total"])
    calibration = _calibration(
        rows,
        history,
        current_event_id=event_id,
        current_kickoff=kickoff,
        current_names=names,
        current_signals=current_signals,
        away_team=away,
        home_team=home,
    )
    latest_market = decode_packed_markets(
        str(game.get(LATEST_AWAY_COLUMN) or ""),
        str(game.get(LATEST_HOME_COLUMN) or ""),
        str(game.get(LATEST_TOTALS_COLUMN) or ""),
    )["game"]
    catalog = [
        _catalog_item(
            "current_participation",
            "both",
            (
                f"{len(names)} celebrities have {len(current)} active distinct "
                f"bets for this game: {', '.join(names)}."
            ),
            supporting_allowed=False,
        ),
        _catalog_item(
            "current_side_distribution",
            "side",
            (
                f"Current full-game side distribution is {side['label']} across "
                f"{side['participants']} celebrities with votes "
                f"{side['votes']}."
            ),
            supporting_allowed=side["selection"] is not None,
        ),
        _catalog_item(
            "current_total_distribution",
            "total",
            (
                f"Current full-game total distribution is {total['label']} across "
                f"{total['participants']} celebrities with votes "
                f"{total['votes']}."
            ),
            supporting_allowed=total["selection"] is not None,
        ),
    ]
    for index, row in enumerate(
        sorted(
            current,
            key=lambda value: (
                _name(value["celebrity_name"]),
                str(value["canonical_key"]),
            ),
        ),
        1,
    ):
        market = str(row.get("market") or "")
        scope = (
            "side"
            if market in {"moneyline", "spread"}
            and str(row.get("period") or "") == "game"
            else "total"
            if market == "total" and str(row.get("period") or "") == "game"
            else "both"
        )
        catalog.append(
            _catalog_item(
                f"current_pick_{index:02d}",
                scope,
                (
                    f"{_name(row['celebrity_name'])} picked "
                    f"{row.get('selection_text') or row.get('direction')}. "
                    f"Exact stored explanation: {row.get('raw_pick_text') or 'none'}"
                ),
                supporting_allowed=scope in {"side", "total"},
            )
        )
    for index, name in enumerate(names, 1):
        records = calibration["individual"][name]
        for market in CALIBRATION_MARKETS:
            scope = "total" if market == "total" else "side"
            catalog.append(
                _catalog_item(
                    f"individual_{index:02d}_{market}",
                    scope,
                    f"{name}'s resolved NFL {market} record is "
                    f"{_record_text(records[market])}, with "
                    f"{records[market]['decisions']} decisions and win rate "
                    f"{records[market]['win_rate']}.",
                    supporting_allowed=records[market]["decisions"] > 0,
                )
            )
    for index, pair in enumerate(calibration["pairwise"], 1):
        first, second = pair["celebrities"]
        for market in CALIBRATION_MARKETS:
            detail = pair[market]
            scope = "total" if market == "total" else "side"
            catalog.append(
                _catalog_item(
                    f"pair_{index:02d}_{market}",
                    scope,
                    (
                        f"{first} and {second} currently have relation "
                        f"{detail['current_relation']} with selections "
                        f"{detail['current_selections']}. Historically they "
                        f"agreed in {detail['agreement_games']} games; "
                        f"{first} went {_record_text(detail['first_record_when_agreeing'])} "
                        f"({_lift_text(detail['agreement_lift_by_celebrity'][first])}) "
                        f"and {second} went {_record_text(detail['second_record_when_agreeing'])} "
                        f"({_lift_text(detail['agreement_lift_by_celebrity'][second])}). "
                        f"They disagreed in {detail['disagreement_games']} games, "
                        f"when {first} went {_record_text(detail['first_record_when_disagreeing'])} "
                        f"({_lift_text(detail['first_lift_when_disagreeing'])}) "
                        f"and {second} went {_record_text(detail['second_record_when_disagreeing'])} "
                        f"({_lift_text(detail['second_lift_when_disagreeing'])})."
                    ),
                    supporting_allowed=(
                        (
                            detail["first_record_when_agreeing"]["decisions"] > 0
                            or detail["second_record_when_agreeing"]["decisions"] > 0
                        )
                        if detail["current_relation"] == "agreement"
                        else (
                            detail["first_record_when_disagreeing"]["decisions"] > 0
                            or detail["second_record_when_disagreeing"]["decisions"] > 0
                        )
                        if detail["current_relation"] == "disagreement"
                        else False
                    ),
                )
            )
    exact = calibration["exact_current_permutation"]
    catalog.extend(
        [
            _catalog_item(
                "props_and_other",
                "both",
                (
                    f"{sum(row['market_family'] not in {'side', 'total'} for row in current)} "
                    "active bets are props or other markets. They are tracked "
                    "as direct celebrity views but do not directly support a "
                    "game side or total recommendation."
                ),
                supporting_allowed=False,
            ),
        ]
    )
    for market in CALIBRATION_MARKETS:
        permutation = exact[market]
        scope = "total" if market == "total" else "side"
        for index, (name, record) in enumerate(
            permutation["records_by_celebrity"].items(),
            1,
        ):
            catalog.append(
                _catalog_item(
                    f"exact_{market}_permutation_{index:02d}",
                    scope,
                    (
                        f"The current exact {market} permutation is "
                        f"{permutation['pattern']} and occurred in "
                        f"{permutation['matching_games']} historical games; "
                        f"{name}'s bet in that permutation went "
                        f"{_record_text(record)}."
                    ),
                    supporting_allowed=record["decisions"] > 0,
                )
            )
    relevant_decisions = max(
        (
            item["decisions"]
            for records in calibration["individual"].values()
            for item in records.values()
        ),
        default=0,
    )
    max_confidence = (
        2 if len(names) == 1 or relevant_decisions == 0 else 3
    )
    return {
        "input_profile": "celebrity_patterns",
        "game": {
            "event_id": event_id,
            "season": int(game["season"]),
            "week": int(game["week"]) if str(game.get("week") or "") else None,
            "commence_time_utc": str(game["commence_time_utc"]),
            "away_team": away,
            "home_team": home,
        },
        "current_picks": [
            {
                "celebrity": _name(row["celebrity_name"]),
                "pick_reference": _reference(
                    row.get("pick_id"),
                    row.get("submission_id"),
                ),
                "submitted_at_utc": str(row["submitted_at_utc"]),
                "period": str(row.get("period") or ""),
                "market_family": str(row.get("market_family") or ""),
                "market": str(row.get("market") or ""),
                "subject": str(row.get("subject") or ""),
                "stat": str(row.get("stat") or ""),
                "direction": str(row.get("direction") or ""),
                "line": _number(row.get("line")),
                "price": _number(row.get("price")),
                "betonline_line": _number(row.get("betonline_line")),
                "betonline_price": _number(row.get("betonline_price")),
                "line_source": str(row.get("line_source") or ""),
                "selection_text": str(row.get("selection_text") or ""),
                "raw_pick_text": str(row.get("raw_pick_text") or ""),
            }
            for row in sorted(
                current,
                key=lambda value: (
                    _name(value["celebrity_name"]),
                    str(value["canonical_key"]),
                ),
            )
        ],
        "participation": {
            "celebrities": names,
            "celebrity_count": len(names),
            "active_bet_count": len(current),
            "side_distribution": side,
            "total_distribution": total,
        },
        "markets": {"current_latest": latest_market},
        "nfl_calibration": calibration,
        "confidence_cap": max_confidence,
        "evidence_catalog": catalog,
    }
