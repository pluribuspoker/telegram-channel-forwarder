"""
ai.py — Claude AI layer: parsing, grading, and context building.
"""

import asyncio
import json
import re

from datetime import date as _date, timedelta

import anthropic

from common import is_regulation_ml
from scores import (
    ESPN_LEAGUES,
    fetch_espn,
    fetch_espn_summary,
    fetch_cfl_context,
    fetch_kbo_context,
    fetch_odds_api_scores,
    fetch_soccer_context,
    fetch_tennis_match_context,
    odds_api_context,
    scoreboard_text,
    line_scores_text,
    box_score_text,
    find_event_ids,
    _completed_events,
    _ufc_bout_completed,
)


_claude: anthropic.AsyncAnthropic | None = None


def claude() -> anthropic.AsyncAnthropic:
    global _claude
    if _claude is None:
        # Explicit per-request timeout so a stalled request can never wedge a
        # caller indefinitely (the SDK default is 600s). The grade daemon runs
        # a persistent loop; without this a single hung request froze it for
        # ~35 min while systemd still reported it "active". 120s is ample for
        # grade/parse calls, which normally complete in a few seconds.
        _claude = anthropic.AsyncAnthropic(timeout=120.0)
    return _claude


# Sentinels returned by build_context to signal "no game data" vs "game not yet played"
CONTEXT_SKIP = "__SKIP__"
CONTEXT_PENDING = "__PENDING__"
CONTEXT_ESPN_ERROR = "__ESPN_ERROR__"  # ESPN fetch failed (network/SSL); retry next run

# Post-parse hint words that indicate Soccer when Claude returns sport="Other"
_SOCCER_HINTS = (
    "bundesliga", "epl", "premier league", "la liga", "serie a",
    "ligue 1", "champions league", "europa league", "soccer",
    "world cup", "fifa",
)

# Country/national team names → Soccer (FIFA) when Claude returns sport="Other"
_FIFA_COUNTRIES = {
    "japan", "brazil", "france", "germany", "argentina", "mexico", "england",
    "spain", "italy", "portugal", "netherlands", "holland", "south korea",
    "korea", "australia", "canada", "usa", "united states", "croatia",
    "morocco", "senegal", "cameroon", "ghana", "nigeria", "egypt", "tunisia",
    "saudi arabia", "qatar", "iran", "uruguay", "colombia", "chile", "ecuador",
    "peru", "paraguay", "costa rica", "panama", "honduras", "jamaica",
    "sweden", "denmark", "norway", "finland", "belgium", "switzerland",
    "austria", "poland", "ukraine", "serbia", "wales", "scotland", "ireland",
    "iceland", "czechia", "czech republic", "turkey", "turkiye", "greece",
    "romania", "hungary", "slovakia", "slovenia", "bosnia", "albania",
    "north macedonia", "georgia", "india", "china", "indonesia",
    "ivory coast", "congo", "algeria", "mali", "south africa",
    "new zealand", "venezuela", "bolivia",
}


# ─── Claude prompts ───────────────────────────────────────────────────────────

