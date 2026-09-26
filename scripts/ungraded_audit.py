#!/usr/bin/env python3
"""Nightly ungraded-pick audit — one fresh /investigate agent per stale pick.

``ungraded-audit.timer`` runs this every night at 04:05 ET (after the last
West-coast finals have had hours to settle, before the 06:00 auto-reboot /
unattended-upgrades window). It:

1. scans ``parse_cache.json`` for entries with unresolved legs (no
   WIN/LOSS/PUSH/VOID verdict) whose stale-reference date — the same
   game-not-post horizon ``grade_daemon._stale_reference_date`` uses — is in
   the past, including entries the daemon already retired as ``_failed``
   (except "message deleted": there is nothing left to grade);
2. groups fan-out copies of the same pick (one source forwarded into several
   dest channels — same capper, same unresolved legs, same reference date)
   so ONE agent fixes every copy, per the multi-dest rule;
3. for each group, SEQUENTIALLY, launches one fresh headless ``claude -p``
   agent in ``/home/forwarder/app`` that runs the ``/investigate`` command:
   decide whether the pick should have graded; if yes, fix the root cause in
   code (with the pinned-test conventions), grade the pick, and verify; if it
   is legitimately ungradeable, change nothing and say so. Agents never push
   and never restart services — the runner does both, once, at the end;
4. audits everything: the full stream-json transcript and final report per
   pick under ``logs/ungraded_audit/<date>/``, one JSONL line per scan and
   per agent call in ``logs/ungraded_audit_runs.jsonl`` (cost, usage, turns,
   commits, outcome), attempts/parking in ``logs/ungraded_audit_state.json``;
5. pushes any commits the agents made, restarts ``grade-daemon`` if code
   changed (the tracker timer picks new code up by itself; the listener is
   NEVER auto-restarted — flood-wait caution — only flagged in the DM);
6. DMs the operator through the watchdog bot (Bot API HTML): a run header,
   then one actionable card per pick — emoji headline hyperlinked to the
   pick's message, prose collapsed in an expandable blockquote,
   WIN/LOSS/PUSH buttons while unresolved (handled by claude-watchdog.service
   via scripts/audit_mark.py; card facts in logs/ungraded_audit_cards.json)
   and a copy_text follow-up prompt. Silent when the scan found nothing
   (watchdog convention: silent-unless-alerting; the scan ledger line is
   still written every night).

Every agent call bills the Claude Code subscription (OAuth token, same as the
interactive session and the god judge). A failed call is logged and reported,
never retried in the same night; a pick gets at most ``--attempt-cap`` nights
(default 2) before it is parked as needs-human. ``--dry-run`` scans and
prints the plan, calls nothing, writes nothing. Exit status is 0 when there
was nothing to do or every failure was a logged agent call, 1 only on an
unexpected exception.

Full detail and recovery levers: docs/ungraded-audit.md.
"""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import html
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import time
import urllib.parse
import urllib.request
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
load_dotenv(ROOT / ".env.local")
load_dotenv(ROOT / ".env")

ET = ZoneInfo("America/New_York")
RESOLVED = ("WIN", "LOSS", "PUSH", "VOID")

CACHE_FILE = ROOT / "parse_cache.json"
STATE_FILE = ROOT / "logs" / "ungraded_audit_state.json"
CARDS_FILE = ROOT / "logs" / "ungraded_audit_cards.json"
CARD_RETENTION_DAYS = 45
LOCK_FILE = ROOT / "logs" / ".ungraded_audit.lock"
TRANSCRIPT_ROOT = ROOT / "logs" / "ungraded_audit"
DEFAULT_RUNS_LOG = ROOT / "logs" / "ungraded_audit_runs.jsonl"
TRANSCRIPT_RETENTION_DAYS = 90

DEFAULT_MAX_PICKS = int(os.environ.get("UNGRADED_AUDIT_MAX_PICKS") or 3)
DEFAULT_DAYS_BACK = int(os.environ.get("UNGRADED_AUDIT_DAYS_BACK") or 10)
DEFAULT_ATTEMPT_CAP = int(os.environ.get("UNGRADED_AUDIT_ATTEMPT_CAP") or 2)
AGENT_TIMEOUT = int(os.environ.get("UNGRADED_AUDIT_AGENT_TIMEOUT") or 1500)
BUDGET_MIN = int(os.environ.get("UNGRADED_AUDIT_BUDGET_MIN") or 90)
MAX_TURNS = int(os.environ.get("UNGRADED_AUDIT_MAX_TURNS") or 150)
MODEL = os.environ.get("UNGRADED_AUDIT_MODEL") or "claude-fable-5"
EFFORT = "max"
DEFAULT_CLAUDE_BIN = (
    os.environ.get("UNGRADED_AUDIT_CLAUDE_BIN")
    or "/home/forwarder/.npm-global/bin/claude"
)
GROUP_CAP = 4  # fan-out copies of one pick handled by a single agent

DEFAULT_MAX_ANOMALIES = int(os.environ.get("UNGRADED_AUDIT_MAX_ANOMALIES") or 2)
ANOMALY_GROUP_CAP = 8  # instances of one rule handed to a single agent

OUTCOMES = (
    "graded",             # verdict now persisted + message/broadcast repaired
    "fixed_needs_verify", # code fixed; normal flow should grade it shortly
    "legit_ungraded",     # correctly ungraded (postponed, not a pick, ...)
    "needs_human",        # product decision / paid API / ambiguity — parked
    "no_issue",           # already resolved by the time the agent looked
    "repaired",           # anomaly: class fixed in code + live artifacts repaired
    "false_positive",     # anomaly: the rule fired on a correct pick
)
PARK_OUTCOMES = ("legit_ungraded", "needs_human", "no_issue",
                 "repaired", "false_positive")


# ─── scan ────────────────────────────────────────────────────────────────────

def _stale_reference_date(leg_verdicts: dict, odds_by_pick: dict, msg_date: str) -> str:
    """Mirror of grade_daemon._stale_reference_date (kept import-free: pulling
    grade_daemon in would drag telethon into a job that must stay light).
    Ages off the GAME, not the post; a leg with no known game contributes
    nothing and falls back to the post date — exactly the leg worth auditing."""
    dates = [d for d in [msg_date] if d]
    for src in (leg_verdicts, odds_by_pick):
        for v in (src or {}).values():
            if isinstance(v, dict):
                gd = v.get("game_date")
                if isinstance(gd, str) and len(gd) == 10:
                    dates.append(gd)
    return max(dates) if dates else msg_date


def _unresolved_indices(picks: list, leg_verdicts: dict) -> list[int]:
    """Same predicate the daemon greps for pending legs, VOID counted as
    settled and a lost parlay's pending siblings treated as moot."""
    parlay_lost = any(p.get("is_parlay_leg") for p in picks) and any(
        (leg_verdicts.get(str(i)) or {}).get("verdict") == "LOSS"
        for i in range(len(picks))
    )
    if parlay_lost:
        return []
    return [
        i for i in range(len(picks))
        if (leg_verdicts.get(str(i)) or {}).get("verdict") not in RESOLVED
    ]


