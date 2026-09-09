#!/usr/bin/env python3
"""Tests for the desk group (moe_desk.py): model, renderers, sync, transport.

Pure Python — no Telethon, no ``moe`` (fcntl) — so it runs on Windows too:
``python -m unittest scripts.test_moe_desk``.
"""

from __future__ import annotations

import io
import json
import sys
import tempfile
import unittest
import urllib.error
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import moe_desk
from moe_desk import (
    BotApi,
    DeskApiError,
    DeskConfig,
    build_desks,
    committee_experts,
    content_hash,
    desk_config_from_env,
    desk_ids_report,
    empty_state,
    leg_label,
    load_state,
    lock_warning_due,
    parse_callback,
    parse_start_param,
    post_scores_notice,
    prune_state,
    render_opinion_details,
    render_picks_card,
    render_queue_card,
    render_review_card,
    render_week_card,
    review_targets,
    resolve_picks_view,
    save_state,
    sync_desk,
    topic_id_from_reply,
)

NOW = datetime(2026, 9, 12, 13, 0, tzinfo=timezone.utc)  # Saturday, 9 AM ET
SEA_KICKOFF = "2026-09-13T20:05:00+00:00"  # Sunday 4:05 PM ET
LAR_KICKOFF = "2026-09-13T20:25:00+00:00"
REGISTRY = {
    "experts": {
        "schedule": {"enabled": True, "mode": "agent", "name": "Schedule Expert"},
        "divisional": {"enabled": True, "mode": "agent", "name": "Divisional Expert"},
        "win_total": {"enabled": True, "mode": "agent", "name": "Win Total Expert"},
        "ak": {"enabled": True, "mode": "human_calibration", "name": "AK Expert"},
        "rating_elo": {"enabled": True, "mode": "model", "name": "Rating Expert (Elo)"},
        "cee": {
            "enabled": True,
            "mode": "agent",
            "committee_optional": True,
            "name": "Cee Expert",
        },
        "disabled": {"enabled": False, "mode": "agent"},
        "god_rules": {"enabled": True, "mode": "aggregator"},
        "god_judge": {"enabled": True, "mode": "aggregator_judge"},
    }
}
CONFIG = DeskConfig(
    bot_token="token",
    chat_id="-1001",
    review_topic=11,
    picks_topic=22,
    scores_topic=33,
    bot_username="nflguesser_bot",
)
SEA_SIDE = {
    "confidence_stars": 1,
    "edge": 0.0329,
    "ev_per_unit": 0.0225,
    "fair_probability": 0.4783,
    "line": -3.5,
    "price": 100,
    "probability": 0.5112,
    "selection": "Seattle Seahawks",
    "stake_fraction": 0.0056,
    "stake_units": 0.6,
}
OVER = {
    "confidence_stars": 1,
    "edge": 0.033,
    "line": 44.5,
    "price": -105,
    "probability": 0.5222,
    "selection": "Over",
    "stake_units": 0.5,
}
PASS_ADVERSE = {
    "selection": "PASS",
    "line": None,
    "price": None,
    "probability": 0.51,
    "edge": 0.03,
    "confidence_stars": 1,
    "stake_units": 0.0,
    "pass_reason": "adverse move",
}
PASS_PLAIN = {**PASS_ADVERSE, "pass_reason": None}


def game(event_id, away, home, kickoff, *, week=1, status="upcoming"):
    return {
        "event_id": event_id,
        "season": 2026,
        "week": week,
        "status": status,
        "commence_time_utc": kickoff,
        "away_team": away,
        "home_team": home,
    }


def row(
    opinion_id,
    event_id,
    expert_id,
    *,
    status="pending",
    model="claude-opus-4-8",
    generated="2026-09-12T12:00:00+00:00",
    generation_status="valid",
    **extra,
):
    names = {
        "schedule": "Schedule Expert",
        "divisional": "Divisional Expert",
        "win_total": "Win Total Expert",
        "ak": "AK Expert",
        "rating_elo": "Rating Expert (Elo)",
        "cee": "Cee Expert",
        "god_rules": "God Expert (Rules)",
        "god_judge": "God Expert (Judge)",
    }
    base = {
        "opinion_id": opinion_id,
        "event_id": event_id,
        "expert_id": expert_id,
        "expert_name": names[expert_id],
        "model": model,
        "generated_at_utc": generated,
        "generation_status": generation_status,
        "review_status": status,
        "reviewed_by": "SS" if status in {"approved", "rejected"} else "",
        "reviewed_at_utc": "2026-09-12T12:41:00+00:00" if status != "pending" else "",
        "review_note": "",
        "away_team": "New England Patriots",
        "home_team": "Seattle Seahawks",
        "predicted_winner": "Seattle Seahawks",
        "home_win_probability": 0.61,
        "predicted_away_score": 20,
        "predicted_home_score": 24,
        "confidence_stars": 2,
        "thesis": "Seattle's 10.5 season total vs New England's 7.5; both forecasters agree.",
        "pick_market": "side",
        "side_pick_json": "",
        "total_pick_json": "",
        "supporting_factors_json": "",
        "counterarguments_json": "",
        "generation_backend": "agent_runtime",
    }
    base.update(extra)
    return base


def arm_row(opinion_id, event_id, expert_id, side, total, **extra):
    extra.setdefault(
        "thesis",
        "God Expert (rules): side Seattle Seahawks -3.5 ★; total Over 44.5 ★.",
    )
    return row(
        opinion_id,
        event_id,
        expert_id,
        model="deterministic" if expert_id == "god_rules" else "claude-fable-5-1",
        pick_market="side_and_total",
        side_pick_json=json.dumps(side),
        total_pick_json=json.dumps(total),
        **extra,
    )


def committee(event_id="401", *, arms_status="pending"):
    return [
        row("a1", event_id, "schedule", status="approved"),
        row("a2", event_id, "divisional", status="approved"),
        row("p1", event_id, "win_total"),
        row("a3", event_id, "ak", status="approved"),
        row("a4", event_id, "rating_elo", status="approved", model="deterministic"),
        arm_row(
            "c884d868-0000",
            event_id,
            "god_rules",
            PASS_ADVERSE,
            {**PASS_PLAIN, "pass_reason": "ev floor"},
            status=arms_status,
            generated="2026-09-12T12:30:00+00:00",
        ),
        arm_row(
            "444bf3de-0000",
            event_id,
            "god_judge",
            PASS_PLAIN,
            PASS_PLAIN,
            status=arms_status,
            generated="2026-09-12T12:31:00+00:00",
            generation_backend="claude_headless",
        ),
    ]


