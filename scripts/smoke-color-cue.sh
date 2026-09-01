#!/bin/zsh
# Deterministic live smoke for the per-writer color cue, against your REAL
# follower (real tmux + real Vim config). Fires two edits with DISTINCT writer
# identities at the follower you already started, so you watch the border go
# neutral (1st writer) -> tinted + label (2nd writer). Content comes from
# qa/fixtures/cue-writer{1,2}.py.
#
# PREREQUISITES (do these first, then run this from the SAME origin pane):
#   1. From the pane where `claude` normally runs:
#        claude-follow start
#      Confirm a follower Vim pane opened beside you.
#   2. Run this script from that SAME pane (it needs your $TMUX_PANE).
#
# WHAT TO WATCH: the follower pane's BORDER.
#   - After writer 1 (you): border stays NEUTRAL (default), file animates.
#   - After writer 2 (code-reviewer): border TINTS and the border TITLE reads
#     "code-reviewer". <-- that is the cue working.
set -e
HERE=$(cd "$(dirname "$0")" && pwd)
FIX="$HERE/../qa/fixtures"
CF="$(cd "$(dirname "$0")/.." && pwd)/bin/claude-follow"
F=/tmp/vaf-smoke-cue.py
if [[ -z "$TMUX_PANE" ]]; then
  echo "ERROR: run this from inside your tmux pane (the one where you ran 'claude-follow start')."
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

echo ">>> Writer 1 (you, no agent_id) — border should stay NEUTRAL. Watch the follower."
fire cue-writer1.py ',"session_id":"you-foreground"'
echo ">>> Look now: border still default? (expected: YES)"
echo "    Press Enter to fire writer 2..."
read _

echo ">>> Writer 2 (agent_id=rev1, agent_type=code-reviewer) — border should TINT + label 'code-reviewer'."
fire cue-writer2.py ',"session_id":"you-foreground","agent_id":"rev1","agent_type":"code-reviewer"'
echo ">>> Look now: border tinted to a color AND the border title reads 'code-reviewer'? (expected: YES)"
echo
echo "Cleanup when done:  $CF stop   (border must return to default)"
