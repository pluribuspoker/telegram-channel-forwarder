#!/bin/bash
# One-time server setup for the pick tracker.
# Run as root on the VPS after deploying the code.
set -e

APP=/home/forwarder/app
VENV=/home/forwarder/venv

echo "=== 1. Make wrapper executable ==="
chmod +x $APP/run_tracker.sh

echo "=== 2. Systemd service ==="
cat > /etc/systemd/system/telegram-tracker.service << 'EOF'
[Unit]
Description=Telegram Pick Grader
After=network.target

[Service]
Type=oneshot
User=forwarder
WorkingDirectory=/home/forwarder/app
EnvironmentFile=/home/forwarder/app/.env
EnvironmentFile=-/home/forwarder/app/.env.local
ExecStart=/home/forwarder/app/run_tracker.sh
StandardOutput=journal
StandardError=journal
EOF

echo "=== 3. Systemd timer (every 5 minutes) ==="
cat > /etc/systemd/system/telegram-tracker.timer << 'EOF'
[Unit]
Description=Run Telegram Pick Grader every 5 minutes

[Timer]
OnCalendar=*:0/5
Persistent=true

[Install]
WantedBy=timers.target
EOF

echo "=== 4. Server aliases ==="
grep -q 'alias grade=' /root/.bash_aliases 2>/dev/null || cat >> /root/.bash_aliases << 'EOF'
alias grade='su - forwarder -c "cd ~/app && ~/venv/bin/python tracker.py --live --days 2 2>&1"'
alias gradetest='su - forwarder -c "cd ~/app && ~/venv/bin/python tracker.py --live --dry-run --days 2 2>&1"'
EOF
source /root/.bash_aliases

echo "=== 5. Enable timer ==="
systemctl daemon-reload
systemctl enable --now telegram-tracker.timer

echo "=== 6. Timer status ==="
systemctl list-timers telegram-tracker.timer

echo ""
echo "Setup complete. Run the backfill with:"
echo "  su - forwarder -c 'cd ~/app && ~/venv/bin/python tracker.py --live --days 365 2>&1'"
