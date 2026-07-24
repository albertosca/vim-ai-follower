#!/bin/zsh
# QA Check 1 — per-writer color cue. Thin wrapper over the shared harness
# (qa-lib.sh) around scripts/smoke-color-cue.sh, which already does the real
# work: starts a follower (if needed) and fires two edits with distinct
# writer identities for Alberto to watch. This script does NOT tear down —
# Check 2 consumes the running follower state directly.
set -e
HERE=$(cd "$(dirname "$0")" && pwd)
REPO=$(cd "$HERE/.." && pwd)
source "$HERE/qa-lib.sh"

if [[ -z "$TMUX_PANE" ]]; then
  echo "ERROR: run this from inside the tmux pane where you ran 'claude-follow start'."
  exit 1
fi

RUN_ID=$(qa_run_id)
RUN_DIR="/tmp/vaf-qa-${RUN_ID}"
mkdir -p "$RUN_DIR"
qa_snapshot_cache
echo "QA run id: $RUN_ID (scratch: $RUN_DIR)"

CF="$REPO/bin/claude-follow"

echo ">>> Check 1 — per-writer color cue"
# NOTE: `claude-follow status` always exits 0, even when nothing is
# running — it prints "no follower active" instead of failing. Check the
# printed text, not the exit code.
if "$CF" status 2>&1 | grep -q "no follower active"; then
  echo ">>> No follower detected — starting one now (claude-follow start)..."
  "$CF" start
fi

zsh "$HERE/smoke-color-cue.sh"

echo
echo "LOOK AT: the follower pane's border color and title bar, at two moments —"
echo "right after writer 1's edit (expect neutral border, no title) and right"
echo "after writer 2's edit (expect a tinted border AND a title reading"
echo "'code-reviewer')."
echo
echo "This check's own scratch dir ($RUN_DIR) is unused by smoke-color-cue.sh"
echo "(it writes to /tmp/vaf-smoke-cue.py directly) — kept only for run-id bookkeeping."
