#!/bin/zsh
# QA Check 4 — window-scoped identity (isolation). Thin wrapper over the
# shared harness around scripts/smoke-window-scoping.sh, which is already
# fully self-contained: it builds a dedicated `vaf-smoke` tmux session (never
# touches your real server), starts a follower in two windows, and animates
# distinct content into each for Alberto to eyeball.
set -e
HERE=$(cd "$(dirname "$0")" && pwd)
REPO=$(cd "$HERE/.." && pwd)
source "$HERE/qa-lib.sh"

RUN_ID=$(qa_run_id)
qa_snapshot_cache
echo "QA run id: $RUN_ID"

echo ">>> Check 4 — window-scoped identity (isolation)"
zsh "$HERE/smoke-window-scoping.sh"

echo
echo "LOOK AT: switching between windows 0 and 1 inside the vaf-smoke session"
echo "(tmux attach -t vaf-smoke), checking each follower's content in turn."
echo "Expect: no bleed — neither window's edits appear in the other's follower."
echo
echo "Cleanup when done:  tmux kill-session -t vaf-smoke"
