"""
scores.py — Sports data layer: ESPN, Odds API, and score formatting.
"""

import asyncio
import json
import os
import re
import time

import httpx
from datetime import date as _date, datetime as _datetime, timedelta, timezone

# common.py imports nothing from here, so this stays one-directional.
from common import is_regulation_ml


# ─── ESPN ─────────────────────────────────────────────────────────────────────

ESPN_LEAGUES: dict[str, tuple[str, str]] = {
    "NBA":   ("basketball", "nba"),
    "WNBA":  ("basketball", "wnba"),
    "NCAAB": ("basketball", "mens-college-basketball"),
    "MLB":   ("baseball", "mlb"),
    "NFL":   ("football", "nfl"),
    "NHL":   ("hockey", "nhl"),
    "NCAAF": ("football", "college-football"),
    "UFC":   ("mma", "ufc"),
    "UFL":   ("football", "ufl"),
    "CFL":   ("football", "cfl"),
    "Lacrosse": ("lacrosse", "pll"),
}

# Fallback ESPN leagues, searched only when the primary scoreboard has no events
# (i.e. the offseason). NBA Summer League games live on separate league endpoints,
# never overlapping the regular season by date, so merging them in is unambiguous.
ESPN_FALLBACK_LEAGUES: dict[str, list[tuple[str, str]]] = {
    "NBA": [
        ("basketball", "nba-summer-las-vegas"),
        ("basketball", "nba-summer-sacramento"),
        ("basketball", "nba-summer-california"),
        ("basketball", "nba-summer-utah"),
        ("basketball", "nba-summer-orlando"),
    ],
}

# Soccer: multiple ESPN leagues to search across
SOCCER_LEAGUES: list[tuple[str, str]] = [
    ("soccer", "ger.1"),           # Bundesliga
    ("soccer", "eng.1"),           # EPL
    ("soccer", "esp.1"),           # La Liga
    ("soccer", "ita.1"),           # Serie A
    ("soccer", "fra.1"),           # Ligue 1
    ("soccer", "usa.1"),           # MLS
    ("soccer", "uefa.champions"),  # Champions League
    ("soccer", "uefa.europa"),     # Europa League
    ("soccer", "uefa.champions_qual"),  # Champions League Qualifying
    ("soccer", "uefa.europa_qual"),     # Europa League Qualifying
    ("soccer", "uefa.conf"),       # Conference League
    ("soccer", "fifa.world"),      # FIFA World Cup
    # Domestic leagues cappers bet that aren't in the top 5 — ESPN covers
    # scores for all of these. Missing them made picks grade as UNKNOWN.
    ("soccer", "swe.1"),           # Swedish Allsvenskan
    ("soccer", "nor.1"),           # Norwegian Eliteserien
    ("soccer", "den.1"),           # Danish Superliga
    ("soccer", "fin.1"),           # Finnish Veikkausliga
    ("soccer", "ned.1"),           # Dutch Eredivisie
    ("soccer", "por.1"),           # Portuguese Primeira Liga
    ("soccer", "bel.1"),           # Belgian Pro League
    ("soccer", "tur.1"),           # Turkish Super Lig
    ("soccer", "sco.1"),           # Scottish Premiership
    ("soccer", "aut.1"),           # Austrian Bundesliga
    ("soccer", "sui.1"),           # Swiss Super League
    ("soccer", "gre.1"),           # Greek Super League
    ("soccer", "bra.1"),           # Brazilian Serie A
    ("soccer", "arg.1"),           # Argentine Liga Profesional
    ("soccer", "mex.1"),           # Mexican Liga MX
    ("soccer", "jpn.1"),           # Japanese J.League
]

# Extra query params per sport (e.g. groups=50 for all D1 NCAAB games)
SPORT_EXTRA_PARAMS: dict[str, dict] = {
    "NCAAB": {"groups": "50"},
    # groups=90 = all Division I (FBS+FCS). The default scoreboard is FBS-only,
    # which leaves FCS matchups (UT Martin, Central Arkansas, ...) invisible.
    "NCAAF": {"groups": "90"},
}

# Odds API sport keys for sports not on ESPN
ODDS_API_KEYS: dict[str, str] = {
    "Boxing": "boxing_boxing",
    "KBO":    "baseball_kbo",
}

_odds_requests_remaining: str | None = None
_odds_requests_used: int = 0


def odds_requests_used() -> int:
    return _odds_requests_used


def odds_requests_remaining() -> str | None:
    return _odds_requests_remaining


async def fetch_odds_api_scores(sport: str, date: str, completed_only: bool = True) -> list[dict]:
    """
    Fetch completed scores from the Odds API for a given sport and date (±1 day).
    Only works within the last ~3 days on the free tier.
    """
    global _odds_requests_remaining, _odds_requests_used
    sport_key = ODDS_API_KEYS.get(sport)
    if not sport_key:
        return []
    api_key = os.getenv("ODDS_API_KEY", "")
    if not api_key:
        return []
    target = _date.fromisoformat(date)
    async with httpx.AsyncClient(timeout=15) as http:
        try:
            r = await http.get(
                f"https://api.the-odds-api.com/v4/sports/{sport_key}/scores",
                params={"apiKey": api_key, "daysFrom": 3},
            )
            r.raise_for_status()
            _odds_requests_remaining = r.headers.get("x-requests-remaining", _odds_requests_remaining)
            used = r.headers.get("x-requests-used")
            if used:
                _odds_requests_used = int(used)
            print(f"    [Odds API] quota remaining: {_odds_requests_remaining}")
            events = r.json()
            if not isinstance(events, list):
                return []
            results = []
            for e in events:
                if completed_only and not e.get("completed"):
                    continue
                try:
                    event_date = _date.fromisoformat(e.get("commence_time", "")[:10])
                    if abs((event_date - target).days) <= 1:
                        results.append(e)
                except ValueError:
                    pass
            return results
        except Exception as exc:
            print(f"    [Odds API error] {sport} {date}: {exc}")
            return []


# ─── KBO (koreabaseball.com) ──────────────────────────────────────────────────

KBO_TEAM_IDS: dict[str, str] = {
    "KT": "KT Wiz",
    "HH": "Hanwha Eagles",
    "LG": "LG Twins",
    "HT": "KIA Tigers",
    "SK": "SSG Landers",
    "WO": "Kiwoom Heroes",
    "OB": "Doosan Bears",
    "SS": "Samsung Lions",
    "LT": "Lotte Giants",
    "NC": "NC Dinos",
}


async def _fetch_kbo_day(http: httpx.AsyncClient, date_str: str) -> list[dict]:
    """Fetch KBO games for a single YYYYMMDD date. Returns Odds-API-compatible dicts."""
    try:
        r = await http.post(
            "https://www.koreabaseball.com/ws/Main.asmx/GetKboGameList",
            json={"leId": "1", "srId": "0,1,3,4,5,7,8,9", "date": date_str},
            headers={
                "Content-Type": "application/json",
                # koreabaseball.com now rejects requests without a Referer
                # (returns an HTML error page → JSON decode fails). See #KBO.
                "Referer": "https://www.koreabaseball.com/",
            },
            timeout=15,
        )
        r.raise_for_status()
        data, _ = json.JSONDecoder().raw_decode(r.text)
        inner = data.get("d", data)
        if isinstance(inner, str):
            inner = json.loads(inner)
        results = []
        for g in (inner.get("game", []) if isinstance(inner, dict) else []):
            home_name = KBO_TEAM_IDS.get(g.get("HOME_ID", ""), g.get("HOME_ID", ""))
            away_name = KBO_TEAM_IDS.get(g.get("AWAY_ID", ""), g.get("AWAY_ID", ""))
            completed = g.get("GAME_RESULT_CK") == 1
            results.append({
                "home_team": home_name,
                "away_team": away_name,
                "completed": completed,
                "scores": [
                    {"name": away_name, "score": g.get("T_SCORE_CN", "")},
                    {"name": home_name, "score": g.get("B_SCORE_CN", "")},
                ] if completed else None,
            })
        return results
    except Exception as exc:
        print(f"    [KBO error] {date_str}: {exc}")
        return []


async def fetch_kbo_context(
    team: str, date: str, *, odds_game_date: str | None = None,
) -> tuple[str, str]:
    """Grade a KBO pick by checking koreabaseball.com.

    If *odds_game_date* is available (from the Odds API commence_time), we
    use that exact date.  Otherwise we fall back to date+1 (the original
    heuristic — picks are sent US evening before the KST game day).
    We never check date+0 because back-to-back series (common in KBO/MLB)
    would match the wrong completed game.
    Returns (context_str, game_date).
    """
    if odds_game_date:
        game_date = _date.fromisoformat(odds_game_date)
    else:
        game_date = _date.fromisoformat(date) + timedelta(days=1)
    game_date_str = game_date.isoformat()

    team_lower = team.lower().strip()
    async with httpx.AsyncClient(timeout=15) as http:
        games = await _fetch_kbo_day(http, game_date.strftime("%Y%m%d"))

    for e in games:
        home, away = e.get("home_team", ""), e.get("away_team", "")
        if not (_team_matches(team_lower, home.lower()) or _team_matches(team_lower, away.lower())):
            continue
        if e.get("completed"):
            scores = e.get("scores") or []
            score_str = "  ".join(f"{s['name']}: {s['score']}" for s in scores)
            return f"{home} vs {away}\n{score_str}", game_date_str
        return "PENDING", game_date_str

    return "", date


# ─── CFL (cfl.ca) ───────────────────────────────────────────────────────────

CFL_TEAMS: dict[str, str] = {
    "SSK": "Saskatchewan Roughriders",
    "OTT": "Ottawa Redblacks",
    "MTL": "Montreal Alouettes",
    "HAM": "Hamilton Tiger-Cats",
    "WPG": "Winnipeg Blue Bombers",
    "CGY": "Calgary Stampeders",
    "EDM": "Edmonton Elks",
    "TOR": "Toronto Argonauts",
    "BC":  "BC Lions",
}

# Reverse: full name → abbreviation
_CFL_ABBR = {v.lower(): k for k, v in CFL_TEAMS.items()}


