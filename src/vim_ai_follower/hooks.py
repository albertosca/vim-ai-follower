"""Claude Code PreToolUse/PostToolUse hook handlers that drive the live follower animation."""

from __future__ import annotations

import dataclasses
import json
import logging
import os
import subprocess
import time
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from vim_ai_follower import cache, config, control, keybindings, writer_cue
from vim_ai_follower import diff as diff_module
from vim_ai_follower.backends import Follower, get_follower
from vim_ai_follower.backends.nvim_connect import launch_standalone_nvim, resolve_nvim_target
from vim_ai_follower.backends.tmux_vim import TmuxVimFollower
from vim_ai_follower.session import Session, resolve_session
from vim_ai_follower.snapshot import load as load_snapshot
from vim_ai_follower.snapshot import save as save_snapshot
from vim_ai_follower.state import FollowerState, touch_open_files
from vim_ai_follower.status_surface import status_surface_for
from vim_ai_follower.tmux import adopt_target, show_popup

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


def _launch_standalone_or_log(window_id: str) -> str | None:
    """Open a standalone nvim window, or log the failure and return None — the
    launcher can exit non-zero (e.g. macOS Automation permission not yet granted
    for Terminal.app), and the hook must degrade to a no-op like the tmux-split
    path does, never raise an uncaught traceback into the tool run."""
    try:
        return launch_standalone_nvim(window_id)
    except subprocess.CalledProcessError as exc:
        logger.warning("standalone nvim launch failed for %s: %s", window_id, exc)
        return None


def _maybe_auto_open(session: Session, file_path: str, cfg: config.Config) -> FollowerState | None:
    if cfg.open_policy == "manual" or not _passes_policy(cfg, file_path):
        return None
    if not session.in_tmux:
        # Standalone: same decisions cmd_start makes outside tmux. No tmux
        # server to bind keys on (keybindings are tmux prefix-key bindings),
        # so registration is skipped here — only the in-tmux path below
        # registers them.
        if cfg.backend != "nvim":
            # The tmux backend drives a tmux split — nothing to attach to
            # standalone, and no tmux session to fail gracefully into.
            return None
        if cfg.nvim_window == "never":
            return None
        sock = _launch_standalone_or_log(session.window_id)
        if sock is None:
            return None
        FollowerState.set(
            session.window_id,
            "nvim",
            sock,
            origin="",
            on_failure=cfg.on_failure,
            speed=cfg.speed,
            adopted=False,
        )
        return FollowerState.get(session.window_id)
    origin = session.origin
    assert origin is not None  # in_tmux sessions always carry TMUX_PANE
    keybindings.register()
    if cfg.backend == "nvim":
        if cfg.nvim_window == "always":
            # nvim_window=always opens a standalone window even inside tmux —
            # honor it on the auto-open path too, matching cmd_start.
            sock = _launch_standalone_or_log(session.window_id)
            if sock is None:
                return None
            FollowerState.set(
                session.window_id,
                "nvim",
                sock,
                origin=origin,
                on_failure=cfg.on_failure,
                speed=cfg.speed,
                adopted=False,
            )
            return FollowerState.get(session.window_id)
        # Same adopt-or-launch selection as cmd_start, but from the hook path.
        try:
            sock, launched = resolve_nvim_target(
                origin, session.window_id, adopt=cfg.adopt_existing
            )
        except subprocess.CalledProcessError as exc:
            logger.warning("nvim auto-open failed from origin %s: %s", origin, exc)
            return None
        FollowerState.set(
            session.window_id,
            "nvim",
            sock,
            origin=origin,
            on_failure=cfg.on_failure,
            speed=cfg.speed,
            adopted=not launched,
        )
        return FollowerState.get(session.window_id)
    adopt = adopt_target(origin) if cfg.adopt_existing else None
    if adopt is not None:
        # shown_any=True from the first moment: an adopted Vim's current tab
        # belongs to the user and must never be renamed over.
        FollowerState.set(
            session.window_id,
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
            session.window_id,
            "tmux",
            started.pane_id,
            origin=origin,
            on_failure=cfg.on_failure,
            speed=cfg.speed,
        )
    return FollowerState.get(session.window_id)


