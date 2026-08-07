"""
Parse a capper's posts CSV to extract official pick placements.

Three-phase pipeline:
  1. Text parse: send tweet text to Claude, extract single official picks
  2. Image parse: for posts that look like pick announcements but had no
     extractable pick from text alone (pick is in attached image), download
     the image and send it to Claude along with the text
  3. Dedup: remove duplicate picks (same bet from multiple tweets)

Step 2 of the capper backfill pipeline:
    fetch_x_posts -> parse_posts_csv -> grade_csv -> format_graded_csv

Usage:
    python scripts/parse_posts_csv.py --account boyerBets_ [--limit N] [--skip-images]
    python scripts/parse_posts_csv.py --account boyerBets_ --days 1   # test on one day
"""

import argparse
import asyncio
import collections
import csv
import functools
import json
import re
import sys
import os
import base64
from datetime import date as _d, timedelta

import httpx

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from ai import _claude_create_with_retry, usage_cost, fmt_cost

OUT_DIR = os.path.join(os.path.dirname(__file__), "output")

CONCURRENCY = 10

OUT_FIELDS = [
    "id", "date", "text", "photos", "videos", "url",
    "sport", "category", "multi", "is_parlay",
    "description", "bet_type", "teams", "player",
    "prop_stat", "line", "direction", "period",
]

# Categories kept by default when grading. Only the free Play of the Day, because
# it is the one category posted publicly, pre-game, every time — so the sample is
# not selected on outcome.
#
# "result" is the dangerous one: those posts reveal a pick only after it won, so
# including them manufactures a near-perfect record out of nothing. "max" and
# "secondary" are real pre-game posts but are announced inconsistently (max plays
# are VIP and surface publicly mostly as wins), so they bias the sample too.
# Override with grade_csv.py --categories.
GRADEABLE_CATEGORIES = {"pod"}

# ── Signals that a post MIGHT contain a pick.
# Only used to pick image-parse candidates in phase 2, never to reject a post in
# phase 1 — phase 1 sends every non-retweet to Claude, so a capper whose wording
# isn't listed here still gets his text picks parsed. Missing a signal only costs
# us a bet slip that lives in an image with no telltale text.
# Short tokens are matched on word boundaries so "pod" does not fire on "podcast".
_PICK_SIGNALS = [
    "official", "play of", "lock of", "bet of", "pick of",
    "i'm on", "im on", "i'll be on", "i will be taking", "i'm taking",
    "i'm riding", "riding with", "hammering", "hammer",
    "i have $", "dropping", "tailing", "fading",
    "unit", "10u", "5u", "1u", "slip", "bet slip", "parlay",
    # An emoji-legend post ("💙 = POD", "🩵 = POD 6-1 Run") carries the pick ONLY
    # in the attached image and names no team, no side and no number in the text.
    # Without these the post never becomes a phase-2 candidate and the pick is
    # lost silently — 19 of @boyerBets_'s PODs went missing exactly this way.
    "pod", "locked", "inbound", "card", "lock",
]

