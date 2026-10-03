"""Deterministic suspicion checks over a priced pick — shared by the
pre-publish gate (odds_gate.py, inside the tracker) and the odds watch
(scripts/odds_watch.py). Pure: no I/O, no network, no Claude.

Every hit is a SUSPICION for a judging agent, never a verdict — long prices
are often right. A hit: {key, idx, rule, price, detail}; idx -1 = the ticket
(parlay) or the message as a whole. Thresholds and the false-positive filters
are tuned by replaying the whole cache (`scripts/odds_watch.py --scan --days
120`), never by guessing. Test: scripts/test_odds_watch.py.
"""

import re
from datetime import datetime, timedelta, timezone
from typing import Any

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
MISS_WINDOW_H = 3          # retryable misses: flag once kickoff is this close

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
    "hold": "HELD before posting by the pre-publish review",
    "miss": "no price found",
}

# Misses the tracker keeps re-fetching (free sources, every 30 min until
# kickoff) — a book lists periods/props only in a pregame window.
_RETRYABLE_MISS_RE = re.compile(r"no_game|prop_not_found|no_\w+_data")
# Misses that are a whole CLASS (a market/sport with no source wired): one
# "no free source" verdict covers every pick of the class for a while.
_CLASS_MISS_RE = re.compile(
    r"(?:prop_stat_unsupported|sport_unsupported|unsupported_bet_type)\(.*\)"
    r"|team_total_unavailable|player_prop_unavailable")
# Never worth an agent: the pick was live when priced, or nothing was fetched.
_SKIP_MISS = {"game_in_progress", "dry_run"}


def fmt(price: int) -> str:
    return f"+{price}" if price > 0 else str(price)


def implied(price: int) -> float:
    return 100 / (price + 100) if price > 0 else -price / (-price + 100)


def ts(value) -> datetime | None:
    try:
        t = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    return t if t.tzinfo else t.replace(tzinfo=timezone.utc)


def price_of(o: dict) -> int | None:
    p = o.get("odds") if isinstance(o, dict) else None
    return p if isinstance(p, int) and not isinstance(p, bool) else None


def is_live(o: dict) -> bool:
    return "live" in str(o.get("match_type") or "")


def miss_class(match_type: str) -> str | None:
    """The class key a "no free source" verdict parks, or None for a
    per-game miss (no_game, no_spread_data, …) that only parks itself."""
    return match_type if _CLASS_MISS_RE.fullmatch(match_type or "") else None


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


def _miss_due(o: dict, now: datetime) -> bool:
    """A miss is worth an agent unless the tracker's own free retries may
    still fill it: retryable types wait until kickoff is near (or, with no
    kickoff known, until two retries have failed)."""
    mt = str(o.get("match_type") or "")
    if not mt or mt in _SKIP_MISS or is_live(o):
        return False
    if not _RETRYABLE_MISS_RE.fullmatch(mt):
        return True
    ct = ts(o.get("commence_time"))
    if ct:
        return ct - now <= timedelta(hours=MISS_WINDOW_H)
    return int(o.get("_retry_n") or 0) >= 2


