"""Desk group: the shared Telegram supergroup where the two reviewers work the
NFL MOE committee and read the God Expert.

One supergroup with forum topics, the intake bot as admin, both reviewers in
it with the same rights. Every card is one message both of them see:

- **Review topic** — one card per upcoming game listing that game's valid
  opinion rows (pending first) with ✅ / ❌ callback buttons per pending row
  and a 👁 deep link into the reviewer's own DM with the bot. A pinned queue
  card summarises every upcoming game's committee (approved / pending /
  missing per voice).
- **Picks topic** — one card per game once a God Expert arm is approved:
  both arms' legs, the committee count, and a collapsed
  ``<blockquote expandable>`` "Why" each viewer opens on their own screen.
  A pinned week card lists the legs for every game.
- **Scores topic** — the grading digest (``scripts/moe_grade.py --notify``).

Loud (a notification) in two places only: a reply under the picks card when
a bet leg is approved, and a reply under the review card when the judge lock
is near with rows still pending. Everything else is posted silently and
edited in place; Telegram edits never notify.

Shared-message rules: a button either acts (approve / reject, checked against
the reviewer list and signed with who tapped) or deep-links into the
tapper's own DM; nothing navigates the shared message.

This module owns the pure logic (model, renderers, sync decisions) and a thin
Bot API transport; ``intake_bot.py`` wires it to the sheet, the Telethon
callback events and a periodic sync task. It imports nothing from ``moe``
(``fcntl``) so its tests run on Windows: the caller passes the hash-verified
approved rows in.
"""

from __future__ import annotations

import hashlib
import html
import json
import os
import re
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Iterable
from zoneinfo import ZoneInfo

ET = ZoneInfo("America/New_York")
ROOT = Path(__file__).resolve().parent
DEFAULT_STATE_PATH = ROOT / "moe_desk_state.json"
STATE_VERSION = 1

# The judge runner skips games inside two hours of kickoff
# (scripts/god_judge_runner.py KICKOFF_CUTOFF); the lock warning counts back
# from that moment.
JUDGE_LOCK = timedelta(hours=2)
DEFAULT_LOCK_WARN_HOURS = 2.0
DEFAULT_SYNC_SECONDS = 120
HORIZON = timedelta(days=10)
RETENTION = timedelta(days=3)
MAX_POSTS_PER_SYNC = 15
MAX_REVIEW_ROWS = 12
THESIS_CHARS = 160
COMMITTEE_THESIS_CHARS = 110
WHY_CHARS = 900

RULES_EXPERT_ID = "god_rules"
JUDGE_EXPERT_ID = "god_judge"
ARM_IDS = (RULES_EXPERT_ID, JUDGE_EXPERT_ID)
ARM_LABELS = {RULES_EXPERT_ID: "Rules", JUDGE_EXPERT_ID: "Judge"}
AGGREGATOR_MODES = {"aggregator", "aggregator_judge"}

VOICE_ABBREVIATIONS = {
    "schedule": "Sch",
    "divisional": "Div",
    "win_total": "WT",
    "ak": "AK",
    "rating_elo": "Elo",
    "cee": "Cee",
    "celebrity": "Celeb",
    RULES_EXPERT_ID: "Rules",
    JUDGE_EXPERT_ID: "Judge",
}
STATUS_MARKS = {
    "approved": "✓",
    "pending": "⏳",
    "rejected": "✗",
    "missing": "—",
}

CALLBACK_PREFIX = "desk:"


def _esc(text: Any) -> str:
    """Telegram HTML needs only &lt; &gt; &amp;; leave quotes readable."""
    return html.escape(str(text), quote=False)


# --------------------------------------------------------------------------
# configuration


@dataclass(frozen=True)
class DeskConfig:
    bot_token: str
    chat_id: str
    review_topic: int
    picks_topic: int
    scores_topic: int | None = None
    bot_username: str = ""
    sync_seconds: int = DEFAULT_SYNC_SECONDS
    lock_warn_hours: float = DEFAULT_LOCK_WARN_HOURS
    state_path: Path = DEFAULT_STATE_PATH

    def with_username(self, username: str) -> "DeskConfig":
        return replace(self, bot_username=(username or "").lstrip("@"))


def _int_or_none(value: Any) -> int | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        return int(text)
    except ValueError:
        return None


def desk_config_from_env(
    environ: Any = None,
) -> DeskConfig | None:
    """The desk is enabled when the chat id and both card topics are set;
    otherwise ``None`` and nothing in the bot touches a group."""
    environ = os.environ if environ is None else environ
    token = str(environ.get("INTAKE_BOT_TOKEN") or "").strip()
    chat_id = str(environ.get("MOE_DESK_CHAT_ID") or "").strip()
    review = _int_or_none(environ.get("MOE_DESK_REVIEW_TOPIC"))
    picks = _int_or_none(environ.get("MOE_DESK_PICKS_TOPIC"))
    if not (token and chat_id and review and picks):
        return None
    sync_seconds = _int_or_none(environ.get("MOE_DESK_SYNC_SECONDS"))
    warn = environ.get("MOE_DESK_LOCK_WARN_HOURS")
    try:
        lock_warn_hours = (
            float(warn) if str(warn or "").strip() else DEFAULT_LOCK_WARN_HOURS
        )
    except ValueError:
        lock_warn_hours = DEFAULT_LOCK_WARN_HOURS
    state_path = str(environ.get("MOE_DESK_STATE_PATH") or "").strip()
    return DeskConfig(
        bot_token=token,
        chat_id=chat_id,
        review_topic=review,
        picks_topic=picks,
        scores_topic=_int_or_none(environ.get("MOE_DESK_SCORES_TOPIC")),
        sync_seconds=max(15, sync_seconds or DEFAULT_SYNC_SECONDS),
        lock_warn_hours=max(0.0, lock_warn_hours),
        state_path=Path(state_path) if state_path else DEFAULT_STATE_PATH,
    )


# --------------------------------------------------------------------------
# rows and games


def _parse_time(value: Any) -> datetime:
    parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def is_valid_row(row: dict[str, Any]) -> bool:
    return str(row.get("generation_status") or "valid") == "valid"