# ── Per-account vocabulary.
# Cappers brand their bets ("mortal mega", "nuke"). That branding is a strong
# pick signal for that account and pure noise for every other one, so it lives
# here rather than in the shared prompt. An account with no entry just runs the
# generic prompt, which is the correct starting point for a new handle.
_ACCOUNT_VOCAB: dict[str, dict] = {
    "BookitWithTrent": {
        "signals": [
            "mortal mega", "mortal", "mega max", "nuke", "nuking",
            "square of", "last chance u", "fugazi", "10 unit",
        ],
        "notes": """\
This account's signature vocabulary:
- "mortal mega" / "mortal mega max" / "mega" — signature bet branding
- "nuke" / "nuking" / "I'm nuking [team]" — placing a big bet
- "play of year" / "square of year" / "play of the day"
- "FUGAZI 5" / "[N]-man nuke" — named parlay formats (multi-leg, so return null)
- "Last Chance U slip" — named bet format
Account-specific non-picks:
- "CASH THE MORTAL MEGA", "howsya", "BANGGGG" — celebrations
- "chalked", "GGs", "🥀" — loss reactions
- "mortal megas?👀", "the next nuke is loading...", "I wanna nuke it so bad" — teasers
- "I have the mortal mega" with no team/line named — teaser, not a pick yet""",
    },
    "boyerBets_": {
        "signals": [
            "play of the day", "pod", "potd", "max play", "free card",
            "full card", "second play", "adding:", "play #",
        ],
        "notes": """\
This account's format is highly templated. Use it:
- "📝🆓 MLB Play of the Day" / "🚨 MLB PLAY OF THE DAY 🚨" / "#NBA Play of the Day" /
  "🆓🥊UFC Play of the Day" — the free POD. category="pod".
- "MLB Play #2" / "Play #3" / "Second Play⭐️" / "⚾️Adding: ..." — category="secondary".
- "MAX Play" naming the pick pre-event — category="max".
- "Full Card" / "Free Card" listing several plays — category="card", multi=true.
- "CASH THE PLAY OF THE DAY", "CASH THAT ONE", "CASH THE MAX PLAY", "ANOTHER FIRST
  INNING CASH", "Results Speak for Themselves", "Your Welcome Fathers'" — these are
  RESULT posts. They name the pick with a 💰 or ✅ next to it AFTER it won.
  category="result". They are never "pod", no matter how they are worded.
- A "(POD)" tag inside a card marks which leg was the free Play of the Day.
- Record lines ("📈 119 - 44 Overall POD Record", "9-0 POD streak", "(12-3 Run)🔮")
  are context only — never a pick.
- "50 Likes❤️ for Play #2" is a promise, not a pick. If no second pick is named in
  that same tweet, the tweet is just the POD.
He bets MLB almost exclusively, heavy on F5 (first 5 innings) and team totals.""",
    },
}


def _vocab(account: str) -> dict:
    return _ACCOUNT_VOCAB.get(account, {})


# ─── Prompts ─────────────────────────────────────────────────────────────────

