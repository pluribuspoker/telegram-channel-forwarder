#!/usr/bin/env python3
"""
tracker.py — Grade sports picks from Telegram channel exports.

Usage:
  python tracker.py --backtest result_df.json
  python tracker.py --backtest result.json
"""

import asyncio
import json
import os
import re
import argparse

from datetime import date as _date, timedelta

import anthropic
import httpx
from dotenv import load_dotenv

from common import VERDICT_EMOJI

load_dotenv()

_claude: anthropic.AsyncAnthropic | None = None


def claude() -> anthropic.AsyncAnthropic:
    global _claude
    if _claude is None:
        _claude = anthropic.AsyncAnthropic()
    return _claude


# Sentinels returned by build_context to signal "no game data" vs "game not yet played"
CONTEXT_SKIP = "__SKIP__"
CONTEXT_PENDING = "__PENDING__"


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


async def fetch_tennis_match_context(player: str, date: str) -> str:
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
_QUALIFIERS = {"state", "tech", "a&m", "am", "international"}


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
        comp = event.get("competitions", [{}])[0]
        event_names = []
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


# ─── Message parsing ──────────────────────────────────────────────────────────

def msg_plain_text(msg: dict) -> str:
    text = msg.get("text", "")
    if isinstance(text, list):
        parts = []
        for chunk in text:
            if isinstance(chunk, str):
                parts.append(chunk)
            elif isinstance(chunk, dict) and chunk.get("type") != "blockquote":
                parts.append(chunk.get("text", ""))
        return "".join(parts)
    return text


def extract_label(text: str) -> str | None:
    if "\u2705" in text:
        return "win"
    if "\u274c" in text:
        return "loss"
    return None


def strip_label(text: str) -> str:
    return re.sub(r"[\u2705\u274c]", "", text).strip()


def grade_matches_label(grade: str, label: str) -> bool:
    """Check if a graded verdict matches the expected label (win/loss)."""
    return (grade == "WIN" and label == "win") or (grade == "LOSS" and label == "loss")


# ─── Claude prompts ───────────────────────────────────────────────────────────

_PARSE_PROMPT = """\
Extract the sports betting pick(s) from this message. Ignore stats, records, and commentary.

Return JSON (no markdown fences):
{{
  "sport": "NBA|NCAAB|MLB|NFL|NHL|UFL|Tennis|UFC|Boxing|Other",
  "picks": [
    {{
      "description": "concise one-line summary of the exact bet",
      "sport": null,
      "bet_type": "spread|moneyline|total|team_total|prop",
      "is_parlay_leg": false,
      "period": "game|1h|2h|1q|2q|3q|4q",
      "teams": ["Team or player name(s) in the bet"],
      "player": "player name if this is a player prop, else null",
      "prop_stat": "stat abbrev if prop (e.g. PTS, REB, AST, PTS+REB, HITS), else null",
      "line": <number or null>,
      "direction": "over|under|null"
    }}
  ]
}}

Classification rules:
- NCAAB = college basketball. NCAAF = college football. The football season ends in January. In February, March, and April there is NO college football. Any college team name appearing in a Feb/Mar/Apr pick is ALWAYS NCAAB, never NCAAF. College team examples: Iowa, Ohio State, Indiana, Texas, Tennessee, Iowa State, Missouri, Florida, Arizona, Duke, Kentucky, UConn, Michigan, Auburn, Houston, Purdue, Illinois, Arkansas, St. Joseph's, New Mexico, Marquette, etc. Critically: bare city/state names like "Arizona", "Florida", "Michigan", "Texas" in a spread or moneyline context during Feb–Apr refer to their COLLEGE team (NCAAB), NOT the pro team (MLB/NFL/NHL). Only classify as a pro sport when the full pro team name is used (e.g. "Arizona Diamondbacks", "Florida Marlins", "Michigan none") or when context makes the pro team unambiguous.
- This NCAAB rule applies to TEAM names only, NOT individual player props. If a pick names a single NBA/NHL/MLB player (e.g. Matas Buzelis, Stephon Castle, Tyler Herro), use their actual professional league (NBA, NHL, MLB, etc.), regardless of the month.
- UFC/MMA: if the pick is on individual MMA/UFC fighter names with a moneyline, classify as UFC. Common fighters: Pereira, Gafurov, Souza, Anders, Sola, Murphy, Aswell, etc.
- Boxing: if the pick involves known professional boxers (e.g. Ryan Garcia, Canelo, Fury, Usyk, Crawford, Beterbiev, etc.), classify as Boxing, not UFC. If a single surname could be a boxer (e.g. Garcia), prefer Boxing over UFC when no other context is available.
- If a single surname with a moneyline has no clear sport context and is not a known boxer or MMA fighter, default to UFC.
- For parlays: list each leg as a separate pick with its REAL bet_type (moneyline, spread, etc.) and set is_parlay_leg=true on each. Do NOT use bet_type="parlay". When players/teams are slash-separated (e.g. "FAA/Shapovalov MLP" or "SPURS/GARCIA MLP"), split them into ONE pick per player/team — do not put two teams in one pick's teams field.
- Cross-sport parlays: if legs belong to different sports (e.g. one NBA team + one UFC fighter), set the pick-level "sport" field to override the top-level sport for that leg. Leave pick "sport" as null when it matches the top-level sport.
- Period: 1h=first half, 2h=second half, 1q=first quarter, game=full game (default).

Message:
{text}"""

