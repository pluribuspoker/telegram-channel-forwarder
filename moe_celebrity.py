"""Deterministic input construction for the Celebrity Consensus Expert."""

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
    return {
        "wins": counts["W"],
        "losses": counts["L"],
        "pushes": counts["P"],
        "games": len(values),
        "chronological_results": "".join(values),
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
        if market == "total" and row_market != "total":
            continue
        direction = str(row.get("direction") or row.get("side") or "")
        if market == "side" and direction not in {away_team, home_team}:
            continue
        if market == "total" and direction not in {"Over", "Under"}:
            continue
        by_name[_name(row.get("celebrity_name"))].add(direction)
    return {
        name: next(iter(values))
        for name, values in by_name.items()
        if len(values) == 1
    }


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
        and (
            str(row.get("market") or "") in {"moneyline", "spread"}
            if market == "side"
            else str(row.get("market") or "") == "total"
        )
        for verdict in [_grade(row, result)]
        if verdict is not None
    }
    return next(iter(verdicts)) if len(verdicts) == 1 else None


def _record_text(record: dict[str, Any]) -> str:
    return (
        f"{record['wins']}-{record['losses']}-{record['pushes']} across "
        f"{record['games']} games"
    )


def _participant_bucket(count: int) -> str:
    return str(count) if count < 4 else "4_plus"


