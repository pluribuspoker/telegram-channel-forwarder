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
    parse_callback,
    parse_start_param,
    post_scores_notice,
    prune_state,
    render_opinion_details,
    render_offline_card,
    render_picks_card,
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
        "celebrity": {
            "enabled": True,
            "mode": "agent",
            "committee_optional": True,
            "name": "Celebrity Expert",
        },
        "hi_lo": {
            "enabled": True,
            "mode": "agent",
            "committee_optional": True,
            "name": "Hi Lo Expert",
        },
        "disabled": {"enabled": False, "mode": "agent"},
        "god_rules": {"enabled": True, "mode": "aggregator"},
        "god_judge": {"enabled": True, "mode": "aggregator_judge"},
    }
}
CONFIG = DeskConfig(
    bot_token="token",
    chat_id="-1001",
    picks_topic=22,
    scores_topic=33,
    bot_username="nflguesser_bot",
)
LATEST_MARKET = {
    "away_spread": 3.5,
    "away_spread_price": -110,
    "away_moneyline": 155,
    "home_spread": -3.5,
    "home_spread_price": -110,
    "home_moneyline": -175,
    "total": 44.5,
    "over_price": -105,
    "under_price": -115,
    "bookmaker": "BetOnline.ag",
    "captured_at": "2026-09-12T12:35:00+00:00",
}
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
# Scoreboard shape: build_scoreboard(latest_per_game=True)["by_expert"] —
# the picks card reads only each record's graded bet legs.
RECORDS = {
    "god_judge": {"legs": {"w": 5, "l": 2, "p": 0}},
    "god_rules": {"legs": {"w": 4, "l": 3, "p": 1}},
    "ak": {"legs": {"w": 3, "l": 1, "p": 0}},
    "schedule": {"legs": {"w": 0, "l": 0, "p": 0}},
}
ABBREVS = {"New England Patriots": "NE", "Seattle Seahawks": "SEA"}


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
        "pikkit": "Pikkit Expert",
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
        self.unpins: list[int] = []
        self.missing: set[int] = set()
        self.undeletable: set[int] = set()
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

    def unpin(self, chat_id, message_id):
        self.unpins.append(message_id)
        return True

    def delete(self, chat_id, message_id):
        if message_id in self.undeletable:
            return False
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
                # a legacy unapproved row is not a committee row: missing
                "win_total": "missing",
            },
        )
        self.assertNotIn("cee", statuses)  # optional and absent
        self.assertEqual((desk.required_approved, desk.required_total), (4, 5))
        self.assertEqual(desk.missing_required, ["win_total"])
        self.assertEqual(
            desk.missing_optional,
            ["cee", "celebrity", "hi_lo"],
        )
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
        # win_total only has an unapproved legacy row: still waiting on it
        self.assertEqual(desk.missing_required, ["win_total"])

    def test_horizon_status_and_started_games(self) -> None:
        far = game("9", "A B", "C D", (NOW + timedelta(days=20)).isoformat())
        done = game("8", "A B", "C D", (NOW - timedelta(hours=1)).isoformat())
        old = game("7", "A B", "C D", (NOW - timedelta(days=5)).isoformat())
        final = game("6", "A B", "C D", SEA_KICKOFF, status="final")
        desks = build_desks([far, done, old, final], [], [], REGISTRY, now=NOW)
        self.assertEqual([d.event_id for d in desks], ["8"])
        self.assertTrue(desks[0].started)

    def test_offline_card_contains_lines_and_approved_picks(self) -> None:
        rows = committee(arms_status="approved")
        desk = build_desks(
            [game("401", "New England Patriots", "Seattle Seahawks", SEA_KICKOFF)],
            rows,
            approved_of(rows),
            REGISTRY,
            now=NOW,
        )[0]

        text, keyboard = render_offline_card(
            desk,
            latest_market=LATEST_MARKET,
            team_abbrevs={
                "New England Patriots": "NE",
                "Seattle Seahawks": "SEA",
            },
        )

        self.assertEqual(keyboard, [])
        self.assertIn("<b>Spread</b> · NE +3.5 (-110) · SEA -3.5 (-110)", text)
        self.assertIn("<b>Moneyline</b> · NE +155 · SEA -175", text)
        self.assertIn("<b>Total</b> · 44.5 (O -105 / U -115)", text)
        self.assertIn("<b>Rules</b>", text)
        self.assertIn("<b>Schedule</b>", text)
        self.assertIn("<b>Consensus</b>", text)
        self.assertIn(
            "<i>No opinion yet · Win Total · Cee · Celebrity · Hi Lo</i>",
            text,
        )
        self.assertNotIn("Waiting on required", text)
        self.assertLess(len(text), 4096)

    def test_committee_selects_latest_approved_per_expert(self) -> None:
        rows = [
            # legacy unapproved drafts and audit rows never join the committee
            row("draft", "401", "schedule", generated="2026-09-09T00:00:00+00:00"),
            row("old", "401", "schedule", status="approved", generated="2026-09-10T00:00:00+00:00"),
            row("newer", "401", "schedule", generated="2026-09-11T00:00:00+00:00"),
            row("hk", "401", "schedule", status="approved", model="claude-haiku-4-5"),
            row("bad", "401", "schedule", generation_status="invalid"),
            row("sample", "401", "schedule", status="not_applicable", generation_status="sample"),
            row("odd", "401", "schedule", status="not_applicable"),
            row("pend", "401", "win_total"),
            row("rej", "401", "divisional", status="rejected"),
            arm_row("j-only", "401", "god_judge", PASS_PLAIN, PASS_PLAIN, generated="2026-09-10T00:00:00+00:00"),
        ]
        desk = build_desks(
            [game("401", "New England Patriots", "Seattle Seahawks", SEA_KICKOFF)],
            rows,
            approved_of(rows),
            REGISTRY,
            now=NOW,
        )[0]
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
        # an unapproved judge row is not a decided arm
        self.assertIsNone(desk.judge)


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

    def test_voice_line_renders_a_cee_spread_cover(self) -> None:
        cee = row(
            "op-cee",
            "evt",
            "cee",
            status="approved",
            pick_market="spread",
            pick_side="New England Patriots",
            side_pick_json=json.dumps(
                {
                    "selection": "New England Patriots",
                    "line": 4.0,
                    "confidence_stars": 2,
                }
            ),
        )
        line = moe_desk.voice_line(cee)
        # Renders Cee's cover pick, not the game's predicted outright winner.
        self.assertIn("Patriots +4", line)
        self.assertNotIn("Seahawks", line)
        self.assertNotIn("%", line)

    def test_picks_card_lists_arms_and_betting_voices_with_records(self) -> None:
        rows = committee(arms_status="approved")
        rows[5]["side_pick_json"] = json.dumps(SEA_SIDE)
        rows[5]["total_pick_json"] = json.dumps(OVER)
        rows[5]["supporting_factors_json"] = json.dumps(
            ["Pool p(home) .56 vs market .58", {"text": "Voices split 3-2"}]
        )
        rows[0]["thesis"] = "Seattle <stronger> at home"
        rows[3]["side_pick_json"] = json.dumps(SEA_SIDE)  # AK bets a leg
        desk = self.desk(rows)
        self.assertTrue(desk.show_picks)
        text, keyboard = render_picks_card(
            desk,
            config=CONFIG,
            records=RECORDS,
            latest_market=LATEST_MARKET,
            team_abbrevs=ABBREVS,
        )
        self.assertEqual(
            text.split("\n"),
            [
                "🏈 <b>Patriots @ Seahawks</b>",
                "Sun Sep 13 · 4:05 PM ET · Week 1",
                "<i>SEA -3.5 · O/U 44.5</i>",
                "",
                "<b>Rules</b> (4-3-1) · Seahawks -3.5 (+100) ★ 0.6u · "
                "Over 44.5 (-105) ★ 0.5u",
                "",
                "<b>AK</b> (3-1) · Seahawks -3.5 (+100) ★ 0.6u",
            ],
        )
        # Lean-only stances stay off the card entirely — no names, no
        # probabilities, no consensus (a zero-leg record shows nothing) —
        # and so does a non-betting arm (the all-PASS judge has no line,
        # no "no bet" filler).
        self.assertNotIn("God", text)
        self.assertNotIn("no bet", text)
        self.assertNotIn("Schedule", text)
        self.assertNotIn("%", text)
        self.assertNotIn("Consensus", text)
        self.assertNotIn("(0-0)", text)
        self.assertNotIn("<blockquote", text)

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
        self.assertIn("<b>Rules</b> · Seahawks -3.5 (+100) ★ 0.6u", picker_text)
        self.assertIn("<b>Select an opinion</b>", picker_text)
        self.assertEqual(
            [
                button["text"]
                for row in picker_keyboard
                for button in row
            ],
            [
                "God Rules",
                "God Judge",
                "Schedule",
                "Divisional",
                "AK",
                "Elo",
                "Refresh opinions",
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

    def test_pikkit_is_shadowed_and_detail_keeps_both_phases(self) -> None:
        initial = row(
            "pikkit-initial",
            "401",
            "pikkit",
            status="approved",
            generated="2026-09-12T01:00:00+00:00",
            pick_market="side_and_total",
            side_pick_json=json.dumps(PASS_PLAIN),
            total_pick_json=json.dumps(PASS_PLAIN),
            calibration_summary_json=json.dumps(
                {"generation_phase": "initial"}
            ),
            full_opinion="Initial movement watch.",
        )
        final = row(
            "pikkit-final",
            "401",
            "pikkit",
            status="approved",
            generated="2026-09-13T18:05:00+00:00",
            pick_market="side_and_total",
            side_pick_json=json.dumps(SEA_SIDE),
            total_pick_json=json.dumps(PASS_PLAIN),
            calibration_summary_json=json.dumps(
                {
                    "generation_phase": "final_t_minus_2h",
                    "sportsbook": {
                        "moneyline": {"best_outcome": "away_win"},
                        "spread": {"best_outcome": "home_cover"},
                    },
                    "movement": {
                        "markets": {
                            "moneyline": {
                                "sides": {
                                    "home": {"handle_pct_change": 0.1},
                                    "away": {"handle_pct_change": -0.1},
                                }
                            }
                        }
                    },
                }
            ),
            full_opinion="Final movement evaluation.",
        )
        rows = committee(arms_status="approved") + [initial, final]
        registry = {
            **REGISTRY,
            "experts": {
                **REGISTRY["experts"],
                "pikkit": {
                    "enabled": True,
                    "mode": "agent",
                    "committee_optional": True,
                    "aggregator_participation": "shadow",
                    "name": "Pikkit Expert",
                },
            },
        }
        desk = build_desks(
            [game("401", "New England Patriots", "Seattle Seahawks", SEA_KICKOFF)],
            rows,
            approved_of(rows),
            registry,
            now=NOW,
        )[0]
        text, _buttons = render_picks_card(desk, config=CONFIG)
        # On the picks card a betting shadow voice is one tagged pick line;
        # its movement detail lives on the offline card and in the opinions.
        self.assertIn(
            "<b>Pikkit</b> <i>Shadow</i> · Seahawks -3.5 (+100) ★ 0.6u",
            text,
        )
        self.assertNotIn("book benefits", text)
        offline_text, _ = render_offline_card(desk)
        self.assertIn("Shadow · Final T-2h", offline_text)
        self.assertIn("move: moneyline home +10% handle", offline_text)
        self.assertIn(
            "book benefits: ML Patriots win, spread Seahawks cover",
            offline_text,
        )
        groups = moe_desk.picks_opinion_groups(desk)
        pikkit = next(group for group in groups if group[0] == "pikkit")
        details = "\n".join(pikkit[2])
        self.assertIn("Initial movement watch.", details)
        self.assertIn("Final movement evaluation.", details)

    def test_pikkit_detail_explains_splits_and_book_arithmetic(self) -> None:
        payload = {
            "selected_snapshot": {
                "snapshot_age_seconds": 793703,
                "betonline": {
                    "away_moneyline": 110,
                    "home_moneyline": -130,
                    "away_spread": 1.5,
                    "away_spread_price": -108,
                    "home_spread": -1.5,
                    "home_spread_price": -112,
                    "total": 39.5,
                    "over_price": -105,
                    "under_price": -115,
                },
                "markets": {
                    "moneyline": {
                        "sides": {
                            "away": {
                                "bet_pct": 0.34446,
                                "handle_pct": 0.464418,
                            },
                            "home": {
                                "bet_pct": 0.65554,
                                "handle_pct": 0.535582,
                            },
                        },
                        "sportsbook": {
                            "net_per_unit_handle": {
                                "away_win": 0.024722,
                                "home_win": 0.052432,
                            }
                        },
                    },
                    "spread": {
                        "sides": {
                            "away": {
                                "bet_pct": 0.640724,
                                "handle_pct": 0.68311,
                            },
                            "home": {
                                "bet_pct": 0.359276,
                                "handle_pct": 0.31689,
                            },
                        },
                        "sportsbook": {
                            "net_per_unit_handle": {
                                "away_cover": -0.315619,
                                "home_cover": 0.400172,
                                "push": 0,
                            }
                        },
                    },
                    "total": {
                        "sides": {
                            "over": {
                                "bet_pct": 0.327759,
                                "handle_pct": 0.268122,
                            },
                            "under": {
                                "bet_pct": 0.672241,
                                "handle_pct": 0.731878,
                            },
                        },
                        "sportsbook": {
                            "net_per_unit_handle": {
                                "over": 0.476524,
                                "under": -0.368293,
                                "push": 0,
                            }
                        },
                    },
                },
            }
        }
        pikkit_row = row(
            "pikkit-readable",
            "401",
            "pikkit",
            status="approved",
            pick_market="side_and_total",
            side_pick_json=json.dumps(PASS_PLAIN),
            total_pick_json=json.dumps(PASS_PLAIN),
            expected_home_margin=1.5,
            away_team="New York Jets",
            home_team="Tennessee Titans",
            predicted_away_score=19,
            predicted_home_score=20,
            home_win_probability=0.543,
            calibration_summary_json=json.dumps(
                {
                    "generation_phase": "initial",
                    "market_baseline": {"projected_total": 39.5},
                    "model_adjustment": {"projected_total": 0},
                }
            ),
            input_json=json.dumps(payload),
        )

        text = moe_desk.render_pikkit_details([pikkit_row])[0]

        self.assertIn("Jets: 34.4% bets / 46.4% money", text)
        self.assertIn(
            "Titans win</b> -130: collect $46.44, pay $41.20 profit",
            text,
        )
        self.assertIn("<b>BO +$5.24</b>", text)
        self.assertIn("<b>BO -$31.56</b>", text)
        self.assertIn(
            "Largest estimated BO liabilities: Under (-$36.83), "
            "Jets cover (-$31.56)",
            text,
        )
        self.assertIn("Pikkit pick: PASS", text)
        self.assertNotIn("Supporting factors", text)

    def test_non_betting_arms_are_absent_not_labeled(self) -> None:
        # A voice bet alone carries the card; arms without a bet leg (no row
        # yet, or an all-PASS decision) simply do not appear.
        rows = committee()  # arms unapproved
        rows[3]["side_pick_json"] = json.dumps(SEA_SIDE)  # AK bets → card
        text, _ = render_picks_card(self.desk(rows), config=CONFIG)
        self.assertNotIn("God", text)
        self.assertNotIn("Rules", text)
        self.assertNotIn("—", text)
        self.assertIn("<b>AK</b> · Seahawks -3.5 (+100) ★ 0.6u", text)
        # The judge betting alone puts 👑 God on top and omits Rules.
        rows = committee(arms_status="approved")
        rows[6]["side_pick_json"] = json.dumps(SEA_SIDE)
        text, _ = render_picks_card(self.desk(rows), config=CONFIG)
        self.assertIn("👑 <b>God</b> · Seahawks -3.5 (+100) ★ 0.6u", text)
        self.assertNotIn("Rules", text)
        self.assertNotIn("no bet", text)

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

    def test_picks_card_needs_an_actual_bet(self) -> None:
        lone = [row("a4", "401", "rating_elo", status="approved", model="deterministic")]
        self.assertFalse(self.desk(lone).show_picks)
        # lean-only voices never earn a picks card, no matter how many
        two = lone + [row("a1", "401", "schedule", status="approved")]
        self.assertFalse(self.desk(two).show_picks)
        # an all-PASS arm decision is not a bet: still no card
        passing = lone + [arm_row("r1", "401", "god_rules", PASS_PLAIN, PASS_PLAIN, status="approved")]
        self.assertFalse(self.desk(passing).show_picks)
        betting_arm = lone + [arm_row("r2", "401", "god_rules", SEA_SIDE, PASS_PLAIN, status="approved")]
        self.assertTrue(self.desk(betting_arm).show_picks)
        betting_voice = lone + [
            row(
                "a1",
                "401",
                "schedule",
                status="approved",
                side_pick_json=json.dumps(SEA_SIDE),
            )
        ]
        self.assertTrue(self.desk(betting_voice).show_picks)

    def test_consensus_does_not_count_a_side_pass(self) -> None:
        # Consensus left the picks card with the lean lines; the offline
        # card still shows it, and a side PASS still never counts.
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
        text, _ = render_offline_card(self.desk(rows))
        self.assertIn("<b>Consensus</b> · split 1–1", text)
        self.assertNotIn("Seahawks 2–1", text)


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

    def test_offline_topic_has_one_keyboard_free_card_per_game(self) -> None:
        config = DeskConfig(
            bot_token="token",
            chat_id="-1001",
            picks_topic=22,
            scores_topic=33,
            offline_topic=44,
        )
        rows = committee("401", arms_status="approved")
        summary = sync_desk(
            config=config,
            api=self.api,
            state=self.state,
            desks=self.desks(rows),
            now=NOW,
            latest_markets={"401": LATEST_MARKET},
            max_posts=50,
        )

        self.assertIn("offline:401", summary.posted)
        offline = next(message for message in self.api.sent if message["topic"] == 44)
        self.assertEqual(offline["keyboard"], [])
        self.assertIn("<b>LATEST LINES</b>", offline["text"])
        self.assertIn("<b>MOE PICKS</b>", offline["text"])

        second = sync_desk(
            config=config,
            api=self.api,
            state=self.state,
            desks=self.desks(rows),
            now=NOW,
            latest_markets={"401": LATEST_MARKET},
            max_posts=50,
        )
        self.assertNotIn("offline:401", second.posted)
        self.assertNotIn("offline:401", second.edited)

    def test_offline_topic_hides_games_without_an_approved_opinion(self) -> None:
        config = DeskConfig(
            bot_token="token",
            chat_id="-1001",
            picks_topic=22,
            scores_topic=33,
            offline_topic=44,
        )
        desks = self.desks([])

        summary = sync_desk(
            config=config,
            api=self.api,
            state=self.state,
            desks=desks,
            now=NOW,
            latest_markets={"401": LATEST_MARKET},
            max_posts=50,
        )

        self.assertFalse(
            any(message["topic"] == 44 for message in self.api.sent)
        )
        self.assertFalse(
            any(key.startswith("offline:") for key in summary.posted)
        )

        self.state["cards"]["offline:401"] = {
            "message_id": 901,
            "topic": 44,
        }
        summary = sync_desk(
            config=config,
            api=self.api,
            state=self.state,
            desks=desks,
            now=NOW,
            latest_markets={"401": LATEST_MARKET},
            max_posts=50,
        )
        self.assertIn("offline:401", summary.deleted)
        self.assertIn(901, self.api.deleted)
        self.assertNotIn("offline:401", self.state["cards"])

    def test_first_pass_posts_one_card_per_game(self) -> None:
        rows = committee("401") + committee("402")
        rows[3]["side_pick_json"] = json.dumps(SEA_SIDE)  # AK bets 401
        rows[10]["side_pick_json"] = json.dumps(SEA_SIDE)  # AK bets 402
        summary = self.sync(rows, records=RECORDS)
        self.assertEqual(summary.posted, ["picks:401", "picks:402"])
        self.assertEqual(summary.alerts, [])
        self.assertEqual([m["topic"] for m in self.api.sent], [22, 22])
        self.assertTrue(all(m["silent"] for m in self.api.sent))
        self.assertEqual(self.api.pins, [])
        card = self.api.sent[0]
        self.assertIn("<b>AK</b> (3-1) · Seahawks -3.5 (+100) ★ 0.6u", card["text"])
        self.assertNotIn("God", card["text"])
        self.assertEqual(
            [button["text"] for row in card["keyboard"] for button in row],
            ["Show full opinions"],
        )
        self.assertEqual(
            self.state["cards"]["picks:401"]["message_id"],
            101,
        )
        self.assertEqual(self.state["kickoffs"]["401"], SEA_KICKOFF)
        self.assertIn("402", self.state["kickoffs"])

    def test_legacy_day_cards_are_deleted_even_when_undeletable(self) -> None:
        self.state["cards"] = {
            "picks-day:2026-09-13": {"message_id": 30, "topic": 22},
            "picks-day:2026-09-14": {"message_id": 31, "topic": 22},
        }
        self.api.undeletable.add(31)
        rows = committee("401", arms_status="approved")
        rows[5]["side_pick_json"] = json.dumps(SEA_SIDE)
        summary = self.sync(rows)
        self.assertEqual(summary.errors, [])
        self.assertIn(30, self.api.deleted)
        self.assertNotIn("picks-day:2026-09-13", self.state["cards"])
        self.assertNotIn("picks-day:2026-09-14", self.state["cards"])
        self.assertIn("picks:401", self.state["cards"])

    def test_second_pass_with_the_same_model_is_a_no_op(self) -> None:
        rows = committee("401", arms_status="approved")
        rows[5]["side_pick_json"] = json.dumps(SEA_SIDE)
        self.sync(rows)
        sent = len(self.api.sent)
        summary = self.sync(rows)
        self.assertEqual((summary.posted, summary.edited, summary.alerts), ([], [], []))
        self.assertEqual(len(self.api.sent), sent)
        self.assertEqual(self.api.edits, [])

    def test_full_opinions_paginate_on_the_same_card(self) -> None:
        rows = committee("401", arms_status="approved")
        rows[5]["side_pick_json"] = json.dumps(SEA_SIDE)
        self.sync(rows)
        picks_id = self.state["cards"]["picks:401"]["message_id"]
        sent = len(self.api.sent)

        self.state["expanded_picks"]["401"] = 0
        summary = self.sync(rows)
        self.assertEqual(summary.posted, [])
        self.assertIn("picks:401", summary.edited)
        self.assertEqual(len(self.api.sent), sent)
        self.assertEqual(
            self.state["cards"]["picks:401"]["message_id"],
            picks_id,
        )
        self.assertIn("Select an opinion", self.api.edits[-1]["text"])
        self.assertEqual(self.state["expanded_picks"]["401"], "menu")

        self.state["expanded_picks"]["401"] = {
            "mode": "opinion",
            "opinion": 0,
            "chunk": 0,
        }
        summary = self.sync(rows)
        self.assertIn("picks:401", summary.edited)
        self.assertIn("God Expert (Rules)", self.api.edits[-1]["text"])

        summary = self.sync(rows)
        self.assertEqual(
            (summary.posted, summary.edited, summary.deleted),
            ([], [], []),
        )

        self.state["expanded_picks"].pop("401")
        summary = self.sync(rows)
        self.assertIn("picks:401", summary.edited)
        self.assertIn("<b>Rules</b> · Seahawks -3.5 (+100) ★ 0.6u", self.api.edits[-1]["text"])

    def test_legacy_game_view_state_collapses_to_the_summary(self) -> None:
        # "game" was the daily index's selected-game marker; on a per-game
        # card it means the summary, which is what already renders — the
        # state entry is dropped and the message is left untouched.
        rows = committee("401", arms_status="approved")
        rows[5]["side_pick_json"] = json.dumps(SEA_SIDE)
        self.sync(rows)
        self.state["expanded_picks"]["401"] = "game"
        summary = self.sync(rows)
        self.assertNotIn("401", self.state["expanded_picks"])
        self.assertEqual((summary.posted, summary.edited), ([], []))

    def test_a_new_approved_row_edits_only_the_affected_cards(self) -> None:
        rows = committee("401") + committee("402")
        rows[3]["side_pick_json"] = json.dumps(SEA_SIDE)
        rows[10]["side_pick_json"] = json.dumps(SEA_SIDE)
        self.sync(rows)
        rows.append(
            row(
                "cee-bet",
                "401",
                "cee",
                status="approved",
                generated="2026-09-12T12:40:00+00:00",
                pick_market="spread",
                side_pick_json=json.dumps(SEA_SIDE),
            )
        )
        summary = self.sync(rows)
        self.assertEqual(summary.posted, [])
        self.assertEqual(summary.edited, ["picks:401"])
        picks_text = self.api.edits[-1]["text"]
        self.assertIn("<b>Cee</b> · Seahawks -3.5 (+100) ★ 0.6u", picks_text)

    def test_approved_arm_posts_the_picks_card_and_one_loud_bet_alert(self) -> None:
        rows = committee("401", arms_status="approved")
        rows[5]["side_pick_json"] = json.dumps(SEA_SIDE)
        summary = self.sync(rows)
        self.assertEqual(summary.posted, ["picks:401"])
        self.assertEqual(summary.alerts, ["betcard:401:side"])
        alert = next(m for m in self.api.sent if not m["silent"])
        self.assertEqual(alert["topic"], 22)
        self.assertEqual(
            alert["reply_to"],
            self.state["cards"]["picks:401"]["message_id"],
        )
        self.assertEqual(
            alert["text"],
            "🔔 <b>Seahawks -3.5</b> · Patriots @ Seahawks\n"
            "<b>God</b> no bet\n"
            "<b>Rules</b> ★ 0.6u (+100)",
        )
        summary = self.sync(rows)
        self.assertEqual(summary.alerts, [])
        self.assertEqual(summary.edited, [])
        self.assertEqual(sum(1 for m in self.api.sent if not m["silent"]), 1)

    def test_units_and_stars_drift_edit_the_card_in_place(self) -> None:
        rows = committee("401", arms_status="approved")
        rows[5]["side_pick_json"] = json.dumps(SEA_SIDE)
        self.sync(rows)
        card_id = self.state["announced"]["betcard:401:side"]["message_id"]
        rows.append(
            arm_row(
                "c884d868-1111",
                "401",
                "god_rules",
                {**SEA_SIDE, "stake_units": 1.4, "confidence_stars": 2},
                {**PASS_PLAIN, "pass_reason": "ev floor"},
                status="approved",
                generated="2026-09-12T12:45:00+00:00",
            )
        )
        summary = self.sync(rows)
        self.assertEqual(summary.alerts, [])
        self.assertIn("betcard:401:side", summary.edited)
        edit = self.api.edits[-1]
        self.assertEqual(edit["id"], card_id)
        self.assertEqual(
            edit["text"],
            "🔔 <b>Seahawks -3.5</b> · Patriots @ Seahawks\n"
            "<b>God</b> no bet\n"
            "<b>Rules</b> ★→★★ 0.6→1.4u (+100)",
        )
        self.assertEqual(sum(1 for m in self.api.sent if not m["silent"]), 1)
        # a third pass with the same rows changes nothing
        edits_before = len(self.api.edits)
        summary = self.sync(rows)
        self.assertEqual(summary.edited, [])
        self.assertEqual(len(self.api.edits), edits_before)

    def test_a_pass_row_reads_no_bet_with_the_short_reason(self) -> None:
        rows = committee("401", arms_status="approved")
        rows[6]["side_pick_json"] = json.dumps(SEA_SIDE)  # judge bets
        rows[5]["side_pick_json"] = json.dumps(PASS_ADVERSE)  # rules sits
        self.sync(rows)
        alert = next(m for m in self.api.sent if not m["silent"])
        self.assertEqual(
            alert["text"],
            "🔔 <b>Seahawks -3.5</b> · Patriots @ Seahawks\n"
            "<b>God</b> ★ 0.6u (+100)\n"
            "<b>Rules</b> no bet · line moved against",
        )

    def test_a_line_move_shows_in_the_headline(self) -> None:
        rows = committee("401", arms_status="approved")
        rows[5]["side_pick_json"] = json.dumps(SEA_SIDE)
        self.sync(rows)
        rows.append(
            arm_row(
                "c884d868-1111",
                "401",
                "god_rules",
                {**SEA_SIDE, "line": -2.5},
                {**PASS_PLAIN, "pass_reason": "ev floor"},
                status="approved",
                generated="2026-09-12T12:45:00+00:00",
            )
        )
        summary = self.sync(rows)
        self.assertIn("betcard:401:side", summary.edited)
        self.assertEqual(
            self.api.edits[-1]["text"],
            "🔔 <b>Seahawks -3.5→-2.5</b> · Patriots @ Seahawks\n"
            "<b>God</b> no bet\n"
            "<b>Rules</b> ★ 0.6u (+100)",
        )

    def test_an_arm_joining_reposts_the_card_loudly(self) -> None:
        rows = committee("401", arms_status="approved")
        rows[5]["side_pick_json"] = json.dumps(SEA_SIDE)
        self.sync(rows)
        card_id = self.state["announced"]["betcard:401:side"]["message_id"]
        rows.append(
            arm_row(
                "444bf3de-1111",
                "401",
                "god_judge",
                SEA_SIDE,
                PASS_PLAIN,
                status="approved",
                generated="2026-09-12T12:45:00+00:00",
                generation_backend="claude_headless",
            )
        )
        summary = self.sync(rows)
        self.assertEqual(summary.alerts, ["betcard:401:side"])
        self.assertIn(card_id, self.api.deleted)
        alert = self.api.sent[-1]
        self.assertFalse(alert["silent"])
        self.assertEqual(
            alert["text"],
            "🔔 <b>Seahawks -3.5</b> · Patriots @ Seahawks\n"
            "<b>God</b> ★ 0.6u (+100)\n"
            "<b>Rules</b> ★ 0.6u (+100)",
        )
        entry = self.state["announced"]["betcard:401:side"]
        self.assertEqual(entry["message_id"], alert["id"])
        # the rules arm's baseline survives the repost
        self.assertEqual(entry["arms"]["god_rules"]["first"]["units"], 0.6)

    def test_a_hand_deleted_card_is_reposted_silently(self) -> None:
        rows = committee("401", arms_status="approved")
        rows[5]["side_pick_json"] = json.dumps(SEA_SIDE)
        self.sync(rows)
        card_id = self.state["announced"]["betcard:401:side"]["message_id"]
        self.api.missing.add(card_id)
        rows.append(
            arm_row(
                "c884d868-1111",
                "401",
                "god_rules",
                {**SEA_SIDE, "stake_units": 1.4},
                {**PASS_PLAIN, "pass_reason": "ev floor"},
                status="approved",
                generated="2026-09-12T12:45:00+00:00",
            )
        )
        summary = self.sync(rows)
        self.assertEqual(summary.alerts, [])
        self.assertEqual(summary.posted, ["betcard:401:side"])
        replacement = self.api.sent[-1]
        self.assertTrue(replacement["silent"])
        self.assertIn("0.6→1.4u", replacement["text"])
        self.assertEqual(
            self.state["announced"]["betcard:401:side"]["message_id"],
            replacement["id"],
        )

    def test_a_withdrawal_strikes_the_card_silently(self) -> None:
        rows = committee("401", arms_status="approved")
        rows[5]["side_pick_json"] = json.dumps(SEA_SIDE)
        self.sync(rows)
        entry = self.state["announced"]["betcard:401:side"]
        card_id = entry["message_id"]
        self.assertEqual(
            entry["arms"]["god_rules"]["leg"], "Seahawks -3.5 (+100) ★ 0.6u"
        )

        rows.append(
            arm_row(
                "c884d868-1111",
                "401",
                "god_rules",
                {**PASS_PLAIN, "pass_reason": "ev floor"},
                PASS_PLAIN,
                status="approved",
                generated="2026-09-12T12:45:00+00:00",
            )
        )
        summary = self.sync(rows)
        self.assertEqual(summary.alerts, [])
        self.assertIn("betcard:401:side", summary.edited)
        edit = self.api.edits[-1]
        self.assertEqual(edit["id"], card_id)
        self.assertEqual(
            edit["text"],
            "🔕 <s>Seahawks -3.5</s> · Patriots @ Seahawks\n"
            "<b>God</b> no bet\n"
            "<b>Rules</b> ✖ <s>★ 0.6u</s> · edge too thin",
        )
        # the card stays as the record — no delete, no separate message
        self.assertNotIn(card_id, self.api.deleted)
        # …while the picks card goes with the last bet (no bets → no card),
        # and the strike landed anyway despite the missing anchor.
        self.assertIn("picks:401", summary.deleted)
        self.assertNotIn("picks:401", self.state["cards"])
        self.assertTrue(
            self.state["announced"]["betcard:401:side"]["arms"]["god_rules"][
                "withdrawn"
            ]
        )
        # idempotent: later passes stay quiet
        edits_before = len(self.api.edits)
        summary = self.sync(rows)
        self.assertEqual(summary.alerts, [])
        self.assertEqual(len(self.api.edits), edits_before)
        self.assertEqual(sum(1 for m in self.api.sent if not m["silent"]), 1)

    def test_a_partial_withdrawal_keeps_the_live_headline(self) -> None:
        rows = committee("401", arms_status="approved")
        rows[5]["side_pick_json"] = json.dumps(SEA_SIDE)
        rows[6]["side_pick_json"] = json.dumps(SEA_SIDE)
        self.sync(rows)
        rows.append(
            arm_row(
                "c884d868-1111",
                "401",
                "god_rules",
                PASS_ADVERSE,
                PASS_PLAIN,
                status="approved",
                generated="2026-09-12T12:45:00+00:00",
            )
        )
        summary = self.sync(rows)
        self.assertEqual(summary.alerts, [])
        self.assertIn("betcard:401:side", summary.edited)
        self.assertEqual(
            self.api.edits[-1]["text"],
            "🔔 <b>Seahawks -3.5</b> · Patriots @ Seahawks\n"
            "<b>God</b> ★ 0.6u (+100)\n"
            "<b>Rules</b> ✖ <s>★ 0.6u</s> · line moved against",
        )

    def test_a_re_bet_after_a_withdrawal_revives_the_card_loudly(self) -> None:
        rows = committee("401", arms_status="approved")
        rows[5]["side_pick_json"] = json.dumps(SEA_SIDE)
        self.sync(rows)
        dead_id = self.state["announced"]["betcard:401:side"]["message_id"]
        rows.append(
            arm_row(
                "c884d868-1111",
                "401",
                "god_rules",
                {**PASS_PLAIN, "pass_reason": "ev floor"},
                PASS_PLAIN,
                status="approved",
                generated="2026-09-12T12:45:00+00:00",
            )
        )
        self.sync(rows)  # silent flip to 🔕
        rows.append(
            arm_row(
                "c884d868-2222",
                "401",
                "god_rules",
                {**SEA_SIDE, "stake_units": 1.1},
                PASS_PLAIN,
                status="approved",
                generated="2026-09-12T12:50:00+00:00",
            )
        )
        summary = self.sync(rows)
        self.assertEqual(summary.alerts, ["betcard:401:side"])
        self.assertIn(dead_id, self.api.deleted)
        alert = self.api.sent[-1]
        self.assertFalse(alert["silent"])
        # fresh baseline: no drift arrows on the revived card
        self.assertEqual(
            alert["text"],
            "🔔 <b>Seahawks -3.5</b> · Patriots @ Seahawks\n"
            "<b>God</b> no bet\n"
            "<b>Rules</b> ★ 1.1u (+100)",
        )
        self.assertNotIn(
            "withdrawn",
            json.dumps(self.state["announced"]["betcard:401:side"]),
        )
        # and a second withdrawal strikes it silently again
        rows.append(
            arm_row(
                "c884d868-3333",
                "401",
                "god_rules",
                PASS_ADVERSE,
                PASS_PLAIN,
                status="approved",
                generated="2026-09-12T12:55:00+00:00",
            )
        )
        summary = self.sync(rows)
        self.assertEqual(summary.alerts, [])
        self.assertIn(
            "<b>Rules</b> ✖ <s>★ 1.1u</s> · line moved against",
            self.api.edits[-1]["text"],
        )

    def test_a_legacy_withdrawal_promotes_to_a_silent_dead_card(self) -> None:
        legacy_entry = {
            "at": "2026-09-12T12:31:00+00:00",
            "event_id": "401",
            "expert_id": "god_rules",
            "kind": "side",
            "leg": "Seahawks -3.5 (+100) ★ 0.6u",
        }
        self.state["announced"]["bet:c884d868-0000:side"] = dict(legacy_entry)
        rows = committee("401", arms_status="approved")  # rules passes now
        summary = self.sync(rows)
        self.assertEqual(summary.alerts, [])
        card = self.api.sent[-1]
        self.assertTrue(card["silent"])
        self.assertEqual(
            card["text"],
            "🔕 <s>Seahawks -3.5</s> · Patriots @ Seahawks\n"
            "<b>God</b> no bet\n"
            "<b>Rules</b> ✖ <s>★ 0.6u</s> · line moved against",
        )
        self.assertNotIn("bet:c884d868-0000:side", self.state["announced"])

        # …unless the loud-🔕 era already recorded that same withdrawal
        self.state["announced"].pop("betcard:401:side")
        self.state["announced"]["bet:c884d868-0000:side"] = dict(legacy_entry)
        self.state["announced"]["withdrawn:c884d868-0000:side"] = {
            "at": "2026-09-12T13:00:00+00:00",
            "event_id": "401",
        }
        sent_before = len(self.api.sent)
        summary = self.sync(rows)
        self.assertEqual(summary.alerts, [])
        self.assertNotIn("betcard:401:side", self.state["announced"])
        self.assertEqual(len(self.api.sent), sent_before)

    def test_legacy_alert_stays_quiet_until_the_bet_moves(self) -> None:
        # A pre-card ``bet:{opinion_id}`` entry covers the standing bet: the
        # deploy itself must not repost anything. The first real change
        # promotes it to a card — silently, with the announced values as
        # the drift baseline.
        self.state["announced"]["bet:c884d868-0000:side"] = {
            "at": "2026-09-12T12:31:00+00:00",
            "event_id": "401",
            "expert_id": "god_rules",
            "kind": "side",
            "leg": "Seahawks -3.5 (+100) ★ 0.6u",
        }
        rows = committee("401", arms_status="approved")
        rows[5]["side_pick_json"] = json.dumps(SEA_SIDE)
        summary = self.sync(rows)
        self.assertEqual(summary.alerts, [])
        self.assertTrue(all(m["silent"] for m in self.api.sent))
        self.assertNotIn("betcard:401:side", self.state["announced"])

        rows.append(
            arm_row(
                "c884d868-1111",
                "401",
                "god_rules",
                {**SEA_SIDE, "stake_units": 1.4},
                {**PASS_PLAIN, "pass_reason": "ev floor"},
                status="approved",
                generated="2026-09-12T12:45:00+00:00",
            )
        )
        summary = self.sync(rows)
        self.assertEqual(summary.alerts, [])
        self.assertEqual(summary.posted, ["betcard:401:side"])
        card = self.api.sent[-1]
        self.assertTrue(card["silent"])
        self.assertIn("<b>Rules</b> ★ 0.6→1.4u (+100)", card["text"])
        self.assertNotIn("bet:c884d868-0000:side", self.state["announced"])

    def test_legacy_announced_bets_never_fire_withdrawals(self) -> None:
        # Entries written before the withdrawal alert carry no expert_id;
        # firing from them on first deploy would alert for every game whose
        # arm currently passes.
        self.state["announced"]["bet:00000000-aaaa:side"] = {
            "at": "2026-09-10T06:30:06+00:00",
            "event_id": "401",
        }
        rows = committee("401", arms_status="approved")
        summary = self.sync(rows)
        self.assertEqual(summary.alerts, [])
        self.assertTrue(all(m["silent"] for m in self.api.sent))

    def test_started_games_are_frozen_and_later_deleted(self) -> None:
        rows = committee("401", arms_status="approved")
        rows[5]["side_pick_json"] = json.dumps(SEA_SIDE)
        self.sync(rows)
        card_id = self.state["cards"]["picks:401"]["message_id"]
        kickoff = datetime.fromisoformat(SEA_KICKOFF)
        # After kickoff the card is frozen: a model change edits nothing.
        rows[5]["side_pick_json"] = json.dumps({**SEA_SIDE, "stake_units": 1.4})
        summary = self.sync(rows, now=kickoff + timedelta(minutes=5))
        self.assertEqual((summary.edited, summary.alerts), ([], []))
        self.assertIn("picks:401", self.state["cards"])
        # When the game ages out of the model, the card is deleted with it.
        self.sync(rows, now=kickoff + timedelta(days=4))
        self.assertIn(card_id, self.api.deleted)
        self.assertNotIn("picks:401", self.state["cards"])
        self.assertNotIn("401", self.state["kickoffs"])

    def test_a_deleted_card_is_reposted(self) -> None:
        rows = committee("401")
        rows[3]["side_pick_json"] = json.dumps(SEA_SIDE)  # AK bets
        self.sync(rows)
        self.api.missing.add(
            self.state["cards"]["picks:401"]["message_id"]
        )
        rows.append(  # the card's content changes
            row(
                "cee-bet",
                "401",
                "cee",
                status="approved",
                generated="2026-09-12T12:40:00+00:00",
                pick_market="spread",
                side_pick_json=json.dumps(SEA_SIDE),
            )
        )
        summary = self.sync(rows)
        self.assertEqual(summary.posted, ["picks:401"])
        self.assertEqual(
            self.state["cards"]["picks:401"]["message_id"],
            self.api.sent[-1]["id"],
        )

    def test_each_game_takes_its_own_budget_slot(self) -> None:
        rows = committee("401") + committee("402")
        rows[3]["side_pick_json"] = json.dumps(SEA_SIDE)
        rows[10]["side_pick_json"] = json.dumps(SEA_SIDE)
        summary = self.sync(rows, max_posts=1)
        self.assertEqual(summary.posted, ["picks:401"])
        self.assertEqual(summary.deferred, ["picks:402"])
        summary = self.sync(rows, max_posts=1)
        self.assertEqual(summary.posted, ["picks:402"])
        self.assertEqual(summary.deferred, [])

    def test_legacy_review_topic_state_is_dropped_without_api_calls(self) -> None:
        # The Review topic was deleted 2026-09-10; its messages died with it,
        # so the sync forgets those cards instead of trying to delete them.
        self.state["cards"]["queue"] = {"message_id": 7, "hash": "h", "topic": 11}
        self.state["cards"]["review:401"] = {"message_id": 8, "hash": "h", "topic": 11}
        self.state["announced"]["lock:401"] = {"at": "", "event_id": "401"}
        self.sync(committee("401"))
        self.assertNotIn("queue", self.state["cards"])
        self.assertNotIn("review:401", self.state["cards"])
        self.assertNotIn("lock:401", self.state["announced"])
        self.assertEqual(self.api.deleted, [])

    def test_api_errors_are_collected_not_raised(self) -> None:
        class Broken(FakeApi):
            def send(self, *args, **kwargs):
                raise DeskApiError("sendMessage: chat not found")

        self.api = Broken()
        rows = committee("401")
        rows[3]["side_pick_json"] = json.dumps(SEA_SIDE)
        summary = self.sync(rows)
        self.assertEqual(summary.posted, [])
        self.assertEqual(len(summary.errors), 1)
        self.assertEqual(self.state["cards"], {})


class StateTests(unittest.TestCase):
    def test_round_trip_and_bad_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "nested" / "state.json"
            state = empty_state()
            state["cards"]["week"] = {"message_id": 5, "hash": "h", "topic": 1}
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
            "picks:old": {"message_id": 1},
            "picks:new": {"message_id": 2},
            "week": {"message_id": 3},
        }
        state["announced"] = {
            "bet:x:side": {"at": "", "event_id": "old"},
            "bet:y:side": {"at": "", "event_id": "new"},
        }
        prune_state(state, now=NOW)
        self.assertEqual(sorted(state["cards"]), ["picks:new", "week"])
        self.assertEqual(list(state["announced"]), ["bet:y:side"])
        self.assertEqual(list(state["kickoffs"]), ["new"])

    def test_content_hash_covers_topic_text_and_keyboard(self) -> None:
        base = content_hash("t", [], 1)
        self.assertNotEqual(base, content_hash("t", [], 2))
        self.assertNotEqual(base, content_hash("t", [[{"text": "x", "callback_data": "y"}]], 1))
        self.assertEqual(base, content_hash("t", [], 1))


class ConfigAndCallbackTests(unittest.TestCase):
    def test_config_from_env_requires_chat_and_picks_topic(self) -> None:
        env = {
            "INTAKE_BOT_TOKEN": "t",
            "MOE_DESK_CHAT_ID": "-100123",
            "MOE_DESK_PICKS_TOPIC": "3",
            "MOE_DESK_SCORES_TOPIC": "4",
            "MOE_DESK_OFFLINE_TOPIC": "5",
            "MOE_DESK_SYNC_SECONDS": "5",
            # obsolete since the Review topic was removed (2026-09-10):
            # both are ignored if a .env still carries them
            "MOE_DESK_REVIEW_TOPIC": "2",
            "MOE_DESK_LOCK_WARN_HOURS": "1.5",
        }
        config = desk_config_from_env(env)
        self.assertEqual(
            (
                config.chat_id,
                config.picks_topic,
                config.scores_topic,
                config.offline_topic,
            ),
            ("-100123", 3, 4, 5),
        )
        self.assertEqual(config.sync_seconds, 15)  # floor
        self.assertEqual(config.with_username("@nflguesser_bot").bot_username, "nflguesser_bot")
        for missing in ("INTAKE_BOT_TOKEN", "MOE_DESK_CHAT_ID", "MOE_DESK_PICKS_TOPIC"):
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
        # the removed review actions no longer parse (stale buttons answer
        # "Unknown desk action" instead of acting)
        self.assertIsNone(parse_callback("desk:ok:abc"))
        self.assertIsNone(parse_callback("desk:no:abc"))
        self.assertIsNone(parse_callback("desk:okarms:401"))
        self.assertEqual(parse_callback("desk:show:401"), ("show", "401"))
        self.assertEqual(parse_callback("desk:hide:401"), ("hide", "401"))
        self.assertEqual(parse_callback("desk:game:401"), ("game", "401"))
        self.assertEqual(parse_callback("desk:games:401"), ("games", "401"))
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
        self.assertEqual(
            parse_callback("desk:refresh:401"),
            ("refresh", "401"),
        )
        self.assertIsNone(parse_callback("desk:zap:401"))
        self.assertIsNone(parse_callback("desk:show:"))
        self.assertIsNone(parse_callback("moe:view:401:0"))


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

    def test_scores_notice_posts_proportional_text_or_declines(self) -> None:
        # Never <pre>: the digest is phone-width prose; a mobile <pre> bubble
        # wrapped the old aligned columns mid-number (2026-09-10). The first
        # line (the "pickbot:" header) is bold, the rest escaped as-is.
        api = FakeApi()
        env = {
            "INTAKE_BOT_TOKEN": "t",
            "MOE_DESK_CHAT_ID": "-1",
            "MOE_DESK_PICKS_TOPIC": "2",
            "MOE_DESK_SCORES_TOPIC": "3",
        }
        self.assertTrue(post_scores_notice("a <b> c\nline 2", environ=env, api=api))
        self.assertEqual(api.sent[0]["topic"], 3)
        self.assertEqual(api.sent[0]["text"], "<b>a &lt;b&gt; c</b>\nline 2")
        self.assertTrue(api.sent[0]["silent"])
        # A pre-rendered HTML digest is sent as-is, the plain text unused.
        self.assertTrue(
            post_scores_notice(
                "plain", html="<b>hdr</b>\n<blockquote>x</blockquote>",
                environ=env, api=api,
            )
        )
        self.assertEqual(
            api.sent[1]["text"], "<b>hdr</b>\n<blockquote>x</blockquote>"
        )
        self.assertFalse(post_scores_notice("x", environ={**env, "MOE_DESK_SCORES_TOPIC": ""}, api=api))
        self.assertFalse(post_scores_notice("x", environ={}, api=api))


if __name__ == "__main__":
    unittest.main()
