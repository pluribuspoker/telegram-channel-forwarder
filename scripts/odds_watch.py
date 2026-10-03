#!/usr/bin/env python3
"""Odds watch — judge flagged prices, find missing ones, and auto-repair.

The operator used to spot a wrong odds tag ("[+245]" on a spread-and-ML that
no book prices near that) and ask for an /investigate. This does that loop
unattended. Two ways in:

- **Event-driven (primary):** the tracker's pre-publish gate (odds_gate.py)
  reviews every freshly priced message BEFORE its tags post — the free checks
  in odds_checks.py plus one subscription-billed Claude review. A suspicious
  leg posts WITHOUT its tag (`hold` in odds_by_pick) and the gate starts
  odds-watch.service at once; a due miss (no price found) starts it too.
- **odds-watch.timer (15 min, backstop):** the same free scan over the last
  36 h — catches what the trigger missed and releases stale holds.

Each pass hands every new flag to ONE headless `claude -p "/investigate …"`
agent (Opus 5.5 high, subscription-billed — the hc-repair chassis) that
JUDGES first. A long price can be right; `legit` is remembered per instance
at that price and never re-flagged. A real error gets every copy repaired
and the class fixed in code with a pinned test. A miss gets a FREE source
found and wired into the code; `no_free_source` parks the miss CLASS
(`prop_stat_unsupported(BTTS)` …) for ODDS_WATCH_CLASS_PARK_DAYS (14) so it
isn't re-asked per pick. After the verdicts the RUNNER releases holds
(everything but needs_human) and re-runs the targeted tracker so the tag
posts. Card via the watchdog bot only when something was wrong/missing or
failed — an all-legit pass is silent.

Guards (state logs/odds_watch_state.json; per instance `<key>:<rule>:<leg>`
+ the price it fired on, per class `class:<match_type>`):
- kill switch ODDS_WATCH_DISABLED=1 → no agent (holds still release)
- flock logs/.odds_watch.lock (one agent at a time; a trigger during a run is
  picked up by the post-run rescan, up to MAX_ROUNDS per invocation); stands
  down while the nightly audit holds its lock (both edit the cache + repo)
- ODDS_WATCH_ATTEMPT_CAP (2) runs per instance → parked
- ODDS_WATCH_DAILY_CAP (8) agents per rolling 24h; ODDS_WATCH_MAX_MESSAGES (4)
- a hold no agent will judge (parked/capped/disabled) or older than
  ODDS_WATCH_HOLD_MAX_MINUTES (90) is released — fail open, a missing tag is
  worse than an unjudged one; needs_human holds stay

The runner pushes the agent's `odds-watch:` commits once and restarts
grade-daemon on a code change (never telegram-forwarder). Ledger
logs/odds_watch_runs.jsonl + logs/odds_watch/<stamp>/ transcripts.
Manual: --dry-run (flags + prompt; spawns/writes nothing), --scan [--days N]
(print every flag over the cache, no state), --rearm [KEY], --no-push.
Test: scripts/test_odds_watch.py.
"""

import argparse
import fcntl
import html
import json
import os
import re
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scripts import ungraded_audit as ua  # noqa: E402  (loads .env/.env.local)
import odds_checks as oc  # noqa: E402

ROOT = ua.ROOT
CACHE_FILE = ROOT / "parse_cache.json"
STATE_FILE = ROOT / "logs" / "odds_watch_state.json"
LOCK_FILE = ROOT / "logs" / ".odds_watch.lock"
RUNS_LOG = ROOT / "logs" / "odds_watch_runs.jsonl"
OUT_DIR = ROOT / "logs" / "odds_watch"
PYTHON = os.environ.get("ODDS_WATCH_PYTHON") or "/home/forwarder/venv/bin/python"

