#!/usr/bin/env python3
"""Build the empirical NFL margin table behind ``aggregator_policy.margin_model``.

Roadmap WP6. From ``data/nfl_lines_history.csv`` (nflverse closing lines; the
close of record for this table is nflverse's, never ESPN's) every
regular-season game with a final score, a closing spread and a closing total
yields two residuals against the market's expectation:

    margin_residual = (home_score - away_score) + home_spread
    total_residual  = (home_score + away_score) - total

``home_spread`` follows the repo convention (negative = home favored), so the
market expects a home margin of ``-home_spread`` and the residual is the
actual margin minus that expectation. Both residuals live on the half-point
lattice.

Bins are one point wide and floor-based: bin ``k`` holds closing lines in
``[k, k + 1)``, so ``home_spread`` -3.5 and -4 share bin -4 and totals 44 and
44.5 share bin 44. Per bin the table stores the game count and the residual
distribution as sorted ``[value, count]`` pairs. A bin is supported at or
above ``min_games`` games; below that ``moe_god`` falls back to the normal
model with the policy sigma.

Two passes produce the committed ``moe/priors/nfl_margins_v1.json``:

1. Calibration. The table is fitted on ``--fit-seasons`` (2016-2024) and
   scored on ``--check-seasons`` (2025), held out. For a grid of thresholds
   ``t`` the empirical survival ``P(r > t) + P(r = t) / 2`` (evaluated exactly
   at lattice points and linearly interpolated between them, exactly as
   ``moe_god.cover_probability`` uses it, with the normal fallback off
   support) and the normal model with the policy sigmas are scored by Brier
   and log loss against the realized residuals; a game whose residual equals
   ``t`` is a push at that threshold and is left out of that threshold's
   score. The report is printed and stored under ``calibration``.
2. Production. The table is rebuilt on ``--seasons`` (all ten) and written
   with sorted keys and fixed formatting, so re-running on the same CSV
   reproduces the file byte for byte.

The lookup arithmetic is ``moe_god``'s (``parse_margin_table``,
``empirical_survival``), so the calibration scores the function production
runs, not a copy of it. Standard library only.
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import sys
from collections import Counter
from pathlib import Path
from typing import Any, Iterable

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from moe_god import (  # noqa: E402
    DEFAULT_POLICY,
    MARGINS_TABLE_PATH,
    empirical_survival,
    normal_cdf,
    parse_margin_table,
)

LINES_CSV = ROOT / "data" / "nfl_lines_history.csv"
NFLVERSE_URL = (
    "https://raw.githubusercontent.com/nflverse/nfldata/master/data/games.csv"
)
SCHEMA_VERSION = 1
TABLE_VERSION = "nfl_margins_v1"
BIN_WIDTH = 1
LATTICE_STEP = 0.5
DEFAULT_MIN_GAMES = 30
DEFAULT_SEASONS = "2016-2025"
DEFAULT_FIT_SEASONS = "2016-2024"
DEFAULT_CHECK_SEASONS = "2025"
GRID_START, GRID_STOP, GRID_STEP = -10.0, 10.0, 0.5
KEY_NUMBERS = {"spread": (-7.0, -3.0, 0.0, 3.0, 7.0), "total": (-3.0, 0.0, 3.0)}
MIN_GAMES_SENSITIVITY = (30, 50, 100)
MARKETS = ("spread", "total")
LOG_CLIP = 1e-6


# --------------------------------------------------------------------------
# Games


def parse_seasons(tokens: Iterable[str]) -> list[int]:
    """``2016-2025``, ``2025``, or a mix of both; sorted and deduplicated."""
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


def half_point(value: float) -> float:
    """Snap to the half-point lattice (residuals never leave it)."""
    return round(float(value) * 2.0) / 2.0


def bin_key(line: float) -> int:
    """Bin ``k`` holds closing lines in ``[k, k + 1)``."""
    return int(math.floor(float(line)))


def game_from_row(row: dict[str, str]) -> dict[str, Any] | None:
    """One usable game or None when a score or a closing line is missing."""
    try:
        away_score = int(float(row["away_score"]))
        home_score = int(float(row["home_score"]))
        home_spread = float(row["home_spread"])
        total = float(row["total"])
    except (KeyError, TypeError, ValueError):
        return None
    return {
        "season": int(row["season"]),
        "week": int(row["week"]),
        "home_spread": home_spread,
        "total": total,
        "margin_residual": half_point((home_score - away_score) + home_spread),
        "total_residual": half_point((home_score + away_score) - total),
    }


def read_games(path: Path, seasons: Iterable[int]) -> list[dict[str, Any]]:
    wanted = set(seasons)
    games = []
    with Path(path).open(encoding="utf-8", newline="") as handle:
        for row in csv.DictReader(handle):
            if int(row["season"]) not in wanted:
                continue
            game = game_from_row(row)
            if game is not None:
                games.append(game)
    return games


# --------------------------------------------------------------------------
# Table


def _number(value: float) -> int | float:
    return int(value) if float(value).is_integer() else float(value)


def build_market_bins(
    games: Iterable[dict[str, Any]], *, line_key: str, residual_key: str
) -> dict[str, dict[str, Any]]:
    counts: dict[int, Counter] = {}
    for game in games:
        counts.setdefault(bin_key(game[line_key]), Counter())[
            half_point(game[residual_key])
        ] += 1
    return {
        str(key): {
            "n": sum(residuals.values()),
            "residuals": [
                [_number(value), int(count)]
                for value, count in sorted(residuals.items())
            ],
        }
        for key, residuals in sorted(counts.items())
    }


def _market_block(bins: dict[str, dict[str, Any]], min_games: int) -> dict[str, Any]:
    supported = [entry for entry in bins.values() if entry["n"] >= min_games]
    return {
        "games": sum(entry["n"] for entry in bins.values()),
        "supported_bins": len(supported),
        "supported_games": sum(entry["n"] for entry in supported),
        "bins": bins,
    }


def build_table(
    games: list[dict[str, Any]],
    *,
    seasons: list[int],
    min_games: int = DEFAULT_MIN_GAMES,
    source_file: str = "data/nfl_lines_history.csv",
) -> dict[str, Any]:
    """The raw table document (what the JSON holds, before ``calibration``)."""
    return {
        "schema_version": SCHEMA_VERSION,
        "version": TABLE_VERSION,
        "source": {
            "file": source_file,
            "provider": "nflverse",
            "close": "nflverse",
            "url": NFLVERSE_URL,
            "note": (
                "nflverse closing spread and total per regular-season game; "
                "ESPN's open/close (data/nfl_open_close.json) is not used."
            ),
        },
        "seasons": list(seasons),
        "games": len(games),
        "bin_width": BIN_WIDTH,
        "bin_convention": (
            "bin k holds closing lines in [k, k + 1); home_spread negative "
            "means the home team is favored"
        ),
        "lattice_step": LATTICE_STEP,
        "min_games": min_games,
        "residuals": {
            "margin": "(home_score - away_score) + home_spread",
            "total": "(home_score + away_score) - total",
        },
        "push_convention": (
            "P(cover) = P(r > t) + P(r = t) / 2 at lattice points, linearly "
            "interpolated between them; bins under min_games fall back to "
            "the normal model"
        ),
        "spread": _market_block(
            build_market_bins(
                games, line_key="home_spread", residual_key="margin_residual"
            ),
            min_games,
        ),
        "total": _market_block(
            build_market_bins(games, line_key="total", residual_key="total_residual"),
            min_games,
        ),
    }


# --------------------------------------------------------------------------
# Calibration


def threshold_grid(
    start: float = GRID_START, stop: float = GRID_STOP, step: float = GRID_STEP
) -> list[float]:
    count = int(round((stop - start) / step))
    return [half_point(start + index * step) for index in range(count + 1)]


def _normal_survival(t: float, sigma: float) -> float:
    return 1.0 - normal_cdf(float(t) / float(sigma))


def _score(probability: float, outcome: float) -> tuple[float, float]:
    p = min(max(float(probability), LOG_CLIP), 1.0 - LOG_CLIP)
    brier = (p - outcome) ** 2
    log_loss = -math.log(p) if outcome else -math.log(1.0 - p)
    return brier, log_loss


class _Tally:
    def __init__(self) -> None:
        self.n = 0
        self.brier = 0.0
        self.log_loss = 0.0

    def add(self, probability: float, outcome: float) -> None:
        brier, log_loss = _score(probability, outcome)
        self.n += 1
        self.brier += brier
        self.log_loss += log_loss

    def summary(self) -> dict[str, Any]:
        if not self.n:
            return {"n": 0, "brier": None, "log_loss": None}
        return {
            "n": self.n,
            "brier": round(self.brier / self.n, 6),
            "log_loss": round(self.log_loss / self.n, 6),
        }


def calibrate_market(
    table: dict[str, Any],
    check_games: list[dict[str, Any]],
    *,
    market: str,
    sigma: float,
    grid: list[float],
) -> dict[str, Any]:
    """Score one market's held-out games on the grid and the key numbers.

    ``table`` is a parsed table (``moe_god.parse_margin_table``). "empirical"
    is the production rule: the table where the bin is supported, the normal
    model elsewhere; "normal" is the normal model everywhere. ``supported``
    restricts both to games whose bin is supported, the clean head-to-head.
    """
    line_key = "home_spread" if market == "spread" else "total"
    residual_key = "margin_residual" if market == "spread" else "total_residual"
    bins = table[market]
    all_tallies = {"empirical": _Tally(), "normal": _Tally()}
    supported_tallies = {"empirical": _Tally(), "normal": _Tally()}
    per_threshold: dict[float, dict[str, _Tally]] = {
        t: {"empirical": _Tally(), "normal": _Tally()} for t in grid
    }
    supported_games = 0
    for game in check_games:
        line = float(game[line_key])
        residual = float(game[residual_key])
        supported = bin_key(line) in bins
        supported_games += int(supported)
        for t in grid:
            if residual == t:
                continue  # a push at this threshold
            outcome = 1.0 if residual > t else 0.0
            normal = _normal_survival(t, sigma)
            empirical = empirical_survival(bins, line, t)
            if empirical is None:
                empirical = normal
            for name, probability in (("empirical", empirical), ("normal", normal)):
                all_tallies[name].add(probability, outcome)
                per_threshold[t][name].add(probability, outcome)
                if supported:
                    supported_tallies[name].add(probability, outcome)
    grid_rows = {
        f"{t:g}": {name: tally.summary() for name, tally in tallies.items()}
        for t, tallies in per_threshold.items()
    }
    return {
        "games": len(check_games),
        "supported_games": supported_games,
        "supported_bin_coverage": (
            round(supported_games / len(check_games), 4) if check_games else None
        ),
        "sigma": sigma,
        "grid_mean": {name: tally.summary() for name, tally in all_tallies.items()},
        "supported_only": {
            name: tally.summary() for name, tally in supported_tallies.items()
        },
        "key_numbers": {f"{t:g}": grid_rows[f"{t:g}"] for t in KEY_NUMBERS[market]},
        "grid": grid_rows,
    }


def calibrate(
    fit_games: list[dict[str, Any]],
    check_games: list[dict[str, Any]],
    *,
    fit_seasons: list[int],
    check_seasons: list[int],
    min_games: int,
    sigma_margin: float,
    sigma_total: float,
    grid: list[float] | None = None,
) -> dict[str, Any]:
    grid = grid if grid is not None else threshold_grid()
    raw = build_table(fit_games, seasons=fit_seasons, min_games=min_games)
    result: dict[str, Any] = {
        "fit_seasons": list(fit_seasons),
        "fit_games": len(fit_games),
        "check_seasons": list(check_seasons),
        "check_games": len(check_games),
        "min_games": min_games,
        "grid": {"start": _number(grid[0]), "stop": _number(grid[-1]), "step": GRID_STEP},
        "scoring": (
            "Brier and log loss of P(residual > t) + P(residual = t) / 2 "
            "against the realized residual; a residual equal to t is a push "
            "and is skipped at that t; 'empirical' falls back to the normal "
            "model off support, 'supported_only' keeps games in supported bins"
        ),
    }
    for market, sigma in (("spread", sigma_margin), ("total", sigma_total)):
        result[market] = calibrate_market(
            parse_margin_table(raw), check_games, market=market, sigma=sigma, grid=grid
        )
    sensitivity = {}
    for candidate in MIN_GAMES_SENSITIVITY:
        parsed = parse_margin_table(
            build_table(fit_games, seasons=fit_seasons, min_games=candidate)
        )
        sensitivity[str(candidate)] = {
            market: {
                key: value
                for key, value in calibrate_market(
                    parsed, check_games, market=market, sigma=sigma, grid=grid
                ).items()
                if key in {"supported_bin_coverage", "grid_mean", "supported_only"}
            }
            for market, sigma in (("spread", sigma_margin), ("total", sigma_total))
        }
    result["min_games_sensitivity"] = sensitivity
    return result


def _fmt(value: Any, spec: str = ".4f") -> str:
    return "—" if value is None else format(float(value), spec)


def format_calibration(calibration: dict[str, Any]) -> list[str]:
    """A self-explanatory report: which model wins, by how much, and where."""
    lines = [
        "Calibration check: table fitted on "
        f"{calibration['fit_seasons'][0]}-{calibration['fit_seasons'][-1]} "
        f"({calibration['fit_games']} games), scored on "
        f"{', '.join(str(s) for s in calibration['check_seasons'])} held out "
        f"({calibration['check_games']} games); min_games "
        f"{calibration['min_games']}; thresholds "
        f"{calibration['grid']['start']}..{calibration['grid']['stop']} by "
        f"{calibration['grid']['step']}. Lower is better."
    ]
    for market in MARKETS:
        block = calibration[market]
        label = "Spread (home margin residual)" if market == "spread" else "Total residual"
        lines.append("")
        lines.append(
            f"{label}: sigma {block['sigma']:g}; {block['supported_games']}/"
            f"{block['games']} held-out games in supported bins "
            f"({float(block['supported_bin_coverage']):.1%})"
        )
        for scope in ("grid_mean", "supported_only"):
            emp, norm = block[scope]["empirical"], block[scope]["normal"]
            title = (
                "all games, normal fallback off support"
                if scope == "grid_mean"
                else "supported-bin games only"
            )
            winner = "tie"
            if emp["brier"] is not None and norm["brier"] is not None:
                if emp["brier"] < norm["brier"]:
                    winner = "empirical"
                elif emp["brier"] > norm["brier"]:
                    winner = "normal"
            lines.append(
                f"  {title:<42} brier empirical={_fmt(emp['brier'])} "
                f"normal={_fmt(norm['brier'])} diff={_fmt((emp['brier'] or 0) - (norm['brier'] or 0), '+.4f')}"
                f" | log loss empirical={_fmt(emp['log_loss'])} "
                f"normal={_fmt(norm['log_loss'])} | n={emp['n']} | {winner}"
            )
        lines.append("  key numbers (Brier, empirical vs normal):")
        for key, row in block["key_numbers"].items():
            emp, norm = row["empirical"], row["normal"]
            diff = (emp["brier"] or 0) - (norm["brier"] or 0)
            lines.append(
                f"    t={key:>3}: empirical={_fmt(emp['brier'])} "
                f"normal={_fmt(norm['brier'])} diff={_fmt(diff, '+.4f')} "
                f"(n={emp['n']})"
            )
    lines.append("")
    lines.append("min_games sensitivity (grid-mean Brier, all games; coverage):")
    for candidate, blocks in calibration["min_games_sensitivity"].items():
        parts = []
        for market in MARKETS:
            emp = blocks[market]["grid_mean"]["empirical"]["brier"]
            norm = blocks[market]["grid_mean"]["normal"]["brier"]
            parts.append(
                f"{market} emp={_fmt(emp)} norm={_fmt(norm)} "
                f"cov={float(blocks[market]['supported_bin_coverage']):.1%}"
            )
        lines.append(f"  min_games={candidate:>3}: " + "; ".join(parts))
    return lines


# --------------------------------------------------------------------------
# Output


def table_text(document: dict[str, Any]) -> str:
    return json.dumps(document, indent=1, sort_keys=True, ensure_ascii=False) + "\n"


def write_table(document: dict[str, Any], path: Path = MARGINS_TABLE_PATH) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(table_text(document), encoding="utf-8", newline="\n")
    tmp.replace(path)


def build_document(
    games_path: Path,
    *,
    seasons: list[int],
    fit_seasons: list[int],
    check_seasons: list[int],
    min_games: int,
    sigma_margin: float,
    sigma_total: float,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """The production table with its calibration block, plus the calibration."""
    fit_games = read_games(games_path, fit_seasons)
    check_games = read_games(games_path, check_seasons)
    calibration = calibrate(
        fit_games,
        check_games,
        fit_seasons=fit_seasons,
        check_seasons=check_seasons,
        min_games=min_games,
        sigma_margin=sigma_margin,
        sigma_total=sigma_total,
    )
    games = read_games(games_path, seasons)
    document = build_table(
        games,
        seasons=seasons,
        min_games=min_games,
        source_file=str(games_path.relative_to(ROOT)).replace("\\", "/")
        if games_path.is_absolute() and ROOT in games_path.parents
        else str(games_path).replace("\\", "/"),
    )
    document["calibration"] = calibration
    return document, calibration


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--games-csv", type=Path, default=LINES_CSV)
    parser.add_argument("--seasons", nargs="+", default=[DEFAULT_SEASONS])
    parser.add_argument("--fit-seasons", nargs="+", default=[DEFAULT_FIT_SEASONS])
    parser.add_argument("--check-seasons", nargs="+", default=[DEFAULT_CHECK_SEASONS])
    parser.add_argument("--min-games", type=int, default=DEFAULT_MIN_GAMES)
    parser.add_argument(
        "--sigma-margin", type=float, default=float(DEFAULT_POLICY["sigma_margin"])
    )
    parser.add_argument(
        "--sigma-total", type=float, default=float(DEFAULT_POLICY["sigma_total"])
    )
    parser.add_argument("--output", type=Path, default=MARGINS_TABLE_PATH)
    parser.add_argument(
        "--dry-run", action="store_true", help="print the report, write nothing"
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.min_games < 1:
        raise SystemExit("--min-games must be at least 1")
    document, calibration = build_document(
        args.games_csv,
        seasons=parse_seasons(args.seasons),
        fit_seasons=parse_seasons(args.fit_seasons),
        check_seasons=parse_seasons(args.check_seasons),
        min_games=args.min_games,
        sigma_margin=args.sigma_margin,
        sigma_total=args.sigma_total,
    )
    print("\n".join(format_calibration(calibration)))
    print()
    for market in MARKETS:
        block = document[market]
        print(
            f"production {market} table: {block['games']} games, "
            f"{len(block['bins'])} bins, {block['supported_bins']} supported "
            f"holding {block['supported_games']} games "
            f"({block['supported_games'] / block['games']:.1%})"
        )
    if args.dry_run:
        print(f"dry run: {args.output} not written")
        return 0
    write_table(document, args.output)
    print(f"wrote {args.output} ({args.output.stat().st_size} bytes)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