def is_arm(r):
    return r["expert_id"] in ("god_rules", "god_judge")


def approved_of(rows):
    """Stand-in for moe.approved_opinions: the caller's hash-verified rows."""
    return [
        r
        for r in rows
        if r.get("generation_status", "valid") == "valid"
        and r.get("review_status") == "approved"
    ]


class FakeApi(BotApi):
    def __init__(self):  # noqa: D401 - no token, no network
        self.sent: list[dict] = []
        self.edits: list[dict] = []
        self.pins: list[int] = []
        self.missing: set[int] = set()
        self.deleted: list[int] = []
        self.next_id = 100

    def send(self, chat_id, thread_id, text, *, keyboard=None, silent=True, reply_to=None):
        self.next_id += 1
        self.sent.append(
            {
                "id": self.next_id,
                "chat": chat_id,
                "topic": thread_id,
                "text": text,
                "keyboard": keyboard,
                "silent": silent,
                "reply_to": reply_to,
            }
        )
        return self.next_id

    def edit(self, chat_id, message_id, text, *, keyboard=None):
        if message_id in self.missing:
            return False
        self.edits.append({"id": message_id, "text": text, "keyboard": keyboard})
        return True

    def pin(self, chat_id, message_id):
        self.pins.append(message_id)
        return True

    def delete(self, chat_id, message_id):
        self.deleted.append(message_id)
        return True


class ModelTests(unittest.TestCase):
    def test_committee_experts_mirror_the_runner_rule(self) -> None:
        self.assertEqual(
            committee_experts(REGISTRY),
            ["ak", "divisional", "rating_elo", "schedule", "win_total"],
        )

    def test_voice_status_and_arms(self) -> None:
        rows = committee()
        desk = build_desks(
            [game("401", "New England Patriots", "Seattle Seahawks", SEA_KICKOFF)],
            rows,
            approved_of(rows),
            REGISTRY,
            now=NOW,
        )[0]
        statuses = {expert_id: status for expert_id, status, _ in desk.voices}
        self.assertEqual(
            statuses,
            {
                "ak": "approved",
                "divisional": "approved",
                "rating_elo": "approved",
                "schedule": "approved",
                "win_total": "pending",
            },
        )
        self.assertNotIn("cee", statuses)  # optional and absent
        self.assertEqual((desk.required_approved, desk.required_total), (4, 5))
        # arms first, then voices oldest first
        self.assertEqual([r["opinion_id"] for r in desk.pending], ["c884d868-0000", "444bf3de-0000", "p1"])
        self.assertEqual([r["opinion_id"] for r in desk.reviewed], ["a3", "a2", "a4", "a1"])
        self.assertIsNone(desk.rules)
        self.assertFalse(desk.started)

    def test_optional_voice_shows_only_when_it_has_a_row(self) -> None:
        rows = committee() + [row("o1", "401", "cee", status="rejected")]
        desk = build_desks(
            [game("401", "New England Patriots", "Seattle Seahawks", SEA_KICKOFF)],
            rows,
            approved_of(rows),
            REGISTRY,
            now=NOW,
        )[0]
        self.assertIn(("cee", "rejected", False), desk.voices)
        self.assertEqual(desk.missing_required, [])

    def test_horizon_status_and_started_games(self) -> None:
        far = game("9", "A B", "C D", (NOW + timedelta(days=20)).isoformat())
        done = game("8", "A B", "C D", (NOW - timedelta(hours=1)).isoformat())
        old = game("7", "A B", "C D", (NOW - timedelta(days=5)).isoformat())
        final = game("6", "A B", "C D", SEA_KICKOFF, status="final")
        desks = build_desks([far, done, old, final], [], [], REGISTRY, now=NOW)
        self.assertEqual([d.event_id for d in desks], ["8"])
        self.assertTrue(desks[0].started)

    def test_pending_means_latest_valid_row_per_expert_and_model(self) -> None:
        rows = [
            # an old pending draft superseded by a newer approved row: hidden
            row("draft", "401", "schedule", generated="2026-09-09T00:00:00+00:00"),
            row("old", "401", "schedule", status="approved", generated="2026-09-10T00:00:00+00:00"),
            # a newer pending row on the same expert+model: actionable
            row("newer", "401", "schedule", generated="2026-09-11T00:00:00+00:00"),
            row("hk", "401", "schedule", status="approved", model="claude-haiku-4-5"),
            row("bad", "401", "schedule", generation_status="invalid"),
            row("sample", "401", "schedule", status="not_applicable", generation_status="sample"),
            row("odd", "401", "schedule", status="not_applicable"),  # never reviewable either
            row("pend", "401", "win_total"),
            row("rej", "401", "divisional", status="rejected"),
            arm_row("j-only", "401", "god_judge", PASS_PLAIN, PASS_PLAIN, generated="2026-09-10T00:00:00+00:00"),
            arm_row("r-old", "401", "god_rules", PASS_PLAIN, PASS_PLAIN, generated="2026-09-11T00:00:00+00:00"),
            arm_row("r-new", "401", "god_rules", PASS_PLAIN, PASS_PLAIN, generated="2026-09-12T00:00:00+00:00"),
        ]
        desk = build_desks(
            [game("401", "New England Patriots", "Seattle Seahawks", SEA_KICKOFF)],
            rows,
            approved_of(rows),
            REGISTRY,
            now=NOW,
        )[0]
        # rules before judge whatever their generation order, then voices oldest first
        self.assertEqual([r["opinion_id"] for r in desk.pending], ["r-new", "j-only", "newer", "pend"])
        # committee: one row per expert — latest approved on any model without a
        # registry default (hk is newer than old), the rejected divisional row
        self.assertEqual([r["opinion_id"] for r in desk.reviewed], ["rej", "hk"])
        with_default = {"experts": {**REGISTRY["experts"], "schedule": {**REGISTRY["experts"]["schedule"], "default_model": "claude-opus-4-8"}}}
        desk = build_desks(
            [game("401", "New England Patriots", "Seattle Seahawks", SEA_KICKOFF)],
            rows,
            approved_of(rows),
            with_default,
            now=NOW,
        )[0]
        self.assertEqual([r["opinion_id"] for r in desk.reviewed], ["rej", "old"])
        self.assertEqual([r["opinion_id"] for r in desk.review_rows][:4], ["r-new", "j-only", "newer", "pend"])


