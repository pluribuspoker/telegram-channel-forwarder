"""Subscription-billed Claude calls — the ONE way this app talks to Claude.

Operator rule (2026-10-03): every Claude call bills the subscription, never
the API. This module runs one headless `claude -p` per call on
CLAUDE_CODE_OAUTH_TOKEN (env, else ~/.claude/auth.env) — ANTHROPIC_API_KEY is
never passed to it — and returns an object shaped like an Anthropic SDK
Message (`.content[0].text`, `.stop_reason`, `.usage`, `.model`), so callers
written against `messages.create` keep working through
`ai._claude_create_with_retry`.

Isolation (same as the god judge / odds review): `--safe-mode` (no CLAUDE.md,
hooks, skills, plugins), `--tools ""`, `--strict-mcp-config` (a plugin MCP
server would be a second Telegram poller and 409 the operator's session),
`--no-session-persistence`, an empty temp cwd. Images go in as base64
content blocks over `--input-format stream-json` (verified with a bet slip).

Not supported by the CLI, so ignored: `max_tokens` (no cap — answers are
short by prompt), `temperature` (classifiers lose temperature=0
determinism). A system prompt goes to `--system-prompt`.

Memory: the VPS has ~1 GB RAM and each CLI process is a few hundred MB, so a
cross-process slot lock (logs/.claude_sub_slot.N, CLAUDE_SUB_MAX_CONCURRENCY,
default 2) caps concurrent calls from every process (tracker, fast path,
daemon, Trent, sauce). Every call is logged to logs/claude_sub_calls.jsonl
(model, wall/API ms, tokens, the CLI's notional cost — NOT spend: the
API-spend ledger logs/claude_spend.jsonl stays for real API dollars only).
Test: scripts/test_claude_sub.py.
"""

import asyncio
import fcntl
import json
import os
import re
import sys
import tempfile
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any

ROOT = Path(__file__).resolve().parent
CLAUDE_BIN = os.environ.get("CLAUDE_SUB_BIN") or "/home/forwarder/.npm-global/bin/claude"
AUTH_ENV = Path(os.environ.get("HOME", "/home/forwarder")) / ".claude" / "auth.env"
CALL_LOG = ROOT / "logs" / "claude_sub_calls.jsonl"
SLOT_DIR = ROOT / "logs"
MAX_CONCURRENCY = int(os.environ.get("CLAUDE_SUB_MAX_CONCURRENCY") or 2)
DEFAULT_TIMEOUT = float(os.environ.get("CLAUDE_SUB_TIMEOUT") or 180)
SLOT_WAIT = float(os.environ.get("CLAUDE_SUB_SLOT_WAIT") or 300)
ATTEMPTS = 3
# Dated snapshot ids the API took; the CLI wants the bare alias.
_DATED = re.compile(r"^(claude-[a-z]+-\d+-\d+)-\d{8}$")
# A usage-limit answer never heals within a retry — fail fast.
_LIMIT_RE = re.compile(r"usage limit|limit reached|credit balance|rate.?limit|429", re.I)
_SOURCE = os.getenv("CLAUDE_SPEND_SOURCE") or os.path.basename(
    (sys.argv[0] or "unknown")).removesuffix(".py") or "unknown"


class ClaudeCallError(RuntimeError):
    """The subscription call produced no usable answer."""

    def __init__(self, msg: str, *, limit: bool = False):
        super().__init__(msg)
        self.limit = limit


def oauth_token() -> str:
    tok = os.environ.get("CLAUDE_CODE_OAUTH_TOKEN", "")
    if tok:
        return tok
    try:
        for line in AUTH_ENV.read_text().splitlines():
            if line.startswith("CLAUDE_CODE_OAUTH_TOKEN="):
                return line.split("=", 1)[1].strip().strip('"').strip("'")
    except OSError:
        pass
    return ""


def cli_model(model: str) -> str:
    m = _DATED.match(model or "")
    return m.group(1) if m else model


def command(model: str, system: str | None, effort: str | None = None) -> list[str]:
    cmd = [CLAUDE_BIN, "-p", "--safe-mode", "--model", cli_model(model),
           "--tools", "", "--strict-mcp-config", "--no-session-persistence",
           "--input-format", "stream-json", "--output-format", "stream-json",
           "--verbose"]
    if effort:
        cmd += ["--effort", effort]
    if system:
        cmd += ["--system-prompt", system]
    return cmd


def environment(token: str) -> dict[str, str]:
    """Only what the CLI needs — deliberately no ANTHROPIC_API_KEY."""
    return {"PATH": os.environ.get("PATH", "/home/forwarder/.npm-global/bin:/usr/local/bin:/usr/bin:/bin"),
            "HOME": os.environ.get("HOME", "/home/forwarder"), "TERM": "dumb",
            "LANG": "C.UTF-8", "CLAUDE_CODE_OAUTH_TOKEN": token,
            "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1", "NIGHTLY_AUDIT": "1"}


def stdin_payload(messages: list[dict]) -> str:
    """The SDK's messages → stream-json user turns. Only user turns are
    meaningful to a one-shot call (no caller sends assistant prefill)."""
    lines = []
    for m in messages:
        if m.get("role") != "user":
            raise ValueError("claude_sub supports user messages only")
        content = m["content"]
        if isinstance(content, str):
            content = [{"type": "text", "text": content}]
        lines.append(json.dumps({"type": "user",
                                 "message": {"role": "user", "content": content}}))
    return "\n".join(lines) + "\n"


