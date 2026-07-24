#!/bin/zsh
# QA Check 7 — parallel-hook serialization. This is the hermetic proof
# (scripts/repro-concurrent-hooks.sh) run as-is: it already streams the live
# animation to the terminal while it races six hook-post processes, then
# prints a clear PASS/FAIL result block — Alberto watches it happen in real
# time, no separate "leave it running" state is needed. It is fully
# self-contained (isolated $HOME/tmux socket, own trap cleanup EXIT), so
# unlike the other checks this one keeps its own internal cleanup rather
# than deferring to the driver.
set -e
HERE=$(cd "$(dirname "$0")" && pwd)
source "$HERE/qa-lib.sh"

RUN_ID=$(qa_run_id)
qa_snapshot_cache
echo "QA run id: $RUN_ID"

echo ">>> Check 7 — parallel-hook serialization (hermetic, live)"
echo "LOOK AT: the real-time animation streamed below — expect one clean tab"
echo "typing one file's content, then the script's printed result block"
echo "(TABS: 1, interleave: 0, literal_open: 0, PASS: ...)."
echo

zsh "$HERE/repro-concurrent-hooks.sh"
