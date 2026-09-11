#!/usr/bin/env python3
"""Headless God Expert judge runner.

``god-judge.timer`` runs this every 30 minutes (at :12 and :42, after the
BetOnline lines fetcher has usually written a fresh capture).

**Late-window voice refresh.** Inside ``REFRESH_WINDOW`` of kickoff (the
final two eligible passes), each human-input voice (``refresh_on_human_input``
in the registry: ak, cee, celebrity) whose newest eligible submission
postdates its approved row -- or that has no approved row while eligible
input exists, which also heals an incomplete committee -- regenerates first:
one ``claude -p`` call at the expert's registered model and effort, persisted
through ``generate_opinion`` exactly like a manual agent run (backend
``claude_headless``, auto-approved when valid). At most ``--max-refreshes``
voices per pass; a failed or invalid regeneration is logged and DMed, and the
standing approved row, if any, remains the voice, so a refresh can never take
a committee away. The refresh tabs are read only when a game is inside the
window; ``GOD_JUDGE_REFRESH=0`` or ``--no-refresh`` disables the step. The
market staleness of a voice is deliberately NOT a refresh trigger -- the
judge already receives each voice's generation-time board and the movement
since (2026-09-10); only new human input regenerates a voice.

For each upcoming game whose committee is complete -- one approved,
hash-verified row for every enabled non-aggregator expert -- it:

1. builds the aggregator input and the masked judge request into a fresh
   temp directory (``input.json``, ``request.json``, and an EMPTY ``cwd``);
2. persists the rules arm on that exact input, unless a valid ``god_rules``
   row already carries the same committee key;
3. runs ``--samples`` ``claude -p`` calls (``GOD_JUDGE_SAMPLES``, default
   1): Fable 5.1 at max effort, every tool disabled, the registered judge
   prompt as the whole system prompt, the request on stdin, from the empty
   directory, in an environment that holds no sheet credentials and no API
   key; each JSON result envelope is captured;
4. persists the judge row through ``generate_opinion`` on the same input
   (backend ``claude_headless``); a response that fails validation persists
   as an invalid audit row like any other response. With two or more
   samples (the judge ensemble, roadmap WP9) every sampled response first
   persists as an audit row with ``generation_status`` ``sample`` (review
   ``not_applicable``, whether or not it validated), and the one judge row
   of the trigger carries the mean of the valid samples' three numbers with
   the reasons of the sample closest to the mean
   (``moe_god.ensemble_response``); when no sample validated the first
   response persists as an ordinary invalid judge row, so the stall cap
   below counts the trigger exactly as a single-sample one;
5. deletes the temp directory and DMs the operator through the watchdog bot
   that the valid rows were approved automatically.

Dedupe is by committee key (``moe_god.committee_key``: the sorted voice
opinion ids plus the latest full-game lines and prices, never the capture
timestamp). A game is skipped inside one hour of kickoff (decisions land
about 1h-1h30 pregame, after the T-90min inactives are in the lines), when
its committee is incomplete, when a valid judge row that has not been
manually rejected already carries the current key, or when two invalid judge rows
carry it (the judge failed twice on this committee; the reviewer is told once
per invocation). A rejected judge row does not block: rejection is the
reviewer asking for a fresh run. At most ``--max-games``
games run per invocation. Every judge call bills the Claude Code
subscription, so a failed call is logged and DMed, never retried; the next
timer slot tries again.

Each call appends one JSON line to ``logs/god_judge_runs.jsonl``
(``GOD_JUDGE_RUNS_LOG``) carrying the envelope's ``duration_ms``,
``total_cost_usd``, ``usage`` and ``num_turns``, plus ``sample`` (1-based)
and ``samples`` (the trigger's call count), so weekly subscription usage can
be measured per trigger.

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
    _parse_response,
    approved_opinions,
    configured_opinion_store,
    generate_opinion,
    load_expert,
)
from moe_ak import _parse_time
from moe_cee import _eligible_submissions
from moe_god import (
    AGGREGATOR_MODES,
    DETERMINISTIC_BACKEND,
    aggregator_policy,
    build_aggregator_input,
    build_judge_request,
    canonical_json,
    ensemble_response,
    load_registry,
    select_voice_rows,
    sha256_text,
)
from nfl_game_history import GAME_HISTORY_HEADERS, GAME_HISTORY_TAB
from nfl_game_annotations import attach_game_annotations, load_game_annotations
from nfl_lines import GAME_HEADERS, SNAPSHOT_HEADERS, get_gspread_client
from scripts.generate_moe_opinion import current_season_finals

ET = ZoneInfo("America/New_York")
RULES_EXPERT_ID = "god_rules"
JUDGE_EXPERT_ID = "god_judge"
JUDGE_MODEL = "claude-fable-5-1"
JUDGE_EFFORT = "max"
JUDGE_BACKEND = "claude_headless"
KICKOFF_CUTOFF = timedelta(hours=1)
# The timer fires every 30 minutes (:12/:42), so a game's final two eligible
# passes fall inside this window; stale human-input voices refresh there.
TIMER_INTERVAL = timedelta(minutes=30)
REFRESH_WINDOW = KICKOFF_CUTOFF + 2 * TIMER_INTERVAL
DEFAULT_MAX_GAMES = 3
DEFAULT_MAX_REFRESHES = 6
INVALID_ATTEMPT_CAP = 2
# claude calls per game: 1 is the single-sample path, 2..MAX the ensemble.
DEFAULT_SAMPLES = 1
MAX_SAMPLES = 5
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
        model: str = JUDGE_MODEL,
        effort: str = JUDGE_EFFORT,
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
        self.model = model
        self.effort = effort
        self.last_call: dict[str, Any] = {}

    def command(self, system_prompt: str) -> list[str]:
        return [
            self.claude_bin,
            "-p",
            *ISOLATION_FLAGS[self.isolation],
            "--model",
            self.model,
            "--effort",
            self.effort,
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
    """Enabled required voices; optional experts join only when available."""
    experts = registry["experts"]
    return [
        expert_id
        for expert_id in sorted(experts)
        if isinstance(experts[expert_id], dict)
        and experts[expert_id].get("enabled")
        and not experts[expert_id].get("committee_optional")
        and str(experts[expert_id].get("mode") or "") not in AGGREGATOR_MODES
    ]


def human_input_experts(registry: dict[str, Any]) -> list[str]:
    """Enabled voices whose input is human submissions, in id order.

    Marked ``refresh_on_human_input`` in the registry: the judge's
    generation-time board covers market drift since a voice generated, but
    nothing compensates for a projection, lean, or celebrity pick that
    postdates the voice's input — those voices regenerate in the late
    window instead.
    """
    experts = registry["experts"]
    return [
        expert_id
        for expert_id in sorted(experts)
        if isinstance(experts[expert_id], dict)
        and experts[expert_id].get("enabled")
        and experts[expert_id].get("refresh_on_human_input")
        and str(experts[expert_id].get("mode") or "") not in AGGREGATOR_MODES
    ]


def latest_human_input(
    expert_id: str,
    game: dict[str, Any],
    refresh_data: dict[str, Any],
) -> datetime | None:
    """When the newest eligible human submission feeding this voice landed,
    or None when the voice's input cannot be built at all. Each branch
    mirrors its builder's own source filter (``build_ak_input``,
    ``build_cee_input``, ``build_celebrity_input``)."""
    event_id = str(game["event_id"])
    kickoff = _parse_time(game["commence_time_utc"])
    if expert_id == "celebrity":
        times = [
            _parse_time(row["submitted_at_utc"])
            for row in refresh_data.get("celebrity_picks") or []
            if str(row.get("event_id") or "") == event_id
            and _parse_time(row["submitted_at_utc"]) < kickoff
        ]
        return max(times, default=None)
    if expert_id == "ak":
        user_id = str(refresh_data.get("ak_user_id") or "")
        rows = [
            row
            for row in refresh_data.get("leans") or []
            if user_id
            and str(row.get("telegram_user_id")) == user_id
            and str(row.get("event_id")) == event_id
            and str(row.get("period")) == "game"
            and str(row.get("prediction_parse_status") or "")
            != "not_applicable"
            and _parse_time(row["submitted_at_utc"]) < kickoff
        ]
        if not rows:
            return None
        current = max(
            rows, key=lambda row: str(row.get("submitted_at_utc") or "")
        )
        # build_ak_input refuses an unparsed newest projection, so an
        # unparsed newest row is not fresh input.
        if str(current.get("prediction_parse_status") or "") != "parsed":
            return None
        return _parse_time(current["submitted_at_utc"])
    if expert_id == "cee":
        user_id = str(refresh_data.get("cee_user_id") or "")
        if not user_id:
            return None
        leans = list(refresh_data.get("leans") or [])
        moneyline = _eligible_submissions(
            leans, game=game, user_id=user_id, market="moneyline"
        )
        if not moneyline:
            return None  # build_cee_input requires a moneyline pick
        spread = _eligible_submissions(
            leans, game=game, user_id=user_id, market="spread"
        )
        return max(
            _parse_time(row["submitted_at_utc"])
            for row in [*moneyline, *spread]
        )
    return None


def stale_human_voices(
    game: dict[str, Any],
    *,
    selected: list[tuple[str, dict[str, Any], dict[str, Any], str]],
    registry: dict[str, Any],
    refresh_data: dict[str, Any],
) -> list[tuple[str, str]]:
    """``(expert_id, reason)`` for every human-input voice whose newest
    eligible submission postdates its selected approved row — or that has no
    approved row at all while eligible input exists (an optional voice that
    was never generated, or a required one whose absence keeps the committee
    incomplete: refreshing heals both)."""
    selected_by_id = {item[0]: item[2] for item in selected}
    stale: list[tuple[str, str]] = []
    for expert_id in human_input_experts(registry):
        newest = latest_human_input(expert_id, game, refresh_data)
        if newest is None:
            continue
        row = selected_by_id.get(expert_id)
        if row is None:
            stale.append((expert_id, "no approved row yet"))
            continue
        generated_raw = str(row.get("generated_at_utc") or "")
        if not generated_raw or newest > _parse_time(generated_raw):
            stale.append(
                (
                    expert_id,
                    f"input from {newest.isoformat()} postdates the voice",
                )
            )
    return stale


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
    game: dict[str, Any],
    *,
    rules_id: str,
    judge_id: str,
    samples: int = 1,
    valid_samples: int = 0,
    failures: Iterable[str] = (),
    refreshed: Iterable[str] = (),
) -> str:
    """The completion DM for automatically approved God Expert rows."""
    judge_text = f"judge {judge_id}"
    if samples > 1:
        judge_text += f" (mean of {valid_samples} of {samples} samples)"
    lines = [
        f"pickbot: new God Expert rows approved for {describe_game(game)}: "
        f"rules {rules_id}, {judge_text}"
    ]
    refreshed = list(refreshed)
    if refreshed:
        lines.append("refreshed voices: " + ", ".join(refreshed))
    failures = list(failures)
    if failures:
        lines.append("sample failures: " + "; ".join(failures))
    return "\n".join(lines)


def append_runs_log(path: str | Path, record: dict[str, Any]) -> None:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    with target.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, sort_keys=True, ensure_ascii=False) + "\n")


def _text_create_fn(text: str) -> Callable[..., Any]:
    """A ``generate_opinion`` create_fn that answers with captured text."""

    async def create_fn(**_kwargs: Any) -> Any:
        return SimpleNamespace(content=[SimpleNamespace(text=text)])

    return create_fn


def _live_create_fn(invoker: Invoker, cwd: str) -> Callable[..., Any]:
    """A ``generate_opinion`` create_fn that runs one headless claude call,
    so input building, validation, persistence, and the one-repair round all
    stay inside ``generate_opinion`` while the inference runs like the
    judge's."""

    async def create_fn(**kwargs: Any) -> Any:
        system = str(kwargs.get("system") or "")
        content = (
            str(kwargs["messages"][0]["content"])
            + "\n"
            + RESPONSE_INSTRUCTION
            + "\n"
        )
        text = await asyncio.to_thread(invoker, system, content, cwd)
        return SimpleNamespace(content=[SimpleNamespace(text=text)])

    return create_fn


