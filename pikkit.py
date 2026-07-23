"""
pikkit.py — Fetch public betting consensus from OddsShark.

Scrapes consensus pick percentages (moneyline, over/under) from
oddsshark.com and classifies each pick as "public" or "book" side.
No auth required.

The module name is kept as pikkit.py for backwards compatibility with
tracker.py, extract_angles.py, and the dashboard.
"""

import re
import logging
from typing import Any

import httpx
from bs4 import BeautifulSoup

log = logging.getLogger(__name__)

_TIMEOUT = 15

# ── In-memory cache (per-process, keyed by sport) ────────────────────────

_consensus_cache: dict[str, list[dict]] = {}  # sport → [game, ...]

# ── Sport → OddsShark URL slug ───────────────────────────────────────────

_SPORT_SLUGS: dict[str, str] = {
    "MLB":    "mlb",
    "NBA":    "nba",
    "NFL":    "nfl",
    "NHL":    "nhl",
    "WNBA":  "wnba",
    "NCAAF":  "ncaaf",
    "CFB":    "ncaaf",
    "NCAAB":  "ncaab",
    "CBB":    "ncaab",
}


# ── Scrape consensus page ────────────────────────────────────────────────


async def _fetch_consensus(sport: str) -> list[dict]:
    """Scrape OddsShark consensus picks for a sport.

    Returns list of dicts, each with:
      home, away (team names),
      home_ml_pct, away_ml_pct (moneyline consensus %),
      over_pct, under_pct, ou_line (over/under consensus).
    """
    slug = _SPORT_SLUGS.get(sport)
    if not slug:
        return []

    if sport in _consensus_cache:
        return _consensus_cache[sport]

    url = f"https://www.oddsshark.com/{slug}/consensus-picks"
    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT, follow_redirects=True) as client:
            resp = await client.get(url, headers={
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            })
    except httpx.HTTPError as e:
        log.warning("[splits] fetch error for %s: %s", sport, e)
        return []

    if resp.status_code != 200:
        log.warning("[splits] %s status %d", sport, resp.status_code)
        return []

    games = _parse_consensus_html(resp.text)
    if games:
        _consensus_cache[sport] = games
        log.info("[splits] loaded %d games for %s", len(games), sport)
    return games


def _parse_consensus_html(html: str) -> list[dict]:
    """Parse OddsShark consensus page HTML into structured game data.

    Each game block in the text follows:
      AWAY_ABBR  →  XX%  →  odds  →  O  →  line  →  XX%  →  ...
      HOME_ABBR  →  XX%  →  odds  →  U  →  line  →  XX%  →  ...
    We find ABBR lines (2-4 uppercase letters) followed by a "XX%" line.
    """
    soup = BeautifulSoup(html, "html.parser")
    text = soup.get_text(separator="\n")
    lines = [l.strip() for l in text.split("\n") if l.strip()]

    games: list[dict] = []
    abbr_re = re.compile(r"^[A-Z]{2,4}$")
    pct_re = re.compile(r"^(\d+)%$")

    i = 0
    while i < len(lines) - 12:
        # Look for: ABBR, then %, then eventually another ABBR, then %
        if not abbr_re.match(lines[i]):
            i += 1
            continue

        m1 = pct_re.match(lines[i + 1]) if i + 1 < len(lines) else None
        if not m1:
            i += 1
            continue

        away_abbr = lines[i]
        away_pct = int(m1.group(1))

        # After away's block: O, line, over%, then eventually HOME_ABBR, home%
        # Scan forward for over/under and the second team
        over_pct = under_pct = ou_line = None
        home_abbr = home_pct_val = None

        for j in range(i + 2, min(i + 20, len(lines))):
            if lines[j] == "O" and j + 2 < len(lines):
                try:
                    ou_line = float(lines[j + 1])
                    pm = pct_re.match(lines[j + 2])
                    if pm:
                        over_pct = int(pm.group(1))
                except ValueError:
                    pass
            elif lines[j] == "U" and j + 2 < len(lines):
                try:
                    float(lines[j + 1])  # validate it's a number
                    pm = pct_re.match(lines[j + 2])
                    if pm:
                        under_pct = int(pm.group(1))
                except ValueError:
                    pass
            elif abbr_re.match(lines[j]) and j + 1 < len(lines):
                pm = pct_re.match(lines[j + 1])
                if pm:
                    home_abbr = lines[j]
                    home_pct_val = int(pm.group(1))
                    i = j + 2  # advance past this game block
                    break
        else:
            i += 1
            continue

        if home_abbr and home_pct_val is not None:
            # Scan past home ABBR for U data if we didn't find it yet
            if under_pct is None:
                for k in range(i - 2, min(i + 10, len(lines))):
                    if k < len(lines) and lines[k] == "U" and k + 2 < len(lines):
                        try:
                            float(lines[k + 1])
                            pm = pct_re.match(lines[k + 2])
                            if pm:
                                under_pct = int(pm.group(1))
                        except ValueError:
                            pass

            game: dict[str, Any] = {
                "away_abbr": away_abbr,
                "home_abbr": home_abbr,
                "away_ml_pct": away_pct / 100,
                "home_ml_pct": home_pct_val / 100,
            }
            if over_pct is not None:
                game["over_pct"] = over_pct / 100
            if under_pct is not None:
                game["under_pct"] = under_pct / 100
            if ou_line is not None:
                game["ou_line"] = ou_line
            games.append(game)

    return games


