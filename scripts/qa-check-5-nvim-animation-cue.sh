#!/bin/zsh
# QA Check 5 — nvim backend: char-by-char animation + floating writer cue.
# Launches a dedicated nvim follower (--backend nvim; does not touch your
# config) and wraps scripts/smoke-nvim.sh, which fires two edits with
# distinct writer identities for Alberto to watch.
set -e
HERE=$(cd "$(dirname "$0")" && pwd)
REPO=$(cd "$HERE/.." && pwd)
source "$HERE/qa-lib.sh"

if [[ -z "$TMUX_PANE" ]]; then
  echo "ERROR: run this from inside the tmux pane where 'claude' normally runs."
  exit 1
fi

RUN_ID=$(qa_run_id)
qa_snapshot_cache
echo "QA run id: $RUN_ID"

CF="$REPO/bin/claude-follow"

echo ">>> Check 5 — nvim backend: char-by-char animation + floating writer cue"
echo ">>> Starting a dedicated nvim follower (claude-follow start --backend nvim)..."
"$CF" start --backend nvim

echo "LOOK AT (pane open): a dedicated nvim pane opened beside you (not a plain Vim pane)."
echo

zsh "$HERE/smoke-nvim.sh"

echo
echo "LOOK AT: the nvim pane while each edit types, at two moments —"
echo "writer 1's edit (expect char-by-char typing, highlighted current line,"
echo "cursor following, no floating window yet) and writer 2's edit (expect a"
echo "small floating window with the label 'code-reviewer' in a color)."
echo
echo "Cleanup when done: close the nvim pane yourself with :q — a launched"
echo "nvim follower is never killed by 'stop' (by design; see Check 6)."
