"""Claude Code PreToolUse/PostToolUse hook handlers that drive the live follower animation."""

from __future__ import annotations

import dataclasses
import json
import logging
import subprocess
import time
from pathlib import Path
from typing import Any

from vim_ai_follower import cache, config, control, keybindings
from vim_ai_follower import diff as diff_module
from vim_ai_follower.backends import Follower, get_follower
from vim_ai_follower.backends.tmux_vim import TmuxVimFollower
from vim_ai_follower.snapshot import load as load_snapshot
from vim_ai_follower.snapshot import save as save_snapshot
from vim_ai_follower.state import FollowerState, touch_open_files
from vim_ai_follower.tmux import TmuxSession, adopt_target

LOG_PATH = cache.CACHE_DIR / "hook.log"

logger = logging.getLogger("vim_ai_follower")

_EDIT_TOOLS = {"Edit", "MultiEdit", "Write"}


def _configure_logging() -> None:
    if logger.handlers:
        return
    LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    handler = logging.FileHandler(LOG_PATH)
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)


def _tool_input(payload: dict[str, Any]) -> dict[str, Any]:
    value = payload.get("tool_input", {})
    return value if isinstance(value, dict) else {}


def _file_path(payload: dict[str, Any]) -> str | None:
    value = _tool_input(payload).get("file_path")
    return value if isinstance(value, str) else None


def _passes_policy(cfg: config.Config, file_path: str) -> bool:
    return cfg.open_policy != "code" or config.is_code_file(file_path)


def _get_active_follower(session_id: str) -> FollowerState | None:
    """Like FollowerState.get, but recovers a dead tmux follower by
    reopening a fresh pane from its recorded origin when on_failure is
    "reopen". Only used by the hook path — cmd_status/cmd_stop report or
    tear down state as-is and never trigger recovery as a side effect."""
    current = FollowerState.get(session_id)
    if current is not None:
        return current

    raw = FollowerState.read(session_id)
    if raw is None or raw.on_failure != "reopen" or raw.backend != "tmux" or not raw.origin:
        return None

    try:
        started = TmuxVimFollower.start(raw.origin)
    except subprocess.CalledProcessError as exc:
        logger.warning("failed to reopen follower pane from origin %s: %s", raw.origin, exc)
        return None

    FollowerState.set(
        session_id,
        "tmux",
        started.pane_id,
        current_file=None,
        origin=raw.origin,
        on_failure=raw.on_failure,
        speed=raw.speed,
    )
    return FollowerState.get(session_id)


def _maybe_auto_open(
    session_id: str, env: dict[str, str], file_path: str, cfg: config.Config
) -> FollowerState | None:
    if cfg.open_policy == "manual" or not _passes_policy(cfg, file_path):
        return None
    origin = env.get("TMUX_PANE", "")
    if not origin:
        return None
    keybindings.register()
    adopt = adopt_target(origin) if cfg.adopt_existing else None
    if adopt is not None:
        # shown_any=True from the first moment: an adopted Vim's current tab
        # belongs to the user and must never be renamed over.
        FollowerState.set(
            session_id,
            "tmux",
            adopt,
            origin=origin,
            on_failure=cfg.on_failure,
            speed=cfg.speed,
            adopted=True,
            shown_any=True,
        )
    else:
        try:
            started = TmuxVimFollower.start(origin)
        except subprocess.CalledProcessError as exc:
            logger.warning("auto-open failed from origin %s: %s", origin, exc)
            return None
        FollowerState.set(
            session_id,
            "tmux",
            started.pane_id,
            origin=origin,
            on_failure=cfg.on_failure,
            speed=cfg.speed,
        )
    return FollowerState.get(session_id)


def _ensure_buffer(session_id: str, follower: Follower, file_path: str) -> None:
    """Switches the follower to file_path's tab (`:tab drop`). Used for Read
    navigation and binary files, where showing the real on-disk content
    immediately is exactly what's wanted — unlike a fresh text edit, which
    goes through show_fresh instead so the finished content is never flashed
    before it's typed. Always runs the preamble — it's cheap and
    self-healing, immune to the user having closed or reordered tabs since
    the last time this file was current."""
    follower.ensure_showing(file_path)
    FollowerState.update_current_file(session_id, file_path)


