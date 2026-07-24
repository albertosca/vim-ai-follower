#!/bin/zsh
# QA Check 2 — `claude-follow stop` restores the origin pane's border.
# Consumes the follower state Check 1 left running: it does not start
# anything, it stops what's already there and gives Alberto something to
# watch (the pane closing, the border resetting).
set -e
HERE=$(cd "$(dirname "$0")" && pwd)
REPO=$(cd "$HERE/.." && pwd)
source "$HERE/qa-lib.sh"

if [[ -z "$TMUX_PANE" ]]; then
  echo "ERROR: run this from inside the same tmux pane used for Check 1."
  exit 1
fi

RUN_ID=$(qa_run_id)
qa_snapshot_cache
echo "QA run id: $RUN_ID"

CF="$REPO/bin/claude-follow"

echo ">>> Check 2 — stop restores the border"
echo ">>> Running: claude-follow stop"
"$CF" stop

echo
echo "LOOK AT: the follower pane closing, and the origin pane's border/title"
echo "bar right after. Expect: pane closed AND border back to default — no"
echo "lingering color, no leftover border-title bar."
echo
echo "Programmatic cross-check (expect empty):"
tmux show-options -wv pane-border-status 2>/dev/null || echo "(no pane-border-status option set — consistent with restored default)"
