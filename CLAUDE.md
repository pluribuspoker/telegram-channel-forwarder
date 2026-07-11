# Telegram Channel Forwarder

## Preferences

- When giving shell commands, chain related steps with `&&` on one line rather than separate lines.
- Use git worktrees for parallel tasks (e.g. a second task while another Claude session is already working). Pattern: `git worktree add ../telegram-forwarder-<slug> -b <branch>`, work there, commit+push the branch, then merge to main from the main repo dir. Name the dir `../telegram-forwarder-<short-slug>`. Clean up with `git worktree remove ../telegram-forwarder-<slug>`.

## Telegram message formatting

When sending/forwarding messages, always preserve `msg.entities` (bold, italic, blockquotes, etc.) via `formatting_entities=`. Never rebuild message text without passing entities through. Use `text_suffix` in `send_group` to append text without dropping entities.

## VPS

- **Reserved IP:** `209.38.51.86` (always use this, not the droplet IP)
- **SSH:** `ssh root@209.38.51.86`
- **Aliases (interactive SSH only):** defined in `/root/.server_aliases.sh` — `flogs`, `tlogs`, `logs`, `start`, `stop`, `restart`, `status`, `deploy`, `grade`, `gradetest`. These are not available via non-interactive `ssh root@... 'command'`.

**Deploy cautiously.** Rapid bot session restarts trigger Telegram flood waits. If you are confident in a fix and have verified it, you may push and deploy. Otherwise let the user handle it.

### Switching to test mode

**Locally** (no stop/start needed — uses local `.env.local` sessions):
```bash
python listener.py --test
```

**On VPS** (must stop the live service first):
```bash
stop
su - forwarder
cd ~/app
~/venv/bin/python listener.py --test
# Ctrl+C when done
exit
start
```

In both cases, `test_source_channel` → `test_dest_channel` from `MAPPINGS_CONFIG`, and all `filter_pattern` checks are bypassed.

### Service name

The systemd unit is `telegram-forwarder.service`. Use `flogs` / `tlogs` aliases for logs on the VPS, or `journalctl -u telegram-forwarder` for raw access.

### Environment files

Two-file split to protect sessions from `syncenv`:

| File | Where | Synced | Contains |
|---|---|---|---|
| `.env` | local + server | ✅ `syncenv` copies this | all config except session strings |
| `.env.local` | local + server (separately) | ❌ never touched | `TELEGRAM_SESSION`, `BOT_SESSION` |

`syncenv` is safe to run freely. `.env.local` is loaded after `.env` in both Python code and systemd, so it always wins.

**Regex escaping in `MAPPINGS_CONFIG`:** The JSON value is inside single quotes in the `.env` file, so regex backslashes need **four** backslashes (`\\\\`) to survive: shell quotes → JSON string → regex. For example, `\d+` becomes `\\\\d+` in `.env`.

**Setting up `.env.local` (first time, on each machine):**

```bash
python scripts/get_session.py      # generates TELEGRAM_SESSION
python scripts/get_bot_session.py  # generates BOT_SESSION
```
Run these **on the VPS** to tie the VPS sessions to `209.38.51.86`. Run **locally** for local dev sessions.

**After writing `.env.local` on the VPS, fix permissions:**
```bash
chmod 600 /home/forwarder/app/.env.local && chown forwarder:forwarder /home/forwarder/app/.env.local
```

---

## Log colors

Colors and formatting are applied by `_fmtlog` in `/root/.server_aliases.sh` — **do not add ANSI codes to Python print statements**. Edit that file directly on the server (no restart needed).

---

## Pick tracker

**`grade` alias uses `--days 1`** — for older picks, run manually:
```bash
su - forwarder -c "cd ~/app && ~/venv/bin/python tracker.py --live --days 2 2>&1"
```

**NHL 3-way / regulation moneyline:** Must win in regulation — OT = LOSS. Detection centralized in `is_regulation_ml()` (`common.py`).

