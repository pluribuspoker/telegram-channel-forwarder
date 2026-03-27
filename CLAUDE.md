# Telegram Channel Forwarder

## VPS

- **Reserved IP:** `209.38.51.86` (always use this, not the droplet IP)
- **Hostname:** `pickbot`
- **SSH:** `vps` (PowerShell alias → `ssh root@209.38.51.86`)

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

### Local PowerShell aliases

```powershell
vps      # ssh root@209.38.51.86
syncenv  # scp local .env to server
ship     # git push + deploy on server
```

### Deploy workflow

```powershell
# Local (PowerShell) — commit, push, and deploy to server in one step
git add -A && git commit -m "..."
ship   # pushes to GitHub + runs deploy on server
```

```bash
# Server only (as root)
deploy   # git pull + restart + status + logs
```

---

## Pick tracker

Runs every 5 minutes via systemd timer (`telegram-tracker.timer`).
Grades sports picks in destination channels by appending ✅/❌ inline after each pick line.
Audit log: `picks.db` (SQLite) + Telegram audit channel (`AUDIT_CHANNEL_ID`).
Parse cache: `parse_cache.json` — avoids re-parsing pending picks on every run.

```bash
journalctl -u telegram-tracker -n 50 --no-pager   # view recent tracker logs
journalctl -u telegram-tracker --since today       # all tracker logs today
journalctl -u telegram-tracker -p err              # only errors
systemctl list-timers telegram-tracker.timer        # check next scheduled run
```

Healthchecks.io receives log output with each ping — last 20 lines on success, last 50 on failure (includes tracebacks).
