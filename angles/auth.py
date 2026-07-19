"""Stateless HMAC-based auth for the Angle Analyzer dashboard.

Tokens are base64url-encoded strings: ``user_id:expiry_unix:hmac_hex``.
Magic-link tokens are short-lived (5 min); session cookies are long-lived
(30 days).  Everything is stdlib-only — no external dependencies.
"""

import base64
import hashlib
import hmac
import os
import time

COOKIE_NAME = "aa_session"
MAGIC_LINK_TTL = 300          # 5 minutes
SESSION_TTL = 30 * 24 * 3600  # 30 days


def get_secret() -> str:
    """Read ANGLES_AUTH_SECRET from the environment."""
    secret = os.environ.get("ANGLES_AUTH_SECRET", "")
    if not secret:
        raise RuntimeError("ANGLES_AUTH_SECRET is not set")
    return secret


def make_token(user_id: int, ttl: int, secret: str) -> str:
    """Create a base64url-encoded HMAC-signed token valid for *ttl* seconds."""
    expiry = int(time.time()) + ttl
    payload = f"{user_id}:{expiry}"
    sig = hmac.new(secret.encode(), payload.encode(), hashlib.sha256).hexdigest()
    raw = f"{payload}:{sig}"
    return base64.urlsafe_b64encode(raw.encode()).rstrip(b"=").decode()


def verify_token(token: str, secret: str) -> int | None:
    """Verify a token.  Returns *user_id* on success, ``None`` on failure."""
    try:
        # Re-pad base64
        padded = token + "=" * (-len(token) % 4)
        raw = base64.urlsafe_b64decode(padded).decode()
        user_id_s, expiry_s, sig = raw.split(":", 2)
        payload = f"{user_id_s}:{expiry_s}"
        expected = hmac.new(secret.encode(), payload.encode(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(sig, expected):
            return None
        if int(expiry_s) < time.time():
            return None
        return int(user_id_s)
    except Exception:
        return None


def make_session_cookie(user_id: int, secret: str) -> str:
    """Create a session token with 30-day TTL."""
    return make_token(user_id, SESSION_TTL, secret)


def verify_session_cookie(cookie_value: str, secret: str) -> int | None:
    """Verify a session cookie.  Returns *user_id* or ``None``."""
    return verify_token(cookie_value, secret)


def parse_cookie_header(cookie_header: str, name: str) -> str | None:
    """Extract a named cookie value from a ``Cookie`` header string."""
    for part in cookie_header.split(";"):
        part = part.strip()
        if part.startswith(f"{name}="):
            return part[len(name) + 1:]
    return None


def set_cookie_header(name: str, value: str, max_age: int, path: str = "/") -> str:
    """Build a ``Set-Cookie`` header value."""
    return (
        f"{name}={value}; Path={path}; Max-Age={max_age}; "
        "HttpOnly; Secure; SameSite=Lax"
    )
