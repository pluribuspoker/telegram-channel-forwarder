# Telegram Channel Forwarder

## Preferences

- When giving shell commands, chain related steps with `&&` on one line rather than separate lines.
- Use git worktrees for parallel tasks (e.g. a second task while another Claude session is already working). Pattern: `git worktree add ../telegram-forwarder-<slug> -b <branch>`, work there, commit+push the branch, then merge to main from the main repo dir. Name the dir `../telegram-forwarder-<short-slug>`. Clean up with `git worktree remove ../telegram-forwarder-<slug>`.

## VPS

- **Reserved IP:** `209.38.51.86` (always use this, not the droplet IP)
- **SSH:** `ssh root@209.38.51.86`
- **Aliases:** defined in `/root/.server_aliases.sh` — `flogs`, `tlogs`, `logs`, `start`, `stop`, `restart`, `status`, `deploy`, `grade`, `gradetest`

**Never restart or deploy the service yourself.** Rapid bot session restarts trigger Telegram flood waits. Always let the user run `deploy`, `restart`, or `start` manually.

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

## Deploy workflow

`syncenv` runs **locally** to push `.env` to the VPS, then `deploy` runs **on the VPS** to pull code and restart services:

```bash
# Local
syncenv
git push

# On VPS
deploy
```
