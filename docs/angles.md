# Angle Analyzer

> Full reference moved out of CLAUDE.md (terse rules live there). This file is read on demand — keep the complete detail and incident history HERE, not in CLAUDE.md.

## Angle Analyzer

`angles/extract_angles.py` scrapes channel `-1002486251914` for picks with blockquoted angle records, parses them into structured data (type, sport, bet type, side, day, unit, time window, off-count, undefeated/winless), enriches with grades from `picks.db`, and outputs `angles/data/angles.json`. Picks without angles get a `no_angle` type for baseline comparison.

`angles/index.html` is a single-file dashboard (Tailwind + Chart.js) that loads the JSON. Features: multi-filter bar (pick-level and angle-level), KPIs, cumulative profit chart, Quick Breakdown pivot table (group by any dimension), searchable/sortable picks log with parsed angle display, CSV export.

**Hosted at:** `https://fightclubpicks.cc` — served by `angles/server.py` (Python stdlib, ~10MB RAM) behind Cloudflare (HTTPS, DDoS protection). Domain: Cloudflare Registrar, A record → `209.38.51.86` proxied.

**Authentication:** Access is gated behind Telegram membership in the Fight Club channel (`-1002486251914`). Users send `/access` to `@forwarder_fc_bot` (or click the deep link on the login page at `/login`). The bot checks membership via Bot API `getChatMember` and replies with a magic link (`/auth?token=...`) valid for 5 minutes. Clicking the link sets an HMAC-signed session cookie (`aa_session`) lasting 30 days. Auth module: `angles/auth.py` (stdlib only, stateless HMAC-SHA256). The `/access` handler lives in `listener.py` on the bot client.

**One-click refresh:** The dashboard has a "Refresh Data" button that streams real-time progress via SSE. Uses the session cookie for auth (no separate API key).

**Systemd:** `angles-dashboard.service` — persistent, port 80, runs as forwarder user.

**Activity dashboard:** Admin-only route at `/activity` tracks page views, unique visitors, and who visited (with Telegram display names). Logging is purely server-side (zero client-side network calls). Data stored in `angles/data/activity.db` (separate from picks.db). Username resolution via Bot API, cached 7 days.

**Env vars:**
- `ANGLES_AUTH_SECRET` — HMAC key for signing auth tokens/cookies (required)
- `ANGLES_PORT` — listen port (default 80)
- `ANGLES_ADMIN_IDS` — comma-separated Telegram user IDs that can access `/activity` (empty = all authenticated users)
- `BOT_TOKEN` — used to resolve Telegram user IDs to display names on the activity dashboard (optional, falls back to numeric IDs)

**Manual data pull (on VPS):**
```bash
su - forwarder -c "cd ~/app && ~/venv/bin/python angles/extract_angles.py"
```

Angle types: `run`, `off_losses`, `off_wins`, `sport_record`, `bet_type_record`, `side_record`, `day_record`, `time_scoped`, `unit_record`, `no_angle`. Prose lines with records buried in sentences are auto-skipped. Context headers (e.g. "L30 days:", "This month:") propagate scope to subsequent bare-record lines. Parenthetical sub-records inherit sport/bet_type/side from parent context.