def _leg_facts(entry: dict, indices: list[int]) -> list[dict[str, Any]]:
    picks = (entry.get("parsed") or {}).get("picks") or []
    lv = entry.get("leg_verdicts") or {}
    odds = entry.get("odds_by_pick") or {}
    facts = []
    for i in indices:
        pick = picks[i] if i < len(picks) else {}
        leg = lv.get(str(i)) or {}
        facts.append({
            "idx": i,
            "description": pick.get("description") or "",
            "bet_type": pick.get("bet_type") or "",
            "sport": pick.get("sport") or (entry.get("parsed") or {}).get("sport") or "",
            "period": pick.get("period") or "",
            "game_date": leg.get("game_date")
            or (odds.get(str(i)) or {}).get("game_date") or "",
            "unknown_attempts": leg.get("unknown_attempts") or 0,
        })
    return facts


def _fingerprint(capper: str, ref: str, legs: list[dict[str, Any]]) -> tuple:
    descs = tuple(sorted(re.sub(r"\s+", " ", l["description"].strip().lower())
                         for l in legs))
    return (capper.strip().lower(), ref, descs)


def scan(
    cache: dict,
    state: dict,
    *,
    today_et: date,
    days_back: int = DEFAULT_DAYS_BACK,
    attempt_cap: int = DEFAULT_ATTEMPT_CAP,
) -> list[dict[str, Any]]:
    """Ungraded-pick groups worth an agent tonight, newest reference first.

    A group is every cache key that carries the same pick (fan-out copies:
    same capper, same unresolved descriptions, same reference date); the
    whole group is skipped when any copy is parked or attempt-capped, since
    one verdict covers them all.
    """
    floor = (today_et - timedelta(days=days_back)).isoformat()
    yesterday = (today_et - timedelta(days=1)).isoformat()
    groups: dict[tuple, dict[str, Any]] = {}
    for key, entry in cache.items():
        if not isinstance(entry, dict) or "parsed" not in entry:
            continue
        if entry.get("_dupe"):
            continue
        picks = (entry.get("parsed") or {}).get("picks") or []
        if not picks:
            continue
        lv = entry.get("leg_verdicts") or {}
        unresolved = _unresolved_indices(picks, lv)
        if not unresolved:
            continue
        reason = str(entry.get("_failed_reason") or "")
        if entry.get("_failed") and reason.startswith("message deleted"):
            continue  # nothing left to grade or repair
        msg_date = str(entry.get("msg_date") or "")[:10]
        ref = _stale_reference_date(lv, entry.get("odds_by_pick") or {}, msg_date)
        if not ref or ref > yesterday:
            continue  # game not over yet — the normal flow still owns it
        if ref < floor:
            continue  # written-off backlog stays written off
        capper = str(entry.get("capper_name") or "")
        legs = _leg_facts(entry, unresolved)
        member = {
            "key": key,
            "msg_date": msg_date,
            "failed": bool(entry.get("_failed")),
            "failed_reason": reason,
            "legs": legs,
            "resolved": [
                {
                    "idx": i,
                    "description": (picks[i].get("description") or "")[:60],
                    "verdict": (lv.get(str(i)) or {}).get("verdict"),
                }
                for i in range(len(picks))
                if (lv.get(str(i)) or {}).get("verdict") in RESOLVED
            ],
        }
        fp = _fingerprint(capper, ref, legs)
        group = groups.setdefault(fp, {
            "capper": capper, "ref_date": ref, "members": [],
        })
        if len(group["members"]) < GROUP_CAP:
            group["members"].append(member)

    out = []
    for group in groups.values():
        group["members"].sort(key=lambda m: m["key"])
        keys = [m["key"] for m in group["members"]]
        st = [state.get(k) or {} for k in keys]
        if any(s.get("parked") for s in st):
            continue
        if max((s.get("attempts") or 0) for s in st) >= attempt_cap:
            continue
        group["keys"] = keys
        group["attempts"] = max((s.get("attempts") or 0) for s in st)
        out.append(group)
    out.sort(key=lambda g: (g["ref_date"], g["keys"][0]), reverse=True)
    return out


# ─── graded-pick anomaly scan ────────────────────────────────────────────────
#
# The ungraded scan only sees legs with NO verdict. A leg whose verdict is right
# but whose label, parse shape, or price is wrong never reaches it ("Italy /
# Belgium BTTS" graded ❌ correctly and broadcast as "Italy/Belgium O0.5" for a
# week-old class of bug). These rules are deterministic invariants over the
# cache — zero API cost; an agent runs only when one fires. Each rule was swept
# over the live cache at build time (2026-09-26) and tightened until the only
# hits were real bugs; widen one only after re-running that sweep.

# (rule id, description regex, label regex): a description naming the market
# must render a label naming it too. Period markets double as a parse check —
# the label carries the parsed period, so "1st half" with period=game fires.
LABEL_MARKETS = (
    ("BTTS", r"\bBTTS\b|both\s+teams\s+to\s+score", r"\bBTTS\b"),
    ("DNB", r"\bDNB\b|draw\s+no\s+bet", r"\bDNB\b"),
    ("DC", r"\bdouble\s+chance\b", r"\bDC\b"),
    ("advance", r"\bto\s+(?:advance|qualify)\b", r"\bto (?:Advance|Qualify)\b"),
    ("3-way", r"\b3.?way\b|\bregulation\b|\b60.?min", r"\b3-way\b"),
    ("F5", r"\bF5\b|\bfirst\s*5\b|\b1st\s*5\b", r"\bF5\b"),
    ("1H", r"\b1H\b|\bfirst\s+half\b|\b1st\s+half\b", r"\b1H\b|\bF5\b"),
    ("1Q", r"\b1Q\b|\bfirst\s+quarter\b|\b1st\s+quarter\b", r"\b1Q\b"),
    ("NRFI", r"\b[NY]RFI\b|\b(?:1st|first)\s+inning\b", r"\b1st Inn\b"),
)
# A label with none of these carries no bet at all ("Angers", "Evan Engram").
_BET_TOKEN_RE = re.compile(
    r"\d|\b(?:ML|DC|DNB|BTTS|Advance|Qualify|Yes|No|KO|TKO|Sub|Dec|Draw)\b")
# Main-line spreads/totals live near -110; past this the price is an alternate
# or a wrong-market binding. Parlay legs are exempt (teaser legs price as
# alternates by design) and so are live prices.
PRICE_BAND = 400

ANOMALY_TITLES = {
    "fanout_split": "fan-out copies disagree",
    "bare_label": "label names no bet",
    "price_band": "price outside the main-line band",
}


def rendered_channels() -> set[int]:
    """Dest channels whose result labels are rendered (broadcast feed or
    Sheets) — the only places a label-only defect is visible."""
    out = set()
    try:
        for m in json.loads(os.environ.get("MAPPINGS_CONFIG") or "[]"):
            if m.get("broadcast_results_channel") or m.get("sheets_id"):
                out.add(int(m["dest_channel"]))
    except (ValueError, TypeError, KeyError):
        pass
    return out