class RenderTests(unittest.TestCase):
    def desk(self, rows, event_id="401", kickoff=SEA_KICKOFF, now=NOW):
        return build_desks(
            [game(event_id, "New England Patriots", "Seattle Seahawks", kickoff)],
            rows,
            approved_of(rows),
            REGISTRY,
            now=now,
        )[0]

    def test_leg_labels(self) -> None:
        self.assertEqual(leg_label(SEA_SIDE, kind="side"), "Seahawks -3.5 (+100) ★ 0.6u")
        self.assertEqual(leg_label(OVER, kind="total"), "Over 44.5 (-105) ★ 0.5u")
        self.assertEqual(leg_label(PASS_ADVERSE, kind="side"), "PASS (adverse move)")
        self.assertEqual(leg_label(PASS_ADVERSE, kind="side", short_pass=True), "pass")
        self.assertEqual(leg_label(None, kind="side"), "—")
        self.assertEqual(
            leg_label(OVER, kind="total", with_stars=False), "Over 44.5 (-105)"
        )

    def test_review_card_lists_actionable_rows_arms_first_with_buttons(self) -> None:
        rows = committee()
        text, keyboard = render_review_card(self.desk(rows), config=CONFIG)
        self.assertIn("📥 <b>Patriots @ Seahawks</b> · Sun Sep 13 · 4:05 PM ET", text)
        self.assertIn("locks 2:05 PM ET · committee 4/5", text)
        self.assertLess(text.index("God Expert (Rules)"), text.index("God Expert (Judge)"))
        self.assertLess(text.index("God Expert (Judge)"), text.index("Win Total Expert"))
        self.assertIn("1 · <b>God Expert (Rules)</b> · <code>c884d868</code>", text)
        self.assertIn("2 · <b>God Expert (Judge)</b> · <code>444bf3de</code> · headless", text)
        self.assertIn("3 · <b>Win Total Expert</b> · <code>opus-4-8</code>", text)
        self.assertIn("Side PASS (adverse move) · Total PASS (ev floor) · p home .61", text)
        self.assertIn("Seahawks 61% ★★ · 20-24 · “Seattle", text)
        # the to-do card carries no approved opinions and no status board
        self.assertNotIn("Schedule Expert", text)
        self.assertNotIn("Committee", text)
        self.assertEqual(len(keyboard), 4)
        self.assertEqual([b["text"] for b in keyboard[2]], ["✅ 3", "❌ 3", "👁 3"])
        self.assertEqual(keyboard[0][0]["callback_data"], "desk:ok:c884d868-0000")
        self.assertEqual(keyboard[2][0]["callback_data"], "desk:ok:p1")
        self.assertEqual(keyboard[2][1]["callback_data"], "desk:no:p1")
        self.assertEqual(keyboard[2][2]["url"], "https://t.me/nflguesser_bot?start=op_p1")
        self.assertEqual(keyboard[3][0]["callback_data"], "desk:okarms:401")
        for row_buttons in keyboard:
            for button in row_buttons:
                if "callback_data" in button:
                    self.assertLessEqual(len(button["callback_data"].encode()), 64)

    def test_review_card_without_username_has_no_read_links(self) -> None:
        rows = committee()
        _, keyboard = render_review_card(
            self.desk(rows), config=DeskConfig("t", "-1", 1, 2)
        )
        self.assertEqual([b["text"] for b in keyboard[0]], ["✅ 1", "❌ 1"])

    def test_approve_both_arms_needs_both_pending(self) -> None:
        rows = committee()
        rows[-1]["review_status"] = "approved"
        _, keyboard = render_review_card(self.desk(rows), config=CONFIG)
        self.assertNotIn(
            "desk:okarms:401", [b.get("callback_data") for r in keyboard for b in r]
        )

    def test_review_card_with_nothing_pending_says_so(self) -> None:
        rows = [r for r in committee() if not is_arm(r) and r["opinion_id"] != "p1"]
        text, keyboard = render_review_card(self.desk(rows), config=CONFIG)
        self.assertIn("Nothing to review.", text)
        self.assertEqual(keyboard, [])

    def test_picks_card_lists_god_and_every_approved_voice(self) -> None:
        rows = committee(arms_status="approved")
        rows[5]["side_pick_json"] = json.dumps(SEA_SIDE)
        rows[5]["total_pick_json"] = json.dumps(OVER)
        rows[5]["supporting_factors_json"] = json.dumps(
            ["Pool p(home) .56 vs market .58", {"text": "Voices split 3-2"}]
        )
        rows[0]["thesis"] = "Seattle <stronger> at home"
        desk = self.desk(rows)
        self.assertTrue(desk.show_picks)
        text, keyboard = render_picks_card(desk, config=CONFIG)
        lines = text.split("\n")
        self.assertEqual(lines[0], "🏈 <b>Patriots @ Seahawks</b> · Sun Sep 13 · 4:05 PM ET")
        self.assertEqual(
            lines[2:5],
            [
                "<b>GOD EXPERT</b>",
                "<b>Rules</b> · Side Seahawks -3.5 (+100) ★ 0.6u · Total Over 44.5 (-105) ★ 0.5u",
                "<b>Judge</b> · Side pass · Total pass",
            ],
        )
        self.assertEqual(
            lines[7:11],
            [
                "<b>Schedule</b> Seahawks 61% ★★ · 20-24",
                "<b>Divisional</b> Seahawks 61% ★★ · 20-24",
                "<b>AK</b> Seahawks 61% ★★ · 20-24",
                "<b>Elo</b> Seahawks 61% ★★ · 20-24",
            ],
        )
        self.assertEqual(lines[-1], "<b>Consensus</b> · Seahawks 4–0")
        self.assertNotIn("<blockquote", text)
        self.assertNotIn("Win Total", text)  # pending, not approved
        self.assertEqual(
            keyboard,
            [[{"text": "Show full opinions", "callback_data": "desk:show:401"}]],
        )
        self.assertNotIn("t.me/nflguesser_bot", str(keyboard))

        picker_text, picker_keyboard = render_picks_card(
            desk,
            config=CONFIG,
            view="menu",
        )
        self.assertIn("<b>GOD EXPERT</b>", picker_text)
        self.assertIn(
            "<b>Schedule</b> Seahawks 61% ★★ · 20-24",
            picker_text,
        )
        self.assertIn("<b>Consensus</b> · Seahawks 4–0", picker_text)
        self.assertIn("<b>Select an opinion</b>", picker_text)
        self.assertEqual(
            [row[0]["text"] for row in picker_keyboard],
            [
                "God Rules",
                "God Judge",
                "Schedule",
                "Divisional",
                "AK",
                "Elo",
                "Back to picks",
            ],
        )
        self.assertTrue(
            all(
                len(button["callback_data"].encode("utf-8")) <= 64
                for row in picker_keyboard
                for button in row
            )
        )

        detail_text, detail_keyboard = render_picks_card(
            desk,
            config=CONFIG,
            view={"mode": "opinion", "opinion": 0, "chunk": 0},
        )
        self.assertIn("🔎 <b>God Expert (Rules)</b>", detail_text)
        self.assertIn("<blockquote expandable>", detail_text)
        self.assertEqual(
            detail_keyboard,
            [
                [
                    {"text": "1/1", "callback_data": "desk:part:401:god_rules:0"},
                ],
                [
                    {"text": "Back to opinions", "callback_data": "desk:show:401"},
                    {"text": "Back to picks", "callback_data": "desk:hide:401"},
                ],
            ],
        )

    def test_picks_card_states_pending_or_missing_god(self) -> None:
        desk = self.desk(committee())  # arms pending, four voices approved
        self.assertTrue(desk.show_picks)
        self.assertIn("<i>Pending review</i>", render_picks_card(desk, config=CONFIG)[0])
        rows = [r for r in committee() if not is_arm(r)]
        self.assertIn("<i>Not available</i>", render_picks_card(self.desk(rows), config=CONFIG)[0])
        rows = committee(arms_status="approved")
        rows[6]["review_status"] = "pending"
        self.assertIn(
            "<b>Rules</b> · Side pass · Total pass\n<b>Judge</b> · —",
            render_picks_card(self.desk(rows), config=CONFIG)[0],
        )

    def test_selected_expert_survives_god_opinions_becoming_available(self) -> None:
        without_god = self.desk(committee())
        selected = resolve_picks_view(
            without_god,
            {"mode": "opinion", "expert": "divisional", "chunk": 0},
        )
        with_god = self.desk(committee(arms_status="approved"))
        self.assertEqual(
            resolve_picks_view(with_god, selected),
            {"mode": "opinion", "expert": "divisional", "chunk": 0},
        )

    def test_full_opinions_use_persisted_text_and_safe_chunks(self) -> None:
        approved = row(
            "a1",
            "401",
            "schedule",
            status="approved",
            full_opinion="<" * 3200,
        )
        messages = render_opinion_details(
            [approved],
            context="Approved committee",
        )
        self.assertEqual(len(messages), 5)
        self.assertTrue(all(len(message) < 4096 for message in messages))
        self.assertTrue(all("&lt;" in message for message in messages))
        self.assertIn("part 1/5", messages[0])
        self.assertIn("Schedule Expert", messages[0])
        detail_text, detail_keyboard = render_picks_card(
            self.desk([approved]),
            config=CONFIG,
            view={"mode": "opinion", "opinion": 0, "chunk": 0},
        )
        self.assertIn("part 1/5", detail_text)
        self.assertIn(
            {"text": "Next", "callback_data": "desk:part:401:schedule:1"},
            detail_keyboard[0],
        )

    def test_picks_card_needs_two_voices_or_an_arm(self) -> None:
        lone = [row("a4", "401", "rating_elo", status="approved", model="deterministic")]
        self.assertFalse(self.desk(lone).show_picks)
        two = lone + [row("a1", "401", "schedule", status="approved")]
        self.assertTrue(self.desk(two).show_picks)
        arm = lone + [arm_row("r1", "401", "god_rules", PASS_PLAIN, PASS_PLAIN, status="approved")]
        self.assertTrue(self.desk(arm).show_picks)

    def test_consensus_does_not_count_a_side_pass(self) -> None:
        rows = [
            row("a1", "401", "schedule", status="approved"),
            row(
                "a2",
                "401",
                "divisional",
                status="approved",
                predicted_winner="New England Patriots",
            ),
            row(
                "a3",
                "401",
                "ak",
                status="approved",
                pick_market="side_and_total",
                side_pick_json=json.dumps(PASS_PLAIN),
                total_pick_json=json.dumps(PASS_PLAIN),
            ),
        ]
        text, _ = render_picks_card(self.desk(rows), config=CONFIG)
        self.assertIn("<b>Consensus</b> · split 1–1", text)
        self.assertNotIn("Seahawks 2–1", text)

    def test_queue_and_week_cards(self) -> None:
        rows = committee("401") + [
            arm_row("r2", "402", "god_rules", SEA_SIDE, PASS_PLAIN, status="approved"),
        ]
        desks = build_desks(
            [
                game("401", "New England Patriots", "Seattle Seahawks", SEA_KICKOFF),
                game("402", "San Francisco 49ers", "Los Angeles Rams", LAR_KICKOFF),
                game("403", "Dallas Cowboys", "Philadelphia Eagles", LAR_KICKOFF, week=2),
            ],
            rows,
            approved_of(rows),
            REGISTRY,
            now=NOW,
        )
        abbrevs = {"Seattle Seahawks": "SEA", "New England Patriots": "NE"}
        text, keyboard = render_queue_card(desks, team_abbrevs=abbrevs)
        self.assertEqual(keyboard, [])
        self.assertEqual(
            text.split("\n"),
            [
                "📥 <b>Review queue</b> · Weeks 1–2",
                "To review: <b>NE @ SEA</b> 3",
                "Committees: 0 of 3 complete · no row yet: AK 2 · Div 2 · Elo 2 · Sch 2 · WT 2",
            ],
        )
        text, _ = render_week_card(desks, team_abbrevs=abbrevs)
        self.assertEqual(
            text.split("\n"),
            [
                "🧠 <b>God Expert</b> · Weeks 1–2",
                "<b>49ers @ Rams</b> Sun 4:25 PM · Seahawks -3.5 (+100) ★ 0.6u · pass · judge —",
            ],
        )

    def test_empty_slate_cards(self) -> None:
        text, _ = render_queue_card([])
        self.assertEqual(
            text.split("\n"),
            ["📥 <b>Review queue</b>", "Nothing to review.", "Committees: 0 of 0 complete"],
        )
        text, _ = render_week_card([])
        self.assertEqual(text.split("\n"), ["🧠 <b>God Expert</b>", "No decided games yet."])


