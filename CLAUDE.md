# Telegram Channel Forwarder

## Preferences

- When giving shell commands, chain related steps with `&&` on one line rather than separate lines.
- Push to GitHub liberally without asking — rollback is easy and deploy is a separate manual step.
- Use git worktrees for parallel tasks (e.g. a second task while another Claude session is already working). Pattern: `git worktree add ../telegram-forwarder-<slug> -b <branch>`, work there, commit+push the branch, then merge to main from the main repo dir. Name the dir `../telegram-forwarder-<short-slug>`. Clean up with `git worktree remove ../telegram-forwarder-<slug>`.

## VPS

- **Reserved IP:** `209.38.51.86` (always use this, not the droplet IP)
- **SSH:** `ssh root@209.38.51.86`
- **Aliases:** defined in `/root/.server_aliases.sh` — `flogs`, `tlogs`, `logs`, `start`, `stop`, `restart`, `status`, `deploy`, `grade`, `gradetest`

**Never restart or deploy the service yourself.** Rapid bot session restarts trigger Telegram flood waits. Always let the user run `deploy`, `restart`, or `start` manually.

### Switching to test mode

```bash
stop
su - forwarder
cd ~/app
~/venv/bin/python listener.py --test
# Ctrl+C when done
exit
start
```

### Environment files

Two-file split to protect sessions from `syncenv`:

| File | Where | Synced | Contains |
|---|---|---|---|
| `.env` | local + server | ✅ `syncenv` copies this | all config except session strings |
| `.env.local` | local + server (separately) | ❌ never touched | `TELEGRAM_SESSION`, `BOT_SESSION` |

`syncenv` is safe to run freely. `.env.local` is loaded after `.env` in both Python code and systemd, so it always wins.

**Setting up `.env.local` (first time, on each machine):**

Run each script — they authenticate interactively and write the session directly to `.env.local`:
```bash
python scripts/get_session.py      # generates TELEGRAM_SESSION
python scripts/get_bot_session.py  # generates BOT_SESSION
```
Run these **on the VPS** to tie the VPS sessions to `209.38.51.86`. Run **locally** for local dev sessions. Each machine keeps its own `.env.local` with its own session strings.

**After writing `.env.local` on the VPS, fix permissions:**
```bash
chmod 600 /home/forwarder/app/.env.local && chown forwarder:forwarder /home/forwarder/app/.env.local
```

**Updating live systemd services after first deploy of this change:**
```bash
sed -i '/EnvironmentFile=.*\.env$/a EnvironmentFile=-/home/forwarder/app/.env.local' /etc/systemd/system/telegram-forwarder.service
sed -i '/EnvironmentFile=.*\.env$/a EnvironmentFile=-/home/forwarder/app/.env.local' /etc/systemd/system/telegram-tracker.service
systemctl daemon-reload
```

---

## Log colors

Colors and formatting are applied by `_fmtlog` in `/root/.server_aliases.sh` — **do not add ANSI codes to Python print statements**. To update: SSH in and edit that file directly (no service restart needed; re-run the tail alias to pick up changes).

**Current format:** `3/28 5:00PM ET 📋  <message>` — compact M/D timestamp (no seconds), service icon instead of hostname+PID (`📋` tracker, `📡` listener/forwarder, `⚙` systemd dimmed).

**`_fmtlog` regex gotcha:** the message capture must use `\]: (.*)` (single space, not `\]: *(.*)`). The `*` variant strips all leading whitespace from the message, which breaks indentation of parlay continuation rows in the pick table.

---

## Pick tracker

Runs every 5 minutes via systemd timer (`telegram-tracker.timer`).
Grades sports picks in destination channels by appending ✅/❌/♻️ inline after each pick line.
Audit log: `picks.db` (SQLite) + Telegram audit channel (`AUDIT_CHANNEL_ID`). PENDING picks written to DB only, not posted to audit channel.
Parse cache: `parse_cache.json` — avoids re-parsing pending picks on every run.
Summary line: `edit:N pend:N fail:N err:N`.
Healthchecks.io receives log output with each ping — last 20 lines on success, last 50 on failure (includes tracebacks).

**Verdicts:** WIN → ✅, LOSS → ❌, PUSH → ♻️ (draw/no contest/refund). PUSH is not broadcast to the results channel.

**`grade` alias uses `--days 1`** — picks posted more than ~24 hours ago won't be scanned. For older picks, run manually:
```bash
su - forwarder -c "cd ~/app && ~/venv/bin/python tracker.py --live --days 2 2>&1"
```

**UFC draws:** graded as PUSH. UFC picks posted the day *before* the card (common for multi-day message threads) are handled — the future-date scan checks bout-level completion, not just event-level.

## Odds integration

`odds.py` — production module. Fetches pre-game odds via Odds API and ESPN at first tracker encounter (live endpoint, no date param). Odds stored in `parse_cache.json` per pick and in `picks.db` (`grades.odds`). Never re-fetched once set.

**Key env var:** `ODDS_API_KEY` in `.env`
**Sports covered:** NBA, NCAAB, NFL, NCAAF, MLB, NHL, UFC, UFL
**Coverage on recent picks:** ~91% (MLB F5 innings and small UFC cards are structural gaps)

Odds are edited into the destination message as soon as fetched (while still PENDING), then preserved through the grading edit: `Hawks +3.5 [-115]✅`

Tracker-fetched odds use **square brackets** `[-115]` to distinguish them from capper-written odds `(-115)`. Both appear in pick messages and broadcast results.

If the game has already started when a pick is first encountered, odds fetch is skipped silently (`game_in_progress` structural miss — no audit warning).

Any unexpected odds failure posts **one** message to the audit channel — never repeated for the same pick.

**Backtest / audit:**
```bash
python scripts/audit_odds.py --days-back 7   # re-run odds audit for past week
python scripts/audit_odds.py --dry-run       # parse only, no API calls
```

## Broadcast results

After grading, the tracker posts a compact result message to a configured broadcast channel (e.g. the members chat). Configured per-mapping in `MAPPINGS_CONFIG`:

```json
{
  "broadcast_results_channel": -100xxxxxxxxxx,
  "test_broadcast_results_channel": -100xxxxxxxxxx
}
```

- Only WIN and LOSS verdicts broadcast (PENDING/PUSH/UNKNOWN skipped)
- `--dry-run` routes to `test_broadcast_results_channel` for safe previewing
- Descriptions are standardized: no odds, `ML` shorthand, `Team1/Team2 O/U` for game totals, `Team O/U` for team totals, period tags (`1H`, `2H`)
- Capper name is a bold hyperlink back to the original pick message
- Parlay legs grouped under one message; mixed-verdict multi-picks show per-pick emoji
- Odds shown inline if available: `✅ Duke -4.5 [-153] · Capper`

**Testing workflow** (reset emojis and re-run locally):
```bash
python scripts/clear_emojis.py --channel -100xxxxxxxxxx  # strip emojis (today)
python scripts/clear_emojis.py --days 2                  # last 2 days
python tracker.py --live --channel -100xxxxxxxxxx        # re-grade + broadcast
```

## Deploy workflow

`syncenv` runs **locally** to push `.env` to the VPS, then `deploy` runs **on the VPS** to pull code and restart services:

```bash
# Local
syncenv
git push

# On VPS
deploy
```
