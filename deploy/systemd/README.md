# systemd units

Version-controlled copies of the systemd units that run this app on the VPS.
These are the source of truth — edit them here, then sync to the VPS.

The live copies live in `/etc/systemd/system/`. This folder exists so the unit
config (e.g. `grade-daemon.service`'s `WatchdogSec`) survives a VPS rebuild and
is reviewable in git.

| Unit | Purpose |
|---|---|
| `telegram-forwarder.service` | Listener (persistent). Forwards channel messages. |
| `telegram-tracker.service` + `.timer` | Pick grader, every 5 min (timer-triggered). |
| `grade-daemon.service` | Grade daemon (persistent). Grades + broadcasts every 10s. Hang-hardened via `WatchdogSec`. |
| `trent-monitor.service` + `.timer` | @BookitWithTrent poller, every 15 min. |
| `god-judge.service` + `.timer` | God Expert judge runner (`scripts/god_judge_runner.py` via `run_god_judge.sh`), every 30 min at :12/:42. `GOD_JUDGE_SAMPLES` headless `claude -p` calls per game with a complete committee (default 1; 2–5 is the judge ensemble: every sampled response persists as an audit row with `generation_status=sample`, one mean judge row reaches review), single attempt per call, `TimeoutStartSec=9000` (3 games × 3 samples × 900 s). Also loads `~/.claude/auth.env` for the subscription token. |
| `moe-grade.service` + `.timer` | MOE opinion grader (`scripts/moe_grade.py --write --notify` via `run_moe_grade.sh`), daily at 05:23 ET. Deterministic (no Claude call, no Telethon), idempotent ledger append, DMs only when rows were appended, `TimeoutStartSec=1200`. |
| `moe-sqlite-backup.service` + `.timer` | Daily online backup of the authoritative MOE SQLite store, with quick/integrity checks and 14-copy retention. |

None of these contain secrets — they load config via `EnvironmentFile=`
(`.env` + `.env.local`), which are not in git.

## Sync a changed unit to the VPS

```bash
sudo cp deploy/systemd/<unit> /etc/systemd/system/<unit> && \
  sudo systemctl daemon-reload && \
  sudo systemctl restart <unit>
```

(As the `forwarder` user, prefix `sudo` with `sudo -n`.)

## Verify the grade-daemon watchdog is being fed

```bash
systemctl show grade-daemon.service -p WatchdogUSec -p WatchdogTimestamp
```

`WatchdogTimestamp` should advance every ~10s. If it goes static, the daemon
loop has stopped turning and systemd will restart it after `WatchdogSec`.
