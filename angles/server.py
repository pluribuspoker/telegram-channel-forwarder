#!/usr/bin/env python3
"""Angle Analyzer dashboard server.

Serves the static dashboard and provides a POST /api/refresh endpoint
to re-extract angle data from Telegram.

Uses only Python stdlib — zero additional dependencies.

Env vars:
    ANGLES_PORT            Port to listen on (default 8080)
    ANGLES_REFRESH_TOKEN   Bearer token for the refresh endpoint (required)
"""

import http.server
import json
import logging
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

log = logging.getLogger("angles")

BASE_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = BASE_DIR.parent
EXTRACT_SCRIPT = BASE_DIR / "extract_angles.py"

BLOCKED_EXTENSIONS = frozenset(
    (".py", ".pyc", ".pyo", ".env", ".db", ".sqlite", ".session")
)

_refresh_lock = threading.Lock()
_last_refresh = 0.0
COOLDOWN_SECS = 60


class Handler(http.server.SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(BASE_DIR), **kwargs)

    def do_GET(self):
        path = self.path.split("?")[0].split("#")[0]
        name = path.rstrip("/").rsplit("/", 1)[-1]
        if name.startswith(".") or Path(name).suffix.lower() in BLOCKED_EXTENSIONS:
            self.send_error(403)
            return
        super().do_GET()

    def do_POST(self):
        if self.path.rstrip("/") == "/api/refresh":
            self._handle_refresh()
        else:
            self.send_error(404)

    def list_directory(self, path):
        """Disable directory listing."""
        self.send_error(403)
        return None

    # ── refresh endpoint ──────────────────────────────────────────────

    def _handle_refresh(self):
        global _last_refresh
        token = os.environ.get("ANGLES_REFRESH_TOKEN", "")
        auth = self.headers.get("Authorization", "")
        if not token or auth != f"Bearer {token}":
            return self._json(401, {"error": "Unauthorized"})

        now = time.time()
        if now - _last_refresh < COOLDOWN_SECS:
            wait = int(COOLDOWN_SECS - (now - _last_refresh))
            return self._json(429, {"error": f"Cooldown — retry in {wait}s"})

        if not _refresh_lock.acquire(blocking=False):
            return self._json(429, {"error": "Refresh already in progress"})

        try:
            _last_refresh = now
            log.info("Starting data refresh")
            proc = subprocess.run(
                [sys.executable, str(EXTRACT_SCRIPT)],
                capture_output=True,
                text=True,
                timeout=300,
                cwd=str(PROJECT_ROOT),
            )
            if proc.returncode == 0:
                log.info("Refresh complete")
                return self._json(200, {"status": "ok"})
            else:
                log.error("Refresh failed: %s", proc.stderr[-500:])
                return self._json(500, {"error": "Extraction failed"})
        except subprocess.TimeoutExpired:
            return self._json(504, {"error": "Timed out (5 min limit)"})
        except Exception as exc:
            log.exception("Refresh error")
            return self._json(500, {"error": str(exc)})
        finally:
            _refresh_lock.release()

    def _json(self, code, data):
        body = json.dumps(data).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, fmt, *args):
        log.info(fmt, *args)


def main():
    if not os.environ.get("ANGLES_REFRESH_TOKEN"):
        sys.exit("ANGLES_REFRESH_TOKEN is required")

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        datefmt="%H:%M:%S",
    )

    port = int(os.environ.get("ANGLES_PORT", 8080))
    server = http.server.ThreadingHTTPServer(("0.0.0.0", port), Handler)
    log.info("Serving on http://0.0.0.0:%d", port)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        log.info("Shutting down")
        server.shutdown()


if __name__ == "__main__":
    main()
