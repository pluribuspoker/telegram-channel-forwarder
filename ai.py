"""
ai.py — Claude AI layer: parsing, grading, and context building.
"""

import asyncio
import json
import re

from datetime import date as _date, timedelta

import anthropic

from scores import (
    ESPN_LEAGUES,
    fetch_espn,
    fetch_espn_summary,
    fetch_odds_api_scores,
    fetch_tennis_match_context,
    odds_api_context,
    scoreboard_text,
    line_scores_text,
    box_score_text,
    find_event_ids,
    _completed_events,
)


_claude: anthropic.AsyncAnthropic | None = None


def claude() -> anthropic.AsyncAnthropic:
    global _claude
    if _claude is None:
        _claude = anthropic.AsyncAnthropic()
    return _claude


# Sentinels returned by build_context to signal "no game data" vs "game not yet played"
CONTEXT_SKIP = "__SKIP__"
CONTEXT_PENDING = "__PENDING__"


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
        return await fetch_tennis_match_context(player_or_team, date, CONTEXT_SKIP), date

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
        if teams or player:
            events = _completed_events(scoreboard)
            relevant_ids = find_event_ids(events, teams, player)
            if relevant_ids:
                # UFC: show the full card so grader sees all bouts; others: filter to the game
                display = scoreboard if sport == "UFC" else {"events": [e for e in events if e.get("id") in set(relevant_ids)]}
                return scoreboard_text(display, sport), date
            # No completed match — check if game/bout exists but hasn't started/finished yet
            if find_event_ids(scoreboard.get("events", []), teams, player):
                return CONTEXT_PENDING, date
            # No match on exact date — try the previous day (handles "sent late" picks)
            prev_date = (_date.fromisoformat(date) - timedelta(days=1)).isoformat()
            prev_sb = await fetch_espn(sport, prev_date)
            if prev_sb:
                prev_events = _completed_events(prev_sb)
                prev_ids = find_event_ids(prev_events, teams, player)
                if prev_ids:
                    display = prev_sb if sport == "UFC" else {"events": [e for e in prev_events if e.get("id") in set(prev_ids)]}
                    return scoreboard_text(display, sport), prev_date
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
