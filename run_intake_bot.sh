#!/bin/bash

set -euo pipefail

APP_DIR="/home/forwarder/app"
PYTHON="/home/forwarder/venv/bin/python"

cd "$APP_DIR"
exec "$PYTHON" -u intake_bot.py
