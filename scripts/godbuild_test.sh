#!/usr/bin/env bash
# VPS scratch-clone test protocol for the God Expert roadmap (docs/god-expert-roadmap.md).
#
#   bash scripts/godbuild_test.sh <slug> <worktree-dir> [extra unittest modules...]
#
# 1. From <worktree-dir>, collects every file that differs from origin/main
#    (committed on the branch or uncommitted) plus untracked files, into a tar.
# 2. On the VPS: fresh `git clone /home/forwarder/app /tmp/godbuild-<slug>` as
#    forwarder, overlays the tar, strips CRs from the overlaid files, chowns.
# 3. Runs the eleven-module God Expert suite plus any extra modules, prints the output,
#    removes the clone. Exit status is unittest's.
#
# Never touches ~/app, the sheet, or the network beyond ssh/scp.
set -euo pipefail

SLUG="${1:?slug}"
WORKTREE="${2:?worktree dir}"
shift 2
EXTRA_MODULES="$*"
VPS="root@209.38.51.86"
CLONE="/tmp/godbuild-${SLUG}"
OVERLAY="/tmp/overlay-${SLUG}.tgz"
SUITE="scripts.test_moe_god scripts.test_moe scripts.test_moe_ak scripts.test_moe_win_total scripts.test_generate_moe_opinion_cli scripts.test_intake_bot scripts.test_god_judge_runner scripts.test_nfl_lines_history scripts.test_moe_margins scripts.test_moe_rating scripts.test_moe_backtest"

cd "$WORKTREE"
git fetch -q origin
LIST="$(mktemp)"
{
  git diff --name-only --diff-filter=ACMR origin/main -- .
  git ls-files --others --exclude-standard
} | sort -u | grep -v '^$' > "$LIST" || true
# Deleted files cannot be overlaid; report them so the operator knows.
DELETED="$(git diff --name-only --diff-filter=D origin/main -- . || true)"
if [ -n "$DELETED" ]; then
  echo "WARNING: deleted files are not reflected in the scratch clone:" >&2
  echo "$DELETED" >&2
fi
COUNT="$(wc -l < "$LIST" | tr -d ' ')"
echo "overlay: ${COUNT} file(s) differ from origin/main"
cat "$LIST"
LOCAL_TAR="$(mktemp --suffix=.tgz)"
if [ "$COUNT" -gt 0 ]; then
  tar czf "$LOCAL_TAR" -T "$LIST"
else
  tar czf "$LOCAL_TAR" -T /dev/null
fi
scp -q "$LOCAL_TAR" "${VPS}:${OVERLAY}"
# The overlaid file list rides along so only those files get their CRs stripped.
scp -q "$LIST" "${VPS}:${OVERLAY}.list"
rm -f "$LOCAL_TAR" "$LIST"

# printf %q keeps the space-separated module lists as single remote arguments.
ssh "$VPS" bash -s -- $(printf '%q ' "$SLUG" "$CLONE" "$OVERLAY" "$EXTRA_MODULES" "$SUITE") <<'REMOTE'
set -uo pipefail
SLUG="$1"; CLONE="$2"; OVERLAY="$3"; EXTRA="$4"; SUITE="$5"
rm -rf "$CLONE"
su - forwarder -c "git clone -q /home/forwarder/app $CLONE" || { echo "clone failed"; exit 2; }
echo "clone at $(su - forwarder -c "cd $CLONE && git log --oneline -1")"
tar xzf "$OVERLAY" -C "$CLONE"
if [ -s "${OVERLAY}.list" ]; then
  while IFS= read -r f; do
    [ -f "$CLONE/$f" ] && sed -i 's/\r$//' "$CLONE/$f"
  done < "${OVERLAY}.list"
fi
chown -R forwarder:forwarder "$CLONE"
rm -f "$OVERLAY" "${OVERLAY}.list"
echo "=== unittest: $SUITE $EXTRA"
su - forwarder -c "cd $CLONE && ~/venv/bin/python -m unittest $SUITE $EXTRA 2>&1"
STATUS=$?
rm -rf "$CLONE"
echo "=== exit $STATUS (clone removed)"
exit $STATUS
REMOTE
