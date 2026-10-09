---
name: record
description: Bet-by-bet record (chronological list + W-L, units, ROI) for any Telegram capper or NFL MOE expert/arm (god_judge, god_rules, ak, cee, …), sent as a formatted Telegram message. Use when the operator asks how a capper/expert/system has been doing, for its record or bets, optionally sides-only or totals-only.
---

# Record

One command renders the record as Telegram HTML and sends it:

```bash
cd ~/app && ~/venv/bin/python scripts/record.py <name> [--market all|sides|totals] --send chat
```

- `<name>`: an MOE expert id or registry name (`god_judge`, `god_rules`, `ak`, `cee`, `celebrity`, …),
  or a capper (`trent`, `travy`, `james bets`, …). Matching ignores case, spaces and emoji. A partial
  name works when it hits exactly one capper. `--list` prints every known name. An ambiguous or
  unknown name exits 2 and lists the candidates, so ask the operator which one they meant.
- `--market`: `sides` (spreads/ML; for MOE the side leg) or `totals` (game + team totals). Default `all`.
- `--send`: `chat` posts into the operator's conversation with this bot (the default when they ask
  in chat). `test` is the TEST channel (format previews), `me` is the watchdog bot DM, or a numeric
  chat id. Without `--send` it prints the HTML.

Format (operator-picked 2026-10-09, "Mix A"): header with record · units · ROI, the ✅❌ strip
(last 20), last 5 and streak, splits (sides / totals / other, and the max-stake bets), then every
week in date order, each with its record/units and a monospace table. Long records split across
messages at week boundaries. Keep it that way; a layout change needs the operator's pick (preview
it with `--send test`).

## What the numbers mean (say so when it matters)

- **MOE experts**: the standing decision at kickoff per game (the latest approved row, the same
  rule as `moe_grade.py`'s scoreboard), graded against ESPN finals at the row's own line. Units =
  `stake_units` (1u for experts without a stake). Reads the NFL sheet + ESPN, so it takes ~1 min
  and runs on the VPS only.
- **Cappers**: the `grades` table in picks.db, one bet per leg. A fanned-out pick counts once.
  Parlay legs and voided/ungraded legs are left out. **Flat 1u per pick** at the posted price (the
  capper's own unit sizes aren't reliable), and −110 is assumed when no price was captured.
  Rows whose parse has aged out of parse_cache.json are classified from the leg text, so a stray
  pick can land under "Other" instead of sides/totals.

After sending, reply briefly with the headline (record, units) and anything surprising.
For a question the message doesn't answer (why a bet lost, a capper's split by sport), go to the data
directly: `moe_grade.py` (read-only without `--write`) and picks.db `grades`.

Code: `scripts/record.py`. Test: `scripts/test_record.py`.