def _parse_cfl_schedule(html: str) -> list[dict]:
    """Parse CFL.ca schedule page into a list of game dicts with quarter scores."""
    games: list[dict] = []

    # Split per game card. Do NOT split on the `int_timestamp` script: an
    # IN-PROGRESS game renders its quarter/clock ("4th 15:00") instead of that
    # script, so a live game carries no timestamp and would be swallowed into
    # the previous card's block — invisible to the parser and thus ungradeable
    # until the game went final. Every card (upcoming, live, final) has the
    # `div-game-id-` anchor.
    blocks = re.split(r'<div id="div-game-id-\d+"', html)
    if len(blocks) < 2:
        return games

    for block in blocks[1:]:
        # Find the first <table> in this block (quarter scores)
        table_m = re.search(r'<table[^>]*>(.*?)</table>', block, re.DOTALL)
        if not table_m:
            continue

        # Date: prefer the card's own kickoff timestamp; a live card has none,
        # so fall back to the date embedded in its ad-slot id.
        pre_table = block[:table_m.start()]
        ts_m = re.search(r'var\s+int_timestamp\s*=\s*Number\((\d+)\)', pre_table)
        if ts_m:
            game_date = _datetime.fromtimestamp(
                int(ts_m.group(1)), tz=timezone.utc).strftime("%Y-%m-%d")
        else:
            ad_m = re.search(r'id="ad-schedule-game-(\d{4}-\d{2}-\d{2})-', pre_table)
            if not ad_m:
                continue
            game_date = ad_m.group(1)

        rows = re.findall(r'<tr[^>]*>(.*?)</tr>', table_m.group(1), re.DOTALL)
        if len(rows) < 3:
            continue

        # Header row uses <th> tags (0 <td> cells); data rows use <td>
        team_rows = []
        for row in rows:
            cells = re.findall(r'<td[^>]*>(.*?)</td>', row, re.DOTALL)
            cells = [c.strip() for c in cells]
            if len(cells) < 5:  # need abbr + at least 4 quarters
                continue
            abbr = cells[0]
            # All cells after abbr are quarter scores (no total column)
            # OT games may have "-" for teams that didn't score in OT
            quarter_scores = cells[1:]
            total = str(sum(int(q) for q in quarter_scores if q not in ("-", "")))
            team_rows.append({"abbr": abbr, "quarters": quarter_scores, "total": total})

        if len(team_rows) != 2:
            continue

        # Determine if game is final (look for "Final" or "F (OT)" before the table)
        is_final = bool(re.search(r'\bFinal\b|F\s*\(OT\)', pre_table, re.IGNORECASE))
        is_ot = bool(re.search(r'F\s*\(OT\)', pre_table, re.IGNORECASE))

        # Live cards mark status "Live" and show the current quarter + clock,
        # e.g. <span class="date">4th 15:00</span>. Halftime reads "Half".
        is_live = (not is_final) and bool(
            re.search(r'<span class="status">\s*Live\s*</span>', pre_table, re.IGNORECASE))
        current_period = None
        if is_live:
            q_m = re.search(r'<span class="date">\s*(\d)(?:st|nd|rd|th)\b', pre_table,
                            re.IGNORECASE)
            if q_m:
                current_period = int(q_m.group(1))
            elif re.search(r'<span class="date">\s*Half\b', pre_table, re.IGNORECASE):
                current_period = 2  # halftime: Q1+Q2 done, Q3 not started
            elif len(team_rows[0]["quarters"]) > 4:
                # Live with an extra column = overtime; regulation is over. Read
                # from the table rather than guessing at unobserved OT markup.
                current_period = 5
            # Still None (unrecognized clock text) → treated as period 0, so
            # nothing early-grades. Failing closed is the safe direction here.

        away = team_rows[0]
        home = team_rows[1]
        games.append({
            "date": game_date,
            "away_abbr": away["abbr"],
            "home_abbr": home["abbr"],
            "away_name": CFL_TEAMS.get(away["abbr"], away["abbr"]),
            "home_name": CFL_TEAMS.get(home["abbr"], home["abbr"]),
            "away_quarters": away["quarters"],
            "home_quarters": home["quarters"],
            "away_total": away["total"],
            "home_total": home["total"],
            "final": is_final,
            "ot": is_ot,
            "live": is_live,
            "current_period": current_period,
        })

    return games


def _format_cfl_line_scores(game: dict) -> str:
    """Format a CFL game's quarter scores like line_scores_text for football."""
    lines = []
    for side in ("away", "home"):
        name = game[f"{side}_name"]
        qs = game[f"{side}_quarters"]
        total = game[f"{side}_total"]
        # qs might be [Q1, Q2, Q3, Q4] or [Q1, Q2, Q3, Q4, OT, ...]
        # Filter out dashes (no OT)
        clean_qs = [q for q in qs if q not in ("-", "")]
        if len(clean_qs) >= 4:
            try:
                h1 = str(int(clean_qs[0]) + int(clean_qs[1]))
                h2 = str(int(clean_qs[2]) + int(clean_qs[3]))
            except ValueError:
                h1 = h2 = "?"
            ot = ""
            if len(clean_qs) > 4:
                ot = f" OT={'|'.join(clean_qs[4:])}"
            lines.append(f"{name}: Q1={clean_qs[0]} Q2={clean_qs[1]} H1={h1} | Q3={clean_qs[2]} Q4={clean_qs[3]} H2={h2}{ot} | Final={total}")
        else:
            lines.append(f"{name}: Final={total}")
    status = "F (OT)" if game.get("ot") else "Final"
    header = f"{game['away_name']} {game['away_total']} at {game['home_name']} {game['home_total']} [{status}]"
    return header + "\n" + "\n".join(lines)


def _cfl_event(game: dict, event_id: str = "cfl") -> dict:
    """One parsed cfl.ca game as an ESPN-shaped event."""
    def competitor(side: str) -> dict:
        linescores = []
        for q in game[f"{side}_quarters"]:
            try:
                linescores.append({"value": float(q), "displayValue": str(q)})
            except (TypeError, ValueError):
                break  # "-" placeholder: no further quarters are real
        return {
            "homeAway": side,
            "team": {"displayName": game[f"{side}_name"]},
            "score": game[f"{side}_total"],
            "linescores": linescores,
        }

    # `live` and `final` are independent flags, and a game that has not kicked
    # off is neither. Mapping "not live" straight to "post" (as this used to)
    # would present an unplayed game as a finished 0-0 one — harmless while the
    # only consumer was build_early_context, which ignores state, but the
    # arithmetic path reads it and would grade the game before it happened.
    if game.get("live"):
        state, completed = "in", False
    elif game.get("final"):
        state, completed = "post", True
    else:
        state, completed = "pre", False

    return {
        "id": event_id,
        "status": {
            "period": game.get("current_period") or 0,
            "type": {"state": state, "completed": completed},
        },
        "competitions": [{"competitors": [competitor("away"), competitor("home")]}],
    }


def _cfl_scoreboard(game: dict) -> dict:
    """Wrap a single parsed cfl.ca game in an ESPN-shaped scoreboard.

    Lets CFL reuse the shared early-grading helpers, which are written against
    ESPN's schema. Without this CFL has no early grading at all: ESPN serves
    zero CFL events, so `build_early_context` is handed an empty scoreboard and
    always returns None.
    """
    return {"events": [_cfl_event(game)]}


# The schedule page is one request covering every game, so cache the parse and
# share it between the context path and the arithmetic path instead of
# scraping cfl.ca once per pick.
_CFL_GAMES_TTL = 60
# Failures are cached too, briefly. The daemon calls this per unresolved CFL
# pick every 10s, so an un-cached failure would turn a cfl.ca outage into one
# request per pick per cycle aimed at a site that is already struggling.
_CFL_GAMES_FAIL_TTL = 15
_cfl_games_cache: tuple[float, list[dict]] | None = None


async def _fetch_cfl_games() -> list[dict]:
    """Parsed cfl.ca schedule, cached briefly. Returns [] on any fetch failure."""
    global _cfl_games_cache
    if _cfl_games_cache:
        age = time.monotonic() - _cfl_games_cache[0]
        ttl = _CFL_GAMES_TTL if _cfl_games_cache[1] else _CFL_GAMES_FAIL_TTL
        if age < ttl:
            return _cfl_games_cache[1]
    async with httpx.AsyncClient(timeout=15, follow_redirects=True) as http:
        try:
            r = await http.get("https://www.cfl.ca/schedule/")
            r.raise_for_status()
        except Exception as exc:
            print(f"    [CFL error] schedule fetch: {exc}")
            _cfl_games_cache = (time.monotonic(), [])
            return []
    games = _parse_cfl_schedule(r.text)
    _cfl_games_cache = (time.monotonic(), games)
    return games


async def fetch_cfl_scoreboard(date: str) -> dict:
    """CFL games near `date` as an ESPN-shaped scoreboard, for the math path.

    Deliberately NOT wired into `fetch_espn`: `validate_sport` keys off CFL's
    ESPN scoreboard being empty to stop CFL teams fuzzy-matching other sports
    ("Blue Bombers" -> MLB "Blue Jays"), so populating it globally would change
    sport validation as a side effect. This feeds grading only.

    Each event gets a unique id — `find_event_ids` returns ids and the caller
    resolves the first match, so a shared id would silently bind every pick to
    whichever game happened to be first.
    """
    games = await _fetch_cfl_games()
    if not games:
        return {"events": []}
    try:
        target = _date.fromisoformat(date)
    except ValueError:
        return {"events": []}

    events = []
    for i, g in enumerate(games):
        try:                                  # ±1 day for timezone differences
            if abs((_date.fromisoformat(g["date"]) - target).days) > 1:
                continue
        except (ValueError, KeyError):
            continue
        events.append(_cfl_event(g, event_id=f"cfl-{g['date']}-{i}"))
    return {"events": events}


async def fetch_cfl_context(
    team: str, date: str, *, odds_game_date: str | None = None,
    period: str = "game",
) -> tuple[str, str]:
    """Grade a CFL pick by scraping CFL.ca schedule for quarter scores.

    ESPN has no CFL data, so we scrape the official site instead.
    A period bet (1Q/1H/...) whose period has already finished is graded from
    the live card while the game is still in progress; everything else waits
    for the final. Returns (context_str, game_date).
    """
    games = await _fetch_cfl_games()
    if not games:
        return "", date
    team_lower = team.lower().strip()

    # Try odds_game_date first, then pick date — odds API sometimes returns
    # the NEXT game date instead of the played game date.
    search_dates = []
    if odds_game_date:
        search_dates.append(odds_game_date)
    if date not in search_dates:
        search_dates.append(date)

    for target_date in search_dates:
        target = _date.fromisoformat(target_date)
        for g in games:
            if not (_team_matches(team_lower, g["away_name"].lower())
                    or _team_matches(team_lower, g["home_name"].lower())
                    or _team_matches(team_lower, g["away_abbr"].lower())
                    or _team_matches(team_lower, g["home_abbr"].lower())):
                continue
            # Match date (±1 day to handle timezone differences)
            try:
                gd = _date.fromisoformat(g["date"])
                if abs((gd - target).days) > 1:
                    continue
            except ValueError:
                continue
            if g["final"]:
                return _format_cfl_line_scores(g), g["date"]
            # Game still running: a period bet can settle as soon as ITS period
            # is over. Teams are omitted from the probe pick on purpose — this
            # game is already the match, and the synthetic scoreboard holds only
            # it, so re-matching by name could only fail.
            if period != "game" and g.get("live"):
                early = build_early_context(
                    "CFL", {"period": period, "teams": [], "player": ""},
                    _cfl_scoreboard(g),
                )
                if early:
                    return early, g["date"]
            return "PENDING", g["date"]

    return "", date


def odds_api_context(fighter: str, events: list[dict]) -> str:
    """Format Odds API event data for a specific fighter."""
    fighter_lower = fighter.lower().strip()
    for e in events:
        home = e.get("home_team", "")
        away = e.get("away_team", "")
        if not (_team_matches(fighter_lower, home.lower()) or _team_matches(fighter_lower, away.lower())):
            continue
        scores = e.get("scores") or []
        score_str = "  ".join(f"{s['name']}: {s['score']}" for s in scores) if scores else "(no score data)"
        return f"{home} vs {away}\n{score_str}"
    return ""


async def fetch_soccer_context(
    teams: list[str], date: str, include_stats: bool = False,
) -> tuple[str, str]:
    """Search ESPN soccer leagues for score context.

    Returns (context_str, game_date).  context_str is "PENDING" if the game
    exists but isn't finished yet, or "" if not found at all.
    When include_stats is True, also fetches match summary for team stats
    (corners, shots, etc.).
    """
    if not teams:
        return "", date

    async def _fetch(http: httpx.AsyncClient, category: str, league: str, date_nodash: str) -> tuple[dict | None, str, str]:
        url = f"https://site.api.espn.com/apis/site/v2/sports/{category}/{league}/scoreboard"
        try:
            r = await http.get(url, params={"dates": date_nodash, "limit": "200"})
            r.raise_for_status()
            return r.json(), category, league
        except Exception:
            return None, category, league

    async with httpx.AsyncClient(timeout=10) as http:
        d = _date.fromisoformat(date)
        for search_date in [date, (d - timedelta(days=1)).isoformat(), (d + timedelta(days=1)).isoformat()]:
            date_nodash = search_date.replace("-", "")
            results = await asyncio.gather(
                *(_fetch(http, cat, lg, date_nodash) for cat, lg in SOCCER_LEAGUES)
            )
            for sb, category, league in results:
                if sb is None:
                    continue
                completed = _completed_events(sb)
                matched = find_event_ids(completed, teams)
                if matched:
                    display = {"events": [e for e in completed if e.get("id") in set(matched)]}
                    ctx = scoreboard_text(display, "Soccer")
                    if include_stats:
                        stats = await _fetch_soccer_stats(http, category, league, matched[0])
                        if stats:
                            ctx += "\n" + stats
                    return ctx, search_date
                if find_event_ids(sb.get("events", []), teams):
                    return "PENDING", search_date
    return "", date