def review_status(row: dict[str, Any]) -> str:
    return str(row.get("review_status") or "pending").strip().lower()


def is_pending_row(row: dict[str, Any]) -> bool:
    return is_valid_row(row) and review_status(row) == "pending"


def is_arm_row(row: dict[str, Any]) -> bool:
    return str(row.get("expert_id") or "") in ARM_IDS


def row_key(row: dict[str, Any]) -> tuple[str, str]:
    return (
        str(row.get("generated_at_utc") or ""),
        str(row.get("opinion_id") or ""),
    )


def latest_row(rows: Iterable[dict[str, Any]]) -> dict[str, Any] | None:
    rows = list(rows)
    return max(rows, key=row_key) if rows else None


def short_id(row: dict[str, Any]) -> str:
    return str(row.get("opinion_id") or "")[:8]


def nickname(team: Any) -> str:
    words = str(team or "").split()
    return words[-1] if words else ""


def teams_label(game: dict[str, Any]) -> str:
    return f"{nickname(game.get('away_team'))} @ {nickname(game.get('home_team'))}"


def _abbrev(team: Any, team_abbrevs: dict[str, str] | None) -> str:
    if team_abbrevs:
        found = team_abbrevs.get(str(team or "").strip())
        if found:
            return found
    return nickname(team)


def kickoff_label(kickoff: datetime) -> str:
    local = kickoff.astimezone(ET)
    return f"{local:%a %b} {local.day} · {clock_label(kickoff)}"


def clock_label(moment: datetime) -> str:
    local = moment.astimezone(ET)
    return local.strftime("%I:%M %p").lstrip("0")


def short_kickoff(kickoff: datetime) -> str:
    local = kickoff.astimezone(ET)
    return f"{local:%a} {clock_label(kickoff)}"


def committee_experts(registry: dict[str, Any]) -> list[str]:
    """Enabled required voices — the same rule as the judge runner's
    ``committee_experts``: optional experts join only when available."""
    experts = registry.get("experts") or {}
    return [
        expert_id
        for expert_id in sorted(experts)
        if isinstance(experts[expert_id], dict)
        and experts[expert_id].get("enabled")
        and not experts[expert_id].get("committee_optional")
        and str(experts[expert_id].get("mode") or "") not in AGGREGATOR_MODES
    ]


def optional_experts(registry: dict[str, Any]) -> list[str]:
    experts = registry.get("experts") or {}
    return [
        expert_id
        for expert_id in sorted(experts)
        if isinstance(experts[expert_id], dict)
        and experts[expert_id].get("enabled")
        and experts[expert_id].get("committee_optional")
        and str(experts[expert_id].get("mode") or "") not in AGGREGATOR_MODES
    ]


def expert_abbreviation(expert_id: str) -> str:
    return VOICE_ABBREVIATIONS.get(expert_id, expert_id[:5].title())


# --------------------------------------------------------------------------
# the model


@dataclass
class GameDesk:
    game: dict[str, Any]
    kickoff: datetime
    started: bool
    rows: list[dict[str, Any]]
    pending: list[dict[str, Any]]  # actionable: latest valid per expert+model
    approved: list[dict[str, Any]]
    reviewed: list[dict[str, Any]]  # one per expert, the aggregator's pick
    voices: list[tuple[str, str, bool]]  # (expert_id, status, required)
    required_total: int
    required_approved: int
    rules: dict[str, Any] | None
    judge: dict[str, Any] | None

    @property
    def event_id(self) -> str:
        return str(self.game.get("event_id") or "")

    @property
    def review_rows(self) -> list[dict[str, Any]]:
        return self.pending + self.reviewed

    @property
    def week(self) -> str:
        return str(self.game.get("week") or "").strip()

    @property
    def missing_required(self) -> list[str]:
        return [
            expert_id
            for expert_id, status, required in self.voices
            if required and status == "missing"
        ]


def latest_valid_by_expert_model(
    rows: Iterable[dict[str, Any]],
) -> dict[tuple[str, str], dict[str, Any]]:
    latest: dict[tuple[str, str], dict[str, Any]] = {}
    for row in rows:
        if not is_valid_row(row) or review_status(row) == "not_applicable":
            continue
        key = (str(row.get("expert_id") or ""), str(row.get("model") or ""))
        current = latest.get(key)
        if current is None or row_key(row) > row_key(current):
            latest[key] = row
    return latest


