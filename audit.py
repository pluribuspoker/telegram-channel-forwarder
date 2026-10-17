"""
audit.py — Audit log for the pick-tracker bot.

Every time the bot grades a pick (or would grade one in dry-run mode) it:
  1. Writes a row to a local SQLite database  (picks.db)
  2. Posts a summary to a private Telegram audit channel  (AUDIT_CHANNEL_ID)

The DB is the source of truth for "what has been graded" so the nightly cron
can skip already-processed messages.  The Telegram channel gives a human-readable
real-time stream of bot actions that can be reviewed on any device.

Usage (from other modules):
    from audit import AuditLog
    audit = AuditLog()          # opens/creates picks.db, reads env vars
    await audit.record(...)     # write DB row + post to Telegram
"""

import asyncio
import hashlib
import json
import os
import time
import urllib.request
import re
import sqlite3
from datetime import datetime, timedelta, timezone

import httpx

from common import (VERDICT_EMOJI, is_btts_as_total, is_regulation_ml,
                    ticket_combined_odds)
from tracker_grading import _overall_verdict


def _clean_desc(desc: str) -> str:
    """Fallback: strip odds and normalize wording from a raw description string."""
    desc = re.sub(r'\s*\([+-]?\d+\)', '', desc)          # (-125), (+110)
    desc = re.sub(r'\s+[+-]\d{3,4}$', '', desc)           # trailing +113 / -138
    desc = re.sub(r'\bMoneyline\b', 'ML', desc, re.IGNORECASE)
    # " vs Arkansas" on spread lines — but only when the bet lives BEFORE the
    # "vs" ("Alabama -7.5 vs Arkansas"). In matchup-first wording ("Angers vs
    # Rennes BTTS") the market follows the matchup, and stripping it would
    # leave a bare team name with no bet at all.
    head = re.sub(r'\s+vs\s+\S.*$', '', desc)
    if re.search(r'\d|\bML\b', head):
        desc = head
    return desc.strip()


def _period_tag(pick: dict) -> str:
    """The period qualifier for a pick's label ("" for a full-game bet)."""
    period = pick.get("period") or "game"
    # Baseball "1H" is conventionally called "F5" (first 5 innings), and "1Q"
    # is the 1st inning (NRFI/YRFI) — a quarter tag on a baseball bet is wrong.
    if pick.get("sport", "") in ("MLB", "KBO"):
        if period == "1h":
            return " F5"
        if period == "1q":
            return " 1st Inn"
    return f" {period.upper()}" if period and period != "game" else ""


