"""
ai.py — Claude AI layer: parsing, grading, and context building.
"""

import asyncio
import json
import re
import time

from datetime import date as _date, timedelta

import anthropic

from common import is_regulation_ml
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
    _ufc_bout_completed,
)


# Cache for Odds API score fetches (KBO, Boxing) — keyed by (sport, date, completed_only).
# Avoids repeated hits within a process and across rapid successive runs.
_scores_cache: dict[tuple, tuple[float, list]] = {}
_SCORES_TTL = 5 * 60  # 5 minutes


async def _fetch_scores_cached(sport: str, date: str, completed_only: bool = True) -> list:
    key = (sport, date, completed_only)
    entry = _scores_cache.get(key)
    if entry and time.monotonic() - entry[0] < _SCORES_TTL:
        return entry[1]
    result = await fetch_odds_api_scores(sport, date, completed_only)
    _scores_cache[key] = (time.monotonic(), result)
    return result


_claude: anthropic.AsyncAnthropic | None = None


def claude() -> anthropic.AsyncAnthropic:
    global _claude
    if _claude is None:
        _claude = anthropic.AsyncAnthropic()
    return _claude


# Sentinels returned by build_context to signal "no game data" vs "game not yet played"
CONTEXT_SKIP = "__SKIP__"
CONTEXT_PENDING = "__PENDING__"
CONTEXT_ESPN_ERROR = "__ESPN_ERROR__"  # ESPN fetch failed (network/SSL); retry next run


# ─── Claude prompts ───────────────────────────────────────────────────────────

