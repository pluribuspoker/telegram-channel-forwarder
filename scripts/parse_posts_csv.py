"""
Parse a capper's posts CSV to extract official pick placements.

Three-phase pipeline:
  1. Text parse: send tweet text to Claude, extract single official picks
  2. Image parse: for posts that look like pick announcements but had no
     extractable pick from text alone (pick is in attached image), download
     the image and send it to Claude along with the text
  3. Dedup: remove duplicate picks (same bet from multiple tweets)

Usage:
    python scripts/parse_posts_csv.py [--limit N] [--skip-images]
"""

import argparse
import asyncio
import csv
import json
import re
import sys
import os
import base64
from datetime import date as _d

import httpx

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from ai import _claude_create_with_retry, usage_cost, fmt_cost

INPUT = os.path.join(os.path.dirname(__file__), "output", "BookitWithTrent_posts.csv")
OUTPUT = os.path.join(os.path.dirname(__file__), "output", "BookitWithTrent_parsed.csv")

CONCURRENCY = 10

OUT_FIELDS = [
    "id", "date", "text", "photos", "videos", "url",
    "sport", "description", "bet_type", "teams", "player",
    "prop_stat", "line", "direction", "period",
]

# ── Signals that a post MIGHT contain a pick (used to identify image candidates)
_PICK_SIGNALS = [
    "mortal mega", "mortal", "mega max", "nuke", "nuking",
    "official", "play of", "square of", "last chance u",
    "i'm on", "i'll be on", "i will be taking", "i'm riding",
    "i have $", "dropping", "10u", "10 unit",
    "slip", "fugazi",
]

# ─── Prompts ─────────────────────────────────────────────────────────────────

_PICK_PROMPT = """\
You are analyzing a tweet from a sports bettor to determine if it contains an OFFICIAL PICK PLACEMENT — a real wager being announced.

{date_context}
STEP 1: Is this an official pick ANNOUNCEMENT?

An official pick is a tweet where the bettor is ANNOUNCING a wager they are placing RIGHT NOW. Look for these signals:
- "mortal mega" / "mortal mega max" / "mega" — signature bet branding
- "nuke" / "nuking" / "I'm nuking [team]" — placing a big bet
- "OFFICIAL PLAY:" / "BEST PLAY:" — designates the pick in multi-prediction posts
- "I'm on [pick]" / "I'll be on" / "I will be taking" / "I'm riding with"
- "I have $X on [team]" / "dropping $X on"
- "10u" / "10 unit" / "$X,000" — unit/dollar sizing
- "play of year" / "square of year" / "play of the day"
- "FUGAZI 5" / "[N]-man nuke" — named parlay formats
- "Last Chance U slip" — named bet format
- Explicit first-person declaration of placing a specific bet with a named team/side/line

NOT an official pick (return null):
- Celebrations/results: "BANGGGG", "✅✅✅", "CASH THE MORTAL MEGA", "howsya", win announcements
- Loss reactions: "chalked", "GGs", "dead", "fuck", "horrible wager", "🥀", "❌"
- In-game commentary: "if this goes over", "they ain't scoring", "I wanna throw up"
- Hopes/wishes without placement: "Life on line, Mbappe is NOT scoring", "will do anything for an Austria bang"
- Rhetorical/teasers: "why shouldn't I just put...", "I wanna nuke it so bad", "mortal megas?👀", "the next nuke is loading..."
- Off-topic: giveaways, streams, card ripping, podcast links
- Pure excitement about a game event: "CEASE NO HITTER" x3 (celebrating in progress, not a bet)
- Score updates, goal celebrations, general commentary
- Teaser without naming the pick: "I have the mortal mega" / "I have the mortal mega. 😈" (no team/line named = not a pick yet)
- Referencing a PAST or EXISTING bet without announcing it: "I lost $X on...", "if you took [team] you're sharp", "mom has no clue I have $5k on [team]", "I will tell my grandchildren about [pick]" — these mention a bet but are NOT the original announcement
- Corrections or clarifications of a PRIOR pick: "AI switched the wording, this is [team] +1.5" — this is clarifying a pick already announced in an earlier tweet, not a new pick

The KEY distinction: the tweet must be the ORIGINAL ANNOUNCEMENT where the bettor declares they are placing the wager. Tweets that reference, joke about, reminisce about, or correct an already-announced bet are NOT picks.

A tweet that mentions "3 STRAIGHT WINNERS ✅✅✅" at the top but then announces the NEXT pick below is still a pick placement — the ✅ line is context, the actual pick follows.

STEP 2: If the post IS an official pick, extract exactly ONE pick.

If the post has "OFFICIAL PLAY:" or "BEST PLAY:", extract ONLY that designated pick, ignore the score predictions and other sides listed.

If the post announces a single bet (e.g. "Germany -1.5 for a mortal mega"), extract that.

If the post is a named parlay (FUGAZI 5, 4-man nuke, etc.) with multiple legs, return null — we only want single-game picks.

Return JSON (no markdown fences). Return null if no official pick or if the pick is a multi-leg parlay:

{{
  "sport": "NBA|NCAAB|MLB|NFL|NHL|UFL|CFL|Tennis|UFC|Boxing|KBO|Soccer|Other",
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

Tweet:
{text}"""