_PARSE_PROMPT = """\
Extract the sports betting pick(s) from this message. Ignore stats, records, and commentary.
Lines prefixed with '>' are blockquote commentary by the poster. If the main text states a pick and the blockquote contains a different spread/line/variation, extract the pick from the main (non-blockquoted) text.
{date_context}
Return JSON (no markdown fences):
{{
  "sport": "NBA|NCAAB|MLB|NFL|NHL|UFL|CFL|Tennis|UFC|Boxing|KBO|Soccer|Other",
  "picks": [
    {{
      "description": "concise one-line summary of the exact bet",
      "sport": null,
      "bet_type": "spread|moneyline|total|team_total|prop|double_chance|draw_no_bet",
      "is_parlay_leg": false,
      "period": "game|1h|2h|1q|2q|3q|4q|1p|2p|3p",
      "teams": ["Full canonical team/player name(s) — e.g. 'Oklahoma City Thunder' not 'OKC Thunder', 'Los Angeles Lakers' not 'LA Lakers'. For player props, this MUST contain the player's current team (e.g. Evan Mobley PRA → teams: ['Cleveland Cavaliers']) so the game can be located — never leave empty for a player prop."],
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
- CFL = Canadian Football League. Classify as CFL if the pick involves CFL team names or explicit CFL context. CFL teams (resolve nicknames to the canonical full name in "teams"): BC Lions, Calgary Stampeders, Edmonton Elks, Hamilton Tiger-Cats (Tiger Cats), Montreal Alouettes, Ottawa Redblacks, Saskatchewan Roughriders (Roughriders/Riders), Toronto Argonauts (Argonauts/Argos), Winnipeg Blue Bombers (Blue Bombers/Bombers). Note: "Lions" alone during CFL season (June–November) with CFL context is BC Lions, not Detroit Lions.
- KBO = Korean Baseball Organization. Classify as KBO if the pick involves KBO team names or explicit KBO context. KBO teams and their common nicknames/abbreviations (resolve any of these to the canonical full name in "teams"): KT Wiz (WIZ/KT), Samsung Lions (LIONS/SAMSUNG), LG Twins (TWINS/LG), Doosan Bears (BEARS/DOOSAN), Lotte Giants (GIANTS/LOTTE), NC Dinos (DINOS/NC), KIA Tigers (TIGERS/KIA), SSG Landers (LANDERS/SSG), Hanwha Eagles (EAGLES/HANWHA), Kiwoom Heroes (HEROES/KIWOOM). Critically: "LIONS" alone is ALWAYS Samsung Lions (not Lotte Giants), "GIANTS" alone is ALWAYS Lotte Giants, etc. Never invent an opponent that isn't in the message text.
- KBO/MLB team name collisions: "Tigers" (KIA Tigers KBO / Detroit Tigers MLB), "Giants" (Lotte Giants KBO / San Francisco Giants MLB), "Twins" (LG Twins KBO / Minnesota Twins MLB). When these names appear alone without explicit KBO context (the word "KBO", a Korean team prefix like KIA/LG/Lotte, or another KBO team as opponent), default to MLB. Only classify as KBO when the message explicitly says "KBO" or uses the full Korean team name (e.g. "KIA Tigers", "LG Twins", "Lotte Giants").
- Soccer/FIFA: if the pick involves country or national team names (e.g. Japan, Brazil, France, Germany, Argentina, Mexico, England, Spain, Italy, Portugal, Netherlands, USA, South Korea, Australia, Canada, etc.) as the team in a spread, moneyline, or total, classify as Soccer. Country names are never used for NBA/MLB/NFL/NHL/NCAAB teams — they always indicate international soccer (FIFA World Cup, friendlies, qualifiers). Use the full country name in "teams" (e.g. "South Korea" not "Korea").
- MLB/NFL team name collisions: "Cardinals" can be Arizona Cardinals (NFL) or St. Louis Cardinals (MLB). "Giants" can be New York Giants (NFL) or San Francisco Giants (MLB). To disambiguate: (1) If an opponent team is mentioned and belongs to only one sport (e.g. "Red Sox", "Dodgers" → MLB; "Cowboys", "Eagles" → NFL), use that sport. (2) If no opponent context: outside the NFL season (mid-February through early September), always resolve to MLB. During NFL season overlap (September through early February), prefer NFL on Sunday/Monday/Thursday (typical NFL game days) and MLB on Tuesday/Wednesday/Friday/Saturday. Always use the full canonical MLB name ("St. Louis Cardinals", "San Francisco Giants") or NFL name ("Arizona Cardinals", "New York Giants") in the teams field.
- If a single surname with a moneyline has no clear sport context and is not a known boxer or MMA fighter, default to UFC.
- For parlays: list each leg as a separate pick with its REAL bet_type (moneyline, spread, etc.) and set is_parlay_leg=true on each. Do NOT use bet_type="parlay". When players/teams are slash-separated (e.g. "FAA/Shapovalov MLP" or "SPURS/GARCIA MLP"), split them into ONE pick per player/team — do not put two teams in one pick's teams field. IMPORTANT: when multiple bets are combined on a single line with "+" or "&" (e.g. "Egypt Double Chance + Under 2.5 Goals"), that is a parlay — split into separate picks and set is_parlay_leg=true on each leg.
- Cross-sport parlays: if legs belong to different sports (e.g. one NBA team + one UFC fighter), set the pick-level "sport" field to override the top-level sport for that leg. Leave pick "sport" as null when it matches the top-level sport.
- Double chance: "X or Draw", "Draw or X", "X or Y" bets that cover two of three outcomes. Use bet_type="double_chance". Put the first-named team in "teams". line and direction should be null.
- Draw no bet (DNB): "X draw no bet", "X DNB". Like moneyline but draw = refund. Use bet_type="draw_no_bet". Put the team in "teams". line and direction should be null.
- Period: 1h=first half, 2h=second half, 1q=first quarter, 1p/2p/3p=hockey periods, game=full game (default).

Message:
{text}"""

