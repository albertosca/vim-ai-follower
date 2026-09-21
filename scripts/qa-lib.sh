#!/bin/zsh
# QA harness shared library — run isolation, verified teardown.
# Sourced by every check script and final teardown. Provides:
#   qa_run_id, qa_snapshot_cache, qa_cache_created, qa_teardown, qa_verify_clean,
#   qa_protect_config, qa_write_test_config, qa_config_handoff,
#   qa_window_id, qa_follower_target, qa_dump_nvim_buffer, qa_dump_vim_buffer

# Generate unique run token (timestamp + random 8-char suffix, usable in /tmp/vaf-qa-<id>/)
qa_run_id() {
  local ts=$(date +%s)
  local rand=$(head -c 4 /dev/urandom | od -An -tx1 | tr -d ' ')
  echo "${ts}-${rand}"
}

# The tmux window id this pane belongs to — the id the follower keys ALL of
# its per-window state by (state file, signal files, pending animation,
# animating marker), the same one session.py derives from $TMUX_PANE. Checks
# that have to look at those files by hand (kill a hook, wait for a pending
# file to appear) resolve the id through this.
qa_window_id() {
  tmux display-message -p -t "$TMUX_PANE" '#{window_id}'
}

# The follower's drive target for this window, read straight out of the
# persisted state file: a tmux pane id for the tmux backend, an nvim RPC
# socket path for the nvim backend. Empty when no follower is registered.
qa_follower_target() {
  local window_id="${1:-$(qa_window_id)}"
  python3 - "$window_id" <<'PY'
import json
import sys
from pathlib import Path

path = Path.home() / ".cache" / "claude-vim-follower" / f"{sys.argv[1]}.pane"
print(json.loads(path.read_text())["target"] if path.exists() else "")
PY
}

# Print the REAL buffer holding `file` out of the nvim listening on `socket`.
# Deliberately NOT capture-pane: that shows the RENDERED screen (wrapped
# lines, truncation, CoC/inlay virtual text mixed in), which has twice been
# mistaken for buffer corruption in this project. Every check whose verdict
# is about CONTENT reads the buffer through this instead.
qa_dump_nvim_buffer() {
  local socket="$1" file="$2"
  python3 - "$socket" "$file" <<'PY'
import sys
from pathlib import Path

import pynvim

# Compare resolved paths: on macOS /tmp is a symlink to /private/tmp, so a
# buffer the follower opened as /tmp/x.py reports its name as /private/tmp/x.py
# and a plain string compare silently finds nothing.
wanted = Path(sys.argv[2]).resolve()
nvim = pynvim.attach("socket", path=sys.argv[1])
for buf in nvim.buffers:
    if buf.name and Path(buf.name).resolve() == wanted:
        # errors="replace": a buffer read WHILE it is being typed can hold a
        # partial multi-byte sequence, and a raw print would die on it
        # instead of showing the check's answer.
        text = "\n".join(buf[:])
        sys.stdout.buffer.write(text.encode("utf-8", "replace") + b"\n")
        break
else:
    print(f"(no buffer for {wanted})", file=sys.stderr)
    raise SystemExit(1)
PY
}

# The tmux-backend twin of qa_dump_nvim_buffer: dump the follower Vim's real
# buffer, for the same reason. Escape Escape first so the Ex command lands
# even if the pane was left in insert mode.
#
# `:w!` to a scratch path rather than `:redir | silent %p`: measured
# 2026-09-21 against a real follower, redir renders every BLANK line as a
# single space and drops the file's final newline, so a byte diff against a
# fixture fails on a buffer that is in fact correct — a dump you have to
# hand-normalize is a dump you cannot trust. The bang overrides the
# follower's readonly relock; the write goes to the scratch path only, never
# to the file under test.
qa_dump_vim_buffer() {
  local pane="$1"
  local out="/tmp/vaf-qa-bufdump-$$.txt"
  tmux send-keys -t "$pane" Escape Escape
  tmux send-keys -t "$pane" -l -- ":silent! w! $out"
  tmux send-keys -t "$pane" Enter
  sleep 0.8
  cat "$out"
  rm -f "$out"
}