def _anomaly_hits(key: str, entry: dict, *, rendered: set[int],
                  format_pick) -> list[dict[str, Any]]:
    """Per-leg invariant violations on RESOLVED legs of one cache entry."""
    picks = (entry.get("parsed") or {}).get("picks") or []
    lv = entry.get("leg_verdicts") or {}
    odds = entry.get("odds_by_pick") or {}
    try:
        channel = int(key.split(":")[0])
    except ValueError:
        channel = 0
    hits = []
    for i, pick in enumerate(picks):
        verdict = (lv.get(str(i)) or {}).get("verdict")
        if verdict not in RESOLVED:
            continue
        desc = pick.get("description") or ""
        try:
            label = format_pick(pick)
        except Exception as exc:  # a crashing renderer is itself an anomaly
            label = f"<render error: {exc}>"
        base = {"key": key, "idx": i, "description": desc, "label": label,
                "verdict": verdict, "bet_type": pick.get("bet_type") or "",
                "period": pick.get("period") or ""}
        for rule, desc_re, label_re in LABEL_MARKETS:
            if (re.search(desc_re, desc, re.IGNORECASE)
                    and not re.search(label_re, label)):
                hits.append({**base, "rule": f"label:{rule}",
                             "detail": f"description names {rule}, label "
                                       f"{label!r} doesn't"})
        if channel in rendered and not _BET_TOKEN_RE.search(label):
            hits.append({**base, "rule": "bare_label",
                         "detail": f"label {label!r} names no bet"})
        o = odds.get(str(i)) or {}
        price, mt = o.get("odds"), str(o.get("match_type") or "")
        if (isinstance(price, int) and not pick.get("is_parlay_leg")
                and pick.get("bet_type") in ("spread", "total", "team_total")
                and abs(price) >= PRICE_BAND and "live" not in mt):
            hits.append({**base, "rule": "price_band",
                         "detail": f"{price:+d} ({mt or 'no match_type'})"})
    return hits


def scan_anomalies(
    cache: dict,
    state: dict,
    *,
    today_et: date,
    days_back: int = DEFAULT_DAYS_BACK,
    attempt_cap: int = DEFAULT_ATTEMPT_CAP,
    rendered: set[int] | None = None,
    format_pick=None,
) -> list[dict[str, Any]]:
    """Graded-pick invariant violations, ONE group per rule (one agent fixes
    the class and repairs every listed instance), newest first. State is kept
    per instance (`anomaly:<rule>:<key>:<leg>`), so a parked instance drops
    out while new instances of the same rule still surface."""
    if format_pick is None:
        from audit import _format_pick as format_pick  # heavy import: lazy
    if rendered is None:
        rendered = rendered_channels()
    floor = (today_et - timedelta(days=days_back)).isoformat()
    hits: list[dict[str, Any]] = []
    fanout: dict[tuple, list[tuple[str, int, str]]] = {}
    for key, entry in cache.items():
        if not isinstance(entry, dict) or "parsed" not in entry or entry.get("_dupe"):
            continue
        lv = entry.get("leg_verdicts") or {}
        msg_date = str(entry.get("msg_date") or "")[:10]
        ref = _stale_reference_date(lv, entry.get("odds_by_pick") or {}, msg_date)
        if not ref or ref < floor:
            continue
        for h in _anomaly_hits(key, entry, rendered=rendered, format_pick=format_pick):
            h["ref_date"] = ref
            h["capper"] = str(entry.get("capper_name") or "")
            hits.append(h)
        # Fan-out copies grade independently; a split verdict on the same
        # leg of the same game means one copy is wrong.
        picks = (entry.get("parsed") or {}).get("picks") or []
        for i, pick in enumerate(picks):
            leg = lv.get(str(i)) or {}
            if leg.get("verdict") not in RESOLVED or not leg.get("game_date"):
                continue
            fp = (str(entry.get("capper_name") or "").strip().lower(),
                  re.sub(r"\s+", " ", (pick.get("description") or "").strip().lower()),
                  leg["game_date"])
            fanout.setdefault(fp, []).append((key, i, leg["verdict"]))
    for (capper, desc, gd), copies in fanout.items():
        if len({v for _, _, v in copies}) < 2:
            continue
        for key, i, verdict in copies:
            entry = cache[key]
            pick = entry["parsed"]["picks"][i]
            hits.append({
                "key": key, "idx": i, "rule": "fanout_split",
                "description": pick.get("description") or "",
                "label": "", "verdict": verdict,
                "bet_type": pick.get("bet_type") or "",
                "period": pick.get("period") or "",
                "ref_date": gd, "capper": str(entry.get("capper_name") or ""),
                "detail": "copies graded " + " / ".join(
                    f"{k}={v}" for k, _, v in sorted(copies)),
            })

    groups: dict[str, dict[str, Any]] = {}
    for h in sorted(hits, key=lambda h: (h["ref_date"], h["key"]), reverse=True):
        sk = f"anomaly:{h['rule']}:{h['key']}:{h['idx']}"
        st = state.get(sk) or {}
        if st.get("parked") or (st.get("attempts") or 0) >= attempt_cap:
            continue
        g = groups.setdefault(h["rule"], {
            "kind": "anomaly", "rule": h["rule"], "instances": [],
            "state_keys": [], "attempts": 0,
        })
        if len(g["instances"]) >= ANOMALY_GROUP_CAP:
            continue
        g["instances"].append(h)
        g["state_keys"].append(sk)
        g["attempts"] = max(g["attempts"], st.get("attempts") or 0)
    out = []
    for g in groups.values():
        first = g["instances"][0]
        g["keys"] = list(dict.fromkeys(h["key"] for h in g["instances"]))
        g["capper"] = first["capper"]
        g["ref_date"] = first["ref_date"]
        g["title"] = anomaly_title(g["rule"])
        # Shape shared with ungraded groups (the run loop reads members/legs).
        g["members"] = [{"key": first["key"], "legs": [
            {"description": first["description"]}]}]
        out.append(g)
    out.sort(key=lambda g: (g["ref_date"], g["rule"]), reverse=True)
    return out


def anomaly_title(rule: str) -> str:
    if rule.startswith("label:"):
        return f"label drops {rule.split(':', 1)[1]}"
    return ANOMALY_TITLES.get(rule, rule)


# ─── prompt ──────────────────────────────────────────────────────────────────

def _tme_link(key: str) -> str:
    try:
        ch, msg = key.split(":")
        return f"https://t.me/c/{ch.removeprefix('-100')}/{msg}"
    except ValueError:
        return ""


def _constraint_lines() -> list[str]:
    """Nightly-run overrides shared by the ungraded and anomaly prompts."""
    lines: list[str] = []
    lines.append("## Constraints — these OVERRIDE the standard /investigate workflow where they conflict")
    lines.append(
        "- You are a headless nightly agent on the VPS (as forwarder, in "
        "/home/forwarder/app); no human is available. Work directly in this "
        "repo — NO git worktree, NO SSH."
    )
    lines.append(
        "- NEVER `git push` and NEVER restart/stop `telegram-forwarder`. The "
        "audit runner pushes and restarts grade-daemon after all agents "
        "finish. If you must edit parse_cache.json entries: `sudo -n "
        "systemctl stop grade-daemon` first, `sudo -n systemctl start "
        "grade-daemon` when done (the runner re-checks it at the end)."
    )
    lines.append(
        "- Commit any code fix locally: stage ONLY the files you changed "
        "(never `git add -A`; the tree may hold unrelated work-in-progress — "
        "leave it untouched), commit message prefixed `nightly-audit:`."
    )
    lines.append(
        "- A code fix must follow the repo's invariants (CLAUDE.md + the "
        "subsystem's docs/*.md — read the doc first) and carry/extend the "
        "pinned test where one exists. Don't fix grading with prompt text. "
        "No new paid API calls; free sources only — if only a paid path "
        "could grade it, report needs_human with the cost math."
    )
    lines.append(
        "- Grade via the real pipeline where possible (targeted tracker run, "
        "`python tracker.py --live --target=<channel>:<msg>`), and verify the "
        "live message/emoji/broadcast state afterwards like the investigate "
        "workflow requires. Do not message the operator; the runner sends "
        "the summary."
    )
    lines.append(
        "- Budget ~20 minutes; the runner kills you at 25. If the root cause "
        "needs a human decision, stop early and report needs_human. Add an "
        "/investigate lesson ONLY for a novel debugging technique, never for "
        "a routine code fix."
    )
    return lines