# Rendered team names are nickname-short ("Denver Broncos ML" → "Broncos ML") —
# operator-picked format, 2026-09-20. An exact-full-name allowlist on purpose:
# stripping a "city" heuristically mangles college ("Ohio State" → "State"),
# soccer clubs ("Manchester United" → "United") and country teams ("Costa
# Rica" → "Rica"), so anything not listed — KBO, soccer, college, national
# teams, and the _clean_desc fallback's free text — renders unchanged. Values
# repeat across leagues (Rangers, Panthers, Cardinals…) exactly the way
# cappers themselves write; within one message the game disambiguates.
_TEAM_SHORT_NAMES_RAW: dict[str, str] = {
    # NFL
    "Arizona Cardinals": "Cardinals", "Atlanta Falcons": "Falcons",
    "Baltimore Ravens": "Ravens", "Buffalo Bills": "Bills",
    "Carolina Panthers": "Panthers", "Chicago Bears": "Bears",
    "Cincinnati Bengals": "Bengals", "Cleveland Browns": "Browns",
    "Dallas Cowboys": "Cowboys", "Denver Broncos": "Broncos",
    "Detroit Lions": "Lions", "Green Bay Packers": "Packers",
    "Houston Texans": "Texans", "Indianapolis Colts": "Colts",
    "Jacksonville Jaguars": "Jaguars", "Kansas City Chiefs": "Chiefs",
    "Las Vegas Raiders": "Raiders", "Los Angeles Chargers": "Chargers",
    "Los Angeles Rams": "Rams", "Miami Dolphins": "Dolphins",
    "Minnesota Vikings": "Vikings", "New England Patriots": "Patriots",
    "New Orleans Saints": "Saints", "New York Giants": "Giants",
    "New York Jets": "Jets", "Philadelphia Eagles": "Eagles",
    "Pittsburgh Steelers": "Steelers", "San Francisco 49ers": "49ers",
    "Seattle Seahawks": "Seahawks", "Tampa Bay Buccaneers": "Buccaneers",
    "Tennessee Titans": "Titans", "Washington Commanders": "Commanders",
    # MLB
    "Arizona Diamondbacks": "Diamondbacks", "Atlanta Braves": "Braves",
    "Baltimore Orioles": "Orioles", "Boston Red Sox": "Red Sox",
    "Chicago Cubs": "Cubs", "Chicago White Sox": "White Sox",
    "Cincinnati Reds": "Reds", "Cleveland Guardians": "Guardians",
    "Colorado Rockies": "Rockies", "Detroit Tigers": "Tigers",
    "Houston Astros": "Astros", "Kansas City Royals": "Royals",
    "Los Angeles Angels": "Angels", "Los Angeles Dodgers": "Dodgers",
    "Miami Marlins": "Marlins", "Milwaukee Brewers": "Brewers",
    "Minnesota Twins": "Twins", "New York Mets": "Mets",
    "New York Yankees": "Yankees", "Oakland Athletics": "Athletics",
    "Philadelphia Phillies": "Phillies", "Pittsburgh Pirates": "Pirates",
    "San Diego Padres": "Padres", "San Francisco Giants": "Giants",
    "Seattle Mariners": "Mariners", "St. Louis Cardinals": "Cardinals",
    "St Louis Cardinals": "Cardinals", "Tampa Bay Rays": "Rays",
    "Texas Rangers": "Rangers", "Toronto Blue Jays": "Blue Jays",
    "Washington Nationals": "Nationals",
    # NBA
    "Atlanta Hawks": "Hawks", "Boston Celtics": "Celtics",
    "Brooklyn Nets": "Nets", "Charlotte Hornets": "Hornets",
    "Chicago Bulls": "Bulls", "Cleveland Cavaliers": "Cavaliers",
    "Dallas Mavericks": "Mavericks", "Denver Nuggets": "Nuggets",
    "Detroit Pistons": "Pistons", "Golden State Warriors": "Warriors",
    "Houston Rockets": "Rockets", "Indiana Pacers": "Pacers",
    "LA Clippers": "Clippers", "Los Angeles Clippers": "Clippers",
    "Los Angeles Lakers": "Lakers", "Memphis Grizzlies": "Grizzlies",
    "Miami Heat": "Heat", "Milwaukee Bucks": "Bucks",
    "Minnesota Timberwolves": "Timberwolves",
    "New Orleans Pelicans": "Pelicans", "New York Knicks": "Knicks",
    "Oklahoma City Thunder": "Thunder", "Orlando Magic": "Magic",
    "Philadelphia 76ers": "76ers", "Phoenix Suns": "Suns",
    "Portland Trail Blazers": "Trail Blazers", "Sacramento Kings": "Kings",
    "San Antonio Spurs": "Spurs", "Toronto Raptors": "Raptors",
    "Utah Jazz": "Jazz", "Washington Wizards": "Wizards",
    # NHL
    "Anaheim Ducks": "Ducks", "Boston Bruins": "Bruins",
    "Buffalo Sabres": "Sabres", "Calgary Flames": "Flames",
    "Carolina Hurricanes": "Hurricanes", "Chicago Blackhawks": "Blackhawks",
    "Colorado Avalanche": "Avalanche", "Columbus Blue Jackets": "Blue Jackets",
    "Dallas Stars": "Stars", "Detroit Red Wings": "Red Wings",
    "Edmonton Oilers": "Oilers", "Florida Panthers": "Panthers",
    "Los Angeles Kings": "Kings", "Minnesota Wild": "Wild",
    "Montreal Canadiens": "Canadiens", "Montréal Canadiens": "Canadiens",
    "Nashville Predators": "Predators", "New Jersey Devils": "Devils",
    "New York Islanders": "Islanders", "New York Rangers": "Rangers",
    "Ottawa Senators": "Senators", "Philadelphia Flyers": "Flyers",
    "Pittsburgh Penguins": "Penguins", "San Jose Sharks": "Sharks",
    "Seattle Kraken": "Kraken", "St. Louis Blues": "Blues",
    "St Louis Blues": "Blues", "Tampa Bay Lightning": "Lightning",
    "Toronto Maple Leafs": "Maple Leafs", "Utah Mammoth": "Mammoth",
    "Vancouver Canucks": "Canucks", "Vegas Golden Knights": "Golden Knights",
    "Washington Capitals": "Capitals", "Winnipeg Jets": "Jets",
    # WNBA
    "Atlanta Dream": "Dream", "Chicago Sky": "Sky",
    "Connecticut Sun": "Sun", "Dallas Wings": "Wings",
    "Golden State Valkyries": "Valkyries", "Indiana Fever": "Fever",
    "Las Vegas Aces": "Aces", "Los Angeles Sparks": "Sparks",
    "Minnesota Lynx": "Lynx", "New York Liberty": "Liberty",
    "Phoenix Mercury": "Mercury", "Portland Fire": "Fire",
    "Seattle Storm": "Storm", "Toronto Tempo": "Tempo",
    "Washington Mystics": "Mystics",
    # CFL
    "BC Lions": "Lions", "Calgary Stampeders": "Stampeders",
    "Edmonton Elks": "Elks", "Hamilton Tiger-Cats": "Tiger-Cats",
    "Montreal Alouettes": "Alouettes", "Ottawa Redblacks": "Redblacks",
    "Saskatchewan Roughriders": "Roughriders",
    "Toronto Argonauts": "Argonauts", "Winnipeg Blue Bombers": "Blue Bombers",
}
_TEAM_SHORT_NAMES = {k.casefold(): v for k, v in _TEAM_SHORT_NAMES_RAW.items()}


# College teams render as the SCHOOL ("Appalachian State Mountaineers" →
# "Appalachian State"), the way cappers write them — operator 2026-10-17.
# ESPN's exact displayName → location pairs, plus mascots that never end a
# school's own name for the parse's non-ESPN spellings; rebuilt by
# scripts/build_college_team_names.py. Missing file = names pass unchanged.
_COLLEGE_SPORTS = {"NCAAF", "NCAAB"}
try:
    with open(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                           "data", "college_team_names.json"), encoding="utf-8") as _f:
        _college = json.load(_f)
except (OSError, ValueError):
    _college = {}
_COLLEGE_SCHOOLS = {k.casefold(): v for k, v in (_college.get("names") or {}).items()}
# Longest first, so "Crimson Tide" goes before a bare "Tide" could.
_COLLEGE_MASCOTS = sorted((m.casefold() for m in _college.get("mascots") or []),
                          key=len, reverse=True)


def _short_team(name: str, sport: str | None = None) -> str:
    """Nickname for a mapped pro team, school for a college team; else unchanged."""
    key = " ".join(str(name).split()).casefold()
    short = _TEAM_SHORT_NAMES.get(key) or _COLLEGE_SCHOOLS.get(key)
    if short:
        return short
    if sport in _COLLEGE_SPORTS:
        for mascot in _COLLEGE_MASCOTS:
            if key.endswith(" " + mascot):
                return " ".join(str(name).split())[:len(key) - len(mascot) - 1]
    return name