def _ensure_buffer(window_id: str, follower: Follower, file_path: str) -> None:
    """Switches the follower to file_path's tab (`:tab drop` on tmux; an
    RPC-based tab lookup on nvim). Used for Read navigation and binary
    files, where showing the real on-disk content immediately is exactly
    what's wanted — unlike a fresh text edit, which goes through show_fresh
    instead so the finished content is never flashed before it's typed.
    Always runs the preamble — it's cheap and self-healing, immune to the
    user having closed or reordered tabs since the last time this file was
    current."""
    follower.ensure_showing(file_path)
    FollowerState.update_current_file(window_id, file_path)


def _touch_and_evict(
    window_id: str, follower: Follower, current: FollowerState, file_path: str, max_tabs: int
) -> None:
    """Bump file_path to most-recent in the tab list and close whatever now
    falls past max_tabs. close_tab wipes the buffer on nvim (which, since
    the buffer is that tab's only window, closes the tab too) and closes
    the tab directly on tmux — both backends implement the Follower
    protocol's close_tab, so eviction is generic here."""
    new_open, evicted = touch_open_files(current.open_files, file_path, max_tabs)
    for old in evicted:
        follower.close_tab(old)
    FollowerState.update(window_id, open_files=new_open, shown_any=True)


def _terminated(lines: Sequence[str]) -> str:
    # Terminate every line with "\n" instead of joining with it: the returned
    # string is later `.splitlines()`'d again (by rewrite_buffer, and shown in
    # the interrupt notification), and a "\n".join round-trip is LOSSY for
    # trailing blank lines — ['a','',''] -> "a\n\n" -> ['a',''] drops one. The
    # terminating form is lossless: ['a','',''] -> "a\n\n\n" -> ['a','',''].
    return "".join(line + "\n" for line in lines)