_PICK_PROMPT = """\
You are analyzing a tweet from a sports bettor. Extract any specific wager it names, and classify what ROLE the tweet plays.

Extraction and policy are deliberately separate: your job is to pull out every named wager and label it honestly. Deciding which categories to keep happens later, so do NOT suppress a wager because it looks like a result post or a paywalled play — label it and move on.

{date_context}
STEP 1: Does the tweet name a SPECIFIC wager (a team/side/player with a bet type, and usually a line or odds)?

If no concrete wager is named, return null. A tweet that only teases ("who wants today's play?", "a Max Play has been dropped in VIP", "I have a banger today") names no wager — return null.

STEP 2: Classify the ROLE of the tweet. This is the most important field. Choose exactly one:

- "pod" — the free Play of the Day: a pre-game announcement of the day's headline pick, posted publicly. Usually branded "Play of the Day", "POD", "🆓", or "PLAY OF THE DAY". This is the primary free play.
- "secondary" — an additional play beyond the POD: "Play #2", "Play #3", "Second Play", "Adding: ...". Still a pre-game announcement, just not the headline play.
- "max" — a "Max Play" (his larger-stake play), announced PRE-GAME with the pick named.
- "card" — a post listing SEVERAL separate plays at once ("Full Card", "Free Card"). Set "multi": true.
- "result" — the tweet is reporting the OUTCOME of a bet, not announcing it. Signals: "CASH THE...", "CASH THAT ONE", "Results Speak for Themselves", ✅/❌/💰 marks next to picks, "Record improves to", past tense. A result post often names the pick — extract it anyway and label it "result".
- "other" — names a wager but is commentary, a lean without placement, a correction, reminiscing, or someone else's pick.

CRITICAL: a tweet naming a pick alongside "CASH", "✅", "💰", or a record that just improved is a "result", NEVER a "pod". The pick is being revealed after the fact. Getting this wrong corrupts the whole analysis, because results only ever reveal winners.

Signals that a tweet is a genuine pre-game announcement:
- "OFFICIAL PLAY:" / "BEST PLAY:" / "LOCK OF THE DAY" — designates the pick in multi-prediction posts
- "I'm on [pick]" / "I'll be on" / "I will be taking" / "I'm riding with" / "hammering [team]"
- "I have $X on [team]" / "dropping $X on"
- "10u" / "5 units" / "$X,000" — unit/dollar sizing attached to a named side
- A bare, confidently stated side with a line and/or odds ("Rangers -1.5 +115"), posted as its own tweet — cappers often announce a play with no verb at all
- Explicit first-person declaration of placing a specific bet with a named team/side/line
{vocab_notes}
Return null (no wager named) for:
- Pure celebration or reaction with no pick named: "BANGGGG", "chalked", "GGs", "🥀"
- In-game commentary: "if this goes over", "they ain't scoring"
- Hopes/wishes without a bet: "Life on line, Mbappe is NOT scoring"
- Teasers naming no pick: "who wants today's POD?", "the next one is loading...", "100 likes and I'll drop it"
- Off-topic: giveaways, streams, podcast links, promo codes, affiliate plugs, sports news
- Score updates, goal celebrations, general commentary
- A record or streak with no pick attached: "121-44 Overall Record", "9-0 POD streak"

A tweet that mentions "3 STRAIGHT WINNERS ✅✅✅" at the top but then announces the NEXT pick below is a pre-game announcement — the ✅ line is prior context, the actual pick follows. Judge the role by the pick being named, not by stray emoji elsewhere in the tweet.

STEP 3: Extract the wager.

If the post has "OFFICIAL PLAY:" or "BEST PLAY:", extract ONLY that designated pick, ignore score predictions and other sides listed.

If the post is a single-game bet, extract it.

If the post is a multi-leg PARLAY (legs combined into one wager, e.g. "Brewers+Phillies MLP"), set "is_parlay": true and describe the whole parlay.

If the post is a "card" listing several separate plays, set "multi": true and extract the play tagged "(POD)" if one is tagged, otherwise the FIRST play listed.

Return JSON (no markdown fences). Return null only when no specific wager is named:

{{
  "sport": "NBA|WNBA|NCAAB|MLB|NFL|NCAAF|NHL|UFL|CFL|Tennis|UFC|Boxing|KBO|Soccer|Other",
  "category": "pod|secondary|max|card|result|other",
  "multi": false,
  "is_parlay": false,
  "pick": {{
    "description": "concise one-line summary of the exact bet",
    "bet_type": "spread|moneyline|total|team_total|prop|double_chance|draw_no_bet",
    "period": "game|1h|2h|1q|2q|3q|4q|1p|2p|3p",
    "teams": ["Full canonical team/player name(s)"],
    "player": "player name if player prop, else null",
    "prop_stat": "stat abbrev if prop (PTS, REB, HR, BTTS, etc.), else null",
    "line": null,
    "direction": "over|under|null"
  }}
}}

Sport classification rules:
- Country/national team names (France, Brazil, Germany, etc.) → Soccer
- Soccer: use "Soccer" for all football/soccer including World Cup, club matches
- NHL: hockey teams (Avalanche, Golden Knights, Hurricanes, etc.)
- NBA: basketball teams (Spurs, Cavs, Thunder, Knicks, etc.)
- MLB: baseball teams (Dodgers, Cubs, Astros, etc.)
- UFC/Boxing: individual fighter names
- For ambiguous names, use context (World Cup = Soccer, playoffs context, etc.)

Notation rules — these change how the bet grades, so read them carefully:
- "F5" / "First 5" / "1st 5" means the first five innings of a baseball game. Set period="1h" (baseball's first half IS the first 5 innings) and KEEP the "F5" wording in the description. An F5 bet graded as a full game is simply wrong.
- "TT" means team total — that one team's runs/points only. bet_type="team_total". "F5 TT o2.5" is BOTH: period="1h" AND bet_type="team_total".
- A bare "o"/"u" prefix means over/under: "u10.5" → direction="under", line=10.5.
- "ML" = moneyline. "-0.5"/"+1.5" style numbers are the spread line; three-digit numbers like "-130"/"+115" are the ODDS, not the line. Never put odds in "line".
- "MLP" means a moneyline parlay of the named teams — set is_parlay=true.
- "AB@BC o5.5" names the two teams in a game and a game total; teams should list both.
- The period belongs only to the leg it is written on. Sanity-check it: an MLB F5 total is ~3.5-7.5 runs, so a total above 7.5 marked "F5" is really a full-game line.

Tweet:
{text}"""

