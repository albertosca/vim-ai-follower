#!/bin/zsh
# QA Check 12 — the "Writing..." cue during EVERY animation, and the static
# gruvbox colorscheme forced before the first one. Restarts the nvim follower
# slow and fires ONE edit into a FRESH file with no writer identity — the
# exact case that used to leave the float body blank for the whole animation
# (28937f0), on the exact run where the colorscheme race used to show
# inconsistent colors (0998397, ordering fixed in c42b77a).
#
# Deliberately the FIRST animation on a FRESH nvim: the colorscheme is forced
# once per nvim process (guarded on g:colors_name), so restarting the follower
# is what makes this check non-vacuous. Re-running it against an nvim that
# already animated measures nothing.
set -e
HERE=$(cd "$(dirname "$0")" && pwd)
REPO=$(cd "$HERE/.." && pwd)
FIX="$REPO/qa/fixtures"
source "$HERE/qa-lib.sh"

if [[ -z "$TMUX_PANE" ]]; then
  echo "ERROR: run this from inside your tmux pane."
  exit 1
fi

RUN_ID=$(qa_run_id)
qa_snapshot_cache
echo "QA run id: $RUN_ID"

CF="$REPO/bin/claude-follow"
F=/tmp/vaf-qa-writing-cue.py

echo ">>> Check 12 — \"Writing...\" cue + static colorscheme (nvim)"
echo ">>> Restarting the nvim follower slow (--backend nvim --speed lento)..."
"$CF" stop >/dev/null 2>&1 || true
"$CF" start --backend nvim --speed lento

rm -f "$F"
P="{\"tool_name\":\"Write\",\"tool_input\":{\"file_path\":\"$F\"},\"session_id\":\"me\"}"
# Order matters: `hook pre` snapshots the CURRENT (absent) file, the content
# lands after it, and `hook post` animates it as a fresh retype.
echo "$P" | "$CF" hook pre
cp "$FIX/cue-writer1.py" "$F"
echo ">>> Firing hook post — animates slowly now, FIRST animation on this nvim."
echo "$P" | "$CF" hook post &

echo
echo "LOOK AT: the nvim follower pane for the whole of this one animation."
echo "  - The small rounded floating box, top-right: its BODY reads"
echo "    \"Writing...\" from the first typed characters, not only after a"
echo "    pause or a des-interrupt. There is no writer identity in this edit,"
echo "    so the box title stays the default — the body is the thing to read."
echo "  - Colors are gruvbox-dark and READABLE from the very first line:"
echo "    syntax colored, comfortable contrast, no washed-out or unreadable"
echo "    stretch that later 'settles' as plugins finish loading."
echo "  - The typed line is HIGHLIGHTED as the cursor walks it (VafTypingLine"
echo "    survives the colorscheme's hi clear — that is the c42b77a fix)."
echo "  - Once the animation finishes: no stuck \"Writing...\" anywhere. With"
echo "    no writer identity (this edit) the whole box CLOSES; with one"
echo "    (Check 5's writer 2) it stays, titled, with a blank body — the"
echo "    run-2 behaviour Alberto chose. Verified live 2026-09-21: box gone."
echo
echo "> tmux-backend variant (optional): the same cue is backend-shared, and"
echo "  on tmux it renders as the pane BORDER TITLE instead of a float. To see"
echo "  it: claude-follow stop; claude-follow start --speed lento, then fire"
echo "  the same two hooks and watch the follower pane's border title."
echo
echo "Cleanup when done:  rm -f $F"
echo "Close the leftover nvim pane/split with :q if stop left one behind."
