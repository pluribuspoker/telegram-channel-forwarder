"""
scores.py — Sports data layer: ESPN, Odds API, and score formatting.
"""

import os

import httpx
from datetime import date as _date, timedelta


# ─── ESPN ─────────────────────────────────────────────────────────────────────

ESPN_LEAGUES: dict[str, tuple[str, str]] = {
    "NBA":   ("basketball", "nba"),
    "NCAAB": ("basketball", "mens-college-basketball"),
    "MLB":   ("baseball", "mlb"),
    "NFL":   ("football", "nfl"),
    "NHL":   ("hockey", "nhl"),
    "NCAAF": ("football", "college-football"),
    "UFC":   ("mma", "ufc"),
    "UFL":   ("football", "ufl"),
}

# Extra query params per sport (e.g. groups=50 for all D1 NCAAB games)
SPORT_EXTRA_PARAMS: dict[str, dict] = {
    "NCAAB": {"groups": "50"},
}

# Odds API sport keys for sports not on ESPN
ODDS_API_KEYS: dict[str, str] = {
    "Boxing": "boxing_boxing",
}


async def fetch_odds_api_scores(sport: str, date: str) -> list[dict]:
    """
    Fetch completed scores from the Odds API for a given sport and date (±1 day).
    Only works within the last ~3 days on the free tier.
    """
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
            events = r.json()
            if not isinstance(events, list):
                return []
            results = []
            for e in events:
                if not e.get("completed"):
                    continue
                try:
                    event_date = _d.fromisoformat(e.get("commence_time", "")[:10])
                    if abs((event_date - target).days) <= 1:
                        results.append(e)
                except ValueError:
                    pass
            return results
        except Exception as exc:
            print(f"    [Odds API error] {sport} {date}: {exc}")
            return []


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


async def fetch_espn(sport: str, date: str) -> dict | None:
    if sport not in ESPN_LEAGUES:
        return None
    category, league = ESPN_LEAGUES[sport]
    url = f"https://site.api.espn.com/apis/site/v2/sports/{category}/{league}/scoreboard"
    params = {"dates": date.replace("-", ""), "limit": "200"}
    params.update(SPORT_EXTRA_PARAMS.get(sport, {}))
    async with httpx.AsyncClient(timeout=10) as http:
        try:
            r = await http.get(url, params=params)
            r.raise_for_status()
            return r.json()
        except Exception as e:
            print(f"    [ESPN error] {sport} {date}: {e}")
            return None


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
    category, league = ESPN_LEAGUES[sport]
    url = f"https://site.api.espn.com/apis/site/v2/sports/{category}/{league}/summary"
    async with httpx.AsyncClient(timeout=15) as http:
        try:
            r = await http.get(url, params={"event": event_id})
            r.raise_for_status()
            return r.json()
        except Exception as e:
            print(f"    [ESPN summary error] {sport}/{event_id}: {e}")
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
                winner = "?"
                status = comp.get("status", {}).get("type", {}).get("description", "")
                for c in comp.get("competitors", []):
                    name = c.get("athlete", {}).get("displayName", "?")
                    fighters.append(name)
                    if c.get("winner"):
                        winner = name
                if fighters:
                    lines.append(f"{' vs '.join(fighters)} → Winner: {winner} [{status}]")
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
            lines.append(f"{away_name} {away_score} at {home_name} {home_score} [{status}]")

    return "\n".join(lines) or "No games found for this date"


def line_scores_text(summary: dict) -> str:
    """Format per-quarter/half scores from a game summary."""
    header = summary.get("header", {})
    comps = header.get("competitions", [{}])[0]
    lines = []

    for c in comps.get("competitors", []):
        team = c.get("team", {}).get("displayName", "?")
        ls = [x.get("displayValue", "?") for x in c.get("linescores", [])]
        final = c.get("score", "?")

        if len(ls) >= 4:
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


def box_score_text(summary: dict, player_hint: str = "") -> str:
    """Format player stats from a game box score."""
    lines = []
    boxscore = summary.get("boxscore", {})

    for team_data in boxscore.get("players", []):
        team_name = team_data.get("team", {}).get("displayName", "?")

        for stat_group in team_data.get("statistics", [])[:1]:
            keys = stat_group.get("keys", [])

            for athlete in stat_group.get("athletes", []):
                name = athlete.get("athlete", {}).get("displayName", "?")
                stats_raw = athlete.get("stats", [])

                # If filtering to a specific player, skip non-matches
                if player_hint:
                    hint_words = [w for w in player_hint.lower().split() if len(w) > 2]
                    name_lower = name.lower()
                    if not any(w in name_lower for w in hint_words):
                        continue

                stats = dict(zip(keys, stats_raw))

                # Basketball stat line
                bball_keys = ["points", "rebounds", "assists", "steals", "blocks"]
                bball = [f"{k[:3].upper()}={stats[k]}" for k in bball_keys if k in stats]

                # Baseball stat line
                bball2_keys = ["hits", "atBats", "runs", "RBIs", "homeRuns", "walks"]
                baseball = [f"{k}={stats[k]}" for k in bball2_keys if k in stats]

                stat_str = bball + baseball
                if stat_str:
                    lines.append(f"  {name} ({team_name}): {', '.join(stat_str)}")

    return "\n".join(lines) or "No player stats found"


# Words that, when following a matched term, indicate it's a different longer team name.
# e.g., "Iowa" should not match "Iowa State Cyclones" because "State" follows "Iowa".
_QUALIFIERS = {"state", "tech", "a&m", "am", "international", "st"}  # "st" = abbrev for State/Saint disambiguation


def _team_matches(term: str, team_name: str) -> bool:
    """Return True if term matches team_name, avoiding ambiguous prefix matches.

    'Iowa' matches 'Iowa Hawkeyes' but NOT 'Iowa State Cyclones'
    'Texas' matches 'Texas Longhorns' but NOT 'Texas Tech Red Raiders'
    """
    t = term.lower().strip()
    n = team_name.lower().strip()
    if not t or not n:
        return False
    if t not in n and n not in t:
        return False
    t_words = t.split()
    n_words = n.split()
    for i in range(len(n_words) - len(t_words) + 1):
        if n_words[i: i + len(t_words)] == t_words:
            next_idx = i + len(t_words)
            if next_idx < len(n_words) and n_words[next_idx] in _QUALIFIERS:
                return False  # e.g., "Iowa" before "State" → skip
            return True
    return True  # term contained in name but not as a clean word sequence


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
