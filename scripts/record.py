"""Bet-by-bet record for any capper or MOE expert, as a Telegram message.

    ~/venv/bin/python scripts/record.py god_judge                 # print the HTML
    ~/venv/bin/python scripts/record.py trent --market totals     # sides | totals | all
    ~/venv/bin/python scripts/record.py god_judge --send chat     # chat | me | test | <chat id>
    ~/venv/bin/python scripts/record.py --list                    # every known name

Read-only. Two sources, picked by name:
  - MOE experts/arms (god_judge, god_rules, ak, cee, …; registry id or name):
    the standing approved row per game — the decision at kickoff, the same rule
    as `moe_grade.py`'s scoreboard (`latest_per_game`) — graded by
    `moe_god.grade_all` against ESPN finals. Units = the row's stake_units
    (1u when an expert has no stake). Needs the NFL sheet (VPS only), ~1 min.
  - Telegram cappers (matched on capper_name, case/emoji-insensitive): the
    `grades` table in picks.db, one line per leg. The structured parse in
    parse_cache.json is used where the entry still exists (bet type, parlay
    flag, per-leg price); older rows fall back to the leg text. Flat 1u per
    pick, at the posted price (−110 when none was captured). Parlay legs and
    voided/ungraded legs are left out; a fanned-out pick counts once.

Layout (operator-picked 2026-10-09, "Mix A"): header (record · units · ROI,
✅❌ strip, last 5, streak, splits), then every group in date order (NFL week
for MOE, Mon–Sun week for cappers) with its record/units and a monospace
table. Split across messages at group boundaries when over Telegram's limit.
"""
from __future__ import annotations

import argparse
import html
import json
import os
import re
import sqlite3
import sys
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

ET = ZoneInfo("America/New_York")
MAX_MSG = 3900  # Telegram caps a message at 4096 chars of text
RESULT_EMOJI = {"W": "✅", "L": "❌", "P": "♻️"}
VERDICT = {"WIN": "W", "LOSS": "L", "PUSH": "P"}
SIDE_TYPES = {"spread", "moneyline", "double_chance", "draw_no_bet"}
TOTAL_TYPES = {"total", "team_total"}
MOE_ICONS = {"god_judge": "👑", "god_rules": "📐"}
MOE_TITLES = {"god_judge": "GOD JUDGE", "god_rules": "GOD RULES"}
BET_W = 18           # bet column width — the table must fit a phone without wrapping
MIN_CAPPER_PICKS = 5  # capper_name also holds junk header lines; list real cappers only


# ─── Bets ────────────────────────────────────────────────────────────────────
# One bet = {when: datetime (ET), group: str, game: str, bet: str,
#            price: int|None, units: float, result: W|L|P,
#            kind: side|total|other, final: str}


def payout(bet: dict) -> float:
    """Units won/lost, risking `units` at `price` (−110 when unknown)."""
    if bet["result"] == "P":
        return 0.0
    if bet["result"] == "L":
        return -bet["units"]
    price = bet["price"] or -110
    return bet["units"] * (100 / abs(price) if price < 0 else price / 100)


def record(bets: list[dict]) -> str:
    w = sum(b["result"] == "W" for b in bets)
    l = sum(b["result"] == "L" for b in bets)
    p = sum(b["result"] == "P" for b in bets)
    return f"{w}-{l}" + (f"-{p}" if p else "")


def filter_market(bets: list[dict], market: str) -> list[dict]:
    if market == "sides":
        return [b for b in bets if b["kind"] == "side"]
    if market == "totals":
        return [b for b in bets if b["kind"] == "total"]
    return list(bets)


# ─── Rendering ───────────────────────────────────────────────────────────────


def _units(x: float) -> str:
    return f"{x:+.2f}u"


def _odds(price) -> str:
    if price is None:
        return ""
    return f"+{price}" if price > 0 else str(price)


def _row(bet: dict, game_w: int, bet_w: int, show_game: bool, show_odds: bool, show_units: bool) -> str:
    parts = [f"{bet['when'].strftime('%a %-m/%-d'):<9}"]
    if show_game:
        parts.append(f"{bet['game']:<{game_w}}")
    label = bet["bet"] if len(bet["bet"]) <= bet_w else bet["bet"][:bet_w - 1] + "…"
    parts.append(f"{label:<{bet_w}}")
    if show_odds:
        parts.append(f"{_odds(bet['price']):>5}")
    if show_units:
        parts.append(f"{bet['units']:>3.1f}")
    parts.append(RESULT_EMOJI[bet["result"]])
    return " ".join(parts)