_GRADE_PROMPT = """\
Grade this sports betting pick. Show your calculation, then give the verdict.

Pick: {pick}
Date: {date}

Game data:
{context}

Rules by bet type:
- Spread (e.g. team -3.5 or team +3.5):
    * Team listed as -X is the FAVORITE. WIN if that team wins by MORE than X. LOSS if they win by less or lose. PUSH if exactly X.
    * Team listed as +X is the UNDERDOG. WIN if that team wins OUTRIGHT (regardless of margin) OR loses by LESS than X. LOSS if they lose by MORE than X. PUSH if they lose by exactly X.
    * Example: Ohio State +8, Ohio State wins outright → WIN (dog won, cover guaranteed).
- Moneyline: did the picked team/fighter win outright?
- Total over/under (bet_type=total): ALWAYS add BOTH teams' scores regardless of how the pick is worded. score_A + score_B = combined. Compare combined to line. Even "Drake 1H Over 62.5" means the whole game's H1 combined, not just Drake's score — because bet_type is total, not team_total.
- Team total (bet_type=team_total, e.g. "Hornets team total over 117.5"): use ONLY the named team's score, not combined.
- Player prop: add the player's listed stats. Compare to line.
- Period bets (1H, 1Q, 2H): use ONLY the scores for that period shown in the data.
- UFC/MMA: use LAST NAME matching if the full name doesn't exactly match. "Alex Sola" matches "Axel Sola".
- Boxing/UFC moneyline: fighter wins the bout → WIN. Loses → LOSS. Scores may show "W"/"L" or numeric points — use winner field or highest score.
- Tennis set bet ("to win 2nd set", "to win a set"): use per-set scores (S1=, S2=, ...) from the data.
  * "win Nth set": player's SN score > opponent's SN score → WIN.
  * "win a set": player won at least one set (any SN) → WIN.
- If game or player not found: UNKNOWN.

Return JSON only (no markdown):
{{"calc": "show the key numbers, e.g. 125+123=248 vs 237.5", "verdict": "WIN|LOSS|PUSH|UNKNOWN"}}"""


# ─── Cost tracking ────────────────────────────────────────────────────────────
# Sonnet 4.6: $3/MTok input, $15/MTok output
_COST_PER_INPUT  = 3.0 / 1_000_000
_COST_PER_OUTPUT = 15.0 / 1_000_000

_usage: dict[str, int] = {"input_tokens": 0, "output_tokens": 0}


def _accum(usage: object) -> None:
    _usage["input_tokens"]  += getattr(usage, "input_tokens",  0)
    _usage["output_tokens"] += getattr(usage, "output_tokens", 0)


def usage_cost() -> float:
    return _usage["input_tokens"] * _COST_PER_INPUT + _usage["output_tokens"] * _COST_PER_OUTPUT


async def _claude_create_with_retry(**kwargs) -> object:
    """Call claude().messages.create with up to 4 retries on transient errors (500, 529)."""
    for attempt in range(4):
        try:
            return await claude().messages.create(**kwargs)
        except (anthropic.InternalServerError, anthropic.APIStatusError) as exc:
            status = getattr(exc, "status_code", None)
            if status not in (500, 529) or attempt == 3:
                raise
            await asyncio.sleep(2 ** attempt)


async def claude_parse(text: str) -> dict | None:
    resp = await _claude_create_with_retry(
        model="claude-sonnet-4-6",
        max_tokens=500,
        messages=[{"role": "user", "content": _PARSE_PROMPT.format(text=text)}],
    )
    _accum(resp.usage)
    raw = re.sub(r"^```(?:json)?\n?|```$", "", resp.content[0].text.strip(), flags=re.MULTILINE).strip()
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return None


