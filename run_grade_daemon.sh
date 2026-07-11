#!/bin/bash
# Grade daemon — persistent process that grades picks every 10s via Bot API.
# No Telethon session = no Telegram flood risk.

APP_DIR="/home/forwarder/app"
PYTHON="/home/forwarder/venv/bin/python"

cd "$APP_DIR"
exec $PYTHON grade_daemon.py