# Capture current cache state before a run starts. Idempotent — a second
# call (e.g. each of the 10 checks in a full battery run calling this) does
# NOT move the marker, so a check late in the run doesn't hide what an
# earlier check created. qa_teardown removes the marker once it has used it,
# so the next independent run starts fresh.
qa_snapshot_cache() {
  local cache_dir=~/.cache/claude-vim-follower
  mkdir -p "$cache_dir"
  local marker="$cache_dir/.qa-snapshot-marker"
  [[ -f "$marker" ]] || touch "$marker"
}

# List cache entries created since qa_snapshot_cache was called.
# Empty output if nothing created or cache doesn't exist.
qa_cache_created() {
  local cache_dir=~/.cache/claude-vim-follower
  local marker="$cache_dir/.qa-snapshot-marker"

  # If marker or cache doesn't exist, nothing was tracked/created
  if [[ ! -f "$marker" ]]; then
    return 0
  fi

  if [[ -d "$cache_dir" ]]; then
    find "$cache_dir" -type f -newer "$marker" 2>/dev/null
  fi
}

# Back up ~/.config/claude-vim-follower/config.json (if any) and register a
# trap that restores it automatically on ANY unexpected exit from this shell
# — an uncaught error under `set -e`, a signal, or an explicit early `exit`.
# The backup lives at /tmp/vaf-config-backup-<id>.json — deliberately OUTSIDE
# the /tmp/vaf-qa-* glob qa_teardown deletes, so a final teardown running
# before the check's own restore can never destroy the only copy of the
# user's real config.
#
# Call this BEFORE writing any test config, THEN immediately set the exit
# trap yourself, AT YOUR SCRIPT'S TOP LEVEL (not inside a function):
#   qa_protect_config
#   trap '_qa_restore_config_on_exit' EXIT
# This library cannot set the trap for you — zsh treats a trap set inside a
# function as scoped to that function, firing the moment the function
# RETURNS rather than when the script's shell actually exits, which would
# restore (and delete the backup of) the real config before the test config
# is even written. The trap must be registered in the caller's own top-level
# scope to fire at real process exit.
#
# Then call qa_write_test_config. If the check's design is to leave the test
# config in place for the driver to watch (rather than restoring the instant
# the script exits), call qa_config_handoff once you reach that point — it
# disarms the auto-restore so the driver's own printed "cleanup when done"
# command is what restores it, matching the check's intended timing. Without
# qa_config_handoff, the trap restores unconditionally the moment the
# script's shell exits.
qa_protect_config() {
  local config_dir=~/.config/claude-vim-follower
  local config="$config_dir/config.json"
  mkdir -p "$config_dir"
  QA_CONFIG_PATH="$config"
  QA_CONFIG_BACKUP="/tmp/vaf-config-backup-$(qa_run_id).json"
  if [[ -f "$config" ]]; then
    cp "$config" "$QA_CONFIG_BACKUP"
    QA_CONFIG_HAD_REAL=1
  else
    QA_CONFIG_HAD_REAL=0
  fi
  QA_CONFIG_HANDOFF=0
}

qa_write_test_config() {
  cat > "$QA_CONFIG_PATH"
}

# Disarm the exit-trap's auto-restore: the check has reached its intended
# "leave the test config running for the driver to watch" point, and the
# driver's own printed cleanup command now owns restoring it.
qa_config_handoff() {
  QA_CONFIG_HANDOFF=1
}

_qa_restore_config_on_exit() {
  [[ -z "$QA_CONFIG_PATH" ]] && return 0
  [[ "$QA_CONFIG_HANDOFF" -eq 1 ]] && return 0
  if [[ "$QA_CONFIG_HAD_REAL" -eq 1 ]]; then
    cp "$QA_CONFIG_BACKUP" "$QA_CONFIG_PATH"
    echo ">>> (auto-restore on exit) restored your real config."
  else
    rm -f "$QA_CONFIG_PATH"
    echo ">>> (auto-restore on exit) removed the test config (none existed before)."
  fi
  rm -f "$QA_CONFIG_BACKUP"
}