# (game, expert_id) -> awaitable outcome dict with at least "status"
# ("approved" | "invalid" | "failed"), plus "opinion_id" / "error".
Refresher = Callable[[dict[str, Any], str], Any]


def make_voice_refresher(
    *,
    store: MoeOpinionStore,
    refresh_data: dict[str, Any],
    history: list[dict[str, Any]],
    snapshots: list[dict[str, Any]],
    registry: dict[str, Any],
    claude_bin: str | Path,
    oauth_token: str,
    isolation: str,
    runs_log: str | Path,
    work_root: str | Path | None = None,
) -> Refresher:
    """The real late-window refresher: one headless claude call per stale
    voice at the expert's registered model and effort, persisted through
    ``generate_opinion`` (which builds the input from the same tabs the
    manual CLI reads, validates, and auto-approves a valid row)."""

    async def refresh(game: dict[str, Any], expert_id: str) -> dict[str, Any]:
        expert = registry["experts"][expert_id]
        model = str(expert.get("default_model") or "")
        effort = str(expert.get("reasoning_effort") or "max")
        invoker = ClaudeHeadlessInvoker(
            claude_bin,
            oauth_token=oauth_token,
            isolation=isolation,
            model=model,
            effort=effort,
        )
        record: dict[str, Any] = {
            "logged_at_utc": datetime.now(timezone.utc).isoformat(),
            "kind": "voice_refresh_call",
            "expert_id": expert_id,
            "event_id": str(game["event_id"]),
            "away_team": str(game["away_team"]),
            "home_team": str(game["home_team"]),
        }
        workdir = Path(
            tempfile.mkdtemp(
                prefix=f"god-refresh-{expert_id}-",
                dir=None if work_root is None else str(work_root),
            )
        )
        started = time.monotonic()
        outcome: dict[str, Any]
        try:
            call_cwd = workdir / "cwd"
            call_cwd.mkdir(mode=0o700)
            row = await generate_opinion(
                expert_id=expert_id,
                game=game,
                history=history,
                leans=refresh_data.get("leans"),
                line_snapshots=snapshots,
                win_predictions=refresh_data.get("win_predictions"),
                celebrity_picks=refresh_data.get("celebrity_picks"),
                celebrity_grades=refresh_data.get("celebrity_grades"),
                ak_user_id=refresh_data.get("ak_user_id"),
                cee_user_id=refresh_data.get("cee_user_id"),
                store=store,
                model=model or None,
                create_fn=_live_create_fn(invoker, str(call_cwd)),
                generation_backend=JUDGE_BACKEND,
                generation_effort=effort,
                repair_attempts=1,
            )
            outcome = {
                "status": "approved",
                "opinion_id": str(row["opinion_id"]),
            }
            record["status"] = "ok"
            record["opinion_id"] = outcome["opinion_id"]
        except ValueError as exc:
            # An input that cannot build, or a response that failed
            # validation (the audit row, if any, is already persisted).
            outcome = {"status": "invalid", "error": str(exc)}
            record["status"] = "invalid"
            record["error"] = str(exc)
        except Exception as exc:
            outcome = {
                "status": "failed",
                "error": f"{type(exc).__name__}: {exc}",
            }
            record["status"] = "error"
            record["error"] = outcome["error"]
        finally:
            shutil.rmtree(workdir, ignore_errors=True)
        record["wall_ms"] = int((time.monotonic() - started) * 1000)
        record.update(invoker.last_call)
        append_runs_log(runs_log, record)
        return outcome

    return refresh