def _reconstruct_partial_fresh(content: str, completed_count: int) -> str:
    return _terminated(content.splitlines()[:completed_count])


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
    # Every partial arrives newline-terminated, and the template already puts
    # a blank line on each side of the quote — so drop exactly the terminator,
    # not every trailing newline: a partial whose last line is genuinely blank
    # must still show that blank line here.
    quoted = partial_content.removesuffix("\n")
    _print_hook_context(
        f"The user interrupted the live preview of {file_path} while it was "
        "being written, edited it themselves, and SAVED their own version — "
        "it is now the file's content on disk. Only this much of your "
        f"version had been shown before they took over:\n\n{quoted}\n\n"
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


def _poll_until_interrupt(
    current: FollowerState,
    session: Session,
    file_path: str,
    after: str,
    partial_content: str,
    initial_mtime: int | None,
) -> bool:
    """One hand-off wait. True when the user pressed S again (des-interrupt);
    False when they released the turn by saving, with the matching
    notification already printed."""
    polls = 0
    while True:
        polls += 1
        if polls % _HANDOFF_REMINDER_POLLS == 0 and session.in_tmux:
            show_popup(current.target, _HANDOFF_CUE)
        if control.check_signal(session.window_id) == "interrupt":
            return True
        try:
            if Path(file_path).read_text() != after:
                control.discard_pending_animation(session.window_id)
                _print_interrupt_notification(file_path, partial_content)
                return False
            # A save is a save: a user who reviewed and kept Claude's
            # version (identical content, fresh mtime) must release the
            # turn too — content comparison alone held it forever.
            if initial_mtime is not None and Path(file_path).stat().st_mtime_ns != initial_mtime:
                control.discard_pending_animation(session.window_id)
                _print_unchanged_save_notification(file_path)
                return False
        except OSError:
            pass  # mid-save or momentarily unreadable: check again
        time.sleep(_HANDOFF_POLL_SECONDS)


def _rearm_handoff(
    follower: Follower,
    window_id: str,
    pending: control.PendingApplyEdit | control.PendingShowFresh,
    partial_content: str,
    completed_count: int,
) -> str:
    """A des-interrupt replay was itself interrupted: persist the SHORTENED
    remainder (load_pending_animation consumed the old one), grow the partial
    by whatever the replay did manage to show, and hand the buffer back to the
    user. Returns the new partial; the caller loops into another hand-off wait
    with it, so interrupt/des-interrupt can repeat as often as the user likes."""
    if isinstance(pending, control.PendingShowFresh):
        # A resumed show_fresh counts LINES of the remainder (both backends
        # index the `lines` tuple they were handed: animate.run_lines and
        # nvim._animate_lines).
        partial_content += _terminated(pending.lines[:completed_count])
        control.save_pending_show_fresh(
            window_id,
            pending.lines[completed_count:],
            pending.pace_seconds,
            # continuation means "the buffer already holds earlier lines",
            # which is exactly "the partial is non-empty" — and the inverse of
            # the `seeded` the next rewrite_buffer/resume pair will compute.
            continuation=partial_content != "",
            file_path=pending.file_path,
            partial=partial_content,
        )
    else:
        # A resumed apply_edit counts OPS of the remainder, and each op's line
        # numbers are relative to the state the ops before it produced — so
        # replaying them onto the previous partial yields exactly the buffer
        # the user is now looking at.
        partial_content = diff_module.apply_ops(partial_content, pending.ops[:completed_count])
        control.save_pending_apply_edit(
            window_id,
            pending.ops[completed_count:],
            pending.pace_seconds,
            file_path=pending.file_path,
            partial=partial_content,
        )
    follower.hand_over()
    FollowerState.update_current_file(window_id, None)
    return partial_content


def _replay_remainder(
    current: FollowerState, session: Session, file_path: str, partial_content: str
) -> str | None:
    """The des-interrupt: discard the user's unsaved typing and put the show
    back on — rebuilding the interrupt-point buffer instantly, then REPLAYING
    the remaining animation at live pace. Without a stored remainder (stale
    state), fall back to reloading the finished file.

    None means this hand-off is over (the replay finished, or there was
    nothing to replay); a string is the new partial for another wait."""
    window_id = session.window_id
    follower = get_follower(current.backend, current.target, window_id=window_id)
    pending = control.load_pending_animation(window_id)
    if pending is None:
        follower.reload_and_relock(file_path)
        FollowerState.update_current_file(window_id, file_path)
        return None
    rebuilt = follower.rewrite_buffer(file_path, partial_content)
    if rebuilt.outcome != "completed":
        # Interrupted during the instant rebuild: none of the remainder ran,
        # so re-arm with it untouched. The half-rebuilt buffer needs no repair
        # — the next des-interrupt rewrites it from the same partial.
        return _rearm_handoff(follower, window_id, pending, partial_content, 0)
    # rewrite_buffer rebuilds the buffer to EXACTLY the partial, so there is
    # no seed to strip — EXCEPT when the partial is empty (interrupt before
    # any line was typed): nvim can't hold a truly empty buffer, so
    # rewrite_buffer forces a single blank line, which IS a seed the replay
    # must type in front of and drop.
    result = follower.resume(pending, seeded=partial_content == "")
    if result.outcome == "completed":
        FollowerState.update_current_file(window_id, file_path)
        return None
    return _rearm_handoff(follower, window_id, pending, partial_content, result.completed_count)


def _await_user_handoff(
    current: FollowerState, session: Session, file_path: str, after: str, partial_content: str
) -> None:
    """The user interrupted and owns the buffer: hold Claude's turn until
    they save their version (release Claude with it, via the notification)
    or press S again (discard their unsaved typing and resume following the
    file Claude wrote). A hook-timeout kill releases Claude without a
    notification — degraded but harmless.

    One iteration per interrupt/des-interrupt cycle, each owning its own
    (pending remainder, partial): a replay that is itself interrupted re-arms
    the wait instead of returning, so the second S is not the last one the
    hook listens to (live finding, 2026-09-16 — a mid-replay interrupt left
    nobody listening and the buffer partial until the next animation)."""
    window_id = session.window_id
    try:
        initial_mtime = Path(file_path).stat().st_mtime_ns
    except OSError:
        initial_mtime = None
    # The replay never writes the file, so initial_mtime stays valid across
    # cycles — and re-reading it after one would silently swallow a save that
    # landed during the replay.
    #
    # Durable cue: the 1.5s "Interrupted" popup is easy to miss, and a held
    # turn with no visible reason reads as Claude hanging. Put the release
    # instructions in the pane's border title for the whole wait (restored
    # on exit), and re-fire a popup reminder roughly every 30s. The popup
    # itself is a tmux display-popup — there is no tmux pane to target
    # standalone, so it's skipped there; the border/floating-window cue
    # above still carries the message.
    surface = status_surface_for(current)
    try:
        while True:
            # Re-marked every cycle: the replay's own animation envelope
            # clears the marker when it ends, so a later cycle would wait
            # unmarked and let a parallel hook claim this window's pane.
            control.mark_animating(window_id, state="handoff")
            surface.set_state(_HANDOFF_CUE)
            if not _poll_until_interrupt(
                current, session, file_path, after, partial_content, initial_mtime
            ):
                return
            # switch the cue from "waiting" to the replay before it runs
            surface.set_state("Writing...")
            replayed = _replay_remainder(current, session, file_path, partial_content)
            if replayed is None:
                return
            partial_content = replayed
    finally:
        surface.clear()
        control.clear_animating(window_id)


def cmd_hook_pre(env: dict[str, str], payload: dict[str, Any]) -> int:
    _configure_logging()
    if payload.get("tool_name") not in _EDIT_TOOLS:
        return 0
    session = resolve_session(env)
    if session is None:
        return 0
    raw = FollowerState.read(session.window_id)
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
    save_snapshot(session.window_id, file_path, before)
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


def _refresh_writer_cue(window_id: str, target: str, payload: dict[str, Any]) -> None:
    """Re-assert the surface after a COMPLETED animation. Stateless on
    purpose: pause/resume runs in another process (cmd_pause), whose surface
    save/restore dies with that process, so a stale transient body
    ("Paused", "Writing...") can outlive the animation on both backends.
    With 2+ writers the idempotent set_writer redraw wipes the transient
    while keeping the identity cue; with fewer there is no cue to keep, so
    clear the surface outright (a no-op on an already-neutral pane)."""
    current = FollowerState.read(window_id)
    if current is None:
        return
    if len(current.writers) >= 2:
        _apply_writer_cue(window_id, target, payload)
        return
    status_surface_for(current, target=target).clear()


def _handle_hook_post_edit(env: dict[str, str], payload: dict[str, Any]) -> int:
    session = resolve_session(env)
    if session is None:
        return 0
    raw = FollowerState.read(session.window_id)
    if raw is not None and not raw.enabled:
        return 0
    file_path = _file_path(payload)
    if file_path is None:
        return 0
    cfg = config.load()
    if not _passes_policy(cfg, file_path):
        return 0
    if not control.try_acquire_animating(session.window_id):
        # Another live hook already owns this window's pane. Six parallel
        # Write tool calls fire six hooks at once; the acquire is atomic, so
        # exactly one wins and animates while the rest land here, skip this
        # edit, and drop the file from open_files (a no-op if it wasn't
        # tracked): its next touch resyncs via a fresh retype instead of
        # animating a diff over a buffer we never updated. A plain
        # check-then-act let all six pass and interleave keystrokes into
        # garble (scripts/repro-concurrent-hooks.sh).
        _register_writer(session.window_id, payload)
        current_state = FollowerState.read(session.window_id)
        if current_state is not None:
            # current_state can be None here: the .animating marker and the
            # .pane state file have decoupled lifecycles. A stop for this
            # window can clear .pane state while a still-live animator's
            # .animating marker survives (cmd_stop never touches its own
            # marker), or the animator can re-mark right after a stop
            # clears it. Either way there's nothing to update — skip.
            FollowerState.update(
                session.window_id,
                open_files=tuple(f for f in current_state.open_files if f != file_path),
            )
        return 0
    try:
        return _animate_edit(payload, session, file_path, cfg)
    finally:
        # Release this window's animation slot on every path — including the
        # ones that never start an animation (no follower, unreadable/binary
        # file), which would otherwise hold the marker until the process
        # exits and needlessly block a concurrent hook in the meantime.
        control.clear_animating(session.window_id)


def _consume_pending_catchup(
    follower: Follower,
    file_path: str,
    pending: control.PendingApplyEdit | control.PendingShowFresh,
) -> None:
    """Silently fast-forward a crash-fallback remainder so the buffer holds
    the state the new edit's ops were computed against.

    Both backends persist pending state now, so this routes through whatever
    backend follower the caller built.

    Rebuilds from the PERSISTED partial first, exactly like the des-interrupt
    path (_replay_remainder) — never from the live buffer's shape. The hook
    that saved the remainder was killed, so nothing repaired the buffer: its
    tail is routinely a HALF-TYPED line (an interrupt no longer snaps the
    current line to its full text, and a kill mid-pause never could). Trusting
    that shape is what broke — _resume_fresh's `start_row = len(existing) - 1`
    targeted the seed blank instead of the partial line and pushed the
    leftover down (measured: ['ab','wx','wxyz','q'] for a 3-line file), and
    _run_ops's range delete, sized to the op's ORIGINAL range, stranded every
    extra row a partly-typed multi-line op had left (['a','WW','XX','YY','X',
    'c']). rewrite_buffer discards all of it and puts back exactly the prefix
    that was really shown.

    Deriving the partial here instead of persisting it does not work: for a
    PendingShowFresh, disk now holds the NEW edit's content, not the one the
    interrupted retype was typing, so "disk minus remainder" is the wrong
    prefix; for a PendingApplyEdit, only the remainder is persisted, so the
    applied prefix cannot be recomputed at all.

    partial=None means the saver could not record it (an older on-disk
    pending file, or the tmux animation drivers) — fall back to the previous
    live-buffer behavior rather than rebuilding to a partial we do not have.

    seeded says whether the buffer the replay starts on carries a trailing
    blank the replay must type in FRONT of and then drop. Both cases that
    reach it are falsy partials: None leaves the live interrupted show_fresh
    buffer, which still carries its seed blank (show_fresh only drops that on
    a completed outcome); an empty partial makes rewrite_buffer force a single
    blank line, since nvim cannot hold a truly empty buffer. A non-empty
    partial rebuilds seedless. (A PendingApplyEdit ignores seeded entirely.)"""
    if pending.partial is not None:
        follower.rewrite_buffer(file_path, pending.partial)
    follower.resume(dataclasses.replace(pending, pace_seconds=0.0), seeded=not pending.partial)


def _animate_edit(
    payload: dict[str, Any],
    session: Session,
    file_path: str,
    cfg: config.Config,
) -> int:
    """Animate one edit (show_fresh for a new file, apply_edit for a diff).
    The caller holds this window's animation slot for the whole call and
    releases it in a finally, so no parallel hook animates the same pane."""
    current = _get_active_follower(session.window_id) or _maybe_auto_open(session, file_path, cfg)
    if current is None:
        return 0
    _register_writer(session.window_id, payload)
    _apply_writer_cue(session.window_id, current.target, payload)
    try:
        raw_after = Path(file_path).read_bytes()
    except OSError as exc:
        logger.warning("failed to read %s: %s", file_path, exc)
        return 0

    follower = get_follower(
        current.backend,
        current.target,
        config.pace_seconds_for(current.speed),
        window_id=session.window_id,
    )
    is_fresh = file_path not in current.open_files
    binary = diff_module.is_binary(raw_after)

    # Consume any paused animation BEFORE animating: replaying it at pace 0
    # brings the buffer to the state the new edit's ops were computed
    # against. When the new edit targets another file, a different pending
    # file (a stale one this hook didn't leave behind), or a binary, the
    # buffer gets wiped/replaced (or isn't animated at all) anyway —
    # consuming without replaying is the correct discard.
    pending = control.load_pending_animation(session.window_id)
    if (
        pending is not None
        and (pending.file_path == file_path or pending.file_path == "")
        and not is_fresh
        and not binary
    ):
        _consume_pending_catchup(follower, file_path, pending)

    if binary:
        # Binary files are never animated, so it's safe to just navigate to
        # them normally (real content shown immediately, nothing to spoil).
        if is_fresh:
            _ensure_buffer(session.window_id, follower, file_path)
        _touch_and_evict(session.window_id, follower, current, file_path, cfg.max_tabs)
        return 0

    after = raw_after.decode("utf-8", errors="replace")

    # Show the "Writing..." cue for the whole animation, not just the
    # pause-resume/des-interrupt-replay special cases that happened to set
    # this string already. The completion refresh (_refresh_writer_cue,
    # already correct) clears it or re-asserts the writer cue once done.
    status_surface_for(current).set_state("Writing...")

    # Evict/persist BEFORE animating so the tab shuffle never lands
    # mid-typing — touch_open_files never puts the just-touched file_path in
    # the evicted slice, so the file about to be animated can never be the
    # one just closed.
    _touch_and_evict(session.window_id, follower, current, file_path, cfg.max_tabs)

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
            FollowerState.update_current_file(session.window_id, None)
            partial = _reconstruct_partial_fresh(after, result.completed_count)
            # Keep the remainder around: a des-interrupt replays it from
            # the interrupt point instead of flashing the finished file.
            control.save_pending_show_fresh(
                session.window_id,
                tuple(after.splitlines())[result.completed_count :],
                config.pace_seconds_for(current.speed),
                continuation=result.completed_count > 0,
                file_path=file_path,
                partial=partial,
            )
            _await_user_handoff(current, session, file_path, after, partial)
        else:
            FollowerState.update_current_file(session.window_id, file_path)
            _refresh_writer_cue(session.window_id, current.target, payload)
        return 0

    before = load_snapshot(session.window_id, file_path)
    ops = diff_module.compute_edit_script(before, after)
    # `before` goes along for the tmux backend: its driver cannot read the
    # buffer, so this is the only way its pause-time crash fallback can record
    # the applied prefix. It is the same string the interrupt path below
    # replays the ops onto, so the two can never disagree.
    result = follower.apply_edit(file_path, ops, before=before)
    if result.outcome == "interrupted":
        FollowerState.update_current_file(session.window_id, None)
        # completed_count indexes the very ops list the animation walked —
        # computing the script once keeps this reconstruction truthful.
        partial = diff_module.apply_ops(before, ops[: result.completed_count])
        control.save_pending_apply_edit(
            session.window_id,
            ops[result.completed_count :],
            config.pace_seconds_for(current.speed),
            file_path=file_path,
            partial=partial,
        )
        _await_user_handoff(current, session, file_path, after, partial)
    else:
        _refresh_writer_cue(session.window_id, current.target, payload)
    return 0


def _handle_hook_post_read(env: dict[str, str], payload: dict[str, Any]) -> int:
    session = resolve_session(env)
    if session is None:
        return 0
    raw = FollowerState.read(session.window_id)
    if raw is not None and not raw.enabled:
        return 0
    file_path = _file_path(payload)
    if file_path is None:
        return 0
    cfg = config.load()
    if not _passes_policy(cfg, file_path):
        return 0
    if control.animating_state(session.window_id) is not None:
        # Another live hook owns this window's pane: navigating now would
        # interleave keystrokes with its animation. Skip; state untouched.
        return 0
    current = _get_active_follower(session.window_id) or _maybe_auto_open(session, file_path, cfg)
    if current is None:
        return 0
    follower = get_follower(current.backend, current.target)
    _ensure_buffer(session.window_id, follower, file_path)
    _touch_and_evict(session.window_id, follower, current, file_path, cfg.max_tabs)
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
