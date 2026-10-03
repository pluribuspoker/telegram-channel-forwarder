"""Pre-publish odds gate — review a freshly priced message BEFORE its tags post.

Called by the tracker right after it prices a message's legs and before it
edits the odds tags in (tracker.py, "Fetch odds at first encounter"). Two
reviewers, both judging the tags we are ABOUT to post:

1. the free deterministic checks (odds_checks.scan_entry — alt-line band,
   same-game parlay legs, teaser price, wrong-game kickoff, …);
2. ONE Claude review of the whole message (Opus 5.5 via claude_sub —
   SUBSCRIPTION-billed headless `claude -p`, never ANTHROPIC_API_KEY,
   operator rule 2026-10-03), which catches what
   looks normal but prices a different bet (an F5 bet at the full-game ML,
   a team total for a game total, the wrong side).

A leg either flags gets `hold` = {why, at} in its odds_by_pick entry:
_insert_odds/_held_odds show no tag for it (a parlay loses its combined
tag), and odds-watch.service is started at once — its agent judges, the
runner releases the hold (legit → the tag posts minutes late; wrong → the
corrected price posts). A due miss (no price found) starts it too, so the
agent can find a free source. Everything fails OPEN: a review that errors,
times out or returns garbage holds nothing (the timer backstop still scans).

Fan-out copies are priced independently, so the Claude verdict is cached per
SOURCE message (logs/odds_review_cache.json, REVIEW_REUSE_HOURS) — one call
per source, not per copy. Kill switch: ODDS_REVIEW_DISABLED=1 (Claude review
only; the free checks still hold). Test: scripts/test_odds_watch.py.
"""

import json
import os
import re
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

import odds_checks as oc

ROOT = Path(__file__).resolve().parent
REVIEW_CACHE = ROOT / "logs" / "odds_review_cache.json"
REVIEW_LOG = ROOT / "logs" / "odds_review.jsonl"
REVIEW_MODEL = os.environ.get("ODDS_REVIEW_MODEL") or "claude-opus-5-5"
REVIEW_EFFORT = os.environ.get("ODDS_REVIEW_EFFORT") or "medium"
REVIEW_TIMEOUT = float(os.environ.get("ODDS_REVIEW_TIMEOUT") or 150)
REVIEW_REUSE_HOURS = 2

SYSTEM_PROMPT = """You review sports-betting odds tags before they are posted.

A capper posted a pick in a Telegram channel. Our software parsed the bet(s)
and looked up a price for each leg from sportsbook feeds; that price is about
to be appended to the post as a tag like [-110]. Your job: catch a tag that
prices a DIFFERENT bet than the one the capper made, or a price no book would
offer for that exact bet.

Flag a leg only when you are fairly confident the price is wrong, e.g.:
- wrong market or period: an F5 / 1st-half / 1st-quarter / 1st-inning bet
  priced like the full game (or the reverse); a team total priced as a game
  total (or the reverse); a player prop priced off a different stat;
- wrong side, team, or game (a price that fits the opponent, or a game on a
  different day);
- the parse doesn't match the text (wrong line, wrong direction, a teaser leg
  at the un-teased line, legs combined that the capper bet separately);
- a parlay whose legs are on the same game multiplied as if independent;
- a price wildly off for the line (an over on a line well below the market
  priced as an underdog, a -3 spread at +250, a main-line total at -400);
- the capper states their own price and ours is far from it for no reason.

Do NOT flag: genuine long shots or heavy favorites, alternate lines the capper
chose (priced long or short accordingly), normal book-to-book differences
(10-15 cents), live/in-game prices, or a leg simply because you are unsure.
`match_type` tells how the price was found: `exact` = the book quoted this
exact line; `proximity_Npts` = estimated from a line N points away (fine for
a half point or so; suspicious when far from a key number).

Return exactly one JSON object and nothing else:
{"suspect": [{"leg": <leg index, or "ticket" for a parlay's combined price>,
              "reason": "<one short sentence>"}]}
Use {"suspect": []} when every tag looks right."""


# ─── Claude review (subscription) ─────────────────────────────────────────────

