#!/usr/bin/env python3
"""Odds watch — catch wrong prices on forwarded picks and auto-repair them.

The operator used to spot a wrong odds tag ("[+245]" on a spread-and-ML that
no book prices near that) and ask for an /investigate. This does that loop
unattended: odds-watch.timer (every 15 min) runs free, deterministic checks
over recently priced cache entries; anything flagged goes to ONE headless
`claude -p "/investigate …"` agent (Opus 5.5 high, subscription-billed — the
hc-repair chassis) that JUDGES each flag first. A long price can be right
(big dogs, UFC, alternates the capper chose, correlated SGPs a book prices
long); those are recorded `legit` and never re-flagged at that price. A real
error gets the live post + cache repaired AND the class fixed in code with a
pinned test, then ONE watchdog card. A pass where everything was legit is
silent (ledger only).

Checks (`scan_entry`, pure; every one is a SUSPICION, never a verdict):
- band          straight spread/total/team_total priced like an alternate
- ml_long       straight moneyline beyond the long-price band
- prop_long     player prop beyond the long-price band
- leg_band      a non-teaser parlay leg outside the straight bands
- parlay_same_game  2+ parlay legs on the same team(s): the combined tag is a
                naive product of correlated legs (the VT +245, 2026-10-02)
- teaser_long   a teaser's combined price beyond any real teaser card
- proximity_gap an estimate from a line 2+ points away (blind to key numbers)
- now_move      the capper stated a price and ours differs by >10 pts implied
- game_mismatch the price came from a different game than the bound ESPN event
- fanout_price  fan-out copies of one pick carry different prices
Live prices (`live_*`) are exempt everywhere — in-game prices are anything.

Guards (state logs/odds_watch_state.json, per instance
`<key>:<rule>:<leg>` + the price it fired on):
- kill switch ODDS_WATCH_DISABLED=1 → exit 0
- flock logs/.odds_watch.lock (one agent at a time); skips while the nightly
  audit holds its lock (both edit the cache + repo)
- an instance judged `legit` stays quiet until its price CHANGES
- ODDS_WATCH_ATTEMPT_CAP (2) runs per instance → parked
- ODDS_WATCH_DAILY_CAP (6) agents per rolling 24h; ODDS_WATCH_MAX_MESSAGES (4)
  messages per agent (newest first; the rest wait for the next pass)
- lookback ODDS_WATCH_LOOKBACK_HOURS (36) on msg_date — repairs are cheapest
  pregame, and closing-line repairs after kickoff still work for a day

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

ROOT = ua.ROOT
CACHE_FILE = ROOT / "parse_cache.json"
STATE_FILE = ROOT / "logs" / "odds_watch_state.json"
LOCK_FILE = ROOT / "logs" / ".odds_watch.lock"
RUNS_LOG = ROOT / "logs" / "odds_watch_runs.jsonl"
OUT_DIR = ROOT / "logs" / "odds_watch"

MODEL = os.environ.get("ODDS_WATCH_MODEL") or "claude-opus-5-5"
EFFORT = os.environ.get("ODDS_WATCH_EFFORT") or "high"
AGENT_TIMEOUT = int(os.environ.get("ODDS_WATCH_AGENT_TIMEOUT") or 1500)
ATTEMPT_CAP = int(os.environ.get("ODDS_WATCH_ATTEMPT_CAP") or 2)
DAILY_CAP = int(os.environ.get("ODDS_WATCH_DAILY_CAP") or 6)
MAX_MESSAGES = int(os.environ.get("ODDS_WATCH_MAX_MESSAGES") or 4)
LOOKBACK_HOURS = float(os.environ.get("ODDS_WATCH_LOOKBACK_HOURS") or 36)

# ─── thresholds ──────────────────────────────────────────────────────────────
# Main spreads/totals sit near -110; a proximity estimate 1-1.5 pts off can
# reach about ±160. Past these the price is an alternate or a wrong market.
LINE_DOG, LINE_FAV = 170, -230
ML_DOG, ML_FAV = 450, -700
PROP_DOG = 800
TEASER_MAX = 220           # real 2-3 leg teaser cards price about -140..+180
PROXIMITY_GAP = 2.0
NOW_MOVE = 0.10            # implied-probability gap, stated vs ours
FANOUT_GAP = 0.06
KICKOFF_GAP_H = 6

RULE_TITLES = {
    "band": "spread/total priced like an alternate",
    "ml_long": "moneyline beyond the long-price band",
    "prop_long": "prop beyond the long-price band",
    "leg_band": "parlay leg outside the straight bands",
    "parlay_same_game": "parlay legs on the same team(s) multiplied as independent",
    "teaser_long": "teaser combined price beyond any teaser card",
    "proximity_gap": "price estimated from a line 2+ pts away",
    "now_move": "far from the capper's stated price",
    "game_mismatch": "priced from a different game than the bound event",
    "fanout_price": "fan-out copies carry different prices",
}

OUTCOMES = (
    "fixed",        # price was wrong: live post + cache repaired, class fixed
    "repaired",     # price was wrong: live artifacts repaired, no code change needed/possible
    "legit",        # the price is right (long shot, alternate, real market)
    "needs_human",  # wrong but unfixable unattended (paid-only source, decision)
)
WRONG_OUTCOMES = ("fixed", "repaired", "needs_human")
BADGE = {
    "fixed": "✅", "repaired": "🔧", "legit": "👌", "needs_human": "🙋",
    "unparsed": "⚠️", "error": "❌", "timeout": "⏱",
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

def _fmt(price: int) -> str:
    return f"+{price}" if price > 0 else str(price)


def implied(price: int) -> float:
    return 100 / (price + 100) if price > 0 else -price / (-price + 100)


def _ts(value) -> datetime | None:
    try:
        t = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    return t if t.tzinfo else t.replace(tzinfo=timezone.utc)


def _price(o: dict) -> int | None:
    p = o.get("odds") if isinstance(o, dict) else None
    return p if isinstance(p, int) and not isinstance(p, bool) else None


def _is_live(o: dict) -> bool:
    return "live" in str(o.get("match_type") or "")


_TEASER_RE = re.compile(r"\bteas(?:e|er|ed)\b", re.IGNORECASE)
_NOW_TAG_RE = re.compile(r"\[([+-]\d{3,4}) now\]")
_SRC_PRICE_RE = re.compile(r"(?<![\d.\[])([+-]\d{3,4})(?![\d.])")


def _team_set(pick: dict) -> frozenset:
    return frozenset(t.strip().lower() for t in (pick.get("teams") or []) if t)


def _same_game_pairs(picks: list[dict], legs: list[int]) -> list[tuple[int, int]]:
    """Parlay legs that name an overlapping team set (and the same period):
    the same game, so their prices are correlated, not independent."""
    pairs = []
    for a_pos, a in enumerate(legs):
        for b in legs[a_pos + 1:]:
            ta, tb = _team_set(picks[a]), _team_set(picks[b])
            if ta and tb and ta & tb and (picks[a].get("period") or "game") == (
                    picks[b].get("period") or "game"):
                pairs.append((a, b))
    return pairs


def scan_entry(key: str, entry: dict) -> list[dict[str, Any]]:
    """Every suspicion on one cache entry. A hit: {key, idx, rule, price,
    detail}; idx -1 = the ticket (parlay) or the message as a whole."""
    picks = (entry.get("parsed") or {}).get("picks") or []
    odds = entry.get("odds_by_pick") or {}
    text = entry.get("html_text") or ""
    hits: list[dict[str, Any]] = []

    def hit(idx: int, rule: str, price: int | None, detail: str) -> None:
        hits.append({"key": key, "idx": idx, "rule": rule, "price": price,
                     "detail": detail})

    legs = [i for i, p in enumerate(picks) if p.get("is_parlay_leg")]
    teaser = bool(_TEASER_RE.search(text))
    for i, pick in enumerate(picks):
        o = odds.get(str(i)) or {}
        price = _price(o)
        if price is None or _is_live(o):
            continue
        bt = pick.get("bet_type") or ""
        mt = str(o.get("match_type") or "")
        what = f"{_fmt(price)} on {bt} ({mt or 'no match_type'})"
        # The capper's own number in the pick ("Elkins ML (+500)" at +561)
        # corroborates a long price — the band checks stand down.
        stated = [int(s) for s in _SRC_PRICE_RE.findall(pick.get("description") or "")]
        # A straight price nobody sees isn't worth an agent: when the capper
        # states their own number, _insert_odds shows ours only on a big move
        # (the `now` check below) — "Michigan -3 -160" carries no tag at all.
        shown = pick.get("is_parlay_leg") or re.search(
            rf"\[{re.escape(_fmt(price))}\b", text)
        if not shown or any(abs(implied(s) - implied(price)) <= NOW_MOVE for s in stated):
            pass
        elif pick.get("is_parlay_leg"):
            if not teaser and bt in ("spread", "total", "team_total") and not (
                    LINE_FAV < price < LINE_DOG):
                hit(i, "leg_band", price, what)
        elif bt in ("spread", "total", "team_total"):
            if not (LINE_FAV < price < LINE_DOG):
                hit(i, "band", price, what)
        elif bt == "moneyline":
            if not (ML_FAV < price < ML_DOG):
                hit(i, "ml_long", price, what)
        elif bt == "prop" and price >= PROP_DOG:
            hit(i, "prop_long", price, what)
        m = re.match(r"proximity_([\d.]+)pts", mt)
        if m and float(m.group(1)) >= PROXIMITY_GAP and shown and not pick.get("is_parlay_leg"):
            hit(i, "proximity_gap", price, what)
        # Wrong-game binding: the price's game vs the event the leg is bound
        # to at post time (espn_events, legacy soccer_events).
        bound = ((entry.get("espn_events") or entry.get("soccer_events") or {})
                 .get(str(i)) or {})
        k_odds, k_bound = _ts(o.get("commence_time")), _ts(bound.get("kickoff"))
        if (k_odds and k_bound and bound.get("status") == "bound"
                and abs((k_odds - k_bound).total_seconds()) > KICKOFF_GAP_H * 3600):
            hit(i, "game_mismatch", price,
                f"odds game {o.get('commence_time')} vs bound "
                f"{bound.get('name')} {bound.get('kickoff')}")

    if len(legs) >= 2:
        leg_prices = [_price(odds.get(str(i)) or {}) for i in legs]
        if all(p is not None for p in leg_prices) and not any(
                _is_live(odds.get(str(i)) or {}) for i in legs):
            from common import parlay_combined_odds
            comb = parlay_combined_odds(leg_prices)
            pairs = _same_game_pairs(picks, legs)
            if pairs:
                hit(-1, "parlay_same_game", comb, "legs " + ", ".join(
                    f"{a}+{b} ({', '.join(sorted(_team_set(picks[a]) & _team_set(picks[b])))})"
                    for a, b in pairs) + f" multiplied to {_fmt(comb)}")
            if teaser and comb is not None and comb > TEASER_MAX:
                hit(-1, "teaser_long", comb, f"{len(legs)}-leg teaser at {_fmt(comb)}")

    # The capper's own stated price vs ours ("Bills -3 (-110) [-190 now]").
    for line in text.split("\n"):
        now = _NOW_TAG_RE.search(line)
        if not now:
            continue
        ours = int(now.group(1))
        stated = [int(s) for s in _SRC_PRICE_RE.findall(_NOW_TAG_RE.sub("", line))]
        if stated and min(abs(implied(s) - implied(ours)) for s in stated) > NOW_MOVE:
            hit(-1, "now_move", ours, f"stated {', '.join(_fmt(s) for s in stated)}"
                f" vs ours {_fmt(ours)}: {re.sub(r'<[^>]+>', '', line).strip()[:80]}")
    return hits


def scan_fanout(cache: dict, keys: list[str]) -> list[dict[str, Any]]:
    """Fan-out copies of one pick (same capper + description + game date)
    are priced independently; materially different prices mean one copy
    bound the wrong market."""
    groups: dict[tuple, list[tuple[str, int, int]]] = {}
    for key in keys:
        entry = cache[key]
        picks = (entry.get("parsed") or {}).get("picks") or []
        for i, o in (entry.get("odds_by_pick") or {}).items():
            price = _price(o)
            if price is None or _is_live(o) or not i.isdigit() or int(i) >= len(picks):
                continue
            fp = (str(entry.get("capper_name") or "").strip().lower(),
                  re.sub(r"\s+", " ", (picks[int(i)].get("description") or "").lower()),
                  o.get("game_date"))
            groups.setdefault(fp, []).append((key, int(i), price))
    hits = []
    for copies in groups.values():
        prices = [p for _, _, p in copies]
        if len({k for k, _, _ in copies}) < 2 or max(map(implied, prices)) - min(map(implied, prices)) <= FANOUT_GAP:
            continue
        detail = "copies priced " + " / ".join(f"{k}={_fmt(p)}" for k, _, p in sorted(copies))
        for key, i, price in copies:
            hits.append({"key": key, "idx": i, "rule": "fanout_price",
                         "price": price, "detail": detail})
    return hits


def recent_keys(cache: dict, now: datetime, lookback_hours: float) -> list[str]:
    floor = now - timedelta(hours=lookback_hours)
    out = []
    for key, entry in cache.items():
        if (not isinstance(entry, dict) or entry.get("_dupe")
                or not entry.get("odds_by_pick") or "parsed" not in entry):
            continue
        t = _ts(entry.get("msg_date"))
        if t and t >= floor:
            out.append(key)
    return out


def scan(cache: dict, keys: list[str]) -> list[dict[str, Any]]:
    hits = []
    for key in keys:
        hits.extend(scan_entry(key, cache[key]))
    hits.extend(scan_fanout(cache, keys))
    return hits


def instance_key(h: dict) -> str:
    return f"{h['key']}:{h['rule']}:{h['idx']}"


def gate(state: dict, h: dict) -> tuple[str, str]:
    """(action, reason): "run" | "skip"."""
    st = state.get(instance_key(h)) or {}
    if st.get("parked"):
        return "skip", f"parked ({st.get('parked_reason')})"
    if st.get("last_outcome") == "legit" and st.get("price") == h["price"]:
        return "skip", "judged legit at this price"
    if st.get("last_outcome") in WRONG_OUTCOMES and st.get("price") == h["price"]:
        return "skip", f"{st['last_outcome']} at this price"
    if int(st.get("attempts") or 0) >= ATTEMPT_CAP and st.get("price") == h["price"]:
        return "skip", "attempt cap"
    return "run", "ok"


def group_by_message(hits: list[dict], cache: dict) -> list[dict[str, Any]]:
    """One target per SOURCE message (fan-out copies together — the agent
    must repair every copy), newest message first."""
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
    return sorted(groups.values(), key=lambda g: g["msg_date"], reverse=True)


def daily_spawns(state: dict, now: datetime) -> int:
    recent = [s for s in state.get("_spawns", [])
              if (t := _ts(s)) and now - t < timedelta(hours=24)]
    state["_spawns"] = recent
    return len(recent)


def record_spawn(state: dict, hits: list[dict], now: datetime) -> None:
    stamp = now.isoformat(timespec="seconds")
    state.setdefault("_spawns", []).append(stamp)
    for h in hits:
        st = state.setdefault(instance_key(h), {})
        if st.get("price") != h["price"]:
            st.pop("attempts", None)  # a re-priced instance is a new question
        st.update(price=h["price"], last_spawn_at=stamp,
                  attempts=int(st.get("attempts") or 0) + 1)


def settle(state: dict, h: dict, outcome: str) -> None:
    st = state.setdefault(instance_key(h), {})
    st["last_outcome"] = outcome
    if outcome == "needs_human":
        st.update(parked=True, parked_reason="needs_human")
    elif int(st.get("attempts") or 0) >= ATTEMPT_CAP and outcome not in OUTCOMES:
        st.update(parked=True, parked_reason=f"attempt cap ({st['attempts']})")


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
    lines = [
        f"/investigate ODDS WATCH {now_et}: a deterministic scan flagged "
        f"{len(groups)} forwarded message(s) whose odds tag MAY be wrong. These "
        "checks are deliberately loose — long prices are often right. For EACH "
        "message, first JUDGE whether the displayed price is what a book "
        "actually offers for the bet the capper made; only a real error gets "
        "repaired and fixed.",
        "",
        "## Flagged messages",
    ]
    for g in groups:
        lines.append(f"### {g['source']}  ({g['capper'] or 'unknown capper'})")
        for key in g["keys"]:
            entry = cache.get(key) or {}
            lines.append(f"- copy {key} {_tme(key)} msg_date {entry.get('msg_date')}"
                         + (" has_media (bet slip/image — the slip is ground truth "
                            "for the price)" if entry.get("has_media") else ""))
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
        "## How to judge (legit is a normal, expected outcome)",
        "- Read docs/odds.md and CLAUDE.md's Odds section first. Re-derive the "
        "price from a FREE source for the bet as the capper wrote it (bet "
        "slip image if any → ESPN/Pinnacle/Bovada via odds.py's free path). "
        "Never spend paid Odds API credit to judge.",
        "- LEGIT when the price is what a book offers for that exact bet: a real "
        "long shot or heavy favorite, an alternate line the capper actually "
        "took, a legit exact match. A price within normal book-to-book spread "
        "(~10 cents on a main line) is legit. Report `legit` and change "
        "nothing — the runner won't re-flag that price.",
        "- WRONG when the tag prices a different bet: wrong market/period/game/"
        "team, a misparse (e.g. a team total for a game total, a teaser leg at "
        "the pre-tease line), correlated legs multiplied as independent, an "
        "estimate far from the exact quote, or a misplaced tag.",
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
        "use the project's locked load/save, never a raw write.",
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
        '"outcome": "<fixed|repaired|legit|needs_human>", "issue": "<what was '
        'wrong, or why the price is right — one sentence>", "action": "<what '
        'you changed / what the operator must do — one sentence>"}]',
    ]
    return "\n".join(lines)


def dm_card(groups: list[dict], reports: dict[str, dict], *, commits: list[str],
            notes: list[str], meta: dict) -> str | None:
    """HTML card for the messages that were WRONG (or failed); None when
    every flag was judged legit — that pass stays silent."""
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


# ─── main ────────────────────────────────────────────────────────────────────

def main() -> int:
    parser = argparse.ArgumentParser(description="Odds watch: flag + auto-repair wrong prices")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--scan", action="store_true",
                        help="print every flag over --days of cache; no state, no agent")
    parser.add_argument("--days", type=float, default=30)
    parser.add_argument("--rearm", nargs="?", const="*", metavar="KEY")
    parser.add_argument("--no-push", action="store_true")
    args = parser.parse_args()

    if os.environ.get("ODDS_WATCH_DISABLED") == "1" and not args.scan:
        print("skip: ODDS_WATCH_DISABLED=1")
        return 0

    now = datetime.now(timezone.utc)
    cache = load_json(CACHE_FILE)

    if args.scan:
        hits = scan(cache, recent_keys(cache, now, args.days * 24))
        for h in sorted(hits, key=lambda h: (h["rule"], h["key"])):
            print(f"{h['rule']:17} {h['key']:24} leg {h['idx']:>2}  {h['detail']}")
        from collections import Counter
        print(f"--- {len(hits)} flag(s): {dict(Counter(h['rule'] for h in hits))}; "
              f"{len(group_by_message(hits, cache))} message(s)")
        return 0

    state = load_json(STATE_FILE)
    if args.rearm:
        for key, st in state.items():
            if not key.startswith("_") and args.rearm in ("*", key):
                for f in ("attempts", "parked", "parked_reason", "last_outcome", "price"):
                    st.pop(f, None)
        save_state(state)
        print(f"re-armed {args.rearm}")
        return 0

    hits = scan(cache, recent_keys(cache, now, LOOKBACK_HOURS))
    live = []
    for h in hits:
        action, reason = gate(state, h)
        if action == "run":
            live.append(h)
        else:
            print(f"{instance_key(h)}: {reason}")
    groups = group_by_message(live, cache)[:MAX_MESSAGES]
    if not groups:
        print(f"clean: {len(hits)} flag(s), none new")
        return 0

    spawned = daily_spawns(state, now)
    if spawned >= DAILY_CAP:
        print(f"daily cap reached ({spawned}/{DAILY_CAP}) — {len(groups)} message(s) wait")
        last = _ts(state.get("_cap_dm_at"))
        if not args.dry_run and (not last or now - last > timedelta(hours=24)):
            if ua.send_watchdog_dm(f"⚠️ Odds watch hit its daily cap ({DAILY_CAP} "
                                   "agents/24h) — flagged prices wait for the next window."):
                state["_cap_dm_at"] = now.isoformat(timespec="seconds")
                save_state(state)
        return 0

    now_et = now.astimezone().strftime("%Y-%m-%d %H:%M %Z")
    prompt = build_prompt(groups, cache, now_et=now_et, head=ua.git_head()[:12])
    if args.dry_run:
        print(prompt)
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

        group_hits = [h for g in groups for h in g["hits"]]
        record_spawn(state, group_hits, now)
        save_state(state)

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
            if args.no_push:
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
        record["notes"] = notes

        state = load_json(STATE_FILE)  # keep anything a manual --rearm wrote
        for g in groups:
            for h in g["hits"]:
                settle(state, h, reports[g["source"]]["outcome"])
        save_state(state)
        ua.append_runs_log(RUNS_LOG, record)
        card = dm_card(groups, reports, commits=commits, notes=notes,
                       meta=invoker.last_call)
        if card:
            ua.send_watchdog_dm(card, as_html=True)
        print("done: " + "; ".join(f"{g['source']}={reports[g['source']]['outcome']}"
                                   for g in groups))
    return 0


if __name__ == "__main__":
    sys.exit(main())
