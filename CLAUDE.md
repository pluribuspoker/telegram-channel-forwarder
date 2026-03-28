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

Colors are applied by `_fmtlog` in `/root/.server_aliases.sh` — **do not add ANSI codes to Python print statements**. To update: SSH in and edit that file directly (no service restart needed; re-run the tail alias to pick up changes).

---

## Pick tracker

Runs every 5 minutes via systemd timer (`telegram-tracker.timer`).
Grades sports picks in destination channels by appending ✅/❌ inline after each pick line.
Audit log: `picks.db` (SQLite) + Telegram audit channel (`AUDIT_CHANNEL_ID`). PENDING picks written to DB only, not posted to audit channel.
Parse cache: `parse_cache.json` — avoids re-parsing pending picks on every run.
Summary line: `edited / pending / failed / errors`.
Healthchecks.io receives log output with each ping — last 20 lines on success, last 50 on failure (includes tracebacks).

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
