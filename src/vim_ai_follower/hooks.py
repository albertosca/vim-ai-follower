"""Claude Code PreToolUse/PostToolUse hook handlers that drive the live follower animation."""

from __future__ import annotations

import dataclasses
import json
import logging
import os
import subprocess
import time
from pathlib import Path
from typing import Any

from vim_ai_follower import cache, config, control, keybindings, writer_cue
from vim_ai_follower import diff as diff_module
from vim_ai_follower.backends import Follower, get_follower
from vim_ai_follower.backends.nvim_connect import resolve_nvim_target
from vim_ai_follower.backends.tmux_vim import TmuxVimFollower
from vim_ai_follower.snapshot import load as load_snapshot
from vim_ai_follower.snapshot import save as save_snapshot
from vim_ai_follower.state import FollowerState, touch_open_files
from vim_ai_follower.status_surface import status_surface_for
from vim_ai_follower.tmux import TmuxWindow, adopt_target, show_popup

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
    if not isinstance(value, str):
        return None
    # Canonical form: Vim resolves buffer names to real paths (on macOS
    # /tmp is a symlink to /private/tmp), so a symlinked spelling makes
    # :tab drop miss the existing buffer and open a duplicate tab.
    return os.path.realpath(value)


def _passes_policy(cfg: config.Config, file_path: str) -> bool:
    return cfg.open_policy != "code" or config.is_code_file(file_path)


def _get_active_follower(window_id: str) -> FollowerState | None:
    """Like FollowerState.get, but recovers a dead tmux follower by
    reopening a fresh pane from its recorded origin when on_failure is
    "reopen". Only used by the hook path — cmd_status/cmd_stop report or
    tear down state as-is and never trigger recovery as a side effect."""
    current = FollowerState.get(window_id)
    if current is not None:
        return current

    raw = FollowerState.read(window_id)
    if raw is None or raw.on_failure != "reopen" or not raw.origin:
        return None

    if raw.backend == "nvim":
        # Recovery never re-adopts: relaunch a fresh dedicated nvim (adopt=
        # False) so a user who closed their own editor is not silently taken
        # over again.
        try:
            sock, _launched = resolve_nvim_target(raw.origin, window_id, adopt=False)
        except subprocess.CalledProcessError as exc:
            logger.warning("failed to relaunch nvim from origin %s: %s", raw.origin, exc)
            return None
        FollowerState.set(
            window_id,
            "nvim",
            sock,
            current_file=None,
            origin=raw.origin,
            on_failure=raw.on_failure,
            speed=raw.speed,
            adopted=False,
        )
        return FollowerState.get(window_id)

    try:
        started = TmuxVimFollower.start(raw.origin)
    except subprocess.CalledProcessError as exc:
        logger.warning("failed to reopen follower pane from origin %s: %s", raw.origin, exc)
        return None

    FollowerState.set(
        window_id,
        "tmux",
        started.pane_id,
        current_file=None,
        origin=raw.origin,
        on_failure=raw.on_failure,
        speed=raw.speed,
    )
    return FollowerState.get(window_id)


def _maybe_auto_open(
    window_id: str, env: dict[str, str], file_path: str, cfg: config.Config
) -> FollowerState | None:
    if cfg.open_policy == "manual" or not _passes_policy(cfg, file_path):
        return None
    origin = env.get("TMUX_PANE", "")
    if not origin:  # pragma: no cover
        # Genuinely unreachable: both callers (_handle_hook_post_edit,
        # _handle_hook_post_read) only reach this function after
        # TmuxWindow.from_env(env) already returned a non-None window,
        # which itself required env.get("TMUX_PANE") to be truthy — the
        # same env dict, never mutated in between.
        return None
    keybindings.register()
    if cfg.backend == "nvim":
        # Same adopt-or-launch selection as cmd_start, but from the hook path.
        try:
            sock, launched = resolve_nvim_target(origin, window_id, adopt=cfg.adopt_existing)
        except subprocess.CalledProcessError as exc:
            logger.warning("nvim auto-open failed from origin %s: %s", origin, exc)
            return None
        FollowerState.set(
            window_id,
            "nvim",
            sock,
            origin=origin,
            on_failure=cfg.on_failure,
            speed=cfg.speed,
            adopted=not launched,
        )
        return FollowerState.get(window_id)
    adopt = adopt_target(origin) if cfg.adopt_existing else None
    if adopt is not None:
        # shown_any=True from the first moment: an adopted Vim's current tab
        # belongs to the user and must never be renamed over.
        FollowerState.set(
            window_id,
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
            window_id,
            "tmux",
            started.pane_id,
            origin=origin,
            on_failure=cfg.on_failure,
            speed=cfg.speed,
        )
    return FollowerState.get(window_id)


