"""Deterministic input for the NFL Disappointment Expert.

The expert sees only the closing BetOnline spread and total and the final
score of every prior regular-season game this season for the two teams, plus
this game's current line. From those it reads which team has been
disappointing against market expectation and which has been the opposite:

- ``ats_margin``: team score - opponent score + team closing spread
  (positive = covered by that many points);
- ``offense_surplus``: points scored - implied team total
  (positive = scored more than the market projected);
- ``defense_surplus``: implied opponent total - points allowed
  (positive = allowed fewer than the market projected).

Implied totals split the closing total by the closing spread, the same
formula as ``moe_god.market_block``'s ``implied_totals``.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Iterable

from nfl_lines import (
    LATEST_AWAY_COLUMN,
    LATEST_HOME_COLUMN,
    LATEST_TOTALS_COLUMN,
    decode_packed_markets,
)

PROFILE = "disappointment"
RECENT_GAMES = 3
MATCH_WINDOW_SECONDS = 6 * 3600
MEASURES = ("ats", "offense", "defense")
_MEAN_FIELDS = {
    "ats": "mean_ats_margin",
    "offense": "mean_offense_surplus",
    "defense": "mean_defense_surplus",
}


def _parse_time(value: Any) -> datetime:
    return datetime.fromisoformat(str(value).replace("Z", "+00:00"))


def _number(value: Any) -> float | None:
    if value in (None, ""):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _round(value: float) -> float:
    rounded = round(float(value), 2)
    return 0.0 if rounded == 0 else rounded


def _plain(value: float) -> int | float:
    return int(value) if float(value).is_integer() else value


def game_line(row: dict[str, Any]) -> dict[str, Any] | None:
    """The latest full-game spread and total of one ``nfl_games`` row."""
    try:
        market = decode_packed_markets(
            str(row.get(LATEST_AWAY_COLUMN) or ""),
            str(row.get(LATEST_HOME_COLUMN) or ""),
            str(row.get(LATEST_TOTALS_COLUMN) or ""),
        )["game"]
    except (IndexError, ValueError):
        # A blank or malformed packed cell is a missing line, not a crash.
        return None
    home_spread = _number(market.get("home_spread"))
    total = _number(market.get("total"))
    if home_spread is None or total is None:
        return None
    return {"home_spread": _plain(home_spread), "total": _plain(total)}


def closing_line(row: dict[str, Any]) -> dict[str, Any] | None:
    """A completed game's closing spread and total.

    The lines fetcher skips started games, so a completed game's latest
    columns are its last pregame capture. A capture stamped at or after
    kickoff is refused rather than trusted.
    """
    captured = str(row.get("latest_captured_at") or "")
    commence = str(row.get("commence_time_utc") or "")
    if captured and commence and _parse_time(captured) >= _parse_time(commence):
        return None
    return game_line(row)


def _matching_line_row(
    final: dict[str, Any],
    games: list[dict[str, Any]],
) -> dict[str, Any] | None:
    kickoff = _parse_time(final["kickoff_utc"])
    matches = [
        row
        for row in games
        if str(row.get("away_team")) == str(final.get("away_team"))
        and str(row.get("home_team")) == str(final.get("home_team"))
        and str(row.get("commence_time_utc") or "")
        and abs(
            (_parse_time(row["commence_time_utc"]) - kickoff).total_seconds()
        )
        <= MATCH_WINDOW_SECONDS
    ]
    return matches[0] if len(matches) == 1 else None


def _team_game(
    team: str,
    final: dict[str, Any],
    line: dict[str, Any],
) -> dict[str, Any]:
    is_home = str(final["home_team"]) == team
    away_score = int(final["away_score"])
    home_score = int(final["home_score"])
    team_score = home_score if is_home else away_score
    opponent_score = away_score if is_home else home_score
    home_spread = float(line["home_spread"])
    total = float(line["total"])
    team_spread = home_spread if is_home else -home_spread
    implied_team = (total - team_spread) / 2
    implied_opponent = total - implied_team
    ats_margin = team_score - opponent_score + team_spread
    total_margin = team_score + opponent_score - total
    return {
        "event_id": str(final.get("event_id") or ""),
        "week": int(final["week"]) if str(final.get("week") or "") else None,
        "opponent": str(final["away_team"] if is_home else final["home_team"]),
        "venue": "home" if is_home else "away",
        "team_score": team_score,
        "opponent_score": opponent_score,
        "closing_spread": _plain(_round(team_spread)),
        "closing_total": _plain(_round(total)),
        "implied_team_total": _round(implied_team),
        "implied_opponent_total": _round(implied_opponent),
        "ats_margin": _round(ats_margin),
        "ats_result": (
            "cover"
            if ats_margin > 0
            else "non_cover"
            if ats_margin < 0
            else "push"
        ),
        "offense_surplus": _round(team_score - implied_team),
        "defense_surplus": _round(implied_opponent - opponent_score),
        "game_total_margin": _round(total_margin),
        "total_result": (
            "over"
            if total_margin > 0
            else "under"
            if total_margin < 0
            else "push"
        ),
    }


def _record(values: list[float]) -> dict[str, int]:
    return {
        "wins": sum(1 for value in values if value > 0),
        "losses": sum(1 for value in values if value < 0),
        "ties": sum(1 for value in values if value == 0),
    }


def _mean(values: list[float]) -> float | None:
    return _round(sum(values) / len(values)) if values else None


def _label(value: float | None) -> str:
    if value is None:
        return "no_games"
    if value > 0:
        return "exceeding"
    if value < 0:
        return "disappointing"
    return "as_expected"


def _profile(labels: dict[str, str], games: int) -> str:
    if not games:
        return "no_games"
    values = set(labels.values())
    if values <= {"disappointing", "as_expected"} and "disappointing" in values:
        return "disappointing"
    if values <= {"exceeding", "as_expected"} and "exceeding" in values:
        return "exceeding"
    if values == {"as_expected"}:
        return "as_expected"
    return "mixed"


def _summary(rows: list[dict[str, Any]]) -> dict[str, Any]:
    ats = [float(row["ats_margin"]) for row in rows]
    offense = [float(row["offense_surplus"]) for row in rows]
    defense = [float(row["defense_surplus"]) for row in rows]
    totals = [float(row["game_total_margin"]) for row in rows]
    summary: dict[str, Any] = {
        "games": len(rows),
        "weeks": [row["week"] for row in rows],
        "ats": _record(ats),
        "mean_ats_margin": _mean(ats),
        "offense_vs_projection": _record(offense),
        "mean_offense_surplus": _mean(offense),
        "defense_vs_projection": _record(defense),
        "mean_defense_surplus": _mean(defense),
        "over_under": _record(totals),
        "mean_game_total_margin": _mean(totals),
    }
    labels = {
        measure: _label(summary[_MEAN_FIELDS[measure]]) for measure in MEASURES
    }
    summary["measure_labels"] = labels
    summary["profile"] = _profile(labels, len(rows))
    return summary


def _team_block(
    team: str,
    finals: list[dict[str, Any]],
    games: list[dict[str, Any]],
) -> dict[str, Any]:
    rows: list[dict[str, Any]] = []
    missing_lines = 0
    for final in finals:
        if team not in (str(final["away_team"]), str(final["home_team"])):
            continue
        line_row = _matching_line_row(final, games)
        line = closing_line(line_row) if line_row is not None else None
        if line is None:
            missing_lines += 1
            continue
        rows.append(_team_game(team, final, line))
    recent = rows[-RECENT_GAMES:]
    return {
        "team": team,
        "season": _summary(rows),
        "last_3": {
            **_summary(recent),
            "same_as_season": len(rows) <= RECENT_GAMES,
        },
        "game_log": {
            (
                f"week_{row['week']}"
                if row["week"] is not None
                else f"event_{row['event_id']}"
            ): row
            for row in rows
        },
        "games_without_closing_line": missing_lines,
    }


def _comparison(
    away: dict[str, Any],
    home: dict[str, Any],
    window: str,
) -> dict[str, Any]:
    away_name, home_name = str(away["team"]), str(home["team"])
    block: dict[str, Any] = {}
    for measure in MEASURES:
        field = _MEAN_FIELDS[measure]
        away_value = away[window][field]
        home_value = home[window][field]
        if away_value is None or home_value is None:
            more = "unavailable"
            difference = None
        else:
            difference = _round(float(away_value) - float(home_value))
            more = (
                away_name
                if difference < 0
                else home_name
                if difference > 0
                else "neither"
            )
        block[measure] = {
            "away_value": away_value,
            "home_value": home_value,
            "away_minus_home": difference,
            "more_disappointing_team": more,
        }
    away_profile = str(away[window]["profile"])
    home_profile = str(home[window]["profile"])
    block["away_profile"] = away_profile
    block["home_profile"] = home_profile
    block["statement"] = (
        f"{away_name} profile {away_profile.replace('_', ' ')}; "
        f"{home_name} profile {home_profile.replace('_', ' ')}."
    )
    return block


def build_disappointment_input(
    game: dict[str, Any],
    games: Iterable[dict[str, Any]],
    current_season_results: Iterable[dict[str, Any]],
) -> dict[str, Any]:
    """One game's market-expectation package for both teams."""
    season = int(game["season"])
    kickoff = _parse_time(game["commence_time_utc"])
    board = list(games)
    finals = sorted(
        (
            row
            for row in current_season_results
            if int(row.get("season") or 0) == season
            and str(row.get("kickoff_utc") or "")
            and _parse_time(row["kickoff_utc"]) < kickoff
            and str(row.get("away_score") or "") != ""
            and str(row.get("home_score") or "") != ""
        ),
        key=lambda row: _parse_time(row["kickoff_utc"]),
    )
    away_team, home_team = str(game["away_team"]), str(game["home_team"])
    away = _team_block(away_team, finals, board)
    home = _team_block(home_team, finals, board)
    current = game_line(game)
    if current is None:
        current_market: dict[str, Any] = {"status": "unavailable"}
    else:
        home_spread = float(current["home_spread"])
        total = float(current["total"])
        current_market = {
            "status": "available",
            "captured_at": str(game.get("latest_captured_at") or ""),
            "home_spread": current["home_spread"],
            "away_spread": _plain(_round(-home_spread)),
            "total": current["total"],
            "implied_away_total": _round((total + home_spread) / 2),
            "implied_home_total": _round((total - home_spread) / 2),
        }
    return {
        "input_profile": PROFILE,
        "game": {
            "event_id": str(game["event_id"]),
            "season": season,
            "season_type": str(game.get("season_type") or ""),
            "week": int(game["week"]) if str(game.get("week") or "") else None,
            "commence_time_utc": str(game["commence_time_utc"]),
            "away_team": away_team,
            "home_team": home_team,
        },
        "current_market": current_market,
        "teams": {"away_team": away, "home_team": home},
        "comparison": {
            "season": _comparison(away, home, "season"),
            "last_3": _comparison(away, home, "last_3"),
        },
        "method": {
            "line_source": (
                "Closing BetOnline full-game spread and total: the last "
                "pregame capture before kickoff."
            ),
            "ats_margin": (
                "Team score minus opponent score plus the team's closing "
                "spread. Positive means the team covered by that many points."
            ),
            "offense_surplus": (
                "Points scored minus the implied team total. Positive means "
                "the offense scored more than the market projected."
            ),
            "defense_surplus": (
                "Implied opponent total minus points allowed. Positive means "
                "the defense allowed fewer points than the market projected."
            ),
            "implied_totals": (
                "Half the closing total, adjusted by half the closing spread."
            ),
            "records": (
                "ats wins/losses/ties = covers/non-covers/pushes. "
                "offense_vs_projection and defense_vs_projection "
                "wins/losses/ties = games beating/missing/matching the "
                "projection. over_under wins/losses/ties = overs/unders/pushes."
            ),
            "labels": (
                "A measure is exceeding when its mean is positive, "
                "disappointing when negative, and as expected at zero. A "
                "profile is disappointing or exceeding when no measure points "
                "the other way, and mixed otherwise."
            ),
        },
        "data_limits": {
            "inputs": (
                "Only this season's prior regular-season closing spreads, "
                "closing totals, and final scores for the two teams, plus "
                "this game's current line."
            ),
            "prohibited": (
                "Injuries, rosters, coaching, news, power ratings, "
                "schedule strength, prior seasons, and other experts."
            ),
            "away_games_without_closing_line": away[
                "games_without_closing_line"
            ],
            "home_games_without_closing_line": home[
                "games_without_closing_line"
            ],
        },
    }
