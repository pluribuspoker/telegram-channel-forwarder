#!/usr/bin/env python3
"""Headless God Expert judge runner.

``god-judge.timer`` runs this every 30 minutes (at :12 and :42, after the
BetOnline lines fetcher has usually written a fresh capture). For each
upcoming game whose committee is complete -- one approved, hash-verified row
for every enabled non-aggregator expert -- it:

1. builds the aggregator input and the masked judge request into a fresh
   temp directory (``input.json``, ``request.json``, and an EMPTY ``cwd``);
2. persists the rules arm on that exact input, unless a valid ``god_rules``
   row already carries the same committee key;
3. runs one ``claude -p`` call: Fable 5.1 at max effort, every tool
   disabled, the registered judge prompt as the whole system prompt, the
   request on stdin, from the empty directory, in an environment that holds
   no sheet credentials and no API key; the JSON result envelope is captured;
4. persists the judge row through ``generate_opinion`` on the same input
   (backend ``claude_headless``); a response that fails validation persists
   as an invalid audit row like any other response;
5. deletes the temp directory and DMs the reviewer through the watchdog bot
   that new pending rows exist. The runner never approves anything.

Dedupe is by committee key (``moe_god.committee_key``: the sorted voice
opinion ids plus the latest full-game lines and prices, never the capture
timestamp). A game is skipped inside two hours of kickoff, when its
committee is incomplete, when a valid judge row that the reviewer has not
rejected already carries the current key, or when two invalid judge rows
carry it (the judge failed twice on this committee; the reviewer is told once
per invocation). A rejected judge row does not block: rejection is the
reviewer asking for a fresh run. At most ``--max-games``
games run per invocation. Every judge call bills the Claude Code
subscription, so a failed call is logged and DMed, never retried; the next
timer slot tries again.

Each call appends one JSON line to ``logs/god_judge_runs.jsonl``
(``GOD_JUDGE_RUNS_LOG``) carrying the envelope's ``duration_ms``,
``total_cost_usd``, ``usage`` and ``num_turns``, so weekly subscription
usage can be measured.

``--dry-run`` does everything except the two persists, the claude call and
the DM, and prints what it would do. The exit status is 0 when there was
nothing to do or every failure was a logged judge call, 1 only on an
unexpected exception.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Callable, Iterable
from zoneinfo import ZoneInfo

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
load_dotenv(ROOT / ".env.local")
load_dotenv(ROOT / ".env")

from moe import (
    MoeOpinionStore,
    approved_opinions,
    configured_opinion_store,
    generate_opinion,
    load_expert,
)
from moe_ak import _parse_time
from moe_god import (
    AGGREGATOR_MODES,
    DETERMINISTIC_BACKEND,
    aggregator_policy,
    build_aggregator_input,
    build_judge_request,
    canonical_json,
    load_registry,
    select_voice_rows,
    sha256_text,
)
from nfl_game_history import GAME_HISTORY_HEADERS, GAME_HISTORY_TAB
from nfl_lines import GAME_HEADERS, SNAPSHOT_HEADERS, get_gspread_client
from scripts.generate_moe_opinion import current_season_finals

ET = ZoneInfo("America/New_York")
RULES_EXPERT_ID = "god_rules"
JUDGE_EXPERT_ID = "god_judge"
JUDGE_MODEL = "claude-fable-5-1"
JUDGE_EFFORT = "max"
JUDGE_BACKEND = "claude_headless"
KICKOFF_CUTOFF = timedelta(hours=2)
DEFAULT_MAX_GAMES = 3
INVALID_ATTEMPT_CAP = 2
CLAUDE_TIMEOUT_SECONDS = 900
DEFAULT_CLAUDE_BIN = "/home/forwarder/.npm-global/bin/claude"
DEFAULT_RUNS_LOG = ROOT / "logs" / "god_judge_runs.jsonl"
RESPONSE_INSTRUCTION = "Return exactly one JSON object and nothing else."
# --safe-mode disables every customization (CLAUDE.md, skills, plugins, hooks,
# MCP servers) while auth works normally, so the subscription OAuth token is
# honored; it is the default. --bare goes further (no keychain reads, no
# background traffic) but its help text (2.1.263) says OAuth is never read in
# that mode, so it only suits an ANTHROPIC_API_KEY setup. Switch with
# GOD_JUDGE_CLAUDE_ISOLATION.
ISOLATION_FLAGS: dict[str, list[str]] = {
    "bare": ["--bare"],
    "safe-mode": ["--safe-mode"],
}
ENVELOPE_FIELDS = (
    "subtype",
    "duration_ms",
    "duration_api_ms",
    "num_turns",
    "total_cost_usd",
    "usage",
    "session_id",
)

Invoker = Callable[[str, str, str], str]
Notify = Callable[[str], Any]


class JudgeCallError(RuntimeError):
    """The headless claude call produced no usable response."""


class ClaudeHeadlessInvoker:
    """One ``claude -p`` judge call; the envelope's metadata lands in ``last_call``."""

    def __init__(
        self,
        claude_bin: str | Path,
        *,
        oauth_token: str,
        isolation: str = "safe-mode",
        timeout: float = CLAUDE_TIMEOUT_SECONDS,
        path: str | None = None,
        home: str | None = None,
    ) -> None:
        if not oauth_token:
            raise ValueError(
                "CLAUDE_CODE_OAUTH_TOKEN is required to run the judge"
            )
        if isolation not in ISOLATION_FLAGS:
            raise ValueError(f"Unknown claude isolation mode: {isolation}")
        self.claude_bin = str(claude_bin)
        self.oauth_token = oauth_token
        self.isolation = isolation
        self.timeout = timeout
        self.path = (
            path
            if path is not None
            else os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin")
        )
        self.home = (
            home if home is not None else os.environ.get("HOME", str(Path.home()))
        )
        self.last_call: dict[str, Any] = {}

    def command(self, system_prompt: str) -> list[str]:
        return [
            self.claude_bin,
            "-p",
            *ISOLATION_FLAGS[self.isolation],
            "--model",
            JUDGE_MODEL,
            "--effort",
            JUDGE_EFFORT,
            "--tools",
            "",
            "--strict-mcp-config",
            "--no-session-persistence",
            "--output-format",
            "json",
            "--system-prompt",
            system_prompt,
        ]

    def environment(self) -> dict[str, str]:
        """Only what the CLI needs: no sheet credentials, no API key."""
        return {
            "PATH": self.path,
            "HOME": self.home,
            "TERM": "dumb",
            "LANG": "C.UTF-8",
            "CLAUDE_CODE_OAUTH_TOKEN": self.oauth_token,
            "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1",
        }

    def __call__(self, system_prompt: str, user_message: str, cwd: str) -> str:
        started = time.monotonic()
        self.last_call = {
            "claude_bin": self.claude_bin,
            "isolation": self.isolation,
        }
        try:
            completed = subprocess.run(
                self.command(system_prompt),
                input=user_message,
                cwd=cwd,
                env=self.environment(),
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=self.timeout,
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            self.last_call["wall_ms"] = int((time.monotonic() - started) * 1000)
            raise JudgeCallError(
                f"claude timed out after {self.timeout:g} s"
            ) from exc
        self.last_call["wall_ms"] = int((time.monotonic() - started) * 1000)
        self.last_call["exit_code"] = completed.returncode
        stderr_tail = completed.stderr.strip()[-1000:]
        if stderr_tail:
            self.last_call["stderr_tail"] = stderr_tail
        if completed.returncode != 0:
            raise JudgeCallError(
                f"claude exited {completed.returncode}: "
                f"{stderr_tail or 'no stderr'}"
            )
        try:
            envelope = json.loads(completed.stdout)
        except json.JSONDecodeError as exc:
            raise JudgeCallError(
                "claude printed no JSON result envelope"
            ) from exc
        if not isinstance(envelope, dict):
            raise JudgeCallError("claude's result envelope is not an object")
        for field in ENVELOPE_FIELDS:
            if field in envelope:
                self.last_call[field] = envelope[field]
        result = envelope.get("result")
        if envelope.get("is_error"):
            detail = str(result or envelope.get("subtype") or "")[:500]
            raise JudgeCallError(f"claude reported an error: {detail}")
        if not isinstance(result, str) or not result.strip():
            raise JudgeCallError("claude returned an empty result")
        return result


def judge_user_message(request: dict[str, Any]) -> str:
    """What the model reads: the request as --show-input prints it, then the ask."""
    return (
        json.dumps(request, indent=2, sort_keys=True)
        + "\n"
        + RESPONSE_INSTRUCTION
        + "\n"
    )


def committee_experts(registry: dict[str, Any]) -> list[str]:
    """Every enabled non-aggregator expert; each needs an approved row."""
    experts = registry["experts"]
    return [
        expert_id
        for expert_id in sorted(experts)
        if isinstance(experts[expert_id], dict)
        and experts[expert_id].get("enabled")
        and str(experts[expert_id].get("mode") or "") not in AGGREGATOR_MODES
    ]


def row_committee_key(row: dict[str, Any]) -> str | None:
    """The committee key persisted inside a row's input_json, if any.

    Both arms carry it at the top level: the rules row's input is the full
    aggregator input, the judge row's is the request derived from it.
    """
    raw = row.get("input_json")
    if not raw:
        return None
    try:
        parsed = json.loads(raw) if isinstance(raw, str) else raw
    except (TypeError, ValueError):
        return None
    if not isinstance(parsed, dict):
        return None
    key = parsed.get("committee_key")
    return str(key) if key else None


def _kickoff_et(game: dict[str, Any]) -> str:
    kickoff = _parse_time(game["commence_time_utc"]).astimezone(ET)
    clock = kickoff.strftime("%I:%M %p").lstrip("0")
    return f"{kickoff:%a %b} {kickoff.day} {clock} ET"


def describe_game(game: dict[str, Any]) -> str:
    return f"{game['away_team']} @ {game['home_team']} ({_kickoff_et(game)})"


def pending_message(
    game: dict[str, Any], *, rules_id: str, judge_id: str
) -> str:
    lines = [
        f"pickbot: new God Expert rows pending for {describe_game(game)}: "
        f"rules {rules_id}, judge {judge_id}"
    ]
    for opinion_id in (rules_id, judge_id):
        if opinion_id != "existing":
            lines.append(
                "python scripts/review_moe_opinion.py "
                f"--opinion-id {opinion_id} --status approved "
                "--reviewed-by <you>"
            )
    return "\n".join(lines)


def append_runs_log(path: str | Path, record: dict[str, Any]) -> None:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    with target.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, sort_keys=True, ensure_ascii=False) + "\n")