async def _fetch_soccer_stats(http: httpx.AsyncClient, category: str, league: str, event_id: str) -> str:
    """Fetch team statistics from ESPN summary for a soccer match."""
    url = f"https://site.api.espn.com/apis/site/v2/sports/{category}/{league}/summary"
    try:
        r = await http.get(url, params={"event": event_id}, timeout=10)
        r.raise_for_status()
        data = r.json()
    except Exception:
        return ""
    box_teams = data.get("boxscore", {}).get("teams", [])
    if not box_teams:
        return ""
    lines = []
    for team_data in box_teams:
        team_name = team_data.get("team", {}).get("displayName", "?")
        stats = {s["name"]: s["displayValue"] for s in team_data.get("statistics", [])}
        parts = [f"{team_name}:"]
        for key, label in [("wonCorners", "Corners"), ("foulsCommitted", "Fouls"),
                           ("totalShots", "Shots"), ("shotsOnTarget", "On Target"),
                           ("offsides", "Offsides"), ("yellowCards", "Yellows")]:
            if key in stats:
                parts.append(f"{label} {stats[key]}")
        lines.append("  ".join(parts))
    return "Match Stats:\n" + "\n".join(lines)


async def _fetch_espn_scoreboard(sport: str, category: str, league: str, date: str) -> dict | None:
    url = f"https://site.api.espn.com/apis/site/v2/sports/{category}/{league}/scoreboard"
    params = {"dates": date.replace("-", ""), "limit": "200"}
    params.update(SPORT_EXTRA_PARAMS.get(sport, {}))
    async with httpx.AsyncClient(timeout=10) as http:
        try:
            r = await http.get(url, params=params)
            r.raise_for_status()
            return r.json()
        except Exception as e:
            print(f"    [ESPN error] {sport} {category}/{league} {date}: {e}")
            return None


async def fetch_espn(sport: str, date: str) -> dict | None:
    if sport not in ESPN_LEAGUES:
        return None
    category, league = ESPN_LEAGUES[sport]
    data = await _fetch_espn_scoreboard(sport, category, league, date)
    # Offseason fallback: when the primary scoreboard is empty, merge in events from
    # fallback leagues (e.g. NBA Summer League) so downstream matching is transparent.
    if (not data or not data.get("events")) and sport in ESPN_FALLBACK_LEAGUES:
        for cat, lg in ESPN_FALLBACK_LEAGUES[sport]:
            fb = await _fetch_espn_scoreboard(sport, cat, lg, date)
            if fb and fb.get("events"):
                if data is None:
                    data = fb
                else:
                    data.setdefault("events", []).extend(fb["events"])
    return data


async def fetch_tennis_match_context(player: str, date: str, CONTEXT_SKIP: str) -> str:
    """
    Search ESPN core API for a tennis match involving `player` on `date`.
    Tries exact date first; falls back to ±1 day only if no exact match found.
    Returns a formatted string with player names, set scores, and winner.
    Returns CONTEXT_SKIP if not found.
    """
    from datetime import date as _d
    player_lower = player.lower().strip()
    date_nodash = date.replace("-", "")
    pick_date_obj = _d.fromisoformat(date)

    async def _search(max_days: int) -> str | None:
        async with httpx.AsyncClient(timeout=20) as http:
            for league in ("atp", "wta"):
                try:
                    r = await http.get(
                        f"https://site.api.espn.com/apis/site/v2/sports/tennis/{league}/scoreboard",
                        params={"dates": date_nodash},
                    )
                    r.raise_for_status()
                except Exception:
                    continue

                events = r.json().get("events", [])
                for event in events:
                    event_id = event.get("id", "")
                    base = f"https://sports.core.api.espn.com/v2/sports/tennis/leagues/{league}/events/{event_id}"

                    page = 1
                    while True:
                        try:
                            r2 = await http.get(f"{base}/competitions", params={"pageSize": 100, "page": page})
                            r2.raise_for_status()
                        except Exception:
                            break
                        data = r2.json()

                        for comp in data.get("items", []):
                            try:
                                comp_date = _d.fromisoformat(comp.get("date", "")[:10])
                                if abs((comp_date - pick_date_obj).days) > max_days:
                                    continue
                            except ValueError:
                                continue
                            comp_id = comp.get("id", "")
                            competitors = comp.get("competitors", [])

                            if not any(_team_matches(player_lower, c.get("name", "").lower()) for c in competitors):
                                continue

                            # Found the match — fetch set scores
                            lines = [f"Tennis match on {comp_date.isoformat()} ({league.upper()}):"]
                            for c in competitors:
                                name = c.get("name", "?")
                                winner = c.get("winner", False)
                                athlete_id = c.get("id", "")
                                try:
                                    r3 = await http.get(f"{base}/competitions/{comp_id}/competitors/{athlete_id}/linescores")
                                    r3.raise_for_status()
                                    sets = r3.json().get("items", [])
                                    set_str = " ".join(f"S{s['period']}={s['displayValue']}" for s in sets)
                                except Exception:
                                    set_str = "(no set data)"
                                winner_flag = " [WINNER]" if winner else ""
                                lines.append(f"  {name}: {set_str}{winner_flag}")
                            return "\n".join(lines)

                        if page >= data.get("pageCount", 1):
                            break
                        page += 1
        return None

    # Exact date first, then ±1 day fallback
    result = await _search(max_days=0)
    if result is None:
        result = await _search(max_days=1)
    return result or CONTEXT_SKIP


async def fetch_espn_summary(sport: str, event_id: str) -> dict | None:
    if sport not in ESPN_LEAGUES:
        return None
    # Summary is league-sensitive: a Summer League event id 404s on the /nba endpoint,
    # so try the primary league first, then any fallback leagues (see ESPN_FALLBACK_LEAGUES).
    leagues = [ESPN_LEAGUES[sport]] + ESPN_FALLBACK_LEAGUES.get(sport, [])
    async with httpx.AsyncClient(timeout=15) as http:
        for category, league in leagues:
            url = f"https://site.api.espn.com/apis/site/v2/sports/{category}/{league}/summary"
            try:
                r = await http.get(url, params={"event": event_id})
                r.raise_for_status()
                return r.json()
            except Exception as e:
                print(f"    [ESPN summary error] {sport} {category}/{league}/{event_id}: {e}")
                continue
    return None


def scoreboard_text(data: dict, sport: str) -> str:
    """Format ESPN scoreboard as readable text for Claude."""
    lines = []
    for event in data.get("events", []):
        all_comps = event.get("competitions", [])

        if sport == "UFC":
            # Each competition is a separate bout — include ALL of them
            for comp in all_comps:
                fighters = []
                winner = None
                status = comp.get("status", {}).get("type", {}).get("description", "")
                completed = comp.get("status", {}).get("type", {}).get("completed", False)
                for c in comp.get("competitors", []):
                    name = c.get("athlete", {}).get("displayName", "?")
                    fighters.append(name)
                    if c.get("winner"):
                        winner = name
                if fighters:
                    if winner:
                        result_str = f"Winner: {winner}"
                    elif completed:
                        result_str = "Winner: DRAW"
                    else:
                        result_str = "Winner: ?"
                    lines.append(f"{' vs '.join(fighters)} → {result_str} [{status}]")
        else:
            comp = all_comps[0] if all_comps else {}
            by_side = {c["homeAway"]: c for c in comp.get("competitors", [])}
            away = by_side.get("away", {})
            home = by_side.get("home", {})
            away_name = away.get("team", {}).get("displayName", "?")
            home_name = home.get("team", {}).get("displayName", "?")
            away_score = away.get("score", "?")
            home_score = home.get("score", "?")
            status = event.get("status", {}).get("type", {}).get("description", "")
            line = f"{away_name} {away_score} at {home_name} {home_score} [{status}]"
            # Include penalty/advancement notes (e.g. "Morocco advance 3-2 on penalties")
            for note in comp.get("notes", []):
                note_text = note.get("headline") or note.get("text", "")
                if note_text:
                    line += f" — {note_text}"
                    break
            # Soccer AET/PEN: compute regulation-time score from goal details
            if sport == "Soccer":
                status_name = (
                    comp.get("status", {}).get("type", {}).get("name", "")
                    or event.get("status", {}).get("type", {}).get("name", "")
                )
                if "AET" in status_name or "PEN" in status_name:
                    home_id = home.get("id") or home.get("team", {}).get("id")
                    away_id = away.get("id") or away.get("team", {}).get("id")
                    reg_home = reg_away = 0
                    for detail in comp.get("details", []):
                        if not detail.get("scoringPlay") or detail.get("shootout"):
                            continue
                        dv = detail.get("clock", {}).get("displayValue", "")
                        m = re.match(r"(\d+)", dv)
                        if not m or int(m.group(1)) > 90:
                            continue  # extra-time goal
                        scored_by = str(detail.get("team", {}).get("id", ""))
                        if detail.get("ownGoal"):
                            if scored_by == str(home_id):
                                reg_away += 1
                            elif scored_by == str(away_id):
                                reg_home += 1
                        else:
                            if scored_by == str(home_id):
                                reg_home += 1
                            elif scored_by == str(away_id):
                                reg_away += 1
                    line += f" (Regulation 90': {away_name} {reg_away}, {home_name} {reg_home})"
            lines.append(line)

    return "\n".join(lines) or "No games found for this date"


def line_scores_text(summary: dict, sport: str = "") -> str:
    """Format per-quarter/half scores from a game summary."""
    header = summary.get("header", {})
    comps = header.get("competitions", [{}])[0]
    lines = []

    is_baseball = sport in ("MLB", "KBO")

    for c in comps.get("competitors", []):
        team = c.get("team", {}).get("displayName", "?")
        ls = [x.get("displayValue", "?") for x in c.get("linescores", [])]
        final = c.get("score", "?")

        if is_baseball and len(ls) >= 5:
            # Baseball: show each inning + H1 (innings 1-5) and H2 (innings 6-9)
            try:
                h1 = str(sum(int(ls[i]) for i in range(5)))
            except ValueError:
                h1 = "?"
            try:
                h2 = str(sum(int(ls[i]) for i in range(5, len(ls))))
            except ValueError:
                h2 = "?"
            innings = ' '.join(f"I{i+1}={s}" for i, s in enumerate(ls))
            lines.append(f"{team}: {innings} | H1={h1} H2={h2} | Final={final}")
        elif len(ls) >= 4:
            # Basketball: Q1 Q2 Q3 Q4 [OT...]
            try:
                h1 = str(int(ls[0]) + int(ls[1]))
                h2 = str(int(ls[2]) + int(ls[3]))
            except ValueError:
                h1 = h2 = "?"
            ot = f" OT={'|'.join(ls[4:])}" if len(ls) > 4 else ""
            lines.append(f"{team}: Q1={ls[0]} Q2={ls[1]} H1={h1} | Q3={ls[2]} Q4={ls[3]} H2={h2}{ot} | Final={final}")
        elif len(ls) >= 2:
            lines.append(f"{team}: {' | '.join(f'P{i+1}={s}' for i, s in enumerate(ls))} | Final={final}")
        else:
            lines.append(f"{team}: Final={final}")

    return "\n".join(lines) or "No line score data available"


