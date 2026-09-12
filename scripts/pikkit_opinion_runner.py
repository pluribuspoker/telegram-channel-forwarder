#!/usr/bin/env python3
"""Generate due initial and final shadow Pikkit Expert opinions."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import shutil
import sys
import tempfile
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
load_dotenv(ROOT / ".env.local")
load_dotenv(ROOT / ".env")
load_dotenv(Path.home() / ".claude" / "auth.env", override=False)

from moe import approved_opinions, configured_opinion_store, generate_opinion
from moe_pikkit import (
    FINAL_PHASE,
    INITIAL_PHASE,
    build_historical_calibration,
    build_pikkit_input,
    opinion_phase_identity,
)
from nfl_game_history import (
    _competitor,
    _score,
    _team_name,
    fetch_regular_season_events,
)
from nfl_lines import GAME_HEADERS, SHEET_TABS, SNAPSHOT_HEADERS, get_gspread_client
from nfl_pikkit import (
    final_snapshot,
    first_snapshot,
    load_snapshot_rows,
    open_snapshot_worksheet,
    parse_time,
)
from scripts.god_judge_runner import (
    ClaudeHeadlessInvoker,
    _live_create_fn,
    append_runs_log,
)

EXPERT_ID = "pikkit"
MODEL = "claude-opus-4-8"
EFFORT = "max"
BACKEND = "claude_headless"
FINAL_RETRY_CUTOFF = timedelta(hours=1)
INVALID_ATTEMPT_CAP = 2
DEFAULT_RUNS_LOG = ROOT / "logs" / "pikkit_opinion_runs.jsonl"


def _row_key(row: dict[str, Any]) -> tuple[str, str]:
    return (
        str(row.get("generated_at_utc") or ""),
        str(row.get("opinion_id") or ""),
    )


def _phase_rows(
    rows: Iterable[dict[str, Any]],
    event_id: str,
    phase: str,
    selected_snapshot_id: str,
    prior_opinion_id: str = "",
) -> list[dict[str, Any]]:
    found = []
    for row in rows:
        if (
            str(row.get("event_id") or "") != str(event_id)
            or str(row.get("expert_id") or "") != EXPERT_ID
        ):
            continue
        try:
            identity = opinion_phase_identity(row)
        except ValueError:
            continue
        if identity == (phase, selected_snapshot_id, prior_opinion_id):
            found.append(row)
    return found


def _resolved_finals(seasons: Iterable[int]) -> list[dict[str, Any]]:
    finals = []
    for season in sorted(set(seasons)):
        for event in fetch_regular_season_events(season, expected_games=None):
            competitions = event.get("competitions") or []
            if not competitions:
                continue
            competition = competitions[0]
            home = _competitor(competition, "home")
            away = _competitor(competition, "away")
            finals.append(
                {
                    "event_id": str(event.get("id") or ""),
                    "kickoff_utc": parse_time(event["date"]).isoformat(),
                    "away_team": _team_name(away),
                    "home_team": _team_name(home),
                    "away_score": _score(away),
                    "home_score": _score(home),
                }
            )
    return finals


async def run_once(
    *,
    games: Iterable[dict[str, Any]],
    snapshot_rows: Iterable[dict[str, Any]],
    line_rows: Iterable[dict[str, Any]],
    opinion_rows: Iterable[dict[str, Any]],
    finals: Iterable[dict[str, Any]],
    store: Any,
    now: datetime,
    invoker: Any,
    dry_run: bool,
    max_opinions: int,
    target_event_id: str | None = None,
    runs_log: str | Path = DEFAULT_RUNS_LOG,
    work_root: str | Path | None = None,
) -> dict[str, list[dict[str, Any]]]:
    snapshot_rows = list(snapshot_rows)
    line_rows = list(line_rows)
    opinion_rows = list(opinion_rows)
    finals = list(finals)
    approved = approved_opinions(opinion_rows)
    summary: dict[str, list[dict[str, Any]]] = {
        "generated": [],
        "skipped": [],
        "failed": [],
        "stalled": [],
    }
    generated_count = 0
    upcoming = sorted(
        (
            dict(game)
            for game in games
            if str(game.get("status") or "") == "upcoming"
            and (
                target_event_id is None
                or str(game.get("event_id")) == str(target_event_id)
            )
        ),
        key=lambda game: parse_time(game["commence_time_utc"]),
    )
    for game in upcoming:
        if generated_count >= max_opinions:
            break
        event_id = str(game["event_id"])
        first = first_snapshot(snapshot_rows, event_id)
        if first is None:
            summary["skipped"].append(
                {"event_id": event_id, "reason": "first snapshot unavailable"}
            )
            continue
        final = final_snapshot(snapshot_rows, event_id)
        phases: list[tuple[str, dict[str, Any], dict[str, Any] | None]] = []
        initial_candidates = [
            row
            for row in approved
            if str(row.get("event_id") or "") == event_id
            and str(row.get("expert_id") or "") == EXPERT_ID
            and opinion_phase_identity(row)
            == (INITIAL_PHASE, str(first["snapshot_id"]), "")
        ]
        initial = max(initial_candidates, key=_row_key) if initial_candidates else None
        if initial is None:
            phases.append((INITIAL_PHASE, first, None))
        if final is not None and initial is not None:
            phases.append((FINAL_PHASE, final, initial))

        for phase, selected, prior in phases:
            if generated_count >= max_opinions:
                break
            prior_id = "" if prior is None else str(prior["opinion_id"])
            identity_rows = _phase_rows(
                opinion_rows,
                event_id,
                phase,
                str(selected["snapshot_id"]),
                prior_id,
            )
            if any(
                str(row.get("generation_status") or "") == "valid"
                and str(row.get("review_status") or "") == "approved"
                for row in identity_rows
            ):
                continue
            invalid_count = sum(
                str(row.get("generation_status") or "") == "invalid"
                for row in identity_rows
            )
            if invalid_count >= INVALID_ATTEMPT_CAP:
                summary["stalled"].append(
                    {
                        "event_id": event_id,
                        "phase": phase,
                        "reason": "validation attempt cap reached",
                    }
                )
                continue
            if (
                phase == FINAL_PHASE
                and parse_time(game["commence_time_utc"]) - now
                < FINAL_RETRY_CUTOFF
            ):
                summary["skipped"].append(
                    {
                        "event_id": event_id,
                        "phase": phase,
                        "reason": "inside the one-hour final retry cutoff",
                    }
                )
                continue
            try:
                calibration = build_historical_calibration(
                    snapshot_rows=snapshot_rows,
                    line_rows=line_rows,
                    opinion_rows=opinion_rows,
                    finals=finals,
                    as_of=parse_time(game["commence_time_utc"]),
                )
                payload = build_pikkit_input(
                    game,
                    phase=phase,
                    snapshot_rows=snapshot_rows,
                    line_rows=line_rows,
                    initial_opinion=prior,
                    historical_calibration=calibration,
                )
            except ValueError as exc:
                summary["failed"].append(
                    {
                        "event_id": event_id,
                        "phase": phase,
                        "stage": "input",
                        "error": str(exc),
                    }
                )
                continue
            if dry_run:
                summary["generated"].append(
                    {
                        "event_id": event_id,
                        "phase": phase,
                        "dry_run": True,
                    }
                )
                generated_count += 1
                continue
            workdir = Path(
                tempfile.mkdtemp(
                    prefix=f"pikkit-{phase}-",
                    dir=None if work_root is None else str(work_root),
                )
            )
            record: dict[str, Any] = {
                "logged_at_utc": datetime.now(timezone.utc).isoformat(),
                "kind": "pikkit_opinion_call",
                "event_id": event_id,
                "phase": phase,
                "selected_snapshot_id": str(selected["snapshot_id"]),
                "prior_opinion_id": prior_id,
                "model": MODEL,
                "effort": EFFORT,
            }
            started = time.monotonic()
            try:
                call_cwd = workdir / "cwd"
                call_cwd.mkdir(mode=0o700)
                row = await generate_opinion(
                    expert_id=EXPERT_ID,
                    game=game,
                    history=[],
                    input_payload=payload,
                    store=store,
                    model=MODEL,
                    create_fn=_live_create_fn(invoker, str(call_cwd)),
                    generation_backend=BACKEND,
                    generation_effort=EFFORT,
                    repair_attempts=1,
                )
                summary["generated"].append(
                    {
                        "event_id": event_id,
                        "phase": phase,
                        "opinion_id": str(row["opinion_id"]),
                    }
                )
                opinion_rows.append(row)
                approved.append(row)
                generated_count += 1
                record["status"] = "ok"
                record["opinion_id"] = str(row["opinion_id"])
            except ValueError as exc:
                summary["failed"].append(
                    {
                        "event_id": event_id,
                        "phase": phase,
                        "stage": "validation",
                        "error": str(exc),
                    }
                )
                record["status"] = "invalid"
                record["error"] = str(exc)
            except Exception as exc:
                error = f"{type(exc).__name__}: {exc}"
                summary["failed"].append(
                    {
                        "event_id": event_id,
                        "phase": phase,
                        "stage": "generation",
                        "error": error,
                    }
                )
                record["status"] = "error"
                record["error"] = error
            finally:
                shutil.rmtree(workdir, ignore_errors=True)
                record["wall_ms"] = int((time.monotonic() - started) * 1000)
                record.update(getattr(invoker, "last_call", {}) or {})
                append_runs_log(runs_log, record)
    return summary


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--target", help="Limit generation to one NFL event id.")
    parser.add_argument("--max-opinions", type=int, default=3)
    parser.add_argument(
        "--claude-bin", default=os.environ.get("CLAUDE_BIN", "claude")
    )
    parser.add_argument("--isolation", choices=("safe-mode",), default="safe-mode")
    parser.add_argument("--runs-log", default=str(DEFAULT_RUNS_LOG))
    parser.add_argument("--work-root")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.max_opinions < 1:
        raise SystemExit("--max-opinions must be at least 1")
    oauth_token = os.environ.get("CLAUDE_CODE_OAUTH_TOKEN", "")
    if not args.dry_run and not oauth_token:
        raise SystemExit("CLAUDE_CODE_OAUTH_TOKEN is required")
    credentials = os.environ.get("GOOGLE_CREDENTIALS", "")
    sheet_id = os.environ.get("NFL_INTAKE_SHEET_ID", "")
    if not credentials or not sheet_id:
        raise RuntimeError(
            "GOOGLE_CREDENTIALS and NFL_INTAKE_SHEET_ID are required"
        )
    spreadsheet = get_gspread_client(credentials).open_by_key(sheet_id)
    games = spreadsheet.worksheet(SHEET_TABS["games"]).get_all_records(
        expected_headers=GAME_HEADERS,
        numericise_ignore=["all"],
    )
    line_rows = spreadsheet.worksheet(SHEET_TABS["snapshots"]).get_all_records(
        expected_headers=SNAPSHOT_HEADERS,
        numericise_ignore=["all"],
    )
    pikkit_ws = open_snapshot_worksheet(credentials, sheet_id)
    snapshot_rows = load_snapshot_rows(pikkit_ws)
    store = configured_opinion_store()
    opinion_rows = store.list()
    seasons = {
        int(game["season"])
        for game in games
        if str(game.get("season") or "").strip()
    }
    finals = _resolved_finals(seasons)
    invoker = None
    if not args.dry_run:
        invoker = ClaudeHeadlessInvoker(
            args.claude_bin,
            oauth_token=oauth_token,
            isolation=args.isolation,
            model=MODEL,
            effort=EFFORT,
        )
    summary = asyncio.run(
        run_once(
            games=games,
            snapshot_rows=snapshot_rows,
            line_rows=line_rows,
            opinion_rows=opinion_rows,
            finals=finals,
            store=store,
            now=datetime.now(timezone.utc),
            invoker=invoker,
            dry_run=args.dry_run,
            max_opinions=args.max_opinions,
            target_event_id=args.target,
            runs_log=args.runs_log,
            work_root=args.work_root,
        )
    )
    print(
        f"Pikkit opinion runner: {len(summary['generated'])} generated, "
        f"{len(summary['skipped'])} skipped, {len(summary['failed'])} failed, "
        f"{len(summary['stalled'])} stalled"
    )
    for group in ("generated", "failed", "stalled"):
        for item in summary[group]:
            print(group[:-1] + ": " + json.dumps(item, sort_keys=True))
    return 1 if summary["failed"] else 0


if __name__ == "__main__":
    sys.exit(main())