def review_payload(picks: list[dict], odds_by_pick: dict, raw_text: str,
                   entry: dict) -> str:
    """The user message the reviewer reads: the post, then every leg with
    the price we're about to show and how it was found."""
    from common import parlay_combined_odds
    bound = entry.get("espn_events") or entry.get("soccer_events") or {}
    legs = []
    for i, p in enumerate(picks):
        o = odds_by_pick.get(str(i)) or {}
        b = bound.get(str(i)) or {}
        legs.append({
            "leg": i,
            **{k: p.get(k) for k in ("description", "sport", "bet_type", "period",
                                     "line", "direction", "player", "prop_stat",
                                     "is_parlay_leg")},
            "price": o.get("odds"), "match_type": o.get("match_type"),
            "book": o.get("bookmaker"), "book_line": o.get("api_line"),
            "game_start_utc": o.get("commence_time"),
            "bound_game": f"{b.get('name')} {b.get('kickoff')}" if b.get("name") else None,
        })
    body: dict[str, Any] = {"post": raw_text.strip()[:3000], "legs": legs}
    parlay = [oc.price_of(odds_by_pick.get(str(i)) or {})
              for i, p in enumerate(picks) if p.get("is_parlay_leg")]
    if len(parlay) >= 2:
        body["parlay_combined_price"] = parlay_combined_odds(parlay)
    return json.dumps(body, indent=1, default=str)


def parse_review(text: str) -> list[dict] | None:
    """[{leg, reason}] or None when the answer has no usable object."""
    m = re.search(r"\{.*\}", text or "", re.DOTALL)
    if not m:
        return None
    try:
        obj = json.loads(m.group(0))
    except json.JSONDecodeError:
        return None
    sus = obj.get("suspect") if isinstance(obj, dict) else None
    if not isinstance(sus, list):
        return None
    out = []
    for s in sus:
        if isinstance(s, dict) and (isinstance(s.get("leg"), int) or s.get("leg") == "ticket"):
            out.append({"leg": s["leg"], "reason": str(s.get("reason") or "")[:300]})
    return out


def claude_review(user_message: str) -> tuple[list[dict] | None, dict]:
    """One subscription-billed call (claude_sub — the app's only Claude path).
    Runs in the tracker's worker thread, so it owns a fresh event loop.
    Returns (suspects or None on any failure, call metadata)."""
    import asyncio
    import claude_sub
    meta: dict[str, Any] = {"model": REVIEW_MODEL, "effort": REVIEW_EFFORT}
    started = time.monotonic()
    try:
        msg = asyncio.run(claude_sub.create(
            model=REVIEW_MODEL, effort=REVIEW_EFFORT, system=SYSTEM_PROMPT,
            messages=[{"role": "user", "content": user_message}],
            timeout=REVIEW_TIMEOUT))
    except Exception as exc:  # noqa: BLE001 — the gate fails open
        meta.update(error=str(exc)[:300], wall_ms=int((time.monotonic() - started) * 1000))
        return None, meta
    meta.update(wall_ms=int((time.monotonic() - started) * 1000),
                usage={"in": msg.usage.input_tokens, "out": msg.usage.output_tokens})
    suspects = parse_review(msg.content[0].text)
    if suspects is None:
        meta["error"] = "unparseable answer"
    return suspects, meta


# ─── the gate ─────────────────────────────────────────────────────────────────

def _load_review_cache() -> dict:
    try:
        return json.loads(REVIEW_CACHE.read_text())
    except (OSError, json.JSONDecodeError):
        return {}


def _save_review_cache(data: dict) -> None:
    cutoff = time.time() - REVIEW_REUSE_HOURS * 3600
    data = {k: v for k, v in data.items() if v.get("at", 0) >= cutoff}
    try:
        REVIEW_CACHE.parent.mkdir(parents=True, exist_ok=True)
        tmp = REVIEW_CACHE.with_suffix(f".json.tmp.{os.getpid()}")
        tmp.write_text(json.dumps(data))
        os.replace(tmp, REVIEW_CACHE)
    except OSError:
        pass