MODEL = os.environ.get("ODDS_WATCH_MODEL") or "claude-opus-5-5"
EFFORT = os.environ.get("ODDS_WATCH_EFFORT") or "high"
AGENT_TIMEOUT = int(os.environ.get("ODDS_WATCH_AGENT_TIMEOUT") or 1500)
ATTEMPT_CAP = int(os.environ.get("ODDS_WATCH_ATTEMPT_CAP") or 2)
DAILY_CAP = int(os.environ.get("ODDS_WATCH_DAILY_CAP") or 8)
MAX_MESSAGES = int(os.environ.get("ODDS_WATCH_MAX_MESSAGES") or 4)
LOOKBACK_HOURS = float(os.environ.get("ODDS_WATCH_LOOKBACK_HOURS") or 36)
CLASS_PARK_DAYS = float(os.environ.get("ODDS_WATCH_CLASS_PARK_DAYS") or 14)
HOLD_MAX_MINUTES = float(os.environ.get("ODDS_WATCH_HOLD_MAX_MINUTES") or 90)
MAX_ROUNDS = 3

RULE_TITLES = oc.RULE_TITLES
OUTCOMES = (
    "fixed",           # wrong/missing: post + cache repaired AND the class fixed in code
    "repaired",        # wrong/missing: post + cache repaired, no code change needed/possible
    "legit",           # the price is right (long shot, alternate, real market)
    "no_free_source",  # a miss no free source can price (its class parks)
    "needs_human",     # wrong but unfixable unattended (paid-only source, decision)
)
WRONG_OUTCOMES = ("fixed", "repaired", "needs_human", "no_free_source")
BADGE = {
    "fixed": "✅", "repaired": "🔧", "legit": "👌", "needs_human": "🙋",
    "no_free_source": "🚫", "unparsed": "⚠️", "error": "❌", "timeout": "⏱",
}


class WatchInvoker(ua.HeadlessInvoker):
    """The audit's invoker (OAuth-only env, hook standdown, strict MCP,
    stream-json transcript, killpg on timeout) with our model/effort."""

    def command(self, prompt: str) -> list[str]:
        cmd = super().command(prompt)
        cmd[cmd.index("--model") + 1] = MODEL
        cmd[cmd.index("--effort") + 1] = EFFORT
        return cmd


# ─── pure logic (tested offline) ─────────────────────────────────────────────

def recent_keys(cache: dict, now: datetime, lookback_hours: float) -> list[str]:
    """Priced (or missed) entries whose msg_date — a calendar date — falls
    inside the lookback."""
    floor = (now - timedelta(hours=lookback_hours)).date().isoformat()
    out = []
    for key, entry in cache.items():
        if (not isinstance(entry, dict) or entry.get("_dupe")
                or not entry.get("odds_by_pick") or "parsed" not in entry):
            continue
        if str(entry.get("msg_date") or "")[:10] >= floor:
            out.append(key)
    return out


def scan(cache: dict, keys: list[str], now: datetime | None = None) -> list[dict[str, Any]]:
    hits = []
    for key in keys:
        hits.extend(oc.scan_entry(key, cache[key], now=now))
    hits.extend(oc.scan_fanout(cache, keys))
    return hits


def instance_key(h: dict) -> str:
    return f"{h['key']}:{h['rule']}:{h['idx']}"


def class_key(h: dict) -> str | None:
    return f"class:{h['miss_class']}" if h.get("miss_class") else None


def gate(state: dict, h: dict, now: datetime | None = None) -> tuple[str, str]:
    """(action, reason): "run" | "skip"."""
    now = now or datetime.now(timezone.utc)
    ck = class_key(h)
    if ck:
        until = oc.ts((state.get(ck) or {}).get("parked_until"))
        if until and now < until:
            return "skip", f"class {h['miss_class']} has no free source (until {until:%m-%d})"
    st = state.get(instance_key(h)) or {}
    if st.get("parked"):
        return "skip", f"parked ({st.get('parked_reason')})"
    same_price = st.get("price") == h["price"]
    if st.get("last_outcome") == "legit" and same_price:
        return "skip", "judged legit at this price"
    if st.get("last_outcome") in WRONG_OUTCOMES and same_price:
        return "skip", f"{st['last_outcome']} at this price"
    if int(st.get("attempts") or 0) >= ATTEMPT_CAP and same_price:
        return "skip", "attempt cap"
    return "run", "ok"