_PARSE_PROMPT = """\
Extract the sports betting pick(s) from this message. Ignore stats, records, and commentary.
{date_context}
Return JSON (no markdown fences):
{{
  "sport": "NBA|NCAAB|MLB|NFL|NHL|UFL|Tennis|UFC|Boxing|KBO|Other",
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
- UFC/MMA: if the pick is on individual MMA/UFC fighter names with a moneyline, classify as UFC. Common fighters: Pereira, Gafurov, Souza, Anders, Sola, Murphy, Aswell, Hooper, Bahamondes, Adesanya, etc. UFC events are held almost exclusively on Saturdays — on Saturdays, single-surname moneyline picks with no clear sport context should be classified as UFC, not Tennis. For UFC/Boxing moneylines, put the fighter name in "teams" (NOT "player") — "player" is only for player props (e.g. points over/under).
- Tennis: ONLY classify as Tennis if the message contains explicit Tennis indicators — tournament names (Open, Slam, ATP, WTA, Masters, Wimbledon), match format words (sets, tiebreak, deuce), court surfaces, or well-known tennis players (Djokovic, Alcaraz, Sinner, Swiatek, Sabalenka, Medvedev, Zverev, Rune, etc.). Do NOT classify as Tennis based solely on a person's surname.
- Boxing: if the pick involves known professional boxers (e.g. Ryan Garcia, Canelo, Fury, Usyk, Crawford, Beterbiev, etc.), classify as Boxing, not UFC. If a single surname could be a boxer (e.g. Garcia), prefer Boxing over UFC when no other context is available.
- KBO = Korean Baseball Organization. Classify as KBO if the pick involves KBO team names (e.g. KT Wiz, Samsung Lions, LG Twins, Doosan Bears, Lotte Giants, NC Dinos, KIA Tigers, SSG Landers, Hanwha Eagles, Kiwoom Heroes) or explicit KBO context.
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
- NHL regulation/3-way moneyline (pick description contains "3-way", "60 min", "regulation", "reg ML", etc.): team must win in REGULATION only. If the score data shows OT=1 (or any OT column with a non-zero value), or P4 or more periods, the game went to overtime — the pick is a LOSS regardless of who won in OT.
- Total over/under (bet_type=total): ALWAYS add BOTH teams' scores regardless of how the pick is worded. score_A + score_B = combined. Compare combined to line. Even "Drake 1H Over 62.5" means the whole game's H1 combined, not just Drake's score — because bet_type is total, not team_total.
- Team total (bet_type=team_total, e.g. "Hornets team total over 117.5"): use ONLY the named team's score, not combined.
- Player prop: add the player's listed stats. Compare to line.
- Period bets (1H, 1Q, 2H): use ONLY the scores for that period shown in the data.
- UFC/MMA: use LAST NAME matching if the full name doesn't exactly match. "Alex Sola" matches "Axel Sola".
- Boxing/UFC moneyline: fighter wins the bout → WIN. Loses → LOSS. Scores may show "W"/"L" or numeric points — use winner field or highest score. Draw or No Contest → PUSH.
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


def fmt_cost(cost: float) -> str:
    """Format cost: $0.00 if zero, $0.0000 otherwise."""
    return f"${cost:.2f}" if cost == 0 else f"${cost:.4f}"


async def _claude_create_with_retry(**kwargs) -> object:
    """Call claude().messages.create with up to 4 retries on transient errors (500, 529).
    Accumulates token usage and prints the per-call cost."""
    for attempt in range(4):
        try:
            resp = await claude().messages.create(**kwargs)
            before = usage_cost()
            _accum(resp.usage)
            delta = usage_cost() - before
            if delta > 0:
                print(f"    [Claude] {fmt_cost(delta)}")
            return resp
        except (anthropic.InternalServerError, anthropic.APIStatusError) as exc:
            status = getattr(exc, "status_code", None)
            if status not in (500, 529) or attempt == 3:
                raise
            await asyncio.sleep(2 ** attempt)


async def claude_parse(text: str, date: str | None = None) -> dict | None:
    from datetime import date as _d
    d = _d.fromisoformat(date) if date else _d.today()
    day_name = d.strftime("%A")
    date_ctx = f"Context: Today is {day_name}.\n" if day_name else ""
    resp = await _claude_create_with_retry(
        model="claude-sonnet-4-6",
        max_tokens=500,
        messages=[{"role": "user", "content": _PARSE_PROMPT.format(text=text, date_context=date_ctx)}],
    )
    raw = re.sub(r"^```(?:json)?\n?|```$", "", resp.content[0].text.strip(), flags=re.MULTILINE).strip()
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        return None

    # Deterministic post-parse correction: Claude sometimes annotates the description
    # with "(KBO)" but leaves sport="Other". Override based on raw message text.
    if parsed and parsed.get("sport") == "Other" and "kbo" in text.lower():
        parsed["sport"] = "KBO"

    return parsed


async def claude_grade(pick_desc: str, date: str, context: str, bet_type: str = "") -> tuple[str, str]:
    """Returns (verdict, calc)."""
    resp = await _claude_create_with_retry(
        model="claude-sonnet-4-6",
        max_tokens=150,
        messages=[{
            "role": "user",
            "content": _GRADE_PROMPT.format(pick=pick_desc, date=date, context=context),
        }],
    )
    raw = resp.content[0].text.strip()
    # Try to parse JSON verdict first
    calc = ""
    try:
        clean = re.sub(r"^```(?:json)?\n?|```$", "", raw, flags=re.MULTILINE).strip()
        result = json.loads(clean)
        verdict = result.get("verdict", "").strip().upper()
        calc = result.get("calc", "")
        if verdict in ("WIN", "LOSS", "PUSH", "UNKNOWN"):
            # Sanity check: WIN/LOSS/PUSH must have numbers in calc (grader did math).
            # Moneylines have no score to cite, so skip the digit check for them.
            if verdict in ("WIN", "LOSS", "PUSH") and bet_type != "moneyline" and not re.search(r'\d', calc):
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
        events = await _fetch_scores_cached("Boxing", date)
        ctx = odds_api_context(fighter, events)
        return (ctx if ctx else CONTEXT_SKIP), date

    # KBO: Odds API scores (free tier = last ~3 days only)
    if sport == "KBO":
        team = teams[0] if teams else ""
        if not team:
            return CONTEXT_SKIP, date
        events = await _fetch_scores_cached("KBO", date)
        ctx = odds_api_context(team, events)
        if ctx:
            return ctx, date
        # No completed result — check if a matching game is scheduled/in-progress
        all_events = await _fetch_scores_cached("KBO", date, completed_only=False)
        if odds_api_context(team, all_events):
            return CONTEXT_PENDING, date
        return CONTEXT_SKIP, date

    # Other unknown sports → skip
    if sport not in ESPN_LEAGUES:
        return CONTEXT_SKIP, date

    # Props and period bets need game summaries.
    # NHL regulation/3-way moneylines also need line scores to detect OT periods.
    is_reg_ml = sport == "NHL" and is_regulation_ml(pick.get("description", ""))
    needs_summary = period != "game" or bet_type == "prop" or is_reg_ml

    if needs_summary and scoreboard:
        events = _completed_events(scoreboard)
        event_ids = find_event_ids(events, teams, player)

        # If no specific teams/player given, search all completed events.
        # If teams/player ARE given but not found in completed events, fall through
        # to the scoreboard path below which properly returns CONTEXT_PENDING/SKIP.
        if not event_ids and not (teams or player):
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
            # UFC: a bout may be completed even while the event card is still in progress.
            # Fall through to the scoreboard display if the specific bout is done.
            ufc_bout_done = sport == "UFC" and not relevant_ids and _ufc_bout_completed(scoreboard, teams, player)
            if relevant_ids or ufc_bout_done:
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
                prev_ufc_done = sport == "UFC" and not prev_ids and _ufc_bout_completed(prev_sb, teams, player)
                if prev_ids or prev_ufc_done:
                    display = prev_sb if sport == "UFC" else {"events": [e for e in prev_events if e.get("id") in set(prev_ids)]}
                    return scoreboard_text(display, sport), prev_date
                if find_event_ids(prev_sb.get("events", []), teams, player):
                    return CONTEXT_PENDING, prev_date
            # Scan the next 3 days for a scheduled matchup — always run this so picks posted
            # in multi-day messages (some games done, some future) are found correctly.
            for offset in range(1, 4):
                future_date = (_date.fromisoformat(date) + timedelta(days=offset)).isoformat()
                future_sb = await fetch_espn(sport, future_date)
                if not future_sb:
                    continue
                # UFC: if the specific bout is already completed on this future date, grade it now
                if sport == "UFC" and _ufc_bout_completed(future_sb, teams, player):
                    return scoreboard_text(future_sb, sport), future_date
                if find_event_ids(future_sb.get("events", []), teams, player):
                    return CONTEXT_PENDING, future_date
            return CONTEXT_SKIP, date
        completed = _completed_events(scoreboard)
        if not completed:
            return CONTEXT_SKIP, date
        return scoreboard_text({"events": completed}, sport), date

    # scoreboard is None — ESPN fetch failed (network/SSL error)
    return CONTEXT_ESPN_ERROR, date