def _ensure_buffer(window_id: str, follower: Follower, file_path: str) -> None:
    """Switches the follower to file_path's tab (`:tab drop`). Used for Read
    navigation and binary files, where showing the real on-disk content
    immediately is exactly what's wanted — unlike a fresh text edit, which
    goes through show_fresh instead so the finished content is never flashed
    before it's typed. Always runs the preamble — it's cheap and
    self-healing, immune to the user having closed or reordered tabs since
    the last time this file was current."""
    follower.ensure_showing(file_path)
    FollowerState.update_current_file(window_id, file_path)


def _touch_and_evict(
    window_id: str, follower: Follower, current: FollowerState, file_path: str, max_tabs: int
) -> None:
    """Bump file_path to most-recent in the tab list and close whatever now
    falls past max_tabs. close_tab wipes the buffer on nvim (it has buffers,
    not tabs) and closes the tab on tmux — both backends implement the
    Follower protocol's close_tab, so eviction is generic here."""
    new_open, evicted = touch_open_files(current.open_files, file_path, max_tabs)
    for old in evicted:
        follower.close_tab(old)
    FollowerState.update(window_id, open_files=new_open, shown_any=True)


def _reconstruct_partial_fresh(content: str, completed_count: int) -> str:
    # Terminate every line with "\n" instead of joining with it: the returned
    # string is later `.splitlines()`'d again (by rewrite_buffer, and shown in
    # the interrupt notification), and a "\n".join round-trip is LOSSY for
    # trailing blank lines — ['a','',''] -> "a\n\n" -> ['a',''] drops one. The
    # terminating form is lossless: ['a','',''] -> "a\n\n\n" -> ['a','',''].
    return "".join(line + "\n" for line in content.splitlines()[:completed_count])


def _print_hook_context(context: str) -> None:
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


def _print_interrupt_notification(file_path: str, partial_content: str) -> None:
    _print_hook_context(
        f"The user interrupted the live preview of {file_path} while it was "
        "being written, edited it themselves, and SAVED their own version — "
        "it is now the file's content on disk. Only this much of your "
        f"version had been shown before they took over:\n\n{partial_content}\n\n"
        "Re-read the file from disk and build on the user's version; do not "
        "restore yours without asking."
    )


def _print_unchanged_save_notification(file_path: str) -> None:
    _print_hook_context(
        f"The user interrupted the live preview of {file_path}, reviewed it, "
        "and saved it unchanged — your version stands as the file's content "
        "on disk. Continue normally."
    )


_HANDOFF_POLL_SECONDS = 0.2
_HANDOFF_REMINDER_POLLS = 150  # ~30s at the poll cadence above
_HANDOFF_CUE = "Claude waiting \u2014 :w releases \u00b7 S discards"


def _await_user_handoff(
    current: FollowerState, window_id: str, file_path: str, after: str, partial_content: str
) -> None:
    """The user interrupted and owns the buffer: hold Claude's turn until
    they save their version (release Claude with it, via the notification)
    or press S again (discard their unsaved typing and resume following the
    file Claude wrote). A hook-timeout kill releases Claude without a
    notification — degraded but harmless."""
    control.mark_animating(window_id, state="handoff")
    try:
        initial_mtime = Path(file_path).stat().st_mtime_ns
    except OSError:
        initial_mtime = None
    # Durable cue: the 1.5s "Interrupted" popup is easy to miss, and a held
    # turn with no visible reason reads as Claude hanging. Put the release
    # instructions in the pane's border title for the whole wait (restored
    # on exit), and re-fire a popup reminder roughly every 30s.
    surface = status_surface_for(current)
    surface.set_state(_HANDOFF_CUE)
    polls = 0
    try:
        while True:
            polls += 1
            if polls % _HANDOFF_REMINDER_POLLS == 0:
                show_popup(current.target, _HANDOFF_CUE)
            signal = control.check_signal(window_id)
            if signal == "interrupt":
                # switch the cue from "waiting" to the replay before it runs
                surface.set_state("Writing...")
                # the des-interrupt: discard the user's unsaved typing and
                # put the show back on — rebuilding the interrupt-point
                # buffer instantly, then REPLAYING the remaining animation
                # at live pace. Without a stored remainder (stale state),
                # fall back to reloading the finished file.
                follower = get_follower(current.backend, current.target, window_id=window_id)
                pending = control.load_pending_animation(window_id)
                if pending is None:
                    follower.reload_and_relock(file_path)
                    FollowerState.update_current_file(window_id, file_path)
                    return
                rebuilt = follower.rewrite_buffer(file_path, partial_content)
                # rewrite_buffer rebuilds the buffer to EXACTLY the partial, so
                # there is no seed to strip — EXCEPT when the partial is empty
                # (interrupt before any line was typed): nvim can't hold a truly
                # empty buffer, so rewrite_buffer forces a single blank line,
                # which IS a seed the replay must type in front of and drop.
                seeded = partial_content == ""
                result = (
                    follower.resume(pending, seeded=seeded)
                    if rebuilt.outcome == "completed"
                    else rebuilt
                )
                if result.outcome == "completed":
                    FollowerState.update_current_file(window_id, file_path)
                else:
                    # Interrupted again mid-replay: hand the buffer over and
                    # release the turn — one hand-off wait per hook. The
                    # partial buffer self-heals at the next completed
                    # animation via the relock's :e!.
                    follower.hand_over()
                    FollowerState.update_current_file(window_id, None)
                return
            try:
                if Path(file_path).read_text() != after:
                    control.discard_pending_animation(window_id)
                    _print_interrupt_notification(file_path, partial_content)
                    return
                # A save is a save: a user who reviewed and kept Claude's
                # version (identical content, fresh mtime) must release the
                # turn too — content comparison alone held it forever.
                if (
                    initial_mtime is not None
                    and Path(file_path).stat().st_mtime_ns != initial_mtime
                ):
                    control.discard_pending_animation(window_id)
                    _print_unchanged_save_notification(file_path)
                    return
            except OSError:
                pass  # mid-save or momentarily unreadable: check again
            time.sleep(_HANDOFF_POLL_SECONDS)
    finally:
        surface.clear()
        control.clear_animating(window_id)