def box_score_text(summary: dict, player_hint: str = "",
                   require_all_words: bool = False) -> str:
    """Format player stats from a game box score.

    Reads EVERY stat group and emits every key=value stat generically: ESPN
    splits MLB boxes into batting/pitching, NFL into passing/rushing/…, NHL
    into forwards/defenses/goalies — a curated single-group read leaves whole
    position groups (all pitchers, everyone but QBs) invisible to the grader.
    The group name is appended when a team has several, so a two-way player's
    batting and pitching lines stay distinguishable.

    require_all_words: match a player only when ALL hint words appear in the
    name — for scanning games other than the pick's bound one, where a loose
    surname match could bind a different player.
    """
    lines = []
    boxscore = summary.get("boxscore", {})

    for team_data in boxscore.get("players", []):
        team_name = team_data.get("team", {}).get("displayName", "?")

        groups = team_data.get("statistics", [])
        multi_group = len(groups) > 1
        for stat_group in groups:
            keys = stat_group.get("keys", [])
            group_name = stat_group.get("type") or stat_group.get("name") or ""

            for athlete in stat_group.get("athletes", []):
                name = athlete.get("athlete", {}).get("displayName", "?")
                stats_raw = athlete.get("stats", [])
                if not stats_raw:  # DNP row
                    continue

                # If filtering to a specific player, skip non-matches
                if player_hint:
                    hint_words = [w for w in player_hint.lower().split() if len(w) > 2]
                    name_lower = name.lower()
                    matcher = all if require_all_words else any
                    if hint_words and not matcher(w in name_lower for w in hint_words):
                        continue

                pairs = [f"{k}={v}" for k, v in zip(keys, stats_raw)]
                if pairs:
                    label = f"{team_name}, {group_name}" if multi_group and group_name else team_name
                    lines.append(f"  {name} ({label}): {', '.join(pairs)}")

    return "\n".join(lines) or "No player stats found"


# Words that, when following a matched term, indicate it's a different longer team name.
# e.g., "Iowa" should not match "Iowa State Cyclones" because "State" follows "Iowa".
_QUALIFIERS = {"state", "tech", "a&m", "am", "international", "st"}  # "st" = abbrev for State/Saint disambiguation

# Common abbreviations / short names → canonical long form used by the Odds API.
# Both keys and values must be **lowercase**.
_TEAM_ALIASES: dict[str, str] = {
    # ── General city abbreviations ────────────────────────────────────
    "okc":           "oklahoma city",
    "ny":            "new york",
    "la":            "los angeles",
    "nj":            "new jersey",
    "sa":            "san antonio",
    "sj":            "san jose",
    "gs":            "golden state",
    "gsw":           "golden state",
    "gb":            "green bay",
    "kc":            "kansas city",
    "tb":            "tampa bay",
    "ne":            "new england",
    "sf":            "san francisco",
    "nola":          "new orleans",
    "philly":        "philadelphia",
    # ── NBA ───────────────────────────────────────────────────────────
    "okc thunder":   "oklahoma city thunder",
    "gs warriors":   "golden state warriors",
    "gsw warriors":  "golden state warriors",
    "sa spurs":      "san antonio spurs",
    "la lakers":     "los angeles lakers",
    "la clippers":   "la clippers",         # ESPN uses "LA Clippers", not "Los Angeles"
    "los angeles clippers": "la clippers",
    "ny knicks":     "new york knicks",
    "nola pelicans": "new orleans pelicans",
    "philly sixers": "philadelphia 76ers",
    "philly 76ers":  "philadelphia 76ers",
    # ── NFL ───────────────────────────────────────────────────────────
    "ny jets":       "new york jets",
    "ny giants":     "new york giants",
    "la rams":       "los angeles rams",
    "la chargers":   "los angeles chargers",
    "sf 49ers":      "san francisco 49ers",
    "sf niners":     "san francisco 49ers",
    "gb packers":    "green bay packers",
    "kc chiefs":     "kansas city chiefs",
    "ne patriots":   "new england patriots",
    "ne pats":       "new england patriots",
    "tb bucs":       "tampa bay buccaneers",
    "jax jaguars":   "jacksonville jaguars",
    "jax jags":      "jacksonville jaguars",
    # ── NHL ───────────────────────────────────────────────────────────
    "la kings":      "los angeles kings",
    "nj devils":     "new jersey devils",
    "ny rangers":    "new york rangers",
    "ny islanders":  "new york islanders",
    "sj sharks":     "san jose sharks",
    "tb lightning":  "tampa bay lightning",
    # ── MLB ───────────────────────────────────────────────────────────
    "ny mets":       "new york mets",
    "ny yankees":    "new york yankees",
    "la dodgers":    "los angeles dodgers",
    "la angels":     "los angeles angels",
    "sf giants":     "san francisco giants",
    "kc royals":     "kansas city royals",
    "tb rays":       "tampa bay rays",
    # ── NCAAF / NCAAB ────────────────────────────────────────────────
    "miami redhawks":      "miami (oh) redhawks",
    "miami ohio":          "miami (oh)",
    "miami ohio redhawks": "miami (oh) redhawks",
    "seattle redhawks":    "seattle u redhawks",
    "seattle":             "seattle u",
    "american eagles":     "american university eagles",
    "american":            "american university",
    "umkc":                "kansas city",
    "umkc roos":           "kansas city roos",
    "fiu":                 "florida international",
    "fiu panthers":        "florida international panthers",
    "umass":               "massachusetts",
    "umass minutemen":     "massachusetts minutemen",
    "cal baptist":         "california baptist",
    "cal baptist lancers": "california baptist lancers",
    "st bonaventure":      "st. bonaventure",
    "st bonaventure bonnies": "st. bonaventure bonnies",
    "george washington colonials": "george washington revolutionaries",
    "grand canyon antelopes": "grand canyon lopes",
    "mel costa":             "melquizael costa",
    # ── UFL ───────────────────────────────────────────────────────────
    "arlington renegades": "dallas renegades",  # rebranded 2025
    # ── Soccer (German/English city name variants) ────────────────────
    "koln":              "cologne",
    "fc koln":           "fc cologne",
    "1. fc koln":        "fc cologne",
    "gladbach":          "monchengladbach",
    "borussia monchengladbach": "monchengladbach",
    "turkey":            "turkiye",  # ESPN uses official "Türkiye"
    # ── Soccer (FIFA World Cup national team aliases) ────────────────
    "bosnia and herzegovina": "bosnia",
    "bosnia-herzegovina":     "bosnia",
    "holland":           "netherlands",
    "czech republic":    "czechia",
    "usa":               "united states",
    "us":                "united states",
    "usmnt":             "united states",
    "korea republic":    "south korea",
    "cabo verde":        "cape verde",
    "drc":               "congo dr",
    "cote d ivoire":     "ivory coast",
}


def _strip_accents(s: str) -> str:
    import unicodedata
    return "".join(
        c for c in unicodedata.normalize("NFD", s) if unicodedata.category(c) != "Mn"
    )


def _team_matches(term: str, team_name: str) -> bool:
    """Return True if term matches team_name, avoiding ambiguous prefix matches.

    'Iowa' matches 'Iowa Hawkeyes' but NOT 'Iowa State Cyclones'
    'Texas' matches 'Texas Longhorns' but NOT 'Texas Tech Red Raiders'
    """
    t = _strip_accents(term.lower().strip()).replace("&", "and").replace("-", " ")
    n = _strip_accents(team_name.lower().strip()).replace("&", "and").replace("-", " ")
    # Expand common abbreviations before matching
    t = _TEAM_ALIASES.get(t, t)
    n = _TEAM_ALIASES.get(n, n)
    if not t or not n:
        return False
    t_words = t.split()
    n_words = n.split()
    # Allow name-order swaps (e.g. "Pat Guilherme" ↔ "Guilherme Pat") —
    # common in UFC/Boxing where Odds API may reverse first/last names.
    if t not in n and n not in t:
        if sorted(t_words) != sorted(n_words):
            return False
    for i in range(len(n_words) - len(t_words) + 1):
        if n_words[i: i + len(t_words)] == t_words:
            next_idx = i + len(t_words)
            if next_idx < len(n_words) and n_words[next_idx] in _QUALIFIERS:
                return False  # e.g., "Iowa" before "State" → skip
            return True
    # No contiguous word-sequence match. Only accept when the API name is a
    # subset/reordering of the pick term — the pick carries extra qualifiers
    # ("Inter Miami CF" ⊇ "Inter Miami") or the two are word-order swaps
    # ("Guilherme Pat" ↔ "Pat Guilherme"). Reject a bare substring buried inside
    # a single word (e.g. "Ko" inside "Balko"/"Guskov"/"Shevchenko"), a false
    # positive that made short fighter/team tokens hijack the wrong event.
    return n in t or sorted(t_words) == sorted(n_words)


def find_event_ids(events: list[dict], teams: list[str], player: str = "") -> list[str]:
    """Find event IDs that match the given team names or player."""
    matched = []
    search_terms = [t.lower() for t in teams if t] + ([player.lower()] if player else [])
    if not search_terms:
        return [e.get("id") for e in events if e.get("id")]

    for event in events:
        # Check ALL competitions — UFC events have many bouts, each a separate competition
        all_comps = event.get("competitions", [{}])
        event_names = []
        for comp in all_comps:
            for c in comp.get("competitors", []):
                n = (
                    c.get("team", {}).get("displayName", "")
                    or c.get("athlete", {}).get("displayName", "")
                ).lower()
                event_names.append(n)

        if any(
            any(_team_matches(term, en) for en in event_names)
            for term in search_terms
        ):
            if event.get("id"):
                matched.append(event["id"])

    return matched


def _completed_events(data: dict) -> list[dict]:
    """Return only completed events from a scoreboard response."""
    return [
        e for e in data.get("events", [])
        if e.get("status", {}).get("type", {}).get("completed", False)
    ]


def _ufc_bout_completed(data: dict, teams: list[str], player: str = "") -> bool:
    """Return True if the specific UFC bout (identified by fighter names) is
    marked Final/completed at the competition level, even if the overall
    event is still In Progress (other bouts on the card are ongoing)."""
    search_terms = [t.lower() for t in teams if t] + ([player.lower()] if player else [])
    if not search_terms:
        return False
    for event in data.get("events", []):
        for comp in event.get("competitions", []):
            if not comp.get("status", {}).get("type", {}).get("completed", False):
                continue
            comp_names = [
                c.get("athlete", {}).get("displayName", "").lower()
                for c in comp.get("competitors", [])
            ]
            if any(
                any(_team_matches(term, cn) for cn in comp_names)
                for term in search_terms
            ):
                return True
    return False


# ─── Early grading (mid-game) ────────────────────────────────────────────────

# Maps (sport, period_code) → list of 1-based ESPN period indices.
_QUARTER_PERIODS = {"1h": [1, 2], "2h": [3, 4], "1q": [1], "2q": [2], "3q": [3], "4q": [4]}
# "1q" in baseball is the 1st inning — the parser's closest enum for NRFI/YRFI
# (total 0.5 on inning 1). Unmapped, an NRFI sat PENDING until the game went
# final ~3h on, when its period was frozen 20 minutes in.
_BASEBALL_PERIODS = {"1h": [1, 2, 3, 4, 5], "2h": [6, 7, 8, 9], "1q": [1]}