def render(title: str, bets: list[dict], market: str = "all", note: str = "") -> list[str]:
    """Telegram HTML messages (Bot API parse_mode=HTML), each ≤ MAX_MSG."""
    bets = sorted(bets, key=lambda b: b["when"])
    scope = {"sides": " · SIDES", "totals": " · TOTALS"}.get(market, "")
    head = [f"<b>{html.escape(title)}</b>{scope}"]
    if not bets:
        return ["\n".join(head + ["", "No graded bets yet."])]
    pl = sum(payout(b) for b in bets)
    risk = sum(b["units"] for b in bets)
    head.append(f"<b>{record(bets)}</b>  ·  <b>{_units(pl)}</b>  ·  ROI {100 * pl / risk:+.1f}%")
    head.append("")
    strip = bets[-20:]
    head.append(("…" if len(bets) > len(strip) else "") + "".join(RESULT_EMOJI[b["result"]] for b in strip))
    last = bets[-1]["result"]
    streak = 0
    for b in reversed(bets):
        if b["result"] != last:
            break
        streak += 1
    head.append(f"Last 5: {record(bets[-5:])}  ·  Streak: {last}{streak}")
    splits = []
    if market == "all":
        for label, kind in (("Sides", "side"), ("Totals", "total"), ("Other", "other")):
            sub = [b for b in bets if b["kind"] == kind]
            if sub:
                splits.append(f"{label} {record(sub)}")
    top = max(b["units"] for b in bets)
    if any(b["units"] != top for b in bets):
        splits.append(f"{top:g}u bets: {record([b for b in bets if b['units'] == top])}")
    if splits:
        head.append("  ·  ".join(splits))
    if note:
        head.append(f"<i>{html.escape(note)}</i>")

    show_game = any(b["game"] for b in bets)
    show_odds = any(b["price"] is not None for b in bets) and not show_game
    show_units = len({b["units"] for b in bets}) > 1
    game_w = max((len(b["game"]) for b in bets), default=0)
    bet_w = min(BET_W, max(len(b["bet"]) for b in bets))
    groups: dict[str, list[dict]] = {}
    for b in bets:
        groups.setdefault(b["group"], []).append(b)
    blocks = []
    for name, gb in groups.items():
        table = "\n".join(_row(b, game_w, bet_w, show_game, show_odds, show_units) for b in gb)
        blocks.append(
            f"<b>{html.escape(name)}</b>  ·  {record(gb)}  ·  {_units(sum(payout(b) for b in gb))}\n"
            f"<pre>{html.escape(table)}</pre>"
        )

    messages, cur = [], "\n".join(head)
    for block in blocks:
        if len(cur) + 2 + len(block) > MAX_MSG:
            messages.append(cur)
            cur = f"<b>{html.escape(title)}</b>{scope} <i>(cont.)</i>"
        cur += "\n\n" + block
    messages.append(cur)
    return messages


# ─── Source: MOE experts ─────────────────────────────────────────────────────


def moe_experts() -> dict[str, str]:
    import moe_god
    registry = moe_god.load_registry()
    return {k: str(v.get("name") or k) for k, v in registry["experts"].items()
            if isinstance(v, dict) and v.get("enabled")}


def _nfl_abbr() -> dict[str, str]:
    from nfl_win_predictions import TEAM_ABBREVIATIONS
    return TEAM_ABBREVIATIONS


