#!/bin/zsh
# QA Check 11 — nvim backend: multi-file tabs. Restarts the nvim follower and
# edits three distinct files back to back, the way one Claude turn touching
# three files does, then prints the follower's real tabpage list over RPC so
# Alberto can compare what he sees against what nvim actually holds.
#
# Serial, not parallel, on purpose: parallel hooks are serialized by the
# atomic animation-slot claim (Check 7) and only ONE of them animates, so
# firing them at once would test that guard instead of tab parity. A turn
# whose tool calls are sequential — the ordinary case — fires them like this.
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
A=/tmp/vaf-qa-multi-alpha.py
B=/tmp/vaf-qa-multi-beta.py
G=/tmp/vaf-qa-multi-gamma.py

echo ">>> Check 11 — nvim backend: multi-file tabs"
echo ">>> Restarting the nvim follower (--backend nvim, default speed)..."
echo "    (stop kills a LAUNCHED nvim pane; an ADOPTED one you close with :q)"
"$CF" stop >/dev/null 2>&1 || true
"$CF" start --backend nvim

fire() {  # $1 = fixture path, $2 = /tmp target
  local fixture="$1" target="$2"
  rm -f "$target"
  local payload="{\"tool_name\":\"Write\",\"tool_input\":{\"file_path\":\"$target\"},\"session_id\":\"me\"}"
  # Order matters: `hook pre` snapshots the CURRENT file, the new content
  # lands after it, and `hook post` animates (see smoke-nvim.sh's note).
  echo "$payload" | "$CF" hook pre
  cp "$fixture" "$target"
  echo "$payload" | "$CF" hook post
}

echo ">>> Editing three files back to back..."
fire "$FIX/multi-alpha.py" "$A"
fire "$FIX/multi-beta.py" "$B"
fire "$FIX/multi-gamma.py" "$G"

WINDOW_ID=$(qa_window_id)
SOCK=$(qa_follower_target "$WINDOW_ID")
echo
echo ">>> nvim's own tabpage list (over RPC, not a screen read):"
python3 - "$SOCK" <<'PY'
import sys

import pynvim

nvim = pynvim.attach("socket", path=sys.argv[1])
tabs = nvim.api.list_tabpages()
for number, tabpage in enumerate(tabs, 1):
    names = []
    for win in nvim.api.tabpage_list_wins(tabpage):
        buf = nvim.api.win_get_buf(win)
        names.append(nvim.api.buf_get_name(buf) or "(unnamed)")
    print(f"  tab {number}: {', '.join(names)}")
print(f"  TABS: {len(tabs)}   (want 3, one per file)")
PY

echo
echo "LOOK AT: the nvim follower pane, with the tab line across its top."
echo "  - THREE tabs in ONE nvim — not three stacked windows/splits, and not"
echo "    one tab whose content was replaced twice."
echo "  - Each tab holds its own file: TAB ALPHA, TAB BETA, TAB GAMMA."
echo "  - Walk them with gt / gT: every tab shows its own marker, complete,"
echo "    with no other file's content mixed in."
echo "  - The list printed above must say TABS: 3 and name the three files."
echo
echo "Cleanup when done:  rm -f $A $B $G"
echo "Close the leftover nvim pane/split with :q if stop left one behind."
