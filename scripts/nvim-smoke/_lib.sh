#!/bin/zsh
# Shared boilerplate for the nvim-backend manual smoke (Checks 5/6 of
# qa/smoke-runbook.md). Each tN-*.sh sources this relative to itself, so the
# scripts run from any clone without hardcoded paths.
REPO="${0:A:h:h:h}"
CF="$REPO/bin/claude-follow"
FIX="$REPO/qa/fixtures"

if [[ -z "$TMUX" || -z "$TMUX_PANE" ]]; then
  echo "ERROR: run this from INSIDE the tmux pane where you ran 'claude-follow start --backend nvim'."
  echo "       (the hooks resolve the follower by this pane's tmux window)"
  return 1 2>/dev/null || exit 1
fi

_payload() {  # $1=file  $2=extra identity json (leading comma, may be empty)
  print -r -- "{\"tool_name\":\"Write\",\"tool_input\":{\"file_path\":\"$1\"}$2}"
}

# drive FIXTURE TMPFILE [IDENTITY_JSON]
# Copies a fixture to a scratch path, then fires the pre+post hooks so the
# nvim follower animates it. A fresh TMPFILE animates as a full char-by-char
# retype (show_fresh); reusing one animates the diff.
drive() {
  local fixture="$1" tmpfile="$2" identity="$3"
  cp "$FIX/$fixture" "$tmpfile"
  _payload "$tmpfile" "$identity" | "$CF" hook pre
  _payload "$tmpfile" "$identity" | "$CF" hook post
}