def moe_bets(expert_id: str) -> list[dict]:
    import moe_grade as mg  # loads .env + .env.local
    from moe import approved_opinions, configured_opinion_store
    from moe_god import _row_order, aggregator_policy, grade_all, load_registry

    creds = os.environ.get("GOOGLE_CREDENTIALS")
    sheet_id = os.environ.get("NFL_INTAKE_SHEET_ID")
    if not creds or not sheet_id:
        raise SystemExit("GOOGLE_CREDENTIALS and NFL_INTAKE_SHEET_ID are required (run on the VPS)")
    ss = mg.get_gspread_client(creds).open_by_key(sheet_id)
    history = ss.worksheet(mg.GAME_HISTORY_TAB).get_all_records(expected_headers=mg.GAME_HISTORY_HEADERS)
    annotations = mg.load_game_annotations(ss)
    history = mg.attach_game_annotations(history, annotations)
    approved = approved_opinions(configured_opinion_store().list())
    mine = [r for r in approved if r.get("expert_id") == expert_id]
    if not mine:
        return []
    season = max(int(r["season"]) for r in mine if str(r.get("season") or "").strip())
    events = mg.fetch_regular_season_events(season, expected_games=None)
    finals = mg.attach_game_annotations(
        mg.build_game_history({season: events}, {season: mg._latest_alignment(history)},
                              validate=False, require_complete_divisional_pairs=False),
        annotations,
    )
    snapshots = ss.worksheet("nfl_line_snapshots").get_all_records(expected_headers=mg.SNAPSHOT_HEADERS)
    registry = load_registry()
    graded = {g["opinion_id"]: g for g in grade_all(
        mine, finals=finals, snapshots=snapshots, registry=registry, policy=aggregator_policy(registry))}
    # The standing row per game: the latest decision (moe_grade's latest_per_game).
    standing: dict[str, dict] = {}
    for row in mine:
        if row["opinion_id"] in graded:
            key = row["event_id"]
            if key not in standing or _row_order(row) > _row_order(standing[key]):
                standing[key] = row
    abbr = _nfl_abbr()
    bets = []
    for row in standing.values():
        g = graded[row["opinion_id"]]
        a = abbr.get(row["away_team"], row["away_team"])
        h = abbr.get(row["home_team"], row["home_team"])
        when = datetime.fromisoformat(str(row["commence_time_utc"]).replace("Z", "+00:00")).astimezone(ET)
        for leg in g["legs"]:
            pick = json.loads(row.get(f"{leg['kind']}_pick_json") or "{}")
            line = leg["line"]
            if leg["kind"] == "total":
                label = ("o" if leg["selection"] == "Over" else "u") + f"{line:g}"
            else:
                team = abbr.get(leg["selection"], leg["selection"])
                label = f"{team} {'PK' if not line else f'{line:+g}'}"
            stake = pick.get("stake_units")
            bets.append({
                "when": when, "group": f"Week {row.get('week')}", "game": f"{a}@{h}", "bet": label,
                "price": int(pick["price"]) if pick.get("price") not in (None, "") else None,
                "units": float(stake) if stake not in (None, "", 0) else 1.0,
                "result": leg["result"], "kind": leg["kind"], "final": g["final"],
            })
    return bets


# ─── Source: Telegram cappers ────────────────────────────────────────────────


def _norm(name: str) -> str:
    return re.sub(r"[^a-z0-9]", "", str(name or "").lower())


def capper_names(db_path: Path) -> dict[str, set[str]]:
    """normalized → the raw capper_name spellings with graded rows."""
    con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    out: dict[str, set[str]] = {}
    counts: dict[str, int] = {}
    for name, n in con.execute(
            "SELECT capper_name, COUNT(*) FROM grades WHERE dry_run = 0 AND capper_name != '' "
            "AND verdict IN ('WIN', 'LOSS', 'PUSH', 'UNKNOWN') GROUP BY capper_name"):
        if _norm(name):
            out.setdefault(_norm(name), set()).add(name)
            counts[_norm(name)] = counts.get(_norm(name), 0) + n
    con.close()
    return {k: v for k, v in out.items() if counts[k] >= MIN_CAPPER_PICKS}


def short_label(desc: str) -> str:
    """'Boston Red Sox moneyline (-107)' → 'Red Sox ML' — the leg text of a row
    whose structured parse is gone (parse_cache is pruned; grades is not)."""
    from audit import _TEAM_SHORT_NAMES
    d = re.sub(r"\s*\([^)]*\)", "", desc)
    d = re.sub(r"\b(regulation time|total (runs|goals|points)( in)?)\b", "", d, flags=re.I)
    d = re.sub(r"\bboth teams to score\b[^|]*", "BTTS", d, flags=re.I)
    d = re.sub(r"\s+(vs\.?|v|and|x)\s+", "/", d, flags=re.I)
    d = re.sub(r"\s+[—–-]\s+[A-Za-z].*$", "", d)  # " — ALDS", " - England vs Argentina"
    if re.search(r"\b(to win|game winner|to advance|moneyline)\b", d, re.I):
        d = re.sub(r"\s*\b(to win|game winner|to advance|moneyline)\b.*$", "", d, flags=re.I) + " ML"
    d = re.sub(r"\b(spread|run line|puck line)\b", "", d, flags=re.I)
    d = re.sub(r"\b[Oo]ver\s+(\d)", r"o\1", d)
    d = re.sub(r"\b[Uu]nder\s+(\d)", r"u\1", d)
    for full in sorted(_TEAM_SHORT_NAMES, key=len, reverse=True):
        if full in d.casefold():
            d = re.sub(re.escape(full), _TEAM_SHORT_NAMES[full], d, flags=re.I)
    return " ".join(d.split())


