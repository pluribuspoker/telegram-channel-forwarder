# Telegram Channel Forwarder

## VPS

- **Reserved IP:** `209.38.51.86` (always use this, not the droplet IP)
- **SSH:** `vps` (PowerShell alias → `ssh root@209.38.51.86`)

### Server aliases (root)

```bash
logs      # prints Ctrl+C hint, then tails journal (Ctrl+C exits logs, service keeps running)
start     # systemctl start telegram-forwarder
stop      # systemctl stop telegram-forwarder
restart   # systemctl restart + logs
status    # systemctl status telegram-forwarder
deploy    # git pull + restart (which includes logs)
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

### Deploy workflow

```powershell
# Local (PowerShell) — commit, push, and deploy to server in one step
git add -A && git commit -m "..."
ship   # pushes to GitHub + runs deploy on server
```

```bash
# Server only (as root)
deploy   # git pull + restart + logs
```
