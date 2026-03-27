# Pick Tracker

Nightly cron that grades forwarded picks (win/loss) by looking up completed game results and editing the Telegram message caption.

---

## Architecture

1. **Cron triggers nightly** (e.g. 2am) on the VPS
2. **Fetch recently completed games** from Odds API scores endpoint
3. **Read ungraded pick messages** from destination channel history
4. **Claude parses each pick** — extracts sport, teams, bet type, line/spread
5. **Match pick to a completed game** from the Odds API results
6. **Claude determines win/loss** based on result + bet details
7. **Bot edits the message caption** — appends ✅ (win) or ❌ (loss)

---

## Channels to support

- TBD (two destination channels — user to provide)

---

## Development phases

### Phase 1 — Backtest harness ✅

- Exported message history from both destination channels
- Built `tracker.py` with `--backtest` mode
- Accuracy: DF 97% (76/78), Cappers Lab 100% (16/16)

### Phase 2 — Iterate on prompts ✅

- Prompts validated against both channels
- No significant iteration needed — high accuracy on first pass

### Phase 3 — Backfill ✅

- Live mode built with per-pick HTML emoji insertion (preserves formatting)
- Ran against full channel history

### Phase 4 — Ongoing cron ✅

- Deployed as systemd timer on VPS — fires every 5 minutes
- Parse cache (`parse_cache.json`) avoids re-parsing pending picks
- Healthchecks.io monitoring with start/success/fail signals

---

## Key decisions

| Decision | Choice |
|---|---|
| Sports API | TBD — ESPN API (free, unofficial) or Odds API (have key). Evaluate during backtest. |
| Win/loss logic | Claude — parses pick text, matches game, determines outcome |
| Marking method | Edit message caption — append ✅ or ❌ |
| Ungraded detection | Check if caption already contains ✅ or ❌ |
| State | Stateless — re-scan recent window each run, skip already-graded |
| Same repo | Yes — shares session, bot token, `.env`, `common.py` |

---

## Env vars needed

```
ODDS_API_KEY=...   # have from previous project, may not be needed if ESPN API suffices
```

All other vars already in `.env`.

---

## Audit log

Every grade action (live or dry-run) writes to `picks.db` (SQLite) and posts to a private Telegram audit channel.

| Component | Detail |
|---|---|
| DB file | `picks.db` on VPS (same dir as tracker.py) — write-only from tracker |
| Table | `grades` — one row per channel+message |
| Key columns | `verdict`, `calc`, `prev_caption`, `new_caption`, `dry_run`, `graded_at` |
| Audit channel | Private Telegram channel; set `AUDIT_CHANNEL_ID` in `.env` — primary review surface |
| Audit message | `✅ WIN — NBA [date]\n<pick desc>\nCalc: ...\n→ t.me/c/...` |
| Double-grade guard | Caption-based: skip messages whose caption already contains ✅ or ❌ |
| Dry-run | `--dry-run` flag — records to DB with `dry_run=1`, posts `[DRY RUN]` to audit channel, skips Telegram edit |

SQLite is append-only from the tracker's perspective — never read back during grading. It exists as a durable off-channel record you can query manually if needed (`sqlite3 picks.db "SELECT * FROM grades ORDER BY graded_at DESC LIMIT 20"`).

Module: `audit.py` (`AuditLog` class).

---

## Files

- `tracker.py` — main script
- `audit.py` — SQLite + Telegram audit log
- `picks.db` — SQLite database (created at first run, gitignored)
- `plans/pick-tracker.md` — this file