_LEG_RE = re.compile(r"^(WIN|LOSS|PUSH|VOID|UNKNOWN|PENDING): (.+?)\|([^|]*)\|(\d{4}-\d\d-\d\d)\|")
_PRICE_RE = re.compile(r"\(([+-]\d{3,4})\)|\s([+-]\d{3,4})(?:\s|$)")
_TOTAL_RE = re.compile(r"\b(over|under|total|o\d|u\d|nrfi|yrfi)", re.I)
_SIDE_RE = re.compile(r"\b(ml|moneyline|to win)\b|\s[+-]\d+(\.5)?\b|\bpk\b", re.I)
_PROP_RE = re.compile(r"\b(td|touchdown|scorer|strikeouts?|yards|points|rebounds|assists|hits|shots|props?)\b", re.I)


def _kind(bet_type: str | None, desc: str) -> str:
    if bet_type in SIDE_TYPES:
        return "side"
    if bet_type in TOTAL_TYPES:
        return "total"
    if bet_type:
        return "other"
    if _PROP_RE.search(desc):
        return "other"
    if _TOTAL_RE.search(desc):
        return "total"
    if _SIDE_RE.search(desc):
        return "side"
    return "other"


def _week_group(d: date) -> str:
    start = d - timedelta(days=d.weekday())
    end = start + timedelta(days=6)
    return f"{start.strftime('%b %-d')}–{end.strftime('%-d') if end.month == start.month else end.strftime('%b %-d')}"


def capper_bets(raw_names: set[str], db_path: Path, cache_path: Path) -> list[dict]:
    from audit import _format_pick
    try:
        cache = json.loads(cache_path.read_text())
    except (OSError, ValueError):
        cache = {}
    con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    marks = ",".join("?" * len(raw_names))
    rows = con.execute(
        f"SELECT channel_id, message_id, date, bet_type, odds, pick_desc, graded_at FROM grades "
        f"WHERE dry_run = 0 AND capper_name IN ({marks}) ORDER BY graded_at", tuple(raw_names)).fetchall()
    con.close()
    bets, seen = [], set()
    for channel_id, message_id, row_date, row_type, row_odds, pick_desc, _ in rows:
        entry = cache.get(f"{channel_id}:{message_id}")
        picks = (entry or {}).get("parsed", {}).get("picks") if isinstance(entry, dict) else None
        legs = [m for m in (_LEG_RE.match(line) for line in (pick_desc or "").split("\n")) if m]
        for i, m in enumerate(legs):
            verdict, desc, _sport, game_date = m.groups()
            if verdict not in VERDICT:
                continue
            pick = picks[i] if picks and len(picks) == len(legs) else None
            if pick is not None:
                if pick.get("is_parlay_leg"):
                    continue
                label = _format_pick(pick)
                kind = _kind(pick.get("bet_type"), desc)
                odds = ((entry.get("odds_by_pick") or {}).get(str(i)) or {}).get("odds")
            else:
                if "parlay" in desc.lower():
                    continue
                label = short_label(desc)
                kind = _kind(row_type if len(legs) == 1 else None, desc)
                found = _PRICE_RE.search(desc)
                odds = int(found.group(1) or found.group(2)) if found else (row_odds if len(legs) == 1 else None)
            key = (game_date, _norm(label), verdict)
            if key in seen:  # the same pick fanned out to several channels
                continue
            seen.add(key)
            d = date.fromisoformat(game_date)
            bets.append({
                "when": datetime(d.year, d.month, d.day, 12, tzinfo=ET), "group": _week_group(d),
                "game": "", "bet": label, "price": int(odds) if odds else None, "units": 1.0,
                "result": VERDICT[verdict], "kind": kind, "final": "",
            })
    return bets