async def claude_grade(pick_desc: str, date: str, context: str) -> tuple[str, str]:
    """Returns (verdict, calc)."""
    resp = await _claude_create_with_retry(
        model="claude-sonnet-4-6",
        max_tokens=150,
        messages=[{
            "role": "user",
            "content": _GRADE_PROMPT.format(pick=pick_desc, date=date, context=context),
        }],
    )
    _accum(resp.usage)
    raw = resp.content[0].text.strip()
    # Try to parse JSON verdict first
    calc = ""
    try:
        clean = re.sub(r"^```(?:json)?\n?|```$", "", raw, flags=re.MULTILINE).strip()
        result = json.loads(clean)
        verdict = result.get("verdict", "").strip().upper()
        calc = result.get("calc", "")
        if verdict in ("WIN", "LOSS", "PUSH", "UNKNOWN"):
            # Sanity check: WIN/LOSS/PUSH must have numbers in calc (grader did math)
            if verdict in ("WIN", "LOSS", "PUSH") and not re.search(r'\d', calc):
                return "UNKNOWN", calc
            return verdict, calc
    except (json.JSONDecodeError, AttributeError):
        pass
    # Fallback: find first valid verdict word in response
    for word in re.sub(r"[^A-Z\s]", "", raw.upper()).split():
        if word in ("WIN", "LOSS", "PUSH", "UNKNOWN"):
            return word, raw
    return "UNKNOWN", raw


# ─── Grade context builder ────────────────────────────────────────────────────

def _completed_events(data: dict) -> list[dict]:
    """Return only completed events from a scoreboard response."""
    return [
        e for e in data.get("events", [])
        if e.get("status", {}).get("type", {}).get("completed", False)
    ]


async def build_context(
    sport: str,
    date: str,
    pick: dict,
    scoreboard: dict | None,
    summary_cache: dict,
) -> tuple[str, str]:
    """Return (context_str, game_date) for grading this pick.
    game_date is the actual date the game is/was played (may differ from pick date)."""
    bet_type = pick.get("bet_type", "")
    period = pick.get("period", "game")
    player = pick.get("player") or ""
    teams = pick.get("teams") or []
    is_parlay_leg = pick.get("is_parlay_leg", False)

    # Tennis: search ESPN core API for match result
    if sport == "Tennis":
        player_or_team = player or (teams[0] if teams else "")
        if not player_or_team:
            return CONTEXT_SKIP, date
        return await fetch_tennis_match_context(player_or_team, date), date

    # Boxing: Odds API scores (free tier = last ~3 days only)
    if sport == "Boxing":
        fighter = player or (teams[0] if teams else "")
        if not fighter:
            return CONTEXT_SKIP, date
        events = await fetch_odds_api_scores("Boxing", date)
        ctx = odds_api_context(fighter, events)
        return (ctx if ctx else CONTEXT_SKIP), date

    # Other unknown sports → skip
    if sport not in ESPN_LEAGUES:
        return CONTEXT_SKIP, date

    # Props and period bets need game summaries
    needs_summary = period != "game" or bet_type == "prop"

    if needs_summary and scoreboard:
        events = _completed_events(scoreboard)
        event_ids = find_event_ids(events, teams, player)

        # If no match by name, search all completed events
        if not event_ids:
            event_ids = [e.get("id") for e in events if e.get("id")]

        parts = []
        for eid in event_ids:
            cache_key = (sport, eid)
            if cache_key not in summary_cache:
                summary_cache[cache_key] = await fetch_espn_summary(sport, eid)
            summary = summary_cache[cache_key]
            if not summary:
                continue
            if bet_type == "prop":
                parts.append(box_score_text(summary, player))
            else:
                parts.append(line_scores_text(summary))

        if parts:
            return "\n\n".join(p for p in parts if p.strip()), date

    # Default: scoreboard text — completed games only
    if scoreboard:
        if sport != "UFC" and (teams or player):
            events = _completed_events(scoreboard)
            relevant_ids = find_event_ids(events, teams, player)
            if relevant_ids:
                filtered = [e for e in events if e.get("id") in set(relevant_ids)]
                if filtered:
                    return scoreboard_text({"events": filtered}, sport), date
            # No completed match — check if game exists but hasn't started/finished yet
            all_events = scoreboard.get("events", [])
            if find_event_ids(all_events, teams, player):
                return CONTEXT_PENDING, date
            # No match on exact date — try the previous day (handles "sent late" picks)
            prev_date = (
                _date.fromisoformat(date) - timedelta(days=1)
            ).isoformat()
            prev_sb = await fetch_espn(sport, prev_date)
            if prev_sb:
                prev_events = _completed_events(prev_sb)
                prev_ids = find_event_ids(prev_events, teams, player)
                if prev_ids:
                    filtered = [e for e in prev_events if e.get("id") in set(prev_ids)]
                    if filtered:
                        return scoreboard_text({"events": filtered}, sport), prev_date
                if find_event_ids(prev_sb.get("events", []), teams, player):
                    return CONTEXT_PENDING, prev_date
            # If the sport had NO completed games at all on the pick date (e.g. picks posted
            # days before a weekend game), scan the next 3 days for a scheduled matchup.
            if not _completed_events(scoreboard):
                for offset in range(1, 4):
                    future_date = (_date.fromisoformat(date) + timedelta(days=offset)).isoformat()
                    future_sb = await fetch_espn(sport, future_date)
                    if future_sb and find_event_ids(future_sb.get("events", []), teams, player):
                        return CONTEXT_PENDING, future_date
            return CONTEXT_SKIP, date
        completed = _completed_events(scoreboard)
        if not completed:
            return CONTEXT_SKIP, date
        return scoreboard_text({"events": completed}, sport), date

    return CONTEXT_SKIP, date