def cmd_hook_pre(env: dict[str, str], payload: dict[str, Any]) -> int:
    _configure_logging()
    if payload.get("tool_name") not in _EDIT_TOOLS:
        return 0
    window = TmuxWindow.from_env(env)
    if window is None:
        return 0
    raw = FollowerState.read(window.window_id)
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
    save_snapshot(window.window_id, file_path, before)
    return 0


def _register_writer(window_id: str, payload: dict[str, Any]) -> None:
    """Append this edit's writer identity+label to the window's append-only
    writers list if it is new. Safe to call on the skip path — it only grows
    the list; it never touches the border."""
    identity = writer_cue.writer_identity(payload)
    if identity is None:
        return
    current = FollowerState.read(window_id)
    if current is None or identity in current.writers:
        return
    FollowerState.update(
        window_id,
        writers=(*current.writers, identity),
        writer_labels=(*current.writer_labels, writer_cue.writer_label(payload)),
    )


def _apply_writer_cue(window_id: str, target: str, payload: dict[str, Any]) -> None:
    """When 2+ distinct writers have touched this window, render the
    animating writer's color and label on the window's status surface (a
    tmux pane border, or an nvim floating window). Best-effort: a missing
    identity or a surface failure never blocks the animation."""
    identity = writer_cue.writer_identity(payload)
    current = FollowerState.read(window_id)
    if identity is None or current is None or len(current.writers) < 2:
        return
    if identity not in current.writers:
        return
    surface = status_surface_for(current, target=target)
    color = writer_cue.color_for(current.writers, identity)
    surface.set_writer(writer_cue.writer_label(payload), color)


