# Agent notes

`CLAUDE.md` is the full project guide. Read it before changing anything. The rule below applies to every agent, including ones that don't run Claude Code hooks.

## Environment: the VPS `.env` is the source of truth

- The canonical `.env` is `/home/forwarder/app/.env` on the VPS (`pickbot`, 209.38.51.86). A desktop or clone `.env` is only a copy. Editing that copy never changes production.
- Change the server `.env` only by running these on the VPS, as `forwarder`, in `/home/forwarder/app`:
  - `python3 scripts/set_env_local.py --file .env KEY=VALUE` (`--unset KEY` removes a key and remembers that it was removed; `--stdin KEY` reads a long value from stdin)
  - `python3 scripts/env_mappings.py list | add '<json>' | remove <id>` for `MAPPINGS_CONFIG`
- Any other write is reverted within seconds by `deploy/env_backup.py`: `sed`, `echo >>`, an editor, a Python one-liner, `scp`, or the old `syncenv` push. Only keys that are new to the server survive. **The writer sees success anyway.** Only the operator gets the 🛡️ DM listing what was rejected.
- To refresh a desktop copy, run `python scripts/pull_env.py`, which pulls from the VPS.
- Server-only secrets go in `.env.local` (never synced) via `set_env_local.py`.
