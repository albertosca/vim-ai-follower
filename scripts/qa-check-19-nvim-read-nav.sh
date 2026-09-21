#!/bin/zsh
# QA Check 19 — nvim backend: a Read of a never-animated file opens a tab
# holding the REAL on-disk text, and an offset past the end of the file lands
# on the last line instead of crashing the hook (98b6201). Before the fix
# ensure_showing delegated to goto_file, whose no-buffer fallback made an
# EMPTY named buffer and never read disk — so a Read opened a blank tab, and
# the goto_line(offset) that follows raised nvim's "Invalid cursor line: out
# of range" straight out of the hook process.
#
# Both hooks run in the FOREGROUND with their exit code and stderr printed:
# the failure mode is a dead hook, so a pass and a crash must not look alike.
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
RUN_DIR="/tmp/vaf-qa-${RUN_ID}"
mkdir -p "$RUN_DIR"
qa_snapshot_cache
echo "QA run id: $RUN_ID (scratch: $RUN_DIR)"

CF="$REPO/bin/claude-follow"
F=/tmp/vaf-qa-read-nav.py

echo ">>> Check 19 — nvim: Read navigation shows disk content, goto_line clamps"
echo ">>> Restarting the nvim follower (--backend nvim) so the file below has"
echo "    definitely never been animated into it..."
"$CF" stop >/dev/null 2>&1 || true
"$CF" start --backend nvim

cp "$FIX/standalone-demo.py" "$F"
LINES=$(wc -l < "$F" | tr -d ' ')
echo ">>> $F is $LINES lines on disk."

WINDOW_ID=$(qa_window_id)
SOCK=$(qa_follower_target "$WINDOW_ID")

read_hook() {  # $1 = offset, $2 = label
  local offset="$1" label="$2"
  local payload="{\"tool_name\":\"Read\",\"tool_input\":{\"file_path\":\"$F\",\"offset\":$offset},\"session_id\":\"me\"}"
  set +e
  echo "$payload" | "$CF" hook post > "$RUN_DIR/$label.out" 2> "$RUN_DIR/$label.err"
  local code=$?
  set -e
  echo "    exit code: $code   (want 0)"
  if [[ -s "$RUN_DIR/$label.err" ]]; then
    echo "    !! stderr (this is the crash this check guards):"
    sed 's/^/    | /' "$RUN_DIR/$label.err"
  else
    echo "    stderr: empty (no traceback)"
  fi
}

cursor_line() {
  python3 - "$SOCK" <<'PY'
import sys

import pynvim

nvim = pynvim.attach("socket", path=sys.argv[1])
print(nvim.api.win_get_cursor(nvim.api.get_current_win())[0])
PY
}

echo ">>> [1/2] Read with offset 5 — a file this nvim has never seen..."
read_hook 5 in-range
echo "    cursor is on line: $(cursor_line)   (want 5)"

echo ">>> [2/2] Read with offset 9999 — far past the end of the file..."
read_hook 9999 past-eof
echo "    cursor is on line: $(cursor_line)   (want $LINES, the last line)"

echo
echo "LOOK AT: the follower nvim pane."
echo "  - A tab opened holding the REAL content of $F (the bfs() algorithm),"
echo "    not an empty buffer and not a blank tab."
echo "  - Both exit codes above are 0 with empty stderr. A traceback saying"
echo "    'Invalid cursor line: out of range' is the FAIL."
echo "  - The cursor ended on the LAST line, not off the end and not on"
echo "    line 1."
echo "  - The buffer is locked (nomodifiable): this nvim was LAUNCHED by the"
echo "    follower, so it owns it. See the adopt note below for the other"
echo "    half of that rule."
echo
echo "Prove the content from the real buffer, not the rendered pane:"
echo "  source $HERE/qa-lib.sh && qa_dump_nvim_buffer $SOCK $F | diff - $FIX/standalone-demo.py"
echo
echo "> Note - ADOPTED nvim (779284b), optional and manual, same shape as"
echo "  Check 6's adopt note. An adopted nvim is the user's OWN editor and"
echo "  navigation must never lock it, while a launched one (above) is"
echo "  always locked. Adoption needs the ORIGIN pane's own process to BE"
echo "  nvim (nvim_connect.discover_adopt_socket globs the pane pid's"
echo "  socket), so the realistic staging is: open nvim as a pane command,"
echo "  run the shell inside it (:terminal), set adopt_existing to true, and"
echo "  run claude-follow start --backend nvim from there. Then fire the"
echo "  same Read and check the buffer stays MODIFIABLE:"
echo "      :echo &modifiable    \" want 1 in an adopted nvim, 0 when launched"
echo "  NOT VERIFIED by this script or by the 2026-09-21 verification run —"
echo "  only the launched half above was exercised live. The adopted half is"
echo "  covered by tests/test_nvim_integration_lock_parity.py."
echo
echo "Cleanup when done:"
echo "  rm -f $F"
echo "  rm -rf $RUN_DIR"
echo "Close the leftover nvim pane/split with :q if stop left one behind."
