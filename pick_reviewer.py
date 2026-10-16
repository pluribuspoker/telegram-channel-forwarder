"""Pick review — one Claude look at EVERY pick: its parse, its verdict, and
every message that parsed to nothing (operator 2026-10-15).

The deterministic checks (odds gate, grader anomalies, ungraded audit) only
spawn Claude on a heuristic hit, and a heuristic misses what it wasn't
written for. This module is the reviewer half of scripts/pick_review.py:
one subscription call per review question (claude_sub — headless `claude -p`,
never ANTHROPIC_API_KEY, operator rule 2026-10-03), no tools, so every bit
of evidence is packed into the payload here:

- parse    the post vs every parsed pick, its bound game and its price
- grade    a resolved leg vs its calc and the ESPN final / line score
- noparse  a message the parser returned no picks for — was there a bet?
- nightly  yesterday's messages end to end (parse, price, verdicts, and any
           leg still unresolved after its game), in small batches

A reviewer only FLAGS. It never changes a verdict or a parse itself
(sampled answers aren't deterministic) — every flag goes to one headless
agent (scripts/pick_review.py --agent) that judges and repairs, and the
operator gets a card with the outcome. Fails open: an errored or
unparseable review returns None and the runner retries it next pass.
Test: scripts/test_pick_review.py.
"""

import asyncio
import json
import os
import re
import time
from datetime import datetime, timezone
from typing import Any, Callable
from zoneinfo import ZoneInfo

from common import ODDS_TAG_RE

REVIEW_MODEL = os.environ.get("PICK_REVIEW_MODEL") or "claude-opus-5-5"
REVIEW_EFFORT = os.environ.get("PICK_REVIEW_EFFORT") or "medium"
REVIEW_TIMEOUT = float(os.environ.get("PICK_REVIEW_TIMEOUT") or 180)
RESOLVED = ("WIN", "LOSS", "PUSH", "VOID")
_ET = ZoneInfo("America/New_York")

_COMMON = """You are a second pair of eyes on an automated sports-pick tracker.
Cappers post picks in Telegram channels. Our software parses each post into
structured bets, binds each bet to a game, prices it from sportsbook feeds,
and later grades it (WIN/LOSS/PUSH/VOID) from final scores. Your answer is
read by software: anything you flag goes to an engineer-agent who
investigates and fixes it, so a false alarm costs that agent's time and a miss
means a wrong pick stays public. Flag only what you are fairly confident is
WRONG, and say exactly what is wrong in one sentence.

Conventions you must not flag:
- Displayed text may already carry OUR additions: a price we appended, a
  ✅/❌/♻️ result emoji. Blockquotes are the capper's stats/records, never bets.
- `description` wording is free text; judge the structured fields.
- Soccer moneyline is 3-way (a draw LOSES); only DNB pushes. Soccer bets settle
  on 90 minutes unless "to advance/qualify". NHL regulation ML: OT = loss.
- MLB "1h"/F5 = first 5 innings; MLB "1q" = 1st inning (NRFI = under 0.5 1q).
- Teaser legs are graded at the line the capper STATED. A bare "TEAM over N" is
  the GAME total unless written team total/TT.
- One pick per line on a card = separate straight bets, not a parlay.
- `has_image: true` = the post carried an image (often a bet slip) that the
  parse may have read; you can't see it. A pick that isn't in the text but
  fits an image post ("Tailing X" + slip) is NOT invented — flag only if it
  contradicts the text.
- A missing price (null odds) is handled elsewhere; don't flag it alone.
- Totals settle MID-GAME once they can't change (an over past its line, an
  under already exceeded), so a WIN/LOSS calc on a mid-game score is right
  when the final can't flip it. Spreads/ML settle at the final.
- A moneyline's `line` field is ignored everywhere — grading, odds routing,
  rendering — and often holds the price (also in `description`); never flag
  it. A pick's null `sport` inherits the post's sport.
- A leg whose price is `held` is already with the odds reviewer; don't flag
  that price.
"""

