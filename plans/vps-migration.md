# Migration Plan: Local → DigitalOcean VPS

## Critical Ordering Rule
The Reserved IP must exist **before** generating the Telegram session. The session gets permanently tied to that IP.

---

## Phase 1 — DigitalOcean Setup (browser)

- [ ] Create account at digitalocean.com
- [ ] Create Droplet: Ubuntu 24.04 LTS, Basic $6/mo, NYC3 or Richmond VA
- [ ] Set up SSH key during creation
- [ ] Go to **Networking → Reserved IPs**, reserve one and attach to the Droplet immediately — write this IP down, it's yours forever

---

## Phase 2 — Server Setup (terminal)

- [ ] SSH in as root: `ssh root@YOUR_RESERVED_IP`
- [ ] `apt update && apt upgrade -y`
- [ ] Create non-root user `forwarder`, configure UFW firewall (SSH only)
- [ ] Install Python venv tools
- [ ] `git clone` the repo to `/home/forwarder/app`
- [ ] `pip install -r requirements.txt` into `/home/forwarder/venv`

---

## Phase 3 — Secrets & Session (critical)

- [ ] Create `/home/forwarder/app/.env` with all secrets, `chmod 600` it
- [ ] Leave `TELEGRAM_SESSION` as a placeholder for now
- [ ] **Run `get_session.py` FROM the server** — ties the session to the Reserved IP
  - Enter API_ID, API_HASH when prompted
  - Enter phone number + Telegram verification code
  - Copy the output session string
- [ ] Update `.env` with the real `TELEGRAM_SESSION` value

---

## Phase 4 — Service & Monitoring

- [ ] Create `/etc/systemd/system/telegram-forwarder.service`:
  ```ini
  [Unit]
  Description=Telegram Channel Forwarder
  After=network-online.target
  Wants=network-online.target

  [Service]
  Type=simple
  User=forwarder
  WorkingDirectory=/home/forwarder/app
  EnvironmentFile=/home/forwarder/app/.env
  ExecStart=/home/forwarder/venv/bin/python listener.py
  Restart=on-failure
  RestartSec=10
  StandardOutput=journal
  StandardError=journal

  [Install]
  WantedBy=multi-user.target
  ```
- [ ] `systemctl daemon-reload && systemctl enable telegram-forwarder && systemctl start telegram-forwarder`
- [ ] Sign up at healthchecks.io (free), create check: 5min period / 10min grace
- [ ] Add `HEALTHCHECK_URL=https://hc-ping.com/your-id` to `.env`
- [ ] Add `heartbeat()` function to `listener.py` (see code change below)

### Code change: listener.py

Add import at top:
```python
import urllib.request
```

Add function before `main()`:
```python
async def heartbeat():
    """Ping healthchecks.io every 4 minutes to signal the service is alive."""
    url = os.environ.get("HEALTHCHECK_URL")
    if not url:
        return
    while True:
        try:
            urllib.request.urlopen(url, timeout=10)
        except Exception:
            pass
        await asyncio.sleep(240)
```

In `main()`, add `asyncio.create_task(heartbeat())` just before `await client.run_until_disconnected()`.

---

## Phase 5 — Verify

- [ ] `systemctl status telegram-forwarder` shows `active (running)`
- [ ] Send a test message in a source channel, confirm it forwards
- [ ] `sudo reboot`, SSH back in, confirm service auto-started
- [ ] Confirm healthchecks.io turns green after ~5 minutes
- [ ] Stop the local listener (two instances = duplicate forwards)

---

## Ongoing Deploy Workflow

```bash
# On local PC
git add -A && git commit -m "your change" && git push

# On server
cd /home/forwarder/app
git pull
sudo systemctl restart telegram-forwarder
systemctl status telegram-forwarder
```

If new packages added:
```bash
/home/forwarder/venv/bin/pip install -r requirements.txt
sudo systemctl restart telegram-forwarder
```

If MAPPINGS_CONFIG updated:
```bash
nano /home/forwarder/app/.env
sudo systemctl restart telegram-forwarder
```

## Viewing Logs

```bash
journalctl -u telegram-forwarder -f          # live tail
journalctl -u telegram-forwarder -n 100      # last 100 lines
journalctl -u telegram-forwarder --since today
```

## If Session Needs Regenerating

```bash
cd /home/forwarder/app
/home/forwarder/venv/bin/python get_session.py
nano /home/forwarder/app/.env  # update TELEGRAM_SESSION
sudo systemctl restart telegram-forwarder
```