_IMAGE_PROMPT = """\
This tweet from a sports bettor contains a bet slip or wager image. \
The tweet text alone does not specify the exact pick — it is shown in the attached image.

{date_context}
Extract the SINGLE wager from the image and classify the ROLE of the post, exactly as
for text posts:
- "pod" — the free Play of the Day, announced pre-game
- "secondary" — an additional pre-game play (Play #2/#3, Second Play, Adding)
- "max" — a Max Play announced pre-game
- "card" — several separate plays listed at once (set "multi": true)
- "result" — the post reports the OUTCOME of a bet (CASH..., ✅, 💰, record improved).
  Extract the pick anyway and label it "result" — never "pod".
- "other" — commentary, a lean, or someone else's pick

If the image shows a multi-leg parlay (legs combined into one slip), set "is_parlay": true.
If it shows multiple separate single bets, set "multi": true and extract the first.

Tweet text: {text}

Return JSON (no markdown fences). Return null only if no specific wager is shown:

{{
  "sport": "NBA|WNBA|NCAAB|MLB|NFL|NCAAF|NHL|UFL|CFL|Tennis|UFC|Boxing|KBO|Soccer|Other",
  "category": "pod|secondary|max|card|result|other",
  "multi": false,
  "is_parlay": false,
  "pick": {{
    "description": "concise one-line summary of the exact bet",
    "bet_type": "spread|moneyline|total|team_total|prop|double_chance|draw_no_bet",
    "period": "game|1h|2h|1q|2q|3q|4q|1p|2p|3p",
    "teams": ["Full canonical team/player name(s)"],
    "player": "player name if player prop, else null",
    "prop_stat": "stat abbrev if prop (PTS, REB, HR, BTTS, etc.), else null",
    "line": null,
    "direction": "over|under|null"
  }}
}}

Notation: "F5"/"First 5" = first five innings of a baseball game → period="1h", and keep
"F5" in the description. "TT" = team total → bet_type="team_total". "o"/"u" prefix =
over/under. Three-digit numbers like "-130" are ODDS, not the line."""


# ─── Helpers ─────────────────────────────────────────────────────────────────

def _date_ctx(date_str: str) -> str:
    if not date_str:
        return ""
    d = _d.fromisoformat(date_str[:10])
    return f"Context: Today is {d.strftime('%A')}, {d.strftime('%B')} {d.day}, {d.year}.\n"


def _extract_result(raw_text: str) -> dict | None:
    raw = re.sub(r"^```(?:json)?\n?|```$", "", raw_text.strip(), flags=re.MULTILINE).strip()
    if raw.lower() == "null":
        return None
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        return None
    if not parsed or "pick" not in parsed or parsed["pick"] is None:
        return None
    return parsed


def _build_row(row: dict, parsed: dict) -> dict:
    pick = parsed["pick"]
    return {
        **row,
        "sport": parsed.get("sport", ""),
        "category": parsed.get("category", "other"),
        "multi": "1" if parsed.get("multi") else "",
        "is_parlay": "1" if parsed.get("is_parlay") else "",
        "description": pick.get("description", ""),
        "bet_type": pick.get("bet_type", ""),
        "teams": json.dumps(pick.get("teams", [])),
        "player": pick.get("player") or "",
        "prop_stat": pick.get("prop_stat") or "",
        "line": pick.get("line") if pick.get("line") is not None else "",
        "direction": pick.get("direction") or "",
        "period": pick.get("period", "game"),
    }


@functools.lru_cache(maxsize=None)
def _signal_re(extra: tuple[str, ...]) -> re.Pattern:
    parts = []
    for s in (*_PICK_SIGNALS, *extra):
        esc = re.escape(s)
        # Bare alphanumeric tokens get word boundaries; phrases with punctuation
        # or spaces ("i'm on", "i have $") are matched as plain substrings.
        parts.append(rf'\b{esc}\b' if s.isalnum() else esc)
    return re.compile("|".join(parts), re.IGNORECASE)