def _calibration(
    rows: list[dict[str, Any]],
    history: list[dict[str, Any]],
    *,
    current_event_id: str,
    current_kickoff: datetime,
    current_names: list[str],
    current_side: dict[str, Any],
    current_total: dict[str, Any],
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

    individual: dict[str, dict[str, Any]] = {}
    for name in current_names:
        side_results = []
        total_results = []
        for event_rows in events.values():
            result = _matching_game(event_rows[0], history)
            if result is None:
                continue
            for row in event_rows:
                if _name(row.get("celebrity_name")) != name:
                    continue
                verdict = _grade(row, result)
                if verdict is None:
                    continue
                if str(row.get("market") or "") in {"moneyline", "spread"}:
                    side_results.append(verdict)
                elif str(row.get("market") or "") == "total":
                    total_results.append(verdict)
        individual[name] = {
            "side": _record(side_results),
            "total": _record(total_results),
        }

    pairwise = []
    for first, second in itertools.combinations(current_names, 2):
        summary = {
            "celebrities": [first, second],
            "side_agreement_games": 0,
            "side_disagreement_games": 0,
            "side_agreement_record": _record([]),
            "total_agreement_games": 0,
            "total_disagreement_games": 0,
            "total_agreement_record": _record([]),
        }
        side_agreed = []
        total_agreed = []
        for event_rows in events.values():
            event_away = str(event_rows[0]["away_team"])
            event_home = str(event_rows[0]["home_team"])
            result = _matching_game(event_rows[0], history)
            if result is None:
                continue
            for market, results in (
                ("side", side_agreed),
                ("total", total_agreed),
            ):
                signals = _signals(
                    event_rows,
                    market=market,
                    away_team=event_away,
                    home_team=event_home,
                )
                if first not in signals or second not in signals:
                    continue
                prefix = f"{market}_"
                if signals[first] == signals[second]:
                    summary[prefix + "agreement_games"] += 1
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
                        results.append(verdict)
                else:
                    summary[prefix + "disagreement_games"] += 1
        summary["side_agreement_record"] = _record(side_agreed)
        summary["total_agreement_record"] = _record(total_agreed)
        pairwise.append(summary)

    current_set = set(current_names)
    exact_side = []
    exact_total = []
    cohort_side = []
    cohort_total = []
    for event_rows in events.values():
        event_names = {
            _name(row.get("celebrity_name")) for row in event_rows
        }
        result = _matching_game(event_rows[0], history)
        if result is None:
            continue
        event_away = str(event_rows[0]["away_team"])
        event_home = str(event_rows[0]["home_team"])
        for market, current_consensus, results in (
            ("side", current_side, cohort_side),
            ("total", current_total, cohort_total),
        ):
            signals = _signals(
                event_rows,
                market=market,
                away_team=event_away,
                home_team=event_home,
            )
            consensus = _consensus(signals)
            if (
                consensus["label"] != current_consensus["label"]
                or _participant_bucket(consensus["participants"])
                != _participant_bucket(current_consensus["participants"])
                or consensus["selection"] is None
            ):
                continue
            verdict = _consensus_verdict(
                event_rows,
                signals=signals,
                selection=consensus["selection"],
                market=market,
                result=result,
            )
            if verdict is not None:
                results.append(verdict)
        if event_names != current_set:
            continue
        for market, results in (
            ("side", exact_side),
            ("total", exact_total),
        ):
            signals = _signals(
                event_rows,
                market=market,
                away_team=event_away,
                home_team=event_home,
            )
            consensus = _consensus(signals)
            selection = consensus["selection"]
            if selection is None:
                continue
            verdict = _consensus_verdict(
                event_rows,
                signals=signals,
                selection=selection,
                market=market,
                result=result,
            )
            if verdict is not None:
                results.append(verdict)
    return {
        "method": (
            "Resolved pre-kickoff NFL celebrity picks before this game's "
            "kickoff. Side, total, and team-total bets are graded from final "
            "scores; player props and other markets remain tracked but "
            "ungraded unless a compatible deterministic result exists. "
            "Group records include only events where every supporting "
            "compatible bet settled to the same verdict."
        ),
        "individual": individual,
        "pairwise": pairwise,
        "exact_active_group": {
            "celebrities": current_names,
            "side": _record(exact_side),
            "total": _record(exact_total),
        },
        "matching_participation_consensus": {
            "side": {
                "participant_bucket": _participant_bucket(
                    current_side["participants"]
                ),
                "consensus_label": current_side["label"],
                **_record(cohort_side),
            },
            "total": {
                "participant_bucket": _participant_bucket(
                    current_total["participants"]
                ),
                "consensus_label": current_total["label"],
                **_record(cohort_total),
            },
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
    """Build one whitelisted, time-aligned celebrity consensus input."""
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
    side = _consensus(
        _signals(
            current,
            market="side",
            away_team=away,
            home_team=home,
        )
    )
    total = _consensus(
        _signals(
            current,
            market="total",
            away_team=away,
            home_team=home,
        )
    )
    calibration = _calibration(
        rows,
        history,
        current_event_id=event_id,
        current_kickoff=kickoff,
        current_names=names,
        current_side=side,
        current_total=total,
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
            "current_side_consensus",
            "side",
            (
                f"Current full-game side signals are {side['label']} across "
                f"{side['participants']} celebrities with votes "
                f"{side['votes']}."
            ),
            supporting_allowed=side["selection"] is not None,
        ),
        _catalog_item(
            "current_total_consensus",
            "total",
            (
                f"Current full-game total signals are {total['label']} across "
                f"{total['participants']} celebrities with votes "
                f"{total['votes']}."
            ),
            supporting_allowed=total["selection"] is not None,
        ),
    ]
    for index, name in enumerate(names, 1):
        records = calibration["individual"][name]
        catalog.extend(
            [
                _catalog_item(
                    f"individual_{index:02d}_side",
                    "side",
                    f"{name}'s resolved NFL side record is "
                    f"{_record_text(records['side'])}.",
                    supporting_allowed=records["side"]["games"] > 0,
                ),
                _catalog_item(
                    f"individual_{index:02d}_total",
                    "total",
                    f"{name}'s resolved NFL total record is "
                    f"{_record_text(records['total'])}.",
                    supporting_allowed=records["total"]["games"] > 0,
                ),
            ]
        )
    for index, pair in enumerate(calibration["pairwise"], 1):
        first, second = pair["celebrities"]
        catalog.extend(
            [
                _catalog_item(
                    f"pair_{index:02d}_side",
                    "side",
                    (
                        f"{first} and {second} agreed on a side in "
                        f"{pair['side_agreement_games']} games and disagreed "
                        f"in {pair['side_disagreement_games']}; their agreed "
                        f"side record is "
                        f"{_record_text(pair['side_agreement_record'])}."
                    ),
                    supporting_allowed=(
                        pair["side_agreement_record"]["games"] > 0
                    ),
                ),
                _catalog_item(
                    f"pair_{index:02d}_total",
                    "total",
                    (
                        f"{first} and {second} agreed on a total in "
                        f"{pair['total_agreement_games']} games and disagreed "
                        f"in {pair['total_disagreement_games']}; their agreed "
                        f"total record is "
                        f"{_record_text(pair['total_agreement_record'])}."
                    ),
                    supporting_allowed=(
                        pair["total_agreement_record"]["games"] > 0
                    ),
                ),
            ]
        )
    exact = calibration["exact_active_group"]
    cohorts = calibration["matching_participation_consensus"]
    catalog.extend(
        [
            _catalog_item(
                "exact_group_side",
                "side",
                "This exact active celebrity group has a resolved side "
                f"consensus record of {_record_text(exact['side'])}.",
                supporting_allowed=exact["side"]["games"] > 0,
            ),
            _catalog_item(
                "exact_group_total",
                "total",
                "This exact active celebrity group has a resolved total "
                f"consensus record of {_record_text(exact['total'])}.",
                supporting_allowed=exact["total"]["games"] > 0,
            ),
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
            _catalog_item(
                "matching_side_participation_consensus",
                "side",
                (
                    "Historical side groups with participant bucket "
                    f"{cohorts['side']['participant_bucket']} and consensus "
                    f"label {cohorts['side']['consensus_label']} have a "
                    f"record of {_record_text(cohorts['side'])}."
                ),
                supporting_allowed=cohorts["side"]["games"] > 0,
            ),
            _catalog_item(
                "matching_total_participation_consensus",
                "total",
                (
                    "Historical total groups with participant bucket "
                    f"{cohorts['total']['participant_bucket']} and consensus "
                    f"label {cohorts['total']['consensus_label']} have a "
                    f"record of {_record_text(cohorts['total'])}."
                ),
                supporting_allowed=cohorts["total"]["games"] > 0,
            ),
        ]
    )
    relevant_games = max(
        (
            item["games"]
            for records in calibration["individual"].values()
            for item in records.values()
        ),
        default=0,
    )
    max_confidence = 2 if len(names) == 1 or relevant_games == 0 else 3
    return {
        "input_profile": "celebrity_consensus",
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
            "side_consensus": side,
            "total_consensus": total,
        },
        "markets": {"current_latest": latest_market},
        "nfl_calibration": calibration,
        "confidence_cap": max_confidence,
        "evidence_catalog": catalog,
    }
