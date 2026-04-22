import re

import httpx

from common import VERDICT_EMOJI, parlay_combined_odds
from tracker_grading import _overall_verdict

_PICK_EMOJI = {k: v for k, v in VERDICT_EMOJI.items() if k in ("WIN", "LOSS", "PUSH")}
_ODDS_TAG_RE = re.compile(r'\s*\[[+-]\d{3,4}[^\]]*\]')
# Lines that look like bet lines: contain odds, units, spread/total numbers, or bet-type keywords
_BET_LINE_RE = re.compile(
    r'(?i)'
    r'(?:\[?[+-]\d{3,4}\]?'           # odds like +150, [-110]
    r'|\b\d+\.5\b'                     # half-point lines (spreads/totals)
    r'|\b\d+\s*units?\b'              # unit sizing
    r'|\bml\b|\bmoneyline\b'          # moneyline keywords
    r'|\bover\b|\bunder\b'              # totals
    r'|\b[+-]\d+(?:\.5)?\b'           # spreads like -3, +7.5
    r')'
)


def _pick_search_terms(pick: dict) -> list[str]:
    """Build lowercase search terms from a pick's teams/player fields."""
    player = pick.get("player") or ""
    teams = pick.get("teams") or []
    identifiers = [player] if player else teams
    terms: list[str] = []
    for t in identifiers:
        tl = t.lower().strip()
        if tl:
            terms.append(tl)
            terms.extend(w for w in tl.split() if len(w) > 3)
    return terms


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


def _match_pick_line(lines: list[str], pick: dict) -> int | None:
    """Find the line index for a pick using cascading fallbacks.

    1. Team/player name match (existing logic)
    2. Description match (e.g. "Cleveland Cavaliers 1H -2.5" in line)
    3. Stripped description (remove team words, match remainder like "1h -2.5")
    4. Bet line number (e.g. "-2.5", "236.5")
    5. Bet-line heuristic: odds, units, spread/total patterns — pick the
       best candidate line that looks like a pick line
    """
    def _available(i: int) -> bool:
        return not any(ch in lines[i] for ch in _PICK_EMOJI.values())

    # Pass 1: team/player name
    search_terms = _pick_search_terms(pick)
    for i, line in enumerate(lines):
        if not _available(i):
            continue
        line_lower = line.lower()
        if any(term in line_lower for term in search_terms):
            return i

    # Pass 2: full description
    desc = (pick.get("description") or "").lower().strip().replace("moneyline", "ml")
    if desc:
        for i, line in enumerate(lines):
            if not _available(i):
                continue
            if desc in line.lower():
                return i

    # Pass 3: description with team/player words stripped
    if desc:
        player = pick.get("player") or ""
        teams = pick.get("teams") or []
        identifiers = [player] if player else teams
        team_words = set()
        for t in identifiers:
            for w in t.lower().split():
                if len(w) > 3:
                    team_words.add(w)
        desc_stripped = desc
        for w in team_words:
            desc_stripped = desc_stripped.replace(w, "")
        desc_stripped = " ".join(desc_stripped.split())
        if len(desc_stripped) >= 3:
            for i, line in enumerate(lines):
                if not _available(i):
                    continue
                if desc_stripped in line.lower():
                    return i

    # Pass 4: raw line number (e.g. "-2.5", "236.5")
    pick_line = pick.get("line")
    if pick_line is not None:
        pick_line_f = float(pick_line)
        line_str = str(int(pick_line_f)) if pick_line_f == int(pick_line_f) else str(pick_line_f)
        for i, line in enumerate(lines):
            if not _available(i):
                continue
            if line_str in line:
                return i

    # Pass 5: find the best line that looks like a bet line (has odds, units,
    # spread numbers, etc.) but isn't a header/capper-name line.
    # Score each line by how many bet-line indicators it has.
    best_i, best_score = None, 0
    for i, line in enumerate(lines):
        if not _available(i):
            continue
        stripped = line.strip()
        if not stripped or len(stripped) < 3:
            continue
        score = len(_BET_LINE_RE.findall(stripped))
        if score > best_score:
            best_score = score
            best_i = i
    if best_i is not None:
        return best_i

    return None