_IMAGE_PROMPT = """\
This tweet from a sports bettor contains a bet slip or wager image. \
The tweet text alone does not specify the exact pick — it is shown in the attached image.

{date_context}
Extract the SINGLE official pick from the image. If the image shows a multi-leg parlay \
(multiple bets combined into one slip), return null — we only want single-game picks. \
If the image shows multiple separate single bets, return null.

Tweet text: {text}

Return JSON (no markdown fences). Return null if multi-leg parlay or no clear single pick:

{{
  "sport": "NBA|NCAAB|MLB|NFL|NHL|UFL|CFL|Tennis|UFC|Boxing|KBO|Soccer|Other",
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
}}"""


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
        "description": pick.get("description", ""),
        "bet_type": pick.get("bet_type", ""),
        "teams": json.dumps(pick.get("teams", [])),
        "player": pick.get("player") or "",
        "prop_stat": pick.get("prop_stat") or "",
        "line": pick.get("line") if pick.get("line") is not None else "",
        "direction": pick.get("direction") or "",
        "period": pick.get("period", "game"),
    }


def _has_pick_signal(text: str) -> bool:
    tl = text.lower()
    return any(s in tl for s in _PICK_SIGNALS)


def _is_retweet(text: str) -> bool:
    """Detect retweets (RT @user: ...)."""
    return text.lstrip().startswith("RT @")


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


def _normalize_teams_key(teams_json: str) -> str:
    """Parse teams JSON, normalize each name, sort, return stable string."""
    try:
        teams = json.loads(teams_json)
    except (json.JSONDecodeError, TypeError):
        return teams_json
    normalized = sorted(_normalize_team(t) for t in teams)
    return json.dumps(normalized)


# ─── Phase 1: Text parse ─────────────────────────────────────────────────────

async def parse_text(row: dict) -> dict | None:
    text = row.get("text", "").strip()
    if not text or _is_retweet(text):
        return None
    try:
        resp = await _claude_create_with_retry(
            model="claude-sonnet-4-6",
            max_tokens=500,
            messages=[{"role": "user", "content": _PICK_PROMPT.format(
                text=text, date_context=_date_ctx(row.get("date", ""))
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

    # 2. Dedup by pick identity (same day + normalized teams + same bet_type)
    #    Line is excluded from the key because the same pick announced in text
    #    may include odds (e.g. "-150") while the image version omits them.
    #    Keeps the first (earliest) occurrence — the original announcement.
    rows.sort(key=lambda r: r["date"])
    seen_picks = set()
    final = []
    for r in rows:
        norm_teams = _normalize_teams_key(r["teams"])
        key = (r["date"][:10], norm_teams, r["bet_type"])
        if key not in seen_picks:
            seen_picks.add(key)
            final.append(r)

    return final


# ─── Main ────────────────────────────────────────────────────────────────────

async def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--limit", type=int, default=0, help="Process only first N rows (0=all)")
    parser.add_argument("--skip-images", action="store_true", help="Skip phase 2 (image parsing)")
    args = parser.parse_args()

    with open(INPUT, newline="", encoding="utf-8") as f:
        rows = list(csv.DictReader(f))

    if args.limit:
        rows = rows[:args.limit]

    # Pre-filter: skip retweets before sending to API
    original_count = len(rows)
    rows = [r for r in rows if not _is_retweet(r.get("text", ""))]
    if len(rows) < original_count:
        print(f"Filtered {original_count - len(rows)} retweets")

    total = len(rows)

    # ── Phase 1: Text parse ──────────────────────────────────────────────
    print(f"Phase 1: Text-parsing {total} rows...")
    results = []
    text_kept_ids = set()
    sem = asyncio.Semaphore(CONCURRENCY)
    lock = asyncio.Lock()

    async def process_text(i: int, row: dict):
        async with sem:
            result = await parse_text(row)
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
            and _has_pick_signal(r.get("text", ""))
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
    with open(OUTPUT, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=OUT_FIELDS, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(results)

    print(f"\nDone! {len(results)} unique official picks")
    print(f"Output: {OUTPUT}")
    print(f"Total API cost: {fmt_cost(usage_cost())}")


if __name__ == "__main__":
    asyncio.run(main())