def _has_pick_signal(text: str, extra_signals: list[str] = ()) -> bool:
    return bool(_signal_re(tuple(extra_signals)).search(text))


_RT_RE = re.compile(r'^\s*RT @(\w+):\s*', re.DOTALL)


def unwrap_retweet(text: str, account: str) -> str | None:
    """Return the tweet body to parse, or None if this is someone else's retweet.

    A SELF-retweet is the author's own pick, not a repost of another handle, so
    dropping every "RT @" deletes real picks outright — for @boyerBets_ that was
    45 of 50 self-RTs with no other copy in the corpus, including whole PODs like
    "Twins F5 +0.5" (2026-07-24). Strip the prefix and parse the body; the
    pick-level dedup collapses any that also appear as an original. Retweets of
    other handles are still dropped — someone else's action, not this capper's.

    Why the original is *missing* rather than merely duplicated: X's profile
    timeline shows a self-retweet in place of the tweet it retweets, so the
    original is never emitted by the UserTweets endpoint even though it is an
    ordinary top-level post — verified, tweet_details returns 2080764014330581091
    fine (inReplyToId None, conversation id itself) while two independent timeline
    scans both omit it. fetch_x_posts.py now unwraps `retweetedTweet` at fetch
    time, which is the real fix because it also recovers the original's timestamp
    and media. This function remains the safety net for CSVs captured before that
    change.
    """
    m = _RT_RE.match(text)
    if not m:
        return text
    if m.group(1).lower() != account.lower().lstrip("@"):
        return None
    return text[m.end():]


def _normalize_team(name: str) -> str:
    """Normalize team name for dedup comparison."""
    # Lowercase, strip whitespace
    n = name.lower().strip()
    # Common expansions
    expansions = {
        "bosnia": "bosnia and herzegovina",
        "bosnia & herzegovina": "bosnia and herzegovina",
        "ivory coast": "ivory coast",
        "cote d'ivoire": "ivory coast",
        "usa": "united states",
        "south korea": "south korea",
        "korea": "south korea",
        "czechia": "czech republic",
        "dr congo": "democratic republic of congo",
        "congo": "democratic republic of congo",
    }
    return expansions.get(n, n)


def _normalize_teams_set(teams_json: str) -> set[str]:
    """Parse teams JSON into a set of normalized names for dedup comparison."""
    try:
        teams = json.loads(teams_json or "[]")
    except (json.JSONDecodeError, TypeError):
        return set()
    return {_normalize_team(t) for t in teams}


# ─── Phase 1: Text parse ─────────────────────────────────────────────────────

async def parse_text(row: dict, vocab_notes: str = "") -> dict | None:
    text = row.get("text", "").strip()
    if not text:
        return None
    try:
        resp = await _claude_create_with_retry(
            model="claude-sonnet-4-6",
            max_tokens=500,
            messages=[{"role": "user", "content": _PICK_PROMPT.format(
                text=text, date_context=_date_ctx(row.get("date", "")),
                vocab_notes=f"\n{vocab_notes}\n" if vocab_notes else "",
            )}],
        )
    except Exception as e:
        print(f"  ERROR text-parse {row.get('id')}: {e}")
        return None
    parsed = _extract_result(resp.content[0].text)
    return _build_row(row, parsed) if parsed else None


# ─── Phase 2: Image parse ────────────────────────────────────────────────────

async def download_image(url: str) -> tuple[str, bytes] | None:
    """Download image, return (media_type, bytes) or None."""
    try:
        async with httpx.AsyncClient(follow_redirects=True, timeout=15) as client:
            resp = await client.get(url)
            resp.raise_for_status()
            ct = resp.headers.get("content-type", "image/jpeg")
            media_type = ct.split(";")[0].strip()
            return media_type, resp.content
    except Exception as e:
        print(f"    image download failed {url}: {e}")
        return None


