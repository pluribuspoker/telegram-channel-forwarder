import re

import httpx

from common import VERDICT_EMOJI
from tracker_grading import _overall_verdict

_PICK_EMOJI = {k: v for k, v in VERDICT_EMOJI.items() if k in ("WIN", "LOSS", "PUSH")}
_ODDS_TAG_RE = re.compile(r'\s*\[[+-]\d{3,4}[^\]]*\]')


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

    is_parlay = any(v[0].get("is_parlay_leg") for v in verdicts)

    if is_parlay:
        overall = _overall_verdict(verdicts)
        emoji = _PICK_EMOJI.get(overall)
        if not emoji:
            return text  # PENDING / UNKNOWN — nothing to insert yet

        # Prefer appending to the "Parlay:" header line
        for i, line in enumerate(lines):
            if "parlay" in line.lower() and not any(ch in line for ch in _PICK_EMOJI.values()):
                lines[i] = f"{line.rstrip()}{emoji}"
                return "\n".join(lines)

        # Fallback: find the last leg line and append there
        last_idx = -1
        for pick, _verdict, _calc, _sport, *_ in verdicts:
            if not pick.get("is_parlay_leg"):
                continue
            teams  = pick.get("teams") or []
            player = pick.get("player") or ""
            identifiers = [player] if player else teams
            search_terms: list[str] = []
            for t in identifiers:
                tl = t.lower().strip()
                if tl:
                    search_terms.append(tl)
                    search_terms.extend(w for w in tl.split() if len(w) > 3)
            for i, line in enumerate(lines):
                if any(term in line.lower() for term in search_terms):
                    last_idx = max(last_idx, i)

        if last_idx >= 0 and not any(ch in lines[last_idx] for ch in _PICK_EMOJI.values()):
            lines[last_idx] = f"{lines[last_idx].rstrip()}{emoji}"
        else:
            lines.append(emoji)
        return "\n".join(lines)

    # ── Non-parlay: per-pick emoji ────────────────────────────────────────────
    for pick, verdict, _calc, _sport, *_ in verdicts:
        emoji = _PICK_EMOJI.get(verdict)
        if not emoji:
            continue  # UNKNOWN / PENDING — leave line alone

        teams  = pick.get("teams") or []
        player = pick.get("player") or ""
        # For player props, search by player name only — team names appear as game
        # headers (e.g. "Pirates / Mets:") and would match the wrong line.
        # For team bets, search by team names.
        identifiers = [player] if player else teams
        search_terms: list[str] = []
        for t in identifiers:
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
    if any(p.get("is_parlay_leg") for p in picks):
        _leg_odds = [odds_by_pick.get(str(i), {}).get("odds") for i in range(len(picks))]
        _valid = [o for o in _leg_odds if o is not None]
        if len(_valid) != len(_leg_odds):
            return text  # partial odds — don't show misleading combined price
        _dec = 1.0
        for _o in _valid:
            _dec *= (_o / 100 + 1) if _o > 0 else (100 / abs(_o) + 1)
        _comb = round((_dec - 1) * 100) if _dec >= 2.0 else round(-100 / (_dec - 1))
        combined_tag = f" [{'+' if _comb > 0 else ''}{_comb}]"
        lines = text.rstrip().split("\n")
        for j, line in enumerate(lines):
            ll = line.lower().lstrip()
            if ll.startswith("•") or ll.startswith("-"):
                continue  # skip leg bullet lines
            if "parlay" not in ll:
                continue
            if _ODDS_TAG_RE.search(line):
                return text  # already tagged — idempotent
            lines[j] = f"{line.rstrip()}{combined_tag}"
            return "\n".join(lines)
        return text

    lines = text.rstrip().split("\n")

    def _fmt(v: int) -> str:
        return f"+{v}" if v > 0 else str(v)

    for idx, pick in enumerate(picks):
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

        teams  = pick.get("teams") or []
        player = pick.get("player") or ""
        identifiers = [player] if player else teams
        search_terms: list[str] = []
        for t in identifiers:
            tl = t.lower().strip()
            if tl:
                search_terms.append(tl)
                search_terms.extend(w for w in tl.split() if len(w) > 3)

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