class _RecordingStore:
    """The store plus a record of what was appended through it, so a sample
    that fails validation can still be named by its audit row's id."""

    def __init__(self, store: MoeOpinionStore) -> None:
        self._store = store
        self.appended: list[dict[str, Any]] = []

    def append(self, row: dict[str, Any]) -> None:
        self._store.append(row)
        self.appended.append(row)

    def list(self, event_id: str | None = None) -> list[dict[str, Any]]:
        return self._store.list(event_id)

    def review(
        self, opinion_id: str, *, status: str, reviewed_by: str, note: str
    ) -> None:
        self._store.review(
            opinion_id, status=status, reviewed_by=reviewed_by, note=note
        )

    def last_opinion_id(self) -> str:
        return str(self.appended[-1]["opinion_id"]) if self.appended else ""


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
    samples: int = DEFAULT_SAMPLES,
    pending_dm: bool = True,
    refresh_data: dict[str, Any] | None = None,
    refresh_voice: Refresher | None = None,
    max_refreshes: int = DEFAULT_MAX_REFRESHES,
) -> dict[str, list[dict[str, Any]]]:
    """One pass over the upcoming slate. Every side effect arrives as an argument.

    ``samples`` is the number of claude calls per game: 1 (the default) is
    the single-sample path, 2 to ``MAX_SAMPLES`` the judge ensemble described
    in the module docstring. ``pending_dm=False`` keeps the completion DM
    quiet; failure and stall DMs are unaffected.

    ``refresh_data`` (the human-submission tabs) arms the late-window voice
    refresh: inside ``REFRESH_WINDOW`` of kickoff, a human-input voice whose
    newest submission postdates its approved row — or that has none —
    regenerates through ``refresh_voice`` before the committee is read, at
    most ``max_refreshes`` voices per pass. ``refresh_data=None`` disables
    the step entirely.
    """
    if not 1 <= int(samples) <= MAX_SAMPLES:
        raise ValueError(f"samples must be between 1 and {MAX_SAMPLES}")
    samples = int(samples)
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
        "refreshed": [],
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
        time_to_kick = _parse_time(game["commence_time_utc"]) - now
        if time_to_kick < KICKOFF_CUTOFF:
            skip(game, "inside the one-hour kickoff cutoff")
            continue
        selected = select_voice_rows(
            approved, event_id=event_id, registry=registry, policy=policy
        )
        refresh_failures: list[str] = []
        refreshed_here: list[str] = []
        if refresh_data is not None and time_to_kick < REFRESH_WINDOW:
            for expert_id, reason in stale_human_voices(
                game,
                selected=selected,
                registry=registry,
                refresh_data=refresh_data,
            ):
                if len(summary["refreshed"]) >= max_refreshes:
                    print(
                        f"{prefix}max refreshes ({max_refreshes}) reached; "
                        "the rest wait for the next pass"
                    )
                    break
                if dry_run or refresh_voice is None:
                    print(
                        f"dry run: would refresh {expert_id} for "
                        f"{describe_game(game)}: {reason}"
                    )
                    summary["refreshed"].append(
                        {
                            "event_id": event_id,
                            "expert_id": expert_id,
                            "reason": reason,
                            "dry_run": True,
                        }
                    )
                    continue
                print(
                    f"refreshing {expert_id} for {describe_game(game)}: "
                    f"{reason}"
                )
                outcome = await refresh_voice(game, expert_id)
                summary["refreshed"].append(
                    {
                        "event_id": event_id,
                        "expert_id": expert_id,
                        "reason": reason,
                        **outcome,
                    }
                )
                if outcome.get("status") == "approved":
                    refreshed_here.append(expert_id)
                    print(
                        f"{describe_game(game)}: refreshed {expert_id} "
                        f"({outcome.get('opinion_id')})"
                    )
                else:
                    error = str(outcome.get("error") or outcome.get("status"))
                    refresh_failures.append(f"{expert_id}: {error}")
                    print(
                        f"{describe_game(game)}: {expert_id} refresh "
                        f"{outcome.get('status')} ({error}); the standing "
                        "approved row, if any, remains the voice"
                    )
            if refresh_failures:
                notify(
                    "pickbot: God Expert voice refresh failed for "
                    f"{describe_game(game)}: " + "; ".join(refresh_failures)
                )
            if refreshed_here:
                known = {str(row.get("opinion_id")) for row in opinion_rows}
                opinion_rows = opinion_rows + [
                    row
                    for row in store.list()
                    if str(row.get("opinion_id")) not in known
                ]
                approved = approved_opinions(opinion_rows)
                selected = select_voice_rows(
                    approved,
                    event_id=event_id,
                    registry=registry,
                    policy=policy,
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
                    f"claude{'' if samples == 1 else f' {samples} times'}, "
                    "persist the judge row, and DM the reviewer"
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
            request_sha256 = sha256_text(canonical_json(request))
            responses: list[tuple[int, str]] = []
            call_errors: list[str] = []
            for index in range(1, samples + 1):
                record: dict[str, Any] = {
                    "logged_at_utc": datetime.now(timezone.utc).isoformat(),
                    "kind": "claude_call",
                    "event_id": event_id,
                    "away_team": str(game["away_team"]),
                    "home_team": str(game["home_team"]),
                    "committee_key": key,
                    "sample": index,
                    "samples": samples,
                }
                started = time.monotonic()
                try:
                    text = claude_invoker(
                        judge_prompt, judge_user_message(request), str(call_cwd)
                    )
                    record["status"] = "ok"
                    responses.append((index, text))
                except Exception as exc:
                    record["status"] = "error"
                    record["error"] = f"{type(exc).__name__}: {exc}"
                    call_errors.append(
                        f"call {index}/{samples}: {record['error']}"
                        if samples > 1
                        else record["error"]
                    )
                record["wall_ms"] = int((time.monotonic() - started) * 1000)
                record.update(getattr(claude_invoker, "last_call", None) or {})
                append_runs_log(runs_log, record)
            if not responses:
                error_text = "; ".join(call_errors)
                print(
                    f"{describe_game(game)}: judge call failed "
                    f"({error_text}); no row persisted"
                )
                notify(
                    "pickbot: God Expert judge call failed for "
                    f"{describe_game(game)}: {error_text}"
                )
                summary["failed"].append(
                    {**attempt, "stage": "claude", "error": error_text}
                )
                continue

            attempt["samples"] = samples
            attempt["valid_samples"] = 0
            attempt["sample_opinion_ids"] = []
            sample_failures = list(call_errors)
            valid_samples: list[dict[str, Any]] = []
            if samples > 1:
                # The ensemble: every response persists as a sample row first
                # (an audit row whether or not it validates); the valid ones
                # combine into the one judge row of the trigger.
                recorder = _RecordingStore(store)
                for index, text in responses:
                    try:
                        sample_row = await generate_opinion(
                            expert_id=JUDGE_EXPERT_ID,
                            game=game,
                            history=[],
                            input_payload=payload,
                            opinions=opinion_rows,
                            store=recorder,
                            create_fn=_text_create_fn(text),
                            generation_backend=JUDGE_BACKEND,
                            generation_effort=JUDGE_EFFORT,
                            model=JUDGE_MODEL,
                            expected_input_sha256=request_sha256,
                            sample=True,
                        )
                    except ValueError as exc:
                        sample_id = recorder.last_opinion_id()
                        print(
                            f"{describe_game(game)}: sample {index}/{samples} "
                            f"failed validation ({exc}); audit row "
                            f"{sample_id or 'not persisted'}"
                        )
                        sample_failures.append(
                            f"sample {index}/{samples} invalid: {exc}"
                        )
                        if sample_id:
                            attempt["sample_opinion_ids"].append(sample_id)
                        continue
                    sample_id = str(sample_row["opinion_id"])
                    attempt["sample_opinion_ids"].append(sample_id)
                    valid_samples.append(
                        {"opinion_id": sample_id, "response": _parse_response(text)}
                    )
                attempt["valid_samples"] = len(valid_samples)
                if valid_samples:
                    response_text = json.dumps(
                        ensemble_response(
                            valid_samples,
                            fair_home=float(payload["market"]["fair"]["home_ml"]),
                            size=samples,
                        ),
                        sort_keys=True,
                    )
                else:
                    # Nothing validated: the first response persists as an
                    # ordinary invalid judge row, so the stall cap counts the
                    # trigger exactly as it would a single-sample one.
                    response_text = responses[0][1]
            else:
                response_text = responses[0][1]

            try:
                judge_row = await generate_opinion(
                    expert_id=JUDGE_EXPERT_ID,
                    game=game,
                    history=[],
                    input_payload=payload,
                    opinions=opinion_rows,
                    store=store,
                    create_fn=_text_create_fn(response_text),
                    generation_backend=JUDGE_BACKEND,
                    generation_effort=JUDGE_EFFORT,
                    model=JUDGE_MODEL,
                    expected_input_sha256=request_sha256,
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
                + (
                    f"; {len(valid_samples)} of {samples} samples valid"
                    if samples > 1
                    else ""
                )
            )
            if pending_dm:
                notify(
                    pending_message(
                        game,
                        rules_id=rules_id,
                        judge_id=judge_id,
                        samples=samples,
                        valid_samples=len(valid_samples),
                        failures=sample_failures,
                        refreshed=refreshed_here,
                    )
                )
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


def build_parser() -> argparse.ArgumentParser:
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
    parser.add_argument(
        "--max-refreshes",
        type=int,
        default=int(
            os.environ.get("GOD_JUDGE_MAX_REFRESHES") or DEFAULT_MAX_REFRESHES
        ),
        help=(
            "Stale human-input voice regenerations per invocation "
            "(GOD_JUDGE_MAX_REFRESHES; default 6)."
        ),
    )
    parser.add_argument(
        "--no-refresh",
        action="store_true",
        default=(os.environ.get("GOD_JUDGE_REFRESH") or "").strip() == "0",
        help=(
            "Skip the late-window voice refresh entirely "
            "(or set GOD_JUDGE_REFRESH=0)."
        ),
    )
    parser.add_argument(
        "--samples",
        type=int,
        default=int(os.environ.get("GOD_JUDGE_SAMPLES") or DEFAULT_SAMPLES),
        help=(
            "claude calls per game (GOD_JUDGE_SAMPLES; default 1). Two to "
            f"{MAX_SAMPLES} is the judge ensemble: every sampled response "
            "persists as an audit row with generation_status sample and the "
            "one judge row carries their mean."
        ),
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.max_games < 1:
        parser.error("--max-games must be at least 1")
    if not 1 <= args.samples <= MAX_SAMPLES:
        parser.error(f"--samples must be between 1 and {MAX_SAMPLES}")
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
    annotation_rows = load_game_annotations(spreadsheet)
    history = attach_game_annotations(history, annotation_rows)
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
        row
        for season in seasons
        for row in current_season_finals(history, season, annotation_rows)
    ]
    registry = load_registry()
    policy = aggregator_policy(registry)
    now = datetime.now(timezone.utc)

    # The refresh tabs are read only when a game is actually inside the
    # late window, so the every-30-minutes pass costs no extra sheet reads
    # on a quiet day.
    refresh_data: dict[str, Any] | None = None
    refresh_voice: Refresher | None = None
    in_refresh_window = not args.no_refresh and any(
        str(game.get("status") or "") == "upcoming"
        and KICKOFF_CUTOFF
        <= _parse_time(game["commence_time_utc"]) - now
        < REFRESH_WINDOW
        for game in games
    )
    if in_refresh_window and human_input_experts(registry):
        from celebrity_grades import configured_celebrity_grade_store
        from celebrity_picks import CELEBRITY_HEADERS
        from intake_bot import _celebrity_worksheet
        from moe_identity import (
            ALLOWED_USER_HEADERS,
            ALLOWED_USERS_TAB,
            resolve_moe_expert_user_id,
        )
        from nfl_lines import LEAN_HEADERS
        from nfl_win_predictions import PREDICTION_HEADERS

        allowed_rows = spreadsheet.worksheet(ALLOWED_USERS_TAB).get_all_records(
            expected_headers=ALLOWED_USER_HEADERS
        )

        def _expert_user_id(expert_id: str) -> str:
            try:
                return resolve_moe_expert_user_id(allowed_rows, expert_id)
            except (RuntimeError, ValueError) as exc:
                print(f"{expert_id} user id unresolved: {exc}", file=sys.stderr)
                return ""

        refresh_data = {
            "leans": spreadsheet.worksheet("nfl_leans").get_all_records(
                expected_headers=LEAN_HEADERS
            ),
            "celebrity_picks": _celebrity_worksheet(
                spreadsheet
            ).get_all_records(expected_headers=CELEBRITY_HEADERS),
            "win_predictions": spreadsheet.worksheet(
                "nfl_win_predictions"
            ).get_all_records(expected_headers=PREDICTION_HEADERS),
            "celebrity_grades": configured_celebrity_grade_store(
                writable=False
            ).list_latest(),
            "ak_user_id": _expert_user_id("ak"),
            "cee_user_id": _expert_user_id("cee"),
        }
        if not args.dry_run:
            refresh_voice = make_voice_refresher(
                store=store,
                refresh_data=refresh_data,
                history=history,
                snapshots=snapshots,
                registry=registry,
                claude_bin=args.claude_bin,
                oauth_token=oauth_token,
                isolation=args.isolation,
                runs_log=args.runs_log,
                work_root=args.work_root,
            )

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
            now=now,
            claude_invoker=claude_invoker,
            notify=send_watchdog_dm,
            max_games=args.max_games,
            dry_run=args.dry_run,
            work_root=args.work_root,
            runs_log_path=args.runs_log,
            samples=args.samples,
            pending_dm=os.environ.get("GOD_JUDGE_PENDING_DM", "1").strip() != "0",
            refresh_data=refresh_data,
            refresh_voice=refresh_voice,
            max_refreshes=args.max_refreshes,
        )
    )
    print(
        f"God judge runner: {len(summary['attempted'])} attempted, "
        f"{len(summary['skipped'])} skipped, {len(summary['failed'])} failed, "
        f"{len(summary['stalled'])} stalled, "
        f"{len(summary['refreshed'])} voices refreshed"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
