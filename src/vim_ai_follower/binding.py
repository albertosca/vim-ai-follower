"""Session→window binding: while a session's hooks can still prove their
window (TMUX_PANE present), remember() persists session_id -> (window_id,
tmux server pid) so a later hook from the SAME session that has lost
TMUX_PANE can recall() it back.

This is recall, not inference: the binding was measured by the session
itself while it could still see its own pane. recall() only returns a
window when the tmux server pid it was written under still matches the
current one — window ids are reassigned across server restarts, so a
mismatch means @18 today is a different window than @18 when it was
written, and using it would animate into someone else's project.

Every public function swallows OS errors and returns the safe value (None
for recall, no write for remember) — the only caller is a hook, and hooks
must never fail a tool call."""

from __future__ import annotations

import contextlib
import json
import os
import re
import subprocess
import time
from pathlib import Path
from typing import Any

from vim_ai_follower import cache

_SAFE_CHARS = re.compile(r"[^A-Za-z0-9_-]")
_MAX_SESSION_ID_LENGTH = 128


def _sanitize_session_id(session_id: str) -> str:
    """Keep [A-Za-z0-9_-], replace everything else with '_', and cap the
    length. Session ids are UUID-shaped today, but a filename derived from
    external input that can contain '/' or '..' is a directory-escape
    waiting to happen."""
    return _SAFE_CHARS.sub("_", session_id)[:_MAX_SESSION_ID_LENGTH]


def _bindings_dir() -> Path:
    # cache.CACHE_DIRECTORY is read at call time (not bound at import) so a test's
    # monkeypatch of the cache module is visible here too.
    return cache.CACHE_DIRECTORY / "bindings"


def _binding_path(session_id: str) -> Path:
    # Never named "*.pane": commands._other_live_follower's orphan scan
    # collects those and would delete our bindings.
    return _bindings_dir() / f"{_sanitize_session_id(session_id)}.json"


def tmux_server_pid() -> int | None:
    """The attached tmux server's pid, or None on ANY failure: tmux missing,
    non-zero exit, or unparseable output. Verified 2026-09-22 to answer
    correctly even with TMUX_PANE unset."""
    try:
        result = subprocess.run(
            ["tmux", "display-message", "-p", "#{pid}"],
            capture_output=True,
            text=True,
            check=False,
        )
    except OSError:
        return None
    if result.returncode != 0:
        return None
    try:
        return int(result.stdout.strip())
    except ValueError:
        return None


def tmux_server_started() -> int | None:
    """The attached tmux server's start time (`#{start_time}`, seconds since
    the epoch), or None on ANY failure. With the pid it names one server
    for good: after a reboot a new server can get the pid an earlier one
    had (final review M10), never its start time too."""
    try:
        result = subprocess.run(
            ["tmux", "display-message", "-p", "#{start_time}"],
            capture_output=True,
            text=True,
            check=False,
        )
    except OSError:
        return None
    if result.returncode != 0:
        return None
    try:
        return int(result.stdout.strip())
    except ValueError:
        return None


def tmux_server_pid_from(environment: dict[str, str]) -> int | None:
    """tmux_server_pid, falling back to the pid tmux writes into $TMUX
    ("socket,pid,session") when the server cannot be asked: what `stop`
    records in its tombstone, which must name a server whenever one can be
    named (an "unknown" one cannot tell this server from a later one)."""
    asked = tmux_server_pid()
    if asked is not None:
        return asked
    fields = environment.get("TMUX", "").split(",")
    if len(fields) >= 2 and fields[1].isdigit():
        return int(fields[1])
    return None


def _read_binding(path: Path) -> dict[str, Any] | None:
    try:
        return json.loads(path.read_text())  # type: ignore[no-any-return]
    except (OSError, json.JSONDecodeError):
        return None


def remember(session_id: str, window_id: str) -> None:
    """Persist that session_id currently animates into window_id, under the
    tmux server running right now. Writes nothing when the server pid can't
    be measured — a binding with no server pid can never be validated on
    recall, and storing one risks a future recall treating "no pid" as "a
    match", which is exactly the wrong-window hole this module exists to
    close."""
    pid = tmux_server_pid()
    if pid is None:
        return
    path = _binding_path(session_id)
    try:
        existing = _read_binding(path)
        if (
            existing is not None
            and existing.get("window_id") == window_id
            and existing.get("tmux_server_pid") == pid
        ):
            return  # steady state: nothing changed, don't write
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = json.dumps(
            {"window_id": window_id, "tmux_server_pid": pid, "updated_at": time.time()}
        )
        # Atomic write, same idiom as state.py/control.py: a reader always
        # sees the whole old or whole new file, never a half-written one.
        temporary_path = path.with_name(f"{path.name}.{os.getpid()}.tmp")
        temporary_path.write_text(payload)
        temporary_path.replace(path)
    except OSError:
        pass


def recall(session_id: str) -> str | None:
    """The window_id last remembered for session_id, or None when there is
    nothing to recall, the tmux server can't be measured right now, or the
    binding was written under a different (since-restarted) tmux server —
    in which case the stale file is deleted so it doesn't accumulate across
    restarts."""
    path = _binding_path(session_id)
    data = _read_binding(path)
    if data is None:
        return None
    current_pid = tmux_server_pid()
    if current_pid is None:
        return None
    if data.get("tmux_server_pid") != current_pid:
        with contextlib.suppress(OSError):
            path.unlink(missing_ok=True)
        return None
    window_id: str | None = data.get("window_id")
    return window_id