# ─── Backtest ─────────────────────────────────────────────────────────────────

def _skip_reason(r: dict) -> str:
    if r.get("is_parlay_leg") and r["sport"] not in ESPN_LEAGUES and r["sport"] != "Tennis":
        return f"parlay (no data: {r['sport']})"
    if r["sport"] not in ESPN_LEAGUES and r["sport"] != "Tennis":
        return f"no data ({r['sport']})"
    if r["bet_type"] == "prop":
        return "prop"
    if r["period"] != "game":
        return f"period ({r['period']})"
    return "unknown"


def _write_detail_file(path: str, source: str, results: list, graded: list,
                       correct_list: list, skipped_list: list, wrong_list: list,
                       cost: float) -> None:
    sep = "=" * 80
    thin = "-" * 80

    with open(path, "w", encoding="utf-8") as f:
        def w(line: str = "") -> None:
            f.write(line + "\n")

        # Header
        w(sep)
        w(f"BACKTEST DETAIL REPORT")
        w(f"Source : {source}")
        w(f"Total  : {len(results)}  |  Graded: {len(graded)}  |  Skipped: {len(skipped_list)}")
        if graded:
            pct = round(100 * len(correct_list) / len(graded))
            w(f"Accuracy: {len(correct_list)}/{len(graded)} ({pct}%)")
        w(f"Cost   : ${cost:.4f}  ({_usage['input_tokens']:,} in / {_usage['output_tokens']:,} out tokens)")
        w(sep)

        for r in results:
            mark = "OK" if r["correct"] else ("--" if r["skipped"] else "XX")
            w()
            w(sep)
            w(f"[{mark}] MSG {r['msg_id']}  |  {r['date']}  |  {r['sport']}  |  label={r['label'].upper()}  grade={r['grade']}")
            w(sep)

            # Raw message text
            w("RAW TEXT:")
            for line in r["raw_text"].splitlines():
                w(f"  {line}")
            w()

            # Parsed pick fields
            w("PARSED PICK:")
            p = r["parsed"]
            w(f"  description : {p.get('description', '')}")
            w(f"  bet_type    : {p.get('bet_type', '')}")
            w(f"  period      : {p.get('period', 'game')}")
            w(f"  teams       : {p.get('teams', [])}")
            w(f"  player      : {p.get('player', '')}")
            w(f"  prop_stat   : {p.get('prop_stat', '')}")
            w(f"  line        : {p.get('line', '')}")
            w(f"  direction   : {p.get('direction', '')}")
            w()

            # Context passed to grader
            w("CONTEXT (sent to grader):")
            ctx = r["context"]
            if ctx == CONTEXT_SKIP:
                w("  [skipped — no grader call]")
            else:
                for line in ctx.splitlines():
                    w(f"  {line}")
            w()

            # Grader output
            w(f"GRADE: {r['grade']}")
            if r["calc"]:
                w(f"CALC : {r['calc']}")
            if r["skipped"]:
                w(f"SKIP REASON: {_skip_reason(r)}")
            w(thin)

        # Incorrect summary
        w()
        w(sep)
        w("INCORRECT GRADES:")
        w(sep)
        if wrong_list:
            for r in wrong_list:
                w(f"  msg {r['msg_id']:3d}  {r['date']}  {r['sport']:6s}  got={r['grade']}  expected={r['label'].upper()}")
                w(f"    pick   : {r['pick']}")
                w(f"    calc   : {r['calc']}")
                w()
        else:
            w("  (none)")


