#!/bin/zsh
# QA harness shared library — run isolation, verified teardown.
# Sourced by every check script and final teardown. Provides:
#   qa_run_id, qa_snapshot_cache, qa_cache_created, qa_teardown, qa_verify_clean

# Generate unique run token (timestamp + random 8-char suffix, usable in /tmp/vaf-qa-<id>/)
qa_run_id() {
  local ts=$(date +%s)
  local rand=$(head -c 4 /dev/urandom | od -An -tx1 | tr -d ' ')
  echo "${ts}-${rand}"
}

# Capture current cache state before a run starts.
# Writes a marker file; qa_cache_created uses it with find -newer.
qa_snapshot_cache() {
  local cache_dir=~/.cache/claude-vim-follower
  mkdir -p "$cache_dir"
  # Marker file timestamp used by find -newer to detect new cache entries
  touch "$cache_dir/.qa-snapshot-marker"
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

# Cleanup: kill vaf-qa* tmux sessions, kill QA nvim processes, remove temp dir,
# remove QA-created cache entries, then verify all is clean.
# Argument: optional run_id (if omitted, cleans ALL vaf-qa-* directories).
qa_teardown() {
  local run_id="$1"

  # Kill all vaf-qa* tmux sessions
  tmux list-sessions 2>/dev/null | grep -E '^vaf-qa' | cut -d: -f1 | while read session; do
    tmux kill-session -t "$session" 2>/dev/null || true
  done

  # Kill QA nvim processes (matching vaf-qa in command line)
  pkill -f 'vaf-qa' 2>/dev/null || true

  # Remove run's temp directory (or all vaf-qa-* if no run_id given)
  if [[ -n "$run_id" ]]; then
    rm -rf "/tmp/vaf-qa-${run_id}" 2>/dev/null || true
  else
    local -a tmpdirs
    tmpdirs=(/tmp/vaf-qa-*(N))
    [[ ${#tmpdirs[@]} -gt 0 ]] && rm -rf $tmpdirs 2>/dev/null || true
  fi

  # Remove QA-created cache entries
  qa_cache_created | while read entry; do
    [[ -n "$entry" ]] && rm -f "$entry" 2>/dev/null || true
  done

  # Run verification and return its exit code
  qa_verify_clean
}

# Verify everything is cleaned up and print a clear report.
# Exits 0 if clean, non-zero (printing what lingers) otherwise.
qa_verify_clean() {
  local any_issues=0

  # Check for remaining vaf-qa* tmux sessions
  if tmux list-sessions 2>/dev/null | grep -E '^vaf-qa' >/dev/null 2>&1; then
    echo "REMAINING: vaf-qa* tmux sessions:"
    tmux list-sessions 2>/dev/null | grep -E '^vaf-qa'
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
    echo "CLEAN: no vaf-qa* tmux session, no /tmp/vaf-qa-*, no QA-created cache entries"
    return 0
  else
    return 1
  fi
}