def _handle_hook_post_edit(env: dict[str, str], payload: dict[str, Any]) -> int:
    window = TmuxWindow.from_env(env)
    if window is None:
        return 0
    raw = FollowerState.read(window.window_id)
    if raw is not None and not raw.enabled:
        return 0
    file_path = _file_path(payload)
    if file_path is None:
        return 0
    cfg = config.load()
    if not _passes_policy(cfg, file_path):
        return 0
    if control.animating_state(window.window_id) is not None:
        # Another live hook owns this window's pane (parallel subagent or
        # background agent). Animating concurrently would interleave
        # keystrokes into one Vim, so skip this edit and drop the file
        # from open_files (a no-op if it wasn't tracked): its next touch
        # resyncs via a fresh retype instead of animating a diff over a
        # buffer we never updated.
        _register_writer(window.window_id, payload)
        current_state = FollowerState.read(window.window_id)
        if current_state is not None:
            # current_state can be None here: the .animating marker and the
            # .pane state file have decoupled lifecycles. A stop for this
            # window can clear .pane state while a still-live animator's
            # .animating marker survives (cmd_stop never touches its own
            # marker), or the animator can re-mark right after a stop
            # clears it. Either way there's nothing to update — skip.
            FollowerState.update(
                window.window_id,
                open_files=tuple(f for f in current_state.open_files if f != file_path),
            )
        return 0
    current = _get_active_follower(window.window_id) or _maybe_auto_open(
        window.window_id, env, file_path, cfg
    )
    if current is None:
        return 0
    _register_writer(window.window_id, payload)
    _apply_writer_cue(window.window_id, current.target, payload)
    try:
        raw_after = Path(file_path).read_bytes()
    except OSError as exc:
        logger.warning("failed to read %s: %s", file_path, exc)
        return 0

    follower = get_follower(
        current.backend,
        current.target,
        config.pace_seconds_for(current.speed),
        window_id=window.window_id,
    )
    is_fresh = file_path not in current.open_files
    binary = diff_module.is_binary(raw_after)

    # Consume any paused animation BEFORE animating: replaying it at pace 0
    # brings the buffer to the state the new edit's ops were computed
    # against. When the new edit targets another file, a different pending
    # file (a stale one this hook didn't leave behind), or a binary, the
    # buffer gets wiped/replaced (or isn't animated at all) anyway —
    # consuming without replaying is the correct discard.
    pending = control.load_pending_animation(window.window_id)
    if (
        pending is not None
        and (pending.file_path == file_path or pending.file_path == "")
        and not is_fresh
        and not binary
    ):
        # Both backends persist pending state now, so route the pace-0 catch-up
        # through the already-constructed backend follower (get_follower above).
        # seeded=True: the live interrupted show_fresh buffer still carries the
        # trailing seed blank (show_fresh only drops it on a completed outcome),
        # so _resume_fresh must type in front of it and drop it. A PendingApply
        # Edit ignores seeded entirely.
        follower.resume(dataclasses.replace(pending, pace_seconds=0.0), seeded=True)

    if binary:
        # Binary files are never animated, so it's safe to just navigate to
        # them normally (real content shown immediately, nothing to spoil).
        if is_fresh:
            _ensure_buffer(window.window_id, follower, file_path)
        _touch_and_evict(window.window_id, follower, current, file_path, cfg.max_tabs)
        return 0

    after = raw_after.decode("utf-8", errors="replace")

    # Evict/persist BEFORE animating so the tab shuffle never lands
    # mid-typing — touch_open_files never puts the just-touched file_path in
    # the evicted slice, so the file about to be animated can never be the
    # one just closed.
    _touch_and_evict(window.window_id, follower, current, file_path, cfg.max_tabs)

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
            FollowerState.update_current_file(window.window_id, None)
            partial = _reconstruct_partial_fresh(after, result.completed_count)
            # Keep the remainder around: a des-interrupt replays it from
            # the interrupt point instead of flashing the finished file.
            control.save_pending_show_fresh(
                window.window_id,
                tuple(after.splitlines())[result.completed_count :],
                config.pace_seconds_for(current.speed),
                continuation=result.completed_count > 0,
                file_path=file_path,
            )
            _await_user_handoff(current, window.window_id, file_path, after, partial)
        else:
            FollowerState.update_current_file(window.window_id, file_path)
        return 0

    before = load_snapshot(window.window_id, file_path)
    ops = diff_module.compute_edit_script(before, after)
    result = follower.apply_edit(file_path, ops)
    if result.outcome == "interrupted":
        FollowerState.update_current_file(window.window_id, None)
        # completed_count indexes the very ops list the animation walked —
        # computing the script once keeps this reconstruction truthful.
        partial = diff_module.apply_ops(before, ops[: result.completed_count])
        control.save_pending_apply_edit(
            window.window_id,
            ops[result.completed_count :],
            config.pace_seconds_for(current.speed),
            file_path=file_path,
        )
        _await_user_handoff(current, window.window_id, file_path, after, partial)
    return 0


def _handle_hook_post_read(env: dict[str, str], payload: dict[str, Any]) -> int:
    window = TmuxWindow.from_env(env)
    if window is None:
        return 0
    raw = FollowerState.read(window.window_id)
    if raw is not None and not raw.enabled:
        return 0
    file_path = _file_path(payload)
    if file_path is None:
        return 0
    cfg = config.load()
    if not _passes_policy(cfg, file_path):
        return 0
    if control.animating_state(window.window_id) is not None:
        # Another live hook owns this window's pane: navigating now would
        # interleave keystrokes with its animation. Skip; state untouched.
        return 0
    current = _get_active_follower(window.window_id) or _maybe_auto_open(
        window.window_id, env, file_path, cfg
    )
    if current is None:
        return 0
    follower = get_follower(current.backend, current.target)
    _ensure_buffer(window.window_id, follower, file_path)
    _touch_and_evict(window.window_id, follower, current, file_path, cfg.max_tabs)
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