# Every quarter-based sport in ESPN_LEAGUES belongs here. WNBA and Lacrosse were
# missing, which silently disabled ALL period math for them — `_extract_period_scores`
# returns None on an unmapped (sport, period), so a WNBA 1H total never reached the
# arithmetic below and fell through to Claude, which graded a bet_type=total as one
# team's half score. Adding a sport means checking this map, not just ESPN_LEAGUES.
PERIOD_MAP: dict[tuple[str, str], list[int]] = {
    **{(s, p): v for s in ("NBA", "WNBA", "NCAAB", "NFL", "NCAAF", "UFL", "CFL", "Lacrosse")
       for p, v in _QUARTER_PERIODS.items()},
    **{(s, p): v for s in ("MLB", "KBO") for p, v in _BASEBALL_PERIODS.items()},
    ("NHL", "1h"): [1],
    ("NHL", "1p"): [1], ("NHL", "2p"): [2], ("NHL", "3p"): [3],
}


# Sports whose scoreboard is a two-competitor scoreline that can be added up.
# An ALLOWLIST, not a denylist: anything unlisted falls through to Claude, which
# is the safe direction. UFC is the reason it must be explicit — an event holds
# many bouts and `_extract_period_scores` reads competitions[0], so a round total
# would "add" two unrelated bout scores and grade to a confident, wrong verdict.
# Soccer is out for the extra-time rule (see _GRADE_PROMPT). CFL/KBO are scraped
# rather than fetched from ESPN, so they decline on empty data anyway.
MATH_GRADABLE_SPORTS = frozenset({
    "MLB", "NBA", "WNBA", "NCAAB", "NCAAF", "NFL", "UFL", "NHL", "Lacrosse", "CFL",
})


def _find_event_for_pick(
    scoreboard: dict, teams: list[str], player: str = "",
) -> dict | None:
    """Find the event matching teams/player in scoreboard data (any state)."""
    events = scoreboard.get("events", [])
    ids = find_event_ids(events, teams, player)
    if not ids:
        return None
    target_id = ids[0]
    for e in events:
        if e.get("id") == target_id:
            return e
    return None


def _extract_period_scores(
    event: dict, sport: str, period: str,
) -> tuple[str, float, str, float] | None:
    """Extract (away_name, away_score, home_name, home_score) for the given period.

    For period='game', uses the competitor total score.
    For specific periods, sums the relevant linescores.
    Returns None if data is insufficient.
    """
    comp = event.get("competitions", [{}])[0]
    by_side = {c.get("homeAway"): c for c in comp.get("competitors", [])}
    away = by_side.get("away", {})
    home = by_side.get("home", {})
    away_name = away.get("team", {}).get("displayName", "")
    home_name = home.get("team", {}).get("displayName", "")

    if period == "game":
        try:
            return (away_name, float(away.get("score", 0)),
                    home_name, float(home.get("score", 0)))
        except (ValueError, TypeError):
            return None

    period_indices = PERIOD_MAP.get((sport, period))
    if not period_indices:
        return None

    away_ls = away.get("linescores", [])
    home_ls = home.get("linescores", [])
    # Need data for all requested periods (1-based → 0-based index)
    max_needed = max(period_indices)
    if len(away_ls) < max_needed or len(home_ls) < max_needed:
        return None

    try:
        away_score = sum(float(away_ls[p - 1].get("value", 0)) for p in period_indices)
        home_score = sum(float(home_ls[p - 1].get("value", 0)) for p in period_indices)
        return (away_name, away_score, home_name, home_score)
    except (ValueError, TypeError):
        return None


def _is_period_complete(event: dict, sport: str, period: str) -> bool:
    """Return True if all periods in the bet's range are already finished."""
    period_indices = PERIOD_MAP.get((sport, period))
    if not period_indices:
        return False
    current_period = event.get("status", {}).get("period", 0)
    return current_period > max(period_indices)


def try_early_grade_math(
    sport: str, pick: dict, scoreboard: dict | None,
) -> tuple[str, str] | None:
    """Grade a total/team_total/spread/moneyline pick by arithmetic on the box score.

    These four are all comparisons against numbers the scoreboard already gives us,
    so when the numbers are present there is nothing for a model to decide. Which
    states qualify depends on whether the quantity can still move against the bet:

    * **totals, mid-game** — only once the value has passed the line, i.e. an over
      that can no longer lose. A running total is monotone, so this stays true.
    * **totals, final** — settles outright, including PUSH and the under side.
    * **spread / moneyline** — final only. A lead is *not* monotone; settling those
      early would call a game that can still be lost.
    * **period bets, once the period is complete** — settle outright even while
      the game runs: a finished F5/1H/1Q scoreline is frozen, so it is exactly
      as final as a finished game. Before this, a live-game period bet fell to
      Claude via build_early_context, which sampled PUSH on a +0.5 F5 tie in
      one channel and WIN on the same numbers in its sibling — and a
      fractional spread can never push.

    Grading settled bets was left entirely to Claude, and it got two wrong. A total:
    "Dallas Wings 1H under 79.5" is `bet_type=total` (the team name identifies the
    *game* — see the Drake example in `_GRADE_PROMPT`) but was graded as Dallas's own
    first half, 36, instead of the game's combined 80, turning a half-point loss into
    a win. And a spread: a -1 run line won by exactly 1 run is a PUSH, graded LOSS.
    Neither is catchable downstream — a wrong verdict prices, grades and broadcasts
    exactly like a right one.

    Returns (verdict, calc) or None to fall through to the normal context+Claude
    path — every guard here fails open, so anything unusual is still graded.
    """
    if not scoreboard:
        return None

    bet_type = pick.get("bet_type", "")
    if bet_type not in ("total", "team_total", "spread", "moneyline"):
        return None

    if sport not in MATH_GRADABLE_SPORTS:
        return None

    teams = pick.get("teams", [])
    player = pick.get("player", "")
    period = pick.get("period", "game")
    direction = pick.get("direction")
    line = pick.get("line")

    if bet_type in ("total", "team_total"):
        if line is None or direction not in ("over", "under"):
            return None
    else:
        # Side bets need a team to attribute the result to, and a spread needs its
        # number. Anything settled by a rule other than the scoreline is refused:
        desc = pick.get("description", "") or ""
        if not teams:
            return None
        if bet_type == "spread" and line is None:
            return None
        # "to advance/qualify" is decided by the tie/series, not this game's score.
        if re.search(r"\bto\s+(advance|qualify)\b", desc, re.IGNORECASE):
            return None
        # Regulation ML must win in regulation — the final score includes OT, so
        # this arithmetic would call an OT win a WIN when it is a LOSS.
        if bet_type == "moneyline" and is_regulation_ml(desc):
            return None

    event = _find_event_for_pick(scoreboard, teams, player)
    if not event:
        return None

    stype = (event.get("status") or {}).get("type") or {}
    state = stype.get("state", "")
    if state not in ("in", "post"):
        return None
    final = state == "post"
    # "post" also covers postponed/suspended/cancelled — those carry no result.
    if final and not stype.get("completed"):
        return None
    # A completed period of a live game is as settled as a final: once the
    # 6th inning / 3rd quarter has started, the F5/1H scoreline can never
    # move again. `_is_period_complete` returns False for period="game" and
    # for anything unmapped, so this only widens the settled set for period
    # bets whose innings/quarters are all in the books.
    settled = final or _is_period_complete(event, sport, period)
    scores = _extract_period_scores(event, sport, period)
    if scores is None:
        return None

    away_name, away_score, home_name, home_score = scores
    period_tag = f" {period.upper()}" if period != "game" else ""
    when = "[final]" if final else f"[{period.upper()} complete]"

    if bet_type in ("spread", "moneyline"):
        # A lead can still be lost, so these settle only once it cannot:
        # game final, or the bet's period complete.
        if not settled:
            return None
        team_name = teams[0] if teams else ""
        if _team_matches(team_name.lower(), away_name.lower()):
            mine, theirs, name = away_score, home_score, away_name
        elif _team_matches(team_name.lower(), home_name.lower()):
            mine, theirs, name = home_score, away_score, home_name
        else:
            return None
        spread = float(line) if bet_type == "spread" else 0.0
        margin = mine + spread - theirs
        shown = f"{name} {mine:g}{spread:+g} vs {theirs:g}" if bet_type == "spread" \
            else f"{name} {mine:g} vs {theirs:g}"
        if margin == 0:
            # A whole-number spread landing exactly on the margin refunds, and so
            # does a tied side bet. This is the case the -1 run line got wrong.
            return ("PUSH", f"{when} {shown}{period_tag} — exact, push")
        return ("WIN" if margin > 0 else "LOSS",
                f"{when} {shown}{period_tag} -> {margin:+g}")

    if bet_type == "total":
        # A total names the GAME even when the pick text names one team, so both
        # sides are always added — this is the sum Claude got wrong.
        value = away_score + home_score
        label = f"{away_score:g}+{home_score:g}={value:g}"
    else:  # team_total — only the named team's score
        team_name = teams[0] if teams else ""
        if _team_matches(team_name.lower(), away_name.lower()):
            value, label = away_score, f"{away_name} {away_score:g}"
        elif _team_matches(team_name.lower(), home_name.lower()):
            value, label = home_score, f"{home_name} {home_score:g}"
        else:
            return None

    if settled:
        if value == line:
            return ("PUSH", f"{when} {label} vs {line}{period_tag} — exact, push")
        hit = (value > line) if direction == "over" else (value < line)
        return ("WIN" if hit else "LOSS",
                f"{when} {label} vs {line}{period_tag}")

    # Mid-game: only a value already past the line is decided.
    if value > line:
        calc = f"[mid-game] {label} vs {line}{period_tag}"
        return ("WIN", calc) if direction == "over" else ("LOSS", calc)

    return None


def build_early_context(
    sport: str, pick: dict, scoreboard: dict | None,
) -> str | None:
    """For period bets where the period is complete but the game is still going,
    format scores as context for Claude grading.

    Math-gradable bet types settle in try_early_grade_math before reaching
    here; this remains for what arithmetic refuses — non-math sports (soccer
    periods), 3-way/regulation rules, props parsed with a period.
    Returns context string or None.
    """
    if not scoreboard:
        return None

    period = pick.get("period", "game")
    if period == "game":
        return None

    teams = pick.get("teams", [])
    player = pick.get("player", "")

    event = _find_event_for_pick(scoreboard, teams, player)
    if not event:
        return None

    state = event.get("status", {}).get("type", {}).get("state", "")
    if state != "in":
        return None

    if not _is_period_complete(event, sport, period):
        return None

    period_indices = PERIOD_MAP.get((sport, period))
    if not period_indices:
        return None

    comp = event.get("competitions", [{}])[0]
    by_side = {c.get("homeAway"): c for c in comp.get("competitors", [])}
    period_label = period.upper()

    lines = [f"[{period_label} final — game still in progress]"]
    for side in ("away", "home"):
        c = by_side.get(side, {})
        name = c.get("team", {}).get("displayName", "?")
        ls = c.get("linescores", [])
        max_needed = max(period_indices)
        if len(ls) < max_needed:
            return None
        parts = [f"P{p}={ls[p - 1].get('displayValue', '?')}" for p in period_indices]
        try:
            total = sum(float(ls[p - 1].get("value", 0)) for p in period_indices)
        except (ValueError, TypeError):
            return None
        lines.append(f"{name}: {' '.join(parts)} | {period_label}={total:g}")

    return "\n".join(lines)


