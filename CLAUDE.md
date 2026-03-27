# Telegram Channel Forwarder

## VPS

- **Reserved IP:** `209.38.51.86` (always use this, not the droplet IP)
- **SSH:** `vps` (PowerShell alias → `ssh root@209.38.51.86`)

### Server aliases (root)

```bash
logs       # prints Ctrl+C hint, then tails journal (Ctrl+C exits logs, service keeps running)
start      # systemctl start telegram-forwarder
stop       # systemctl stop telegram-forwarder
restart    # systemctl restart telegram-forwarder + status
status     # systemctl status telegram-forwarder
deploy     # git pull + restart + status + logs

grade      # run pick grader live (last 1 day) — same as the 5-min timer
gradetest  # dry run pick grader (last 2 days) — no edits, shows what would happen
```

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
journalctl -u telegram-tracker -n 50 --no-pager  # view tracker run logs
systemctl list-timers telegram-tracker.timer       # check next scheduled run
```