def _insert_emojis(text: str, verdicts: list[tuple]) -> str:
    """
    Insert verdict emoji(s) into the message text.

    Parlay messages: add a single overall verdict emoji on the "Parlay:" header
    line (or after the last leg if no header found).  Per-leg emojis are NOT
    inserted — the parlay is a single bet.

    Non-parlay messages: insert per-pick verdict emojis inline after each
    pick's line, matched by team/player name.

    Lines that can't be matched are left unchanged.
    Returns the modified text (or original if nothing could be matched).
    """
    lines = text.rstrip().split("\n")

    parlay_verdicts = [v for v in verdicts if v[0].get("is_parlay_leg")]
    standalone_verdicts = [v for v in verdicts if not v[0].get("is_parlay_leg")]

    # ── Standalone picks: per-pick emoji ──────────────────────────────────────
    for pick, verdict, _calc, _sport, *_ in standalone_verdicts:
        emoji = _PICK_EMOJI.get(verdict)
        if not emoji:
            continue  # UNKNOWN / PENDING — leave line alone

        matched = _match_pick_line(lines, pick)
        if matched is not None:
            lines[matched] = f"{lines[matched].rstrip()}{emoji}"

    # ── Parlay: single overall emoji ──────────────────────────────────────────
    if parlay_verdicts:
        overall = _overall_verdict(parlay_verdicts)
        emoji = _PICK_EMOJI.get(overall)
        if emoji:
            # Prefer appending to the "Parlay:" header line
            placed = False
            for i, line in enumerate(lines):
                if "parlay" in line.lower() and not any(ch in line for ch in _PICK_EMOJI.values()):
                    lines[i] = f"{line.rstrip()}{emoji}"
                    placed = True
                    break

            if not placed:
                # Fallback: find the last leg line and append there
                last_idx = -1
                for pick, _verdict, _calc, _sport, *_ in parlay_verdicts:
                    search_terms = _pick_search_terms(pick)
                    for i, line in enumerate(lines):
                        if any(term in line.lower() for term in search_terms):
                            last_idx = max(last_idx, i)

                if last_idx >= 0 and not any(ch in lines[last_idx] for ch in _PICK_EMOJI.values()):
                    lines[last_idx] = f"{lines[last_idx].rstrip()}{emoji}"
                else:
                    lines.append(emoji)

    return "\n".join(lines)


def _fmt_odds_audit(pick: dict, sport: str, capper: str, result) -> str:
    fmt  = result.format() or "?"
    desc = pick.get("description", "")
    bk   = result.bookmaker or "?"
    lines = [
        f"📊 <b>odds</b>: {desc} → [{fmt}]",
        f"{result.match_type} · {bk}",
    ]
    if result.api_line is not None and result.pick_line is not None and result.api_line != result.pick_line:
        lines.append(f"api_line: {result.api_line} | pick_line: {result.pick_line}")
    lines.append(f"{sport} · {capper}")
    return "\n".join(lines)