# ── Team matching ────────────────────────────────────────────────────────

# Map common abbreviations to full team names for matching
_ABBR_TO_NAMES: dict[str, list[str]] = {
    # MLB
    "NYY": ["yankees", "new york yankees"],
    "BOS": ["red sox", "boston red sox"],
    "LAD": ["dodgers", "los angeles dodgers"],
    "LA":  ["dodgers", "los angeles dodgers"],
    "ATL": ["braves", "atlanta braves"],
    "HOU": ["astros", "houston astros"],
    "PHI": ["phillies", "philadelphia phillies"],
    "SD":  ["padres", "san diego padres"],
    "SF":  ["giants", "san francisco giants"],
    "TB":  ["rays", "tampa bay rays"],
    "NYM": ["mets", "new york mets"],
    "MIL": ["brewers", "milwaukee brewers"],
    "CLE": ["guardians", "cleveland guardians"],
    "MIN": ["twins", "minnesota twins"],
    "BAL": ["orioles", "baltimore orioles"],
    "SEA": ["mariners", "seattle mariners"],
    "TEX": ["rangers", "texas rangers"],
    "CHC": ["cubs", "chicago cubs"],
    "CIN": ["reds", "cincinnati reds"],
    "ARI": ["diamondbacks", "arizona diamondbacks", "dbacks", "d-backs"],
    "KC":  ["royals", "kansas city royals"],
    "DET": ["tigers", "detroit tigers"],
    "PIT": ["pirates", "pittsburgh pirates"],
    "STL": ["cardinals", "st. louis cardinals", "st louis cardinals"],
    "COL": ["rockies", "colorado rockies"],
    "MIA": ["marlins", "miami marlins"],
    "WSH": ["nationals", "washington nationals"],
    "LAA": ["angels", "los angeles angels"],
    "ATH": ["athletics", "oakland athletics", "oakland a's", "a's"],
    "OAK": ["athletics", "oakland athletics"],
    "CWS": ["white sox", "chicago white sox"],
    "CHW": ["white sox", "chicago white sox"],
    "TOR": ["blue jays", "toronto blue jays"],
    # NBA
    "LAL": ["lakers", "los angeles lakers"],
    "BKN": ["nets", "brooklyn nets"],
    "GSW": ["warriors", "golden state warriors"],
    "LAC": ["clippers", "los angeles clippers", "la clippers"],
    "CHI": ["bulls", "chicago bulls"],
    "DAL": ["mavericks", "dallas mavericks", "mavs"],
    "DEN": ["nuggets", "denver nuggets"],
    "IND": ["pacers", "indiana pacers"],
    "MEM": ["grizzlies", "memphis grizzlies"],
    "OKC": ["thunder", "oklahoma city thunder"],
    "PHX": ["suns", "phoenix suns"],
    "POR": ["trail blazers", "portland trail blazers", "blazers"],
    "SAC": ["kings", "sacramento kings"],
    "SAS": ["spurs", "san antonio spurs"],
    "UTA": ["jazz", "utah jazz"],
    "WAS": ["wizards", "washington wizards"],
    "CHA": ["hornets", "charlotte hornets"],
    "NOP": ["pelicans", "new orleans pelicans"],
    "ORL": ["magic", "orlando magic"],
    # NFL
    "NE":  ["patriots", "new england patriots"],
    "GB":  ["packers", "green bay packers"],
    "NO":  ["saints", "new orleans saints"],
    "LV":  ["raiders", "las vegas raiders"],
    "JAX": ["jaguars", "jacksonville jaguars"],
    "TEN": ["titans", "tennessee titans"],
    "CAR": ["panthers", "carolina panthers"],
    # NHL
    "VGK": ["golden knights", "vegas golden knights"],
    "EDM": ["oilers", "edmonton oilers"],
    "WPG": ["jets", "winnipeg jets"],
    "VAN": ["canucks", "vancouver canucks"],
    "CGY": ["flames", "calgary flames"],
    "OTT": ["senators", "ottawa senators"],
    "MTL": ["canadiens", "montreal canadiens"],
    "BUF": ["sabres", "buffalo sabres"],
    "FLA": ["panthers", "florida panthers"],
    "CBJ": ["blue jackets", "columbus blue jackets"],
    "ANA": ["ducks", "anaheim ducks"],
    "SJS": ["sharks", "san jose sharks"],
    "NSH": ["predators", "nashville predators"],
    "STL_NHL": ["blues", "st. louis blues"],
    "CAR_NHL": ["hurricanes", "carolina hurricanes"],
    "NYR": ["rangers", "new york rangers"],
    "NYI": ["islanders", "new york islanders"],
    # WNBA
    "LAS": ["aces", "las vegas aces"],
    "SEA_W": ["storm", "seattle storm"],
    "NY":  ["liberty", "new york liberty"],
    "CONN": ["sun", "connecticut sun"],
    "CHI_W": ["sky", "chicago sky"],
    "MIN_W": ["lynx", "minnesota lynx"],
    "IND_W": ["fever", "indiana fever"],
    "DAL_W": ["wings", "dallas wings"],
    "PHX_W": ["mercury", "phoenix mercury"],
    "ATL_W": ["dream", "atlanta dream"],
    "WAS_W": ["mystics", "washington mystics"],
    "LAX": ["sparks", "los angeles sparks"],
    "GSV": ["valkyries", "golden state valkyries"],
    "POR_W": ["fire", "portland fire"],
}


