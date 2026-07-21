#!/bin/bash
# Sauce daily scraper — runs at 6 AM ET via cron
# Scrapes Kyle Kirms open bets, grades past picks, sends rendered image DM

# Without pipefail, `python ... | tee` returns tee's status (always 0), so cron
# recorded a crashed run as a success.
set -o pipefail

APP_DIR="/home/forwarder/app"
PYTHON="/home/forwarder/venv/bin/python"
LOGFILE="/tmp/sauce_daily_last_run.log"

cd "$APP_DIR"
source .env
[ -f .env.local ] && source .env.local

$PYTHON scripts/sauce_daily.py --channel -1003977774560 2>&1 | tee "$LOGFILE"