PARSE_PROMPT = _COMMON + """
TASK: check the PARSE of one post. Flag when:
- a bet the capper clearly made is missing from the parsed picks, or a parsed
  pick is not a bet in the post (a record, a recap, a past result, a teaser
  for a pick posted elsewhere);
- a pick's sport, team/player, market (bet_type), period, line or direction
  does not match what the capper wrote;
- legs are combined into a parlay the capper bet separately, or a real
  parlay/ticket was split into straight bets;
- the bound game (`bound_game`, `game_date`) is the wrong game for the pick:
  wrong team (a pro vs college team sharing a place name), wrong sport, or a
  date that doesn't fit the post (`msg_date`, "today/tonight", a named day);
- the price is plainly for a different bet (an F5 bet priced like a full game).

Return exactly one JSON object and nothing else:
{"flags": [{"leg": <pick index, or "message" for a missing pick>,
            "problem": "<one sentence>"}]}
Use {"flags": []} when the parse is right."""

GRADE_PROMPT = _COMMON + """
TASK: check the VERDICT of the resolved leg(s) of one post. Each leg carries
our verdict, our `calc` (the arithmetic we graded on) and, when we could fetch
it, `espn` — the bound game's line/box score straight from ESPN. Flag when:
- the verdict is wrong for the bet as parsed given the score (wrong side of
  the line, a push graded as a win/loss, wrong period's score used, a 3-way ML
  draw graded as a push, a player stat misread);
- `calc` uses a score that doesn't match `espn`, or a different game than the
  bound one;
- the parse itself is wrong in a way that changes the result (the capper bet
  a different team, line or period than the one graded).
Missing `espn` is not a reason to flag — check `calc` against the bet then.

Return exactly one JSON object and nothing else:
{"flags": [{"leg": <leg index>, "problem": "<one sentence>"}]}
Use {"flags": []} when every verdict is right."""

NOPARSE_PROMPT = _COMMON + """
TASK: our parser found NO picks in this post. Flag it when the post contains
a real bet a reader could place (a team/player and a market: side, ML,
total, prop, parlay), including terse ones ("Kings NBA Pre Season" under a
capper header can be a pick). Do not flag chatter, promos, recaps of past
results, record updates, "no play today", or a pick merely teased for later.

Return exactly one JSON object and nothing else:
{"flags": [{"leg": "message", "problem": "<which bet was missed>"}]}
Use {"flags": []} when there is no bet in the post."""

NIGHTLY_PROMPT = _COMMON + """
TASK: overnight review of several posts from yesterday, end to end. For each
post check the parse (as above), the bound game, the price, and every leg's
verdict against `espn` and `calc`. A leg with NO verdict whose game is over
(`espn` shows a final, or kickoff was 6+ hours before `now`) is a grading
failure — flag it. A leg still pending before its game is fine.

Return exactly one JSON object and nothing else:
{"flags": [{"message": "<the post's key exactly>", "leg": <leg index or
            "message">, "problem": "<one sentence>"}]}
Use {"flags": []} when everything is right."""

PROMPTS = {"parse": PARSE_PROMPT, "grade": GRADE_PROMPT,
           "noparse": NOPARSE_PROMPT, "nightly": NIGHTLY_PROMPT}


# ─── payloads ─────────────────────────────────────────────────────────────────

def display_text(entry: dict) -> str:
    """The post as shown, tags stripped (our prices are noise for the parse)."""
    text = re.sub(r"<br\s*/?>", "\n", entry.get("html_text") or "")
    text = re.sub(r"<blockquote[^>]*>", "[quote] ", text)
    text = re.sub(r"</blockquote>", " [/quote]", text)
    text = re.sub(r"<[^>]+>", "", text)
    import html as _html
    text = _html.unescape(text)
    return ODDS_TAG_RE.sub("", text).strip()[:3000]


def picks_of(entry: dict) -> list[dict]:
    parsed = entry.get("parsed")
    if isinstance(parsed, dict):
        return parsed.get("picks") or []
    return []


_PICK_FIELDS = ("description", "sport", "bet_type", "period", "teams", "player",
                "prop_stat", "line", "direction", "is_parlay_leg")


