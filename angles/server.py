#!/usr/bin/env python3
"""Angle Analyzer dashboard server.

Serves the static dashboard behind Telegram-based authentication and provides
a POST /api/refresh endpoint that streams real-time progress via SSE.

Uses only Python stdlib — zero additional dependencies.

Env vars:
    ANGLES_PORT            Port to listen on (default 8080)
    ANGLES_AUTH_SECRET     HMAC secret for signing auth tokens/cookies (required)
    ANGLES_REFRESH_USERS   Comma-separated Telegram user IDs allowed to refresh
                           (empty = all authenticated users can refresh)
    ANGLES_ADMIN_IDS       Comma-separated Telegram user IDs that can access /activity
                           (empty = all authenticated users can access)
    BOT_TOKEN              Bot API token for resolving user display names (optional)
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

# auth module lives in the same directory
sys.path.insert(0, str(Path(__file__).resolve().parent))
from auth import (  # noqa: E402
    COOKIE_NAME,
    SESSION_TTL,
    get_secret,
    make_session_cookie,
    parse_cookie_header,
    set_cookie_header,
    verify_session_cookie,
    verify_token,
)
from activity import init_db as init_activity_db, log_visit, get_activity  # noqa: E402

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

# Telegram user IDs allowed to trigger refresh
_REFRESH_ALLOWED_IDS: set[int] = set()
_raw = os.environ.get("ANGLES_REFRESH_USERS", "")
if _raw:
    _REFRESH_ALLOWED_IDS = {int(x.strip()) for x in _raw.split(",") if x.strip()}

# Admin user IDs (can access /activity)
_ADMIN_IDS: set[int] = set()
_raw_admin = os.environ.get("ANGLES_ADMIN_IDS", "")
if _raw_admin:
    _ADMIN_IDS = {int(x.strip()) for x in _raw_admin.split(",") if x.strip()}

# Paths that bypass authentication
_PUBLIC_PATHS = frozenset(("/login", "/auth", "/logout"))

# Only log visits for these paths (main page loads, not assets)
_TRACKED_PATHS = frozenset(("/", "/index.html"))


class Handler(http.server.SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(BASE_DIR), **kwargs)

    def do_GET(self):
        path = self.path.split("?")[0].split("#")[0]
        name = path.rstrip("/").rsplit("/", 1)[-1]

        # Block sensitive files
        if name.startswith(".") or Path(name).suffix.lower() in BLOCKED_EXTENSIONS:
            self.send_error(403)
            return

        # Public routes (no auth required)
        if path == "/login":
            return self._serve_file("login.html")
        if path == "/auth":
            return self._handle_auth()
        if path == "/logout":
            return self._handle_logout()

        # Everything else requires a valid session
        if not self._check_session():
            return self._redirect("/login")

        user_id = self._get_session_user_id()

        if self.path.rstrip("/") == "/api/me":
            can_refresh = not _REFRESH_ALLOWED_IDS or user_id in _REFRESH_ALLOWED_IDS
            is_admin = not _ADMIN_IDS or user_id in _ADMIN_IDS
            return self._json(200, {"can_refresh": can_refresh, "is_admin": is_admin})

        # Activity dashboard (admin-only)
        if path == "/activity":
            if _ADMIN_IDS and user_id not in _ADMIN_IDS:
                self.send_error(403)
                return
            return self._serve_file("activity.html")

        if path.startswith("/api/activity"):
            if _ADMIN_IDS and user_id not in _ADMIN_IDS:
                return self._json(403, {"error": "Forbidden"})
            return self._handle_activity()

        # Log page view (server-side only, no client overhead)
        if path in _TRACKED_PATHS:
            ip = self.headers.get("X-Forwarded-For", "").split(",")[0].strip() or self.client_address[0]
            ua = self.headers.get("User-Agent", "")
            threading.Thread(
                target=log_visit, args=(user_id, path, ip, ua), daemon=True
            ).start()

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

    # ── authentication ────────────────────────────────────────────────

    def _check_session(self) -> bool:
        """Return True if the request carries a valid session cookie."""
        return self._get_session_user_id() is not None

    def _get_session_user_id(self) -> int | None:
        """Return the Telegram user ID from the session cookie, or None."""
        cookie_header = self.headers.get("Cookie", "")
        token = parse_cookie_header(cookie_header, COOKIE_NAME)
        if not token:
            return None
        return verify_session_cookie(token, get_secret())

    def _handle_auth(self):
        """Validate a magic-link token and set a session cookie."""
        qs = self.path.split("?", 1)[1] if "?" in self.path else ""
        params = dict(p.split("=", 1) for p in qs.split("&") if "=" in p)
        token = params.get("token", "")

        secret = get_secret()
        user_id = verify_token(token, secret)
        if user_id is None:
            self.send_response(302)
            self.send_header("Location", "/login?error=expired")
            self.end_headers()
            return

        cookie = make_session_cookie(user_id, secret)
        self.send_response(302)
        self.send_header("Set-Cookie", set_cookie_header(COOKIE_NAME, cookie, SESSION_TTL))
        self.send_header("Location", "/")
        self.end_headers()

    def _handle_logout(self):
        """Clear the session cookie and redirect to /login."""
        self.send_response(302)
        self.send_header("Set-Cookie", set_cookie_header(COOKIE_NAME, "", 0))
        self.send_header("Location", "/login")
        self.end_headers()

    # ── helpers ───────────────────────────────────────────────────────

    def _redirect(self, location):
        self.send_response(302)
        self.send_header("Location", location)
        self.end_headers()

    def _serve_file(self, filename):
        """Serve a specific file from BASE_DIR (bypasses auth)."""
        filepath = BASE_DIR / filename
        if not filepath.is_file():
            self.send_error(404)
            return
        content = filepath.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(content)))
        self.end_headers()
        self.wfile.write(content)

    # ── activity endpoint ─────────────────────────────────────────────

    def _handle_activity(self):
        """Return activity JSON for the admin dashboard."""
        qs = self.path.split("?", 1)[1] if "?" in self.path else ""
        params = dict(p.split("=", 1) for p in qs.split("&") if "=" in p)
        days = min(int(params.get("days", "30")), 365)
        data = get_activity(days)
        return self._json(200, data)

    # ── refresh endpoint (SSE) ────────────────────────────────────────

    def _handle_refresh(self):
        global _last_refresh

        user_id = self._get_session_user_id()
        if not user_id:
            return self._json(401, {"error": "Unauthorized"})

        if _REFRESH_ALLOWED_IDS and user_id not in _REFRESH_ALLOWED_IDS:
            return self._json(403, {"error": "You don't have permission to refresh data"})

        now = time.time()
        if now - _last_refresh < COOLDOWN_SECS:
            wait = int(COOLDOWN_SECS - (now - _last_refresh))
            return self._json(429, {"error": f"Cooldown \u2014 retry in {wait}s"})

        if not _refresh_lock.acquire(blocking=False):
            return self._json(429, {"error": "Refresh already in progress"})

        _last_refresh = now

        # Start SSE stream
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("X-Accel-Buffering", "no")
        self.end_headers()

        try:
            log.info("Starting data refresh (SSE)")
            proc = subprocess.Popen(
                [sys.executable, str(EXTRACT_SCRIPT)],
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                bufsize=1,
                cwd=str(PROJECT_ROOT),
            )

            while True:
                line = proc.stdout.readline()
                if not line:
                    break
                line = line.strip()
                if line.startswith("PROGRESS:"):
                    self._sse(line[9:])

            proc.wait(timeout=300)
            if proc.returncode != 0:
                self._sse(json.dumps({"stage": "error",
                                      "error": "Extraction failed"}))
        except subprocess.TimeoutExpired:
            proc.kill()
            self._sse(json.dumps({"stage": "error",
                                  "error": "Timed out (5 min limit)"}))
        except (BrokenPipeError, ConnectionResetError):
            log.info("Client disconnected during refresh")
            proc.kill()
        except Exception as exc:
            log.exception("Refresh error")
            try:
                self._sse(json.dumps({"stage": "error", "error": str(exc)}))
            except (BrokenPipeError, ConnectionResetError):
                pass
        finally:
            _refresh_lock.release()

    def _sse(self, data):
        """Send one SSE event. Lets BrokenPipeError propagate."""
        self.wfile.write(f"data: {data}\n\n".encode())
        self.wfile.flush()

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
    if not os.environ.get("ANGLES_AUTH_SECRET"):
        sys.exit("ANGLES_AUTH_SECRET is required")

    init_activity_db()

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