def _touch_and_evict(
    session_id: str, follower: Follower, current: FollowerState, file_path: str, max_tabs: int
) -> None:
    """Bump file_path to most-recent in the tab list and close whatever now
    falls past max_tabs. Eviction only means anything for the tab-based tmux
    backend; on any other backend the close is skipped (nvim_rpc has no tabs)
    rather than asserted, so a future backend can grow open_files without an
    AssertionError crashing the hook."""
    new_open, evicted = touch_open_files(current.open_files, file_path, max_tabs)
    for old in evicted:
        if isinstance(follower, TmuxVimFollower):
            follower.close_tab(old)
    FollowerState.update(session_id, open_files=new_open, shown_any=True)


def _reconstruct_partial_fresh(content: str, completed_count: int) -> str:
    return "\n".join(content.splitlines()[:completed_count])


def _print_interrupt_notification(file_path: str, partial_content: str) -> None:
    context = (
        f"The user interrupted the live preview of {file_path} while it was "
        "being written, edited it themselves, and SAVED their own version — "
        "it is now the file's content on disk. Only this much of your "
        f"version had been shown before they took over:\n\n{partial_content}\n\n"
        "Re-read the file from disk and build on the user's version; do not "
        "restore yours without asking."
    )
    print(
        json.dumps(
            {
                "hookSpecificOutput": {
                    "hookEventName": "PostToolUse",
                    "additionalContext": context,
                }
            }
        )
    )


_HANDOFF_POLL_SECONDS = 0.2


def _await_user_handoff(
    current: FollowerState, session_id: str, file_path: str, after: str, partial_content: str
) -> None:
    """The user interrupted and owns the buffer: hold Claude's turn until
    they save their version (release Claude with it, via the notification)
    or press S again (discard their unsaved typing and resume following the
    file Claude wrote). A hook-timeout kill releases Claude without a
    notification — degraded but harmless."""
    control.mark_animating(session_id, state="handoff")
    try:
        while True:
            signal = control.check_signal(session_id)
            if signal == "interrupt":
                # the des-interrupt: reload the file Claude wrote, discarding
                # unsaved edits (and turning the renamed buffer into a real
                # file buffer); relock and resume following.
                TmuxVimFollower(pane_id=current.target, session_id=session_id).reload_and_relock(
                    file_path
                )
                FollowerState.update_current_file(session_id, file_path)
                return
            try:
                if Path(file_path).read_text() != after:
                    _print_interrupt_notification(file_path, partial_content)
                    return
            except OSError:
                pass  # mid-save or momentarily unreadable: check again
            time.sleep(_HANDOFF_POLL_SECONDS)
    finally:
        control.clear_animating(session_id)


def cmd_hook_pre(env: dict[str, str], payload: dict[str, Any]) -> int:
    _configure_logging()
    if payload.get("tool_name") not in _EDIT_TOOLS:
        return 0
    session = TmuxSession.from_env(env)
    if session is None:
        return 0
    raw = FollowerState.read(session.session_id)
    if raw is not None and not raw.enabled:
        return 0
    file_path = _file_path(payload)
    if file_path is None:
        return 0
    if not _passes_policy(config.load(), file_path):
        return 0
    try:
        before = Path(file_path).read_text()
    except (OSError, UnicodeDecodeError):
        before = ""
    save_snapshot(session.session_id, file_path, before)
    return 0


