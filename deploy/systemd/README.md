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
