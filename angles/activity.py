"""Activity tracking for the Angle Analyzer dashboard.

Logs page views server-side (zero client overhead) in a separate SQLite DB.
Resolves Telegram user IDs to display names via Bot API (cached).
"""

import json
import sqlite3
import time
import urllib.request
import urllib.error
import os
from pathlib import Path

DB_PATH = Path(__file__).resolve().parent / "data" / "activity.db"


def _get_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(str(DB_PATH), timeout=5)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=3000")
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    """Create tables if they don't exist."""
    conn = _get_conn()
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS visits (
            id INTEGER PRIMARY KEY,
            user_id INTEGER NOT NULL,
            ts REAL NOT NULL,
            path TEXT NOT NULL,
            ip TEXT,
            user_agent TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_visits_ts ON visits(ts);
        CREATE INDEX IF NOT EXISTS idx_visits_user ON visits(user_id);

        CREATE TABLE IF NOT EXISTS users (
            user_id INTEGER PRIMARY KEY,
            first_name TEXT,
            username TEXT,
            updated_at REAL
        );
    """)
    conn.close()


def log_visit(user_id: int, path: str, ip: str | None = None,
              user_agent: str | None = None):
    """Record a page view. Called server-side on authenticated requests."""
    conn = _get_conn()
    conn.execute(
        "INSERT INTO visits (user_id, ts, path, ip, user_agent) VALUES (?, ?, ?, ?, ?)",
        (user_id, time.time(), path, ip, user_agent),
    )
    conn.commit()
    conn.close()


def _resolve_user(user_id: int, bot_token: str) -> dict | None:
    """Fetch user info from Bot API. Returns {first_name, username} or None."""
    url = f"https://api.telegram.org/bot{bot_token}/getChat?chat_id={user_id}"
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "AnglesDashboard/1.0"})
        with urllib.request.urlopen(req, timeout=5) as resp:
            data = json.loads(resp.read())
            if data.get("ok"):
                r = data["result"]
                return {
                    "first_name": r.get("first_name", ""),
                    "username": r.get("username", ""),
                }
    except (urllib.error.URLError, json.JSONDecodeError, KeyError, OSError):
        pass
    return None


def _ensure_user_cached(user_id: int, conn: sqlite3.Connection):
    """Resolve and cache a user if not already cached (or stale >7 days)."""
    row = conn.execute("SELECT updated_at FROM users WHERE user_id = ?", (user_id,)).fetchone()
    if row and (time.time() - row["updated_at"]) < 7 * 86400:
        return  # fresh enough

    bot_token = os.environ.get("BOT_TOKEN", "")
    if not bot_token:
        return

    info = _resolve_user(user_id, bot_token)
    if info:
        conn.execute(
            "INSERT OR REPLACE INTO users (user_id, first_name, username, updated_at) "
            "VALUES (?, ?, ?, ?)",
            (user_id, info["first_name"], info["username"], time.time()),
        )
        conn.commit()


def get_activity(days: int = 30) -> dict:
    """Return activity data for the admin dashboard."""
    conn = _get_conn()
    cutoff = time.time() - days * 86400

    # Get all visits in range
    visits = conn.execute(
        "SELECT user_id, ts, path, ip, user_agent FROM visits "
        "WHERE ts > ? ORDER BY ts DESC",
        (cutoff,),
    ).fetchall()

    # Unique user IDs
    user_ids = list({v["user_id"] for v in visits})

    # Resolve usernames (lazy, cached)
    for uid in user_ids:
        _ensure_user_cached(uid, conn)

    # Build user map
    user_map = {}
    for uid in user_ids:
        row = conn.execute(
            "SELECT first_name, username FROM users WHERE user_id = ?", (uid,)
        ).fetchone()
        if row and (row["first_name"] or row["username"]):
            display = row["first_name"] or ""
            if row["username"]:
                display = f"{display} (@{row['username']})".strip()
            user_map[uid] = display
        else:
            user_map[uid] = str(uid)

    conn.close()

    # Build response
    recent_visits = []
    for v in visits[:200]:
        recent_visits.append({
            "user_id": v["user_id"],
            "display_name": user_map.get(v["user_id"], str(v["user_id"])),
            "ts": v["ts"],
            "path": v["path"],
        })

    # Daily unique visitors (for chart)
    daily = {}
    for v in visits:
        day = time.strftime("%Y-%m-%d", time.gmtime(v["ts"]))
        if day not in daily:
            daily[day] = set()
        daily[day].add(v["user_id"])
    daily_uniques = [{"date": d, "count": len(uids)} for d, uids in sorted(daily.items())]

    # Per-user summary
    user_summary = {}
    for v in visits:
        uid = v["user_id"]
        if uid not in user_summary:
            user_summary[uid] = {"count": 0, "last_seen": 0}
        user_summary[uid]["count"] += 1
        user_summary[uid]["last_seen"] = max(user_summary[uid]["last_seen"], v["ts"])

    user_list = []
    for uid, stats in sorted(user_summary.items(), key=lambda x: x[1]["last_seen"], reverse=True):
        user_list.append({
            "user_id": uid,
            "display_name": user_map.get(uid, str(uid)),
            "visits": stats["count"],
            "last_seen": stats["last_seen"],
        })

    return {
        "total_visits": len(visits),
        "unique_visitors": len(user_ids),
        "recent": recent_visits,
        "daily_uniques": daily_uniques,
        "users": user_list,
        "days": days,
    }
