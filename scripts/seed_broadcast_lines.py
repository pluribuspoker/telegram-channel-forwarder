"""Seed the broadcast_lines dedupe ledger from already-broadcast verdicts.

Run at deploy whenever `_format_pick`'s output changes (nickname map, layout,
new branch wording): the ledger stores fingerprints OF the rendered line, so a
renderer change orphans every live row and the next restated bet re-posts —
the exact incident the ledger exists to stop (investigate lesson 51). This
recomputes each recently-broadcast line's fingerprint with the CURRENT
production code path (`_capper_label` / `_format_pick` /
`_result_line_fingerprint`, ticket semantics included) and claims it.

Only lines still inside the dedupe window are seeded — a seeded row's
`sent_at` is now, so seeding older results would wrongly extend their
suppression against a genuinely re-bet market. Recency comes from the leg's
`game_date` (results post within hours of the final), falling back to the
entry's `msg_date`.

    ~/venv/bin/python scripts/seed_broadcast_lines.py --dry-run
    ~/venv/bin/python scripts/seed_broadcast_lines.py

Idempotent (INSERT OR IGNORE on the fingerprint PK). Stop grade-daemon first
so it can't post an old-format line between the scan and its restart.
"""
import argparse
import json
import os
import sqlite3
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from dotenv import load_dotenv

load_dotenv()
load_dotenv(".env.local", override=True)

from audit import (  # noqa: E402
    BROADCAST_DEDUPE_DAYS, DB_PATH, _capper_label, _format_pick,
    _result_line_fingerprint,
)
from tracker_grading import _overall_verdict  # noqa: E402

CACHE_PATH = os.path.join(os.path.dirname(DB_PATH), "parse_cache.json")


def _broadcast_map() -> dict[int, int]:
    out: dict[int, int] = {}
    for m in json.loads(os.getenv("MAPPINGS_CONFIG", "[]")):
        dest, bc = m.get("dest_channel"), m.get("broadcast_results_channel")
        if dest and bc:
            out[dest] = bc
    return out


def _recent(v: dict, entry: dict, cutoff: date) -> bool:
    for raw in (v.get("game_date"), (entry.get("msg_date") or "")[:10]):
        if raw:
            try:
                return date.fromisoformat(raw) >= cutoff
            except ValueError:
                continue
    return False


def collect(cache: dict, bc_map: dict[int, int], cutoff: date) -> list[tuple[int, str, str]]:
    """(target_channel, fingerprint, human line) per already-broadcast result."""
    rows: list[tuple[int, str, str]] = []
    for key, entry in cache.items():
        try:
            channel_id = int(key.split(":")[0])
        except ValueError:
            continue
        target = bc_map.get(channel_id)
        if not target or not isinstance(entry, dict):
            continue
        picks = (entry.get("parsed") or {}).get("picks") or []
        verdicts = entry.get("leg_verdicts") or {}
        capper = _capper_label(entry.get("capper_name") or "")

        def leg(i: int) -> tuple[dict, str, None]:
            v = verdicts.get(str(i)) or {}
            return (picks[i], v.get("verdict") or "PENDING", None)

        legs = [leg(i) for i in range(len(picks))]
        parlay = [(p, v, o) for p, v, o in legs if p.get("is_parlay_leg")]

        for i, (p, v, _) in enumerate(legs):
            vd = verdicts.get(str(i)) or {}
            if (v in ("WIN", "LOSS", "PUSH") and vd.get("broadcasted")
                    and not p.get("is_parlay_leg") and _recent(vd, entry, cutoff)):
                text = _format_pick(p)
                rows.append((target, _result_line_fingerprint(capper, v, text),
                             f"{capper} | {v} | {text}"))

        if parlay:
            pv = _overall_verdict(parlay)
            bc_flags = [verdicts.get(str(i), {}) for i, (p, _, _) in enumerate(legs)
                        if p.get("is_parlay_leg")]
            if (pv in ("WIN", "LOSS", "PUSH")
                    and any(v.get("broadcasted") for v in bc_flags)
                    and any(_recent(v, entry, cutoff) for v in bc_flags)):
                text = "parlay:" + " / ".join(_format_pick(p) for p, _, _ in parlay)
                rows.append((target, _result_line_fingerprint(capper, pv, text),
                             f"{capper} | {pv} | {text}"))
    return rows


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--days", type=int, default=BROADCAST_DEDUPE_DAYS)
    ap.add_argument("--source", default=f"seed:{date.today().isoformat()}")
    args = ap.parse_args()

    cutoff = date.today() - timedelta(days=args.days)
    cache = json.load(open(CACHE_PATH))
    rows = collect(cache, _broadcast_map(), cutoff)

    # One capper restating a bet yields the same fingerprint twice — dedupe for
    # the report; the PK would ignore the second insert anyway.
    seen: dict[tuple[int, str], str] = {}
    for target, fp, line in rows:
        seen.setdefault((target, fp), line)

    for (target, _fp), line in seen.items():
        print(f"  {target}  {line}")
    print(f"{len(seen)} line(s) since {cutoff}")

    if args.dry_run:
        print("dry run — nothing written")
        return 0

    now = datetime.now(timezone.utc).isoformat()
    with sqlite3.connect(DB_PATH) as conn:
        conn.executemany(
            "INSERT OR IGNORE INTO broadcast_lines"
            " (target_channel, fingerprint, sent_at, source) VALUES (?,?,?,?)",
            [(t, fp, now, args.source) for (t, fp) in seen],
        )
        conn.commit()
        total = conn.execute("SELECT COUNT(*) FROM broadcast_lines").fetchone()[0]
    print(f"seeded — ledger now holds {total} row(s)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
