"""Desk group: the shared Telegram supergroup where the operators read the
NFL MOE committee and the God Expert.

One supergroup with forum topics, the intake bot as admin. Every card is one
message everyone sees:

- **Picks topic** — one card per game once a God Expert arm is approved:
  both arms' legs, the committee count, and a collapsed
  ``<blockquote expandable>`` "Why" each viewer opens on their own screen.
  A pinned week card lists the legs for every game, plus which required
  voices still have no approved row (those games the judge runner skips
  as "committee incomplete").
- **Scores topic** — the grading digest (``scripts/moe_grade.py --notify``).
- **Offline topic** — one plain-text card per game with the latest full-game
  lines, both God arms, every approved voice pick, and consensus. Cards update
  before kickoff and then freeze so a previously synced Telegram client can
  read its cached copy without invoking the bot.

Review is automatic: every structurally valid opinion row — voices and both
God Expert arms — is approved at generation (``moe_god.review_policy``,
2026-09-09), so the desk shows outcomes and never asks for a decision. The
Review topic with its ✅/❌ cards, the pinned queue card and the judge-lock
warning were removed 2026-09-10; rejecting a bad row remains possible with
``scripts/review_moe_opinion.py``.

Loud (a notification) in one place only: a reply under the picks card when
a bet leg is approved — and, symmetrically, when a later pass withdraws a
previously-announced bet (flips it to PASS). Everything else is posted
silently and edited in place; Telegram edits never notify.

Shared-message rules: a button either switches the shared card's view or
deep-links into the tapper's own DM; nothing else navigates the shared
message.

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

DEFAULT_SYNC_SECONDS = 120
HORIZON = timedelta(days=10)
RETENTION = timedelta(days=3)
MAX_POSTS_PER_SYNC = 15
THESIS_CHARS = 160
COMMITTEE_THESIS_CHARS = 110
WHY_CHARS = 1800
DETAIL_BODY_CHARS = 3000

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
    "hi_lo": "Hi Lo",
    "pikkit": "Pikkit",
    RULES_EXPERT_ID: "Rules",
    JUDGE_EXPERT_ID: "Judge",
}
VOICE_NAMES = {
    "schedule": "Schedule",
    "divisional": "Divisional",
    "win_total": "Win Total",
    "ak": "AK",
    "rating_elo": "Elo",
    "cee": "Cee",
    "celebrity": "Celebrity",
    "hi_lo": "Hi Lo",
    "pikkit": "Pikkit",
}
VOICE_DISPLAY_ORDER = [
    "schedule",
    "divisional",
    "win_total",
    "ak",
    "rating_elo",
    "cee",
    "celebrity",
    "hi_lo",
    "pikkit",
]

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
    picks_topic: int
    scores_topic: int | None = None
    offline_topic: int | None = None
    bot_username: str = ""
    sync_seconds: int = DEFAULT_SYNC_SECONDS
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
    """The desk is enabled when the chat id and the Picks topic are set;
    otherwise ``None`` and nothing in the bot touches a group.
    ``MOE_DESK_REVIEW_TOPIC`` / ``MOE_DESK_LOCK_WARN_HOURS`` are obsolete
    (the Review topic was removed 2026-09-10) and are ignored if present."""
    environ = os.environ if environ is None else environ
    token = str(environ.get("INTAKE_BOT_TOKEN") or "").strip()
    chat_id = str(environ.get("MOE_DESK_CHAT_ID") or "").strip()
    picks = _int_or_none(environ.get("MOE_DESK_PICKS_TOPIC"))
    if not (token and chat_id and picks):
        return None
    sync_seconds = _int_or_none(environ.get("MOE_DESK_SYNC_SECONDS"))
    state_path = str(environ.get("MOE_DESK_STATE_PATH") or "").strip()
    return DeskConfig(
        bot_token=token,
        chat_id=chat_id,
        picks_topic=picks,
        scores_topic=_int_or_none(environ.get("MOE_DESK_SCORES_TOPIC")),
        offline_topic=_int_or_none(environ.get("MOE_DESK_OFFLINE_TOPIC")),
        sync_seconds=max(15, sync_seconds or DEFAULT_SYNC_SECONDS),
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


def voice_name(row: dict[str, Any]) -> str:
    expert_id = str(row.get("expert_id") or "")
    return VOICE_NAMES.get(expert_id) or str(row.get("expert_name") or expert_id)


def _voice_sort_key(row: dict[str, Any]) -> tuple[int, str]:
    expert_id = str(row.get("expert_id") or "")
    if expert_id in VOICE_DISPLAY_ORDER:
        return (VOICE_DISPLAY_ORDER.index(expert_id), expert_id)
    return (len(VOICE_DISPLAY_ORDER), expert_id)


# --------------------------------------------------------------------------
# the model


@dataclass
class GameDesk:
    game: dict[str, Any]
    kickoff: datetime
    started: bool
    rows: list[dict[str, Any]]
    approved: list[dict[str, Any]]
    reviewed: list[dict[str, Any]]  # one per expert, the aggregator's pick
    voices: list[tuple[str, str, bool]]  # (expert_id, status, required)
    required_total: int
    required_approved: int
    missing_optional: list[str]
    rules: dict[str, Any] | None
    judge: dict[str, Any] | None
    shadow_experts: set[str] = field(default_factory=set)

    @property
    def event_id(self) -> str:
        return str(self.game.get("event_id") or "")

    @property
    def approved_voices(self) -> list[dict[str, Any]]:
        """The committee as the picks card shows it: one approved row per
        voice, in display order."""
        return sorted(
            (
                row
                for row in self.reviewed
                if not is_arm_row(row) and review_status(row) == "approved"
            ),
            key=_voice_sort_key,
        )

    @property
    def show_picks(self) -> bool:
        """A picks card is worth posting once two voices have spoken or a God
        arm is approved; a lone rating row on every game would be noise."""
        return (
            self.rules is not None
            or self.judge is not None
            or len(self.approved_voices) >= 2
        )

    @property
    def show_offline(self) -> bool:
        """Offline snapshots start with the first approved opinion."""
        return bool(self.rules or self.judge or self.approved_voices)

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
        approved_experts = {str(row.get("expert_id")) for row in approved}
        rejected_experts = {
            str(row.get("expert_id"))
            for row in game_rows
            if is_valid_row(row) and review_status(row) == "rejected"
        }

        def status_of(expert_id: str) -> str:
            if expert_id in approved_experts:
                return "approved"
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
                missing_optional=[
                    expert_id
                    for expert_id in optional
                    if status_of(expert_id) == "missing"
                ],
                rules=latest_row(
                    row for row in approved if row.get("expert_id") == RULES_EXPERT_ID
                ),
                judge=latest_row(
                    row for row in approved if row.get("expert_id") == JUDGE_EXPERT_ID
                ),
                shadow_experts={
                    expert_id
                    for expert_id, config in (registry.get("experts") or {}).items()
                    if isinstance(config, dict)
                    and str(config.get("aggregator_participation") or "active")
                    == "shadow"
                },
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


def _model_text(row: dict[str, Any]) -> str:
    model = str(row.get("model") or "").strip()
    return model.replace("claude-", "") if model else ""


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


def _all_factor_texts(value: Any) -> list[str]:
    if value in (None, ""):
        return []
    try:
        parsed = json.loads(value) if isinstance(value, str) else value
    except (TypeError, ValueError):
        return []
    if not isinstance(parsed, list):
        return []
    texts: list[str] = []
    for item in parsed:
        if isinstance(item, str):
            text = item
        elif isinstance(item, dict):
            text = next(
                (
                    str(item[key])
                    for key in ("text", "claim", "factor", "reason", "summary")
                    if item.get(key)
                ),
                "",
            )
        else:
            text = ""
        if text.strip():
            texts.append(text.strip())
    return texts


def _detail_body(row: dict[str, Any]) -> str:
    full = str(row.get("full_opinion") or "").strip()
    if full:
        return full
    sections: list[str] = []
    thesis = str(row.get("thesis") or "").strip()
    if thesis:
        sections.append(f"Thesis\n{thesis}")
    for label, key in (
        ("Supporting factors", "supporting_factors_json"),
        ("Counterarguments", "counterarguments_json"),
        ("No-signal factors", "no_signal_factors_json"),
        ("Discarded considerations", "discarded_considerations_json"),
    ):
        factors = _all_factor_texts(row.get(key))
        if factors:
            sections.append(
                label + "\n" + "\n".join(f"- {item}" for item in factors)
            )
    return "\n\n".join(sections) or "No detailed explanation was persisted."


def _split_plain_text(
    text: str, limit: int = DETAIL_BODY_CHARS
) -> list[str]:
    chunks: list[str] = []
    current: list[str] = []
    escaped_length = 0
    for character in text:
        character_length = len(_esc(character))
        if current and escaped_length + character_length > limit:
            chunks.append("".join(current).strip())
            current = []
            escaped_length = 0
        current.append(character)
        escaped_length += character_length
    if current or not chunks:
        chunks.append("".join(current).strip())
    return chunks


def render_opinion_details(
    rows: Iterable[dict[str, Any]],
    *,
    context: str,
) -> list[str]:
    """Render persisted full opinions as safe same-topic reply messages."""
    messages: list[str] = []
    for row in rows:
        name = str(row.get("expert_name") or row.get("expert_id") or "Expert")
        model = _model_text(row)
        chunks = _split_plain_text(_detail_body(row))
        for index, chunk in enumerate(chunks):
            suffix = (
                f" · part {index + 1}/{len(chunks)}"
                if len(chunks) > 1
                else ""
            )
            lines = [
                f"🔎 <b>{_esc(name)}</b>{_esc(suffix)}",
                f"<i>{_esc(context)}{(' · ' + _esc(model)) if model else ''}</i>",
            ]
            if index == 0:
                lines.append(row_summary(row))
            lines.append(
                f"<blockquote expandable>{_esc(chunk)}</blockquote>"
            )
            messages.append("\n".join(lines))
    return messages


def _short_legs(row: dict[str, Any]) -> str:
    side, total = arm_legs(row)
    return (
        f"{leg_label(side, kind='side', with_reason=False, short_pass=True)} · "
        f"{leg_label(total, kind='total', with_reason=False, short_pass=True)}"
    )


def _labeled_legs(row: dict[str, Any]) -> str:
    side, total = arm_legs(row)
    return (
        "Side "
        f"{leg_label(side, kind='side', with_reason=False, short_pass=True)}"
        " · Total "
        f"{leg_label(total, kind='total', with_reason=False, short_pass=True)}"
    )


def god_pick_lines(desk: GameDesk) -> list[str]:
    if desk.rules is None and desk.judge is None:
        return ["<i>Not available</i>"]
    lines = []
    for label, row in (("Rules", desk.rules), ("Judge", desk.judge)):
        value = "—" if row is None else _labeled_legs(row)
        lines.append(f"<b>{label}</b> · {_esc(value)}")
    return lines


def _pikkit_summary(row: dict[str, Any]) -> dict[str, Any]:
    try:
        parsed = json.loads(str(row.get("calibration_summary_json") or "{}"))
    except (TypeError, ValueError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _pikkit_phase_label(row: dict[str, Any]) -> str:
    return (
        "Final T-2h"
        if _pikkit_summary(row).get("generation_phase") == "final_t_minus_2h"
        else "Initial"
    )


def _pikkit_book_outcomes(row: dict[str, Any]) -> str:
    sportsbook = _pikkit_summary(row).get("sportsbook") or {}
    home = nickname(row.get("home_team"))
    away = nickname(row.get("away_team"))
    outcome_labels = {
        "home_win": f"{home} win",
        "away_win": f"{away} win",
        "home_cover": f"{home} cover",
        "away_cover": f"{away} cover",
        "over": "Over",
        "under": "Under",
        "push": "push",
    }
    labels = []
    for market, short in (
        ("moneyline", "ML"),
        ("spread", "spread"),
        ("total", "total"),
    ):
        data = sportsbook.get(market)
        if isinstance(data, dict) and data.get("best_outcome"):
            outcome = str(data["best_outcome"])
            labels.append(
                f"{short} {outcome_labels.get(outcome, outcome.replace('_', ' '))}"
            )
    return ", ".join(labels)


def _pikkit_strongest_movement(row: dict[str, Any]) -> str:
    movement = _pikkit_summary(row).get("movement") or {}
    strongest: tuple[float, str] | None = None
    for market, data in (movement.get("markets") or {}).items():
        for side, changes in (data.get("sides") or {}).items():
            try:
                change = float(changes["handle_pct_change"])
            except (KeyError, TypeError, ValueError):
                continue
            label = f"{market} {side} {change:+.0%} handle"
            if strongest is None or abs(change) > strongest[0]:
                strongest = (abs(change), label)
    return "" if strongest is None else strongest[1]


def voice_line(row: dict[str, Any]) -> str:
    """One line per committee voice: ``Schedule Seahawks 66% ★★★ · 20-26``."""
    name = _esc(voice_name(row))
    if str(row.get("expert_id") or "") == "pikkit":
        phase = _pikkit_phase_label(row)
        book = _pikkit_book_outcomes(row)
        movement = _pikkit_strongest_movement(row)
        details = []
        if movement:
            details.append(f"move: {movement}")
        if book:
            details.append(f"book benefits: {book}")
        suffix = " · " + " · ".join(details) if details else ""
        return (
            f"<b>{name}</b> <i>Shadow · {_esc(phase)}</i> · "
            f"{_esc(_labeled_legs(row))}{_esc(suffix)}"
        )
    if str(row.get("pick_market") or "") == "side_and_total" and row.get("side_pick_json"):
        return f"<b>{name}</b> {_esc(_labeled_legs(row))}"
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
    text = f"<b>{name}</b> {_esc(head)}"
    if score:
        text += f" · {score}"
    return text


def consensus_line(desk: GameDesk) -> str:
    counts: dict[str, int] = {}
    for row in desk.approved_voices:
        if str(row.get("expert_id") or "") in desk.shadow_experts:
            continue
        if str(row.get("pick_market") or "") == "side_and_total":
            side, _ = arm_legs(row)
            winner = (
                str(side.get("selection") or "").strip()
                if leg_is_bet(side)
                else ""
            )
        else:
            winner = str(row.get("predicted_winner") or "").strip()
        if winner:
            counts[winner] = counts.get(winner, 0) + 1
    if not counts:
        return ""
    ordered = sorted(counts.items(), key=lambda item: (-item[1], item[0]))
    if len(ordered) > 1 and ordered[0][1] == ordered[1][1]:
        score = "–".join(str(count) for _, count in ordered)
        return f"<b>Consensus</b> · split {score}"
    winner, count = ordered[0]
    return (
        f"<b>Consensus</b> · {_esc(nickname(winner))} "
        f"{count}–{sum(counts.values()) - count}"
    )


def _why_block(desk: GameDesk) -> str:
    """Collapsed on the card: the arms' theses and factors, then one thesis
    per voice."""
    pieces: list[str] = []
    for label, row in (("Rules", desk.rules), ("Judge", desk.judge)):
        if row is None:
            continue
        thesis = _clip(row.get("thesis"), 260)
        segment = [f"<b>{label}</b>" + (f" · {_esc(thesis)}" if thesis else "")]
        for factor in _factor_texts(row.get("supporting_factors_json"), 3):
            segment.append(f"• {_esc(_clip(factor, 200))}")
        for factor in _factor_texts(row.get("counterarguments_json"), 2):
            segment.append(f"◦ {_esc(_clip(factor, 200))}")
        pieces.append("\n".join(segment))
    for row in desk.approved_voices:
        thesis = _clip(row.get("thesis"), 220)
        if thesis:
            pieces.append(f"<b>{_esc(voice_name(row))}</b> · {_esc(thesis)}")
    text = "\n".join(pieces)
    if len(text) > WHY_CHARS:
        text = text[: WHY_CHARS - 1].rstrip() + "…"
    return text


def picks_opinion_groups(
    desk: GameDesk,
) -> list[tuple[str, str, list[str]]]:
    """Selectable opinions, with both God arms fixed above other experts."""
    labeled_rows: list[tuple[str, dict[str, Any] | None]] = [
        ("God Rules", desk.rules),
        ("God Judge", desk.judge),
    ]
    labeled_rows.extend(
        (voice_name(row), row) for row in desk.approved_voices
    )
    groups = []
    for label, row in labeled_rows:
        if row is None:
            continue
        expert_id = str(row.get("expert_id") or "")
        detail_rows = [row]
        context = "Approved committee"
        if expert_id == "pikkit":
            detail_rows = sorted(
                (
                    candidate
                    for candidate in desk.approved
                    if str(candidate.get("expert_id") or "") == "pikkit"
                ),
                key=row_key,
            )
            context = "Shadow Pikkit Expert · initial and final"
        groups.append(
            (
                expert_id,
                label,
                render_opinion_details(detail_rows, context=context),
            )
        )
    return groups


def resolve_picks_view(
    desk: GameDesk,
    raw: Any,
) -> str | dict[str, Any] | None:
    """Normalize persisted Picks view state, including the old flat pager."""
    if raw is True or (isinstance(raw, int) and not isinstance(raw, bool)):
        return "menu"
    if raw == "menu":
        return "menu"
    if not isinstance(raw, dict) or raw.get("mode") != "opinion":
        return None
    groups = picks_opinion_groups(desk)
    if not groups:
        return None
    try:
        expert = str(raw.get("expert") or "")
        if expert:
            opinion = next(
                index
                for index, (key, _, _) in enumerate(groups)
                if key == expert
            )
        else:
            opinion = max(
                0,
                min(int(raw.get("opinion", 0)), len(groups) - 1),
            )
        chunks = groups[opinion][2]
        chunk = max(0, min(int(raw.get("chunk", 0)), len(chunks) - 1))
    except (StopIteration, TypeError, ValueError):
        return "menu"
    return {
        "mode": "opinion",
        "expert": groups[opinion][0],
        "chunk": chunk,
    }


def render_picks_card(
    desk: GameDesk,
    *,
    config: DeskConfig,
    view: Any = None,
) -> tuple[str, Keyboard]:
    """Compact index, opinion picker, or one opinion in the same message."""
    game = desk.game
    groups = picks_opinion_groups(desk)
    selected = resolve_picks_view(desk, view)
    header = (
        f"🏈 <b>{_esc(teams_label(game))}</b> · "
        f"{kickoff_label(desk.kickoff)} ET"
    )
    summary_lines = [
        header,
        "",
        "<b>GOD EXPERT</b>",
        *god_pick_lines(desk),
    ]
    if desk.approved_voices:
        summary_lines += [
            "",
            "<b>EXPERTS</b>",
            *[voice_line(row) for row in desk.approved_voices],
        ]
        consensus = consensus_line(desk)
        if consensus:
            summary_lines += ["", consensus]
    if selected == "menu" and groups:
        keyboard = [
            [
                _button(
                    label,
                    callback=f"{CALLBACK_PREFIX}op:{desk.event_id}:{expert}",
                )
            ]
            for expert, label, _ in groups
        ]
        keyboard.append(
            [
                _button(
                    "Refresh opinions",
                    callback=f"{CALLBACK_PREFIX}refresh:{desk.event_id}",
                ),
                _button(
                    "Back to picks",
                    callback=f"{CALLBACK_PREFIX}hide:{desk.event_id}",
                )
            ]
        )
        return "\n".join(
            [*summary_lines, "", "<b>Select an opinion</b>"]
        ), keyboard
    if isinstance(selected, dict) and groups:
        expert = selected["expert"]
        opinion = next(
            index
            for index, (key, _, _) in enumerate(groups)
            if key == expert
        )
        chunk = selected["chunk"]
        _, _, details = groups[opinion]
        lines = [
            header,
            "",
            details[chunk],
        ]
        navigation: list[dict[str, str]] = []
        if chunk > 0:
            navigation.append(
                _button(
                    "Previous",
                    callback=(
                        f"{CALLBACK_PREFIX}part:{desk.event_id}:"
                        f"{expert}:{chunk - 1}"
                    ),
                )
            )
        navigation.append(
            _button(
                f"{chunk + 1}/{len(details)}",
                callback=(
                    f"{CALLBACK_PREFIX}part:{desk.event_id}:"
                    f"{expert}:{chunk}"
                ),
            )
        )
        if chunk + 1 < len(details):
            navigation.append(
                _button(
                    "Next",
                    callback=(
                        f"{CALLBACK_PREFIX}part:{desk.event_id}:"
                        f"{expert}:{chunk + 1}"
                    ),
                )
            )
        return "\n".join(lines), [
            navigation,
            [
                _button(
                    "Back to opinions",
                    callback=f"{CALLBACK_PREFIX}show:{desk.event_id}",
                ),
                _button(
                    "Back to picks",
                    callback=f"{CALLBACK_PREFIX}hide:{desk.event_id}",
                ),
            ],
        ]

    keyboard = (
        [[_button("Show full opinions", callback=f"{CALLBACK_PREFIX}show:{desk.event_id}")]]
        if groups
        else []
    )
    return "\n".join(summary_lines), keyboard


def _line_value(value: Any, *, signed: bool = False) -> str:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return "—"
    text = f"{number:g}"
    if signed and number > 0:
        text = f"+{text}"
    return text


def _line_with_price(line: Any, price: Any, *, signed: bool = False) -> str:
    line_text = _line_value(line, signed=signed)
    price_text = _line_value(price, signed=True)
    return line_text if price_text == "—" else f"{line_text} ({price_text})"


def _captured_label(value: Any) -> str:
    if not value:
        return ""
    try:
        return clock_label(_parse_time(value))
    except (TypeError, ValueError):
        return ""


def render_offline_card(
    desk: GameDesk,
    *,
    latest_market: dict[str, Any] | None = None,
    team_abbrevs: dict[str, str] | None = None,
) -> tuple[str, Keyboard]:
    """One self-contained, keyboard-free game snapshot for offline reading."""
    game = desk.game
    away = _abbrev(game.get("away_team"), team_abbrevs)
    home = _abbrev(game.get("home_team"), team_abbrevs)
    lines = [
        (
            f"🏈 <b>{_esc(teams_label(game))}</b> · "
            f"{kickoff_label(desk.kickoff)} ET"
        ),
        "",
        "<b>LATEST LINES</b>",
    ]
    market = latest_market or {}
    if not market or all(
        market.get(key) is None
        for key in ("away_spread", "home_spread", "away_moneyline", "home_moneyline", "total")
    ):
        lines.append("No full-game line data yet.")
    else:
        lines.extend(
            [
                (
                    f"<b>Spread</b> · {_esc(away)} "
                    f"{_esc(_line_with_price(market.get('away_spread'), market.get('away_spread_price'), signed=True))}"
                    f" · {_esc(home)} "
                    f"{_esc(_line_with_price(market.get('home_spread'), market.get('home_spread_price'), signed=True))}"
                ),
                (
                    f"<b>Moneyline</b> · {_esc(away)} "
                    f"{_esc(_line_value(market.get('away_moneyline'), signed=True))}"
                    f" · {_esc(home)} "
                    f"{_esc(_line_value(market.get('home_moneyline'), signed=True))}"
                ),
                (
                    f"<b>Total</b> · {_esc(_line_value(market.get('total')))} "
                    f"(O {_esc(_line_value(market.get('over_price'), signed=True))} / "
                    f"U {_esc(_line_value(market.get('under_price'), signed=True))})"
                ),
            ]
        )
        metadata = [
            str(market.get("bookmaker") or "").strip(),
            _captured_label(market.get("captured_at")),
        ]
        metadata = [item for item in metadata if item]
        if metadata:
            lines.append(f"<i>{_esc(' · '.join(metadata))}</i>")
    lines.extend(["", "<b>MOE PICKS</b>", *god_pick_lines(desk)])
    if desk.approved_voices:
        lines.extend(voice_line(row) for row in desk.approved_voices)
        consensus = consensus_line(desk)
        if consensus:
            lines.extend(["", consensus])
    else:
        lines.append("<i>No approved expert picks yet.</i>")
    missing = [*desk.missing_required, *desk.missing_optional]
    if missing:
        lines.append(
            "<i>No opinion yet · "
            + _esc(
                " · ".join(
                    VOICE_NAMES.get(expert_id, expert_abbreviation(expert_id))
                    for expert_id in missing
                )
            )
            + "</i>"
        )
    return "\n".join(lines), []


def _week_label(desks: Iterable[GameDesk]) -> str:
    weeks = sorted(
        {int(desk.week) for desk in desks if desk.week.isdigit()}
    )
    if not weeks:
        return ""
    if len(weeks) == 1:
        return f"Week {weeks[0]}"
    return f"Weeks {weeks[0]}–{weeks[-1]}"


def _teams_short(desk: GameDesk, team_abbrevs: dict[str, str] | None) -> str:
    away = _abbrev(desk.game.get("away_team"), team_abbrevs)
    home = _abbrev(desk.game.get("home_team"), team_abbrevs)
    return _esc(f"{away} @ {home}")


def _week_legs(desk: GameDesk) -> str:
    parts = []
    parts.append(_short_legs(desk.rules) if desk.rules is not None else "rules —")
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
    """One line per decided game (an approved God arm), then which required
    voices still have no approved row — those are the games the judge
    runner skips as "committee incomplete"."""
    active = [desk for desk in desks if not desk.started]
    decided = [desk for desk in active if desk.rules is not None or desk.judge is not None]
    week = _week_label(active)
    head = "🧠 <b>God Expert</b>"
    if week:
        head += f" · {week}"
    lines = [head]
    if not decided:
        lines.append("No decided games yet.")
    for desk in decided:
        lines.append(
            f"<b>{_teams_short(desk, team_abbrevs)}</b> {short_kickoff(desk.kickoff)} · "
            f"{_esc(_week_legs(desk))}"
        )
    missing_counts: dict[str, int] = {}
    for desk in active:
        for expert_id in desk.missing_required:
            missing_counts[expert_id] = missing_counts.get(expert_id, 0) + 1
    if missing_counts:
        lines.append(
            _esc(
                "Waiting on: "
                + " · ".join(
                    f"{expert_abbreviation(expert_id)} {count}"
                    for expert_id, count in sorted(missing_counts.items())
                )
            )
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


def render_withdrawal_alert(
    bet_entry: dict[str, Any], arm_row: dict[str, Any], kind: str
) -> str:
    """``🔕 Withdrawn · 49ers +3.5 (-102) ★ 0.8u · rules arm — ev floor``.

    ``bet_entry`` is the announced-bet state record carrying the leg label
    as it was alerted; the reason comes from the superseding row's PASS leg.
    """
    side, total = arm_legs(arm_row)
    leg = side if kind == "side" else total
    arm = ARM_LABELS.get(str(arm_row.get("expert_id")), "Arm").lower()
    text = f"🔕 Withdrawn · {bet_entry.get('leg') or 'bet'} · {arm} arm"
    reason = str((leg or {}).get("pass_reason") or "").strip()
    if reason:
        text += f" — {reason}"
    return _esc(text)


def render_scores_notice(text: str) -> str:
    """Proportional text with a bold first line — never <pre>.

    The digest is phone-width prose lines now; the aligned-columns era
    rendered as <pre>, which a mobile bubble (~24 monospace chars) wrapped
    mid-number into soup (operator-reported, 2026-09-10).
    """
    head, sep, rest = _esc(text).partition("\n")
    return f"<b>{head}</b>{sep}{rest}"


# --------------------------------------------------------------------------
# state


def empty_state() -> dict[str, Any]:
    return {
        "version": STATE_VERSION,
        "cards": {},
        "announced": {},
        "kickoffs": {},
        "expanded_picks": {},
    }


def load_state(path: str | Path) -> dict[str, Any]:
    path = Path(path)
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return empty_state()
    if not isinstance(raw, dict) or raw.get("version") != STATE_VERSION:
        return empty_state()
    state = empty_state()
    for key in ("cards", "announced", "kickoffs", "expanded_picks"):
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


def prune_state(
    state: dict[str, Any],
    *,
    now: datetime,
    preserve_event_ids: Iterable[str] = (),
) -> None:
    """Drop cards and announcements for games that kicked off more than the
    retention window ago, so the file stays the size of one slate."""
    preserve = set(preserve_event_ids)
    expired = set()
    for event_id, kickoff in list(state["kickoffs"].items()):
        try:
            if _parse_time(kickoff) < now - RETENTION:
                expired.add(event_id)
        except ValueError:
            expired.add(event_id)
    expired -= preserve
    for event_id in expired:
        state["kickoffs"].pop(event_id, None)
        state["expanded_picks"].pop(event_id, None)
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
    deleted: list[str] = field(default_factory=list)
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


def _cleanup_obsolete(
    *,
    key: str,
    entry: dict[str, Any],
    config: DeskConfig,
    api: BotApi,
    summary: SyncSummary,
) -> None:
    remaining: list[int] = []
    for raw_message_id in entry.get("obsolete_message_ids") or []:
        try:
            message_id = int(raw_message_id)
        except (TypeError, ValueError):
            continue
        try:
            deleted = api.delete(config.chat_id, message_id)
        except DeskApiError as exc:
            summary.errors.append(f"{key}: obsolete delete failed: {exc}")
            remaining.append(message_id)
            continue
        if deleted:
            summary.deleted.append(f"{key}:obsolete:{message_id}")
        else:
            summary.errors.append(
                f"{key}: obsolete delete failed for {message_id}"
            )
            remaining.append(message_id)
    if remaining:
        entry["obsolete_message_ids"] = remaining
    else:
        entry.pop("obsolete_message_ids", None)


def _delete_entry_messages(
    *,
    key: str,
    entry: dict[str, Any],
    config: DeskConfig,
    api: BotApi,
    summary: SyncSummary,
) -> bool:
    active_remaining: int | None = None
    obsolete_remaining: list[int] = []
    raw_active = entry.get("message_id")
    try:
        active_id = int(raw_active)
    except (TypeError, ValueError):
        active_id = None
    raw_obsolete = entry.get("obsolete_message_ids") or []
    for is_active, raw_message_id in [
        (True, active_id),
        *[(False, value) for value in raw_obsolete],
    ]:
        try:
            message_id = int(raw_message_id)
        except (TypeError, ValueError):
            continue
        try:
            deleted = api.delete(config.chat_id, message_id)
        except DeskApiError as exc:
            summary.errors.append(f"{key}: delete failed: {exc}")
            if is_active:
                active_remaining = message_id
            else:
                obsolete_remaining.append(message_id)
            continue
        if not deleted:
            summary.errors.append(f"{key}: delete failed for {message_id}")
            if is_active:
                active_remaining = message_id
            else:
                obsolete_remaining.append(message_id)
    if active_remaining is None:
        entry.pop("message_id", None)
        entry.pop("hash", None)
        entry.pop("reply_to", None)
    else:
        entry["message_id"] = active_remaining
    if obsolete_remaining:
        entry["obsolete_message_ids"] = obsolete_remaining
    else:
        entry.pop("obsolete_message_ids", None)
    if active_remaining is None and not obsolete_remaining:
        return True
    return False


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
    reply_to: int | None = None,
    edit_only: bool = False,
) -> int | None:
    digest = content_hash(text, keyboard, topic)
    entry = state["cards"].get(key)
    if isinstance(entry, dict):
        _cleanup_obsolete(
            key=key,
            entry=entry,
            config=config,
            api=api,
            summary=summary,
        )
    if isinstance(entry, dict) and entry.get("message_id"):
        if entry.get("reply_to") != reply_to:
            if not budget.take():
                summary.deferred.append(key)
                return int(entry["message_id"])
            try:
                replacement_id = api.send(
                    config.chat_id,
                    topic,
                    text,
                    keyboard=keyboard,
                    silent=True,
                    reply_to=reply_to,
                )
            except DeskApiError as exc:
                summary.errors.append(f"{key}: {exc}")
                return int(entry["message_id"])
            old_id = int(entry["message_id"])
            obsolete = [
                old_id,
                *[
                    int(value)
                    for value in entry.get("obsolete_message_ids") or []
                ],
            ]
            replacement = {
                "message_id": replacement_id,
                "hash": digest,
                "topic": topic,
                "reply_to": reply_to,
                "obsolete_message_ids": obsolete,
            }
            state["cards"][key] = replacement
            summary.posted.append(key)
            _cleanup_obsolete(
                key=key,
                entry=replacement,
                config=config,
                api=api,
                summary=summary,
            )
            return replacement_id
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
        if edit_only:
            summary.errors.append(f"{key}: existing message could not be edited")
            return None
        state["cards"].pop(key, None)
    if edit_only:
        summary.errors.append(f"{key}: existing message is not tracked")
        return None
    if not budget.take():
        summary.deferred.append(key)
        return None
    try:
        message_id = api.send(
            config.chat_id,
            topic,
            text,
            keyboard=keyboard,
            silent=True,
            reply_to=reply_to,
        )
    except DeskApiError as exc:
        summary.errors.append(f"{key}: {exc}")
        return None
    replacement = {
        "message_id": message_id,
        "hash": digest,
        "topic": topic,
        "reply_to": reply_to,
    }
    if isinstance(entry, dict) and entry.get("obsolete_message_ids"):
        replacement["obsolete_message_ids"] = list(
            entry["obsolete_message_ids"]
        )
    state["cards"][key] = replacement
    summary.posted.append(key)
    if pin:
        api.pin(config.chat_id, message_id)
    return message_id


def _remove_legacy_picks_details(
    *,
    config: DeskConfig,
    api: BotApi,
    state: dict[str, Any],
    summary: SyncSummary,
) -> set[str]:
    failed_events: set[str] = set()
    for key in [
        key for key in state["cards"] if key.startswith("picks-detail:")
    ]:
        entry = state["cards"][key]
        if _delete_entry_messages(
            key=key,
            entry=entry,
            config=config,
            api=api,
            summary=summary,
        ):
            state["cards"].pop(key, None)
            summary.deleted.append(key)
        else:
            _, _, remainder = key.partition(":")
            event_id, _, _ = remainder.partition(":")
            if event_id:
                failed_events.add(event_id)
    return failed_events


def _selected_picks_view(
    state: dict[str, Any],
    desk: GameDesk,
) -> str | dict[str, Any] | None:
    selected = resolve_picks_view(
        desk,
        state["expanded_picks"].get(desk.event_id),
    )
    if selected is None:
        state["expanded_picks"].pop(desk.event_id, None)
    else:
        state["expanded_picks"][desk.event_id] = selected
    return selected


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
    extra: dict[str, Any] | None = None,
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
    entry: dict[str, Any] = {"at": now.isoformat(), "event_id": event_id}
    if extra:
        entry.update(extra)
    state["announced"][key] = entry
    summary.alerts.append(key)


def _announced_bet(
    state: dict[str, Any], *, event_id: str, expert_id: str, kind: str
) -> tuple[str, dict[str, Any]] | None:
    """The latest announced bet for this event+arm+kind: (opinion_id, entry).

    Only entries that recorded ``expert_id`` participate — legacy entries
    predate the withdrawal alert, and firing from them on first deploy
    would spray alerts for every game whose arm currently passes.
    """
    best: tuple[str, str, dict[str, Any]] | None = None
    for key, entry in state["announced"].items():
        if not key.startswith("bet:") or not isinstance(entry, dict):
            continue
        if (
            entry.get("event_id") != event_id
            or entry.get("expert_id") != expert_id
            or entry.get("kind") != kind
        ):
            continue
        _, _, remainder = key.partition(":")
        opinion_id, _, _ = remainder.partition(":")
        at = str(entry.get("at") or "")
        # >= so an equal timestamp resolves to the later-inserted entry —
        # insertion order is announcement order.
        if best is None or at >= best[0]:
            best = (at, opinion_id, entry)
    return None if best is None else (best[1], best[2])


def _drop_review_topic_state(state: dict[str, Any]) -> None:
    """Forget the Review topic's cards (review:* and the pinned queue) and
    its lock alerts without any API calls: the topic was deleted 2026-09-10
    and its messages died with it, so a delete attempt could only fail."""
    for key in list(state["cards"]):
        if key == "queue" or key.startswith("review:"):
            state["cards"].pop(key, None)
    for key in list(state["announced"]):
        if key.startswith("lock:"):
            state["announced"].pop(key, None)


def sync_desk(
    *,
    config: DeskConfig,
    api: BotApi,
    state: dict[str, Any],
    desks: Iterable[GameDesk],
    now: datetime,
    team_abbrevs: dict[str, str] | None = None,
    latest_markets: dict[str, dict[str, Any]] | None = None,
    max_posts: int = MAX_POSTS_PER_SYNC,
    priority_event_id: str | None = None,
    edit_only_event_id: str | None = None,
) -> SyncSummary:
    """Reconcile the group with the model: post missing cards, edit changed
    ones, announce new bet legs and withdrawals of announced bets once
    each. Idempotent — an unchanged model is a no-op — and bounded to
    ``max_posts`` new messages per pass so a first run over a full slate
    spreads across passes."""
    desks = list(desks)
    summary = SyncSummary()
    priority = None
    if priority_event_id:
        desks.sort(
            key=lambda desk: (
                desk.event_id != priority_event_id,
                desk.kickoff,
                desk.event_id,
            )
        )
        priority = next(
            (
                desk
                for desk in desks
                if desk.event_id == priority_event_id
            ),
            None,
        )
        if not any(desk.event_id == priority_event_id for desk in desks):
            summary.errors.append(
                f"picks:{priority_event_id}: game is no longer available"
            )
    budget = _Budget(max_posts)
    latest_markets = latest_markets or {}
    _drop_review_topic_state(state)
    preserve = _remove_legacy_picks_details(
        state=state,
        config=config,
        api=api,
        summary=summary,
    )
    prune_state(state, now=now, preserve_event_ids=preserve)
    for desk in desks:
        state["kickoffs"][desk.event_id] = desk.kickoff.isoformat()
        if config.offline_topic:
            offline_key = f"offline:{desk.event_id}"
            if desk.show_offline and (
                not desk.started or offline_key not in state["cards"]
            ):
                text, keyboard = render_offline_card(
                    desk,
                    latest_market=latest_markets.get(desk.event_id),
                    team_abbrevs=team_abbrevs,
                )
                _upsert_card(
                    key=offline_key,
                    topic=config.offline_topic,
                    text=text,
                    keyboard=keyboard,
                    config=config,
                    api=api,
                    state=state,
                    budget=budget,
                    summary=summary,
                )
            elif not desk.show_offline and offline_key in state["cards"]:
                if _delete_entry_messages(
                    key=offline_key,
                    entry=state["cards"][offline_key],
                    config=config,
                    api=api,
                    summary=summary,
                ):
                    state["cards"].pop(offline_key, None)
                    summary.deleted.append(offline_key)
        if desk.started:
            picks_key = f"picks:{desk.event_id}"
            existing = state["cards"].get(picks_key)
            if desk.show_picks and isinstance(existing, dict):
                text, keyboard = render_picks_card(
                    desk,
                    config=config,
                    view=_selected_picks_view(state, desk),
                )
                picks_id = _upsert_card(
                    key=picks_key,
                    topic=config.picks_topic,
                    text=text,
                    keyboard=keyboard,
                    config=config,
                    api=api,
                    state=state,
                    budget=budget,
                    summary=summary,
                    edit_only=edit_only_event_id == desk.event_id,
                )
            elif priority_event_id == desk.event_id:
                summary.errors.append(
                    f"picks:{desk.event_id}: card is no longer available"
                )
            continue
        picks_id = None
        if desk.show_picks:
            text, keyboard = render_picks_card(
                desk,
                config=config,
                view=_selected_picks_view(state, desk),
            )
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
                edit_only=edit_only_event_id == desk.event_id,
            )
            for arm_row in (desk.rules, desk.judge):
                if arm_row is None:
                    continue
                expert_id = str(arm_row.get("expert_id") or "")
                side, total = arm_legs(arm_row)
                for kind, leg in (("side", side), ("total", total)):
                    if leg_is_bet(leg):
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
                            extra={
                                "expert_id": expert_id,
                                "kind": kind,
                                "leg": leg_label(leg, kind=kind),
                            },
                        )
                        continue
                    # The arm's latest row no longer bets this kind: if an
                    # earlier row's bet was announced, say so once — loudly,
                    # keyed by the withdrawn bet's opinion id so a re-bet
                    # and later re-withdrawal alert again.
                    prior = _announced_bet(
                        state,
                        event_id=desk.event_id,
                        expert_id=expert_id,
                        kind=kind,
                    )
                    if prior is None:
                        continue
                    bet_opinion_id, bet_entry = prior
                    _announce(
                        key=f"withdrawn:{bet_opinion_id}:{kind}",
                        event_id=desk.event_id,
                        topic=config.picks_topic,
                        text=render_withdrawal_alert(bet_entry, arm_row, kind),
                        reply_to=picks_id,
                        config=config,
                        api=api,
                        state=state,
                        budget=budget,
                        summary=summary,
                        now=now,
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


def post_scores_notice(
    text: str,
    *,
    html: str | None = None,
    environ: Any = None,
    api: BotApi | None = None,
) -> bool:
    """Post the grading digest into the Scores topic. ``False`` when the desk
    or its Scores topic is not configured, or the post failed — the caller
    falls back to the watchdog DM. ``html`` is a pre-rendered Bot API HTML
    digest sent as-is; without it, ``text`` goes through
    ``render_scores_notice``."""
    config = desk_config_from_env(environ)
    if config is None or not config.scores_topic:
        return False
    api = api or BotApi(config.bot_token)
    try:
        api.send(
            config.chat_id,
            config.scores_topic,
            html if html else render_scores_notice(text),
            silent=True,
        )
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
    """``desk:show:<event>``; ``desk:hide:<event>``;
    ``desk:op:<event>:<expert>``; ``desk:part:<event>:<expert>:<chunk>``;
    ``desk:refresh:<event>``. Anything else — including the removed review
    actions ``ok``/``no``/``okarms`` — returns ``None``."""
    if not data.startswith(CALLBACK_PREFIX):
        return None
    action, _, target = data[len(CALLBACK_PREFIX):].partition(":")
    if action not in {
        "show",
        "hide",
        "op",
        "part",
        "page",
        "refresh",
    } or not target:
        return None
    return action, target