def _log(record: dict) -> None:
    try:
        REVIEW_LOG.parent.mkdir(parents=True, exist_ok=True)
        with REVIEW_LOG.open("a") as fh:
            fh.write(json.dumps(record, default=str) + "\n")
    except OSError:
        pass


def gate(cache_key: str, entry: dict, picks: list[dict], odds_by_pick: dict, *,
         raw_text: str, tagged_html: str, fresh: set[int],
         now: datetime | None = None,
         reviewer: Callable[[str], tuple[list[dict] | None, dict]] | None = None,
         review_enabled: bool | None = None) -> dict[str, Any]:
    """Review the legs priced THIS pass (`fresh`) and hold the suspicious
    ones IN PLACE (odds_by_pick[i]["hold"]). Returns {held: {leg: [why]},
    trigger: bool (start odds-watch now), hits, review}."""
    now = now or datetime.now(timezone.utc)
    candidate = {"parsed": {"picks": picks}, "odds_by_pick": odds_by_pick,
                 "html_text": tagged_html,
                 "espn_events": entry.get("espn_events"),
                 "soccer_events": entry.get("soccer_events")}
    hits = oc.scan_entry(cache_key, candidate, now=now)
    parlay_legs = {i for i, p in enumerate(picks) if p.get("is_parlay_leg")}
    priced_fresh = {i for i in fresh if oc.price_of(odds_by_pick.get(str(i)) or {}) is not None
                    and not oc.is_live(odds_by_pick.get(str(i)) or {})}

    def fresh_leg(i: int) -> bool:
        # A parlay's combined tag is new whenever any of its legs is.
        return i in priced_fresh or (i in parlay_legs and bool(priced_fresh & parlay_legs))

    held: dict[int, list[str]] = {i: why for i, why in oc.holdable(hits, picks).items()
                                  if fresh_leg(i)}

    review: dict[str, Any] = {"ran": False}
    if review_enabled is None:
        review_enabled = os.environ.get("ODDS_REVIEW_DISABLED") != "1"
    if priced_fresh and review_enabled:
        src = str(entry.get("_source_key") or cache_key)
        rc = _load_review_cache()
        prior = rc.get(src)
        sig = [[p.get("description"), (odds_by_pick.get(str(i)) or {}).get("odds")]
               for i, p in enumerate(picks)]
        if prior and prior.get("legs") == sig:
            suspects, meta = prior.get("suspect"), {"reused": True}
        else:
            message = review_payload(picks, odds_by_pick, raw_text, entry)
            suspects, meta = (reviewer or claude_review)(message)
            if suspects is not None:
                rc[src] = {"at": time.time(), "suspect": suspects, "legs": sig}
                _save_review_cache(rc)
        review = {"ran": True, "suspect": suspects, **meta}
        for s in suspects or []:
            targets = sorted(parlay_legs) if s["leg"] == "ticket" else [s["leg"]]
            for i in targets:
                if 0 <= i < len(picks) and fresh_leg(i):
                    held.setdefault(i, []).append(f"review: {s['reason']}")

    stamp = now.isoformat(timespec="seconds")
    for i, why in held.items():
        o = odds_by_pick.get(str(i))
        if isinstance(o, dict) and oc.price_of(o) is not None:
            o["hold"] = {"why": why, "at": stamp}
    held = {i: w for i, w in held.items() if (odds_by_pick.get(str(i)) or {}).get("hold")}
    misses = [h for h in hits if h["rule"] == "miss" and h["idx"] in fresh]
    out = {"held": held, "trigger": bool(held or misses), "hits": hits, "review": review}
    _log({"ts": stamp, "key": cache_key, "fresh": sorted(fresh),
          "held": {str(k): v for k, v in held.items()},
          "misses": [h["detail"] for h in misses],
          "review": {k: v for k, v in review.items() if k != "usage"}})
    return out


def trigger_watch() -> None:
    """Start odds-watch.service now (fire-and-forget). A start while it runs
    is coalesced by systemd; the runner's post-run rescan picks the flag up."""
    if os.environ.get("ODDS_WATCH_DISABLED") == "1":
        return
    try:
        subprocess.Popen(["sudo", "-n", "systemctl", "start", "--no-block", "odds-watch.service"],
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except OSError:
        pass