def leg_view(entry: dict, i: int, pick: dict) -> dict[str, Any]:
    o = (entry.get("odds_by_pick") or {}).get(str(i)) or {}
    b = (entry.get("espn_events") or entry.get("soccer_events") or {}).get(str(i)) or {}
    out: dict[str, Any] = {"leg": i, **{k: pick.get(k) for k in _PICK_FIELDS}}
    out["sport"] = pick.get("sport") or (entry.get("parsed") or {}).get("sport")
    out["price"] = o.get("odds")
    if o.get("match_type"):
        out["price_source"] = f"{o.get('bookmaker')} / {o.get('match_type')}"
    if o.get("hold"):
        out["price_held"] = True
    if b:
        out["bound_game"] = b.get("name")
        out["game_date"] = b.get("date")
        out["kickoff_et"] = _et(b.get("kickoff"))
        if b.get("status") not in (None, "bound"):
            out["binding_status"] = b.get("status")
    elif o.get("commence_time"):
        out["kickoff_et"] = _et(o.get("commence_time"))
    return out


def verdict_view(entry: dict, i: int) -> dict[str, Any]:
    v = (entry.get("leg_verdicts") or {}).get(str(i))
    if not isinstance(v, dict) or v.get("verdict") not in RESOLVED:
        return {"verdict": None}
    return {"verdict": v.get("verdict"), "calc": v.get("calc"),
            "graded_game_date": v.get("game_date")}


def _et(ts: Any) -> str | None:
    if not ts:
        return None
    try:
        t = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
    except ValueError:
        return None
    if t.tzinfo is None:
        t = t.replace(tzinfo=timezone.utc)
    return t.astimezone(_ET).strftime("%a %Y-%m-%d %H:%M ET")


def parse_payload(key: str, entry: dict) -> str:
    body = {"key": key, "capper": entry.get("capper_name"),
            "msg_date": entry.get("msg_date"), "has_image": bool(entry.get("has_media")),
            "post": display_text(entry),
            "picks": [leg_view(entry, i, p) for i, p in enumerate(picks_of(entry))]}
    return json.dumps(body, indent=1, default=str)


def grade_payload(key: str, entry: dict, legs: list[int],
                  evidence: dict[int, str]) -> str:
    picks = picks_of(entry)
    rows = []
    for i in legs:
        row = leg_view(entry, i, picks[i]) | verdict_view(entry, i)
        if evidence.get(i):
            row["espn"] = evidence[i]
        rows.append(row)
    body = {"key": key, "capper": entry.get("capper_name"),
            "msg_date": entry.get("msg_date"), "has_image": bool(entry.get("has_media")),
            "post": display_text(entry), "all_picks": [p.get("description") for p in picks], "resolved_legs": rows}
    return json.dumps(body, indent=1, default=str)


def noparse_payload(key: str, capper: str, text: str, date: str) -> str:
    return json.dumps({"key": key, "capper": capper, "msg_date": date,
                       "post": (text or "").strip()[:3000]}, indent=1)


def nightly_payload(items: list[tuple[str, dict, dict[int, str]]], now: datetime) -> str:
    posts = []
    for key, entry, evidence in items:
        legs = []
        for i, p in enumerate(picks_of(entry)):
            row = leg_view(entry, i, p) | verdict_view(entry, i)
            if evidence.get(i):
                row["espn"] = evidence[i]
            legs.append(row)
        posts.append({"key": key, "capper": entry.get("capper_name"),
                      "msg_date": entry.get("msg_date"),
                      "has_image": bool(entry.get("has_media")),
                      "post": display_text(entry), "legs": legs})
    return json.dumps({"now": _et(now.isoformat()), "posts": posts}, indent=1, default=str)


# ─── evidence (free ESPN reads) ───────────────────────────────────────────────

