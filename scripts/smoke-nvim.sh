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
CF=claude-follow
F=/tmp/vaf-nvim-smoke.py
if [[ -z "$TMUX_PANE" ]]; then
  echo "ERROR: run this from inside your tmux pane (the one where you ran 'claude-follow start --backend nvim')."
  exit 1
fi

pre_post() {  # $1 = extra JSON identity fields
  local id="$1"
  echo "{\"tool_name\":\"Write\",\"tool_input\":{\"file_path\":\"$F\"}${id}}" | "$CF" hook pre
  echo "{\"tool_name\":\"Write\",\"tool_input\":{\"file_path\":\"$F\"}${id}}" | "$CF" hook post
}

echo ">>> Writer 1 (you, no agent_id) — watch it TYPE char-by-char into nvim; NO floating window yet."
cp "$FIX/cue-writer1.py" "$F"
pre_post ',"session_id":"you-foreground"'
echo ">>> Did the content type in char-by-char (line highlighted, cursor following)? (expected: YES)"
echo "    Press Enter to fire writer 2..."
read _

echo ">>> Writer 2 (agent_id=rev1, agent_type=code-reviewer) — a FLOATING window with a colored 'code-reviewer' label should appear."
cp "$FIX/cue-writer2.py" "$F"
pre_post ',"session_id":"you-foreground","agent_id":"rev1","agent_type":"code-reviewer"'
echo ">>> Look now: floating window with the label 'code-reviewer' in a color? (expected: YES)"
echo
echo "Cleanup when done:  $CF stop"
echo "  NOTE: stop() is a no-op for a LAUNCHED nvim by design (it never kills your editor),"
echo "  so the nvim pane STAYS OPEN — close it yourself with :q in that pane. State/keybindings"
echo "  are cleared. (Killing a launched follower is a documented later-phase item.)"