# Cleanup: kill vaf-qa*/vaf-smoke* tmux sessions, remove the run's temp
# directory, remove QA-created cache entries, then verify all is clean.
# Argument: optional run_id (if omitted, cleans ALL vaf-qa-* directories).
#
# Does NOT kill nvim processes: a launched follower's nvim (tmux-backend
# animation, or a standalone no-tmux window) carries no reliable "this is
# QA" marker in its argv or socket path — it's keyed by the real tmux
# window/terminal identity, indistinguishable from the user's own nvim.
# Blindly pkill-ing nvim here would risk killing the user's real work, which
# is worse than leaving one behind. Each check's own "Cleanup when done"
# output tells the driver how to close its follower/window by hand.
qa_teardown() {
  local run_id="$1"

  # Kill all vaf-qa*/vaf-smoke* tmux sessions (vaf-smoke* covers the
  # pre-existing smoke-*.sh helpers some checks wrap, e.g. Check 4's
  # window-scoping session, which predate the vaf-qa-only naming convention)
  tmux list-sessions 2>/dev/null | grep -E '^(vaf-qa|vaf-smoke)' | cut -d: -f1 | while read session; do
    tmux kill-session -t "$session" 2>/dev/null || true
  done

  # Remove run's temp directory (or all vaf-qa-* if no run_id given)
  if [[ -n "$run_id" ]]; then
    rm -rf "/tmp/vaf-qa-${run_id}" 2>/dev/null || true
  else
    local -a tmpdirs
    tmpdirs=(/tmp/vaf-qa-*(N))
    [[ ${#tmpdirs[@]} -gt 0 ]] && rm -rf $tmpdirs 2>/dev/null || true
  fi

  # Remove QA-created cache entries, then reset the snapshot marker so the
  # next independent run (or the next full battery) starts from a clean diff
  # baseline instead of one permanently pinned to the very first run ever.
  qa_cache_created | while read entry; do
    [[ -n "$entry" ]] && rm -f "$entry" 2>/dev/null || true
  done
  rm -f ~/.cache/claude-vim-follower/.qa-snapshot-marker

  # Run verification and return its exit code
  qa_verify_clean
}

# Verify everything is cleaned up and print a clear report.
# Exits 0 if clean, non-zero (printing what lingers) otherwise.
qa_verify_clean() {
  local any_issues=0

  # Check for remaining vaf-qa*/vaf-smoke* tmux sessions
  if tmux list-sessions 2>/dev/null | grep -E '^(vaf-qa|vaf-smoke)' >/dev/null 2>&1; then
    echo "REMAINING: vaf-qa*/vaf-smoke* tmux sessions:"
    tmux list-sessions 2>/dev/null | grep -E '^(vaf-qa|vaf-smoke)'
    any_issues=1
  fi

  # Check for remaining /tmp/vaf-qa-* directories (zsh nullglob to avoid "no matches" warning)
  local -a leftovers
  leftovers=(/tmp/vaf-qa-*(N))
  if [[ ${#leftovers[@]} -gt 0 ]]; then
    echo "REMAINING: /tmp/vaf-qa-* directories:"
    print -l $leftovers
    any_issues=1
  fi

  # Check for QA-created cache entries
  local created_count=$(qa_cache_created | wc -l)
  if [[ $created_count -gt 0 ]]; then
    echo "REMAINING: QA-created cache entries:"
    qa_cache_created
    any_issues=1
  fi

  # Report clean state
  if [[ $any_issues -eq 0 ]]; then
    echo "CLEAN: no vaf-qa*/vaf-smoke* tmux session, no /tmp/vaf-qa-*, no QA-created cache entries"
    return 0
  else
    return 1
  fi
}