async def run_backtest(filepath: str) -> None:
    with open(filepath, encoding="utf-8") as f:
        data = json.load(f)

    channel_name = data.get("name", filepath)
    messages = [m for m in data["messages"] if m.get("type") == "message"]

    print(f"\nBacktest: {channel_name}  ({len(messages)} messages)")
    print("=" * 72)

    results = []
    scoreboard_cache: dict[tuple[str, str], dict | None] = {}
    summary_cache: dict[tuple[str, str], dict | None] = {}

    for msg in messages:
        plain = msg_plain_text(msg)
        if not plain.strip():
            continue

        label = extract_label(plain)
        if label is None:
            continue

        clean = strip_label(plain)
        date = msg["date"][:10]

        parsed = await claude_parse(clean)
        if not parsed:
            print(f"  [parse fail] msg {msg['id']}")
            continue

        sport = parsed.get("sport", "Other")
        picks = parsed.get("picks", [])
        if not picks:
            continue

        # Fetch scoreboard once per (sport, date)
        sb_key = (sport, date)
        if sb_key not in scoreboard_cache:
            scoreboard_cache[sb_key] = await fetch_espn(sport, date)
        scoreboard = scoreboard_cache[sb_key]

        for pick in picks:
            pick_desc = pick.get("description", clean[:80])
            bet_type = pick.get("bet_type", "")
            period = pick.get("period", "game")
            is_parlay_leg = pick.get("is_parlay_leg", False)
            # Per-pick sport override (used for cross-sport parlays)
            pick_sport = pick.get("sport") or sport

            # Fetch scoreboard for per-pick sport if different from message sport
            if pick_sport != sport:
                pick_sb_key = (pick_sport, date)
                if pick_sb_key not in scoreboard_cache:
                    scoreboard_cache[pick_sb_key] = await fetch_espn(pick_sport, date)
                pick_scoreboard = scoreboard_cache[pick_sb_key]
            else:
                pick_scoreboard = scoreboard

            context, _game_date = await build_context(pick_sport, date, pick, pick_scoreboard, summary_cache)

            if context == CONTEXT_SKIP:
                grade, calc = "UNKNOWN", ""
            else:
                grade, calc = await claude_grade(pick_desc, date, context)

            correct = grade_matches_label(grade, label)
            skipped = grade in ("PUSH", "UNKNOWN")
            mark = "OK" if correct else ("--" if skipped else "XX")

            print(
                f"  {mark}  msg {msg['id']:3d}  {date}  {pick_sport:6s}  "
                f"{grade:7s}  label={label.upper():<4s}  {pick_desc[:48]}"
            )
            results.append({
                "msg_id": msg["id"],
                "date": date,
                "sport": pick_sport,
                "label": label,
                "grade": grade,
                "calc": calc,
                "correct": correct,
                "skipped": skipped,
                "pick": pick_desc,
                "bet_type": bet_type,
                "period": period,
                "is_parlay_leg": is_parlay_leg,
                "parsed": pick,
                "context": context,
                "raw_text": msg_plain_text(msg),
            })

    # ── Summary ──
    graded = [r for r in results if not r["skipped"]]
    correct_list = [r for r in graded if r["correct"]]
    skipped_list = [r for r in results if r["skipped"]]
    wrong_list = [r for r in graded if not r["correct"]]

    print(f"\n{'=' * 72}")
    total = len(results)
    if graded:
        pct = round(100 * len(correct_list) / len(graded))
        print(f"Accuracy : {len(correct_list)}/{len(graded)} ({pct}%)  |  skipped: {len(skipped_list)}/{total}")
    else:
        print(f"Accuracy : N/A  |  skipped: {len(skipped_list)}/{total}")

    cost = usage_cost()
    print(f"Cost     : ${cost:.4f}  ({_usage['input_tokens']:,} in / {_usage['output_tokens']:,} out tokens, Sonnet 4.6)")

    if wrong_list:
        print("\nIncorrect grades:")
        for r in wrong_list:
            print(f"  msg {r['msg_id']:3d}  {r['date']}  {r['sport']:6s}  got={r['grade']}  expected={r['label'].upper()}")
            print(f"         {r['pick'][:65]}")

    # Skip breakdown
    if skipped_list:
        skip_by_reason: dict[str, int] = {}
        for r in skipped_list:
            reason = _skip_reason(r)
            skip_by_reason[reason] = skip_by_reason.get(reason, 0) + 1

        print("\nSkipped breakdown:")
        for reason, count in sorted(skip_by_reason.items(), key=lambda x: -x[1]):
            print(f"  {reason}: {count}")

    # ── Write detail file ──
    base = os.path.splitext(os.path.basename(filepath))[0]
    out_path = f"backtest_{base}.txt"
    _write_detail_file(out_path, filepath, results, graded, correct_list, skipped_list, wrong_list, cost)
    print(f"\nDetail file: {out_path}")