def _format_pick(pick: dict, in_game: bool = False) -> str:
    """Build a standardized, odds-free pick description from structured parse fields.

    `in_game` = the line renders under a final-score header that already names the
    game, so game-level bets drop the matchup and totals spell out the side
    ("Under 41.5", not "Seahawks/Broncos U41.5"; operator 2026-10-16). Display only — fingerprints and Sheets always
    use the full label.

    Wraps `_format_pick_body` with a net for the period qualifier: every branch is
    supposed to interpolate it, and two of them didn't, so a first-half under went
    out as a full-game one. If a future branch forgets, appending the tag names the
    right bet in a slightly odd order — strictly better than naming the wrong one.
    """
    label = _format_pick_body(pick, in_game)
    tag = _period_tag(pick)
    if not tag or tag.strip() in label:
        return label
    # The description fallback carries the capper's own wording ("First 5 Innings"),
    # which states the period without matching the tag — don't double it up.
    if label == _clean_desc(pick.get("description", "")):
        return label
    return f"{label}{tag}"


def _format_pick_body(pick: dict, in_game: bool = False) -> str:
    bet_type  = pick.get("bet_type", "")
    # New list on purpose — never mutate the parse's own teams in place.
    teams     = [_short_team(t, pick.get("sport")) for t in (pick.get("teams") or [])]
    line      = pick.get("line")
    direction = pick.get("direction") or ""
    player    = pick.get("player") or ""
    prop_stat = pick.get("prop_stat") or ""

    # BTTS sometimes parses as a game total instead of a team-level prop
    # (line=null → the description fallback left a bare team name; line=0.5 →
    # "Italy/Belgium O0.5"). Recover the stat so every shape renders through
    # the team-prop branch. New parses are normalized in ai.py; this covers
    # entries cached before that.
    if is_btts_as_total(pick):
        bet_type, prop_stat = "prop", "BTTS"

    period_tag = _period_tag(pick)
    team = teams[0] if teams else ""

    if bet_type == "spread" and team and line is not None:
        sign = "+" if line > 0 else ""
        return f"{team}{period_tag} {sign}{line:g}"

    if bet_type == "double_chance" and team:
        return f"{team}{period_tag} DC"

    if bet_type == "draw_no_bet" and team:
        return f"{team}{period_tag} DNB"

    if bet_type == "moneyline" and team:
        desc_raw = pick.get("description", "")
        advance_m = re.search(r'\bto\s+(advance|qualify)\b', desc_raw, re.IGNORECASE)
        if advance_m:
            suffix = f" to {advance_m.group(1).title()}"
        elif is_regulation_ml(desc_raw):
            suffix = " 3-way ML"
        else:
            suffix = " ML"
        return f"{team}{period_tag}{suffix}"

    # Without a direction there is no O/U to print and the label would read
    # "Dallas Wings 79.5" — a number with no bet. Fall through to the description.
    if bet_type in ("total", "team_total") and line is not None and direction in ("over", "under"):
        d = "O" if direction == "over" else "U"
        if in_game:
            # Spelled out under the score header, where the bet stands alone
            # ("Under 41.5"; operator 2026-10-16). Compact lines keep "U41.5".
            d = "Over " if direction == "over" else "Under "
        if bet_type == "team_total" and team:
            head = team
        elif in_game:
            head = ""
        elif len(teams) >= 2:
            head = f"{teams[0]}/{teams[1]}"
        elif team:
            head = team
        else:
            head = ""
        # lstrip covers the headless game total, where period_tag is the whole
        # prefix (" 1H " -> "1H "), and collapses to "" when both are empty.
        prefix = f"{head}{period_tag} ".lstrip()
        stat = f" {prop_stat}" if prop_stat else ""
        return f"{prefix}{d}{line:g}{stat}"

    if bet_type == "prop" and player:
        if line is not None and direction:
            d = "O" if direction == "over" else "U"
            stat = f" {prop_stat}" if prop_stat else ""
            return f"{player}{period_tag} {d}{line:g}{stat}"
        # Line-less props (anytime/first TD scorer): the stat IS the bet — a
        # bare player name names no bet. No stat either → the capper's own
        # wording; the name alone only as a last resort.
        if prop_stat:
            return f"{player}{period_tag} {prop_stat}"
        desc = _clean_desc(pick.get("description", ""))
        return desc or f"{player}{period_tag}"

    # Team-level props (BTTS, clean sheet, etc.) — no player, but has teams + prop_stat
    if bet_type == "prop" and prop_stat and teams:
        matchup = "" if in_game else (" vs ".join(teams) if len(teams) >= 2 else teams[0])
        stat = prop_stat
        if prop_stat == "BTTS" and direction:
            stat += " Yes" if direction == "over" else " No"
        return f"{matchup}{period_tag} {stat}".lstrip()

    # Fallback to cleaned description string
    return _clean_desc(pick.get("description", ""))


def _fmt_odds(o: int | None) -> str:
    """American odds as a signed string; empty when the pick was never priced."""
    if o is None:
        return ""
    return f"+{o}" if o > 0 else str(o)


def _pick_link(channel_id: int, message_id: int) -> str:
    """Deep link to a pick message in a private channel."""
    return f"https://t.me/c/{str(abs(channel_id))[3:]}/{message_id}"


def pick_link_for_key(key: str) -> str:
    """t.me link for a `<channel>:<msg>` cache key; "" when it doesn't parse."""
    m = re.fullmatch(r"(-100\d+):(\d+)", key or "")
    return _pick_link(int(m.group(1)), int(m.group(2))) if m else ""


def _capper_label(capper_name: str) -> str:
    """Squeeze a capper handle down to label size.

    The label is the message's first line, which for analytics-source channels is
    often a paragraph of reasoning rather than a short handle. Cap it so the label
    stays handle-sized and never swallows the result line.
    """
    label = " ".join((capper_name or "").split())
    if len(label) > 40:
        label = label[:39].rstrip() + "…"
    return label


def _nb(s: str) -> str:
    """Non-breaking spaces, so a capper name wraps as one piece ("RIGGED GEMS"
    never splits across lines, and "· Boyer" never strands the middot)."""
    return str(s).replace(" ", "\u00a0")