class SyncTests(unittest.TestCase):
    def setUp(self) -> None:
        self.api = FakeApi()
        self.state = empty_state()
        self.games = [
            game("401", "New England Patriots", "Seattle Seahawks", SEA_KICKOFF),
            game("402", "San Francisco 49ers", "Los Angeles Rams", LAR_KICKOFF),
        ]

    def desks(self, rows, now=NOW):
        return build_desks(self.games, rows, approved_of(rows), REGISTRY, now=now)

    def sync(self, rows, now=NOW, **kwargs):
        return sync_desk(
            config=CONFIG,
            api=self.api,
            state=self.state,
            desks=self.desks(rows, now),
            now=now,
            **kwargs,
        )

    def test_first_pass_posts_cards_and_pins_queue_and_week(self) -> None:
        rows = committee("401")
        summary = self.sync(rows)
        self.assertEqual(summary.posted, ["review:401", "picks:401", "queue", "week"])
        self.assertEqual(summary.alerts, [])
        topics = [m["topic"] for m in self.api.sent]
        self.assertEqual(topics, [11, 22, 11, 22])
        self.assertTrue(all(m["silent"] for m in self.api.sent))
        self.assertEqual(self.api.pins, [103, 104])
        self.assertEqual(self.state["cards"]["review:401"]["message_id"], 101)
        self.assertEqual(self.state["kickoffs"]["401"], SEA_KICKOFF)
        self.assertIn("402", self.state["kickoffs"])

    def test_second_pass_with_the_same_model_is_a_no_op(self) -> None:
        rows = committee("401")
        self.sync(rows)
        sent = len(self.api.sent)
        summary = self.sync(rows)
        self.assertEqual((summary.posted, summary.edited, summary.alerts), ([], [], []))
        self.assertEqual(len(self.api.sent), sent)
        self.assertEqual(self.api.edits, [])

    def test_full_opinions_paginate_on_the_same_card(self) -> None:
        rows = committee("401")
        self.sync(rows)
        picks_id = self.state["cards"]["picks:401"]["message_id"]
        sent = len(self.api.sent)

        self.state["expanded_picks"]["401"] = 0
        summary = self.sync(rows, priority_event_id="401")
        self.assertEqual(summary.posted, [])
        self.assertIn("picks:401", summary.edited)
        self.assertEqual(len(self.api.sent), sent)
        self.assertEqual(
            self.state["cards"]["picks:401"]["message_id"],
            picks_id,
        )
        self.assertIn("Select an opinion", self.api.edits[-1]["text"])
        self.assertEqual(self.state["expanded_picks"]["401"], "menu")
        self.assertFalse(
            any(key.startswith("picks-detail:401:") for key in self.state["cards"])
        )

        self.state["expanded_picks"]["401"] = {
            "mode": "opinion",
            "opinion": 0,
            "chunk": 0,
        }
        summary = self.sync(rows)
        self.assertIn("picks:401", summary.edited)
        self.assertIn("Schedule Expert", self.api.edits[-1]["text"])

        summary = self.sync(rows)
        self.assertEqual(
            (summary.posted, summary.edited, summary.deleted),
            ([], [], []),
        )

        self.state["expanded_picks"].pop("401")
        summary = self.sync(rows)
        self.assertIn("picks:401", summary.edited)
        self.assertIn("<b>GOD EXPERT</b>", self.api.edits[-1]["text"])

    def test_legacy_detail_replies_are_removed_without_new_posts(self) -> None:
        rows = committee("401")
        self.sync(rows)
        self.state["cards"]["picks-detail:401:0"] = {
            "message_id": 901,
            "obsolete_message_ids": [902],
            "topic": 22,
            "reply_to": self.state["cards"]["picks:401"]["message_id"],
        }
        sent = len(self.api.sent)
        summary = self.sync(rows)
        self.assertEqual(len(self.api.sent), sent)
        self.assertIn("picks-detail:401:0", summary.deleted)
        self.assertIn(901, self.api.deleted)
        self.assertIn(902, self.api.deleted)
        self.assertNotIn("picks-detail:401:0", self.state["cards"])

    def test_legacy_detail_replies_are_removed_when_picks_are_hidden(self) -> None:
        rows = committee("401")
        self.sync(rows)
        self.state["cards"]["picks-detail:401:0"] = {
            "message_id": 901,
            "topic": 22,
        }
        summary = self.sync(rows[:1])
        self.assertIn("picks-detail:401:0", summary.deleted)
        self.assertIn(901, self.api.deleted)
        self.assertNotIn("picks-detail:401:0", self.state["cards"])

    def test_legacy_detail_replies_are_removed_when_game_leaves_slate(self) -> None:
        rows = committee("401")
        self.sync(rows)
        self.state["cards"]["picks-detail:401:0"] = {
            "message_id": 901,
            "topic": 22,
        }
        summary = sync_desk(
            config=CONFIG,
            api=self.api,
            state=self.state,
            desks=[],
            now=NOW,
        )
        self.assertIn("picks-detail:401:0", summary.deleted)
        self.assertIn(901, self.api.deleted)
        self.assertNotIn("picks-detail:401:0", self.state["cards"])

    def test_failed_legacy_detail_delete_remains_retryable(self) -> None:
        rows = committee("401")
        self.sync(rows)
        key = "picks-detail:401:0"
        self.state["cards"][key] = {
            "message_id": 901,
            "obsolete_message_ids": [902],
            "topic": 22,
        }
        original_delete = self.api.delete

        def fail_obsolete(chat_id, message_id):
            if message_id == 902:
                return False
            return original_delete(chat_id, message_id)

        self.api.delete = fail_obsolete
        self.sync(rows, priority_event_id="401")
        entry = self.state["cards"][key]
        self.assertNotIn("message_id", entry)
        self.assertEqual(entry["obsolete_message_ids"], [902])
        self.assertIn(901, self.api.deleted)

        self.api.delete = original_delete
        self.sync(rows, priority_event_id="401")
        self.assertNotIn(key, self.state["cards"])
        self.assertIn(902, self.api.deleted)

    def test_same_card_pagination_still_works_after_kickoff(self) -> None:
        rows = committee("401")
        self.sync(rows)
        picks_id = self.state["cards"]["picks:401"]["message_id"]
        self.state["expanded_picks"]["401"] = {
            "mode": "opinion",
            "opinion": 0,
            "chunk": 0,
        }
        summary = self.sync(
            rows,
            now=datetime.fromisoformat(SEA_KICKOFF) + timedelta(minutes=5),
            priority_event_id="401",
        )
        self.assertIn("picks:401", summary.edited)
        picks_edit = next(
            edit for edit in self.api.edits if edit["id"] == picks_id
        )
        self.assertIn("Schedule Expert", picks_edit["text"])

    def test_callback_pagination_never_reposts_a_missing_card(self) -> None:
        rows = committee("401")
        self.sync(rows)
        picks_id = self.state["cards"]["picks:401"]["message_id"]
        self.api.missing.add(picks_id)
        self.state["expanded_picks"]["401"] = "menu"
        sent = len(self.api.sent)

        summary = self.sync(
            rows,
            priority_event_id="401",
            edit_only_event_id="401",
        )

        self.assertEqual(len(self.api.sent), sent)
        self.assertEqual(summary.posted, [])
        self.assertTrue(
            any(
                error.startswith("picks:401:")
                for error in summary.errors
            )
        )
        self.assertEqual(
            self.state["cards"]["picks:401"]["message_id"],
            picks_id,
        )

    def test_a_review_edits_only_the_affected_cards(self) -> None:
        rows = committee("401")
        self.sync(rows)
        rows[2]["review_status"] = "approved"
        rows[2]["reviewed_by"] = "AK"
        summary = self.sync(rows)
        self.assertEqual(summary.posted, [])
        self.assertEqual(summary.edited, ["review:401", "picks:401", "queue"])
        review_text, picks_text = self.api.edits[0]["text"], self.api.edits[1]["text"]
        self.assertNotIn("Win Total", review_text)  # decided: off the to-do card
        self.assertIn("<b>Win Total</b> Seahawks 61% ★★ · 20-24", picks_text)

    def test_approved_arm_posts_the_picks_card_and_one_loud_bet_alert(self) -> None:
        rows = committee("401", arms_status="approved")
        rows[5]["side_pick_json"] = json.dumps(SEA_SIDE)
        summary = self.sync(rows)
        self.assertEqual(
            summary.posted, ["review:401", "picks:401", "queue", "week"]
        )
        self.assertEqual(summary.alerts, ["bet:c884d868-0000:side"])
        alert = next(m for m in self.api.sent if not m["silent"])
        self.assertEqual(alert["topic"], 22)
        self.assertEqual(alert["reply_to"], self.state["cards"]["picks:401"]["message_id"])
        self.assertEqual(
            alert["text"], "🔔 Bet · Seahawks -3.5 (+100) ★ 0.6u · rules arm\njudge arm: PASS"
        )
        summary = self.sync(rows)
        self.assertEqual(summary.alerts, [])
        self.assertEqual(sum(1 for m in self.api.sent if not m["silent"]), 1)

    def test_lock_warning_fires_once_inside_the_window(self) -> None:
        rows = committee("401")
        kickoff = datetime.fromisoformat(SEA_KICKOFF)
        before = kickoff - timedelta(hours=4, minutes=1)
        inside = kickoff - timedelta(hours=3, minutes=30)
        self.sync(rows, now=before)
        self.assertEqual(sum(1 for m in self.api.sent if not m["silent"]), 0)
        summary = self.sync(rows, now=inside)
        self.assertEqual(summary.alerts, ["lock:401"])
        alert = next(m for m in self.api.sent if not m["silent"])
        self.assertEqual(alert["topic"], 11)
        self.assertEqual(alert["reply_to"], self.state["cards"]["review:401"]["message_id"])
        self.assertEqual(
            alert["text"], "🔔 Patriots @ Seahawks locks for the judge in 1h 30m · 3 pending"
        )
        self.sync(rows, now=inside + timedelta(minutes=10))
        self.assertEqual(sum(1 for m in self.api.sent if not m["silent"]), 1)
        # no pending rows, no warning
        quiet = [r for r in committee("402", arms_status="approved") if r["opinion_id"] != "p1"]
        desk = self.desks(quiet, now=inside)[1]
        self.assertFalse(lock_warning_due(desk, config=CONFIG, now=inside))

    def test_started_games_are_frozen_and_later_pruned(self) -> None:
        rows = committee("401")
        self.sync(rows)
        kickoff = datetime.fromisoformat(SEA_KICKOFF)
        rows[2]["review_status"] = "approved"
        summary = self.sync(rows, now=kickoff + timedelta(minutes=5))
        self.assertNotIn("review:401", summary.edited)
        self.assertIn("review:401", self.state["cards"])
        self.sync(rows, now=kickoff + timedelta(days=4))
        self.assertNotIn("review:401", self.state["cards"])
        self.assertNotIn("401", self.state["kickoffs"])

    def test_a_deleted_card_is_reposted(self) -> None:
        rows = committee("401")
        self.sync(rows)
        self.api.missing.add(self.state["cards"]["review:401"]["message_id"])
        rows[2]["review_status"] = "rejected"
        summary = self.sync(rows)
        self.assertEqual(summary.posted, ["review:401"])
        self.assertEqual(self.state["cards"]["review:401"]["message_id"], self.api.sent[-1]["id"])

    def test_post_budget_defers_the_rest_to_the_next_pass(self) -> None:
        rows = committee("401") + committee("402")
        summary = self.sync(rows, max_posts=2)
        self.assertEqual(summary.posted, ["review:401", "picks:401"])
        self.assertEqual(summary.deferred, ["review:402", "picks:402", "queue", "week"])
        summary = self.sync(rows, max_posts=2)
        self.assertEqual(summary.posted, ["review:402", "picks:402"])
        summary = self.sync(rows, max_posts=2)
        self.assertEqual(summary.posted, ["queue", "week"])

    def test_review_card_is_deleted_once_nothing_is_left_to_review(self) -> None:
        rows = committee("401")
        self.sync(rows)
        review_id = self.state["cards"]["review:401"]["message_id"]
        for r in rows:
            if r["review_status"] == "pending":
                r["review_status"] = "approved"
                r["reviewed_by"] = "AK"
        summary = self.sync(rows)
        self.assertEqual(summary.deleted, ["review:401"])
        self.assertEqual(self.api.deleted, [review_id])
        self.assertNotIn("review:401", self.state["cards"])
        self.assertIn("picks:401", self.state["cards"])
        # a fresh pending row brings the to-do card back
        rows.append(row("p2", "401", "win_total", generated="2026-09-12T13:00:00+00:00"))
        summary = self.sync(rows)
        self.assertEqual(summary.posted, ["review:401"])
        self.assertEqual(summary.deleted, [])

    def test_api_errors_are_collected_not_raised(self) -> None:
        class Broken(FakeApi):
            def send(self, *args, **kwargs):
                raise DeskApiError("sendMessage: chat not found")

        self.api = Broken()
        summary = self.sync(committee("401"))
        self.assertEqual(summary.posted, [])
        self.assertEqual(len(summary.errors), 4)
        self.assertEqual(self.state["cards"], {})


