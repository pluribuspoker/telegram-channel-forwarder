#!/bin/bash
set -euo pipefail

cd /home/forwarder/app
exec /home/forwarder/venv/bin/python \
    scripts/fetch_nfl_lines.py --scheduled --write