def _normalize(name: str) -> str:
    return re.sub(r"[^a-z0-9 ]", "", name.lower()).strip()


def _abbr_matches_team(abbr: str, pick_team: str) -> bool:
    """Check if an OddsShark abbreviation matches a pick's team name."""
    pn = _normalize(pick_team)
    if not pn:
        return False

    # Direct abbreviation match
    if _normalize(abbr) == pn:
        return True

    # Check our abbreviation map
    names = _ABBR_TO_NAMES.get(abbr, [])
    for name in names:
        nn = _normalize(name)
        if nn == pn or nn in pn or pn in nn:
            return True
        # Single-word match: "cubs" matches "chicago cubs"
        if pn in nn.split() or nn in pn.split():
            return True

    return False


def _match_game(pick: dict, games: list[dict]) -> tuple[dict, str] | None:
    """Match a pick to an OddsShark game. Returns (game, 'home'|'away') or None."""
    teams = pick.get("teams", [])
    if not teams:
        return None

    for game in games:
        for team in teams:
            if _abbr_matches_team(game["home_abbr"], team):
                return game, "home"
            if _abbr_matches_team(game["away_abbr"], team):
                return game, "away"
    return None


# ── Classify pick side ───────────────────────────────────────────────────


def _classify(pick: dict, game: dict, side: str) -> dict | None:
    """Classify a pick as public or book side using consensus data."""
    bet_type = (pick.get("bet_type") or "").lower()
    direction = (pick.get("direction") or "").lower()
    desc = (pick.get("description") or "").lower()

    # Determine market
    is_total = bet_type in ("total", "over/under", "over", "under") or \
               "over" in desc or "under" in desc

    if is_total:
        over_pct = game.get("over_pct")
        under_pct = game.get("under_pct")
        if over_pct is None and under_pct is None:
            return None

        if direction == "over" or "over" in desc:
            pct = over_pct or (1 - under_pct if under_pct else None)
        elif direction == "under" or "under" in desc:
            pct = under_pct or (1 - over_pct if over_pct else None)
        else:
            return None

        if pct is None:
            return None
        market = "total"
    else:
        # ML or spread — use moneyline consensus
        if side == "home":
            pct = game.get("home_ml_pct")
        else:
            pct = game.get("away_ml_pct")
        if pct is None:
            return None
        market = "moneyline"

    pick_side = "public" if pct > 0.5 else "book"

    return {
        "side":       pick_side,
        "public_pct": round(pct, 4),
        "market":     market,
        "source":     "oddsshark",
    }


# ── High-level: fetch splits for a pick ─────────────────────────────────


async def get_pick_splits(
    pick: dict,
    sport: str,
    dt: str,
) -> dict | None:
    """End-to-end: scrape OddsShark consensus and classify a pick.

    Returns a dict suitable for storing in pikkit_by_pick, or None.
    """
    games = await _fetch_consensus(sport)
    if not games:
        return None

    result = _match_game(pick, games)
    if not result:
        return None

    game, side = result
    return _classify(pick, game, side)
