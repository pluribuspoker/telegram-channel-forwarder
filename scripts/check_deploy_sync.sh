#!/bin/bash
# Diff every file under deploy/ against its live VPS location.
# Exits non-zero if any drift is found.
#
# Mappings:
#   deploy/systemd/<f>  ->  /etc/systemd/system/<f>
#   deploy/hooks/<f>    ->  /home/forwarder/.claude/hooks/<f>

set -euo pipefail
VPS="root@209.38.51.86"
REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
DRIFT=0

# Dual-mode: from a dev machine we read the live files over SSH; but when this
# runs ON the VPS itself (hostname pickbot) SSH-to-self returns nothing and
# every file falsely reads as DRIFT — so read the live files directly instead.
if [ "$(hostname)" = "pickbot" ]; then
  ON_VPS=1
  echo "(running on the VPS — comparing against local live files, no SSH)"
else
  ON_VPS=0
  echo "(running remotely — comparing against $VPS over SSH)"
fi

read_live() {
  local path="$1"
  if [ "$ON_VPS" = "1" ]; then
    cat "$path" 2>/dev/null
  else
    ssh "$VPS" "cat $path" 2>/dev/null
  fi
}

check_file() {
  local repo_path="$1" remote_path="$2" label="$3"
  if diff <(cat "$repo_path") <(read_live "$remote_path") >/dev/null 2>&1; then
    echo "  OK   $label"
  else
    echo "  DRIFT $label"
    DRIFT=1
  fi
}

echo "=== deploy/systemd ==="
for f in "$REPO_ROOT"/deploy/systemd/*; do
  name=$(basename "$f")
  [ "$name" = "README.md" ] && continue
  check_file "$f" "/etc/systemd/system/$name" "systemd/$name"
done

echo "=== deploy/hooks ==="
for f in "$REPO_ROOT"/deploy/hooks/*; do
  name=$(basename "$f")
  [ "$name" = "README.md" ] && continue
  # Windows-only hooks aren't deployed on the Linux VPS — skip so they don't
  # read as phantom drift.
  case "$name" in *.win.sh) continue ;; esac
  check_file "$f" "/home/forwarder/.claude/hooks/$name" "hooks/$name"
done

echo ""
if [ "$DRIFT" -eq 0 ]; then
  echo "All in sync."
else
  echo "DRIFT DETECTED — see above."
  exit 1
fi
