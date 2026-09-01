#!/bin/zsh
# Deterministic live smoke for the FIRST-CLASS NVIM BACKEND, against a real
# launched nvim (real tmux + your real nvim config). Fires two edits with
# DISTINCT writer identities into the nvim follower you started with
# `start --backend nvim`, so you watch:
#   - char-by-char typing over RPC (not a flash) with the current line
#     highlighted (extmark) and the cursor following;
#   - after the 2nd distinct writer, the NVIM FLOATING STATUS WINDOW appears
#     with the writer label colored by identity (nvim's writer cue — this is
#     what replaces the tmux border tint).
# Content comes from qa/fixtures/cue-writer{1,2}.py.
#
# PREREQUISITES (do these first, then run this from the SAME origin pane):
#   1. From the pane where `claude` normally runs (adopt_existing defaults to
#      false, so this LAUNCHES a dedicated nvim in a split — it does NOT touch
#      your config):
#        claude-follow start --backend nvim
#      Confirm a dedicated nvim pane opened beside you.
#   2. Run this script from that SAME pane (it needs your $TMUX_PANE).
#
# WHAT TO WATCH: the nvim follower pane.
#   - Writer 1 (you): the file types in char-by-char; NO floating window yet.
#   - Writer 2 (code-reviewer): a small floating window appears with the label
#     "code-reviewer" in a color. <-- that is the nvim writer cue working.
set -e
HERE=$(cd "$(dirname "$0")" && pwd)
FIX="$HERE/../qa/fixtures"
CF="$(cd "$HERE/.." && pwd)/bin/claude-follow"
F=/tmp/vaf-nvim-smoke.py
if [[ -z "$TMUX_PANE" ]]; then
  echo "ERROR: run this from inside your tmux pane (the one where you ran 'claude-follow start --backend nvim')."
  exit 1
fi

rm -f "$F"   # writer 1 must be a FRESH file (full retype)

fire() {  # $1 = fixture filename, $2 = extra JSON identity fields
  local fixture="$1" id="$2"
  local payload="{\"tool_name\":\"Write\",\"tool_input\":{\"file_path\":\"$F\"}${id}}"
  # Order matters: `hook pre` snapshots the CURRENT file, the new content
  # lands after it, and `hook post` animates the diff. Copying the fixture
  # first made the pre snapshot identical to the post content, so an
  # existing file's "edit" diffed empty and animated NOTHING — the second
  # writer of this script never actually animated (2026-08-25 battery,
  # Check 5 finding).
  echo "$payload" | "$CF" hook pre
  cp "$FIX/$fixture" "$F"
  echo "$payload" | "$CF" hook post
}

echo ">>> Writer 1 (you, no agent_id) — watch it TYPE char-by-char into nvim; NO floating window yet."
fire cue-writer1.py ',"session_id":"you-foreground"'
echo ">>> Did the content type in char-by-char (line highlighted, cursor following)? (expected: YES)"
echo "    Press Enter to fire writer 2..."
read _

echo ">>> Writer 2 (agent_id=rev1, agent_type=code-reviewer) — a FLOATING window with a colored 'code-reviewer' label should appear."
fire cue-writer2.py ',"session_id":"you-foreground","agent_id":"rev1","agent_type":"code-reviewer"'
echo ">>> Look now: floating window with the label 'code-reviewer' in a color? (expected: YES)"
echo
echo "Cleanup when done:  $CF stop"
echo "  NOTE: stop kills a LAUNCHED nvim pane (the follower owns it); only an ADOPTED"
echo "  nvim (adopt_existing) is never killed — that one you close yourself with :q."