class StateTests(unittest.TestCase):
    def test_round_trip_and_bad_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "nested" / "state.json"
            state = empty_state()
            state["cards"]["queue"] = {"message_id": 5, "hash": "h", "topic": 1}
            save_state(path, state)
            self.assertEqual(load_state(path), state)
            path.write_text("{not json", encoding="utf-8")
            self.assertEqual(load_state(path), empty_state())
            path.write_text(json.dumps({"version": 99}), encoding="utf-8")
            self.assertEqual(load_state(path), empty_state())
            self.assertEqual(load_state(Path(tmp) / "absent.json"), empty_state())

    def test_old_state_loads_with_collapsed_picks(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "state.json"
            path.write_text(
                json.dumps(
                    {
                        "version": 1,
                        "cards": {"picks:401": {"message_id": 4}},
                        "announced": {},
                        "kickoffs": {},
                    }
                ),
                encoding="utf-8",
            )
            state = load_state(path)
        self.assertEqual(state["cards"]["picks:401"]["message_id"], 4)
        self.assertEqual(state["expanded_picks"], {})

    def test_prune_keeps_recent_and_global_cards(self) -> None:
        state = empty_state()
        state["kickoffs"] = {
            "old": (NOW - timedelta(days=4)).isoformat(),
            "new": (NOW - timedelta(days=1)).isoformat(),
        }
        state["cards"] = {
            "review:old": {"message_id": 1},
            "picks:new": {"message_id": 2},
            "queue": {"message_id": 3},
        }
        state["announced"] = {
            "bet:x:side": {"at": "", "event_id": "old"},
            "lock:new": {"at": "", "event_id": "new"},
        }
        prune_state(state, now=NOW)
        self.assertEqual(sorted(state["cards"]), ["picks:new", "queue"])
        self.assertEqual(list(state["announced"]), ["lock:new"])
        self.assertEqual(list(state["kickoffs"]), ["new"])

    def test_content_hash_covers_topic_text_and_keyboard(self) -> None:
        base = content_hash("t", [], 1)
        self.assertNotEqual(base, content_hash("t", [], 2))
        self.assertNotEqual(base, content_hash("t", [[{"text": "x", "callback_data": "y"}]], 1))
        self.assertEqual(base, content_hash("t", [], 1))


class ConfigAndCallbackTests(unittest.TestCase):
    def test_config_from_env_requires_chat_and_both_topics(self) -> None:
        env = {
            "INTAKE_BOT_TOKEN": "t",
            "MOE_DESK_CHAT_ID": "-100123",
            "MOE_DESK_REVIEW_TOPIC": "2",
            "MOE_DESK_PICKS_TOPIC": "3",
            "MOE_DESK_SCORES_TOPIC": "4",
            "MOE_DESK_SYNC_SECONDS": "5",
            "MOE_DESK_LOCK_WARN_HOURS": "1.5",
        }
        config = desk_config_from_env(env)
        self.assertEqual(
            (config.chat_id, config.review_topic, config.picks_topic, config.scores_topic),
            ("-100123", 2, 3, 4),
        )
        self.assertEqual(config.sync_seconds, 15)  # floor
        self.assertEqual(config.lock_warn_hours, 1.5)
        self.assertEqual(config.with_username("@nflguesser_bot").bot_username, "nflguesser_bot")
        for missing in ("INTAKE_BOT_TOKEN", "MOE_DESK_CHAT_ID", "MOE_DESK_REVIEW_TOPIC", "MOE_DESK_PICKS_TOPIC"):
            partial = {k: v for k, v in env.items() if k != missing}
            self.assertIsNone(desk_config_from_env(partial), missing)
        self.assertIsNone(desk_config_from_env({**env, "MOE_DESK_PICKS_TOPIC": "x"}))
        self.assertIsNone(desk_config_from_env({**env, "MOE_DESK_SCORES_TOPIC": ""}).scores_topic)

    def test_parse_start_param(self) -> None:
        self.assertEqual(parse_start_param("/start op_c884d868-1"), ("op", "c884d868-1"))
        self.assertEqual(parse_start_param("/start@nflguesser_bot game_401"), ("game", "401"))
        self.assertIsNone(parse_start_param("/start"))
        self.assertIsNone(parse_start_param("/start hello"))
        self.assertIsNone(parse_start_param("/start op_../x"))

    def test_desk_ids_report_and_topic_id(self) -> None:
        class Reply:
            def __init__(self, forum_topic, top=None, msg=None):
                self.forum_topic = forum_topic
                self.reply_to_top_id = top
                self.reply_to_msg_id = msg

        self.assertIsNone(topic_id_from_reply(None))
        self.assertIsNone(topic_id_from_reply(Reply(False, msg=5)))
        self.assertEqual(topic_id_from_reply(Reply(True, msg=7)), 7)
        self.assertEqual(topic_id_from_reply(Reply(True, top=7, msg=9)), 7)
        ready = desk_ids_report(-1001, title="MOE", supergroup=True, topics=True, topic_id=7)
        self.assertEqual(
            ready.split("\n"),
            [
                "desk · MOE",
                "chat_id: -1001",
                "supergroup · topics on",
                "this topic id: 7",
                "MOE_DESK_CHAT_ID=<chat_id> in .env, then scripts/desk_setup.py --create-topics",
            ],
        )
        basic = desk_ids_report(-42, title="MOE", supergroup=False, topics=False)
        self.assertIn("basic group · topics off", basic)
        self.assertIn("Not ready: Edit → Topics on", basic)
        self.assertNotIn("topic id", basic)

    def test_parse_callback(self) -> None:
        self.assertEqual(parse_callback("desk:ok:abc"), ("ok", "abc"))
        self.assertEqual(parse_callback("desk:no:abc"), ("no", "abc"))
        self.assertEqual(parse_callback("desk:okarms:401"), ("okarms", "401"))
        self.assertEqual(parse_callback("desk:show:401"), ("show", "401"))
        self.assertEqual(parse_callback("desk:hide:401"), ("hide", "401"))
        self.assertEqual(
            parse_callback("desk:page:401:2"),
            ("page", "401:2"),
        )
        self.assertEqual(
            parse_callback("desk:op:401:schedule"),
            ("op", "401:schedule"),
        )
        self.assertEqual(
            parse_callback("desk:part:401:schedule:1"),
            ("part", "401:schedule:1"),
        )
        self.assertIsNone(parse_callback("desk:zap:401"))
        self.assertIsNone(parse_callback("desk:ok:"))
        self.assertIsNone(parse_callback("moe:view:401:0"))

    def test_review_targets(self) -> None:
        rows = committee("401")
        self.assertEqual(review_targets("ok", "p1", rows), ([rows[2]], None))
        self.assertEqual(review_targets("no", "zzz", rows)[1], "That row is no longer in the sheet.")
        self.assertEqual(review_targets("ok", "a1", rows)[1], "Already approved by SS.")
        rows.append(row("bad", "401", "schedule", generation_status="invalid"))
        self.assertIn("audit row", review_targets("ok", "bad", rows)[1])
        arms, error = review_targets("okarms", "401", rows)
        self.assertIsNone(error)
        self.assertEqual([r["expert_id"] for r in arms], ["god_rules", "god_judge"])
        rows[6]["review_status"] = "approved"
        self.assertEqual(review_targets("okarms", "401", rows)[1], "Both arms are no longer pending.")


class _Response:
    def __init__(self, payload):
        self._payload = json.dumps(payload).encode("utf-8")

    def read(self):
        return self._payload

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _http_error(code, payload):
    return urllib.error.HTTPError(
        "https://api.telegram.org", code, "err", {}, io.BytesIO(json.dumps(payload).encode())
    )


class BotApiTests(unittest.TestCase):
    def test_send_encodes_the_keyboard_and_returns_the_message_id(self) -> None:
        calls = []

        def opener(request, timeout):
            calls.append((request.full_url, request.data.decode()))
            return _Response({"ok": True, "result": {"message_id": 77}})

        api = BotApi("tok", opener=opener)
        message_id = api.send(
            "-1", 11, "<b>x</b>", keyboard=[[{"text": "a", "callback_data": "b"}]], silent=True, reply_to=5
        )
        self.assertEqual(message_id, 77)
        url, data = calls[0]
        self.assertEqual(url, "https://api.telegram.org/bottok/sendMessage")
        self.assertIn("message_thread_id=11", data)
        self.assertIn("disable_notification=True", data)
        self.assertIn("reply_to_message_id=5", data)
        self.assertIn("parse_mode=HTML", data)
        self.assertIn("inline_keyboard", data)

    def test_edit_distinguishes_unchanged_missing_and_other_errors(self) -> None:
        answers = iter(
            [
                _http_error(400, {"ok": False, "description": "Bad Request: message is not modified"}),
                _http_error(400, {"ok": False, "description": "Bad Request: message to edit not found"}),
                _http_error(400, {"ok": False, "description": "Bad Request: can't parse entities"}),
            ]
        )

        def opener(request, timeout):
            raise next(answers)

        api = BotApi("tok", opener=opener)
        self.assertTrue(api.edit("-1", 1, "t"))
        self.assertFalse(api.edit("-1", 1, "t"))
        with self.assertRaisesRegex(DeskApiError, "parse entities"):
            api.edit("-1", 1, "t")

    def test_rate_limit_is_retried_once_after_retry_after(self) -> None:
        sleeps = []
        answers = iter(
            [
                _http_error(429, {"ok": False, "parameters": {"retry_after": 7}}),
                _Response({"ok": True, "result": {"message_id": 1}}),
            ]
        )

        def opener(request, timeout):
            answer = next(answers)
            if isinstance(answer, Exception):
                raise answer
            return answer

        api = BotApi("tok", opener=opener, sleep=sleeps.append)
        self.assertEqual(api.send("-1", None, "t"), 1)
        self.assertEqual(sleeps, [7.0])

    def test_network_failure_is_a_desk_error(self) -> None:
        def opener(request, timeout):
            raise urllib.error.URLError("down")

        with self.assertRaisesRegex(DeskApiError, "down"):
            BotApi("tok", opener=opener).get_me()

    def test_scores_notice_posts_pre_block_or_declines(self) -> None:
        api = FakeApi()
        env = {
            "INTAKE_BOT_TOKEN": "t",
            "MOE_DESK_CHAT_ID": "-1",
            "MOE_DESK_REVIEW_TOPIC": "1",
            "MOE_DESK_PICKS_TOPIC": "2",
            "MOE_DESK_SCORES_TOPIC": "3",
        }
        self.assertTrue(post_scores_notice("a <b> c", environ=env, api=api))
        self.assertEqual(api.sent[0]["topic"], 3)
        self.assertEqual(api.sent[0]["text"], "<pre>a &lt;b&gt; c</pre>")
        self.assertTrue(api.sent[0]["silent"])
        self.assertFalse(post_scores_notice("x", environ={**env, "MOE_DESK_SCORES_TOPIC": ""}, api=api))
        self.assertFalse(post_scores_notice("x", environ={}, api=api))


if __name__ == "__main__":
    unittest.main()
