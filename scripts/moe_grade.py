#!/usr/bin/env python3
"""Grade every approved MOE opinion against ESPN finals and closing lines.

Prints the per-expert scoreboard (Brier, ATS and O/U at close, leg record,
closing-line value), then the God Expert disagreement report: per game
graded for both arms, each arm's Brier, whether the legs agreed, and who was
right where they differed; season totals give the paired Brier difference
with its standard error, the leg agreement rate, and the disagreement
record. With --write, appends one row per graded opinion to the append-only
``moe_grades`` tab, then one ``mean_of_arms`` row per paired game (the
bake-off's free third row: a ledger row, never an expert), skipping opinion
ids already present. With --notify as well, posts the digest — per game:
the final, the closing spread/total the picks were graded against, and
each expert's graded side, line, and bet legs with ✅/❌/♻️ results, then
the season scoreboard — to the desk group's Scores topic when one is
configured (moe_desk.py), else DMs the operator through the watchdog bot,
only when rows were appended (the run ``moe-grade.timer`` makes daily); a
digest that reaches neither after a successful append exits non-zero so
the healthcheck alerts.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timezone
from html import escape as _escape_html
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
load_dotenv(ROOT / ".env.local")
load_dotenv(ROOT / ".env")

from moe import approved_opinions, configured_opinion_store
from celebrity_grades import (
    build_celebrity_grade_rows,
    configured_celebrity_grade_store,
)
from celebrity_picks import CELEBRITY_HEADERS
from intake_bot import _celebrity_worksheet
from moe_god import (
    GRADE_HEADERS,
    GRADES_TAB,
    MEAN_OF_ARMS_ID,
    aggregator_policy,
    arm_pairs,
    build_scoreboard,
    closing_market,
    disagreement_report,
    format_disagreement_report,
    grade_all,
    ledger_row,
    load_registry,
    mean_of_arms_results,
)
from nfl_game_history import (
    GAME_HISTORY_HEADERS,
    GAME_HISTORY_TAB,
    build_game_history,
    fetch_regular_season_events,
)
from nfl_game_annotations import (
    attach_game_annotations,
    game_annotation_context,
    load_game_annotations,
)
from nfl_lines import (
    LEAN_HEADERS,
    SNAPSHOT_HEADERS,
    _call_with_retry,
    get_gspread_client,
)
from nfl_win_predictions import ensure_worksheet
from scripts.generate_moe_opinion import _latest_alignment
from scripts.god_judge_runner import send_watchdog_dm


def deliver_notification(text: str, html: str | None = None) -> bool:
    """The desk group's Scores topic when configured, else the watchdog DM.

    The Scores post prefers the pre-rendered ``html`` digest; the watchdog
    DM is sent without a parse mode, so it always gets the plain ``text``.
    """
    from moe_desk import post_scores_notice

    if post_scores_notice(text, html=html):
        return True
    return send_watchdog_dm(text)


# Telegram caps a message at 4096 characters; leave room for the ellipsis.
NOTIFY_MAX_CHARS = 4000
NOTIFY_MAX_GAMES = 20

# The pipeline's verdict emojis (VERDICT_EMOJI in common.py keys on
# WIN/LOSS/PUSH; the grade ledger stores W/L/P).
_RESULT_EMOJI = {"W": "✅", "L": "❌", "P": "♻️"}


def _nick(team: str) -> str:
    """'Seattle Seahawks' → 'Seahawks'. NFL nicknames are one word."""
    return str(team or "").rsplit(" ", 1)[-1]


def _num(value) -> float | None:
    """A sheet cell as a float, or None — cells arrive as '', str, int, float."""
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _record_line(expert_id: str, record: dict) -> str:
    """One season-to-date line per expert, phone-width (no aligned columns)."""
    parts = [f"n{record['resolved']}"]
    if record["brier"] is not None:
        parts.append(f"B {record['brier']:.4f}")
    ats, ou, legs = record["ats"], record["ou"], record["legs"]
    parts.append(f"ats {ats['w']}-{ats['l']}-{ats['p']}")
    parts.append(f"ou {ou['w']}-{ou['l']}-{ou['p']}")
    if any(legs.values()):
        parts.append(f"legs {legs['w']}-{legs['l']}-{legs['p']}")
    if record["clv_points_mean"] is not None:
        parts.append(f"clv {record['clv_points_mean']:+.2f}/{record['clv_legs']}")
    return f"{expert_id} · " + " · ".join(parts)


def _passed_pick(value) -> bool:
    """True when a persisted pick json explicitly declares selection PASS."""
    if value in (None, ""):
        return False
    try:
        parsed = json.loads(value) if isinstance(value, str) else value
    except (TypeError, ValueError):
        return False
    return isinstance(parsed, dict) and str(parsed.get("selection")) == "PASS"


def _bet_line(row: dict, opinion: dict) -> str | None:
    """'↳ bet: Seahawks -3 ♻️ (clv +0)' from the row's graded legs.

    Legs are the actual policy bets (the God arms, leg-bearing voices like
    AK); a row whose opinion declared picks but graded no leg bet nothing —
    that PASS is worth a line, silence is not.
    """
    legs: list[str] = []
    side_sel = str(row.get("side_selection") or "")
    if side_sel:
        line = _num(row.get("side_line"))
        part = _nick(side_sel) + (f" {line:+g}" if line is not None else "")
        part += f" {_RESULT_EMOJI.get(str(row.get('side_result') or ''), '')}"
        clv = _num(row.get("side_clv_points"))
        if clv is not None:
            part += f" (clv {clv:+.1f})"
        legs.append(part.strip())
    total_sel = str(row.get("total_selection") or "")
    if total_sel:
        line = _num(row.get("total_line"))
        label = {"Over": "O", "Under": "U"}.get(total_sel, total_sel)
        part = label + (f" {line:g}" if line is not None else "")
        part += f" {_RESULT_EMOJI.get(str(row.get('total_result') or ''), '')}"
        clv = _num(row.get("total_clv_points"))
        if clv is not None:
            part += f" (clv {clv:+.1f})"
        legs.append(part.strip())
    if legs:
        return "↳ bet: " + " · ".join(legs)
    if _passed_pick(opinion.get("side_pick_json")) or _passed_pick(
        opinion.get("total_pick_json")
    ):
        return "↳ bet: PASS"
    return None


def _expert_lines(
    row: dict, opinion: dict, closing: dict | None, away: str, home: str
) -> list[str]:
    """One stance line per graded row — the side and line it was graded on.

    ATS is the predicted winner taken at that side's closing spread; O/U is
    the projected-total lean against the closing total (the exact grading
    rules in moe_god.grade_opinion_row) — so what reads '{expert} 20-24:
    Seahawks -3 ♻️ · U 44.5 ✅ · B 0.1444' IS the grade, spelled out.
    """
    parts: list[str] = []
    winner = str(opinion.get("predicted_winner") or "")
    ats = str(row.get("ats_at_close") or "")
    if ats:
        spread = None
        if closing is not None and winner in (away, home):
            spread = _num(
                closing.get("home_spread")
                if winner == home
                else closing.get("away_spread")
            )
        label = (
            f"{_nick(winner)} {spread:+g}"
            if spread is not None
            else (_nick(winner) if winner else "ats")
        )
        parts.append(f"{label} {_RESULT_EMOJI.get(ats, ats)}")
    ou = str(row.get("ou_at_close") or "")
    if ou:
        total = _num((closing or {}).get("total"))
        away_proj = _num(opinion.get("predicted_away_score"))
        home_proj = _num(opinion.get("predicted_home_score"))
        lean = ""
        if total is not None and away_proj is not None and home_proj is not None:
            lean = "O" if away_proj + home_proj > total else "U"
        label = f"{lean} {total:g}" if lean and total is not None else "o/u"
        parts.append(f"{label} {_RESULT_EMOJI.get(ou, ou)}")
    brier = _num(row.get("brier"))
    if brier is not None:
        parts.append(f"B {brier:.4f}")
    proj = ""
    away_proj = _num(opinion.get("predicted_away_score"))
    home_proj = _num(opinion.get("predicted_home_score"))
    if away_proj is not None and home_proj is not None:
        proj = f" {away_proj:g}-{home_proj:g}"
    head = f"{row['expert_id']}{proj}: " + (" · ".join(parts) if parts else "graded")
    lines = [head]
    bet = _bet_line(row, opinion)
    if bet:
        lines.append(bet)
    return lines


def notification_text(
    *,
    season: int,
    scoreboard: dict,
    new_rows: list[dict],
    finals: list[dict] | None = None,
    opinions: list[dict] | None = None,
    snapshots: list[dict] | None = None,
    html: bool = False,
) -> str:
    """The grading digest for a run that appended rows.

    Season header with the ledger delta, then one block per graded game (in
    ledger order, capped at NOTIFY_MAX_GAMES): the final, the closing spread
    and total the picks were graded against, and one line per expert — its
    projected score, the side and line of its ATS/O-U grades with ✅/❌/♻️
    results, and its actual bet legs (or PASS) where it placed any. When a
    game produced several rows for one expert (the arms re-run per
    committee), only the latest shows. The season-to-date scoreboard closes
    the message. Blocks degrade to bare header lines from the end when the
    full text would pass Telegram's limit.

    ``html=True`` renders the Bot API HTML the Scores topic gets — the same
    content visually chunked (operator-asked, 2026-09-10: the flat version
    still read as one wall): bold section and game headers, each game's
    expert lines in a <blockquote> (Telegram's indent bar separates the
    games), the expert bolded at the start of its line, and the season
    scoreboard collapsed into a <blockquote expandable>. Plain mode is what
    the watchdog-DM fallback sends, tag-free.
    """
    opinions = list(opinions or [])
    by_id = {str(op.get("opinion_id") or ""): op for op in opinions}
    mean_count = sum(1 for row in new_rows if row["expert_id"] == MEAN_OF_ARMS_ID)
    finals_by_event = {
        str(row.get("event_id")): row for row in (finals or [])
    }

    rows_by_event: dict[str, list[dict]] = {}
    event_order: list[str] = []
    for row in new_rows:
        event_id = str(row.get("event_id"))
        if event_id not in rows_by_event:
            event_order.append(event_id)
        rows_by_event.setdefault(event_id, []).append(row)

    def _closing_for(event_id: str) -> dict | None:
        if not snapshots:
            return None
        opinion = next(
            (
                op
                for op in opinions
                if str(op.get("event_id")) == event_id
                and op.get("commence_time_utc")
            ),
            None,
        )
        if opinion is None:
            return None
        try:
            return closing_market(
                event_id, str(opinion["commence_time_utc"]), snapshots
            )
        except Exception:
            return None

    def _latest_per_expert(rows: list[dict]) -> list[dict]:
        best: dict[str, tuple[tuple[str, str], dict]] = {}
        for row in rows:
            opinion = by_id.get(str(row.get("opinion_id") or "")) or {}
            key = (
                str(opinion.get("generated_at_utc") or ""),
                str(row.get("opinion_id") or ""),
            )
            expert_id = str(row.get("expert_id"))
            if expert_id not in best or key > best[expert_id][0]:
                best[expert_id] = (key, row)
        return [
            row
            for _, row in sorted(
                best.values(),
                key=lambda item: (
                    item[1]["expert_id"] == MEAN_OF_ARMS_ID,
                    item[1]["expert_id"],
                ),
            )
        ]

    footnotes: list[str] = []
    blocks: list[list[str]] = []
    for event_id in event_order[:NOTIFY_MAX_GAMES]:
        rows = rows_by_event[event_id]
        final_ctx = finals_by_event.get(event_id, {})
        annotations = list(final_ctx.get("game_annotations") or [])
        for annotation in annotations:
            note = f"* {annotation['summary']}"
            if note not in footnotes:
                footnotes.append(note)
        first = rows[0]
        away, home = str(first["away_team"]), str(first["home_team"])
        away_score, _, home_score = str(first.get("final") or "").partition("-")
        marker = "*" if annotations else ""
        header = (
            f"🏈 {_nick(away)} {away_score} @ {_nick(home)} {home_score}{marker}"
        )
        week = first.get("week")
        if week not in (None, ""):
            header += f" · Week {week}"
        closing = _closing_for(event_id)
        home_spread = _num((closing or {}).get("home_spread"))
        total = _num((closing or {}).get("total"))
        close_parts = [
            part
            for part in (
                f"{_nick(home)} {home_spread:+g}" if home_spread is not None else "",
                f"total {total:g}" if total is not None else "",
            )
            if part
        ]
        block = [
            header,
            "close: " + (" · ".join(close_parts) if close_parts else "unavailable"),
        ]
        for row in _latest_per_expert(rows):
            opinion = by_id.get(str(row.get("opinion_id") or "")) or {}
            block.extend(_expert_lines(row, opinion, closing, away, home))
        blocks.append(block)

    header_lines = [
        f"pickbot: MOE grades · season {season}",
        f"{scoreboard['resolved_games']} resolved "
        f"game{'s' if scoreboard['resolved_games'] != 1 else ''} · "
        f"{scoreboard['graded_opinions']} graded opinions",
        f"+{len(new_rows) - mean_count} opinion rows, +{mean_count} mean-of-arms "
        f"→ {GRADES_TAB}",
    ]
    extra_games = len(event_order) - len(blocks)
    season_lines = ["season so far:"] + [
        _record_line(expert_id, record)
        for expert_id, record in sorted(scoreboard["by_expert"].items())
    ]

    esc = _escape_html if html else (lambda value: value)

    def _bold(line: str) -> str:
        return f"<b>{esc(line)}</b>" if html else line

    def _expert_html(line: str) -> str:
        # '↳ bet: …' stays plain; 'ak 20-24: …' bolds its prefix through the
        # colon so each expert anchors its own line inside the quote.
        if line.startswith("↳"):
            return esc(line)
        head, sep, rest = line.partition(": ")
        if not sep:
            return esc(line)
        return f"<b>{esc(head)}:</b> {esc(rest)}"

    def _assemble(detailed: int) -> str:
        lines = [_bold(header_lines[0])]
        lines.extend(esc(line) for line in header_lines[1:])
        for i, block in enumerate(blocks):
            lines.append("")
            lines.append(_bold(block[0]))
            if i >= detailed:
                continue
            if html:
                lines.append(esc(block[1]))
                if block[2:]:
                    quoted = "\n".join(_expert_html(line) for line in block[2:])
                    lines.append(f"<blockquote>{quoted}</blockquote>")
            else:
                lines.extend(block[1:])
        if extra_games:
            lines.append(esc(f"+{extra_games} more games"))
        if footnotes:
            lines.extend(["", *(esc(note) for note in footnotes)])
        if html:
            season_records = "\n".join(
                f"<b>{esc(head)}</b> · {esc(rest)}" if sep else esc(line)
                for line in season_lines[1:]
                for head, sep, rest in [line.partition(" · ")]
            )
            lines.extend(
                ["", _bold("season so far"),
                 f"<blockquote expandable>{season_records}</blockquote>"]
            )
        else:
            lines.extend(["", *season_lines])
        return "\n".join(lines)

    # Full detail if it fits; otherwise degrade trailing games to their bare
    # score-header line (never drop a game), and hard-truncate only as the
    # last resort. A hard truncation could cut an HTML tag open, so the html
    # mode's last resort is the tag-free plain render instead — parse_mode
    # HTML shows it as-is (the digest never contains <, > or &).
    for detailed in range(len(blocks), -1, -1):
        text = _assemble(detailed)
        if len(text) <= NOTIFY_MAX_CHARS:
            return text
    if html:
        return notification_text(
            season=season,
            scoreboard=scoreboard,
            new_rows=new_rows,
            finals=finals,
            opinions=opinions,
            snapshots=snapshots,
        )
    return text[: NOTIFY_MAX_CHARS - 1] + "…"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--season",
        type=int,
        default=None,
        help="Season to grade (defaults to the latest season in nfl_games).",
    )
    parser.add_argument(
        "--write",
        action="store_true",
        help="Append graded opinions to the moe_grades tab.",
    )
    parser.add_argument(
        "--notify",
        action="store_true",
        help=(
            "After --write appended rows, DM the scoreboard through the "
            "watchdog bot (WATCHDOG_BOT_TOKEN / WATCHDOG_USER_ID). No-op "
            "without --write or without new rows; a failed DM exits non-zero."
        ),
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help=(
            "Print the scoreboard, the disagreement report, and the "
            "mean-of-arms grades as JSON instead of text."
        ),
    )
    args = parser.parse_args()

    credentials = os.environ.get("GOOGLE_CREDENTIALS", "")
    sheet_id = os.environ.get("NFL_INTAKE_SHEET_ID", "")
    if not credentials or not sheet_id:
        raise RuntimeError(
            "GOOGLE_CREDENTIALS and NFL_INTAKE_SHEET_ID are required"
        )
    spreadsheet = get_gspread_client(credentials).open_by_key(sheet_id)
    history = spreadsheet.worksheet(GAME_HISTORY_TAB).get_all_records(
        expected_headers=GAME_HISTORY_HEADERS
    )
    annotation_rows = load_game_annotations(spreadsheet)
    history = attach_game_annotations(history, annotation_rows)
    rows = configured_opinion_store().list()
    approved = approved_opinions(rows)
    seasons = sorted(
        {int(row["season"]) for row in approved if str(row.get("season") or "").strip()}
    )
    season = args.season or (seasons[-1] if seasons else datetime.now().year)
    events = fetch_regular_season_events(season, expected_games=None)
    finals = attach_game_annotations(
        build_game_history(
            {season: events},
            {season: _latest_alignment(history)},
            validate=False,
            require_complete_divisional_pairs=False,
        ),
        annotation_rows,
    )
    celebrity_rows = _celebrity_worksheet(spreadsheet).get_all_records(
        expected_headers=CELEBRITY_HEADERS
    )
    leans = spreadsheet.worksheet("nfl_leans").get_all_records(
        expected_headers=LEAN_HEADERS
    )
    celebrity_grades = build_celebrity_grade_rows(
        celebrity_rows,
        leans,
        history,
        preferred_finals=finals,
        season=season,
        graded_at_utc=datetime.now(timezone.utc).isoformat(),
    )
    snapshots = spreadsheet.worksheet("nfl_line_snapshots").get_all_records(
        expected_headers=SNAPSHOT_HEADERS
    )
    registry = load_registry()
    policy = aggregator_policy(registry)
    graded_at = datetime.now(timezone.utc).isoformat()
    scoreboard = build_scoreboard(
        approved,
        finals=finals,
        snapshots=snapshots,
        registry=registry,
        policy=policy,
        as_of=graded_at,
    )
    graded = grade_all(
        approved,
        finals=finals,
        snapshots=snapshots,
        registry=registry,
        policy=policy,
    )
    pairs = arm_pairs(approved, graded)
    report = disagreement_report(pairs)
    means = mean_of_arms_results(pairs, finals=finals, snapshots=snapshots)
    if args.json:
        print(
            json.dumps(
                {
                    "scoreboard": scoreboard,
                    "disagreement": report,
                    "mean_of_arms": means,
                    "game_annotation_context": game_annotation_context(
                        finals,
                        deterministic_treatment="include",
                    ),
                },
                indent=2,
                sort_keys=True,
            )
        )
    else:
        print(
            f"Season {season}: {scoreboard['resolved_games']} resolved games, "
            f"{scoreboard['graded_opinions']} graded opinions "
            f"({len(approved)} approved rows, {len(finals)} finals)."
        )
        for expert_id, record in sorted(scoreboard["by_expert"].items()):
            print(_record_line(expert_id, record))
        print()
        for line in format_disagreement_report(report):
            print(line)
        for game in finals:
            for annotation in game.get("game_annotations") or []:
                print(
                    f"* {game['away_team']} @ {game['home_team']}: "
                    f"{annotation['summary']}"
                )
    if not args.write:
        return
    celebrity_inserted = configured_celebrity_grade_store(
        writable=True,
        initialize=True,
    ).append_rows(celebrity_grades)
    print(
        f"Celebrity grades: {len(celebrity_grades)} gradeable latest picks, "
        f"{celebrity_inserted} appended."
    )
    worksheet = ensure_worksheet(spreadsheet, GRADES_TAB, GRADE_HEADERS)
    existing = set(
        _call_with_retry(
            worksheet.col_values, GRADE_HEADERS.index("opinion_id") + 1
        )[1:]
    )
    new_rows = [
        ledger_row(result, graded_at_utc=graded_at)
        for result in graded + means
        if result["opinion_id"] not in existing
    ]
    if not new_rows:
        print("moe_grades is already up to date.")
        return
    _call_with_retry(
        worksheet.append_rows,
        [[row.get(header, "") for header in GRADE_HEADERS] for row in new_rows],
        value_input_option="RAW",
    )
    mean_count = sum(
        1 for row in new_rows if row["expert_id"] == MEAN_OF_ARMS_ID
    )
    print(
        f"Appended {len(new_rows)} graded rows to {GRADES_TAB} "
        f"({mean_count} mean-of-arms)."
    )
    digest_kwargs = dict(
        season=season,
        scoreboard=scoreboard,
        new_rows=new_rows,
        finals=finals,
        opinions=approved,
        snapshots=snapshots,
    )
    if args.notify and not deliver_notification(
        notification_text(**digest_kwargs),
        html=notification_text(**digest_kwargs, html=True),
    ):
        # The append succeeded and will not repeat (opinion-id dedupe), so a
        # lost DM is the operator's only signal — fail the run and let the
        # healthcheck /fail ping carry the log tail.
        raise SystemExit("notify: digest not delivered after appending rows")


if __name__ == "__main__":
    main()