def parse_stream(stdout: str) -> dict:
    """The final `result` event of a stream-json run."""
    final = None
    for line in stdout.splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            ev = json.loads(line)
        except json.JSONDecodeError:
            continue
        if ev.get("type") == "result":
            final = ev
    if final is None:
        raise ClaudeCallError("claude printed no result event")
    return final


def to_message(final: dict, model: str) -> SimpleNamespace:
    text = final.get("result")
    if final.get("is_error") or not isinstance(text, str):
        detail = str(text or final.get("subtype") or "error")[:300]
        raise ClaudeCallError(f"claude reported an error: {detail}",
                              limit=bool(_LIMIT_RE.search(detail)))
    usage = final.get("usage") or {}
    return SimpleNamespace(
        content=[SimpleNamespace(type="text", text=text)],
        stop_reason=final.get("stop_reason") or "end_turn",
        usage=SimpleNamespace(input_tokens=int(usage.get("input_tokens") or 0),
                              output_tokens=int(usage.get("output_tokens") or 0)),
        model=model,
        notional_usd=final.get("total_cost_usd"),
    )


class _Slot:
    """Cross-process cap on concurrent CLI calls (flock on N slot files)."""

    def __init__(self) -> None:
        self.fh = None

    async def __aenter__(self):
        SLOT_DIR.mkdir(parents=True, exist_ok=True)
        deadline = time.monotonic() + SLOT_WAIT
        while True:
            for n in range(max(1, MAX_CONCURRENCY)):
                fh = open(SLOT_DIR / f".claude_sub_slot.{n}", "a")
                try:
                    fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    self.fh = fh
                    return self
                except BlockingIOError:
                    fh.close()
            if time.monotonic() > deadline:
                raise ClaudeCallError(f"no claude slot free in {SLOT_WAIT:.0f}s")
            await asyncio.sleep(0.25)

    async def __aexit__(self, *exc):
        if self.fh:
            fcntl.flock(self.fh, fcntl.LOCK_UN)
            self.fh.close()


def _log(record: dict) -> None:
    try:
        CALL_LOG.parent.mkdir(parents=True, exist_ok=True)
        with CALL_LOG.open("a") as fh:
            fh.write(json.dumps(record, default=str) + "\n")
    except OSError:
        pass


async def _run_once(cmd: list[str], stdin: str, env: dict, timeout: float) -> str:
    with tempfile.TemporaryDirectory(prefix="claude_sub_") as cwd:
        proc = await asyncio.create_subprocess_exec(
            *cmd, cwd=cwd, env=env, stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            start_new_session=True)
        try:
            out, err = await asyncio.wait_for(proc.communicate(stdin.encode()), timeout)
        except (asyncio.TimeoutError, asyncio.CancelledError) as exc:
            # Never orphan a CLI process (a few hundred MB on a 1 GB box) —
            # incl. when the CALLER is cancelled (grade_daemon's cycle timeout).
            try:
                os.killpg(proc.pid, 9)
            except ProcessLookupError:
                pass
            if isinstance(exc, asyncio.CancelledError):
                raise
            await proc.wait()
            raise ClaudeCallError(f"claude timed out after {timeout:.0f}s")
    if proc.returncode != 0 and b'"type":"result"' not in out:
        tail = err.decode("utf-8", "replace").strip()[-300:]
        raise ClaudeCallError(f"claude exited {proc.returncode}: {tail or 'no stderr'}",
                              limit=bool(_LIMIT_RE.search(tail)))
    return out.decode("utf-8", "replace")


async def create(*, model: str, messages: list[dict], system: str | None = None,
                 max_tokens: int | None = None, temperature: float | None = None,
                 effort: str | None = None, timeout: float | None = None,
                 **_ignored: Any) -> SimpleNamespace:
    """messages.create, billed to the subscription. max_tokens/temperature
    are accepted for call-site compatibility and ignored (see module doc)."""
    token = oauth_token()
    if not token:
        raise ClaudeCallError("no CLAUDE_CODE_OAUTH_TOKEN (env or ~/.claude/auth.env)")
    cmd = command(model, system if isinstance(system, str) else None, effort)
    stdin = stdin_payload(messages)
    env = environment(token)
    started = time.monotonic()
    last: Exception | None = None
    for attempt in range(ATTEMPTS):
        try:
            async with _Slot():
                out = await _run_once(cmd, stdin, env, timeout or DEFAULT_TIMEOUT)
            final = parse_stream(out)
            msg = to_message(final, model)
            _log({"ts": round(time.time(), 3), "source": _SOURCE, "model": cli_model(model),
                  "ok": True, "attempt": attempt + 1,
                  "wall_ms": int((time.monotonic() - started) * 1000),
                  "api_ms": final.get("duration_api_ms"),
                  "in": msg.usage.input_tokens, "out": msg.usage.output_tokens,
                  "notional_usd": msg.notional_usd})
            return msg
        except ClaudeCallError as exc:
            last = exc
            if exc.limit or attempt == ATTEMPTS - 1:
                break
            await asyncio.sleep(2 ** (attempt + 1))
    _log({"ts": round(time.time(), 3), "source": _SOURCE, "model": cli_model(model),
          "ok": False, "error": str(last)[:300],
          "wall_ms": int((time.monotonic() - started) * 1000)})
    raise last  # type: ignore[misc]