_GRADE_PROMPT = """\
Grade this sports betting pick. Show your calculation, then give the verdict.

Pick: {pick}{prop_stat_line}
Date: {date}

Game data:
{context}

Rules by bet type:
- Spread (e.g. team -3.5 or team +3.5):
    * Team listed as -X is the FAVORITE. WIN if that team wins by MORE than X. LOSS if they win by less or lose. PUSH if exactly X.
    * Team listed as +X is the UNDERDOG. WIN if that team wins OUTRIGHT (regardless of margin) OR loses by LESS than X. LOSS if they lose by MORE than X. PUSH if they lose by exactly X.
    * Example: Ohio State +8, Ohio State wins outright → WIN (dog won, cover guaranteed).
- Double chance: covers two of three outcomes (e.g. "team or draw"). WIN if either covered outcome occurs. LOSS only if the one uncovered outcome occurs.
- Draw no bet (DNB): team wins → WIN. Team loses → LOSS. Draw → PUSH.
- Moneyline: did the picked team/fighter win outright?
- NHL regulation/3-way moneyline (pick description contains "3-way", "60 min", "regulation", "reg ML", etc.): team must win in REGULATION only. If the score data shows OT=1 (or any OT column with a non-zero value), or P4 or more periods, the game went to overtime — the pick is a LOSS regardless of who won in OT.
- Soccer moneyline is 3-way: team must win outright. A draw (in regulation or otherwise) is a LOSS, not a push. Only "draw no bet" (DNB) pushes on a draw.
- Soccer extra time: When the status shows "AET" (After Extra Time) or "PEN" (penalties), a "(Regulation 90': ...)" note shows the 90-minute score. For moneyline and total bets, use the REGULATION score, NOT the final AET score. Extra-time goals do not count. Exception: if the pick says "to advance" or "to qualify", use the final result (the team that advances wins).
- Total over/under (bet_type=total): ALWAYS add BOTH teams' scores regardless of how the pick is worded. score_A + score_B = combined. Compare combined to line. Even "Drake 1H Over 62.5" means the whole game's H1 combined, not just Drake's score — because bet_type is total, not team_total.
- Team total (bet_type=team_total, e.g. "Hornets team total over 117.5"): use ONLY the named team's score, not combined.
- Player prop (bet_type=prop): sum ONLY the stats explicitly named in the "Prop stat:" field. Do NOT include any other stats shown in the box score. Stat abbreviations decompose on '+' and '/'. Basketball: PTS=points, REB=rebounds, AST=assists, STL=steals, BLK=blocks, 3PM/3PT=three pointers, TO/TOV=turnovers. Baseball: H/HITS=hits, HR=homeRuns, R=runs, RBI=RBIs, BB=walks, AB=atBats. Examples: "PTS+REB+AST" → add ONLY PTS+REB+AST (ignore STL/BLK even if shown); "PRA" → PTS+REB+AST; "PR" → PTS+REB; "P+A" → PTS+AST. Show each component number in calc, e.g. "PTS(10)+REB(6)+AST(1)=17 vs 29.5".
- Period bets (1H, 1Q, 2H, 1P/2P/3P for hockey): use ONLY the scores for that period shown in the data.
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


def _salvage_truncated(raw: str) -> dict | None:
    """Try to recover completed picks from truncated JSON output."""
    # Find the last complete object in the picks array
    idx = raw.rfind('},')
    if idx == -1:
        return None
    # Close the array and outer object
    candidate = raw[:idx + 1] + '\n  ]\n}'
    try:
        parsed = json.loads(candidate)
        if parsed.get("picks"):
            return parsed
    except json.JSONDecodeError:
        pass
    return None


async def claude_parse(text: str, date: str | None = None) -> dict | None:
    from datetime import date as _d
    d = _d.fromisoformat(date) if date else _d.today()
    day_name = d.strftime("%A")
    date_ctx = f"Context: Today is {day_name}, {d.strftime('%B')} {d.day}.\n" if day_name else ""
    resp = await _claude_create_with_retry(
        model="claude-sonnet-4-6",
        max_tokens=1500,
        messages=[{"role": "user", "content": _PARSE_PROMPT.format(text=text, date_context=date_ctx)}],
    )
    raw = re.sub(r"^```(?:json)?\n?|```$", "", resp.content[0].text.strip(), flags=re.MULTILINE).strip()
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        # Truncated JSON — try to salvage completed picks
        parsed = _salvage_truncated(raw)
        if not parsed:
            return None

    # Deterministic post-parse correction: Claude sometimes annotates the description
    # with "(KBO)" but leaves sport="Other". Override based on raw message text.
    if parsed and parsed.get("sport") == "Other" and "kbo" in text.lower():
        parsed["sport"] = "KBO"

    # Reverse: KBO with ambiguous team names (Tigers/Giants/Twins) but no explicit
    # KBO context in the message → default to MLB. Real KBO picks say "KBO" or use
    # full Korean names (KIA Tigers, LG Twins, Lotte Giants).
    _KBO_MLB_OVERLAP = {"tigers", "giants", "twins"}
    if parsed and parsed.get("sport") == "KBO" and "kbo" not in text.lower():
        all_teams = []
        for pick in parsed.get("picks", []):
            all_teams.extend(t.lower() for t in pick.get("teams", []))
        # Only override if every team is an ambiguous overlap name
        if all_teams and all(
            any(frag in t for frag in _KBO_MLB_OVERLAP) for t in all_teams
        ):
            parsed["sport"] = "MLB"

    if parsed and parsed.get("sport") == "Other" and "cfl" in text.lower():
        parsed["sport"] = "CFL"

    # CFL team names misclassified as another sport (e.g., "Blue Bombers" parsed
    # as MLB "Blue Jays"). Check pick descriptions for uniquely-CFL fragments.
    _CFL_UNIQUE_TEAMS = [
        ("blue bombers", "Winnipeg Blue Bombers"),
        ("stampeders", "Calgary Stampeders"),
        ("argonauts", "Toronto Argonauts"),
        ("alouettes", "Montreal Alouettes"),
        ("redblacks", "Ottawa Redblacks"),
        ("roughriders", "Saskatchewan Roughriders"),
        ("tiger-cats", "Hamilton Tiger-Cats"),
        ("tiger cats", "Hamilton Tiger-Cats"),
    ]
    if parsed and parsed.get("sport") != "CFL":
        for pick in parsed.get("picks", []):
            desc_lower = pick.get("description", "").lower()
            for frag, canonical in _CFL_UNIQUE_TEAMS:
                if frag in desc_lower:
                    parsed["sport"] = "CFL"
                    pick["teams"] = [canonical]
                    break

    if parsed and parsed.get("sport") == "Other":
        tl = text.lower()
        if any(h in tl for h in _SOCCER_HINTS):
            parsed["sport"] = "Soccer"

    # Country/national team names in parsed teams → Soccer (FIFA)
    if parsed and parsed.get("sport") == "Other":
        for pick in parsed.get("picks", []):
            if any(t.lower() in _FIFA_COUNTRIES for t in pick.get("teams", [])):
                parsed["sport"] = "Soccer"
                break

    return parsed


async def claude_grade(pick_desc: str, date: str, context: str, bet_type: str = "", prop_stat: str = "") -> tuple[str, str]:
    """Returns (verdict, calc)."""
    prop_stat_line = f"\nProp stat: {prop_stat}" if prop_stat else ""
    resp = await _claude_create_with_retry(
        model="claude-sonnet-4-6",
        max_tokens=500,
        messages=[{
            "role": "user",
            "content": _GRADE_PROMPT.format(pick=pick_desc, date=date, context=context, prop_stat_line=prop_stat_line),
        }],
    )
    raw = resp.content[0].text.strip()
    # If the response was truncated, don't trust the text for verdict extraction
    truncated = getattr(resp, "stop_reason", None) == "max_tokens"
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
    # If truncated, JSON was incomplete — don't scan raw text for stray verdict words
    if truncated:
        return "UNKNOWN", raw
    # Fallback: find LAST valid verdict word in response (first may be from rule explanation)
    last_verdict = None
    for word in re.sub(r"[^A-Z\s]", "", raw.upper()).split():
        if word in ("WIN", "LOSS", "PUSH", "UNKNOWN"):
            last_verdict = word
    if last_verdict:
        return last_verdict, raw
    return "UNKNOWN", raw


# ─── Grade context builder ────────────────────────────────────────────────────

async def build_context(
    sport: str,
    date: str,
    pick: dict,
    scoreboard: dict | None,
    summary_cache: dict,
    odds_game_date: str | None = None,
    msg_date: str | None = None,
) -> tuple[str, str]:
    """Return (context_str, game_date) for grading this pick.
    game_date is the actual date the game is/was played (may differ from pick date).
    msg_date is the original message date (before odds/day-hint overrides)."""
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

    # KBO: koreabaseball.com scores (Odds API never populates KBO results)
    if sport == "KBO":
        team = teams[0] if teams else ""
        if not team:
            return CONTEXT_SKIP, date
        ctx, game_date = await fetch_kbo_context(team, date, odds_game_date=odds_game_date)
        if ctx == "PENDING":
            return CONTEXT_PENDING, game_date
        return (ctx if ctx else CONTEXT_PENDING), game_date

    # CFL: cfl.ca scores (ESPN has no CFL data)
    if sport == "CFL":
        team = teams[0] if teams else ""
        if not team:
            return CONTEXT_SKIP, date
        ctx, game_date = await fetch_cfl_context(team, date, odds_game_date=odds_game_date)
        if ctx == "PENDING":
            return CONTEXT_PENDING, game_date
        return (ctx if ctx else CONTEXT_PENDING), game_date

    # Soccer: search ESPN across multiple leagues
    if sport == "Soccer":
        if not teams:
            return CONTEXT_SKIP, date
        needs_stats = bool(pick.get("prop_stat"))
        ctx, game_date = await fetch_soccer_context(teams, date, include_stats=needs_stats)
        if ctx == "PENDING":
            return CONTEXT_PENDING, game_date
        return (ctx if ctx else CONTEXT_SKIP), game_date

    # Other unknown sports → skip
    if sport not in ESPN_LEAGUES:
        return CONTEXT_SKIP, date

    # Props and period bets need game summaries.
    # NHL regulation/3-way moneylines also need line scores to detect OT periods.
    is_reg_ml = sport == "NHL" and is_regulation_ml(pick.get("description", ""))
    needs_summary = period != "game" or bet_type == "prop" or is_reg_ml
    # True when the date was overridden (e.g. by odds_game_date) from the
    # original message date — signals that the prev-day fallback is safe.
    date_was_overridden = msg_date and date != msg_date

    if needs_summary and scoreboard:
        events = _completed_events(scoreboard)
        event_ids = find_event_ids(events, teams, player)

        # If no specific teams/player given, search all completed events.
        # If teams/player ARE given but not found in completed events, fall through
        # to the scoreboard path below which properly returns CONTEXT_PENDING/SKIP.
        if not event_ids and not (teams or player):
            event_ids = [e.get("id") for e in events if e.get("id")]

        # No completed game on the primary date — try the previous day, but only
        # when the date was overridden.  This handles consecutive-day series where
        # the Odds API returns a game_date one day later than the actual game
        # (message sent May 8, odds say May 9, but the completed game is on
        # May 8's ESPN schedule).
        if not event_ids and (teams or player) and date_was_overridden:
            prev_date = (_date.fromisoformat(date) - timedelta(days=1)).isoformat()
            prev_sb = await fetch_espn(sport, prev_date)
            if prev_sb:
                prev_completed = _completed_events(prev_sb)
                prev_ids = find_event_ids(prev_completed, teams, player)
                if prev_ids:
                    event_ids = prev_ids
                    scoreboard = prev_sb
                    date = prev_date

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
                parts.append(line_scores_text(summary, sport))

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
            # No completed match — check if game/bout exists but hasn't started/finished yet.
            # Before returning PENDING, check the previous day for a completed game
            # when the date was overridden (consecutive-day series with wrong odds date).
            if find_event_ids(scoreboard.get("events", []), teams, player):
                if date_was_overridden:
                    prev_date = (_date.fromisoformat(date) - timedelta(days=1)).isoformat()
                    prev_sb = await fetch_espn(sport, prev_date)
                    if prev_sb:
                        prev_events = _completed_events(prev_sb)
                        prev_ids = find_event_ids(prev_events, teams, player)
                        prev_ufc_done = sport == "UFC" and not prev_ids and _ufc_bout_completed(prev_sb, teams, player)
                        if prev_ids or prev_ufc_done:
                            display = prev_sb if sport == "UFC" else {"events": [e for e in prev_events if e.get("id") in set(prev_ids)]}
                            return scoreboard_text(display, sport), prev_date
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
                # If the game is already completed on this future date, grade it now
                future_completed = _completed_events(future_sb)
                future_ids = find_event_ids(future_completed, teams, player)
                if future_ids:
                    display = future_sb if sport == "UFC" else {"events": [e for e in future_completed if e.get("id") in set(future_ids)]}
                    return scoreboard_text(display, sport), future_date
                # UFC: a bout may be completed even while the event card is still in progress
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
