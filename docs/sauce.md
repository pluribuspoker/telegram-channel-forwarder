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