def group_by_message(hits: list[dict], cache: dict) -> list[dict[str, Any]]:
    """One target per SOURCE message (fan-out copies together — the agent
    must repair every copy). Held prices first (a tag is hidden until the
    verdict), then newest message first."""
    groups: dict[str, dict[str, Any]] = {}
    for h in hits:
        entry = cache.get(h["key"]) or {}
        src = str(entry.get("_source_key") or h["key"])
        g = groups.setdefault(src, {"source": src, "keys": [], "hits": [],
                                    "msg_date": str(entry.get("msg_date") or ""),
                                    "capper": str(entry.get("capper_name") or "")})
        if h["key"] not in g["keys"]:
            g["keys"].append(h["key"])
        g["hits"].append(h)
        g["msg_date"] = max(g["msg_date"], str(entry.get("msg_date") or ""))
    return sorted(groups.values(), key=lambda g: (
        any(h["rule"] == "hold" for h in g["hits"]), g["msg_date"]), reverse=True)


def daily_spawns(state: dict, now: datetime) -> int:
    recent = [s for s in state.get("_spawns", [])
              if (t := oc.ts(s)) and now - t < timedelta(hours=24)]
    state["_spawns"] = recent
    return len(recent)


def record_spawn(state: dict, hits: list[dict], now: datetime) -> None:
    stamp = now.isoformat(timespec="seconds")
    state.setdefault("_spawns", []).append(stamp)
    for h in hits:
        st = state.setdefault(instance_key(h), {})
        if "price" in st and st.get("price") != h["price"]:
            st.pop("attempts", None)  # a re-priced instance is a new question
            st.pop("last_outcome", None)
        st.update(price=h["price"], last_spawn_at=stamp,
                  attempts=int(st.get("attempts") or 0) + 1)


def settle(state: dict, h: dict, outcome: str, now: datetime) -> None:
    st = state.setdefault(instance_key(h), {})
    st["last_outcome"] = outcome
    if outcome == "no_free_source" and class_key(h):
        state[class_key(h)] = {
            "parked_until": (now + timedelta(days=CLASS_PARK_DAYS)).isoformat(timespec="seconds"),
            "since": now.isoformat(timespec="seconds"), "example": h["key"]}
    elif outcome in ("needs_human", "no_free_source"):
        st.update(parked=True, parked_reason=outcome)
    elif int(st.get("attempts") or 0) >= ATTEMPT_CAP and outcome not in OUTCOMES:
        st.update(parked=True, parked_reason=f"attempt cap ({st['attempts']})")


def release_due(hits: list[dict], state: dict, now: datetime, *,
                agent_keys: set[str]) -> list[dict]:
    """Hold hits to release WITHOUT an agent verdict: any hold the coming
    agent (if any) won't judge — parked, capped, cap/kill switch, or simply
    not in this pass's batch — once it is older than HOLD_MAX_MINUTES, and
    immediately when the gate says no agent will ever look at it.
    needs_human holds stay — that price was judged wrong."""
    out = []
    for h in hits:
        if h["rule"] != "hold" or instance_key(h) in agent_keys:
            continue
        st = state.get(instance_key(h)) or {}
        if st.get("parked_reason") == "needs_human":
            continue
        at = oc.ts(h.get("hold_at"))
        stale = bool(at and now - at > timedelta(minutes=HOLD_MAX_MINUTES))
        action, _ = gate(state, h, now)
        if stale or action == "skip":
            out.append(h)
    return out


_RESULT_RE = re.compile(r"ODDS_WATCH_RESULT:\s*(\[.*?\])\s*$", re.MULTILINE | re.DOTALL)