async def _leg_evidence(entry: dict, i: int, pick: dict) -> str | None:
    import httpx
    import scores
    b = (entry.get("espn_events") or entry.get("soccer_events") or {}).get(str(i)) or {}
    sport = pick.get("sport") or (entry.get("parsed") or {}).get("sport")
    if not b.get("id"):
        return None
    if sport == "Soccer":
        async with httpx.AsyncClient(timeout=15) as http:
            summary = await scores._fetch_soccer_summary(http, str(b["id"]))
        sides = scores._soccer_side_scores(summary) if summary else None
        if not sides:
            return None
        status = (((summary.get("header") or {}).get("competitions") or [{}])[0]
                  .get("status", {}).get("type", {}).get("name"))
        return " | ".join(f"{s['name']} periods={s['p']}" for s in sides.values()) + \
            f" | status={status}"
    summary = await scores.fetch_espn_summary(sport, str(b["id"]))
    if not summary:
        return None
    text = scores.line_scores_text(summary, sport) or ""
    status = (((summary.get("header") or {}).get("competitions") or [{}])[0]
              .get("status", {}).get("type", {}).get("description"))
    if status:
        text += f"\nstatus: {status}"
    if pick.get("player"):
        box = scores.box_score_text(summary, pick["player"]) or ""
        text += "\n" + box[:1500]
    return text.strip()[:2500] or None


def evidence_for(entry: dict, legs: list[int]) -> dict[int, str]:
    """{leg: ESPN score text} for bound legs; any failure → no evidence."""
    picks = picks_of(entry)

    async def run() -> dict[int, str]:
        out = {}
        cache: dict[str, str | None] = {}
        for i in legs:
            if i >= len(picks):
                continue
            b = (entry.get("espn_events") or entry.get("soccer_events") or {}).get(str(i)) or {}
            ck = f"{b.get('id')}:{picks[i].get('player')}"
            if ck not in cache:
                try:
                    cache[ck] = await _leg_evidence(entry, i, picks[i])
                except Exception:  # noqa: BLE001 — evidence is best-effort
                    cache[ck] = None
            if cache[ck]:
                out[i] = cache[ck]
        return out

    try:
        return asyncio.run(run())
    except Exception:  # noqa: BLE001
        return {}


# ─── the call ─────────────────────────────────────────────────────────────────

def parse_flags(text: str, stage: str) -> list[dict] | None:
    """[{leg, problem[, message]}] or None when the answer has no usable object."""
    m = re.search(r"\{.*\}", text or "", re.DOTALL)
    if not m:
        return None
    try:
        obj = json.loads(m.group(0))
    except json.JSONDecodeError:
        return None
    flags = obj.get("flags") if isinstance(obj, dict) else None
    if not isinstance(flags, list):
        return None
    out = []
    for f in flags:
        if not isinstance(f, dict) or not str(f.get("problem") or "").strip():
            continue
        leg = f.get("leg")
        if not (isinstance(leg, int) or leg == "message"):
            leg = "message"
        row = {"leg": leg, "problem": str(f["problem"]).strip()[:400]}
        if stage == "nightly":
            if not f.get("message"):
                continue
            row["message"] = str(f["message"])
        out.append(row)
    return out


def review(stage: str, payload: str) -> tuple[list[dict] | None, dict]:
    """One subscription-billed call. (flags or None on any failure, meta)."""
    import claude_sub
    meta: dict[str, Any] = {"model": REVIEW_MODEL, "effort": REVIEW_EFFORT}
    started = time.monotonic()
    try:
        msg = asyncio.run(claude_sub.create(
            model=REVIEW_MODEL, effort=REVIEW_EFFORT, system=PROMPTS[stage],
            messages=[{"role": "user", "content": payload}], timeout=REVIEW_TIMEOUT))
    except Exception as exc:  # noqa: BLE001 — fails open
        meta.update(error=str(exc)[:300], wall_ms=int((time.monotonic() - started) * 1000))
        return None, meta
    meta["wall_ms"] = int((time.monotonic() - started) * 1000)
    flags = parse_flags(msg.content[0].text, stage)
    if flags is None:
        meta["error"] = "unparseable answer"
        meta["answer"] = (msg.content[0].text or "")[:300]
    return flags, meta


Reviewer = Callable[[str, str], tuple[list[dict] | None, dict]]
