#!/usr/bin/env python3
"""Offline tests for claude_sub (subscription-only Claude calls).

Pins the operator rule (2026-10-03: every Claude call bills the subscription):
the CLI command/env never carry ANTHROPIC_API_KEY, the app has no API client
left, SDK-shaped calls (text, images, system) map onto stream-json, the result
is SDK-shaped, transient errors retry while a usage limit fails fast, and the
cross-process slot caps concurrency. No network, no claude binary.

    python scripts/test_claude_sub.py
"""
import asyncio
import json
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import claude_sub as cs  # noqa: E402

failures = []


def check(label: str, ok: bool, detail: str = ""):
    print(f"{'PASS' if ok else 'FAIL'}  {label}" + (f"  ({detail})" if detail and not ok else ""))
    if not ok:
        failures.append(label)


cs.CALL_LOG = Path(tempfile.mkdtemp()) / "calls.jsonl"
cs.SLOT_DIR = cs.CALL_LOG.parent
os.environ["CLAUDE_CODE_OAUTH_TOKEN"] = "oauth-tok"
os.environ["ANTHROPIC_API_KEY"] = "sk-must-never-reach-the-cli"

cmd = cs.command("claude-haiku-4-5-20251001", "sys prompt", "low")
check("dated API ids map to the CLI alias", cmd[cmd.index("--model") + 1] == "claude-haiku-4-5")
check("isolated headless call: -p --safe-mode, no tools, strict MCP, stream-json in/out",
      all(f in cmd for f in ("-p", "--safe-mode", "--strict-mcp-config", "--no-session-persistence"))
      and cmd[cmd.index("--tools") + 1] == ""
      and cmd[cmd.index("--input-format") + 1] == "stream-json"
      and cmd[cmd.index("--system-prompt") + 1] == "sys prompt"
      and cmd[cmd.index("--effort") + 1] == "low")
env = cs.environment(cs.oauth_token())
check("env carries the OAuth token and NO API key",
      env["CLAUDE_CODE_OAUTH_TOKEN"] == "oauth-tok" and not any("ANTHROPIC" in k for k in env), env)

img = {"type": "image", "source": {"type": "base64", "media_type": "image/jpeg", "data": "AAAA"}}
lines = cs.stdin_payload([{"role": "user", "content": [img, {"type": "text", "text": "odds?"}]},
                          {"role": "user", "content": "plain"}]).strip().split("\n")
first, second = json.loads(lines[0]), json.loads(lines[1])
check("image blocks pass through as stream-json user content",
      first["type"] == "user" and first["message"]["content"][0] == img)
check("a plain-string message becomes one text block",
      second["message"]["content"] == [{"type": "text", "text": "plain"}])
try:
    cs.stdin_payload([{"role": "assistant", "content": "prefill"}])
    check("assistant prefill is refused", False)
except ValueError:
    check("assistant prefill is refused", True)

STREAM = "\n".join([
    json.dumps({"type": "system", "subtype": "init"}),
    json.dumps({"type": "assistant", "message": {}}),
    json.dumps({"type": "result", "result": "-115", "is_error": False,
                "usage": {"input_tokens": 12, "output_tokens": 3}, "total_cost_usd": 0.001,
                "duration_api_ms": 900}),
])
msg = cs.to_message(cs.parse_stream(STREAM), "claude-sonnet-4-6")
check("result is SDK-shaped (.content[0].text, .usage, .stop_reason)",
      msg.content[0].text == "-115" and msg.usage.output_tokens == 3 and msg.stop_reason == "end_turn")
try:
    cs.to_message({"is_error": True, "result": "Claude AI usage limit reached|1760000000"}, "m")
    check("usage-limit answers are flagged as limits", False)
except cs.ClaudeCallError as exc:
    check("usage-limit answers are flagged as limits", exc.limit)


def run(coro):
    return asyncio.run(coro)


calls = []


def fake_runs(*outs):
    seq = list(outs)

    async def _run(cmd, stdin, env, timeout):
        calls.append(cmd)
        o = seq.pop(0)
        if isinstance(o, Exception):
            raise o
        return o
    return _run


_sleep = asyncio.sleep
async def _no_sleep(_s):
    await _sleep(0)
cs.asyncio.sleep = _no_sleep

cs._run_once = fake_runs(cs.ClaudeCallError("claude exited 1: overloaded"), STREAM)
calls.clear()
m = run(cs.create(model="claude-sonnet-4-6", max_tokens=10, temperature=0,
                  messages=[{"role": "user", "content": "x"}]))
check("a transient failure retries, max_tokens/temperature are accepted and ignored",
      m.content[0].text == "-115" and len(calls) == 2)

cs._run_once = fake_runs(cs.ClaudeCallError("limit", limit=True), STREAM)
calls.clear()
try:
    run(cs.create(model="m", messages=[{"role": "user", "content": "x"}]))
    check("a usage limit fails fast (no retry)", False)
except cs.ClaudeCallError:
    check("a usage limit fails fast (no retry)", len(calls) == 1)
log = [json.loads(l) for l in cs.CALL_LOG.read_text().splitlines()]
check("every call is logged (ok and failed), notional cost kept out of the spend ledger",
      log[0]["ok"] and log[-1]["ok"] is False and "notional_usd" in log[0])

cs.MAX_CONCURRENCY = 1
cs.SLOT_WAIT = 0.3
async def _two_at_once():
    async with cs._Slot():
        try:
            async with cs._Slot():
                return False
        except cs.ClaudeCallError:
            return True
check("the slot lock caps concurrent CLI calls", run(_two_at_once()))

import ai  # noqa: E402
try:
    ai.claude()
    check("the app has no API client left (ai.claude() refuses)", False)
except RuntimeError:
    check("the app has no API client left (ai.claude() refuses)", True)
src = Path(ai.__file__).read_text()
check("ai.py no longer imports the anthropic SDK", "import anthropic" not in src)

print(f"\n{'ALL PASS' if not failures else f'{len(failures)} FAILED'}")
sys.exit(1 if failures else 0)
