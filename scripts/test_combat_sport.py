"""Regression test: a fighter parsed as Boxing who is on an MMA card → UFC.

Self-contained (no Telegram/API) — run it directly:

    ~/venv/bin/python scripts/test_combat_sport.py

Dagger's "Roque Conceição ML -110 2U" (-1002486251914:3974, 2026-10-06) is a
Dana White's Contender Series bout; the parse said Boxing (no odds source →
sport_unsupported(Boxing), and a boxing grading feed). resolve_combat_sport
(odds.py) moves it to UFC because Bovada's MMA coupon lists him and its boxing
coupon does not; real boxers and unknowns keep the parse.

Fixture: Bovada's ufc-mma and boxing coupons captured 2026-10-06T23:52Z,
trimmed to event identity (competitors/startTime).
"""
import asyncio
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import odds

FIXTURE = Path(__file__).resolve().parent / "fixtures" / "bovada_combat_coupons_20261006.json"
DATA = json.load(open(FIXTURE, encoding="utf-8"))


def _resolve(sport, teams, mma=DATA["ufc-mma"], boxing=DATA["boxing"]):
    odds._bovada_cache["UFC"] = (time.time(), mma)
    odds._bovada_cache["Boxing"] = (time.time(), boxing)
    return asyncio.run(odds.resolve_combat_sport(sport, teams))


CASES = [
    # (label, sport, teams, mma coupon, boxing coupon, expected)
    ("the incident: MMA fighter parsed as Boxing", "Boxing", ["Roque Conceição"], None, None, "UFC"),
    ("surname only, accent-free", "Boxing", ["Conceicao"], None, None, "UFC"),
    ("opponent on the same bout", "Boxing", ["Alexander Chavez"], None, None, "UFC"),
    ("real boxer on the boxing card stays", "Boxing", ["Tyson Fury"], None, None, "Boxing"),
    ("boxer on no card stays (no MMA match)", "Boxing", ["Canelo Alvarez"], None, None, "Boxing"),
    ("different first name = partial match, stays", "Boxing", ["Robson Conceicao"], None, None, "Boxing"),
    ("empty boxing coupon (failed fetch) fails closed", "Boxing", ["Roque Conceição"], None, [], "Boxing"),
    ("empty MMA coupon fails closed", "Boxing", ["Roque Conceição"], [], None, "Boxing"),
    ("UFC parse is never touched", "UFC", ["Tyson Fury"], None, None, "UFC"),
    ("no teams", "Boxing", [], None, None, "Boxing"),
]

fails = 0
for label, sport, teams, mma, boxing, want in CASES:
    got = _resolve(sport, teams,
                   DATA["ufc-mma"] if mma is None else mma,
                   DATA["boxing"] if boxing is None else boxing)
    ok = got == want
    fails += not ok
    print(f"{'PASS' if ok else 'FAIL'}  {label}: {sport} {teams} -> {got} (want {want})")

print(f"\n{len(CASES) - fails}/{len(CASES)} passed")
sys.exit(1 if fails else 0)