def scan_entry(key: str, entry: dict, *, now: datetime | None = None,
               misses: bool = True) -> list[dict[str, Any]]:
    """Every suspicion on one cache entry (or a candidate about to be posted:
    html_text = the text WITH our tags placed)."""
    now = now or datetime.now(timezone.utc)
    picks = (entry.get("parsed") or {}).get("picks") or []
    odds = entry.get("odds_by_pick") or {}
    text = entry.get("html_text") or ""
    hits: list[dict[str, Any]] = []

    def hit(idx: int, rule: str, price: int | None, detail: str, **extra) -> None:
        hits.append({"key": key, "idx": idx, "rule": rule, "price": price,
                     "detail": detail, **extra})

    legs = [i for i, p in enumerate(picks) if p.get("is_parlay_leg")]
    teaser = bool(_TEASER_RE.search(text))
    for i, pick in enumerate(picks):
        o = odds.get(str(i)) or {}
        price = price_of(o)
        bt = pick.get("bet_type") or ""
        mt = str(o.get("match_type") or "")
        if price is None:
            if misses and o and _miss_due(o, now):
                hit(i, "miss", None, f"no price for {bt} ({mt})",
                    miss_class=miss_class(mt), match_type=mt)
            continue
        if is_live(o):
            continue
        what = f"{fmt(price)} on {bt} ({mt or 'no match_type'})"
        if o.get("hold"):
            # The review already judged it suspicious; its reasons ARE the flag.
            hit(i, "hold", price, f"{what}: " + "; ".join(
                str(r) for r in (o["hold"].get("why") or [])),
                hold_at=o["hold"].get("at"))
            continue
        # The capper's own number in the pick ("Elkins ML (+500)" at +561)
        # corroborates a long price — the band checks stand down.
        stated = [int(s) for s in _SRC_PRICE_RE.findall(pick.get("description") or "")]
        # A straight price nobody sees isn't worth an agent: when the capper
        # states their own number, _insert_odds shows ours only on a big move
        # (the `now` check below) — "Michigan -3 -160" carries no tag at all.
        shown = pick.get("is_parlay_leg") or re.search(
            rf"\[{re.escape(fmt(price))}\b", text)
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
        k_odds, k_bound = ts(o.get("commence_time")), ts(bound.get("kickoff"))
        if (k_odds and k_bound and bound.get("status") == "bound"
                and abs((k_odds - k_bound).total_seconds()) > KICKOFF_GAP_H * 3600):
            hit(i, "game_mismatch", price,
                f"odds game {o.get('commence_time')} vs bound "
                f"{bound.get('name')} {bound.get('kickoff')}")

    if len(legs) >= 2:
        leg_odds = [odds.get(str(i)) or {} for i in legs]
        leg_prices = [price_of(o) for o in leg_odds]
        if (all(p is not None for p in leg_prices) and not any(map(is_live, leg_odds))
                and not any(o.get("hold") for o in leg_odds)):
            from common import parlay_combined_odds
            comb = parlay_combined_odds(leg_prices)
            pairs = _same_game_pairs(picks, legs)
            if pairs:
                hit(-1, "parlay_same_game", comb, "legs " + ", ".join(
                    f"{a}+{b} ({', '.join(sorted(_team_set(picks[a]) & _team_set(picks[b])))})"
                    for a, b in pairs) + f" multiplied to {fmt(comb)}")
            if teaser and comb is not None and comb > TEASER_MAX:
                hit(-1, "teaser_long", comb, f"{len(legs)}-leg teaser at {fmt(comb)}")

    # The capper's own stated price vs ours ("Bills -3 (-110) [-190 now]").
    for line in text.split("\n"):
        now_tag = _NOW_TAG_RE.search(line)
        if not now_tag:
            continue
        ours = int(now_tag.group(1))
        stated = [int(s) for s in _SRC_PRICE_RE.findall(_NOW_TAG_RE.sub("", line))]
        if stated and min(abs(implied(s) - implied(ours)) for s in stated) > NOW_MOVE:
            hit(-1, "now_move", ours, f"stated {', '.join(fmt(s) for s in stated)}"
                f" vs ours {fmt(ours)}: {re.sub(r'<[^>]+>', '', line).strip()[:80]}")
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
            price = price_of(o)
            if price is None or is_live(o) or not i.isdigit() or int(i) >= len(picks):
                continue
            fp = (str(entry.get("capper_name") or "").strip().lower(),
                  re.sub(r"\s+", " ", (picks[int(i)].get("description") or "").lower()),
                  o.get("game_date"))
            groups.setdefault(fp, []).append((key, int(i), price))
    hits = []
    for copies in groups.values():
        prices = [p for _, _, p in copies]
        if (len({k for k, _, _ in copies}) < 2
                or max(map(implied, prices)) - min(map(implied, prices)) <= FANOUT_GAP):
            continue
        detail = "copies priced " + " / ".join(f"{k}={fmt(p)}" for k, _, p in sorted(copies))
        for key, i, price in copies:
            hits.append({"key": key, "idx": i, "rule": "fanout_price",
                         "price": price, "detail": detail})
    return hits


def holdable(hits: list[dict], picks: list[dict]) -> dict[int, list[str]]:
    """Leg index -> reasons, for the hits that pin a DISPLAYED price to legs:
    a ticket-level parlay hit holds every parlay leg (the combined tag goes
    down with any one of them). Misses and message-level `now_move` hold
    nothing — there is no price of ours to hide, or no single leg to pin."""
    out: dict[int, list[str]] = {}
    legs = [i for i, p in enumerate(picks) if p.get("is_parlay_leg")]
    for h in hits:
        if h["rule"] in ("miss", "now_move", "hold"):
            continue
        targets = legs if h["idx"] < 0 else [h["idx"]]
        for i in targets:
            out.setdefault(i, []).append(f"{h['rule']}: {h['detail']}")
    return out