def parse_results(result_text: str, groups: list[dict]) -> dict[str, dict]:
    """source -> {outcome, issue, action}; an unreported message = unparsed."""
    reports: dict[str, dict] = {}
    for raw in reversed(_RESULT_RE.findall(result_text or "")):
        try:
            items = json.loads(raw)
        except json.JSONDecodeError:
            continue
        if isinstance(items, list):
            for it in items:
                if isinstance(it, dict) and it.get("outcome") in OUTCOMES:
                    reports.setdefault(str(it.get("message") or ""), it)
            break
    tail = re.sub(r"\s+", " ", (result_text or "").strip())[-200:]
    out = {}
    for g in groups:
        it = reports.get(g["source"])
        if it is None:
            it = next((reports[k] for k in g["keys"] if k in reports), None)
        if it is None and len(groups) == 1 and len(reports) == 1:
            it = next(iter(reports.values()))
        if it is None:
            out[g["source"]] = {"outcome": "unparsed", "issue": tail, "action": ""}
        else:
            out[g["source"]] = {"outcome": it["outcome"],
                                "issue": str(it.get("issue") or "").strip()[:400],
                                "action": str(it.get("action") or "").strip()[:400]}
    return out


def _tme(key: str) -> str:
    return ua._tme_link(key)


def build_prompt(groups: list[dict], cache: dict, *, now_et: str, head: str) -> str:
    has_miss = any(h["rule"] == "miss" for g in groups for h in g["hits"])
    has_hold = any(h["rule"] == "hold" for g in groups for h in g["hits"])
    lines = [
        f"/investigate ODDS WATCH {now_et}: {len(groups)} forwarded message(s) "
        "flagged — a price that MAY be wrong, and/or a pick we found NO price "
        "for. The checks are deliberately loose: long prices are often right. "
        "For EACH message, first JUDGE; only a real error or a findable price "
        "gets work.",
        "",
        "## Flagged messages",
    ]
    for g in groups:
        lines.append(f"### {g['source']}  ({g['capper'] or 'unknown capper'})")
        for key in g["keys"]:
            entry = cache.get(key) or {}
            lines.append(f"- copy {key} {_tme(key)} msg_date {entry.get('msg_date')}"
                         + (" has_media (if it is a bet slip, the slip is ground "
                            "truth for the price)" if entry.get("has_media") else ""))
        for h in g["hits"]:
            leg = "ticket" if h["idx"] < 0 else f"leg {h['idx']}"
            lines.append(f"- FLAG {h['rule']} ({RULE_TITLES.get(h['rule'], '')}) on "
                         f"{h['key']} {leg}: {h['detail']}")
        first = cache.get(g["keys"][0]) or {}
        lines += ["- displayed text (first copy):", "```",
                  re.sub(r"<[^>]+>", "", first.get("html_text") or "")[:1500].strip(), "```"]
        picks = (first.get("parsed") or {}).get("picks") or []
        compact = [{k: p.get(k) for k in ("description", "sport", "bet_type", "period",
                                         "line", "direction", "is_parlay_leg")}
                   | {"odds": (first.get("odds_by_pick") or {}).get(str(i))}
                   for i, p in enumerate(picks)]
        lines += ["- parsed picks + odds_by_pick (first copy):", "```",
                  json.dumps(compact, indent=1, default=str)[:2500], "```", ""]
    lines += [
        f"- repo HEAD at spawn: {head}",
        "",
        "## How to judge a price (legit is a normal, expected outcome)",
        "- Read docs/odds.md (incl. its Odds watch section) and CLAUDE.md's Odds "
        "section first. Re-derive the price from a FREE source for the bet as "
        "the capper wrote it (bet slip if any → ESPN/Pinnacle/Bovada via "
        "odds.py's free path). Never spend paid Odds API credit.",
        "- LEGIT when the price is what a book offers for that exact bet: a real "
        "long shot or heavy favorite, an alternate the capper actually took, a "
        "legit exact match, normal book-to-book spread (~10 cents on a main "
        "line). Report `legit` and change nothing.",
        "- WRONG when the tag prices a different bet: wrong market/period/game/"
        "team, a misparse (team total for a game total, teaser leg at the "
        "pre-tease line), correlated legs multiplied as independent, an "
        "estimate far from the exact quote, a misplaced tag.",
    ]
    if has_hold:
        lines += [
            "- A `hold` flag means the pre-publish review hid that leg's tag "
            "(odds_by_pick[leg].hold) until your verdict. Do NOT remove holds "
            "yourself: the runner releases them after you report (everything "
            "but needs_human) and re-posts via the targeted tracker. If you "
            "re-price a held leg, write the corrected odds into odds_by_pick "
            "and leave `hold` in place.",
        ]
    if has_miss:
        lines += [
            "",
            "## A `miss` (we found no price)",
            "- Find the price from a FREE source for that exact bet (ESPN, "
            "Pinnacle guest API, Bovada coupons/prop groups, or another free, "
            "unauthenticated feed you can verify). If one exists, wire it into "
            "the odds code (free-first order, matching guards, a pinned test "
            "from a real captured payload) and price this pick through the "
            "real pipeline (drop the leg's odds_by_pick, targeted tracker) → "
            "`fixed`.",
            "- If no free source prices it (a market no free book lists, a "
            "sport with no feed) → `no_free_source`, naming the sources you "
            "checked in `issue`. That parks the whole miss class for "
            f"{CLASS_PARK_DAYS:.0f} days, so be sure. Never use the paid API.",
            "- A miss caused by a misparse (wrong sport/market/team) is a parse "
            "bug: fix the class, re-price → `fixed`.",
        ]
    lines += [
        "",
        "## When it IS wrong",
        "- Fix the CLASS in code (parse backstop / odds routing / placement) "
        "with a pinned test, following the subsystem's doc and CLAUDE.md "
        "invariants; don't fix a wrong price by widening a downstream guard or "
        "with prompt text alone. Sweep parse_cache.json for other live "
        "instances of the same class and repair those too.",
        "- Repair EVERY fan-out copy listed (and the DAGGER source mirror if "
        "grade_source applies): cache entry (odds_by_pick / parsed) and the "
        "live Telegram post. Re-pricing before first pitch: delete the leg's "
        "odds_by_pick and run the targeted tracker; after start: write the "
        "closing line via odds._try_pregame(...) per docs/odds.md. Verify "
        "the live post text afterwards.",
        "- If the only correct source is paid or the fix needs a product "
        "decision → `needs_human` with exactly what the operator must decide.",
        "",
        "## Constraints — these OVERRIDE the standard /investigate workflow where they conflict",
        "- You are a headless agent on the VPS (as forwarder, in "
        "/home/forwarder/app); no human is available. Work directly in this "
        "repo — NO git worktree, NO SSH.",
        "- NEVER `git push` and NEVER restart/stop telegram-forwarder or "
        "claude-channels. The runner pushes and restarts grade-daemon after "
        "you. Before editing parse_cache.json: `sudo -n systemctl stop "
        "grade-daemon`, and `sudo -n systemctl start grade-daemon` when done; "
        "use the project's locked load/save (tracker_cache), never a raw write.",
        "- No new Claude API (ANTHROPIC_API_KEY) calls in any code you add — "
        "Claude runs on the subscription only.",
        "- Commit any code fix locally: stage ONLY files you changed (never "
        "`git add -A`; the tree holds unrelated WIP), message prefixed "
        "`odds-watch:`. Commit as soon as the test passes — uncommitted edits "
        "are reverted on a timeout. Add a CLAUDE.md/docs rule for a new class.",
        f"- Budget ~{AGENT_TIMEOUT // 60 - 5} minutes; the runner kills you at "
        f"{AGENT_TIMEOUT // 60}. Judge every message before fixing any. Do not "
        "message the operator; the runner sends the card. Add an /investigate "
        "lesson ONLY for a novel debugging technique.",
        "",
        "## Result contract (the runner parses this)",
        "End your FINAL message with exactly one line listing EVERY message above:",
        'ODDS_WATCH_RESULT: [{"message": "<the ### heading key exactly>", '
        '"outcome": "<fixed|repaired|legit|no_free_source|needs_human>", '
        '"issue": "<what was wrong / why the price is right / which free '
        'sources you checked — one sentence>", "action": "<what you changed / '
        'what the operator must do — one sentence>"}]',
    ]
    return "\n".join(lines)