def _insert_odds(text: str, picks: list[dict], odds_by_pick: dict) -> str:
    """
    Insert odds tags directly after each pick line, e.g. 'Duke -4.5 (-153)'.

    For parlays: inserts combined parlay price on the header line (the line
    containing "parlay" that isn't a leg bullet). Individual leg prices are
    not shown — only the combined payout odds.
    Idempotent — skips lines that already carry an odds tag.
    Uses same search-term logic as _insert_emojis.
    """
    parlay_idxs = [i for i, p in enumerate(picks) if p.get("is_parlay_leg")]
    standalone_idxs = [i for i, p in enumerate(picks) if not p.get("is_parlay_leg")]

    lines = text.rstrip().split("\n")

    if parlay_idxs:
        _leg_odds = [odds_by_pick.get(str(i), {}).get("odds") for i in parlay_idxs]
        _comb = parlay_combined_odds(_leg_odds)
        if _comb is not None:
            combined_tag = f" [{'+' if _comb > 0 else ''}{_comb}]"
            # Find the parlay/teaser header line (e.g. "Two Team Teaser / Parlay:")
            # Prefer this over team-name matching, which can misfire when a leg
            # line itself mentions multiple teams (e.g. "Pistons / Magic o208.5").
            header_j = -1
            for j, line in enumerate(lines):
                if re.search(r'\b(?:parlay|teaser)\b', line, re.IGNORECASE):
                    header_j = j
                    break
            # Fallback: line mentioning multiple leg teams
            if header_j < 0:
                leg_terms = []
                for i in parlay_idxs:
                    for t in (picks[i].get("teams") or []):
                        for w in t.lower().split():
                            if len(w) > 3:
                                leg_terms.append(w)
                best_j, best_count = -1, 0
                for j, line in enumerate(lines):
                    ll = line.lower()
                    hits = sum(1 for t in leg_terms if t in ll)
                    if hits > best_count:
                        best_count = hits
                        best_j = j
                if best_j >= 0 and best_count >= 2:
                    header_j = best_j
            if header_j >= 0 and not _ODDS_TAG_RE.search(lines[header_j]):
                lines[header_j] = f"{lines[header_j].rstrip()}{combined_tag}"

        # If no standalone picks, we're done
        if not standalone_idxs:
            return "\n".join(lines)

    def _fmt(v: int) -> str:
        return f"+{v}" if v > 0 else str(v)

    for idx, pick in enumerate(picks):
        if pick.get("is_parlay_leg"):
            continue  # parlay legs handled above via combined odds
        odds_val = odds_by_pick.get(str(idx), {}).get("odds")
        if odds_val is None:
            continue
        match_type  = odds_by_pick.get(str(idx), {}).get("match_type", "")
        pregame_val = odds_by_pick.get(str(idx), {}).get("pregame_odds")
        if match_type.startswith("live_"):
            odds_tag = f" [{_fmt(odds_val)} live]"
        elif match_type.startswith("pregame_"):
            odds_tag = f" [{_fmt(odds_val)} pre]"
        else:
            odds_tag = f" [{_fmt(odds_val)}]"

        search_terms = _pick_search_terms(pick)

        # Try description first: more specific than team/player fragments and avoids
        # false matches on game-info header lines (e.g. "Defenders @ Aviators / 8:00 PM").
        # Normalise "moneyline" → "ml" so AI-expanded descriptions match message abbreviations.
        desc = (pick.get("description") or "").lower().strip().replace("moneyline", "ml")
        desc_matched = False
        if desc:
            for j, line in enumerate(lines):
                if desc in line.lower():
                    if not _ODDS_TAG_RE.search(line):
                        lines[j] = f"{line.rstrip()}{odds_tag}"
                    desc_matched = True
                    break

        if not desc_matched:
            for j, line in enumerate(lines):
                if " @ " in line:  # skip game-info header (e.g. "Team A @ Team B / 8:00 PM")
                    continue
                if any(term in line.lower() for term in search_terms):
                    if _ODDS_TAG_RE.search(line):
                        break  # already tagged — idempotent
                    lines[j] = f"{line.rstrip()}{odds_tag}"
                    desc_matched = True
                    break

        # Third fallback: strip team/player names from desc and search for the remainder.
        # Catches abbreviations like "Dbacks ML (2 units)" when AI parsed "Arizona Diamondbacks ML".
        if not desc_matched and desc:
            player = pick.get("player") or ""
            teams = pick.get("teams") or []
            identifiers = [player] if player else teams
            team_words = set()
            for t in identifiers:
                for w in t.lower().split():
                    if len(w) > 3:
                        team_words.add(w)
            desc_stripped = desc
            for w in team_words:
                desc_stripped = desc_stripped.replace(w, "")
            desc_stripped = " ".join(desc_stripped.split())  # collapse whitespace
            if len(desc_stripped) >= 4:
                for j, line in enumerate(lines):
                    if " @ " in line:
                        continue
                    if desc_stripped in line.lower():
                        if not _ODDS_TAG_RE.search(line):
                            lines[j] = f"{line.rstrip()}{odds_tag}"
                        desc_matched = True
                        break

        # Fourth fallback: search for the raw bet line number (e.g. "236.5" or "-7.5").
        # Catches heavily abbreviated team names (e.g. "Twolves / Sixers under 236.5")
        # where no team name or description fragment survived the previous passes.
        if not desc_matched:
            pick_line = pick.get("line")
            if pick_line is not None:
                pick_line_f = float(pick_line)
                line_str = str(int(pick_line_f)) if pick_line_f == int(pick_line_f) else str(pick_line_f)
                for j, row in enumerate(lines):
                    if " @ " in row:
                        continue
                    if line_str in row:
                        if not _ODDS_TAG_RE.search(row):
                            lines[j] = f"{row.rstrip()}{odds_tag}"
                        desc_matched = True
                        break

    return "\n".join(lines)


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