def extract_espn_bookmaker(competition: dict) -> dict | None:
    """Convert ESPN competition.odds[0] into a single Odds-API-style bookmaker dict.

    Returns None if no odds data is present (completed games have empty odds).
    Only covers main spread, total, and moneyline — no alternate lines.
    """
    import re as _re
    odds_list = competition.get("odds", [])
    if not odds_list:
        return None
    o = odds_list[0]

    home_name = o.get("homeTeamOdds", {}).get("team", {}).get("displayName", "")
    away_name = o.get("awayTeamOdds", {}).get("team", {}).get("displayName", "")
    if not home_name or not away_name:
        # Fall back to competitors list
        for c in competition.get("competitors", []):
            name = c.get("team", {}).get("displayName", "")
            if c.get("homeAway") == "home":
                home_name = home_name or name
            else:
                away_name = away_name or name

    markets: list[dict] = []

    # Moneyline
    ml = o.get("moneyline", {})
    home_ml = ml.get("home", {}).get("close", {}).get("odds") or ml.get("home", {}).get("open", {}).get("odds")
    away_ml = ml.get("away", {}).get("close", {}).get("odds") or ml.get("away", {}).get("open", {}).get("odds")
    if home_ml and away_ml and home_name and away_name:
        try:
            markets.append({"key": "h2h", "outcomes": [
                {"name": home_name, "price": int(home_ml)},
                {"name": away_name, "price": int(away_ml)},
            ]})
        except (ValueError, TypeError):
            pass

    # Spread
    ps = o.get("pointSpread", {})
    home_line = ps.get("home", {}).get("close", {}).get("line") or ps.get("home", {}).get("open", {}).get("line")
    home_odds = ps.get("home", {}).get("close", {}).get("odds") or ps.get("home", {}).get("open", {}).get("odds")
    away_line = ps.get("away", {}).get("close", {}).get("line") or ps.get("away", {}).get("open", {}).get("odds")
    away_odds = ps.get("away", {}).get("close", {}).get("odds") or ps.get("away", {}).get("open", {}).get("odds")
    # Also try top-level spread field (abs value) + details string for sign
    if not home_line:
        spread_abs = o.get("spread")
        details = o.get("details", "")       # e.g. "ILL -6.5" — favorite listed first
        fav_abbr = details.split()[0] if details else ""
        if spread_abs is not None and home_name and away_name:
            # Determine which team is the favourite from details abbreviation
            home_is_fav = fav_abbr and home_name.upper().startswith(fav_abbr.upper())
            if home_is_fav:
                home_line, away_line = f"-{spread_abs}", f"+{spread_abs}"
            else:
                home_line, away_line = f"+{spread_abs}", f"-{spread_abs}"
            home_odds = away_odds = "-110"   # ESPN doesn't expose vig at this level

    if home_line and home_odds and away_line and away_odds and home_name and away_name:
        try:
            markets.append({"key": "spreads", "outcomes": [
                {"name": home_name, "point": float(home_line), "price": int(home_odds)},
                {"name": away_name, "point": float(away_line), "price": int(away_odds)},
            ]})
        except (ValueError, TypeError):
            pass

    # Total
    tot = o.get("total", {})
    over_line  = tot.get("over",  {}).get("close", {}).get("line")  or tot.get("over",  {}).get("open", {}).get("line")
    over_odds  = tot.get("over",  {}).get("close", {}).get("odds")  or tot.get("over",  {}).get("open", {}).get("odds")
    under_line = tot.get("under", {}).get("close", {}).get("line")  or tot.get("under", {}).get("open", {}).get("line")
    under_odds = tot.get("under", {}).get("close", {}).get("odds")  or tot.get("under", {}).get("open", {}).get("odds")
    # Fallback: top-level overUnder field
    if not over_line:
        ou = o.get("overUnder")
        if ou is not None:
            over_line = under_line = str(ou)
            over_odds = under_odds = "-110"
    if over_line and over_odds:
        try:
            # Strip leading o/u prefix  ("o221.5" → 221.5)
            ov = float(_re.sub(r'^[a-zA-Z]+', '', over_line))
            uv = float(_re.sub(r'^[a-zA-Z]+', '', under_line)) if under_line else ov
            markets.append({"key": "totals", "outcomes": [
                {"name": "Over",  "point": ov, "price": int(over_odds)},
                {"name": "Under", "point": uv, "price": int(under_odds) if under_odds else int(over_odds)},
            ]})
        except (ValueError, TypeError):
            pass

    if not markets:
        return None
    return {"key": "espn_draftkings", "markets": markets}


def espn_bookmakers_for_teams(espn_data: dict, teams: list[str]) -> list[dict]:
    """Find the event matching teams in ESPN scoreboard data and return its bookmaker list.

    Returns [] if no event found or no odds available.
    Only works pre-game (ESPN clears odds once games are completed).
    """
    if not espn_data or not teams:
        return []
    for event in espn_data.get("events", []):
        for comp in event.get("competitions", []):
            comp_names = [
                c.get("team", {}).get("displayName", "").lower()
                for c in comp.get("competitors", [])
            ]
            if any(_team_matches(t.lower(), cn) for t in teams for cn in comp_names):
                bk = extract_espn_bookmaker(comp)
                return [bk] if bk else []
    return []


# ─── ESPN sport validation ───────────────────────────────────────────────────

# Team name fragments that exist in multiple ESPN sports
_AMBIGUOUS_SPORTS = {
    "rangers":   ["MLB", "NHL"],
    "cardinals": ["MLB", "NFL"],
    "giants":    ["MLB", "NFL"],
    "kings":     ["NBA", "NHL"],
    "blues":     ["NHL", "MLB"],
    "panthers":  ["NHL", "NFL"],
    "jets":      ["NHL", "NFL"],
}


# ─── Cross-league nickname collisions ────────────────────────────────────────
#
# _AMBIGUOUS_SPORTS above keys on fragments of the PARSED team name, which only
# works when the shared word survives into the canonical name ("rangers" is in
# both "Texas Rangers" and "New York Rangers"). Some nicknames leave no shared
# fragment at all: "Snakes" is the Arizona Diamondbacks in MLB and the Maryland
# Whipsnakes in the PLL. The ambiguity exists ONLY in the raw message and is
# erased the instant Claude commits to a canonical name — after which nothing
# downstream can tell a correct resolution from a wrong one, because both find
# a real game, a real closing line, and grade cleanly to OPPOSITE verdicts.
#
# So this map is keyed on the RAW MESSAGE TOKEN and resolved against the
# schedule: whichever candidate actually has a game that day wins. Prompt rules
# can only ever pin the nickname to one league; the schedule is evidence.
_NICKNAME_COLLISIONS: dict[str, list[tuple[str, str]]] = {
    "snakes": [("MLB", "Arizona Diamondbacks"), ("Lacrosse", "Maryland Whipsnakes")],
}

# Tiebreak window, used ONLY when more than one candidate has a game that day.
# A pick posted just before one game and most of a day before another is for the
# near one; anything less lopsided is escalated to a human instead of guessed.
_COLLISION_NEAR_HOURS = 2.0
_COLLISION_FAR_HOURS  = 6.0


def _event_start(evt: dict) -> _datetime | None:
    try:
        return _datetime.fromisoformat((evt.get("date") or "").replace("Z", "+00:00"))
    except ValueError:
        return None


def _swap_team_in_desc(desc: str, token: str, old_name: str, new_name: str) -> str:
    """Re-point a pick description at the resolved team.

    The description is load-bearing downstream (it is what claude_grade reads and
    what _insert_odds/_insert_emojis match against the message text), so leaving
    "Arizona Diamondbacks -1.5" on a pick whose team is now the Whipsnakes would
    hand the grader a contradiction.
    """
    if not desc:
        return desc
    for pat in (re.escape(old_name), rf"\b{re.escape(old_name.split()[-1])}\b", rf"\b{re.escape(token)}\b"):
        out = re.sub(pat, new_name, desc, flags=re.IGNORECASE)
        if out != desc:
            return out
    # None of the three matched. That happens when the parse garbled the name
    # badly enough that the description shares no text with either the old name
    # or the message token ("Meditbe Moneyline" for a bet on Uroš Medić) — which
    # is exactly when the description is most wrong. Returning it unchanged
    # would leave the contradiction this function exists to prevent, so fall
    # back to replacing everything ahead of the bet-type keyword.
    m = re.search(
        r"\b(moneyline|ml|spread|puck ?line|run ?line|total|over|under|to win|"
        r"[+-]\d|\d+(\.\d+)?u\b)", desc, flags=re.IGNORECASE)
    if m and m.start() > 0:
        return new_name + " " + desc[m.start():]
    return desc


_VERIFY_STOPWORDS = {
    "over", "under", "moneyline", "spread", "parlay", "leg", "the", "and", "vs",
    "win", "wins", "ml", "tt", "unit", "units", "max", "lock", "play", "pick",
    "team", "total", "line", "odds", "bet", "prop", "first", "half", "game",
}


# Promotions/leagues that share a sport bucket but NOT a schedule. If the
# capper names one of these and it isn't what we classified, the slate we are
# checking is simply the wrong one — a surname that happens to match somebody on
# it is a coincidence, not a confirmation. Seen live: a PFL parlay classified as
# UFC, whose "Medic" leg would otherwise bind to UFC's Uroš Medić and grade to a
# verdict about a fight the capper never bet on. Stuck-and-flagged beats
# confidently-wrong, so this suppresses the repair rather than risking it.
_RIVAL_PROMOTIONS = {
    "UFC": ("pfl", "bellator", "one championship", "one fc", "invicta", "ksw"),
}


def _promotion_conflict(sport: str, raw_text: str) -> str | None:
    low = (raw_text or "").lower()
    for name in _RIVAL_PROMOTIONS.get(sport, ()):
        if re.search(rf"\b{re.escape(name)}\b", low):
            return name.upper()
    return None


def _competitor_names(events: list[dict]) -> list[str]:
    """Every competitor on the slate, teams and athletes alike.

    UFC puts the whole card in ONE event with a competition per bout, so this
    deliberately walks competitions rather than events.
    """
    names = []
    for event in events:
        for comp in event.get("competitions", [{}]):
            for c in comp.get("competitors", []):
                n = (c.get("team", {}).get("displayName", "")
                     or c.get("athlete", {}).get("displayName", ""))
                if n:
                    names.append(n)
    return names


def _tokens_from_text(raw_text: str) -> list[str]:
    """Candidate name tokens from the capper's own words."""
    out = []
    for t in re.split(r"[^\w'’\-]+", raw_text or ""):
        t = t.strip("-'’")
        if len(t) > 2 and not t.isdigit() and t.lower() not in _VERIFY_STOPWORDS:
            out.append(t)
    return out