def build_prompt(group: dict[str, Any], *, today_et: date) -> str:
    """The whole -p prompt: a /investigate invocation whose argument carries
    the facts, the mission, the nightly-run constraint overrides, and the
    machine-readable result contract the runner parses for the DM."""
    lines = []
    first = group["members"][0]
    desc0 = (first["legs"][0]["description"] or "pick")[:80] if first["legs"] else "pick"
    lines.append(
        f"/investigate NIGHTLY UNGRADED AUDIT {today_et.isoformat()}: "
        f"pick by {group['capper'] or 'unknown capper'} — “{desc0}” — still has no "
        f"verdict although its stale-reference date is {group['ref_date']}. "
        "Determine whether it SHOULD have been graded; if yes, fix the root "
        "cause in code AND grade it; if it is legitimately ungradeable, change "
        "nothing and say so."
    )
    lines.append("")
    lines.append("## Facts (from parse_cache.json)")
    if len(group["members"]) > 1:
        lines.append(
            f"- {len(group['members'])} cache entries are fan-out copies of the SAME "
            "pick (one source → several dest channels, independent cache/message/"
            "broadcast per copy). Whatever you conclude or repair must cover EVERY "
            "copy listed below, not just the first."
        )
    for m in group["members"]:
        lines.append(f"- cache key `{m['key']}` ({_tme_link(m['key'])}), posted {m['msg_date']}"
                     + (f", RETIRED by grade-daemon: {m['failed_reason']!r}" if m["failed"] else ""))
        for leg in m["legs"]:
            bits = [f"leg {leg['idx']}: {leg['description']!r}"]
            if leg["bet_type"]:
                bits.append(f"bet_type={leg['bet_type']}")
            if leg["period"]:
                bits.append(f"period={leg['period']}")
            if leg["sport"]:
                bits.append(f"sport={leg['sport']}")
            bits.append(f"game_date={leg['game_date'] or 'UNKNOWN'}")
            if leg["unknown_attempts"]:
                bits.append(f"unknown_attempts={leg['unknown_attempts']} (capped at 6)")
            lines.append("    - unresolved " + ", ".join(bits))
        for r in m["resolved"]:
            lines.append(f"    - resolved leg {r['idx']}: {r['description']!r} → {r['verdict']}")
    lines.append("")
    lines.extend(_constraint_lines())
    lines.append("")
    lines.append("## Result contract")
    lines.append(
        "End your FINAL message with exactly one line (single line, valid "
        "JSON, no code fence):"
    )
    lines.append(
        'AUDIT_RESULT: {"outcome": "graded|fixed_needs_verify|legit_ungraded|'
        'needs_human|no_issue", "issue": "<root cause, telegraph style, '
        '<=90 chars — no commit hashes, dates, or test names; the ledger '
        'holds those>", "action": "<what changed, <=90 chars, or none>"}'
    )
    return "\n".join(lines)


def build_anomaly_prompt(group: dict[str, Any], *, today_et: date) -> str:
    """The -p prompt for one anomaly rule: every instance it fired on, the
    mission (confirm, fix the CLASS, repair every live artifact — or call it a
    false positive), the shared nightly constraints, and the result contract."""
    rule = group["rule"]
    lines = [
        f"/investigate NIGHTLY ANOMALY AUDIT {today_et.isoformat()}: the "
        f"deterministic invariant `{rule}` ({group['title']}) fired on "
        f"{len(group['instances'])} already-GRADED leg(s). Their verdicts may "
        "well be right — the suspect is the label, parse shape, price, or one "
        "fan-out copy. Confirm whether each instance is a real defect; if yes, "
        "fix the whole class in code and repair every live artifact; if the "
        "rule misfired, change nothing and say why.",
        "",
        "## Instances (from parse_cache.json; label = audit._format_pick output)",
    ]
    for h in group["instances"]:
        lines.append(
            f"- `{h['key']}` leg {h['idx']} ({_tme_link(h['key'])}), capper "
            f"{h['capper'] or '?'}, ref {h['ref_date']}: {h['description']!r} "
            f"→ {h['verdict']}; bet_type={h['bet_type'] or '?'}, "
            f"period={h['period'] or '?'}; {h['detail']}")
    lines += [
        "",
        "## What \"repair every live artifact\" means here",
        "- The renderer output feeds the results broadcast (the channel's "
        "`broadcast_results_channel` in MAPPINGS_CONFIG) and Sheets: after a "
        "renderer fix, find each instance's broadcast (Telethon search of the "
        "results channel for the old label) and edit it IN PLACE via the Bot "
        "API sender (`BOT_TOKEN`, HTML, matching a known-correct analog's "
        "format), then re-seed the dedupe ledger "
        "(`scripts/seed_broadcast_lines.py`) with grade-daemon stopped.",
        "- A wrong parse field (period, bet_type, line) in the cache: fix the "
        "parse code, then correct the cached parse of each instance (daemon "
        "stopped) so the next reader sees the right shape.",
        "- A wrong verdict (fan-out split, or a verdict the corrected parse "
        "contradicts): fix grades row + leg_verdicts + message emoji + "
        "broadcast, per the investigate lessons on sibling sweeps.",
        "",
    ]
    lines.extend(_constraint_lines())
    lines += [
        "",
        "## Result contract",
        "End your FINAL message with exactly one line (single line, valid "
        "JSON, no code fence):",
        'AUDIT_RESULT: {"outcome": "repaired|fixed_needs_verify|'
        'false_positive|needs_human", "issue": "<root cause, telegraph style, '
        '<=90 chars — no commit hashes, dates, or test names>", "action": '
        '"<what changed, <=90 chars, or none; for false_positive, how the '
        'rule should be narrowed>"}',
    ]
    return "\n".join(lines)


def _prompt_for(group: dict[str, Any], *, today_et: date) -> str:
    if group.get("kind") == "anomaly":
        return build_anomaly_prompt(group, today_et=today_et)
    return build_prompt(group, today_et=today_et)


# ─── headless agent ──────────────────────────────────────────────────────────

class AgentCallError(RuntimeError):
    pass


ENVELOPE_FIELDS = (
    "subtype", "duration_ms", "duration_api_ms", "num_turns",
    "total_cost_usd", "usage", "session_id",
)


