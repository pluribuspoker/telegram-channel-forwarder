# Sauce daily (Kyle Kirms)

> Full reference moved out of CLAUDE.md (terse rules live there). This file is read on demand — keep the complete detail and incident history HERE, not in CLAUDE.md.

## Sauce daily (Kyle Kirms)

`scripts/sauce_daily.py` scrapes the SAUCE tab, grades picks, renders an image (Pillow), and sends it to channel `-1003977774560`. Runs daily at **6 AM ET** via cron on the VPS (`run_sauce_daily.sh`).

- **Google Sheet:** `1yozWEoQ5m6rqNC8-E5UGwg0ySjYbAybNHwPmtNTYIzM` (shared with service account)
- **Source data:** Published Google Sheet embedded at kylekirms.com/open-bets (sheet ID `1yjaN85i-WRhRrBcozOG70vTX6cTNpJzFmuNJ8KgL-14`)
- **DB table:** `sauce_picks` in `picks.db`
- **Cron log:** `/tmp/sauce_daily_cron.log`
- **Image rendering:** Uses **Pillow** (`render_image_pil` in `sauce_daily.py`), rendered in-process — no Chromium. Switched off Playwright (commit e252302) because the headless-Chromium render tree OOM'd on the ~1GB/no-swap VPS. Requires `fonts-liberation` on the VPS (`/usr/share/fonts/truetype/liberation/`); result marks are vector-drawn (check/cross/circle/?), not emoji.

**Manual run on VPS:**
```bash
su - forwarder -c "cd ~/app && ~/venv/bin/python scripts/sauce_daily.py --channel -1003977774560 2>&1"
```

**ESPN sport validation:** `validate_sport()` in `scores.py` verifies Claude's sport classification against ESPN game schedules. Catches ambiguous teams (Rangers, Cardinals, Giants, etc.). Also wired into the core tracker flow in `tracker.py`.

## Intraday watcher (`sauce-watch.timer`, 2026-09-20)

New bets reach the channel within ~20–25 min instead of at the next 6 AM run, with no extra visibility risk: every tick is ONE anonymous GET to the sheet's publish-to-web endpoint on docs.google.com (the same fetch the daily uses — kylekirms.com is never touched, and Google exposes no view logs or analytics for published sheets, so the owner can't see pulls or their frequency; the only real exposure vectors were his website and authenticated sheet access, and we do neither).

- Chain: `sauce-watch.timer` → `sauce-watch.service` → `run_sauce_watch.sh` → `sauce_daily.py --channel -1003977774560 --only-if-new`. Cadence `OnUnitActiveSec=20min` + `RandomizedDelaySec=300` (jitter so polls don't tick like a metronome).
- `--only-if-new` (watcher mode): after the scrape, `get_new_picks()` diffs sheet rows against `sauce_picks` on the SAME key `upsert_picks` inserts on (`(_date_to_iso(date), bet)`), so a detected pick always upserts and can never re-trigger. No new rows → exit before any Claude/ESPN/sheet work. New rows → the normal full pipeline runs and the sent image carries a count-only `🆕 N new pick(s)` caption (operator-picked 2026-09-20 from five test-channel mockups — no pick list; the image carries the details).
- **Cost:** a no-change tick is $0 Claude (one GET, one SQLite read). A new-pick tick costs the same Haiku parse as the daily run (<1¢) — spend scales with how often Kyle posts, not with poll frequency.
- **Concurrency:** `run_sauce_watch.sh` and `run_sauce_daily.sh` share `flock` on `/tmp/sauce_daily.lock` (watch skips its tick after 300s waiting, exit 0; daily fails after 600s) so a tick and the 6 AM cron can't interleave DB writes or double-send.
- **Known failure mode (accepted):** a new-pick tick dying after upsert (step 3) but before the send skips the ping for those picks — the rows are now "known", so the next new pick or the 6 AM image covers them. Don't "fix" this by moving upsert after send; grading needs the rows in the DB first.
- Log: `/tmp/sauce_watch_last_run.log` (journal has the same lines). Healthcheck env: `SAUCE_WATCH_HEALTHCHECK_URL` (unset = silent no-op, same as trent).
- Disable: `sudo systemctl disable --now sauce-watch.timer`. The 6 AM cron is unchanged (unconditional send, no caption) and stays as the daily anchor/grading recap.

