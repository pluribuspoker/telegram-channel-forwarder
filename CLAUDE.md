# Telegram Channel Forwarder

## VPS

- **Reserved IP:** `209.38.51.86` (always use this, not the droplet IP)
- **Hostname:** `pickbot`
- **SSH:** `ssh root@209.38.51.86`

### Server aliases (root)

```bash
flogs      # tail forwarder logs
tlogs      # tail tracker logs
logs       # tail both interleaved
start      # start forwarder + status
stop       # stop forwarder + status
restart    # restart forwarder + status
status     # forwarder status
deploy     # git pull + restart + forwarder status + last tracker run + tail both

grade      # run pick grader live (last 1 day) — same as the 5-min timer
gradetest  # dry run pick grader (last 2 days) — no edits, shows what would happen
```

Aliases are defined in `/root/.server_aliases.sh` (sourced from `.bashrc`).

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

Two-file split to protect the VPS Telegram session from `syncenv`:

| File | Where | Synced | Contains |
|---|---|---|---|
| `.env` | local + server | ✅ `syncenv` copies this | all config except `TELEGRAM_SESSION` |
| `.env.local` | **server only** | ❌ never touched | `TELEGRAM_SESSION` (VPS session string) |

`syncenv` is safe to run freely. `.env.local` is loaded after `.env` in both Python code and systemd, so it always wins.

**Creating `.env.local` on a new server:**
```bash
echo 'TELEGRAM_SESSION="<run get_session.py on the VPS>"' > /home/forwarder/app/.env.local
chmod 600 /home/forwarder/app/.env.local
chown forwarder:forwarder /home/forwarder/app/.env.local
```
Generate the session string by running `scripts/get_session.py` **on the VPS** (not locally) so Telegram ties the session to `209.38.51.86`.

**Updating live systemd services after first deploy of this change:**
```bash
# Run once on VPS as root
sed -i '/EnvironmentFile=.*\.env$/a EnvironmentFile=-/home/forwarder/app/.env.local' /etc/systemd/system/telegram-forwarder.service
sed -i '/EnvironmentFile=.*\.env$/a EnvironmentFile=-/home/forwarder/app/.env.local' /etc/systemd/system/telegram-tracker.service
systemctl daemon-reload
```

### Local PowerShell aliases

```powershell
vps      # ssh root@209.38.51.86
syncenv  # scp local .env to server (safe — never touches .env.local)
```

### Important

**Never restart or deploy the service yourself.** Rapid bot session restarts trigger Telegram flood waits (rate limiting). Always let the user run `deploy`, `restart`, or `start` manually.

### Deploy workflow

```bash
# Server (as root)
deploy   # git pull + source ~/.bashrc + restart + forwarder status + last tracker run + tail both logs
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

```bash
journalctl -u telegram-tracker -n 50 --no-pager   # view recent tracker logs
journalctl -u telegram-tracker --since today       # all tracker logs today
journalctl -u telegram-tracker -p err              # only errors
systemctl list-timers telegram-tracker.timer        # check next scheduled run
```

Healthchecks.io receives log output with each ping — last 20 lines on success, last 50 on failure (includes tracebacks).