# Merged result lines read sides → totals → props (operator 2026-10-16).
_SIDE_BET_TYPES = ("spread", "moneyline", "double_chance", "draw_no_bet")


def _market_rank(pick: dict) -> int:
    bet_type = pick.get("bet_type", "")
    if is_btts_as_total(pick):
        return 2
    if bet_type in _SIDE_BET_TYPES:
        return 0
    if bet_type in ("total", "team_total"):
        return 1
    return 2 if bet_type == "prop" else 3


# Header icon for a game where every merged bet won / lost (2+ bets; a push
# breaks the sweep). Icons only (three in a row), no tally text (operator 2026-10-16).
SWEEP_ICON = {"WIN": "🧹🧹🧹", "LOSS": "🥶🥶🥶"}


DB_PATH = os.path.join(os.path.dirname(__file__), "picks.db")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS grades (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    graded_at     TEXT    NOT NULL,          -- ISO-8601 UTC timestamp
    channel_id    INTEGER NOT NULL,          -- Telegram channel ID
    message_id    INTEGER NOT NULL,          -- Telegram message ID
    date          TEXT    NOT NULL,          -- Pick date  YYYY-MM-DD
    sport         TEXT,
    capper_name   TEXT,                      -- first line of message (capper handle)
    pick_desc     TEXT,
    bet_type      TEXT,
    verdict       TEXT    NOT NULL,          -- WIN / LOSS / PUSH / UNKNOWN / PENDING
    calc          TEXT,                      -- grader's arithmetic string
    prev_caption  TEXT,                      -- caption before edit (if any)
    new_caption   TEXT,                      -- caption after edit  (if any)
    dry_run       INTEGER NOT NULL DEFAULT 0 -- 1 = logged only, no Telegram edit
);

CREATE UNIQUE INDEX IF NOT EXISTS grades_msg
    ON grades (channel_id, message_id);
"""

_SCHEMA_API_COSTS = """
CREATE TABLE IF NOT EXISTS api_costs (
    id                   INTEGER PRIMARY KEY AUTOINCREMENT,
    logged_at            TEXT    NOT NULL,
    run_type             TEXT,
    claude_cost          REAL,
    odds_requests_used   INTEGER
);
"""

# One row per result line posted to a broadcast results channel — the memory
# behind the cross-post dedupe (see broadcast_results). Keyed on the line's
# identity, not its source message, because the whole point is catching the
# same bet arriving from a DIFFERENT message.
_SCHEMA_BROADCAST_LINES = """
CREATE TABLE IF NOT EXISTS broadcast_lines (
    target_channel INTEGER NOT NULL,
    fingerprint    TEXT    NOT NULL,
    sent_at        TEXT    NOT NULL,
    source         TEXT,
    PRIMARY KEY (target_channel, fingerprint)
);
"""

# An identical result line re-posted to the same target within this window is
# suppressed; past it, it may post again (a genuinely re-bet market weeks
# later is a new result). Rows are pruned after _BROADCAST_LINES_KEEP_DAYS.
BROADCAST_DEDUPE_DAYS = 3
_BROADCAST_LINES_KEEP_DAYS = 30


def _result_line_fingerprint(capper_label: str, verdict: str, bet_text: str) -> str:
    """Identity of one result line in a results feed: who, which bet, which outcome.

    The price is deliberately NOT part of the identity: the two copies of a
    restated bet fetch odds at different times, and snapshot drift (-141 vs
    -139) doesn't make them different bets. Different cappers on the same bet
    stay distinct — each earns their own result line.
    """
    parts = [capper_label or "", verdict or "", bet_text or ""]
    return hashlib.sha256("\x1f".join(parts).encode()).hexdigest()[:32]


_MIGRATIONS = [
    "ALTER TABLE grades ADD COLUMN capper_name TEXT",
    "ALTER TABLE grades ADD COLUMN odds INTEGER",
    "ALTER TABLE grades ADD COLUMN odds_bookmaker TEXT",
    "ALTER TABLE grades ADD COLUMN odds_match_type TEXT",
    "ALTER TABLE grades ADD COLUMN post_fingerprint TEXT",
]


def _post_fingerprint(row: dict, channel_name: str, capper_name: str, edit_failed: bool) -> str:
    """Hash of everything `_post_telegram` actually renders.

    The audit channel is a log of grading *actions*, and both loops re-grade a
    message on every pass, so a message that never reaches a terminal state gets
    re-recorded forever. Re-posting a record identical to the last one for the
    same message says nothing new — it is pure noise — so the fingerprint is
    compared against the stored one and the post is skipped when it matches.

    Cover exactly the fields that shape the message: a changed verdict, calc,
    pick list or edit-failure IS news and must still post. `graded_at`, odds and
    the captions are deliberately excluded — they move without changing a word
    of what the reader sees.
    """
    parts = [
        str(row.get("verdict") or ""),
        str(row.get("pick_desc") or ""),
        str(row.get("calc") or ""),
        str(row.get("sport") or ""),
        str(row.get("date") or ""),
        str(row.get("dry_run") or 0),
        "1" if edit_failed else "0",
        channel_name or "",
        capper_name or "",
    ]
    return hashlib.sha256("\x1f".join(parts).encode()).hexdigest()[:32]



ANOMALIES_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                              "logs", "grader_anomalies.jsonl")
ANOMALY_KEEP_DAYS = 14


def load_anomalies() -> list[dict]:
    try:
        with open(ANOMALIES_FILE, encoding="utf-8") as f:
            rows = []
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    row = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(row, dict):
                    rows.append(row)
            return rows
    except OSError:
        return []


def save_anomalies(rows: list[dict]) -> None:
    cutoff = time.time() - ANOMALY_KEEP_DAYS * 86400
    rows = [r for r in rows
            if r.get("status") == "pending" or float(r.get("ts") or 0) >= cutoff]
    os.makedirs(os.path.dirname(ANOMALIES_FILE), exist_ok=True)
    tmp = ANOMALIES_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
    os.replace(tmp, ANOMALIES_FILE)


def append_anomaly(text: str, key: str = "", capper: str = "") -> None:
    """Queue one pending anomaly (idempotent on identical pending text).

    `key` = the pick's cache key (`<channel>:<msg>`): stored with its t.me
    link so the agent finds the pick and hc-repair's card links it."""
    rows = load_anomalies()
    if any(r.get("status") == "pending" and r.get("text") == text for r in rows):
        return
    row = {
        "id": hashlib.sha256(f"{time.time()}{text}".encode()).hexdigest()[:10],
        "ts": time.time(),
        "text": text,
        "status": "pending",
    }
    if key:
        row["key"] = key
        if link := pick_link_for_key(key):
            row["link"] = link
    if capper:
        row["capper"] = capper
    rows.append(row)
    save_anomalies(rows)