# ─── CLI ─────────────────────────────────────────────────────────────────────


# The Claude chat bot's token (the Telegram plugin's own .env): "chat" lands the
# record in the operator's conversation with @ForwarderClaudeBot. Sending never
# touches the plugin's getUpdates poller.
CLAUDE_BOT_ENV = Path.home() / ".claude" / "channels" / "telegram" / ".env"


def _token(target: str) -> str:
    if target == "chat":
        from dotenv import dotenv_values
        return dotenv_values(CLAUDE_BOT_ENV)["TELEGRAM_BOT_TOKEN"]
    return os.environ["WATCHDOG_BOT_TOKEN"] if target == "me" else os.environ["BOT_TOKEN"]


def _chat_id(target: str) -> int:
    if target in ("me", "chat"):
        return int(os.environ["WATCHDOG_USER_ID"])
    if target == "test":
        mappings = json.loads(os.environ.get("MAPPINGS_CONFIG") or "[]")
        mappings = mappings if isinstance(mappings, list) else mappings.get("mappings", [])
        ids = {m.get("test_dest_channel") for m in mappings if m.get("test_dest_channel")}
        if len(ids) != 1:
            raise SystemExit(f"can't resolve the TEST channel from MAPPINGS_CONFIG: {ids}")
        return int(ids.pop())
    return int(target)


def send(messages: list[str], target: str) -> None:
    import httpx
    token = _token(target)
    chat = _chat_id(target)
    for text in messages:
        r = httpx.post(f"https://api.telegram.org/bot{token}/sendMessage",
                       json={"chat_id": chat, "text": text, "parse_mode": "HTML",
                             "disable_web_page_preview": True}, timeout=20).json()
        if not r.get("ok"):
            raise SystemExit(f"send failed: {r.get('description')}")
        print(f"sent message {r['result']['message_id']} to {chat}")


def main(argv: list[str] | None = None) -> int:
    from dotenv import load_dotenv
    load_dotenv(ROOT / ".env")
    load_dotenv(ROOT / ".env.local", override=True)

    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("name", nargs="?", help="MOE expert id/name or capper name")
    ap.add_argument("--market", choices=("all", "sides", "totals"), default="all")
    ap.add_argument("--send", metavar="TARGET",
                    help="chat (Claude bot DM) | me (watchdog bot DM) | test (TEST channel) | <chat id>; "
                         "default prints")
    ap.add_argument("--list", action="store_true", help="list every known expert and capper")
    ap.add_argument("--db", default=str(ROOT / "picks.db"))
    ap.add_argument("--cache", default=str(ROOT / "parse_cache.json"))
    args = ap.parse_args(argv)

    experts = moe_experts()
    cappers = capper_names(Path(args.db))
    if args.list or not args.name:
        print("MOE experts: " + ", ".join(sorted(experts)))
        print("Cappers: " + ", ".join(sorted(min(v) for v in cappers.values())))
        return 0

    want = _norm(args.name)
    expert = next((k for k, v in experts.items() if want in (_norm(k), _norm(v))), None)
    if expert:
        bets = moe_bets(expert)
        title = f"{MOE_ICONS.get(expert, '🏈')} {MOE_TITLES.get(expert, experts[expert].upper())} · NFL"
        note = ""
    else:
        hits = [k for k in cappers if k == want] or [k for k in cappers if want in k]
        if len(hits) != 1:
            print(f"{'No' if not hits else 'Ambiguous'} match for {args.name!r}: "
                  + ", ".join(sorted(min(cappers[h]) for h in hits)) if hits else
                  f"No expert or capper matches {args.name!r} (try --list)", file=sys.stderr)
            return 2
        raw = cappers[hits[0]]
        bets = capper_bets(raw, Path(args.db), Path(args.cache))
        title = f"📊 {re.sub(r'^[^A-Za-z0-9]+', '', min(raw, key=len)).upper()}"
        note = "Flat 1u per pick · parlays excluded"
    messages = render(title, filter_market(bets, args.market), args.market, note)
    if args.send:
        send(messages, args.send)
    else:
        print("\n\n────────\n\n".join(messages))
    return 0


if __name__ == "__main__":
    sys.exit(main())