async def parse_image(row: dict) -> dict | None:
    photos = row.get("photos", "").strip()
    if not photos:
        return None
    # Use first image only
    img_url = photos.split("|")[0].strip()
    if not img_url:
        return None

    img = await download_image(img_url)
    if not img:
        return None
    media_type, img_bytes = img
    img_b64 = base64.b64encode(img_bytes).decode()

    text = row.get("text", "").strip()
    try:
        resp = await _claude_create_with_retry(
            model="claude-sonnet-4-6",
            max_tokens=500,
            messages=[{"role": "user", "content": [
                {"type": "image", "source": {"type": "base64", "media_type": media_type, "data": img_b64}},
                {"type": "text", "text": _IMAGE_PROMPT.format(
                    text=text, date_context=_date_ctx(row.get("date", ""))
                )},
            ]}],
        )
    except Exception as e:
        print(f"  ERROR image-parse {row.get('id')}: {e}")
        return None
    parsed = _extract_result(resp.content[0].text)
    return _build_row(row, parsed) if parsed else None


# ─── Phase 3: Dedup ──────────────────────────────────────────────────────────

def dedup(rows: list[dict]) -> list[dict]:
    """Remove duplicate tweet IDs and duplicate picks (same bet from multiple tweets)."""
    # 1. Dedup by tweet ID (keep first occurrence)
    seen_ids = set()
    unique = []
    for r in rows:
        if r["id"] not in seen_ids:
            seen_ids.add(r["id"])
            unique.append(r)
    rows = unique

    # 2. Dedup by pick identity (same day + normalized teams + bet_type + period).
    #    Line is excluded from the key because the same pick announced in text
    #    may include odds (e.g. "-150") while the image version omits them.
    #    Period IS in the key: a capper can play both "Cardinals F5 ML" and
    #    "Cardinals ML" on the same day, and those are two different bets.
    #    Sorting by date keeps the EARLIEST occurrence, which is what collapses a
    #    later "CASH THE POD" result post into the original pre-game announcement.
    rows.sort(key=lambda r: r["date"])
    final: list[dict] = []
    kept_teams: list[tuple] = []
    for r in rows:
        teams = _normalize_teams_set(r["teams"])
        sig = (r["date"][:10], r["bet_type"],
               r.get("period", "game"), r.get("direction", ""))
        # Same wager, one copy naming only the side ("Twins ML") and the other
        # naming the matchup ("Twins/Cardinals ML"). An exact team-set key misses
        # this, so compare by subset: within one date+bet_type+period+direction,
        # overlapping team sets where one contains the other are one bet. Two
        # genuinely different bets that day differ in side, so they never nest.
        dup = any(
            sig == ksig and (teams <= kteams or kteams <= teams)
            for ksig, kteams in kept_teams
        )
        if dup:
            continue
        kept_teams.append((sig, teams))
        final.append(r)

    return final


# ─── Main ────────────────────────────────────────────────────────────────────