class HeadlessInvoker:
    """One fresh full-tool ``claude -p`` run; stream-json teed to a file.

    Unlike the god judge (isolated, toolless), this agent needs the whole dev
    setup: cwd = the repo (CLAUDE.md + .claude/commands/investigate.md load
    from there), permissions skipped, all tools. Its environment is still
    built from scratch: CLAUDE_CODE_OAUTH_TOKEN so the run bills the
    subscription, and deliberately NO ANTHROPIC_API_KEY (present in .env —
    the CLI would bill the API with it; the bash commands the agent runs
    don't need it inherited either, every script load_dotenv()s from disk).
    NIGHTLY_AUDIT=1 makes the session hooks (resume-notify DM,
    post-investigate stop gate) stand down. --no-session-persistence keeps
    nightly transcripts out of ~/.claude/projects, where they would poison
    the resume-notify hook's previous-session lookup; the stream file IS the
    durable transcript. --strict-mcp-config stops the Telegram channel
    plugin's MCP server from spawning under the agent — that server is a
    Bot API poller, and a second poller on the token gets the interactive
    session's poller 409'd to death (verified: the flag suppresses the bun
    spawn entirely, claude-code 2.1.266).
    """

    def __init__(self, claude_bin: str, *, oauth_token: str,
                 timeout: int = AGENT_TIMEOUT) -> None:
        if not oauth_token:
            raise ValueError("CLAUDE_CODE_OAUTH_TOKEN is required")
        self.claude_bin = claude_bin
        self.oauth_token = oauth_token
        self.timeout = timeout
        self.last_call: dict[str, Any] = {}

    def command(self, prompt: str) -> list[str]:
        return [
            self.claude_bin, "-p", prompt,
            "--dangerously-skip-permissions",
            "--model", MODEL,
            "--effort", EFFORT,
            "--max-turns", str(MAX_TURNS),
            "--output-format", "stream-json",
            "--verbose",
            "--no-session-persistence",
            # A plugin MCP server here is a second Bot API poller on the shared
            # bot token; Telegram 409s one of the two dead — usually the
            # interactive session's, whose supervisor then restarts it mid-run.
            # Agents read Telegram via Telethon scripts, never MCP.
            "--strict-mcp-config",
        ]

    def environment(self) -> dict[str, str]:
        return {
            "PATH": os.environ.get(
                "PATH",
                "/home/forwarder/.npm-global/bin:/usr/local/bin:/usr/bin:/bin",
            ),
            "HOME": os.environ.get("HOME", "/home/forwarder"),
            "TERM": "dumb",
            "LANG": "C.UTF-8",
            "CLAUDE_CODE_OAUTH_TOKEN": self.oauth_token,
            "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1",
            "NIGHTLY_AUDIT": "1",
        }

    def __call__(self, prompt: str, transcript_path: Path) -> str:
        """Returns the final result text; raises AgentCallError otherwise."""
        started = time.monotonic()
        self.last_call = {"transcript": str(transcript_path)}
        transcript_path.parent.mkdir(parents=True, exist_ok=True)
        with transcript_path.open("wb") as out:
            proc = subprocess.Popen(
                self.command(prompt),
                cwd=str(ROOT),
                env=self.environment(),
                stdin=subprocess.DEVNULL,
                stdout=out,
                stderr=subprocess.PIPE,
                start_new_session=True,  # so a timeout can kill bash children too
            )
            try:
                _, stderr = proc.communicate(timeout=self.timeout)
            except subprocess.TimeoutExpired:
                try:
                    os.killpg(proc.pid, signal.SIGKILL)
                except OSError:
                    pass
                proc.wait(timeout=30)
                self.last_call["wall_ms"] = int((time.monotonic() - started) * 1000)
                raise AgentCallError(f"agent timed out after {self.timeout}s")
        self.last_call["wall_ms"] = int((time.monotonic() - started) * 1000)
        self.last_call["exit_code"] = proc.returncode
        tail = (stderr or b"").decode("utf-8", "replace").strip()[-1000:]
        if tail:
            self.last_call["stderr_tail"] = tail
        envelope = self._result_event(transcript_path)
        if envelope is None:
            raise AgentCallError(
                f"no result event in stream (exit {proc.returncode}): "
                f"{tail or 'no stderr'}"
            )
        for field in ENVELOPE_FIELDS:
            if field in envelope:
                self.last_call[field] = envelope[field]
        result = envelope.get("result")
        if envelope.get("is_error"):
            raise AgentCallError(
                f"agent reported an error: {str(result or envelope.get('subtype'))[:500]}"
            )
        if proc.returncode != 0:
            raise AgentCallError(
                f"claude exited {proc.returncode}: {tail or 'no stderr'}"
            )
        if not isinstance(result, str) or not result.strip():
            raise AgentCallError("agent returned an empty result")
        return result

    @staticmethod
    def _result_event(transcript_path: Path) -> dict | None:
        event = None
        try:
            with transcript_path.open(encoding="utf-8", errors="replace") as fh:
                for line in fh:
                    line = line.strip()
                    if not line.startswith("{"):
                        continue
                    try:
                        obj = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if isinstance(obj, dict) and obj.get("type") == "result":
                        event = obj
        except OSError:
            return None
        return event


_AUDIT_RE = re.compile(r"AUDIT_RESULT:\s*(\{.*?\})\s*$", re.MULTILINE | re.DOTALL)


def parse_audit_result(result_text: str) -> dict[str, str]:
    """Last AUDIT_RESULT line of the agent's final message, validated; an
    unparseable report degrades to outcome=unparsed with the tail as issue."""
    matches = _AUDIT_RE.findall(result_text or "")
    for raw in reversed(matches):
        try:
            obj = json.loads(raw)
        except json.JSONDecodeError:
            continue
        if isinstance(obj, dict) and obj.get("outcome") in OUTCOMES:
            return {
                "outcome": str(obj["outcome"]),
                "issue": str(obj.get("issue") or "").strip()[:300],
                "action": str(obj.get("action") or "").strip()[:300],
            }
    tail = re.sub(r"\s+", " ", (result_text or "").strip())[-200:]
    return {"outcome": "unparsed", "issue": tail, "action": ""}


# ─── state / ledger / git / DM ───────────────────────────────────────────────

def load_state() -> dict:
    try:
        data = json.loads(STATE_FILE.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def save_state(state: dict) -> None:
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = STATE_FILE.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(state, indent=2, sort_keys=True) + "\n",
                   encoding="utf-8")
    os.replace(tmp, STATE_FILE)


def record_attempt(state: dict, keys: list[str], outcome: str,
                   *, attempt_cap: int) -> bool:
    """Bump every copy of the pick; park terminal outcomes and capped
    attempts. Returns True when the group just got parked."""
    parked = False
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    for key in keys:
        st = state.get(key) or {}
        st["attempts"] = (st.get("attempts") or 0) + 1
        st["last_run"] = now
        st["last_outcome"] = outcome
        if outcome in PARK_OUTCOMES:
            st["parked"] = True
            st["parked_reason"] = outcome
            parked = True
        elif st["attempts"] >= attempt_cap:
            st["parked"] = True
            st["parked_reason"] = f"attempt cap ({st['attempts']})"
            parked = True
        state[key] = st
    return parked


def append_runs_log(path: Path, record: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(record, sort_keys=True, ensure_ascii=False) + "\n")


def _git(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git", *args], cwd=str(ROOT), capture_output=True,
                          text=True, timeout=120)


def git_head() -> str:
    return _git("rev-parse", "HEAD").stdout.strip()


def git_dirty_paths() -> set[str]:
    out = _git("status", "--porcelain").stdout
    return {line[3:].strip() for line in out.splitlines() if line.strip()}


