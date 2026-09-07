#!/usr/bin/env python3
"""Fit the Elo rating voice and write ``moe/priors/nfl_elo_v1.json``.

Offline and pure stdlib: reads the committed ``data/nfl_lines_history.csv``
(nflverse regular-season finals, 1999 onward) and

1. warms up from ``BASE_RATING`` over the seasons before ``--fit-seasons``;
2. grid-searches K, home field (``hfa``, in rating points) and the
   between-season regression fraction on the fit seasons, minimizing the
   Brier score of the pregame home-win probability -- first a coarse grid,
   then a fine grid around the coarse optimum; every point replays the whole
   file so the warm-up sees the candidate parameters too;
3. fits ``points_per_elo`` by least squares of the actual margin on the
   adjusted rating gap over the fit seasons;
4. checks the untouched ``--check-season``: the Elo Brier against the
   de-vigged closing moneyline's Brier (target: within 0.01), and the margin
   RMSE against the closing spread's (informational);
5. writes the prior: parameters, seasons, game counts, the check numbers,
   the league scoring rate of the check season (the voice's projected
   total), and the end-of-check-season ratings for all 32 teams.

Re-run after each season ends (``--check-season`` = the season just played)
and commit the new prior; ``moe_rating.preseason_ratings`` refuses to rate a
season the prior does not immediately precede.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from moe_rating import (  # noqa: E402
    BASE_RATING,
    ELO_PRIOR_PATH,
    LINES_CSV_PATH,
    PRIOR_SCHEMA_VERSION,
    brier_score,
    closing_moneyline_brier,
    fit_parameters,
    fit_points_per_elo,
    league_scoring_rate,
    margin_rmse,
    read_lines_csv,
    replay_games,
)
from scripts.fetch_nfl_lines_history import parse_seasons  # noqa: E402

COARSE_K = [float(k) for k in range(10, 41, 2)]
COARSE_HFA = [float(h) for h in range(0, 101, 10)]
COARSE_REGRESSION = [0.2, 0.25, 1.0 / 3.0, 0.4, 0.5]
TARGET_WITHIN = 0.01


def _frange(center: float, radius: float, step: float, *, floor: float = 0.0) -> list[float]:
    values = []
    count = int(round(2 * radius / step))
    for index in range(count + 1):
        value = round(center - radius + index * step, 6)
        if value >= floor:
            values.append(value)
    return values


def fit(
    rows: list[dict[str, str]],
    *,
    fit_seasons: list[int],
    check_season: int,
) -> dict:
    played = [row for row in rows if row["home_score"] != "" and row["away_score"] != ""]
    seasons = sorted({int(row["season"]) for row in played})
    warmup = [season for season in seasons if season < min(fit_seasons)]
    coarse = fit_parameters(
        played,
        fit_seasons=fit_seasons,
        k_grid=COARSE_K,
        hfa_grid=COARSE_HFA,
        regression_grid=COARSE_REGRESSION,
    )
    best = coarse["params"]
    fine = fit_parameters(
        played,
        fit_seasons=fit_seasons,
        k_grid=_frange(best["k"], 2.0, 1.0, floor=1.0),
        hfa_grid=_frange(best["hfa"], 10.0, 2.0),
        regression_grid=sorted({round(best["regression"] + delta, 6) for delta in (-0.05, 0.0, 0.05) if 0 < best["regression"] + delta < 1}),
    )
    params = dict(fine["params"])
    # points_per_elo by least squares on the fit seasons with the fitted gap.
    replay = replay_games(played, params, collect_from=min(fit_seasons))
    slope = fit_points_per_elo(replay["predictions"], fit_seasons)
    params["points_per_elo"] = round(slope, 2) if slope else params["points_per_elo"]
    params["regression"] = round(params["regression"], 6)
    # Final replay with the fitted parameters; the check season is untouched
    # by the fit (nothing above scored it).
    replay = replay_games(played, params, collect_from=min(fit_seasons))
    predictions = replay["predictions"]
    fit_brier, fit_games = brier_score(predictions, fit_seasons)
    check_brier, check_games = brier_score(predictions, [check_season])
    ml_brier, ml_games = closing_moneyline_brier(predictions, [check_season])
    elo_rmse, _ = margin_rmse(predictions, [check_season], source="elo")
    spread_rmse, spread_games = margin_rmse(predictions, [check_season], source="closing_spread")
    if check_brier is None or ml_brier is None:
        raise ValueError(f"Season {check_season} has no games or no moneylines to check")
    check_end = replay_games(
        [row for row in played if int(row["season"]) <= check_season], params
    )
    if check_end["last_season"] != check_season:
        raise ValueError(f"Season {check_season} is not the last played season before the cut")
    ratings = {team: round(value, 4) for team, value in sorted(check_end["ratings"].items())}
    if len(ratings) != 32:
        raise ValueError(f"Expected 32 rated teams, found {len(ratings)}")
    return {
        "schema_version": PRIOR_SCHEMA_VERSION,
        "version": "v1",
        "model": "elo",
        "base_rating": BASE_RATING,
        "parameters": params,
        "warmup_seasons": [min(warmup), max(warmup)] if warmup else [],
        "fit_seasons": [min(fit_seasons), max(fit_seasons)],
        "through_season": check_season,
        "league_scoring_rate": league_scoring_rate(played, check_season),
        "fit": {
            "objective": "brier of the pregame home-win probability on the fit seasons",
            "coarse_grid": {
                "k": COARSE_K,
                "hfa": COARSE_HFA,
                "regression": [round(value, 6) for value in COARSE_REGRESSION],
                "best": {key: (round(value, 6) if key == "regression" else value) for key, value in coarse["params"].items() if key != "points_per_elo"},
                "brier": round(coarse["brier"], 6),
                "evaluated": coarse["evaluated"],
            },
            "fine_grid": {
                "best": {key: (round(value, 6) if key == "regression" else value) for key, value in fine["params"].items() if key != "points_per_elo"},
                "brier": round(fine["brier"], 6),
                "evaluated": fine["evaluated"],
            },
            "points_per_elo": "least squares of the actual margin on the adjusted gap over the fit seasons",
            "fit_seasons": [min(fit_seasons), max(fit_seasons)],
            "fit_brier": round(fit_brier, 6),
            "fit_games": fit_games,
            "check_season": check_season,
            "check_games": check_games,
            "check_elo_brier": round(check_brier, 6),
            "check_closing_ml_brier": round(ml_brier, 6),
            "check_closing_ml_games": ml_games,
            "check_brier_difference": round(check_brier - ml_brier, 6),
            "target_within": TARGET_WITHIN,
            "target_met": bool(check_brier - ml_brier <= TARGET_WITHIN),
            "check_margin_rmse_elo": round(elo_rmse, 4) if elo_rmse is not None else None,
            "check_margin_rmse_closing_spread": round(spread_rmse, 4) if spread_rmse is not None else None,
            "check_margin_games": spread_games,
            "ties_count_half": True,
        },
        "ratings": ratings,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--csv", type=Path, default=LINES_CSV_PATH, help=f"games file (default {LINES_CSV_PATH})")
    parser.add_argument("--out", type=Path, default=ELO_PRIOR_PATH, help=f"prior to write (default {ELO_PRIOR_PATH})")
    parser.add_argument("--fit-seasons", nargs="+", default=["2023-2024"], help="seasons the grid search scores (default 2023-2024)")
    parser.add_argument("--check-season", type=int, default=2025, help="untouched season for the Brier check; its end ratings are the prior's (default 2025)")
    parser.add_argument("--dry-run", action="store_true", help="print the prior; write nothing")
    args = parser.parse_args(argv)
    fit_seasons = parse_seasons(args.fit_seasons)
    if args.check_season <= max(fit_seasons):
        parser.error("--check-season must follow the fit seasons")
    rows = read_lines_csv(args.csv)
    csv_bytes = args.csv.read_bytes()
    prior = fit(rows, fit_seasons=fit_seasons, check_season=args.check_season)
    seasons = sorted({int(row["season"]) for row in rows})
    prior["source"] = {
        "path": str(args.csv.resolve().relative_to(ROOT)).replace("\\", "/") if args.csv.resolve().is_relative_to(ROOT) else str(args.csv),
        "sha256": hashlib.sha256(csv_bytes).hexdigest(),
        "seasons": f"{seasons[0]}-{seasons[-1]}",
        "games": len(rows),
        "played": sum(1 for row in rows if row["home_score"] != ""),
        "origin": "nflverse games.csv via scripts/fetch_nfl_lines_history.py",
    }
    text = json.dumps(prior, indent=2, sort_keys=True) + "\n"
    fit_block = prior["fit"]
    print(
        f"fit {fit_block['fit_seasons']}: K {prior['parameters']['k']:g}, hfa "
        f"{prior['parameters']['hfa']:g}, regression {prior['parameters']['regression']:g}, "
        f"points_per_elo {prior['parameters']['points_per_elo']:g}; Brier "
        f"{fit_block['fit_brier']:.4f} over {fit_block['fit_games']} games "
        f"(coarse {fit_block['coarse_grid']['evaluated']} + fine {fit_block['fine_grid']['evaluated']} replays)"
    )
    print(
        f"check {fit_block['check_season']}: Elo Brier {fit_block['check_elo_brier']:.4f} vs closing "
        f"moneyline {fit_block['check_closing_ml_brier']:.4f} ({fit_block['check_brier_difference']:+.4f}, "
        f"target within {TARGET_WITHIN:g}: {'met' if fit_block['target_met'] else 'NOT met'}); margin RMSE "
        f"{fit_block['check_margin_rmse_elo']} vs closing spread {fit_block['check_margin_rmse_closing_spread']}"
    )
    print(
        f"league scoring rate {prior['league_scoring_rate']['season']}: "
        f"{prior['league_scoring_rate']['mean_total']} over {prior['league_scoring_rate']['games']} games"
    )
    top = sorted(prior["ratings"].items(), key=lambda item: -item[1])[:5]
    print("top ratings: " + ", ".join(f"{team} {value:.0f}" for team, value in top))
    if args.dry_run:
        print(text)
        return 0
    args.out.parent.mkdir(parents=True, exist_ok=True)
    with open(args.out, "w", encoding="utf-8", newline="\n") as handle:
        handle.write(text)
    print(f"wrote {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