def actionable_pending(rows: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    """Pending rows still worth a decision: a pending row that is the latest
    valid row for its expert and model. An older draft superseded by a
    newer row on the same expert and model (approved, rejected or pending)
    is hidden — the aggregator only ever reads the latest approved row, so
    deciding a superseded draft changes nothing. God arms first, then
    voices oldest first."""
    latest = latest_valid_by_expert_model(rows)
    pending = [row for row in latest.values() if review_status(row) == "pending"]
    return sorted(pending, key=lambda row: (0 if is_arm_row(row) else 1, row_key(row)))


def committee_rows(
    rows: Iterable[dict[str, Any]],
    approved_ids: set[str],
    *,
    expert_order: Iterable[str],
    default_models: dict[str, str],
) -> list[dict[str, Any]]:
    """One reviewed row per expert, in ``expert_order`` then any other
    expert with rows: the approved row the aggregator selects (latest
    approved on the expert's default model, else latest approved on any
    model — moe_god.select_voice_rows), else the latest rejected valid row.
    Experts without a reviewed row are absent (the queue card shows them
    as missing)."""
    rows = [row for row in rows if is_valid_row(row)]
    by_expert: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        by_expert.setdefault(str(row.get("expert_id") or ""), []).append(row)
    order = list(expert_order) + [
        expert_id for expert_id in sorted(by_expert) if expert_id not in set(expert_order)
    ]
    selected: list[dict[str, Any]] = []
    for expert_id in order:
        candidates = by_expert.get(expert_id, [])
        approved = [row for row in candidates if str(row.get("opinion_id")) in approved_ids]
        if approved:
            default_model = default_models.get(expert_id, "")
            on_default = [row for row in approved if str(row.get("model")) == default_model]
            selected.append(latest_row(on_default or approved))
            continue
        rejected = [row for row in candidates if review_status(row) == "rejected"]
        if rejected:
            selected.append(latest_row(rejected))
    return selected


def build_desks(
    games: Iterable[dict[str, Any]],
    rows: Iterable[dict[str, Any]],
    approved_rows: Iterable[dict[str, Any]],
    registry: dict[str, Any],
    *,
    now: datetime,
) -> list[GameDesk]:
    """One desk per upcoming game inside the horizon (plus games that kicked
    off inside the retention window, marked ``started`` so the sync freezes
    them). ``approved_rows`` are the caller's hash-verified approved rows
    (``moe.approved_opinions``)."""
    rows = list(rows)
    approved_ids = {
        str(row.get("opinion_id")) for row in approved_rows if row.get("opinion_id")
    }
    required = committee_experts(registry)
    optional = optional_experts(registry)
    default_models = {
        expert_id: str(config.get("default_model") or "")
        for expert_id, config in (registry.get("experts") or {}).items()
        if isinstance(config, dict)
    }
    by_event: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        by_event.setdefault(str(row.get("event_id") or ""), []).append(row)
    desks: list[GameDesk] = []
    for game in games:
        if str(game.get("status") or "") != "upcoming":
            continue
        try:
            kickoff = _parse_time(game["commence_time_utc"])
        except (KeyError, ValueError, TypeError):
            continue
        if kickoff < now - RETENTION or kickoff > now + HORIZON:
            continue
        event_id = str(game.get("event_id") or "")
        game_rows = by_event.get(event_id, [])
        approved = [
            row for row in game_rows if str(row.get("opinion_id")) in approved_ids
        ]
        pending = actionable_pending(game_rows)
        approved_experts = {str(row.get("expert_id")) for row in approved}
        pending_experts = {str(row.get("expert_id")) for row in pending}
        rejected_experts = {
            str(row.get("expert_id"))
            for row in game_rows
            if is_valid_row(row) and review_status(row) == "rejected"
        }

        def status_of(expert_id: str) -> str:
            if expert_id in approved_experts:
                return "approved"
            if expert_id in pending_experts:
                return "pending"
            if expert_id in rejected_experts:
                return "rejected"
            return "missing"

        voices = [(expert_id, status_of(expert_id), True) for expert_id in required]
        voices += [
            (expert_id, status_of(expert_id), False)
            for expert_id in optional
            if status_of(expert_id) != "missing"
        ]
        desks.append(
            GameDesk(
                game=game,
                kickoff=kickoff,
                started=kickoff <= now,
                rows=game_rows,
                pending=pending,
                approved=approved,
                reviewed=committee_rows(
                    game_rows,
                    approved_ids,
                    expert_order=[*required, *optional, *ARM_IDS],
                    default_models=default_models,
                ),
                voices=voices,
                required_total=len(required),
                required_approved=sum(
                    1 for expert_id in required if expert_id in approved_experts
                ),
                rules=latest_row(
                    row for row in approved if row.get("expert_id") == RULES_EXPERT_ID
                ),
                judge=latest_row(
                    row for row in approved if row.get("expert_id") == JUDGE_EXPERT_ID
                ),
            )
        )
    desks.sort(key=lambda desk: (desk.kickoff, desk.event_id))
    return desks


# --------------------------------------------------------------------------
# legs and labels


def leg_from_json(value: Any) -> dict[str, Any] | None:
    if value in (None, ""):
        return None
    try:
        parsed = json.loads(value) if isinstance(value, str) else value
    except (TypeError, ValueError):
        return None
    if not isinstance(parsed, dict) or "selection" not in parsed:
        return None
    return parsed


def leg_is_bet(leg: dict[str, Any] | None) -> bool:
    return bool(leg) and str(leg.get("selection") or "PASS").upper() != "PASS"


def _price_text(price: Any) -> str:
    try:
        return f"({int(float(price)):+d})"
    except (TypeError, ValueError):
        return ""


def _units_text(leg: dict[str, Any]) -> str:
    try:
        units = float(leg.get("stake_units") or 0)
    except (TypeError, ValueError):
        return ""
    return f"{units:g}u" if units > 0 else ""


def leg_label(
    leg: dict[str, Any] | None,
    *,
    kind: str,
    with_stars: bool = True,
    with_reason: bool = True,
    short_pass: bool = False,
) -> str:
    """``49ers +3.5 (-110) ★ 1.1u`` / ``Over 44.5 (-105) ★`` / ``PASS (ev floor)``."""
    if leg is None:
        return "—"
    if not leg_is_bet(leg):
        if short_pass:
            return "pass"
        reason = str(leg.get("pass_reason") or "").strip()
        return f"PASS ({reason})" if reason and with_reason else "PASS"
    selection = str(leg.get("selection") or "")
    if kind == "side":
        selection = nickname(selection)
    try:
        line = float(leg.get("line"))
        line_text = f"{line:+g}" if kind == "side" else f"{line:g}"
    except (TypeError, ValueError):
        line_text = ""
    parts = [selection, line_text, _price_text(leg.get("price"))]
    if with_stars:
        try:
            stars = "★" * max(1, int(leg.get("confidence_stars") or 1))
        except (TypeError, ValueError):
            stars = "★"
        parts += [stars, _units_text(leg)]
    return " ".join(part for part in parts if part)


def arm_legs(row: dict[str, Any]) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    return (
        leg_from_json(row.get("side_pick_json")),
        leg_from_json(row.get("total_pick_json")),
    )


def _p_home(row: dict[str, Any]) -> str:
    try:
        return f"p home {float(row.get('home_win_probability')):.2f}".replace(
            "0.", "."
        )
    except (TypeError, ValueError):
        return ""


def _stars(row: dict[str, Any]) -> str:
    try:
        return "★" * max(1, int(row.get("confidence_stars") or 1))
    except (TypeError, ValueError):
        return "★"


def _clip(text: Any, limit: int) -> str:
    text = re.sub(r"\s+", " ", str(text or "")).strip()
    if len(text) <= limit:
        return text
    return text[: limit - 1].rstrip() + "…"


def row_summary(row: dict[str, Any], *, thesis_chars: int = THESIS_CHARS) -> str:
    """One line per row: the arms' two legs, or a voice's pick."""
    if str(row.get("pick_market") or "") == "side_and_total" and row.get(
        "side_pick_json"
    ):
        side, total = arm_legs(row)
        parts = [
            f"Side {_esc(leg_label(side, kind='side'))}",
            f"Total {_esc(leg_label(total, kind='total'))}",
        ]
        p_home = _p_home(row)
        if p_home:
            parts.append(_esc(p_home))
        return " · ".join(parts)
    winner = nickname(row.get("predicted_winner"))
    try:
        probability = float(row.get("home_win_probability"))
        if str(row.get("predicted_winner")) != str(row.get("home_team")):
            probability = 1 - probability
        pct = f"{probability:.0%}"
    except (TypeError, ValueError):
        pct = ""
    try:
        score = (
            f"{int(float(row.get('predicted_away_score')))}-"
            f"{int(float(row.get('predicted_home_score')))}"
        )
    except (TypeError, ValueError):
        score = ""
    head = " ".join(part for part in (winner, pct, _stars(row)) if part)
    parts = [_esc(head)]
    if score:
        parts.append(score)
    thesis = _clip(row.get("thesis"), thesis_chars)
    if thesis:
        parts.append(f"“{_esc(thesis)}”")
    return " · ".join(parts)


def _reviewed_mark(row: dict[str, Any]) -> str:
    status = review_status(row)
    mark = {"approved": "✅", "rejected": "❌"}.get(status)
    if not mark:
        return ""
    who = _esc(str(row.get("reviewed_by") or "").strip())
    when = ""
    if row.get("reviewed_at_utc"):
        try:
            when = clock_label(_parse_time(row["reviewed_at_utc"]))
        except ValueError:
            when = ""
    text = " ".join(part for part in (mark, who, when) if part)
    note = _clip(row.get("review_note"), 80)
    if status == "rejected" and note:
        text += f" · “{_esc(note)}”"
    return text


def _model_text(row: dict[str, Any]) -> str:
    model = str(row.get("model") or "").strip()
    return model.replace("claude-", "") if model else ""


def deep_link(config: DeskConfig, param: str) -> str | None:
    if not config.bot_username:
        return None
    return f"https://t.me/{config.bot_username}?start={param}"


def parse_start_param(text: str) -> tuple[str, str] | None:
    """``/start op_<opinion id>`` → ``("op", id)``; ``/start game_<event>`` →
    ``("game", event)``; anything else ``None``."""
    match = re.match(r"^/start(?:@\w+)?\s+(op|game)_([A-Za-z0-9\-]+)\s*$", text or "")
    if not match:
        return None
    return match.group(1), match.group(2)


# --------------------------------------------------------------------------
# renderers: (html, inline keyboard rows)

Keyboard = list[list[dict[str, str]]]


def _button(text: str, *, callback: str | None = None, url: str | None = None) -> dict[str, str]:
    if url:
        return {"text": text, "url": url}
    return {"text": text, "callback_data": callback or ""}


def render_review_card(
    desk: GameDesk, *, config: DeskConfig
) -> tuple[str, Keyboard]:
    game = desk.game
    lock = desk.kickoff - JUDGE_LOCK
    to_review = len(desk.pending)
    lines = [
        f"📥 <b>{_esc(teams_label(game))}</b> · {kickoff_label(desk.kickoff)} ET",
        (
            f"judge locks {clock_label(lock)} ET · committee "
            f"{desk.required_approved}/{desk.required_total} · "
            + (f"{to_review} to review" if to_review else "nothing to review")
        ),
    ]
    keyboard: Keyboard = []
    visible = desk.pending[:MAX_REVIEW_ROWS]
    pending_arms: dict[str, str] = {}
    for index, row in enumerate(visible, start=1):
        name = _esc(str(row.get("expert_name") or row.get("expert_id") or ""))
        head = f"{index} · <b>{name}</b>"
        if is_arm_row(row):
            head += f" · <code>{_esc(short_id(row))}</code>"
            if str(row.get("generation_backend") or "") == "claude_headless":
                head += " · headless"
        else:
            model = _model_text(row)
            if model:
                head += f" · <code>{_esc(model)}</code>"
        lines += ["", head, row_summary(row)]
        opinion_id = str(row.get("opinion_id") or "")
        buttons = [
            _button(f"✅ {index}", callback=f"{CALLBACK_PREFIX}ok:{opinion_id}"),
            _button(f"❌ {index}", callback=f"{CALLBACK_PREFIX}no:{opinion_id}"),
        ]
        link = deep_link(config, f"op_{opinion_id}")
        if link:
            buttons.append(_button(f"👁 {index}", url=link))
        keyboard.append(buttons)
        if is_arm_row(row):
            pending_arms.setdefault(str(row["expert_id"]), opinion_id)
    hidden = len(desk.pending) - len(visible)
    if hidden > 0:
        lines += ["", f"<i>+{hidden} more to review</i>"]
    if desk.reviewed:
        lines += ["", "<b>Committee</b>"]
        for row in desk.reviewed:
            name = _esc(str(row.get("expert_name") or row.get("expert_id") or ""))
            head = f"<b>{name}</b>"
            if is_arm_row(row):
                head += f" · <code>{_esc(short_id(row))}</code>"
            else:
                model = _model_text(row)
                if model:
                    head += f" · <code>{_esc(model)}</code>"
            mark = _reviewed_mark(row)
            if mark:
                head += f" · {mark}"
            lines += [head, row_summary(row, thesis_chars=COMMITTEE_THESIS_CHARS)]
    if len(pending_arms) == 2:
        keyboard.append(
            [
                _button(
                    "✅ Approve both arms",
                    callback=f"{CALLBACK_PREFIX}okarms:{desk.event_id}",
                )
            ]
        )
    return "\n".join(lines), keyboard


def _factor_texts(value: Any, limit: int) -> list[str]:
    if value in (None, ""):
        return []
    try:
        parsed = json.loads(value) if isinstance(value, str) else value
    except (TypeError, ValueError):
        return []
    if not isinstance(parsed, list):
        return []
    texts: list[str] = []
    for item in parsed[:limit]:
        if isinstance(item, str):
            texts.append(item)
        elif isinstance(item, dict):
            for key in ("text", "claim", "factor", "reason", "summary"):
                if item.get(key):
                    texts.append(str(item[key]))
                    break
    return [text for text in texts if text.strip()]


def _why_block(desk: GameDesk) -> str:
    pieces: list[str] = []
    for arm_row in (desk.rules, desk.judge):
        if arm_row is None:
            continue
        label = ARM_LABELS.get(str(arm_row.get("expert_id")), "Arm")
        thesis = _clip(arm_row.get("thesis"), 260)
        segment = [f"<b>{label}</b>"]
        if thesis:
            segment[0] += f" · {_esc(thesis)}"
        for factor in _factor_texts(arm_row.get("supporting_factors_json"), 3):
            segment.append(f"• {_esc(_clip(factor, 200))}")
        for factor in _factor_texts(arm_row.get("counterarguments_json"), 2):
            segment.append(f"◦ {_esc(_clip(factor, 200))}")
        pieces.append("\n".join(segment))
    text = "\n".join(pieces)
    if len(text) > WHY_CHARS:
        text = text[: WHY_CHARS - 1].rstrip() + "…"
    return text


def _arm_line(label: str, row: dict[str, Any] | None) -> str:
    if row is None:
        return f"<b>{label}</b>  —"
    side, total = arm_legs(row)
    text = (
        f"<b>{label}</b>  {_esc(leg_label(side, kind='side'))} · "
        f"total {_esc(leg_label(total, kind='total'))}"
    )
    p_home = _p_home(row)
    if p_home:
        text += f" · {_esc(p_home)}"
    return text


def render_picks_card(
    desk: GameDesk, *, config: DeskConfig
) -> tuple[str, Keyboard]:
    game = desk.game
    lines = [
        f"🏈 <b>{_esc(teams_label(game))}</b> · {kickoff_label(desk.kickoff)} ET",
        _arm_line("Rules", desk.rules),
        _arm_line("Judge", desk.judge),
    ]
    stamps = []
    for label, row in (("rules", desk.rules), ("judge", desk.judge)):
        if row is not None and row.get("generated_at_utc"):
            try:
                stamps.append(f"{label} {clock_label(_parse_time(row['generated_at_utc']))}")
            except ValueError:
                pass
    committee = f"committee {desk.required_approved}/{desk.required_total}"
    lines.append(" · ".join([committee, *stamps]))
    why = _why_block(desk)
    if why:
        lines.append(f"<blockquote expandable>{why}</blockquote>")
    keyboard: Keyboard = []
    link = deep_link(config, f"game_{desk.event_id}")
    if link:
        keyboard.append([_button("👁 All opinions", url=link)])
    return "\n".join(lines), keyboard


def _week_label(desks: Iterable[GameDesk]) -> str:
    weeks = sorted(
        {int(desk.week) for desk in desks if desk.week.isdigit()}
    )
    if not weeks:
        return ""
    if len(weeks) == 1:
        return f"Week {weeks[0]}"
    return f"Weeks {weeks[0]}–{weeks[-1]}"


def _voice_status_text(desk: GameDesk) -> str:
    parts = []
    for expert_id, status, required in desk.voices:
        mark = STATUS_MARKS.get(status, "?")
        text = f"{expert_abbreviation(expert_id)} {mark}"
        if not required:
            text += " (opt)"
        parts.append(text)
    arms = []
    for arm_id, approved_row in ((RULES_EXPERT_ID, desk.rules), (JUDGE_EXPERT_ID, desk.judge)):
        if approved_row is not None:
            arms.append("✓")
        elif any(
            is_pending_row(row) and row.get("expert_id") == arm_id for row in desk.rows
        ):
            arms.append("⏳")
        else:
            arms.append("—")
    return " ".join(parts) + f" · God {arms[0]}/{arms[1]}"


def render_queue_card(
    desks: Iterable[GameDesk],
    *,
    team_abbrevs: dict[str, str] | None = None,
) -> tuple[str, Keyboard]:
    active = [desk for desk in desks if not desk.started]
    pending_rows = sum(len(desk.pending) for desk in active)
    pending_games = sum(1 for desk in active if desk.pending)
    week = _week_label(active)
    head = "📥 <b>Review queue</b>"
    if week:
        head += f" · {week}"
    head += f" · {pending_rows} pending in {pending_games} game{'s' if pending_games != 1 else ''}"
    lines = [head]
    if not active:
        lines.append("No upcoming games inside ten days.")
        return "\n".join(lines), []
    missing_counts: dict[str, int] = {}
    for desk in active:
        away = _abbrev(desk.game.get("away_team"), team_abbrevs)
        home = _abbrev(desk.game.get("home_team"), team_abbrevs)
        lines.append(
            f"<b>{_esc(f'{away} @ {home}')}</b> {short_kickoff(desk.kickoff)} · "
            f"{_esc(_voice_status_text(desk))}"
        )
        for expert_id in desk.missing_required:
            missing_counts[expert_id] = missing_counts.get(expert_id, 0) + 1
    if missing_counts:
        missing = " · ".join(
            f"{expert_abbreviation(expert_id)} {count}"
            for expert_id, count in sorted(missing_counts.items())
        )
        lines += ["", f"no row yet · {_esc(missing)}"]
    return "\n".join(lines), []


def _week_legs(desk: GameDesk) -> str:
    if desk.rules is None and desk.judge is None:
        return f"waiting on committee ({desk.required_approved}/{desk.required_total})"
    parts = []
    if desk.rules is not None:
        side, total = arm_legs(desk.rules)
        parts.append(
            f"{leg_label(side, kind='side', with_reason=False, short_pass=True)} · "
            f"{leg_label(total, kind='total', with_reason=False, short_pass=True)}"
        )
    else:
        parts.append("rules —")
    if desk.judge is not None:
        side, total = arm_legs(desk.judge)
        parts.append(
            "judge "
            f"{leg_label(side, kind='side', with_stars=False, with_reason=False, short_pass=True)}/"
            f"{leg_label(total, kind='total', with_stars=False, with_reason=False, short_pass=True)}"
        )
    else:
        parts.append("judge —")
    return " · ".join(parts)


def render_week_card(
    desks: Iterable[GameDesk],
    *,
    team_abbrevs: dict[str, str] | None = None,
) -> tuple[str, Keyboard]:
    active = [desk for desk in desks if not desk.started]
    complete = sum(1 for desk in active if desk.rules is not None)
    week = _week_label(active)
    head = "🧠 <b>God Expert</b>"
    if week:
        head += f" · {week}"
    head += f" · {complete} of {len(active)} committees complete"
    lines = [head]
    if not active:
        lines.append("No upcoming games inside ten days.")
        return "\n".join(lines), []
    for desk in active:
        away = _abbrev(desk.game.get("away_team"), team_abbrevs)
        home = _abbrev(desk.game.get("home_team"), team_abbrevs)
        lines.append(
            f"<b>{_esc(f'{away} @ {home}')}</b> {short_kickoff(desk.kickoff)} · "
            f"{_esc(_week_legs(desk))}"
        )
    return "\n".join(lines), []


def render_bet_alert(desk: GameDesk, arm_row: dict[str, Any], kind: str) -> str:
    side, total = arm_legs(arm_row)
    leg = side if kind == "side" else total
    arm = ARM_LABELS.get(str(arm_row.get("expert_id")), "Arm").lower()
    text = f"🔔 Bet · {leg_label(leg, kind=kind)} · {arm} arm"
    other = desk.judge if arm_row is desk.rules else desk.rules
    if other is not None:
        other_side, other_total = arm_legs(other)
        other_leg = other_side if kind == "side" else other_total
        other_arm = ARM_LABELS.get(str(other.get("expert_id")), "Arm").lower()
        text += f"\n{other_arm} arm: {leg_label(other_leg, kind=kind)}"
    return _esc(text)


def render_lock_warning(desk: GameDesk, *, now: datetime) -> str:
    remaining = desk.kickoff - JUDGE_LOCK - now
    minutes = max(0, int(remaining.total_seconds() // 60))
    hours, minutes = divmod(minutes, 60)
    when = f"{hours}h {minutes:02d}m" if hours else f"{minutes}m"
    return _esc(
        f"🔔 {teams_label(desk.game)} locks for the judge in {when} · "
        f"{len(desk.pending)} pending"
    )


def render_scores_notice(text: str) -> str:
    """The grading digest is aligned plain text; keep the columns."""
    return f"<pre>{_esc(text)}</pre>"


# --------------------------------------------------------------------------
# state


def empty_state() -> dict[str, Any]:
    return {"version": STATE_VERSION, "cards": {}, "announced": {}, "kickoffs": {}}


def load_state(path: str | Path) -> dict[str, Any]:
    path = Path(path)
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return empty_state()
    if not isinstance(raw, dict) or raw.get("version") != STATE_VERSION:
        return empty_state()
    state = empty_state()
    for key in ("cards", "announced", "kickoffs"):
        if isinstance(raw.get(key), dict):
            state[key] = raw[key]
    return state


def save_state(path: str | Path, state: dict[str, Any]) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp = tempfile.mkstemp(prefix=path.name, suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(state, handle, ensure_ascii=False, indent=1, sort_keys=True)
        os.replace(temp, path)
    except BaseException:
        try:
            os.unlink(temp)
        except OSError:
            pass
        raise


def prune_state(state: dict[str, Any], *, now: datetime) -> None:
    """Drop cards and announcements for games that kicked off more than the
    retention window ago, so the file stays the size of one slate."""
    expired = set()
    for event_id, kickoff in list(state["kickoffs"].items()):
        try:
            if _parse_time(kickoff) < now - RETENTION:
                expired.add(event_id)
        except ValueError:
            expired.add(event_id)
    for event_id in expired:
        state["kickoffs"].pop(event_id, None)
    for key in list(state["cards"]):
        _, _, event_id = key.partition(":")
        if event_id and event_id in expired:
            state["cards"].pop(key, None)
    for key, value in list(state["announced"].items()):
        event_id = value.get("event_id") if isinstance(value, dict) else None
        if event_id in expired:
            state["announced"].pop(key, None)


def content_hash(text: str, keyboard: Keyboard, topic: int) -> str:
    payload = json.dumps([topic, text, keyboard], ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


# --------------------------------------------------------------------------
# transport


class DeskApiError(RuntimeError):
    pass


class BotApi:
    """The few Bot API calls the desk needs, over ``urllib`` (the same way
    ``scripts/god_judge_runner.send_watchdog_dm`` sends). One 429 is honoured
    by waiting ``retry_after`` (capped) and retrying once."""

    def __init__(
        self,
        token: str,
        *,
        opener: Callable[..., Any] = urllib.request.urlopen,
        timeout: float = 20.0,
        sleep: Callable[[float], None] = time.sleep,
        max_retry_after: float = 35.0,
    ) -> None:
        self._token = token
        self._opener = opener
        self._timeout = timeout
        self._sleep = sleep
        self._max_retry_after = max_retry_after

    def call(self, method: str, **params: Any) -> Any:
        clean = {
            key: (json.dumps(value) if isinstance(value, (dict, list)) else value)
            for key, value in params.items()
            if value is not None
        }
        data = urllib.parse.urlencode(clean).encode()
        url = f"https://api.telegram.org/bot{self._token}/{method}"
        for attempt in (1, 2):
            try:
                with self._opener(
                    urllib.request.Request(url, data=data), timeout=self._timeout
                ) as response:
                    body = json.loads(response.read().decode("utf-8"))
            except urllib.error.HTTPError as exc:
                try:
                    body = json.loads(exc.read().decode("utf-8"))
                except Exception:  # noqa: BLE001 - the status is the message
                    body = {"ok": False, "description": f"HTTP {exc.code}"}
                if exc.code == 429 and attempt == 1:
                    retry_after = float(
                        (body.get("parameters") or {}).get("retry_after") or 1
                    )
                    self._sleep(min(retry_after, self._max_retry_after))
                    continue
            except (urllib.error.URLError, OSError, ValueError) as exc:
                raise DeskApiError(f"{method}: {exc}") from exc
            if body.get("ok"):
                return body.get("result")
            raise DeskApiError(f"{method}: {body.get('description') or body}")
        raise DeskApiError(f"{method}: rate limited twice")

    def send(
        self,
        chat_id: str,
        thread_id: int | None,
        text: str,
        *,
        keyboard: Keyboard | None = None,
        silent: bool = True,
        reply_to: int | None = None,
    ) -> int:
        result = self.call(
            "sendMessage",
            chat_id=chat_id,
            message_thread_id=thread_id,
            text=text,
            parse_mode="HTML",
            disable_web_page_preview=True,
            disable_notification=silent,
            reply_to_message_id=reply_to,
            allow_sending_without_reply=True,
            reply_markup={"inline_keyboard": keyboard} if keyboard else None,
        )
        return int(result["message_id"])

    def edit(
        self,
        chat_id: str,
        message_id: int,
        text: str,
        *,
        keyboard: Keyboard | None = None,
    ) -> bool:
        """``True`` when the message now shows ``text`` (an unchanged message
        counts); ``False`` when it no longer exists and must be re-posted."""
        try:
            self.call(
                "editMessageText",
                chat_id=chat_id,
                message_id=message_id,
                text=text,
                parse_mode="HTML",
                disable_web_page_preview=True,
                reply_markup={"inline_keyboard": keyboard or []},
            )
        except DeskApiError as exc:
            message = str(exc).lower()
            if "not modified" in message:
                return True
            if "not found" in message or "message to edit" in message:
                return False
            raise
        return True

    def pin(self, chat_id: str, message_id: int) -> bool:
        try:
            self.call(
                "pinChatMessage",
                chat_id=chat_id,
                message_id=message_id,
                disable_notification=True,
            )
        except DeskApiError as exc:
            print(f"desk: pin failed: {exc}", file=sys.stderr)
            return False
        return True

    def delete(self, chat_id: str, message_id: int) -> bool:
        try:
            self.call("deleteMessage", chat_id=chat_id, message_id=message_id)
        except DeskApiError:
            return False
        return True

    def create_topic(self, chat_id: str, name: str) -> int:
        result = self.call("createForumTopic", chat_id=chat_id, name=name)
        return int(result["message_thread_id"])

    def get_chat(self, chat_id: str) -> dict[str, Any]:
        return self.call("getChat", chat_id=chat_id)

    def get_member(self, chat_id: str, user_id: int) -> dict[str, Any]:
        return self.call("getChatMember", chat_id=chat_id, user_id=user_id)

    def get_me(self) -> dict[str, Any]:
        return self.call("getMe")


# --------------------------------------------------------------------------
# sync


@dataclass
class SyncSummary:
    posted: list[str] = field(default_factory=list)
    edited: list[str] = field(default_factory=list)
    alerts: list[str] = field(default_factory=list)
    deferred: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)


class _Budget:
    def __init__(self, limit: int) -> None:
        self.limit = limit
        self.used = 0

    def take(self) -> bool:
        if self.used >= self.limit:
            return False
        self.used += 1
        return True


def _upsert_card(
    *,
    key: str,
    topic: int,
    text: str,
    keyboard: Keyboard,
    config: DeskConfig,
    api: BotApi,
    state: dict[str, Any],
    budget: _Budget,
    summary: SyncSummary,
    pin: bool = False,
) -> int | None:
    digest = content_hash(text, keyboard, topic)
    entry = state["cards"].get(key)
    if isinstance(entry, dict) and entry.get("message_id"):
        if entry.get("hash") == digest:
            return int(entry["message_id"])
        try:
            if api.edit(config.chat_id, int(entry["message_id"]), text, keyboard=keyboard):
                entry["hash"] = digest
                summary.edited.append(key)
                return int(entry["message_id"])
        except DeskApiError as exc:
            summary.errors.append(f"{key}: {exc}")
            return int(entry["message_id"])
        state["cards"].pop(key, None)
    if not budget.take():
        summary.deferred.append(key)
        return None
    try:
        message_id = api.send(config.chat_id, topic, text, keyboard=keyboard, silent=True)
    except DeskApiError as exc:
        summary.errors.append(f"{key}: {exc}")
        return None
    state["cards"][key] = {"message_id": message_id, "hash": digest, "topic": topic}
    summary.posted.append(key)
    if pin:
        api.pin(config.chat_id, message_id)
    return message_id


def _announce(
    *,
    key: str,
    event_id: str,
    topic: int,
    text: str,
    reply_to: int | None,
    config: DeskConfig,
    api: BotApi,
    state: dict[str, Any],
    budget: _Budget,
    summary: SyncSummary,
    now: datetime,
) -> None:
    if key in state["announced"]:
        return
    if not budget.take():
        summary.deferred.append(key)
        return
    try:
        api.send(config.chat_id, topic, text, silent=False, reply_to=reply_to)
    except DeskApiError as exc:
        summary.errors.append(f"{key}: {exc}")
        return
    state["announced"][key] = {"at": now.isoformat(), "event_id": event_id}
    summary.alerts.append(key)


def lock_warning_due(desk: GameDesk, *, config: DeskConfig, now: datetime) -> bool:
    if not desk.pending or desk.started or config.lock_warn_hours <= 0:
        return False
    lock = desk.kickoff - JUDGE_LOCK
    return lock - timedelta(hours=config.lock_warn_hours) <= now < lock


def sync_desk(
    *,
    config: DeskConfig,
    api: BotApi,
    state: dict[str, Any],
    desks: Iterable[GameDesk],
    now: datetime,
    team_abbrevs: dict[str, str] | None = None,
    max_posts: int = MAX_POSTS_PER_SYNC,
) -> SyncSummary:
    """Reconcile the group with the model: post missing cards, edit changed
    ones, announce new bet legs and near locks once. Idempotent — an
    unchanged model is a no-op — and bounded to ``max_posts`` new messages
    per pass so a first run over a full slate spreads across passes."""
    desks = list(desks)
    summary = SyncSummary()
    budget = _Budget(max_posts)
    prune_state(state, now=now)
    for desk in desks:
        state["kickoffs"][desk.event_id] = desk.kickoff.isoformat()
        if desk.started:
            continue
        review_id = None
        if desk.review_rows:
            text, keyboard = render_review_card(desk, config=config)
            review_id = _upsert_card(
                key=f"review:{desk.event_id}",
                topic=config.review_topic,
                text=text,
                keyboard=keyboard,
                config=config,
                api=api,
                state=state,
                budget=budget,
                summary=summary,
            )
        picks_id = None
        if desk.rules is not None or desk.judge is not None:
            text, keyboard = render_picks_card(desk, config=config)
            picks_id = _upsert_card(
                key=f"picks:{desk.event_id}",
                topic=config.picks_topic,
                text=text,
                keyboard=keyboard,
                config=config,
                api=api,
                state=state,
                budget=budget,
                summary=summary,
            )
            for arm_row in (desk.rules, desk.judge):
                if arm_row is None:
                    continue
                side, total = arm_legs(arm_row)
                for kind, leg in (("side", side), ("total", total)):
                    if not leg_is_bet(leg):
                        continue
                    _announce(
                        key=f"bet:{arm_row.get('opinion_id')}:{kind}",
                        event_id=desk.event_id,
                        topic=config.picks_topic,
                        text=render_bet_alert(desk, arm_row, kind),
                        reply_to=picks_id,
                        config=config,
                        api=api,
                        state=state,
                        budget=budget,
                        summary=summary,
                        now=now,
                    )
        if lock_warning_due(desk, config=config, now=now):
            _announce(
                key=f"lock:{desk.event_id}",
                event_id=desk.event_id,
                topic=config.review_topic,
                text=render_lock_warning(desk, now=now),
                reply_to=review_id,
                config=config,
                api=api,
                state=state,
                budget=budget,
                summary=summary,
                now=now,
            )
    text, keyboard = render_queue_card(desks, team_abbrevs=team_abbrevs)
    _upsert_card(
        key="queue",
        topic=config.review_topic,
        text=text,
        keyboard=keyboard,
        config=config,
        api=api,
        state=state,
        budget=budget,
        summary=summary,
        pin=True,
    )
    text, keyboard = render_week_card(desks, team_abbrevs=team_abbrevs)
    _upsert_card(
        key="week",
        topic=config.picks_topic,
        text=text,
        keyboard=keyboard,
        config=config,
        api=api,
        state=state,
        budget=budget,
        summary=summary,
        pin=True,
    )
    return summary


def post_scores_notice(text: str, *, environ: Any = None, api: BotApi | None = None) -> bool:
    """Post the grading digest into the Scores topic. ``False`` when the desk
    or its Scores topic is not configured, or the post failed — the caller
    falls back to the watchdog DM."""
    config = desk_config_from_env(environ)
    if config is None or not config.scores_topic:
        return False
    api = api or BotApi(config.bot_token)
    try:
        api.send(config.chat_id, config.scores_topic, render_scores_notice(text), silent=True)
    except DeskApiError as exc:
        print(f"desk: scores post failed: {exc}", file=sys.stderr)
        return False
    return True


# --------------------------------------------------------------------------
# the /desk command (sent inside the group)


def topic_id_from_reply(reply: Any) -> int | None:
    """The forum topic a message was posted in, from its Telethon reply
    header: ``reply_to_top_id`` when replying inside a topic, else
    ``reply_to_msg_id`` (the topic's root); ``None`` outside topics and in
    the General topic."""
    if reply is None or not getattr(reply, "forum_topic", False):
        return None
    return getattr(reply, "reply_to_top_id", None) or getattr(reply, "reply_to_msg_id", None)


def desk_ids_report(
    chat_id: Any,
    *,
    title: str = "",
    supergroup: bool = False,
    topics: bool = False,
    topic_id: int | None = None,
) -> str:
    """What ``/desk`` answers in a group: the ids the desk needs and whether
    the group is ready for it."""
    lines = [f"desk · {title}".rstrip(" ·"), f"chat_id: {chat_id}"]
    kind = "supergroup" if supergroup else "basic group"
    lines.append(f"{kind} · topics {'on' if topics else 'off'}")
    if topic_id:
        lines.append(f"this topic id: {topic_id}")
    if not supergroup or not topics:
        lines.append("Not ready: Edit → Topics on (this converts it to a supergroup).")
    else:
        lines.append(
            "MOE_DESK_CHAT_ID=<chat_id> in .env, then "
            "scripts/desk_setup.py --create-topics"
        )
    return "\n".join(lines)


# --------------------------------------------------------------------------
# callbacks


def parse_callback(data: str) -> tuple[str, str] | None:
    """``desk:ok:<opinion>`` → ``("ok", opinion)``; ``desk:no:<opinion>``;
    ``desk:okarms:<event>``. Anything else ``None``."""
    if not data.startswith(CALLBACK_PREFIX):
        return None
    action, _, target = data[len(CALLBACK_PREFIX):].partition(":")
    if action not in {"ok", "no", "okarms"} or not target:
        return None
    return action, target


def review_targets(
    action: str,
    target: str,
    rows: Iterable[dict[str, Any]],
) -> tuple[list[dict[str, Any]], str | None]:
    """The rows a callback acts on, or a refusal. Only pending valid rows are
    reviewable; ``okarms`` needs both arms pending for the event."""
    rows = list(rows)
    if action in {"ok", "no"}:
        row = next((row for row in rows if str(row.get("opinion_id")) == target), None)
        if row is None:
            return [], "That row is no longer in the sheet."
        if not is_valid_row(row):
            return [], "That row is an audit row and cannot be reviewed."
        status = review_status(row)
        if status != "pending":
            by = str(row.get("reviewed_by") or "someone").strip()
            return [], f"Already {status} by {by}."
        return [row], None
    arms = {
        str(row["expert_id"]): row
        for row in actionable_pending(
            row for row in rows if str(row.get("event_id")) == target
        )
        if is_arm_row(row)
    }
    if len(arms) != 2:
        return [], "Both arms are no longer pending."
    return [arms[RULES_EXPERT_ID], arms[JUDGE_EXPERT_ID]], None