async def grade_one(text: str, date: str) -> None:
    """Parse and grade a single pick message, printing full detail."""
    import sys
    sys.stdout.reconfigure(encoding="utf-8")

    label = extract_label(text)
    clean = strip_label(text)

    parsed = await claude_parse(clean)
    if not parsed:
        print("[parse fail]")
        return

    sport = parsed.get("sport", "Other")
    picks = parsed.get("picks", [])
    print(f"Sport: {sport}  |  {len(picks)} pick(s)")
    print()

    scoreboard_cache: dict = {}
    summary_cache: dict = {}

    sb_key = (sport, date)
    scoreboard_cache[sb_key] = await fetch_espn(sport, date)

    for i, pick in enumerate(picks, 1):
        pick_sport = pick.get("sport") or sport
        if pick_sport != sport:
            ps_key = (pick_sport, date)
            if ps_key not in scoreboard_cache:
                scoreboard_cache[ps_key] = await fetch_espn(pick_sport, date)
            scoreboard = scoreboard_cache[ps_key]
        else:
            scoreboard = scoreboard_cache[sb_key]

        pick_desc = pick.get("description", clean[:80])
        print(f"Pick {i}: {pick_desc}")
        print(f"  sport={pick_sport}  bet_type={pick.get('bet_type')}  period={pick.get('period','game')}"
              f"  teams={pick.get('teams')}  player={pick.get('player')}"
              f"  line={pick.get('line')}  dir={pick.get('direction')}"
              f"  parlay_leg={pick.get('is_parlay_leg', False)}")

        context, _game_date = await build_context(pick_sport, date, pick, scoreboard, summary_cache)
        print()
        print("  CONTEXT:")
        if context == CONTEXT_SKIP:
            print("    [skipped]")
        else:
            for ln in context.splitlines():
                print(f"    {ln}")
        print()

        if context != CONTEXT_SKIP:
            grade, calc = await claude_grade(pick_desc, date, context)
            print(f"  GRADE : {grade}")
            print(f"  CALC  : {calc}")
        else:
            grade = "UNKNOWN"
            print(f"  GRADE : UNKNOWN (skipped)")

        if label:
            correct = grade_matches_label(grade, label)
            print(f"  LABEL : {label.upper()}  →  {'OK' if correct else ('--' if grade in ('PUSH','UNKNOWN') else 'XX')}")
        print()

    cost = usage_cost()
    print(f"Cost: ${cost:.4f}  ({_usage['input_tokens']:,} in / {_usage['output_tokens']:,} out tokens)")


# ─── Live mode ────────────────────────────────────────────────────────────────

_PICK_EMOJI = {k: v for k, v in VERDICT_EMOJI.items() if k in ("WIN", "LOSS", "PUSH")}


def _insert_emojis(text: str, verdicts: list[tuple]) -> str:
    """
    Insert per-pick verdict emojis inline after each pick's line in the message.
    Matches each pick to its line using team/player names, then appends the emoji.
    Lines that can't be matched are left unchanged.
    Returns the modified text (or original if nothing could be matched).
    """
    lines = text.rstrip().split("\n")

    for pick, verdict, _calc, _sport, *_ in verdicts:
        emoji = _PICK_EMOJI.get(verdict)
        if not emoji:
            continue  # UNKNOWN / PENDING — leave line alone

        teams  = pick.get("teams") or []
        player = pick.get("player") or ""
        # Build search terms: full name + individual words longer than 3 chars
        search_terms: list[str] = []
        for t in teams + ([player] if player else []):
            tl = t.lower().strip()
            if tl:
                search_terms.append(tl)
                search_terms.extend(w for w in tl.split() if len(w) > 3)

        for i, line in enumerate(lines):
            if any(ch in line for ch in _PICK_EMOJI.values()):
                continue  # already has an emoji — skip
            line_lower = line.lower()
            if any(term in line_lower for term in search_terms):
                lines[i] = f"{line.rstrip()}{emoji}"
                break  # one match per pick

    return "\n".join(lines)

def _overall_verdict(verdicts: list[tuple]) -> str:
    """
    Collapse per-pick verdicts into a single message verdict.

    Parlay legs: ALL must WIN → WIN; any LOSS → LOSS; any UNKNOWN → UNKNOWN.
    Non-parlay:  all must agree (all WIN or all LOSS); mixed or any UNKNOWN → UNKNOWN.
    """
    if not verdicts:
        return "UNKNOWN"
    all_v = [v[1] for v in verdicts]
    is_parlay = any(v[0].get("is_parlay_leg") for v in verdicts)
    if is_parlay:
        if "PENDING" in all_v:
            return "PENDING"
        if "UNKNOWN" in all_v:
            return "UNKNOWN"
        if "LOSS" in all_v:
            return "LOSS"
        if all(v == "WIN" for v in all_v):
            return "WIN"
        if "PUSH" in all_v:
            return "PUSH"
        return "UNKNOWN"
    else:
        unique = set(all_v) - {"PUSH"}
        if "PENDING" in unique:
            return "PENDING"
        if "UNKNOWN" in unique or len(unique) > 1:
            return "UNKNOWN"
        return unique.pop() if unique else "PUSH"


async def _bot_edit_message(
    bot_token: str,
    channel_id: int,
    message_id: int,
    new_text: str,
    has_media: bool,
) -> bool:
    """Edit a message via Bot API. Returns True on success."""
    method = "editMessageCaption" if has_media else "editMessageText"
    field  = "caption"            if has_media else "text"
    try:
        async with httpx.AsyncClient(timeout=10) as http:
            r = await http.post(
                f"https://api.telegram.org/bot{bot_token}/{method}",
                json={"chat_id": channel_id, "message_id": message_id,
                      field: new_text, "parse_mode": "HTML"},
            )
            if not r.is_success:
                print(f"    [bot edit error] {r.status_code}: {r.text[:120]}")
                return False
            return True
    except Exception as exc:
        print(f"    [bot edit error] {exc}")
        return False


