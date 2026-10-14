#!/usr/bin/env python3
"""Offline test: the odds tag is a bare price behind an invisible marker.

Operator 2026-10-13: "[-130]" → "-130". A bare price reads exactly like one
the capper typed, so every tag starts with common.ODDS_TAG_MARK (U+2060, kept
by Telegram through send/edit) — the only way the renderers re-find, swap and
strip their own tag. Pins the rendered shapes, the whole-tag matching (-130 is
not a prefix of -1300 or "-130 now"), and that every consumer still reads the
legacy bracket form on posts tagged before the switch.

    python scripts/test_odds_tag_format.py
"""
import importlib.util
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import odds_checks as oc  # noqa: E402
from ai import _demote_unworded_card_parlay  # noqa: E402
from common import ODDS_TAG_RE, has_odds_tag, modernize_odds_tags, odds_tag  # noqa: E402
from tracker import _is_text_thin  # noqa: E402
from tracker_format import _insert_odds  # noqa: E402

M = "⁠"
failures = []


def check(label: str, ok: bool, detail: object = ""):
    print(f"{'PASS' if ok else 'FAIL'}  {label}" + (f"  ({detail!r})" if detail and not ok else ""))
    if not ok:
        failures.append(label)


def pick(desc, bt="moneyline", teams=None, line=None, leg=False):
    return {"description": desc, "sport": "NFL", "bet_type": bt, "is_parlay_leg": leg,
            "period": "game", "teams": teams or [], "player": None, "prop_stat": None,
            "line": line, "direction": None}


def o(price, mt="exact", **kw):
    return {"odds": price, "match_type": mt, **kw}


BILLS = [pick("Buffalo Bills ML", teams=["Buffalo Bills"])]

# ── the rendered shapes: no brackets anywhere ───────────────────────────────
check("plain tag", _insert_odds("Bills ML", BILLS, {"0": o(-130)}) == f"Bills ML {M}-130")
check("plus sign kept", _insert_odds("Bills ML", BILLS, {"0": o(150)}) == f"Bills ML {M}+150")
check("live + pregame closing line",
      _insert_odds("Bills ML", BILLS, {"0": o(1200, "live_exact", pregame_odds=-107)})
      == f"Bills ML {M}+1200 live {M}-107 pre")
check("pregame-only tag", _insert_odds("Bills ML", BILLS, {"0": o(-107, "pregame_exact")})
      == f"Bills ML {M}-107 pre")
stated = [pick("Bills -3 (-110)", "spread", teams=["Buffalo Bills"], line=-3)]
check("moved past the capper's price → now tag",
      _insert_odds("Bills -3 (-110)", stated, {"0": o(-190)}) == f"Bills -3 (-110) {M}-190 now")
legs = [pick("Bills ML", teams=["Buffalo Bills"], leg=True),
        pick("Chiefs ML", teams=["Kansas City Chiefs"], leg=True)]
check("parlay combined tag on the header",
      _insert_odds("Parlay:\nBills ML\nChiefs ML", legs, {"0": o(-150), "1": o(-150)})
      .split("\n")[0] == f"Parlay: {M}+178")
check("visible text has no brackets",
      "[" not in _insert_odds("Bills ML", BILLS, {"0": o(1200, "live_exact", pregame_odds=-107)}))

# ── idempotency + whole-tag matching ────────────────────────────────────────
once = _insert_odds("Bills ML", BILLS, {"0": o(-130)})
check("re-render is a no-op", _insert_odds(once, BILLS, {"0": o(-130)}) == once)
check("-130 is not inside -1300", not has_odds_tag(f"x {M}-1300", odds_tag(-130)))
check("-190 is not inside '-190 now'", not has_odds_tag(f"x {M}-190 now", odds_tag(-190)))
check("a whole tag matches", has_odds_tag(f"x {M}-190 now ✅", odds_tag(-190, "now")))
check("the capper's bare -130 is not our tag", not ODDS_TAG_RE.search("Bills ML -130"))
check("strip leaves the capper's text", ODDS_TAG_RE.sub("", f"Bills ML -110 {M}-190 now") == "Bills ML -110")

# ── estimate → quote swap, new and legacy tags ──────────────────────────────
tcu = [pick("TCU +8.5", "spread", teams=["TCU Horned Frogs"], line=8.5)]
swap = {"0": o(-160, replaced=-179)}
check("swap a marked tag", _insert_odds(f"TCU +8.5 {M}-179", tcu, swap) == f"TCU +8.5 {M}-160")
check("swap a legacy [..] tag", _insert_odds("TCU +8.5 [-179]", tcu, swap) == f"TCU +8.5 {M}-160")
check("swap never eats a longer price",
      _insert_odds(f"TCU +8.5 {M}-1790", tcu, {"0": o(-160, replaced=-179)}) == f"TCU +8.5 {M}-1790")

# ── legacy posts: recognized as ours, rewritten on the next render ──────────
check("legacy tag modernized in place",
      _insert_odds("Bills ML [-130] ✅", BILLS, {"0": o(-130)}) == f"Bills ML {M}-130 ✅")
check("legacy now tag modernized, no second tag",
      _insert_odds("Bills -3 (-110) [-190 now]", stated, {"0": o(-190)}) == f"Bills -3 (-110) {M}-190 now")
check("modernize keeps kinds", modernize_odds_tags("[+1200 live] [-107 pre]") == f"{M}+1200 live {M}-107 pre")

# ── consumers that read tags off the live post ──────────────────────────────
for form, text in (("marked", f"Bills -3 (-110) {M}-190 now"), ("legacy", "Bills -3 (-110) [-190 now]")):
    entry = {"html_text": text, "parsed": {"picks": stated}, "odds_by_pick": {"0": o(-190)}}
    rules = {h["rule"] for h in oc.scan_entry("-1:1", entry)}
    check(f"odds_checks: now_move off a {form} tag", "now_move" in rules, rules)
for form, text in (("marked", f"Bulldogs +6.5 {M}-300"), ("legacy", "Bulldogs +6.5 [-300]")):
    dog = [pick("Bulldogs +6.5", "spread", teams=["Georgia Bulldogs"], line=6.5)]
    entry = {"html_text": text, "parsed": {"picks": dog}, "odds_by_pick": {"0": o(-300)}}
    rules = {h["rule"] for h in oc.scan_entry("-1:1", entry)}
    check(f"odds_checks: a shown {form} tag is judged", "band" in rules, rules)
check("thin check ignores our tag", _is_text_thin(f"it's coming home {M}+150"))
card = {"picks": [pick("Bills ML", leg=True), pick("Chiefs ML", leg=True)]}
_demote_unworded_card_parlay(card, f"Bills ML {M}-150\nChiefs ML {M}-120", has_image=False)
check("card demote: our tag isn't a second bet token",
      not any(p["is_parlay_leg"] for p in card["picks"]))

spec = importlib.util.spec_from_file_location("extract_angles", ROOT / "angles" / "extract_angles.py")
try:
    ea = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(ea)
except Exception as exc:  # noqa: BLE001 - telethon/dotenv missing → skip, not fail
    print(f"SKIP  angles extractor ({type(exc).__name__}: {exc})")
else:
    check("angles: reads a marked tag", ea._extract_odds(f"Bills ML {M}-130 ✅") == -130)
    check("angles: reads a marked tag at the end", ea._extract_odds(f"Bills ML {M}+150") == 150)
    check("angles: still reads a legacy tag", ea._extract_odds("Bills ML [-130]") == -130)

print(f"\n{'ALL PASS' if not failures else f'{len(failures)} FAILED'}")
sys.exit(1 if failures else 0)
