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

check_file() {
  local repo_path="$1" remote_path="$2" label="$3"
  if diff <(cat "$repo_path") <(ssh "$VPS" "cat $remote_path" 2>/dev/null) >/dev/null 2>&1; then
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
  check_file "$f" "/home/forwarder/.claude/hooks/$name" "hooks/$name"
done

echo ""
if [ "$DRIFT" -eq 0 ]; then
  echo "All in sync."
else
  echo "DRIFT DETECTED — see above."
  exit 1
fi