def git_commits_between(old: str, new: str) -> list[str]:
    if not old or not new or old == new:
        return []
    out = _git("log", "--oneline", f"{old}..{new}").stdout
    return [line.strip() for line in out.splitlines() if line.strip()]


def git_changed_files(old: str, new: str) -> list[str]:
    if not old or not new or old == new:
        return []
    out = _git("diff", "--name-only", old, new).stdout
    return [line.strip() for line in out.splitlines() if line.strip()]


def git_revert_paths(paths: set[str]) -> list[str]:
    """Discard a failed agent's uncommitted edits to TRACKED files only —
    never paths that were already dirty before it started, never untracked
    files (those are left in place and reported)."""
    reverted = []
    for path in sorted(paths):
        tracked = _git("ls-files", "--error-unmatch", path).returncode == 0
        if tracked:
            if _git("checkout", "--", path).returncode == 0:
                reverted.append(path)
    return reverted


def send_watchdog_dm(text: str, *, as_html: bool = False,
                     reply_markup: dict | None = None) -> bool:
    """DM the operator through the watchdog bot (same send as god_judge_runner).

    as_html sends Bot API HTML (expandable blockquotes need it); a rejected
    payload falls back to a tag-stripped plain send so the report still lands.
    reply_markup is a raw Bot API inline keyboard dict (verdict callbacks are
    handled by claude-watchdog.service; copy_text buttons are client-side).
    """
    token = os.environ.get("WATCHDOG_BOT_TOKEN", "")
    uid = os.environ.get("WATCHDOG_USER_ID", "")
    if not token or not uid:
        print("WATCHDOG_BOT_TOKEN / WATCHDOG_USER_ID not set", file=sys.stderr)
        return False
    url = f"https://api.telegram.org/bot{token}/sendMessage"

    def _post(payload: dict[str, str]) -> bool:
        if reply_markup:
            payload["reply_markup"] = json.dumps(reply_markup)
        data = urllib.parse.urlencode(payload).encode()
        with urllib.request.urlopen(
            urllib.request.Request(url, data=data), timeout=20
        ) as response:
            return response.status == 200

    try:
        if as_html:
            try:
                return _post({"chat_id": uid, "text": text,
                              "parse_mode": "HTML",
                              "disable_web_page_preview": "true"})
            except Exception as exc:
                print(f"HTML send failed ({exc}), retrying plain",
                      file=sys.stderr)
                text = html.unescape(re.sub(r"<[^>]+>", "", text))
        return _post({"chat_id": uid, "text": text})
    except Exception as exc:
        print(f"send failed: {exc}", file=sys.stderr)
        return False


OUTCOME_BADGE = {  # (emoji, label) per AUDIT_RESULT outcome
    "graded": ("✅", "graded"),
    "fixed_needs_verify": ("🔧", "fixed — verify tomorrow"),
    "legit_ungraded": ("⚪", "legit ungraded"),
    "needs_human": ("🙋", "NEEDS HUMAN"),
    "no_issue": ("👌", "already resolved"),
    "repaired": ("🛠", "repaired"),
    "false_positive": ("🙈", "rule misfired"),
    "unparsed": ("⚠️", "ran, report unparsed"),
    "error": ("❌", "agent failed"),
    "timeout": ("⏱", "agent timed out"),
}


# Outcomes whose pick is already settled — no WIN/LOSS/PUSH buttons.
SETTLED_OUTCOMES = ("graded", "no_issue")


def _card_id(run_date: str, primary_key: str) -> str:
    return hashlib.sha1(f"{run_date}|{primary_key}".encode()).hexdigest()[:10]


def _card_key(r: dict) -> str:
    """What a card is ABOUT: the primary cache key, or the rule for an anomaly
    card (its instances can share a key with an ungraded card the same night)."""
    if r.get("kind") == "anomaly":
        return f"anomaly:{r.get('rule')}"
    return (r.get("keys") or [""])[0]


def _transcript_stem(r: dict) -> str:
    return _card_key(r).replace(":", "_")


def _follow_up_prompt(r: dict, *, run_date: str) -> str:
    """Short prompt the operator can paste at the Claude session to follow up
    on this pick's audit (copy_text buttons cap at 256 chars)."""
    primary = (r.get("keys") or [""])[0]
    safe_key = _transcript_stem(r)
    return (f"inv follow up nightly audit {run_date}: "
            f"{r.get('capper') or '?'} — {(r.get('desc') or 'pick')[:48]} | "
            f"key {primary} | outcome {r['outcome']} | transcript "
            f"logs/ungraded_audit/{run_date}/{safe_key}.stream.jsonl")[:256]


def compose_header(results: list[dict], notes: list[str], *,
                   run_date: str) -> str:
    """Run summary: outcome tally + the runner's push/restart/⚠ notes.
    Static ledger/transcript paths stay out — they never change."""
    esc = html.escape
    tally: dict[str, int] = {}
    for r in results:
        tally[r["outcome"]] = tally.get(r["outcome"], 0) + 1
    bits = " ".join(f"{OUTCOME_BADGE.get(o, ('❓', o))[0]}{n}"
                    for o, n in sorted(tally.items()))
    lines = [f"pickbot: nightly audit {run_date} — "
             f"{len(results)} pick(s): {bits}".rstrip(": ")]
    lines.extend(esc(n) for n in notes)
    return "\n".join(lines)


def compose_card(r: dict, *, run_date: str) -> tuple[str, dict]:
    """One audited pick group → (Bot API HTML, inline keyboard).

    Headline links the pick's Telegram message; the agent's issue/action
    prose collapses into a <blockquote expandable> (desk-card pattern).
    Still-unresolved picks get WIN/LOSS/PUSH buttons (handled by
    claude-watchdog.service → scripts/audit_mark.py); every card gets a
    copy_text follow-up prompt button."""
    esc = html.escape
    keys = r.get("keys") or []
    primary = keys[0] if keys else ""
    desc = (r.get("desc") or "pick")[:48]
    copies = f", ×{r['n_keys']}" if r.get("n_keys", 1) > 1 else ""
    emoji, label = OUTCOME_BADGE.get(r["outcome"], ("❓", r["outcome"]))
    title = f"{esc(r.get('capper') or '?')} — {esc(desc)}"
    if r.get("kind") == "anomaly":
        title = f"{esc(r.get('title') or r.get('rule') or 'anomaly')}: {title}"
    link = _tme_link(primary)
    if link:
        title = f'<a href="{link}">{title}</a>'
    line = f"{emoji} <b>{title}</b> ({esc(r['ref_date'])}{copies}) — {label}"
    if r.get("commits"):
        line += f" · {len(r['commits'])} commit(s)"
    if r.get("parked"):
        line += " [parked]"
    lines = [line]
    extra = [f'<a href="{_tme_link(k)}">{"#" if r.get("kind") == "anomaly" else "copy "}{i}</a>'
             for i, k in enumerate(keys[1:], 2) if _tme_link(k)]
    if extra:
        lines.append(("also: " if r.get("kind") == "anomaly" else "fan-out: ")
                     + " · ".join(extra))
    detail = esc(r.get("issue") or "").strip()
    if r.get("action") and r["action"].lower() not in ("", "none"):
        detail += ("\n→ " if detail else "→ ") + esc(r["action"])
    if detail:
        lines.append(f"<blockquote expandable>{detail}</blockquote>")

    cid = _card_id(run_date, _card_key(r))
    rows = []
    # Anomaly picks already carry a verdict — a verdict tap would be a no-op.
    if r["outcome"] not in SETTLED_OUTCOMES and r.get("kind") != "anomaly":
        rows.append([
            {"text": "✅ Win", "callback_data": f"aud:{cid}:W"},
            {"text": "❌ Loss", "callback_data": f"aud:{cid}:L"},
            {"text": "🟨 Push", "callback_data": f"aud:{cid}:P"},
        ])
    rows.append([{"text": "📋 Follow-up prompt",
                  "copy_text": {"text": _follow_up_prompt(r, run_date=run_date)}}])
    return "\n".join(lines), {"inline_keyboard": rows}