async def run_live(dry_run: bool = False, days: int = 7, channel: int | None = None) -> None:
    import datetime as dt
    from telethon import TelegramClient
    from telethon.sessions import StringSession
    from audit import AuditLog

    api_id    = int(os.getenv("TELEGRAM_API_ID", "0"))
    api_hash  = os.getenv("TELEGRAM_API_HASH", "")
    session   = os.getenv("TELEGRAM_SESSION", "")
    bot_token = os.getenv("BOT_TOKEN", "")

    channels_raw = os.getenv("GRADE_CHANNELS", "[]")
    try:
        channel_ids = json.loads(channels_raw)
    except json.JSONDecodeError:
        print("ERROR: GRADE_CHANNELS must be a JSON array, e.g. [-1001234567890]")
        return
    if not channel_ids:
        print("ERROR: GRADE_CHANNELS not set in .env")
        return
    if channel is not None:
        channel_ids = [channel]

    audit  = AuditLog()
    cutoff = dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=days)
    mode   = "DRY RUN" if dry_run else "LIVE"

    async with TelegramClient(StringSession(session), api_id, api_hash) as client:
        # Resolve channel names once up front
        channel_names: dict[int, str] = {}
        for cid in channel_ids:
            try:
                entity = await client.get_entity(cid)
                channel_names[cid] = getattr(entity, "title", str(cid))
            except Exception:
                channel_names[cid] = str(cid)

        print(f"\nLive grader — {mode}  |  last {days} days  |  channels: {[channel_names[c] for c in channel_ids]}")
        print("=" * 72)

        for channel_id in channel_ids:
            ch_name = channel_names[channel_id]
            print(f"\n{ch_name}  ({channel_id}):")
            scoreboard_cache: dict = {}
            summary_cache:   dict = {}
            edited = skipped = errors = 0

            async for msg in client.iter_messages(channel_id):
                msg_date = msg.date
                if msg_date.tzinfo is None:
                    msg_date = msg_date.replace(tzinfo=dt.timezone.utc)
                if msg_date < cutoff:
                    break

                text = msg.text or ""
                date_str = msg_date.strftime("%Y-%m-%d")

                if not text.strip():
                    continue
                # Skip already graded (check plain text)
                if any(ch in text for ch in ("✅", "❌", "↩️")):
                    continue

                capper = next((l.strip() for l in text.splitlines() if l.strip()), "")
                parsed = await claude_parse(text)
                snippet = " ".join(text.split())[:80]
                if not parsed:
                    skipped += 1
                    print(f"\n  [SKIP] msg {msg.id}  {date_str}  parse failed")
                    print(f"         {snippet}")
                    await audit.record(
                        channel_id=channel_id, message_id=msg.id, date=date_str,
                        sport="Other", pick_desc=snippet, bet_type="",
                        verdict="UNKNOWN", calc="parse failed",
                        prev_caption=text, dry_run=dry_run, channel_name=ch_name, capper_name=capper,
                    )
                    continue
                sport = parsed.get("sport", "Other")
                picks = parsed.get("picks", [])
                if not picks:
                    skipped += 1
                    print(f"\n  [SKIP] msg {msg.id}  {date_str}  no picks extracted  ({sport})")
                    print(f"         {snippet}")
                    await audit.record(
                        channel_id=channel_id, message_id=msg.id, date=date_str,
                        sport=sport, pick_desc=snippet, bet_type="",
                        verdict="UNKNOWN", calc="no picks extracted",
                        prev_caption=text, dry_run=dry_run, channel_name=ch_name, capper_name=capper,
                    )
                    continue

                sb_key = (sport, date_str)
                if sb_key not in scoreboard_cache:
                    scoreboard_cache[sb_key] = await fetch_espn(sport, date_str)

                verdicts = []
                for pick in picks:
                    pick_sport = pick.get("sport") or sport
                    ps_key = (pick_sport, date_str)
                    if ps_key not in scoreboard_cache:
                        scoreboard_cache[ps_key] = await fetch_espn(pick_sport, date_str)
                    context, game_date = await build_context(
                        pick_sport, date_str, pick,
                        scoreboard_cache[ps_key], summary_cache,
                    )
                    if context == CONTEXT_PENDING:
                        verdict, calc = "PENDING", ""
                    elif context == CONTEXT_SKIP:
                        verdict, calc = "UNKNOWN", ""
                    else:
                        verdict, calc = await claude_grade(
                            pick.get("description", text[:80]), date_str, context,
                        )
                    verdicts.append((pick, verdict, calc, pick_sport, game_date))

                # Build edited text — per-pick emoji inserted inline after each pick's line
                # Convert to HTML to preserve original formatting entities
                from telethon.extensions import html as tl_html
                import html as _html
                html_text = tl_html.unparse(text, msg.entities or [])
                # Escape any HTML special chars that Telethon may have left as plain text
                # (unparse already handles this, but sanitise spoiler tag for Bot API)
                html_text = html_text.replace("<spoiler>", "<tg-spoiler>").replace("</spoiler>", "</tg-spoiler>")
                new_text = _insert_emojis(html_text, verdicts)
                graded = [v for v in verdicts if v[1] in _PICK_EMOJI]
                overall = _overall_verdict(verdicts)

                # Print all picks with their individual verdicts
                has_pending = any(v[1] == "PENDING" for v in verdicts)
                if not graded:
                    tag = "WAIT" if has_pending else "SKIP"
                else:
                    tag = "DRY " if dry_run else "EDIT"
                print(f"\n  [{tag}] msg {msg.id}  {date_str}  {sport}")
                for pick, verdict, calc, ps, *_ in verdicts:
                    desc = pick.get("description", "")[:60]
                    calc_str = f"  ({calc})" if calc else ""
                    print(f"         {verdict:<7}  {desc}{calc_str}")

                # Nothing gradeable — log and skip
                if not graded:
                    skipped += 1
                    all_descs = "\n".join(
                        f"{v[1]}: {v[0].get('description', '')}|{v[3]}|{v[4]}|{v[2]}" for v in verdicts
                    )
                    first_pick, _, first_calc, first_sport, first_game_date = verdicts[0]
                    await audit.record(
                        channel_id=channel_id, message_id=msg.id, date=date_str,
                        sport=first_sport,
                        pick_desc=all_descs,
                        bet_type=first_pick.get("bet_type", ""),
                        verdict=overall, calc=first_calc,
                        prev_caption=text, dry_run=dry_run, channel_name=ch_name, capper_name=capper,
                    )
                    continue

                first_pick, _, first_calc, first_sport, first_game_date = verdicts[0]

                if not dry_run:
                    ok = await _bot_edit_message(
                        bot_token, channel_id, msg.id, new_text, msg.media is not None,
                    )
                    if not ok:
                        errors += 1
                        continue
                    await asyncio.sleep(0.5)   # stay under Telegram flood limit

                edited += 1
                all_descs = "\n".join(
                    f"{v[0].get('description', '')}|{v[3]}|{v[4]}|{v[2]}" for v in verdicts if v[1] in _PICK_EMOJI
                )
                all_calcs = "  ·  ".join(
                    v[2] for v in verdicts if v[1] in _PICK_EMOJI and v[2]
                )
                await audit.record(
                    channel_id=channel_id,
                    message_id=msg.id,
                    date=date_str,
                    sport=first_sport,
                    pick_desc=all_descs or first_pick.get("description", ""),
                    bet_type=first_pick.get("bet_type", ""),
                    verdict=overall,
                    calc=all_calcs or first_calc,
                    prev_caption=text,
                    new_caption=new_text if not dry_run else "",
                    dry_run=dry_run,
                    channel_name=ch_name,
                    capper_name=capper,
                )

            print(f"\n  => edited: {edited}  skipped: {skipped}  errors: {errors}")

    cost = usage_cost()
    print(f"\nCost: ${cost:.4f}  ({_usage['input_tokens']:,} in / {_usage['output_tokens']:,} out)")