async def verify_picks_on_schedule(
    picks: list[dict],
    sport: str,
    raw_text: str,
    date_str: str,
    scoreboard_cache: dict,
    *,
    day_hint: str | None = None,
) -> list[dict]:
    """Check every pick's team against the day's schedule; repair it from the
    raw message when the parse names someone who isn't playing.

    `validate_sport` already asks "does this team have a game?", but it answers
    by returning its arguments unchanged — which is also what it returns when it
    gives up, so a confirmed team and a hallucinated one are indistinguishable to
    the caller. That gap is the whole bug: a parse can invent a fighter who is on
    no card anywhere ("Ihor Medic" for a night whose main event was Uroš Medić),
    grade UNKNOWN for free forever, and nothing says a word. Worse, it can invent
    one who IS fighting in a different bout, in which case it grades cleanly to a
    verdict about the wrong contest.

    The repair is that the capper's own text already held the answer: the token
    "Medic" matches Uroš Medić exactly. The parse threw that away by expanding a
    surname into a full name it guessed at. So when the parsed team matches
    nobody, we go back to the words that were actually written.

    Returns one dict per pick: {index, status, teams, description, note} where
    status is:
      n/a         no ESPN schedule for this sport, or the fetch failed — an
                  outage must never be read as a bad parse
      confirmed   the parsed team is really competing that day
      corrected   it wasn't; exactly one message token resolves, so use that
      unverified  nothing resolves, or several do — flag it, never guess
      suspect     it IS competing, but nobody in the message named them while a
                  word they did write points at someone else — the wrong-contest
                  case, which otherwise grades cleanly and silently

    Costs no Claude calls and no extra HTTP: it reads the same `scoreboard_cache`
    that `validate_sport` has already populated for this sport/date.
    """
    results: list[dict] = []
    if not picks:
        return results

    # Which competitors are already spoken for by a leg that verified cleanly.
    # Without this a parlay defeats the repair: "Medic/Rakic parlay" offers both
    # tokens to every leg, so the broken leg sees two candidates and gives up.
    claimed: set[str] = set()
    slates: dict[str, list[str]] = {}

    # Cappers routinely post the evening before ("SATURDAY BEST BET" sent
    # Friday; KBO is always posted the night before), so checking only the post
    # date would return "no schedule" for a large share of picks and skip
    # verification on exactly the ones nobody is watching. Union the candidate
    # days — matching is by competitor NAME, so a team appearing on both days
    # collapses to one entry and cannot manufacture ambiguity.
    cands = [d for d in (day_hint, date_str) if d]
    try:
        cands.append((_date.fromisoformat(date_str) + timedelta(days=1)).isoformat())
    except ValueError:
        pass
    seen_days: list[str] = []
    for d in cands:
        if d not in seen_days:
            seen_days.append(d)

    async def _slate(psport: str) -> list[str] | None:
        if psport not in ESPN_LEAGUES:
            return None
        if psport not in slates:
            names: list[str] = []
            for d in seen_days:
                key = (psport, d)
                if key not in scoreboard_cache:
                    scoreboard_cache[key] = await fetch_espn(psport, d)
                sb = scoreboard_cache[key]
                if sb and sb.get("events"):
                    names.extend(_competitor_names(sb.get("events", [])))
            if not names:
                return None
            slates[psport] = names
        return slates.get(psport)

    # Pass 1 — confirm what we can, and record who each confirmed leg is using.
    pending: list[int] = []
    for i, pick in enumerate(picks):
        psport = pick.get("sport") or sport
        names = await _slate(psport)
        if names is None:
            results.append({"index": i, "status": "n/a", "teams": pick.get("teams", []),
                            "description": pick.get("description", ""), "note": ""})
            continue
        teams = [t for t in (pick.get("teams") or []) if t]
        hits = [n for t in teams for n in names if _team_matches(t.lower(), n.lower())]
        if hits:
            claimed.update(h.lower() for h in hits)
            results.append({"index": i, "status": "confirmed", "teams": teams,
                            "description": pick.get("description", ""), "note": "",
                            "_hits": hits})
        else:
            results.append({"index": i, "status": "", "teams": teams,
                            "description": pick.get("description", ""), "note": ""})
            pending.append(i)

    # Pass 2 — repair the rest from the capper's own words.
    tokens = _tokens_from_text(raw_text)
    for i in pending:
        pick = picks[i]
        psport = pick.get("sport") or sport
        names = slates.get(psport) or []
        matches: dict[str, str] = {}      # competitor -> token that found them
        for tok in tokens:
            for n in names:
                if n.lower() in claimed:
                    continue
                if _team_matches(tok.lower(), n.lower()):
                    matches.setdefault(n, tok)
        old = " ".join(results[i]["teams"]) or "?"
        rival = _promotion_conflict(psport, raw_text)
        if rival:
            results[i].update(
                status="unverified",
                note=f"{old!r} is on no {psport} slate for {'/'.join(seen_days)}, and the message "
                     f"names {rival} — the pick is probably {rival}, which we do not carry, "
                     f"so this is a coverage gap and not a repairable parse",
            )
        elif len(matches) == 1:
            new_name, tok = next(iter(matches.items()))
            claimed.add(new_name.lower())
            results[i].update(
                status="corrected",
                teams=[new_name],
                description=_swap_team_in_desc(
                    pick.get("description", ""), tok,
                    (results[i]["teams"] or [tok])[0], new_name),
                note=f"{old!r} is on no {psport} slate for {'/'.join(seen_days)}; "
                     f"message says {tok!r} → {new_name}",
            )
        elif len(matches) > 1:
            results[i].update(
                status="unverified",
                note=f"{old!r} is on no {psport} slate for {'/'.join(seen_days)}; message tokens are ambiguous "
                     f"between {', '.join(sorted(matches))} — not guessing",
            )
        else:
            results[i].update(
                status="unverified",
                note=f"{old!r} matches no competitor on the {psport} slate for {'/'.join(seen_days)}, "
                     f"and neither does anything in the message",
            )

    # Pass 3 — the quiet one. A leg can confirm against the slate and still be
    # about the wrong contest: "Medic/Rakic parlay" parsed as Mateusz Rębecki,
    # who really was fighting that night, just not in the bout anyone bet on. It
    # found a real market, priced it, and graded to a clean verdict — nothing
    # downstream can tell that from a correct pick, which is what makes it the
    # dangerous failure and not the loud one. The tell is textual, not
    # schedule-based: nobody in the message ever names this competitor, while a
    # word they DID write points at someone still unspoken for. Both halves are
    # required — that conjunction is what keeps a legitimate nickname the slate
    # doesn't spell out ("Snakes" for the D-backs) from tripping it. Reported,
    # never auto-corrected: overriding a team that genuinely is playing on a
    # textual hunch is its own way to grade the wrong game.
    for res in results:
        if res["status"] != "confirmed":
            continue
        hits = res.pop("_hits", [])
        if any(_team_matches(tok.lower(), h.lower()) for tok in tokens for h in hits):
            continue                      # the capper's own words back this pick
        psport = picks[res["index"]].get("sport") or sport
        others = sorted({
            n for tok in tokens for n in (slates.get(psport) or [])
            if n.lower() not in claimed and _team_matches(tok.lower(), n.lower())
        })
        if others:
            res["status"] = "suspect"
            res["note"] = (
                f"parsed {', '.join(hits)!r} but the message never names them; "
                f"it does name {', '.join(others)} — verify which contest this bet is on"
            )
    for res in results:
        res.pop("_hits", None)
    return results


async def resolve_nickname_collision(
    sport: str,
    teams: list[str],
    description: str,
    raw_text: str,
    date_str: str,
    scoreboard_cache: dict,
    *,
    post_time: _datetime | None = None,
) -> tuple[str, list[str], str, str | None]:
    """Resolve a cross-league nickname using the schedule rather than the prompt.

    Returns (sport, teams, description, warning). Everything is returned
    unchanged when the message carries no collision token, when Claude resolved
    it to something outside the candidate set, or when the day stays genuinely
    ambiguous — in that last case `warning` is set so it gets flagged instead of
    silently guessed.
    """
    low = (raw_text or "").lower()
    token = next((t for t in _NICKNAME_COLLISIONS if re.search(rf"\b{t}\b", low)), None)
    if not token:
        return sport, teams, description, None
    candidates = _NICKNAME_COLLISIONS[token]

    # Only intervene when Claude actually landed on one of the known candidates.
    # If it produced something else entirely, this map isn't what's in play and
    # overriding would itself be a guess.
    parsed_team = " ".join(teams).lower()
    matched = next(
        (t for _s, t in candidates if t.split()[-1].lower() in parsed_team), None
    )
    if not matched:
        return sport, teams, description, None

    found: list[tuple[str, str, _datetime | None]] = []
    for cand_sport, cand_team in candidates:
        if cand_sport not in ESPN_LEAGUES:
            continue
        key = (cand_sport, date_str)
        if key not in scoreboard_cache:
            scoreboard_cache[key] = await fetch_espn(cand_sport, date_str)
        sb = scoreboard_cache[key]
        if not sb:
            continue
        events = sb.get("events", [])
        ids = find_event_ids(events, [cand_team])
        if not ids:
            continue
        start = next((_event_start(e) for e in events if e.get("id") in ids), None)
        found.append((cand_sport, cand_team, start))

    def _resolved(cand: tuple[str, str, _datetime | None]):
        s, t, _ = cand
        if t == matched:
            return sport, teams, description, None
        return s, [t], _swap_team_in_desc(description, token, matched, t), None

    if not found:
        return sport, teams, description, None
    if len(found) == 1:
        return _resolved(found[0])

    if post_time is not None:
        # A pregame pick cannot be for a game that had already started.
        upcoming = [f for f in found if f[2] and f[2] > post_time]
        if len(upcoming) == 1:
            return _resolved(upcoming[0])
        if len(upcoming) >= 2:
            ranked = sorted(upcoming, key=lambda f: f[2])
            near_h = (ranked[0][2] - post_time).total_seconds() / 3600
            next_h = (ranked[1][2] - post_time).total_seconds() / 3600
            if near_h <= _COLLISION_NEAR_HOURS and next_h >= _COLLISION_FAR_HOURS:
                return _resolved(ranked[0])

    listing = " vs ".join(f"{t} ({s})" for s, t, _ in found)
    warn = (f'ambiguous nickname "{token}" — {listing} both play {date_str}; '
            f'kept {sport} {teams}, verify')
    return sport, teams, description, warn


async def validate_sport(
    sport: str,
    teams: list[str],
    bet_text: str,
    date_str: str,
    scoreboard_cache: dict[tuple[str, str], dict | None],
) -> tuple[str, list[str]]:
    """Verify a sport classification against ESPN schedules.

    Checks that the team actually has a game in the classified sport on the
    given date.  If not, tries alternative sports (especially for ambiguous
    names like Rangers, Cardinals, Giants).

    Returns (corrected_sport, corrected_teams).  If no correction needed,
    returns the originals unchanged.
    """
    if sport not in ESPN_LEAGUES or not teams:
        return sport, teams

    # Fetch scoreboard for the classified sport
    sb_key = (sport, date_str)
    if sb_key not in scoreboard_cache:
        scoreboard_cache[sb_key] = await fetch_espn(sport, date_str)
    sb = scoreboard_cache[sb_key]

    # CFL: ESPN has no current CFL data — don't fall through to alternative
    # sport matching which would wrongly reassign CFL teams (e.g., "Blue
    # Bombers" fuzzy-matching MLB "Blue Jays"). CFL uses cfl.ca for scoring.
    if sport == "CFL" and (not sb or not sb.get("events")):
        return sport, teams

    # Raw fragments from the bet text for fuzzy matching
    raw_terms = re.split(r'[\s/]+', bet_text)
    raw_terms = [t for t in raw_terms if len(t) > 2 and t not in ("Over", "Under", "ML", "TT")]
    search_teams = list(set(teams + raw_terms))

    if sb and find_event_ids(sb.get("events", []), teams):
        return sport, teams  # confirmed (exact match)

    # Fuzzy match: team name fragments (e.g. "Tigers") against the SAME sport
    # before trying alternatives — catches cases like "KIA Tigers" → "Detroit Tigers"
    if sb:
        matched_ids = find_event_ids(sb.get("events", []), search_teams)
        if matched_ids:
            corrected_teams = teams
            for evt in sb.get("events", []):
                if evt.get("id") in matched_ids:
                    for comp in evt.get("competitions", [{}]):
                        for c in comp.get("competitors", []):
                            name = c.get("team", {}).get("displayName", "")
                            if name and any(f.lower() in name.lower() for f in raw_terms):
                                corrected_teams = [name]
                                break
            return sport, corrected_teams

    # Build set of alternative sports to check
    needs_check: set[str] = set()
    for team in teams:
        tl = team.lower()
        for frag, alt_sports in _AMBIGUOUS_SPORTS.items():
            if frag in tl:
                for alt in alt_sports:
                    if alt != sport and alt in ESPN_LEAGUES:
                        needs_check.add(alt)

    if not needs_check:
        for alt in ["MLB", "NBA", "NHL", "NFL"]:
            if alt != sport and alt in ESPN_LEAGUES:
                needs_check.add(alt)

    for alt_sport in needs_check:
        alt_key = (alt_sport, date_str)
        if alt_key not in scoreboard_cache:
            scoreboard_cache[alt_key] = await fetch_espn(alt_sport, date_str)
        alt_sb = scoreboard_cache[alt_key]
        if not alt_sb:
            continue
        alt_events = alt_sb.get("events", [])
        matched_ids = find_event_ids(alt_events, search_teams)
        if matched_ids:
            # A cross-sport override needs team-level evidence. The fragment
            # match above fires on shared city words too — "San", "Los",
            # "Angeles" from "San Francisco 49ers vs Los Angeles Rams" matched
            # the Angels' MLB game and rebound a correctly-parsed NFL Week-1
            # lookahead to sport=MLB / teams=["Los Angeles Angels"], which then
            # math-graded the wrong game. Only flip the sport when a bet-text
            # token names the club itself (nickname / short name / abbreviation);
            # geography alone proves nothing — keep checking other sports.
            corrected = _nickname_evidence(alt_events, matched_ids, raw_terms)
            if corrected is not None:
                return alt_sport, corrected

    return sport, teams