def anomaly_check_ping() -> None:
    """Agent-first signal for queued anomalies: /fail with the pending list
    while any exist (hc-repair's agent fixes + resolves them), success when
    clear. GRADER_ANOMALIES_HEALTHCHECK_URL unset = no-op."""
    url = os.getenv("GRADER_ANOMALIES_HEALTHCHECK_URL", "")
    if not url:
        return
    pending = [r for r in load_anomalies() if r.get("status") == "pending"]
    suffix, body = "", "no pending anomalies"
    if pending:
        suffix = "/fail"
        lines = [f"{len(pending)} pending grader anomalies "
                 "(resolve: scripts/grader_anomalies.py --resolve <id>|all):"]
        for r in pending[:12]:
            stamp = datetime.fromtimestamp(float(r.get("ts") or 0)).strftime("%m-%d %H:%M")
            lines.append(f"[{r.get('id')}] {stamp} {str(r.get('text', ''))[:300]}")
        body = "\n".join(lines)
    try:
        req = urllib.request.Request(url + suffix, data=body.encode())
        with urllib.request.urlopen(req, timeout=10):
            pass
    except Exception as exc:  # noqa: BLE001
        print(f"[audit] anomaly check ping failed: {exc}")


class AuditLog:
    """
    Thin wrapper around the SQLite audit DB + Telegram audit channel.

    All public methods are async-safe (they offload SQLite to a thread
    via asyncio.to_thread so they won't block the event loop).
    """

    def __init__(
        self,
        db_path: str = DB_PATH,
        bot_token: str | None = None,
        broadcast_results_mappings: dict[int, int] | None = None,
    ):
        self.db_path = db_path
        self.bot_token = bot_token or os.getenv("BOT_TOKEN", "")
        self.broadcast_results_mappings: dict[int, int] = broadcast_results_mappings or {}
        self._init_db()

    # ── DB helpers ─────────────────────────────────────────────────────────────

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def _init_db(self) -> None:
        with self._connect() as conn:
            conn.executescript(_SCHEMA)
            conn.executescript(_SCHEMA_API_COSTS)
            conn.executescript(_SCHEMA_BROADCAST_LINES)
            for mig in _MIGRATIONS:
                try:
                    conn.execute(mig)
                    conn.commit()
                except Exception:
                    pass  # column already exists

    def _insert(self, row: dict) -> bool:
        """Insert or replace a grade row (idempotent on channel_id+message_id).

        Returns True when this row's audit post would say something the last
        post for this message didn't — see `_post_fingerprint`. The read and the
        replace share one connection so the comparison is against the row we are
        about to overwrite, not one a concurrent writer slipped in.

        Opens its own connection because this runs in a background thread via
        asyncio.to_thread."""
        cols = ", ".join(row.keys())
        placeholders = ", ".join("?" for _ in row)
        sql = f"INSERT OR REPLACE INTO grades ({cols}) VALUES ({placeholders})"
        with self._connect() as conn:
            prior = conn.execute(
                "SELECT post_fingerprint FROM grades WHERE channel_id = ? AND message_id = ?",
                (row["channel_id"], row["message_id"]),
            ).fetchone()
            # NULL for rows written before this column existed — post once, then settle.
            prior_fp = prior["post_fingerprint"] if prior else None
            changed = prior_fp != row.get("post_fingerprint")
            conn.execute(sql, list(row.values()))
            conn.commit()
        return changed

    def _claim_broadcast_lines(
        self, target: int, fingerprints: list[str], source: str
    ) -> set[str]:
        """Return the subset of `fingerprints` not yet posted to `target` within
        BROADCAST_DEDUPE_DAYS, claiming them now.

        Claimed BEFORE the send, same direction the daemon's flush takes with
        `broadcasted`: a crash between claim and send drops a result rather
        than duplicating one (a missed post is recoverable, a duplicate isn't).
        A fingerprint repeated within one call is returned once — first wins.
        Runs in a thread (own connection), like _insert.
        """
        now = datetime.now(timezone.utc)
        cutoff = (now - timedelta(days=BROADCAST_DEDUPE_DAYS)).isoformat()
        keep: set[str] = set()
        with self._connect() as conn:
            conn.execute(
                "DELETE FROM broadcast_lines WHERE sent_at < ?",
                ((now - timedelta(days=_BROADCAST_LINES_KEEP_DAYS)).isoformat(),),
            )
            for fp in fingerprints:
                if fp in keep:
                    continue
                row = conn.execute(
                    "SELECT sent_at FROM broadcast_lines"
                    " WHERE target_channel = ? AND fingerprint = ?",
                    (target, fp),
                ).fetchone()
                if row and row["sent_at"] >= cutoff:
                    continue  # already in the feed — noise
                conn.execute(
                    "INSERT INTO broadcast_lines"
                    " (target_channel, fingerprint, sent_at, source)"
                    " VALUES (?, ?, ?, ?)"
                    " ON CONFLICT(target_channel, fingerprint)"
                    " DO UPDATE SET sent_at = excluded.sent_at,"
                    "               source = excluded.source",
                    (target, fp, now.isoformat(), source),
                )
                keep.add(fp)
            conn.commit()
        return keep

    # ── Core record method ─────────────────────────────────────────────────────

    async def record(
        self,
        *,
        channel_id: int,
        message_id: int,
        date: str,
        sport: str,
        pick_desc: str,
        bet_type: str,
        verdict: str,
        calc: str,
        prev_caption: str = "",
        new_caption: str = "",
        dry_run: bool = False,
        channel_name: str = "",
        capper_name: str = "",
        odds: int | None = None,
        odds_bookmaker: str | None = None,
        odds_match_type: str | None = None,
        edit_failed: bool = False,
    ) -> None:
        """
        Write to DB and post an audit Telegram message.
        Safe to call from an async context — SQLite work runs in a thread.
        """
        row = {
            "graded_at":       datetime.now(timezone.utc).isoformat(),
            "channel_id":      channel_id,
            "message_id":      message_id,
            "date":            date,
            "sport":           sport,
            "capper_name":     capper_name,
            "pick_desc":       pick_desc,
            "bet_type":        bet_type,
            "verdict":         verdict,
            "calc":            calc or "",
            "prev_caption":    prev_caption,
            "new_caption":     new_caption,
            "dry_run":         1 if dry_run else 0,
            "odds":            odds,
            "odds_bookmaker":  odds_bookmaker,
            "odds_match_type": odds_match_type,
        }
        row["post_fingerprint"] = _post_fingerprint(row, channel_name, capper_name, edit_failed)
        await asyncio.to_thread(self._insert, row)
        # The per-pick Telegram card feed was retired 2026-10-11 (operator:
        # never read) — the grades table, dashboard and vps_grades.py remain
        # the query surfaces; anomalies go agent-first via warn(kind="anomaly").

    # ── Operator-facing anomalies (agent-first, 2026-10-11) ──────────────────

    async def warn(self, text: str, kind: str = "info", *, key: str = "",
                   capper: str = "") -> None:
        """Surface a pipeline warning. Never raises.

        kind="info": journal only — the Telegram audit channel is gone
        (operator 2026-10-11: never read; auto-repairs, odds lines and the
        per-pick feed live in the run log).
        kind="anomaly": ALSO queued to logs/grader_anomalies.jsonl — the
        agent-first path: the end-of-run check ping fails while anything is
        pending, hc-repair's agent fixes the pick and resolves the entry
        (scripts/grader_anomalies.py), and its card is the operator's DM.
        `key` (cache key) + `capper` ride along so that card links the pick.
        """
        plain = re.sub(r"<[^>]+>", "", text)
        print(f"[audit] {plain}")
        if kind != "anomaly":
            return
        try:
            await asyncio.to_thread(append_anomaly, plain, key, re.sub(r"<[^>]+>", "", capper))
        except Exception as exc:  # noqa: BLE001
            print(f"[audit] anomaly queue failed: {exc}")

    # ── Broadcast results channel ──────────────────────────────────────────────

    async def broadcast_results(
        self,
        *,
        channel_id: int,
        message_id: int,
        pick_results: list[tuple[dict, str, int | None]],  # (pick_dict, verdict, odds) per pick
        capper_name: str = "",
        client=None,
        reply_to_id: int | None = None,
    ) -> None:
        """Post a compact result message to the broadcast results channel for this source channel."""
        target = self.broadcast_results_mappings.get(channel_id)
        if not target or not self.bot_token:
            return

        # Keep resolved picks (WIN/LOSS/PUSH) and broadcast if any are present
        resolved = [(p, v, o) for p, v, o in pick_results if v in ("WIN", "LOSS", "PUSH")]
        if not resolved:
            return

        import html as _html

        def e(s: str) -> str:
            return _html.escape(str(s))

        link = _pick_link(channel_id, message_id)
        capper_label = _capper_label(capper_name)

        # Bold header form for the multi-pick layout (one name over several
        # lines); singles instead put the bet first and the capper after a
        # middot (operator-picked over em-dash, 2026-09-21) — the exact line
        # grammar broadcast_group uses — so every
        # result line in the feed reads one way (operator-picked, 2026-09-20).
        capper_linked = f'<b><a href="{link}">{e(capper_label)}</a></b>' if capper_label else f'<a href="{link}">view</a>'
        capper_tail = f'<a href="{link}">{_nb(e(capper_label)) if capper_label else "view"}</a>'

        # Detect a parlay from ALL passed legs, not just the resolved ones: a
        # parlay that settled on one lost leg still passes its other (voided /
        # pending) legs so the whole ticket can be shown and priced.
        is_parlay = any(p.get("is_parlay_leg") for p, _, _o in pick_results)

        # The ticket's verdict comes from ITS legs alone, by the one rule for
        # the whole codebase (tracker_grading._overall_verdict): any LOSS ends
        # it, pushed legs drop out (WIN+PUSH is a WIN, only all-push is a PUSH),
        # and a pending leg holds it open. A ticket with no settled result must
        # not render at all — a standalone sibling resolving first used to drag
        # the untouched ticket in as a bogus "❓ … Parlay:" line (2026-09-20);
        # the straight legs broadcast below and the ticket waits its turn.
        parlay_all = [(p, v, o) for p, v, o in pick_results if p.get("is_parlay_leg")]
        parlay_verdict = _overall_verdict(parlay_all) if parlay_all else "UNKNOWN"
        if is_parlay and parlay_verdict not in ("WIN", "LOSS", "PUSH"):
            is_parlay = False
            resolved = [(p, v, o) for p, v, o in resolved if not p.get("is_parlay_leg")]
            if not resolved:
                return

        # ── Cross-post dedupe ────────────────────────────────────────────
        # The per-message guards (broadcasted flags, _post_fingerprint) can't
        # see a capper RESTATING a bet in a later message — a recap listing
        # the day's plays grades and broadcasts every line again (2026-09-20:
        # Colts/Chiefs U47.5 posted twice, once from its own message, once
        # from the evening recap). A line whose (capper, verdict, bet) this
        # target already carries within BROADCAST_DEDUPE_DAYS is dropped, and
        # the whole post is skipped when nothing survives. The ticket is one
        # unit: a restated parlay dedupes whole, its legs aren't lines here.
        source = f"{channel_id}:{message_id}"
        ticket_fp = None
        if is_parlay:
            ticket_fp = _result_line_fingerprint(
                capper_label, parlay_verdict,
                "parlay:" + " / ".join(_format_pick(p) for p, _, _ in parlay_all),
            )
        leg_fps = {
            j: _result_line_fingerprint(capper_label, v, _format_pick(p))
            for j, (p, v, o) in enumerate(resolved)
            if not p.get("is_parlay_leg")
        }
        claim = ([ticket_fp] if ticket_fp else []) + list(leg_fps.values())
        kept = await asyncio.to_thread(
            self._claim_broadcast_lines, target, claim, source
        )
        if len(kept) < len(set(claim)):
            if ticket_fp and ticket_fp not in kept:
                is_parlay = False
            resolved = [
                rv for j, rv in enumerate(resolved)
                if (is_parlay and rv[0].get("is_parlay_leg"))
                or leg_fps.get(j) in kept
            ]
            skipped = not resolved and not is_parlay
            print(f"[broadcast_results] deduped already-posted line(s) from {source}"
                  + (" — nothing new, skipping post" if skipped else ""))
            if skipped:
                return

        picks = [(_format_pick(p), v, _fmt_odds(o)) for p, v, o in resolved]

        def _pick_line(desc: str, verdict: str, odds_str: str) -> str:
            odds_part = f" {e(odds_str)}" if odds_str else ""
            return f"{VERDICT_EMOJI.get(verdict, '')} {e(desc)}{odds_part}"

        # Check is_parlay BEFORE the single-pick case: a parlay that settled on
        # a single lost leg (siblings still pending/dropped) has len(picks)==1 but
        # must still render as a Parlay, not a lone straight pick.
        if is_parlay:
            # One ticket: list every leg and price the whole parlay from every
            # leg's odds — not just the individually-resolved legs.
            overall_emoji = VERDICT_EMOJI[parlay_verdict]
            # A pushed leg is voided from the ticket, so it can't multiply into
            # the payout — price only the live legs (all-push → no price at all).
            # A same-game ticket (correlated legs) has no honest product.
            live_legs = [(p, o) for p, v, o in parlay_all if v != "PUSH"]
            combined = ticket_combined_odds(
                [p for p, _ in live_legs], [o for _, o in live_legs]
            )
            combined_part = f" {e(_fmt_odds(combined))}" if combined is not None else ""
            # Inline the legs on a single line (Ko ML / Duncan ML) instead of one
            # bullet per leg, so the whole ticket reads as one compact result.
            # A pushed leg carries its ♻️ inline — that's what explains a ✅
            # ticket paying less than the posted parlay price.
            legs = " / ".join(
                e(_format_pick(p)) + (" ♻️" if v == "PUSH" else "")
                for p, v, _ in parlay_all
            )
            ticket_line = f"Parlay: {legs}{combined_part}"
            straight = [(_format_pick(p), v, _fmt_odds(o)) for p, v, o in resolved
                        if not p.get("is_parlay_leg")]
            if straight:
                # Standalone picks sharing the message are separate bets: they
                # keep their own lines and the settled ticket joins as one more,
                # all under the one capper header (the multi-pick layout).
                lines = [_pick_line(d, v, o) for d, v, o in straight]
                lines.append(f"{overall_emoji} {ticket_line}")
                text = capper_linked + "\n" + "\n".join(lines)
            else:
                text = f"{overall_emoji} {ticket_line} ·\u00a0{capper_tail}"
        elif len(picks) == 1:
            desc, verdict, odds_str = picks[0]
            emoji = VERDICT_EMOJI.get(verdict, "")
            odds_part = f" {e(odds_str)}" if odds_str else ""
            text = f"{emoji} {e(desc)}{odds_part} ·\u00a0{capper_tail}"
        else:
            # Non-parlay multi-pick: one emoji per pick
            lines = [_pick_line(d, v, o) for d, v, o in picks]
            text = capper_linked + "\n" + "\n".join(lines)

        # Try to find the auto-forwarded message in the discussion group so we can reply to it.
        # Use pre-cached reply_to_id if provided (from grade daemon); fall back to Telethon lookup.
        if reply_to_id is None and client is not None:
            try:
                from telethon.tl.functions.messages import GetDiscussionMessageRequest
                disc = await client(GetDiscussionMessageRequest(peer=channel_id, msg_id=message_id))
                reply_to_id = disc.messages[0].id
            except Exception:
                pass  # no linked group or message not found — send without reply

        await self._post_broadcast(target=target, text=text, reply_to_id=reply_to_id, link=link)

    async def broadcast_group(
        self,
        *,
        target_channel: int,
        header: str,
        items: list[dict],
        reply_to_id: int | None = None,
    ) -> None:
        """Post ONE result message covering several cappers' picks on the same game.

        Also the renderer for a LONE pick whose final score resolved: the daemon
        routes those here so every scored result shares the score-header format
        ("⚾️ Marlins 1–6 Cubs" + pick line with the capper after the middot), and
        `broadcast_results` stays the compact fallback for the rest (parlays,
        mid-game settles, sports ESPN doesn't carry).

        Each item is {channel_id, message_id, capper, pick, verdict, odds} — one per
        resolved LEG, so a multi-pick message whose legs all landed on this game
        contributes several items sharing one capper and message link. Bets that
        are identical — same verdict, same description, same price — collapse onto a
        single line naming every capper, so three cappers on Cubs ML read as one line
        instead of three near-identical messages. Each name still deep-links to that
        capper's own pick message, so nothing is lost but the repetition.

        Price is part of the merge key on purpose: two cappers who got different
        numbers on the same side did not make the same bet, and printing one of the
        two prices would misreport the other's result.
        """
        if not target_channel or not self.bot_token or not items:
            return

        # Cross-post dedupe — same memory broadcast_results consults: a line
        # this feed already carries (same capper, same bet, same verdict,
        # within BROADCAST_DEDUPE_DAYS) adds nothing, whichever format posted
        # it first. Filtering per item also collapses one capper's restated
        # bet landing twice inside a single merge group (two messages, one
        # game), which would otherwise print the same name twice on the line.
        fps = [
            _result_line_fingerprint(
                _capper_label(it.get("capper", "")),
                it["verdict"],
                _format_pick(it["pick"]),
            )
            for it in items
        ]
        kept = await asyncio.to_thread(
            self._claim_broadcast_lines, target_channel, fps,
            f"{items[0]['channel_id']}:{items[0]['message_id']}",
        )
        if len(kept) < len(fps):
            pruned, seen = [], set()
            for it, fp in zip(items, fps):
                if fp in kept and fp not in seen:
                    seen.add(fp)
                    pruned.append(it)
            print("[broadcast_group] deduped already-posted line(s)"
                  + ("" if pruned else " — nothing new, skipping post"))
            if not pruned:
                return
            items = pruned

        import html as _html

        def e(s: str) -> str:
            return _html.escape(str(s))

        specs: list[tuple[str, str, str]] = []       # (verdict, desc, odds) in first-seen order
        who: list[list[str]] = []                    # linked capper names per spec
        ranks: list[int] = []                        # market rank per spec
        seen: dict[tuple[str, str, str], int] = {}
        for it in items:
            # Under the score header the matchup is already named — game-level
            # bets render bare ("U41.5"). Headerless merges keep it.
            desc = _format_pick(it["pick"], in_game=bool(header))
            key = (it["verdict"], desc, _fmt_odds(it.get("odds")))
            label = _capper_label(it.get("capper", ""))
            link = _pick_link(it["channel_id"], it["message_id"])
            linked = f'<a href="{link}">{_nb(e(label)) if label else "view"}</a>'
            idx = seen.get(key)
            if idx is None:
                seen[key] = len(specs)
                specs.append(key)
                who.append([linked])
                ranks.append(_market_rank(it["pick"]))
            elif linked not in who[idx]:
                who[idx].append(linked)

        # Sides, then totals, then props; message order within each (stable sort).
        order = sorted(range(len(specs)), key=lambda i: ranks[i])
        specs = [specs[i] for i in order]
        who = [who[i] for i in order]

        # The header carries bold + underline and a blank line under it; the pick
        # descriptions stay plain. Bolding those too (the first cut did) left the
        # whole message one weight of white text with nothing to read as the title —
        # and mobile wrapping of long descriptions blurs the rows together further.
        lines = []
        for (verdict, desc, odds_str), names in zip(specs, who):
            odds_part = f" {e(odds_str)}" if odds_str else ""
            lines.append(
                f"{VERDICT_EMOJI.get(verdict, '')} {e(desc)}{odds_part} ·\u00a0{', '.join(names)}"
            )

        verdicts = {v for v, _, _ in specs}
        sweep = SWEEP_ICON.get(next(iter(verdicts))) if len(specs) >= 2 and len(verdicts) == 1 else None
        title = f"<b><u>{e(header)}</u></b>" + (f" {sweep}" if sweep else "")
        text = "\n".join(([title, ""] if header else []) + lines)
        link = _pick_link(items[0]["channel_id"], items[0]["message_id"])
        await self._post_broadcast(
            target=target_channel, text=text, reply_to_id=reply_to_id, link=link
        )

    async def _post_broadcast(
        self, *, target: int, text: str, reply_to_id: int | None, link: str
    ) -> None:
        """POST one message to a broadcast results channel. Never raises."""
        import html as _html

        def e(s: str) -> str:
            return _html.escape(str(s))

        payload: dict = {
            "chat_id": target,
            "text": text,
            "parse_mode": "HTML",
            "disable_web_page_preview": True,
            # If reply_to_id points to a message the bot can't resolve (e.g. the
            # auto-forwarded discussion copy ended up in a state where Bot API
            # returns "message to be replied not found"), Telegram will still
            # post the broadcast — just without the reply thread — instead of
            # dropping the whole request. Without this flag the sendMessage
            # returns 400 and the result silently never posts.
            "allow_sending_without_reply": True,
        }
        if reply_to_id is not None:
            payload["reply_to_message_id"] = reply_to_id

        try:
            async with httpx.AsyncClient(timeout=10) as http:
                r = await http.post(
                    f"https://api.telegram.org/bot{self.bot_token}/sendMessage",
                    json=payload,
                )
            if not r.is_success:
                snippet = r.text[:300]
                print(f"[broadcast_results] Telegram post failed: {r.status_code} {snippet}")
                await self.warn(
                    f"⚠ <b>broadcast_results failed</b> [{r.status_code}]\n"
                    f"{e(snippet)}\n"
                    f'<a href="{link}">view pick</a>'
                )
        except Exception as exc:
            print(f"[broadcast_results] Telegram post failed: {exc}")
            await self.warn(
                f"⚠ <b>broadcast_results exception</b>\n"
                f"{e(str(exc)[:300])}\n"
                f'<a href="{link}">view pick</a>'
            )


def log_api_costs(run_type: str, claude_cost: float, odds_requests: int, db_path: str = DB_PATH) -> None:
    """Write a run-level API cost row to picks.db. Safe to call without an AuditLog instance."""
    import sqlite3 as _sqlite3
    conn = _sqlite3.connect(db_path)
    try:
        conn.execute(
            "INSERT INTO api_costs (logged_at, run_type, claude_cost, odds_requests_used) VALUES (?, ?, ?, ?)",
            (datetime.now(timezone.utc).isoformat(), run_type, claude_cost, odds_requests),
        )
        conn.commit()
    finally:
        conn.close()