def dm_card(groups: list[dict], reports: dict[str, dict], *, commits: list[str],
            notes: list[str], meta: dict) -> str | None:
    """HTML card for the messages that were WRONG/missing (or failed); None
    when every flag was judged legit — that pass stays silent."""
    esc = html.escape
    shown = [g for g in groups if reports[g["source"]]["outcome"] != "legit"]
    if not shown and not commits and not any(n.startswith("⚠") for n in notes):
        return None
    blocks = []
    for g in shown:
        r = reports[g["source"]]
        rules = sorted({h["rule"] for h in g["hits"]})
        link = _tme(g["keys"][0])
        head = f"<a href=\"{esc(link)}\">{esc(g['capper'] or g['source'])}</a>" if link \
            else esc(g["capper"] or g["source"])
        inner = [f"<b>Flag:</b> {esc(', '.join(rules))}"]
        if r.get("issue"):
            inner.append(f"<b>Issue:</b> {esc(r['issue'])}")
        if r.get("action"):
            inner.append(f"<b>Action:</b> {esc(r['action'])}")
        blocks.append(f"{BADGE.get(r['outcome'], '❓')} {head} — {esc(r['outcome'])}\n"
                      f"<blockquote expandable>{chr(10).join(inner)}</blockquote>")
    legit = len(groups) - len(shown)
    foot = []
    if legit:
        foot.append(f"👌 {legit} other flag(s) judged legit")
    foot.append("<b>Commits:</b> " + (esc("; ".join(commits)) if commits else "none"))
    foot += [esc(n) for n in notes]
    bits = [f"{MODEL}/{EFFORT}"]
    if meta.get("wall_ms"):
        bits.append(f"{int(meta['wall_ms'] / 1000)}s")
    foot.append(esc(" · ".join(bits)))
    return "🎯 <b>Odds watch</b>\n" + "\n".join(blocks) + "\n" + "\n".join(foot)