def _nickname_evidence(
    events: list[dict], matched_ids: list[str], raw_terms: list[str]
) -> list[str] | None:
    """Corrected team list for a fuzzy cross-sport match, or None without
    club-level evidence.

    A bet-text fragment corroborates a scoreboard team only when it names the
    club itself — nickname ("Tigers"), short display name, or abbreviation.
    City words ("Los", "San", "Angeles") are substrings of half the display
    names in every league and must never rebind a pick across sports.
    """
    for evt in events:
        if evt.get("id") not in matched_ids:
            continue
        for comp in evt.get("competitions", [{}]):
            for c in comp.get("competitors", []):
                team = c.get("team", {})
                display = team.get("displayName", "")
                if not display:
                    continue
                names = [n for n in (team.get("name"), team.get("shortDisplayName")) if n]
                abbrev = (team.get("abbreviation") or "").lower()
                for frag in raw_terms:
                    fl = frag.lower()
                    if (abbrev and fl == abbrev) or any(_team_matches(fl, n) for n in names):
                        return [display]
    return None


# ── Free closing odds from ESPN (backfills) ───────────────────────────────────
# ESPN's summary `pickcenter` carries a sportsbook's OPEN and CLOSE for the
# moneyline, spread (line + juice) and total — no API key, no quota. That makes
# it the right source for backfills, where the Odds API's historical endpoint
# costs 10 per region per market and a season's worth of picks is a whole
# month's plan (it is what exhausted the quota on 2026-08-08).
#
# It is NOT a drop-in for live pricing. Measured against 62 picks the Odds API
# had already priced exactly, ESPN's close runs a median 1.37 points of implied
# probability worse for the bettor (worse on 50/62) — because this is ONE book's
# closing number against the Odds API's best-of-eleven. For a capper's historical
# record that is arguably the fairer basis; for a live pick it is money left on
# the table, which is why nothing in the tracker calls this.

_PICKCENTER_SIDE_FIELD = {"moneyline": "moneyline", "spread": "pointSpread"}


def _pc_american(raw: object) -> int | None:
    """'+130' / '-157' / 'EVEN' -> int."""
    if raw is None:
        return None
    s = str(raw).replace("+", "").strip()
    if s.lower() in ("even", "pk", ""):
        return 100
    try:
        return int(float(s))
    except ValueError:
        return None


def _pc_line(raw: object) -> float | None:
    """'o9.5' / 'u9' / '-1.5' / 'PK' -> float."""
    if raw is None:
        return None
    s = str(raw).lower().lstrip("ou").replace("+", "").strip()
    if s in ("pk", "even", ""):
        return 0.0
    try:
        return float(s)
    except ValueError:
        return None


def _pc_side_for_team(competition: dict, teams: list[str], desc: str) -> str | None:
    """'home'/'away' for the picked team. Longest name match wins, so 'Chicago
    White Sox' doesn't lose to a bare 'Chicago' on the other side."""
    want = (" ".join(t for t in teams if t) or desc or "").lower()
    best: tuple[str, int] | None = None
    for c in competition.get("competitors", []):
        t = c.get("team", {}) or {}
        for nm in (t.get("displayName"), t.get("shortDisplayName"), t.get("name"), t.get("location")):
            if nm and nm.lower() in want and (best is None or len(nm) > best[1]):
                best = (c.get("homeAway", ""), len(nm))
    return best[0] if best and best[0] else None


async def espn_closing_odds(sport: str, date: str, pick: dict, *,
                            allow_open: bool = False) -> dict | None:
    """Closing American odds for `pick` from ESPN pickcenter, or None.

    Game-level moneyline/spread/total only — pickcenter has no period markets.
    A spread/total whose closing line differs from the pick's is refused: that is
    a different bet, and pricing it would misstate the payout for a grade that
    was decided at the pick's own line.

    `allow_open` also accepts the open/current number, for a game that has not
    finished and therefore has no close yet (see espn_current_odds).
    """
    if (pick.get("period") or "game") != "game":
        return None
    bet_type = pick.get("bet_type", "")
    if bet_type not in ("moneyline", "spread", "total"):
        return None
    if sport not in ESPN_LEAGUES:
        return None

    sb = await fetch_espn(sport, date)
    if not sb:
        return None
    events = sb.get("events", [])
    teams = [t for t in (pick.get("teams") or []) if t]
    ids = find_event_ids(events, teams or [pick.get("description", "")])
    if not ids:
        return None
    event = next((e for e in events if e.get("id") == ids[0]), None)
    competition = (event or {}).get("competitions", [{}])[0]

    summary = await fetch_espn_summary(sport, ids[0])
    providers = (summary or {}).get("pickcenter") or []
    if not providers:
        return None
    pc = providers[0]
    book = ((pc.get("provider") or {}).get("name") or "ESPN")

    if bet_type == "total":
        direction = (pick.get("direction") or "over").lower()
        _side = (pc.get("total") or {}).get("over" if direction == "over" else "under") or {}
        node, open_node = _side.get("close") or {}, _side.get("open") or {}
    else:
        side = _pc_side_for_team(competition, teams, pick.get("description", ""))
        if not side:
            return None
        _side = (pc.get(_PICKCENTER_SIDE_FIELD[bet_type]) or {}).get(side) or {}
        node, open_node = _side.get("close") or {}, _side.get("open") or {}

    odds = _pc_american(node.get("odds"))
    line = _pc_line(node.get("line"))
    if odds is None and allow_open:
        # A scheduled or in-progress game has no close yet; ESPN still carries
        # the opening/current number, which is the live market price we want.
        odds = _pc_american((open_node or {}).get("odds"))
        line = _pc_line((open_node or {}).get("line")) if line is None else line
    if odds is None:
        return None
    if bet_type in ("spread", "total") and pick.get("line") is not None and line is not None:
        try:
            if abs(line - float(pick["line"])) > 1e-6:
                return None
        except (TypeError, ValueError):
            return None
    return {"odds": odds, "line": line, "bookmaker": book, "match_type": "espn_close"}


async def espn_current_odds(sport: str, date: str, pick: dict) -> dict | None:
    """ESPN price for a game that may not have finished — the free stand-in while
    the Odds API quota is out.

    Tries the post date and the NEXT day: 93% of picks are posted on game day,
    but cappers do post the night before, and ESPN only carries odds for the
    current day's slate (measured: present 6h+ before first pitch, absent for
    tomorrow's games entirely). Nothing here can price a pick posted two days
    out — it is retried on a later run instead.
    """
    for offset in (0, 1):
        try:
            d = (_date.fromisoformat(date[:10]) + timedelta(days=offset)).isoformat()
        except ValueError:
            return None
        got = await espn_closing_odds(sport, d, pick, allow_open=True)
        if got:
            return {**got, "match_type": "espn_current", "game_date": d}
    return None


# ── How far back ESPN still has closing odds ──────────────────────────────────
# Measured 2026-08-09: 100% of 64 sampled games from 2026-01 to 2026-08 carried a
# closing ML and total, still present at 2025-12, gone by 2025-10. So the useful
# range is roughly the trailing 8-9 months — but that horizon MOVES with the
# calendar, which is why this probes for it instead of hardcoding a date. A
# backfill can then ask for "as far back as we have odds" and stay correct next
# year without anyone editing a constant.
_HORIZON_TTL_DAYS = 7
_HORIZON_MAX_MONTHS = 18
_HORIZON_MISS_STREAK = 2   # consecutive month misses before calling it the edge

# Which leagues are actually playing in a given month — probing an out-of-season
# league returns an empty scoreboard, which is "no games", NOT "no odds", and
# would otherwise report a horizon far shorter than the truth.
_HORIZON_SPORTS_BY_MONTH: dict[int, tuple[str, ...]] = {
    1: ("NBA", "NHL"),   2: ("NBA", "NHL"),   3: ("NBA", "NHL"),
    4: ("MLB", "NBA"),   5: ("MLB", "NBA"),   6: ("MLB", "WNBA"),
    7: ("MLB", "WNBA"),  8: ("MLB", "WNBA"),  9: ("MLB", "NFL"),
    10: ("MLB", "NBA"),  11: ("NBA", "NFL"),  12: ("NBA", "NFL"),
}


def _horizon_state_path() -> str:
    return os.path.join(os.path.expanduser("~"), ".espn_odds_horizon.json")


async def _month_has_odds(year: int, month: int) -> bool | None:
    """True/False if a sampled game that month has closing odds; None if no games
    were found at all (out of season / nothing scheduled) and it can't be judged."""
    saw_games = False
    for sport in _HORIZON_SPORTS_BY_MONTH.get(month, ("NBA", "MLB")):
        for day in (15, 8, 22):
            try:
                sb = await fetch_espn(sport, f"{year:04d}-{month:02d}-{day:02d}")
            except Exception:  # noqa: BLE001 - a probe must never break the caller
                continue
            events = (sb or {}).get("events", [])
            if not events:
                continue
            saw_games = True
            for ev in events[:2]:
                summary = await fetch_espn_summary(sport, ev.get("id", ""))
                providers = (summary or {}).get("pickcenter") or []
                if not providers:
                    continue
                pc = providers[0]
                ml = ((pc.get("moneyline") or {}).get("home") or {}).get("close", {}).get("odds")
                tot = ((pc.get("total") or {}).get("over") or {}).get("close", {}).get("odds")
                if ml or tot:
                    return True
            break  # found games for this sport/month and none had odds — try next sport
    return False if saw_games else None


async def espn_odds_horizon(*, refresh: bool = False) -> str:
    """Oldest date ESPN still prices, as YYYY-MM-DD. Cached for a week.

    Walks back a month at a time and stops after two consecutive months that had
    games but no odds — one miss alone can be a quiet month rather than the edge.
    Falls back to 8 months on any failure, which is the measured typical value.
    """
    path = _horizon_state_path()
    if not refresh:
        try:
            with open(path) as f:
                state = json.load(f)
            checked = _date.fromisoformat(state["checked_at"])
            if (_date.today() - checked).days < _HORIZON_TTL_DAYS:
                return state["horizon"]
        except (OSError, ValueError, KeyError):
            pass

    today = _date.today()
    oldest_ok = today
    misses = 0
    for back in range(1, _HORIZON_MAX_MONTHS + 1):
        y, m = today.year, today.month - back
        while m <= 0:
            m += 12
            y -= 1
        has = await _month_has_odds(y, m)
        if has is None:
            continue                      # no games to judge by; keep walking
        if has:
            oldest_ok = _date(y, m, 1)
            misses = 0
        else:
            misses += 1
            if misses >= _HORIZON_MISS_STREAK:
                break
    horizon = oldest_ok.isoformat()
    try:
        with open(path, "w") as f:
            json.dump({"checked_at": today.isoformat(), "horizon": horizon}, f)
    except OSError:
        pass
    return horizon