def _handle_hook_post_edit(env: dict[str, str], payload: dict[str, Any]) -> int:
    session = TmuxSession.from_env(env)
    if session is None:
        return 0
    raw = FollowerState.read(session.session_id)
    if raw is not None and not raw.enabled:
        return 0
    file_path = _file_path(payload)
    if file_path is None:
        return 0
    cfg = config.load()
    if not _passes_policy(cfg, file_path):
        return 0
    current = _get_active_follower(session.session_id) or _maybe_auto_open(
        session.session_id, env, file_path, cfg
    )
    if current is None:
        return 0
    try:
        raw_after = Path(file_path).read_bytes()
    except OSError as exc:
        logger.warning("failed to read %s: %s", file_path, exc)
        return 0

    follower = get_follower(
        current.backend,
        current.target,
        config.pace_seconds_for(current.speed),
        session_id=session.session_id,
    )
    is_fresh = file_path not in current.open_files
    binary = diff_module.is_binary(raw_after)

    # Consume any paused animation BEFORE animating: replaying it at pace 0
    # brings the buffer to the state the new edit's ops were computed
    # against. When the new edit targets another file, a different pending
    # file (a stale one this hook didn't leave behind), or a binary, the
    # buffer gets wiped/replaced (or isn't animated at all) anyway —
    # consuming without replaying is the correct discard.
    pending = control.load_pending_animation(session.session_id)
    if (
        pending is not None
        and (pending.file_path == file_path or pending.file_path == "")
        and not is_fresh
        and not binary
    ):
        assert isinstance(follower, TmuxVimFollower)  # only tmux ever persists pending state
        follower.resume(dataclasses.replace(pending, pace_seconds=0.0))

    if binary:
        # Binary files are never animated, so it's safe to just navigate to
        # them normally (real content shown immediately, nothing to spoil).
        if is_fresh:
            _ensure_buffer(session.session_id, follower, file_path)
        _touch_and_evict(session.session_id, follower, current, file_path, cfg.max_tabs)
        return 0

    after = raw_after.decode("utf-8", errors="replace")

    # Evict/persist BEFORE animating so the tab shuffle never lands
    # mid-typing — touch_open_files never puts the just-touched file_path in
    # the evicted slice, so the file about to be animated can never be the
    # one just closed.
    _touch_and_evict(session.session_id, follower, current, file_path, cfg.max_tabs)

    if is_fresh:
        result = follower.show_fresh(file_path, after, in_new_tab=current.shown_any)
        if result.outcome == "interrupted":
            # Clear the display-only pointer — the user owns the buffer now
            # and may change it under us. Freshness is keyed on open_files,
            # which the interrupt path never touches, so the next edit is
            # still a diff (apply_edit) against the pre-edit snapshot, never
            # a full retype. Even a killed handoff self-heals: the partial
            # buffer converges to disk at the next completed animation via
            # the `:silent! e!` relock.
            # (_await_user_handoff restores tracking on a des-interrupt.)
            FollowerState.update_current_file(session.session_id, None)
            partial = _reconstruct_partial_fresh(after, result.completed_count)
            _await_user_handoff(current, session.session_id, file_path, after, partial)
        else:
            FollowerState.update_current_file(session.session_id, file_path)
        return 0

    before = load_snapshot(session.session_id, file_path)
    ops = diff_module.compute_edit_script(before, after)
    result = follower.apply_edit(file_path, ops)
    if result.outcome == "interrupted":
        FollowerState.update_current_file(session.session_id, None)
        # completed_count indexes the very ops list the animation walked —
        # computing the script once keeps this reconstruction truthful.
        partial = diff_module.apply_ops(before, ops[: result.completed_count])
        _await_user_handoff(current, session.session_id, file_path, after, partial)
    return 0


def _handle_hook_post_read(env: dict[str, str], payload: dict[str, Any]) -> int:
    session = TmuxSession.from_env(env)
    if session is None:
        return 0
    raw = FollowerState.read(session.session_id)
    if raw is not None and not raw.enabled:
        return 0
    file_path = _file_path(payload)
    if file_path is None:
        return 0
    cfg = config.load()
    if not _passes_policy(cfg, file_path):
        return 0
    current = _get_active_follower(session.session_id) or _maybe_auto_open(
        session.session_id, env, file_path, cfg
    )
    if current is None:
        return 0
    follower = get_follower(current.backend, current.target)
    _ensure_buffer(session.session_id, follower, file_path)
    _touch_and_evict(session.session_id, follower, current, file_path, cfg.max_tabs)
    offset = _tool_input(payload).get("offset")
    if isinstance(offset, int) and offset > 0:
        follower.goto_line(offset)
    return 0


def cmd_hook_post(env: dict[str, str], payload: dict[str, Any]) -> int:
    _configure_logging()
    tool_name = payload.get("tool_name")
    if tool_name == "Read":
        return _handle_hook_post_read(env, payload)
    if tool_name in _EDIT_TOOLS:
        return _handle_hook_post_edit(env, payload)
    return 0