def register_cards(cards: list[dict], *, path: Path = CARDS_FILE) -> None:
    """Persist card_id → group facts so audit_mark.py can resolve a button
    tap (callback_data caps at 64 bytes — too small for the keys). The card's
    html/keyboard ride along so the bot can re-render after marking."""
    try:
        reg = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(reg, dict):
            reg = {}
    except (OSError, json.JSONDecodeError):
        reg = {}
    floor = (datetime.now(ET).date()
             - timedelta(days=CARD_RETENTION_DAYS)).isoformat()
    reg = {cid: c for cid, c in reg.items()
           if isinstance(c, dict) and str(c.get("run_date") or "") >= floor}
    for c in cards:
        r = c["r"]
        reg[c["card_id"]] = {
            "run_date": c["run_date"], "keys": r.get("keys") or [],
            "capper": r.get("capper") or "", "desc": r.get("desc") or "",
            "outcome": r["outcome"], "html": c["html"],
            "keyboard": c["markup"], "marked": None,
        }
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(reg, indent=2, sort_keys=True) + "\n",
                   encoding="utf-8")
    os.replace(tmp, path)


def prune_old_transcripts(root: Path, *, today_et: date) -> None:
    if not root.is_dir():
        return
    floor = (today_et - timedelta(days=TRANSCRIPT_RETENTION_DAYS)).isoformat()
    for child in root.iterdir():
        if child.is_dir() and re.fullmatch(r"\d{4}-\d{2}-\d{2}", child.name) \
                and child.name < floor:
            shutil.rmtree(child, ignore_errors=True)


# ─── run ─────────────────────────────────────────────────────────────────────