# ─── I/O ─────────────────────────────────────────────────────────────────────

def load_json(path: Path) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def save_state(state: dict) -> None:
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = STATE_FILE.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(state, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(tmp, STATE_FILE)


def audit_running() -> bool:
    """The nightly ungraded audit edits the same cache + repo."""
    try:
        with ua.LOCK_FILE.open("a") as fh:
            fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
            fcntl.flock(fh, fcntl.LOCK_UN)
        return False
    except BlockingIOError:
        return True
    except OSError:
        return False


def release_holds(targets: list[tuple[str, int]]) -> list[str]:
    """Drop `hold` from these (key, leg) pairs — and from the same leg of
    every fan-out copy sharing the source — through the locked cache merge,
    then re-run the targeted tracker so the tag posts. Returns the keys
    re-run."""
    if not targets:
        return []
    from tracker_cache import _load_pending_cache, _save_pending_cache
    cache = _load_pending_cache()

    def src_of(k: str) -> str:
        return str((cache.get(k) or {}).get("_source_key") or k)

    legs_by_src: dict[str, set[int]] = {}
    for k, i in targets:
        legs_by_src.setdefault(src_of(k), set()).add(i)
    touched = []
    for key, entry in cache.items():
        if not isinstance(entry, dict) or src_of(key) not in legs_by_src:
            continue
        changed = False
        for i in legs_by_src[src_of(key)]:
            o = (entry.get("odds_by_pick") or {}).get(str(i))
            if isinstance(o, dict) and "hold" in o:
                o.pop("hold")
                changed = True
        if changed:
            touched.append(key)
    if not touched:
        return []
    _save_pending_cache(cache)
    cmd = [PYTHON, str(ROOT / "tracker.py"), "--live"] + [f"--target={k}" for k in touched]
    try:
        r = subprocess.run(cmd, cwd=str(ROOT), capture_output=True, text=True, timeout=600)
        print(f"released holds on {touched}: tracker exit {r.returncode}")
    except subprocess.TimeoutExpired:
        print(f"released holds on {touched}: tracker timed out (next pass places the tag)")
    return touched


# ─── main ────────────────────────────────────────────────────────────────────

def run_agent(groups: list[dict], cache: dict, state: dict, now: datetime,
              *, no_push: bool) -> None:
    """One agent over these groups → push/restart → settle → release → card."""
    group_hits = [h for g in groups for h in g["hits"]]
    record_spawn(state, group_hits, now)
    save_state(state)

    now_et = now.astimezone().strftime("%Y-%m-%d %H:%M %Z")
    prompt = build_prompt(groups, cache, now_et=now_et, head=ua.git_head()[:12])
    run_dir = OUT_DIR / now.strftime("%Y%m%d-%H%M%S")
    record: dict[str, Any] = {"ts": now.isoformat(timespec="seconds"),
                              "flags": [instance_key(h) for h in group_hits]}
    head0, dirty0 = ua.git_head(), ua.git_dirty_paths()
    invoker = WatchInvoker(
        os.environ.get("ODDS_WATCH_CLAUDE_BIN") or ua.DEFAULT_CLAUDE_BIN,
        oauth_token=os.environ.get("CLAUDE_CODE_OAUTH_TOKEN", ""),
        timeout=AGENT_TIMEOUT,
    )
    try:
        result_text = invoker(prompt, run_dir / "agent.jsonl")
        (run_dir / "result.md").write_text(result_text, encoding="utf-8")
        reports = parse_results(result_text, groups)
    except ua.AgentCallError as exc:
        outcome = "timeout" if "timed out" in str(exc) else "error"
        reports = {g["source"]: {"outcome": outcome, "issue": str(exc)[:400],
                                 "action": ""} for g in groups}
    record["reports"] = reports
    record.update(invoker.last_call)

    notes: list[str] = []
    head1 = ua.git_head()
    commits = ua.git_commits_between(head0, head1)
    record["commits"] = commits
    leftover = ua.git_dirty_paths() - dirty0
    if leftover and all(r["outcome"] in ("error", "timeout", "unparsed")
                        for r in reports.values()):
        record["reverted"] = ua.git_revert_paths(leftover)
    if commits:
        if no_push:
            notes.append(f"{len(commits)} commit(s) NOT pushed (--no-push)")
        elif ua._git("push").returncode != 0:
            notes.append("⚠ git push FAILED")
        changed = ua.git_changed_files(head0, head1)
        if any(f.endswith(".py") for f in changed):
            r = subprocess.run(["sudo", "-n", "systemctl", "restart", "grade-daemon"],
                               capture_output=True, text=True, timeout=120)
            if r.returncode != 0:
                notes.append("⚠ grade-daemon restart failed")
        if "listener.py" in changed:
            notes.append("⚠ listener.py changed — telegram-forwarder NOT "
                         "auto-restarted, deploy it yourself")
    if subprocess.run(["systemctl", "is-active", "--quiet", "grade-daemon"]).returncode:
        ok = subprocess.run(["sudo", "-n", "systemctl", "start", "grade-daemon"],
                            capture_output=True, timeout=120).returncode == 0
        notes.append("⚠ grade-daemon was down — restarted" if ok
                     else "⚠ grade-daemon DOWN and restart failed")

    state = load_json(STATE_FILE)  # keep anything a manual --rearm wrote
    to_release = []
    for g in groups:
        outcome = reports[g["source"]]["outcome"]
        for h in g["hits"]:
            settle(state, h, outcome, now)
            if h["rule"] == "hold" and outcome != "needs_human":
                to_release.append((h["key"], h["idx"]))
    save_state(state)
    record["released"] = release_holds(to_release)
    record["notes"] = notes
    ua.append_runs_log(RUNS_LOG, record)
    card = dm_card(groups, reports, commits=commits, notes=notes, meta=invoker.last_call)
    if card:
        ua.send_watchdog_dm(card, as_html=True)
    print("done: " + "; ".join(f"{g['source']}={reports[g['source']]['outcome']}"
                               for g in groups))


def plan(cache: dict, state: dict, now: datetime) -> tuple[list[dict], list[dict]]:
    """(hits, eligible groups) for one pass."""
    hits = scan(cache, recent_keys(cache, now, LOOKBACK_HOURS), now)
    live = []
    for h in hits:
        action, reason = gate(state, h, now)
        if action == "run":
            live.append(h)
        else:
            print(f"{instance_key(h)}: {reason}")
    return hits, group_by_message(live, cache)[:MAX_MESSAGES]


def main() -> int:
    parser = argparse.ArgumentParser(description="Odds watch: judge, find, repair prices")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--scan", action="store_true",
                        help="print every flag over --days of cache; no state, no agent")
    parser.add_argument("--days", type=float, default=30)
    parser.add_argument("--rearm", nargs="?", const="*", metavar="KEY")
    parser.add_argument("--no-push", action="store_true")
    args = parser.parse_args()

    now = datetime.now(timezone.utc)
    cache = load_json(CACHE_FILE)

    if args.scan:
        hits = scan(cache, recent_keys(cache, now, args.days * 24), now)
        for h in sorted(hits, key=lambda h: (h["rule"], h["key"])):
            print(f"{h['rule']:17} {h['key']:24} leg {h['idx']:>2}  {h['detail']}")
        from collections import Counter
        print(f"--- {len(hits)} flag(s): {dict(Counter(h['rule'] for h in hits))}; "
              f"{len(group_by_message(hits, cache))} message(s)")
        return 0

    state = load_json(STATE_FILE)
    if args.rearm:
        for key in list(state):
            if key.startswith("_") or args.rearm not in ("*", key):
                continue
            if key.startswith("class:"):
                state.pop(key)
            else:
                for f in ("attempts", "parked", "parked_reason", "last_outcome", "price"):
                    state[key].pop(f, None)
        save_state(state)
        print(f"re-armed {args.rearm}")
        return 0

    disabled = os.environ.get("ODDS_WATCH_DISABLED") == "1"
    for rnd in range(MAX_ROUNDS):
        if rnd:
            now = datetime.now(timezone.utc)
            cache = load_json(CACHE_FILE)
            state = load_json(STATE_FILE)
        hits, groups = plan(cache, state, now)
        capped = daily_spawns(state, now) >= DAILY_CAP
        groups_for_agent = [] if capped or disabled else groups
        agent_keys = {instance_key(h) for g in groups_for_agent for h in g["hits"]}
        stale = release_due(hits, state, now, agent_keys=agent_keys)
        if stale:
            print(f"releasing {len(stale)} hold(s) no agent will judge now")
            if not args.dry_run:
                release_holds([(h["key"], h["idx"]) for h in stale])
        if disabled:
            print("skip: ODDS_WATCH_DISABLED=1")
            return 0
        if not groups:
            print(f"clean: {len(hits)} flag(s), none new")
            return 0
        if capped:
            print(f"daily cap reached ({DAILY_CAP}) — {len(groups)} message(s) wait")
            last = oc.ts(state.get("_cap_dm_at"))
            if not args.dry_run and (not last or now - last > timedelta(hours=24)):
                if ua.send_watchdog_dm(f"⚠️ Odds watch hit its daily cap ({DAILY_CAP} "
                                       "agents/24h) — flagged prices wait, held tags "
                                       "release unjudged."):
                    state["_cap_dm_at"] = now.isoformat(timespec="seconds")
                    save_state(state)
            return 0
        if args.dry_run:
            print(build_prompt(groups, cache, now_et=now.astimezone().strftime(
                "%Y-%m-%d %H:%M %Z"), head=ua.git_head()[:12]))
            return 0
        if audit_running():
            print("nightly audit running — next pass")
            return 0
        LOCK_FILE.parent.mkdir(parents=True, exist_ok=True)
        with LOCK_FILE.open("w") as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                print("an agent run holds the lock; next pass")
                return 0
            run_agent(groups, cache, state, now, no_push=args.no_push)
    return 0


if __name__ == "__main__":
    sys.exit(main())