def send_watchdog_dm(text: str) -> bool:
    """DM the operator through the watchdog bot (deploy/mem_watchdog.py's send)."""
    token = os.environ.get("WATCHDOG_BOT_TOKEN", "")
    uid = os.environ.get("WATCHDOG_USER_ID", "")
    if not token or not uid:
        print("WATCHDOG_BOT_TOKEN / WATCHDOG_USER_ID not set", file=sys.stderr)
        return False
    data = urllib.parse.urlencode({"chat_id": uid, "text": text}).encode()
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    try:
        with urllib.request.urlopen(
            urllib.request.Request(url, data=data), timeout=20
        ) as response:
            return response.status == 200
    except Exception as exc:
        print(f"send failed: {exc}", file=sys.stderr)
        return False


async def run_once(
    *,
    games: Iterable[dict[str, Any]],
    opinion_rows: Iterable[dict[str, Any]],
    finals: Iterable[dict[str, Any]],
    snapshots: Iterable[dict[str, Any]],
    store: MoeOpinionStore,
    registry: dict[str, Any],
    policy: dict[str, Any],
    now: datetime,
    claude_invoker: Invoker,
    notify: Notify,
    max_games: int = DEFAULT_MAX_GAMES,
    dry_run: bool = False,
    work_root: str | Path | None = None,
    runs_log_path: str | Path | None = None,
) -> dict[str, list[dict[str, Any]]]:
    """One pass over the upcoming slate. Every side effect arrives as an argument."""
    opinion_rows = list(opinion_rows)
    finals = list(finals)
    snapshots = list(snapshots)
    approved = approved_opinions(opinion_rows)
    required = committee_experts(registry)
    judge_prompt = load_expert(JUDGE_EXPERT_ID)["prompt_text"]
    runs_log = Path(runs_log_path) if runs_log_path else DEFAULT_RUNS_LOG
    summary: dict[str, list[dict[str, Any]]] = {
        "attempted": [],
        "skipped": [],
        "failed": [],
        "stalled": [],
    }
    prefix = "dry run: " if dry_run else ""

    def skip(game: dict[str, Any], reason: str) -> None:
        print(f"{prefix}skip {describe_game(game)}: {reason}")
        summary["skipped"].append(
            {"event_id": str(game["event_id"]), "reason": reason}
        )

    def rows_for(event_id: str, expert_id: str) -> list[dict[str, Any]]:
        return [
            row
            for row in opinion_rows
            if str(row.get("event_id")) == event_id
            and str(row.get("expert_id")) == expert_id
        ]

    upcoming = sorted(
        (game for game in games if str(game.get("status") or "") == "upcoming"),
        key=lambda game: _parse_time(game["commence_time_utc"]),
    )
    for game in upcoming:
        if len(summary["attempted"]) >= max_games:
            print(
                f"{prefix}max games ({max_games}) reached; the rest wait for "
                "the next pass"
            )
            break
        event_id = str(game["event_id"])
        if _parse_time(game["commence_time_utc"]) - now < KICKOFF_CUTOFF:
            skip(game, "inside the two-hour kickoff cutoff")
            continue
        selected = select_voice_rows(
            approved, event_id=event_id, registry=registry, policy=policy
        )
        present = {item[0] for item in selected}
        missing = [expert_id for expert_id in required if expert_id not in present]
        if missing:
            skip(
                game,
                "committee incomplete, no approved row for "
                + ", ".join(missing),
            )
            continue
        try:
            payload = build_aggregator_input(
                game,
                approved_opinions=approved,
                finals=finals,
                snapshots=snapshots,
                registry=registry,
                policy=policy,
            )
        except ValueError as exc:
            skip(game, f"input could not be built: {exc}")
            continue
        key = str(payload["committee_key"])
        judge_statuses = [
            str(row.get("generation_status") or "")
            for row in rows_for(event_id, JUDGE_EXPERT_ID)
            if row_committee_key(row) == key
            # A rejected row is the reviewer asking for a fresh run.
            and str(row.get("review_status") or "") != "rejected"
        ]
        if "valid" in judge_statuses:
            skip(game, f"a valid judge row already carries committee {key[:12]}")
            continue
        if judge_statuses.count("invalid") >= INVALID_ATTEMPT_CAP:
            summary["stalled"].append(
                {
                    "event_id": event_id,
                    "committee_key": key,
                    "game": describe_game(game),
                }
            )
            skip(
                game,
                f"judge failed validation {INVALID_ATTEMPT_CAP} times on "
                f"committee {key[:12]}; waiting for the committee to change",
            )
            continue
        existing_rules = [
            row
            for row in rows_for(event_id, RULES_EXPERT_ID)
            if str(row.get("generation_status") or "") == "valid"
            and row_committee_key(row) == key
        ]
        request = build_judge_request(payload)
        attempt: dict[str, Any] = {
            "event_id": event_id,
            "game": describe_game(game),
            "committee_key": key,
            "dry_run": dry_run,
        }
        summary["attempted"].append(attempt)
        workdir = Path(
            tempfile.mkdtemp(
                prefix="god-judge-",
                dir=None if work_root is None else str(work_root),
            )
        )
        try:
            (workdir / "input.json").write_text(
                json.dumps(payload, indent=2, sort_keys=True) + "\n",
                encoding="utf-8",
            )
            (workdir / "request.json").write_text(
                json.dumps(request, indent=2, sort_keys=True) + "\n",
                encoding="utf-8",
            )
            call_cwd = workdir / "cwd"
            call_cwd.mkdir(mode=0o700)
            rules_state = "existing" if existing_rules else "new"
            if dry_run:
                print(
                    f"dry run: {describe_game(game)} event {event_id} committee "
                    f"{key}: would persist the rules row ({rules_state}), call "
                    "claude, persist the judge row, and DM the reviewer"
                )
                continue
            if existing_rules:
                rules_id = "existing"
            else:
                try:
                    rules_row = await generate_opinion(
                        expert_id=RULES_EXPERT_ID,
                        game=game,
                        history=[],
                        input_payload=payload,
                        opinions=opinion_rows,
                        store=store,
                        generation_backend=DETERMINISTIC_BACKEND,
                    )
                except ValueError as exc:
                    print(
                        f"{describe_game(game)}: rules arm failed validation "
                        f"({exc}); judge not run"
                    )
                    summary["failed"].append(
                        {**attempt, "stage": "rules", "error": str(exc)}
                    )
                    continue
                rules_id = str(rules_row["opinion_id"])
                attempt["rules_opinion_id"] = rules_id
            record: dict[str, Any] = {
                "logged_at_utc": datetime.now(timezone.utc).isoformat(),
                "kind": "claude_call",
                "event_id": event_id,
                "away_team": str(game["away_team"]),
                "home_team": str(game["home_team"]),
                "committee_key": key,
            }
            started = time.monotonic()
            try:
                response_text: str | None = claude_invoker(
                    judge_prompt, judge_user_message(request), str(call_cwd)
                )
                record["status"] = "ok"
            except Exception as exc:
                response_text = None
                record["status"] = "error"
                record["error"] = f"{type(exc).__name__}: {exc}"
            record["wall_ms"] = int((time.monotonic() - started) * 1000)
            record.update(getattr(claude_invoker, "last_call", None) or {})
            append_runs_log(runs_log, record)
            if response_text is None:
                print(
                    f"{describe_game(game)}: judge call failed "
                    f"({record['error']}); no row persisted"
                )
                notify(
                    "pickbot: God Expert judge call failed for "
                    f"{describe_game(game)}: {record['error']}"
                )
                summary["failed"].append(
                    {**attempt, "stage": "claude", "error": record["error"]}
                )
                continue

            async def create_fn(**_kwargs: Any) -> Any:
                return SimpleNamespace(
                    content=[SimpleNamespace(text=response_text)]
                )

            try:
                judge_row = await generate_opinion(
                    expert_id=JUDGE_EXPERT_ID,
                    game=game,
                    history=[],
                    input_payload=payload,
                    opinions=opinion_rows,
                    store=store,
                    create_fn=create_fn,
                    generation_backend=JUDGE_BACKEND,
                    generation_effort=JUDGE_EFFORT,
                    model=JUDGE_MODEL,
                    expected_input_sha256=sha256_text(canonical_json(request)),
                )
            except ValueError as exc:
                print(
                    f"{describe_game(game)}: judge response failed validation "
                    f"({exc}); audit row persisted"
                )
                summary["failed"].append(
                    {**attempt, "stage": "judge_validation", "error": str(exc)}
                )
                continue
            judge_id = str(judge_row["opinion_id"])
            attempt["judge_opinion_id"] = judge_id
            print(
                f"{describe_game(game)}: persisted rules {rules_id}, judge "
                f"{judge_id} (committee {key[:12]})"
            )
            notify(pending_message(game, rules_id=rules_id, judge_id=judge_id))
        finally:
            shutil.rmtree(workdir, ignore_errors=True)
    if summary["stalled"]:
        text = (
            "pickbot: God Expert judge failed validation twice on the current "
            "committee; no further attempts until a voice or the lines "
            "change:\n"
            + "\n".join(
                f"- {item['game']} (committee {item['committee_key'][:12]})"
                for item in summary["stalled"]
            )
        )
        if dry_run:
            print(f"dry run: would DM: {text}")
        else:
            notify(text)
    return summary


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--max-games",
        type=int,
        default=int(os.environ.get("GOD_JUDGE_MAX_GAMES") or DEFAULT_MAX_GAMES),
        help="Games to process per invocation (default 3).",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Build inputs and print the plan; persist nothing, call nothing.",
    )
    parser.add_argument(
        "--claude-bin",
        default=os.environ.get("GOD_JUDGE_CLAUDE_BIN") or DEFAULT_CLAUDE_BIN,
        help="Claude Code binary (GOD_JUDGE_CLAUDE_BIN).",
    )
    parser.add_argument(
        "--isolation",
        choices=sorted(ISOLATION_FLAGS),
        default=os.environ.get("GOD_JUDGE_CLAUDE_ISOLATION") or "safe-mode",
        help="How the CLI is isolated from hooks, plugins and CLAUDE.md "
        "(GOD_JUDGE_CLAUDE_ISOLATION; default safe-mode, which keeps the "
        "OAuth token usable; bare never reads OAuth).",
    )
    parser.add_argument(
        "--runs-log",
        type=Path,
        default=Path(os.environ.get("GOD_JUDGE_RUNS_LOG") or DEFAULT_RUNS_LOG),
        help="JSONL usage log, one line per claude call (GOD_JUDGE_RUNS_LOG).",
    )
    parser.add_argument(
        "--work-root",
        type=Path,
        default=(
            Path(os.environ["GOD_JUDGE_WORK_ROOT"])
            if os.environ.get("GOD_JUDGE_WORK_ROOT")
            else None
        ),
        help="Parent of the per-game temp directories (default: system temp).",
    )
    args = parser.parse_args(argv)
    if args.max_games < 1:
        parser.error("--max-games must be at least 1")
    oauth_token = os.environ.get("CLAUDE_CODE_OAUTH_TOKEN", "")
    if not args.dry_run and not oauth_token:
        parser.error(
            "CLAUDE_CODE_OAUTH_TOKEN is not set; the judge bills the "
            "subscription through it (use --dry-run to inspect the slate)"
        )

    credentials = os.environ.get("GOOGLE_CREDENTIALS", "")
    sheet_id = os.environ.get("NFL_INTAKE_SHEET_ID", "")
    if not credentials or not sheet_id:
        raise RuntimeError(
            "GOOGLE_CREDENTIALS and NFL_INTAKE_SHEET_ID are required"
        )
    spreadsheet = get_gspread_client(credentials).open_by_key(sheet_id)
    games = spreadsheet.worksheet("nfl_games").get_all_records(
        expected_headers=GAME_HEADERS
    )
    history = spreadsheet.worksheet(GAME_HISTORY_TAB).get_all_records(
        expected_headers=GAME_HISTORY_HEADERS
    )
    snapshots = spreadsheet.worksheet("nfl_line_snapshots").get_all_records(
        expected_headers=SNAPSHOT_HEADERS
    )
    store = configured_opinion_store()
    opinion_rows = store.list()
    seasons = sorted(
        {
            int(game["season"])
            for game in games
            if str(game.get("status") or "") == "upcoming"
            and str(game.get("season") or "").strip()
        }
    )
    finals = [
        row for season in seasons for row in current_season_finals(history, season)
    ]
    registry = load_registry()
    policy = aggregator_policy(registry)
    claude_invoker: Invoker
    if args.dry_run:

        def claude_invoker(_system_prompt: str, _user_message: str, _cwd: str) -> str:
            raise RuntimeError("--dry-run never calls claude")

    else:
        claude_invoker = ClaudeHeadlessInvoker(
            args.claude_bin, oauth_token=oauth_token, isolation=args.isolation
        )
    summary = asyncio.run(
        run_once(
            games=games,
            opinion_rows=opinion_rows,
            finals=finals,
            snapshots=snapshots,
            store=store,
            registry=registry,
            policy=policy,
            now=datetime.now(timezone.utc),
            claude_invoker=claude_invoker,
            notify=send_watchdog_dm,
            max_games=args.max_games,
            dry_run=args.dry_run,
            work_root=args.work_root,
            runs_log_path=args.runs_log,
        )
    )
    print(
        f"God judge runner: {len(summary['attempted'])} attempted, "
        f"{len(summary['skipped'])} skipped, {len(summary['failed'])} failed, "
        f"{len(summary['stalled'])} stalled"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