# ─── Entry point ──────────────────────────────────────────────────────────────

async def main() -> None:
    import sys
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    parser = argparse.ArgumentParser(description="Grade sports betting picks")
    parser.add_argument("--backtest", metavar="FILE", help="JSON export file to backtest")
    parser.add_argument("--grade",    metavar="TEXT", help="Grade a single pick message")
    parser.add_argument("--live",     action="store_true", help="Grade live Telegram channels")
    parser.add_argument("--date",     metavar="YYYY-MM-DD", help="Date for --grade (default: today)")
    parser.add_argument("--days",     type=int, default=7,
                        help="Days back to scan in --live mode (default: 7)")
    parser.add_argument("--channel",  type=int, metavar="ID",
                        help="Limit --live to a single channel ID (overrides GRADE_CHANNELS)")
    parser.add_argument("--dry-run",  action="store_true",
                        help="Log what would be graded/edited without touching Telegram")
    args = parser.parse_args()

    if args.backtest:
        await run_backtest(args.backtest)
    elif args.grade:
        date = args.date or _date.today().isoformat()
        await grade_one(args.grade, date)
    elif args.live:
        await run_live(dry_run=args.dry_run, days=args.days, channel=args.channel)
    else:
        parser.print_help()


if __name__ == "__main__":
    asyncio.run(main())