async def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--account", required=True, help="X handle, e.g. boyerBets_")
    parser.add_argument("--limit", type=int, default=0, help="Process only first N rows (0=all)")
    parser.add_argument("--days", type=int, default=0,
                        help="Only the most recent N days of posts (0=all). Use for cheap test runs.")
    parser.add_argument("--skip-images", action="store_true", help="Skip phase 2 (image parsing)")
    parser.add_argument("--output", default=None, help="Override output CSV path")
    parser.add_argument("--input", default=None, help="Override input CSV path")
    args = parser.parse_args()

    input_csv = args.input or os.path.join(OUT_DIR, f"{args.account}_posts.csv")
    output_csv = args.output or os.path.join(OUT_DIR, f"{args.account}_parsed.csv")

    with open(input_csv, newline="", encoding="utf-8") as f:
        rows = list(csv.DictReader(f))

    # The fetch can emit the same tweet many times when pagination overlaps (one
    # @boyerBets_ post landed 33x). Every copy would be a separate paid API call.
    seen_ids: set[str] = set()
    deduped = []
    for r in rows:
        if r.get("id") in seen_ids:
            continue
        seen_ids.add(r.get("id", ""))
        deduped.append(r)
    if len(deduped) < len(rows):
        print(f"Dropped {len(rows) - len(deduped)} duplicate tweet ids")
    rows = deduped

    if args.days:
        latest = max(r["date"][:10] for r in rows)
        cutoff = (_d.fromisoformat(latest) - timedelta(days=args.days - 1)).isoformat()
        rows = [r for r in rows if r["date"][:10] >= cutoff]
        print(f"--days {args.days}: {len(rows)} posts from {cutoff} to {latest}")

    if args.limit:
        rows = rows[:args.limit]

    # Drop retweets of OTHER handles, but keep self-retweets with the "RT @me:"
    # prefix stripped — see unwrap_retweet. Rewriting `text` here means every
    # downstream phase (image candidacy, prompts, dedup) sees the clean body.
    original_count = len(rows)
    kept = []
    unwrapped = 0
    for r in rows:
        body = unwrap_retweet(r.get("text", ""), args.account)
        if body is None:
            continue
        if body != r.get("text", ""):
            r = {**r, "text": body}
            unwrapped += 1
        kept.append(r)
    rows = kept
    print(f"Filtered {original_count - len(rows)} retweets of other accounts; "
          f"kept {unwrapped} self-retweets")

    total = len(rows)

    vocab = _vocab(args.account)
    vocab_notes = vocab.get("notes", "")
    extra_signals = vocab.get("signals", [])
    if vocab_notes:
        print(f"Using account vocabulary for {args.account}")

    # ── Phase 1: Text parse ──────────────────────────────────────────────
    print(f"Phase 1: Text-parsing {total} rows...")
    results = []
    text_kept_ids = set()
    sem = asyncio.Semaphore(CONCURRENCY)
    lock = asyncio.Lock()

    async def process_text(i: int, row: dict):
        async with sem:
            result = await parse_text(row, vocab_notes)
        if result:
            async with lock:
                results.append(result)
                text_kept_ids.add(row["id"])
        status = "KEEP" if result else "skip"
        if (i + 1) % 100 == 0 or i + 1 == total:
            print(f"  [{i+1}/{total}] {status} | cost: {fmt_cost(usage_cost())}")

    await asyncio.gather(*(process_text(i, row) for i, row in enumerate(rows)))
    print(f"  Phase 1 done: {len(results)} picks from text")

    # ── Phase 2: Image parse for candidates ──────────────────────────────
    if not args.skip_images:
        # Candidates: posts with pick signals + images, not already parsed from text
        image_candidates = [
            r for r in rows
            if r["id"] not in text_kept_ids
            and r.get("photos", "").strip()
            and _has_pick_signal(r.get("text", ""), extra_signals)
        ]
        if image_candidates:
            print(f"Phase 2: Image-parsing {len(image_candidates)} candidates...")

            async def process_image(i: int, row: dict):
                async with sem:
                    result = await parse_image(row)
                if result:
                    async with lock:
                        results.append(result)
                status = "KEEP" if result else "skip"
                print(f"  img [{i+1}/{len(image_candidates)}] {status} | cost: {fmt_cost(usage_cost())}")

            await asyncio.gather(*(process_image(i, row) for i, row in enumerate(image_candidates)))
            print(f"  Phase 2 done: {len(results)} total picks")
        else:
            print("Phase 2: No image candidates found")

    # ── Phase 3: Dedup + sort ────────────────────────────────────────────
    print("Phase 3: Dedup + sort...")
    results = dedup(results)

    # ── Write output ─────────────────────────────────────────────────────
    with open(output_csv, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=OUT_FIELDS, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(results)

    by_cat = collections.Counter(r.get("category", "other") for r in results)
    print(f"\nDone! {len(results)} unique wagers extracted")
    print("By category:")
    for cat, n in by_cat.most_common():
        keep = "KEEP" if cat in GRADEABLE_CATEGORIES else "excluded"
        print(f"  {cat:10s} {n:4d}  {keep}")
    gradeable = sum(n for c, n in by_cat.items() if c in GRADEABLE_CATEGORIES)
    print(f"  {'-' * 28}\n  gradeable  {gradeable:4d}")
    print(f"Output: {output_csv}")
    print(f"Total API cost: {fmt_cost(usage_cost())}")


if __name__ == "__main__":
    asyncio.run(main())