**KBO (Korean Baseball):** Graded via `koreabaseball.com` ASMX endpoint (`fetch_kbo_context` in `scores.py`). The Odds API has KBO odds but never populates scores, so we scrape the official site instead. Picks are always sent the US evening before the game day, so the code fetches `date+1` to find the correct game. Team ID map (`KBO_TEAM_IDS`) is in `scores.py`. If a pick re-parses as `sport: "Other"` despite the message containing "kbo", the post-parse correction in `claude_parse` (`ai.py`) should catch it.

## Odds integration

To force a re-fetch after manually restoring a cache entry: delete the `odds_by_pick` key from the relevant `parse_cache.json` entry — the next run will re-fetch and re-edit.

**Backtest / audit:**
```bash
python scripts/audit_odds.py --days-back 7
python scripts/audit_odds.py --dry-run
```

## Broadcast results

**Testing workflow** (reset emojis and re-run locally):
```bash
python scripts/clear_emojis.py --channel -100xxxxxxxxxx  # strip emojis (today)
python scripts/clear_emojis.py --days 2                  # last 2 days
python tracker.py --live --channel -100xxxxxxxxxx        # re-grade + broadcast
```

## Sauce daily (Kyle Kirms)

`scripts/sauce_daily.py` scrapes the SAUCE tab, grades picks, renders a screenshot, and sends it to channel `-1003977774560`. Runs daily at **6 AM ET** via cron on the VPS (`run_sauce_daily.sh`).

- **Google Sheet:** `1yozWEoQ5m6rqNC8-E5UGwg0ySjYbAybNHwPmtNTYIzM` (shared with service account)
- **Source data:** Published Google Sheet embedded at kylekirms.com/open-bets (sheet ID `1yjaN85i-WRhRrBcozOG70vTX6cTNpJzFmuNJ8KgL-14`)
- **DB table:** `sauce_picks` in `picks.db`
- **Cron log:** `/tmp/sauce_daily_cron.log`
- **Screenshot rendering:** Uses Playwright (headless Chromium). Requires `fonts-liberation` and `fonts-noto-color-emoji` on VPS for correct font/emoji rendering.

**Manual run on VPS:**
```bash
su - forwarder -c "cd ~/app && ~/venv/bin/python scripts/sauce_daily.py --channel -1003977774560 2>&1"
```

**ESPN sport validation:** `validate_sport()` in `scores.py` verifies Claude's sport classification against ESPN game schedules. Catches ambiguous teams (Rangers, Cardinals, Giants, etc.). Also wired into the core tracker flow in `tracker.py`.

## Twitter/X pick parsing

`scripts/parse_posts_csv.py` parses a capper's tweets CSV (from `fetch_x_posts.py`) to extract official pick placements. Three-phase pipeline:

1. **Text parse** — sends each tweet to Claude to determine if it's an official pick announcement (not commentary, celebration, or reaction)
2. **Image parse** — for posts with pick signals in text but no extractable pick (bet slip in attached image), downloads the image and sends it to Claude
3. **Dedup** — removes duplicate tweet IDs and duplicate picks (same day + normalized teams + same bet_type)

```bash
python scripts/parse_posts_csv.py              # full run
python scripts/parse_posts_csv.py --limit 10   # test on first 10 rows
python scripts/parse_posts_csv.py --skip-images # text-only (cheaper)
```

**Input:** `scripts/output/<Account>_posts.csv` (from `fetch_x_posts.py`)
**Output:** `scripts/output/<Account>_parsed.csv` with structured columns: sport, description, bet_type, teams, player, prop_stat, line, direction, period.

Key design decisions:
- RT filter (`_is_retweet`) skips retweets before hitting the API
- Team name normalization (`_normalize_team`) handles variant spellings for dedup (e.g. "Bosnia" vs "Bosnia and Herzegovina")
- No hardcoded exclude lists — all filtering is via prompt rules and algorithmic dedup so the script works for any capper's account

## Deploy workflow

`syncenv` runs **locally** to push `.env` to the VPS, then deploy on the VPS:

```bash
# Local
syncenv
git push

# On VPS (via SSH)
ssh root@209.38.51.86 'cd /home/forwarder/app && git pull && systemctl restart telegram-forwarder'
```