def run(args: argparse.Namespace) -> int:
    today_et = datetime.now(ET).date()
    run_date = today_et.isoformat()
    runs_log = Path(args.runs_log)

    try:
        cache = json.loads(CACHE_FILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        print(f"cannot read {CACHE_FILE}: {exc}", file=sys.stderr)
        return 1
    state = load_state()

    if args.target:
        groups = []
        for key in args.target:
            entry = cache.get(key)
            if not isinstance(entry, dict) or "parsed" not in entry:
                print(f"--target {key}: no such cache entry", file=sys.stderr)
                return 1
            picks = (entry.get("parsed") or {}).get("picks") or []
            lv = entry.get("leg_verdicts") or {}
            indices = _unresolved_indices(picks, lv) or list(range(len(picks)))
            msg_date = str(entry.get("msg_date") or "")[:10]
            groups.append({
                "capper": str(entry.get("capper_name") or ""),
                "ref_date": _stale_reference_date(
                    lv, entry.get("odds_by_pick") or {}, msg_date),
                "members": [{
                    "key": key, "msg_date": msg_date,
                    "failed": bool(entry.get("_failed")),
                    "failed_reason": str(entry.get("_failed_reason") or ""),
                    "legs": _leg_facts(entry, indices),
                    "resolved": [],
                }],
                "keys": [key], "attempts": 0,
            })
    else:
        groups = scan(cache, state, today_et=today_et,
                      days_back=args.days_back, attempt_cap=args.attempt_cap)
    anomalies: list[dict[str, Any]] = []
    if not args.target and args.max_anomalies > 0:
        anomalies = scan_anomalies(cache, state, today_et=today_et,
                                   days_back=args.days_back,
                                   attempt_cap=args.attempt_cap)

    append_runs_log(runs_log, {
        "logged_at_utc": datetime.now(timezone.utc).isoformat(),
        "kind": "scan", "run_date": run_date, "dry_run": args.dry_run,
        "groups": len(groups),
        "keys": [g["keys"] for g in groups],
        "anomalies": [{"rule": g["rule"], "instances": g["state_keys"]}
                      for g in anomalies],
    })
    print(f"scan: {len(groups)} candidate group(s) "
          f"({sum(len(g['keys']) for g in groups)} cache keys), "
          f"{len(anomalies)} anomaly rule(s) firing")

    # Ungraded picks first — anomaly agents only use what the budget leaves.
    picked = groups[: args.max_picks] + anomalies[: args.max_anomalies]
    if args.dry_run:
        for g in groups + anomalies:
            marker = "RUN " if g in picked else "wait"
            desc = g["members"][0]["legs"][0]["description"][:60] if g["members"][0]["legs"] else ""
            what = f"[{g['rule']}] " if g.get("kind") == "anomaly" else ""
            print(f"  [{marker}] {what}{g['capper'] or '?'} — {desc!r} ref {g['ref_date']} "
                  f"keys {g['keys']} attempts {g['attempts']}")
        for first in ([g for g in picked if g.get("kind") != "anomaly"][:1]
                      + [g for g in picked if g.get("kind") == "anomaly"][:1]):
            print("\n--- prompt for first group"
                  + (" (anomaly)" if first.get("kind") == "anomaly" else "") + " ---")
            print(_prompt_for(first, today_et=today_et))
        return 0

    if not picked:
        print("nothing to audit — clean night")
        prune_old_transcripts(TRANSCRIPT_ROOT, today_et=today_et)
        return 0

    oauth = os.environ.get("CLAUDE_CODE_OAUTH_TOKEN", "")
    invoker = HeadlessInvoker(args.claude_bin, oauth_token=oauth,
                              timeout=args.agent_timeout)
    deadline = time.monotonic() + BUDGET_MIN * 60
    head0 = git_head()
    results: list[dict[str, Any]] = []
    notes: list[str] = []

    for group in picked:
        if time.monotonic() > deadline - args.agent_timeout - 60:
            notes.append(f"⏱ budget exhausted — "
                         f"{len(picked) - len(results)} group(s) postponed")
            break
        desc = (group["members"][0]["legs"][0]["description"]
                if group["members"][0]["legs"] else "")
        record: dict[str, Any] = {
            "logged_at_utc": datetime.now(timezone.utc).isoformat(),
            "kind": "agent", "run_date": run_date,
            "keys": group["keys"], "capper": group["capper"],
            "ref_date": group["ref_date"], "desc": desc,
        }
        if group.get("kind") == "anomaly":
            record["kind"] = "anomaly_agent"
            record["rule"] = group["rule"]
            record["instances"] = group["state_keys"]
        safe_key = _transcript_stem(group)
        transcript = TRANSCRIPT_ROOT / run_date / f"{safe_key}.stream.jsonl"
        prompt = _prompt_for(group, today_et=today_et)
        pre_head, pre_dirty = git_head(), git_dirty_paths()
        print(f"→ agent for {group['keys']} ({group['capper']!r}, ref {group['ref_date']})")
        started = time.monotonic()
        try:
            result_text: str | None = invoker(prompt, transcript)
            record["status"] = "ok"
        except AgentCallError as exc:
            result_text = None
            record["status"] = "error"
            record["error"] = str(exc)
        record["wall_ms"] = int((time.monotonic() - started) * 1000)
        record.update(invoker.last_call)

        post_head, post_dirty = git_head(), git_dirty_paths()
        record["commits"] = git_commits_between(pre_head, post_head)
        record["changed_files"] = git_changed_files(pre_head, post_head)
        leftover = post_dirty - pre_dirty

        if result_text is not None:
            (transcript.parent / f"{safe_key}.result.md").write_text(
                result_text + "\n", encoding="utf-8")
            audit = parse_audit_result(result_text)
        else:
            audit = {
                "outcome": "timeout" if "timed out" in record.get("error", "")
                else "error",
                "issue": record.get("error", ""), "action": "",
            }
        record.update(audit)

        if audit["outcome"] in ("error", "timeout", "unparsed") and leftover:
            record["reverted"] = git_revert_paths(leftover)
            still = sorted(leftover - set(record["reverted"]))
            if still:
                record["leftover_untracked"] = still
                notes.append(f"⚠ {group['keys'][0]}: failed agent left "
                             f"untracked files: {', '.join(still[:5])}")
        elif leftover:
            record["uncommitted_leftover"] = sorted(leftover)
            notes.append(f"⚠ {group['keys'][0]}: agent left uncommitted "
                         f"changes: {', '.join(sorted(leftover)[:5])}")

        parked = record_attempt(state, group.get("state_keys") or group["keys"],
                                audit["outcome"],
                                attempt_cap=args.attempt_cap)
        save_state(state)
        record["parked"] = parked
        append_runs_log(runs_log, record)
        results.append({
            "capper": group["capper"], "desc": desc,
            "ref_date": group["ref_date"], "n_keys": len(group["keys"]),
            "keys": list(group["keys"]),
            "outcome": audit["outcome"], "issue": audit["issue"],
            "action": audit["action"], "commits": record["commits"],
            "parked": parked,
            "kind": group.get("kind") or "ungraded",
            "rule": group.get("rule"), "title": group.get("title"),
        })
        print(f"  ← {audit['outcome']}: {audit['issue'][:120]}")

    # ── one push, one daemon restart, at the end ────────────────────────────
    head1 = git_head()
    if head1 != head0:
        if args.no_push:
            notes.append(f"{len(git_commits_between(head0, head1))} commit(s) "
                         "NOT pushed (--no-push)")
        else:
            push = _git("push")
            if push.returncode == 0:
                notes.append(f"pushed {len(git_commits_between(head0, head1))} "
                             "commit(s)")
            else:
                notes.append("⚠ git push FAILED: "
                             + (push.stderr or push.stdout).strip()[-200:])
        restart = subprocess.run(
            ["sudo", "-n", "systemctl", "restart", "grade-daemon"],
            capture_output=True, text=True, timeout=120)
        notes.append("grade-daemon restarted" if restart.returncode == 0
                     else f"⚠ grade-daemon restart failed: {restart.stderr.strip()[-200:]}")
        touched = set()
        for r in results:
            for c in git_changed_files(head0, head1):
                touched.add(c)
        if "listener.py" in touched:
            notes.append("⚠ listener.py changed — telegram-forwarder NOT "
                         "auto-restarted, deploy it yourself")
    else:
        active = subprocess.run(
            ["systemctl", "is-active", "--quiet", "grade-daemon"])
        if active.returncode != 0:
            started_ok = subprocess.run(
                ["sudo", "-n", "systemctl", "start", "grade-daemon"],
                capture_output=True, text=True, timeout=120)
            notes.append("⚠ grade-daemon was down — restarted" if started_ok.returncode == 0
                         else "⚠ grade-daemon DOWN and restart failed")

    prune_old_transcripts(TRANSCRIPT_ROOT, today_et=today_et)

    header = compose_header(results, notes, run_date=run_date)
    cards = []
    for r in results:
        card_html, markup = compose_card(r, run_date=run_date)
        cards.append({"card_id": _card_id(run_date, _card_key(r)),
                      "run_date": run_date, "html": card_html,
                      "markup": markup, "r": r})
    print("---\n" + header)
    for c in cards:
        print("---\n" + c["html"])
    if results or any(n.startswith("⚠") for n in notes):
        if args.no_dm:
            print("(DM suppressed by --no-dm)")
        else:
            register_cards(cards)
            send_watchdog_dm(header, as_html=True)
            for c in cards:
                send_watchdog_dm(c["html"], as_html=True,
                                 reply_markup=c["markup"])
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--max-picks", type=int, default=DEFAULT_MAX_PICKS,
                        help="pick groups per night (default %(default)s)")
    parser.add_argument("--max-anomalies", type=int, default=DEFAULT_MAX_ANOMALIES,
                        help="graded-pick anomaly rules per night, after the "
                             "ungraded picks (default %(default)s; 0 = off)")
    parser.add_argument("--days-back", type=int, default=DEFAULT_DAYS_BACK,
                        help="reference-date window (default %(default)s)")
    parser.add_argument("--attempt-cap", type=int, default=DEFAULT_ATTEMPT_CAP,
                        help="nights per pick before parking (default %(default)s)")
    parser.add_argument("--agent-timeout", type=int, default=AGENT_TIMEOUT,
                        help="seconds per agent (default %(default)s)")
    parser.add_argument("--claude-bin", default=DEFAULT_CLAUDE_BIN)
    parser.add_argument("--runs-log", default=str(
        Path(os.environ.get("UNGRADED_AUDIT_RUNS_LOG") or DEFAULT_RUNS_LOG)))
    parser.add_argument("--dry-run", action="store_true",
                        help="scan + print plan and first prompt; run nothing")
    parser.add_argument("--target", action="append", default=[],
                        help="cache key to audit regardless of scan/state "
                             "(repeatable)")
    parser.add_argument("--rearm", action="append", default=[],
                        help="clear state for a cache key, then exit")
    parser.add_argument("--list-state", action="store_true")
    parser.add_argument("--no-dm", action="store_true")
    parser.add_argument("--no-push", action="store_true")
    args = parser.parse_args(argv)

    if os.environ.get("UNGRADED_AUDIT_DISABLED"):
        print("UNGRADED_AUDIT_DISABLED is set — exiting")
        return 0
    if args.list_state:
        print(json.dumps(load_state(), indent=2, sort_keys=True))
        return 0
    if args.rearm:
        state = load_state()
        for key in args.rearm:
            if state.pop(key, None) is not None:
                print(f"re-armed {key}")
            else:
                print(f"{key} had no state")
        save_state(state)
        return 0
    if not args.dry_run and not os.environ.get("CLAUDE_CODE_OAUTH_TOKEN"):
        parser.error("CLAUDE_CODE_OAUTH_TOKEN is not set (agents bill the "
                     "subscription through it; --dry-run works without)")

    LOCK_FILE.parent.mkdir(parents=True, exist_ok=True)
    with LOCK_FILE.open("w") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            print("another ungraded-audit run holds the lock — exiting")
            return 0
        return run(args)


if __name__ == "__main__":
    sys.exit(main())
