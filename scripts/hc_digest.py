#!/usr/bin/env python3
"""Daily alerting digest — the morning read for everything below the DM bar.

The agent-first pipeline (scripts/hc_repair.py) only DMs when an agent
finished, was blocked, or escalation fired. Everything quieter lands here,
once a day (hc-digest.timer, 08:30 ET), in ONE watchdog-bot message:

  🤫 blips        — checks that went down and self-healed with no agent
                    (hc_repair state `_blip_log`, 24h)
  🤖 agent runs   — hc-repair spawns in the last 24h with per-target outcomes,
                    commits and notional cost (logs/hc_repair_runs.jsonl)
  📌 parked       — targets waiting on a human (--rearm hint)
  ⏳ open         — episodes down right now and how long
  🧾 anomalies    — grader anomaly queue: pending + resolved in 24h
  📉 X wobble     — trent watcher bootstrap degradation, 4+ events/24h
                    (reads ~/.trent_watcher_state.json; the old ℹ️ DM is gone)

Nothing happened → no DM (the Alert digest check's success ping still proves
the digest ran; a crash misses the ping and hc-repair spawns an agent).

    ~/venv/bin/python scripts/hc_digest.py [--dry-run]
"""
from __future__ import annotations

import html
import json
import os
import sys
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scripts import hc_repair as hr  # noqa: E402  (loads .env/.env.local via ua)
from scripts import ungraded_audit as ua  # noqa: E402

TRENT_STATE = Path.home() / ".trent_watcher_state.json"
WINDOW = timedelta(hours=24)
TRENT_WOBBLE_MIN = 4


def _ts(value):
    return hr._ts(value)


def _fmt_t(iso: str) -> str:
    t = _ts(iso)
    return t.astimezone().strftime("%H:%M") if t else "?"


def load_json(path: Path, default):
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, type(default)) else default
    except (OSError, json.JSONDecodeError):
        return default


def recent_runs(now: datetime) -> list[dict]:
    out = []
    try:
        lines = hr.RUNS_LOG.read_text(encoding="utf-8").splitlines()
    except OSError:
        return out
    for line in lines[-80:]:
        try:
            rec = json.loads(line)
        except json.JSONDecodeError:
            continue
        t = _ts(rec.get("ts"))
        if t and now - t < WINDOW:
            out.append(rec)
    return out


def build_digest(now: datetime, state: dict, runs: list[dict],
                 anomalies: list[dict], trent_state: dict) -> str | None:
    """The digest HTML, or None when the last 24h were completely clean."""
    e = html.escape
    sections: list[str] = []

    blips = [b for b in state.get("_blip_log", [])
             if (t := _ts(b.get("ts"))) and now - t < WINDOW]
    if blips:
        per: dict[str, list[str]] = {}
        for b in blips:
            name = b.get("check") or "?"
            if b.get("unit") and b["unit"] not in name:
                name = f"{name} / {b['unit']}"
            per.setdefault(name, []).append(_fmt_t(b.get("ts")))
        rows = [f"{e(n)} ×{len(ts)} ({', '.join(ts[-4:])})"
                for n, ts in sorted(per.items())]
        sections.append("🤫 <b>Blips</b> (self-healed, no agent): " + " · ".join(rows))

    runs = [r for r in runs if (t := _ts(r.get("ts"))) and now - t < WINDOW]
    if runs:
        bits = []
        cost = 0.0
        commits = 0
        for rec in runs:
            cost += float(rec.get("total_cost_usd") or 0)
            commits += len(rec.get("commits") or [])
            for label, rep in (rec.get("reports") or {}).items():
                bits.append(f"{e(label)}={e(str(rep.get('outcome', '?')))}")
        sections.append(
            f"🤖 <b>Agent runs</b>: {len(runs)} — " + "; ".join(bits[:10])
            + (f" (+{len(bits) - 10} more)" if len(bits) > 10 else "")
            + f" · {commits} commit(s) · ~${cost:.2f} notional")

    parked = [(k, v) for k, v in state.items()
              if not k.startswith("_") and isinstance(v, dict) and v.get("parked")]
    if parked:
        rows = [f"{e(v.get('check') or k)} ({e(str(v.get('parked_reason')))}) — "
                f"<code>--rearm '{e(k)}'</code>" for k, v in parked]
        sections.append("📌 <b>Parked, needs you</b>: " + " · ".join(rows))

    open_eps = [(k, v) for k, v in state.items()
                if not k.startswith("_") and isinstance(v, dict)
                and v.get("first_seen") and not v.get("parked")]
    if open_eps:
        rows = [f"{e(v.get('check') or k)} (down since {_fmt_t(v['first_seen'])})"
                for k, v in open_eps]
        sections.append("⏳ <b>Open right now</b>: " + " · ".join(rows))

    pending = [a for a in anomalies if a.get("status") == "pending"]
    resolved = [a for a in anomalies if a.get("status") == "resolved"
                and (t := _ts_num(a.get("resolved_ts"))) and now - t < WINDOW]
    if pending or resolved:
        line = f"🧾 <b>Grader anomalies</b>: {len(pending)} pending, {len(resolved)} resolved/24h"
        if pending:
            line += " — " + " · ".join(
                f"[{e(str(a.get('id')))}] {e(str(a.get('text', ''))[:80])}"
                for a in pending[:3])
        sections.append(line)

    events = trent_state.get("degraded_events")
    wobble = [t for ts in (events if isinstance(events, list) else [])
              if (t := _ts(ts)) and now - t < WINDOW]
    if len(wobble) >= TRENT_WOBBLE_MIN:
        sections.append(
            f"📉 <b>X bootstrap wobble</b>: {len(wobble)} degraded Trent runs in 24h "
            "(watcher healthy and self-recovering; a code fix may be due if it "
            "worsens — scripts/x_client.py)")

    if not sections:
        return None
    head = ("🗞 <b>Daily alerting digest</b> — last 24h "
            f"(to {now.astimezone().strftime('%a %H:%M %Z')})")
    return head + "\n" + "\n".join(sections)


def _ts_num(value) -> datetime | None:
    try:
        return datetime.fromtimestamp(float(value), timezone.utc)
    except (TypeError, ValueError, OSError):
        return None


def ping(ok: bool = True) -> None:
    url = os.environ.get("ALERT_DIGEST_HEALTHCHECK_URL", "")
    if not url:
        return
    try:
        with urllib.request.urlopen(
                urllib.request.Request(url + ("" if ok else "/fail")), timeout=10):
            pass
    except Exception as exc:  # noqa: BLE001
        print(f"digest ping failed: {exc}", file=sys.stderr)


def main() -> int:
    dry = "--dry-run" in sys.argv
    now = datetime.now(timezone.utc)
    state = hr.load_state()
    try:
        from audit import load_anomalies
        anomalies = load_anomalies()
    except Exception:  # noqa: BLE001
        anomalies = []
    text = build_digest(now, state, recent_runs(now), anomalies,
                        load_json(TRENT_STATE, {}))
    if text is None:
        print("clean 24h — no digest")
    elif dry:
        print(text)
    else:
        sent = ua.send_watchdog_dm(text, as_html=True)
        print("digest sent" if sent else "digest DM FAILED")
        if not sent:
            ping(ok=False)
            return 1
    if not dry:
        ping()
    return 0


if __name__ == "__main__":
    sys.exit(main())
