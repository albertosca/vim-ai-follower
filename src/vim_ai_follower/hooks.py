"""Claude Code PreToolUse/PostToolUse hook handlers that drive the live follower animation."""

from __future__ import annotations

import contextlib
import dataclasses
import json
import logging
import os
import subprocess
import time
from collections.abc import Sequence
from pathlib import Path
from typing import Any, Protocol

from vim_ai_follower import binding, cache, config, control, keybindings, snapshot, writer_cue
from vim_ai_follower import diff as diff_module
from vim_ai_follower.backends import BufferProbe, Follower, get_follower, nvim_connect
from vim_ai_follower.backends.nvim_connect import (
    NvimNeverListened,
    launch_standalone_nvim,
    resolve_nvim_target,
)
from vim_ai_follower.backends.tmux_vim import TmuxVimFollower
from vim_ai_follower.session import Session, other_live_followers, resolve_session
from vim_ai_follower.snapshot import load as load_snapshot
from vim_ai_follower.snapshot import save as save_snapshot
from vim_ai_follower.state import FollowerState, touch_open_files
from vim_ai_follower.status_surface import StatusSurface, status_surface_for
from vim_ai_follower.tmux import adopt_target, show_popup

LOG_PATH = cache.CACHE_DIR / "hook.log"

logger = logging.getLogger("vim_ai_follower")

_EDIT_TOOLS = {"Edit", "MultiEdit", "Write"}

# How long one resolved identity stays quiet after warning about a lost window.
# Ten minutes, chosen against both failure modes: the live incident (2026-09-22)
# cost a second session half an hour, and at this interval such a window holds
# about three warnings — impossible to miss, impossible to mistake for noise.
# One minute would put a line in the log every few edits and train the reader
# to scroll past it; an hour risks a whole session leaving a single line that
# scrolls away above the real question. It also bounds the cost, since the
# scan behind the warning probes every registered follower's liveness.
IDENTITY_WARN_INTERVAL_SECONDS = 600


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


def _identity_warning_marker(window_id: str) -> Path:
    """Throttle marker for this identity's lost-window warning. The suffix is
    deliberately not ".pane": cmd_stop's orphan scan globs *.pane and would
    treat the marker as abandoned follower state."""
    return cache.CACHE_DIR / f"{window_id}.identity-warn"


def _claim_identity_warning(window_id: str) -> bool:
    """True at most once per IDENTITY_WARN_INTERVAL_SECONDS per identity.

    Claims the slot BEFORE the caller decides whether there is anything to warn
    about, which is what keeps the quiet case cheap: a genuinely standalone run
    with no follower anywhere would otherwise pay a full liveness scan on every
    single edit forever, since it never warns and so would never write a
    marker. The cost is that a follower registered elsewhere right after a
    fruitless scan goes unreported for up to one interval."""
    marker = _identity_warning_marker(window_id)
    try:
        if time.time() - marker.stat().st_mtime < IDENTITY_WARN_INTERVAL_SECONDS:
            return False
    except OSError:
        pass  # no marker yet (or unreadable) — treat as never warned
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.touch()
    return True


def _foreign_keys_warning_marker() -> Path:
    # Not ".pane": cmd_stop's orphan scan globs *.pane.
    return cache.CACHE_DIR / "keys-foreign-owner.warn"


def _claim_foreign_keys_warning() -> bool:
    """True at most once per IDENTITY_WARN_INTERVAL_SECONDS: a live foreign
    owner is a stable state `start` already announced, so every edit of a
    long turn repeating it would bury hook.log."""
    marker = _foreign_keys_warning_marker()
    try:
        if time.time() - marker.stat().st_mtime < IDENTITY_WARN_INTERVAL_SECONDS:
            return False
    except OSError:
        pass  # no marker yet (or unreadable) — treat as never warned
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.touch()
    return True


def _heal_keybindings() -> None:
    """Re-point the server-global prefix keys at THIS installation when they
    name a dead or superseded one — the version-stamped plugin root dies on
    `plugin update`. Runs before any animation so the keys controlling it
    already work. Never raises: hooks must not fail the tool call."""
    try:
        result = keybindings.claim()
    except (OSError, subprocess.CalledProcessError) as exc:
        logger.warning("keybinding heal failed: %s", exc)
        return
    note = keybindings.describe(result)
    if note is None:
        return
    if result.outcome is keybindings.ClaimOutcome.KEPT_FOREIGN:
        if _claim_foreign_keys_warning():
            logger.warning(note)
        return
    logger.info(note)


def _live_follower_healing_keys(session: Session) -> FollowerState | None:
    """_get_active_follower, plus the per-hook key heal for a LIVE follower
    only. Gating on mere state re-bound the server-global keys from windows
    whose follower pane had died; resolving liveness here costs nothing extra
    because the hook needs this answer anyway, and it still precedes any
    animation the keys would control. Auto-open claims with repair=True on
    its own path, so it is not healed twice."""
    current = _get_active_follower(session.window_id)
    if current is not None and session.in_tmux:
        _heal_keybindings()
    return current


def _warn_lost_window_identity(session: Session) -> None:
    """One log line for the give-up that used to be silent.

    Only fires for a SYNTHETIC identity (in_tmux False). That is the whole
    signal: a hook re-parented onto a `claude bg-spare` worker loses
    $TMUX_PANE, so resolve_session falls back to term-<TERM_SESSION_ID> and
    every lookup misses, while the real window's follower stays alive and
    enabled. An in-tmux window with no follower is the ordinary case — Alberto
    runs Claude in several windows with a follower in one — and logging that
    would bury this line in wallpaper.

    Never escalates into a guess about which window was meant: see
    docs on the Part 3 decision in tests/test_hook_identity_diagnostics.py.
    Best-effort throughout; the hook's return value never depends on it."""
    if session.in_tmux:
        return
    if not _claim_identity_warning(session.window_id):
        return
    others = other_live_followers(session.window_id)
    if not others:
        return
    logger.warning(
        "no follower registered for resolved identity %s (in_tmux=%s, no TMUX_PANE in this "
        "hook's environment), but %d other identity/identities have a live follower: %s — "
        "this session most likely lost its tmux window identity, so edits resolve against a "
        "synthetic id and nothing animates; the follower itself is fine",
        session.window_id,
        session.in_tmux,
        len(others),
        ", ".join(f"{o.window_id} ({o.backend} {o.target})" for o in others),
    )


def _payload_session_id(payload: dict[str, Any]) -> str | None:
    """The hook payload's top-level session id, or None when it carries none.

    Deliberately NOT writer_cue.writer_identity, which prefers agent_id. That
    preference is right for the writer cue — two agents animating into one
    window need two colors — and wrong here: a subagent shares its parent
    session's process and therefore its window, so a per-agent binding would
    store the same fact under a second key that then ages on its own schedule
    and invents a way for the two to disagree."""
    value = payload.get("session_id")
    return value if isinstance(value, str) and value else None


def _recover_window_from_binding(session: Session, session_id: str) -> Session | None:
    """The window this session remembered while it could still prove it, or
    None when there is nothing to recall or the remembered window no longer
    has a live follower.

    This is recall, not inference: binding.recall only answers for a binding
    this same session_id wrote under the tmux server that is still running, so
    the window id here was MEASURED by this session from its own $TMUX_PANE.
    That is what separates it from picking the one live follower that happens
    to be out there — see test_a_sole_other_follower_is_never_borrowed.

    FollowerState.get costs a real backend round-trip (a tmux shell-out, an
    nvim RPC connect), which is why it is reached only after a binding was
    actually found: a session that never wrote one pays a missing-file read
    per hook and nothing else."""
    window_id = binding.recall(session_id)
    if window_id is None:
        return None
    live = FollowerState.get(window_id)
    if live is None:
        return None
    logger.warning(
        "recovered window %s for session %s from a stored session->window binding: this "
        "hook's environment carries no TMUX_PANE, so the identity resolved to %s and nothing "
        "would have animated. The binding was written by this same session while it could "
        "still see its own pane, under the tmux server still running now — if an edit lands "
        "in the wrong editor, this line is the reason",
        window_id,
        session_id,
        session.window_id,
    )
    return Session(window_id=window_id, origin=live.origin, in_tmux=True)


def _resolve_session_for(env: dict[str, str], payload: dict[str, Any]) -> Session | None:
    """resolve_session, plus the session→window binding in both directions.

    The single entry point every hook uses, so pre and post can never disagree
    about which window this edit belongs to (they would otherwise diff a fresh
    buffer against an empty snapshot and retype the whole file every time).

    Four branches, in order:
      1. Nothing resolved at all — pass the None through; every caller exits 0.
      2. No session id in the payload — nothing to key a binding on.
      3. In tmux: the window is proved, so record it for later.
      4. Not in tmux: try to recover the window a previous hook of this same
         session proved. On any doubt, fall through to the session as resolved,
         which is exactly today's behavior (synthetic identity + the existing
         lost-identity warning)."""
    session = resolve_session(env)
    if session is None:
        return None
    session_id = _payload_session_id(payload)
    if session_id is None:
        return session
    if session.in_tmux:
        binding.remember(session_id, session.window_id)
        return session
    return _recover_window_from_binding(session, session_id) or session


def _launch_standalone_or_log(window_id: str) -> str | None:
    """Open a standalone nvim window, or log the failure and return None — the
    launcher can exit non-zero (e.g. macOS Automation permission not yet granted
    for Terminal.app), and the hook must degrade to a no-op like the tmux-split
    path does, never raise an uncaught traceback into the tool run."""
    if nvim_connect.launch_backoff_active(window_id):
        return None  # a recent launch failed: already logged, not retried yet
    try:
        return launch_standalone_nvim(
            window_id, wait_seconds=nvim_connect.AUTO_OPEN_SOCKET_WAIT_SECONDS
        )
    except (subprocess.CalledProcessError, NvimNeverListened) as exc:
        # Every hook blocks the tool call it runs in: retrying on each one
        # stalled every Edit/Write/Read for the whole wait. Back off, once.
        nvim_connect.record_launch_failure(window_id)
        logger.warning(
            "standalone nvim launch failed for %s: %s — not auto-opening %s again for "
            "%d minutes (`claude-follow start` retries at once)",
            window_id,
            exc,
            window_id,
            nvim_connect.LAUNCH_BACKOFF_SECONDS // 60,
        )
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
    # Guarded like _heal_keybindings: a bind-key failure must not escape the
    # hook, and the follower still opens — it works without the prefix keys.
    try:
        claimed = keybindings.claim(repair=True)
    except (OSError, subprocess.CalledProcessError) as exc:
        logger.warning("keybinding claim failed: %s", exc)
    else:
        note = keybindings.describe(claimed)
        if note is not None:
            logger.info(note)
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
    """Switches the follower to file_path's tab (a by-number buffer switch,
    or `:tab drop` for a file no buffer holds yet, on tmux; an RPC-based tab
    lookup on nvim). Used for Read navigation and binary files, where
    showing the real on-disk content is exactly what's wanted. Both backends
    re-read an already-open buffer when it is CLEAN, so a file rewritten
    outside Claude's Edits is not shown stale, and leave one holding unsaved
    user typing alone: tmux through the swap-guarded `_RELOAD_IF_CLEAN`,
    nvim through `_reload_if_clean`, where "clean" also covers a buffer
    holding only follower text (it is never written, so always 'modified').
    A fresh text edit
    never comes here, and neither does an Edit whose buffer does not hold
    the diff's base (listed but unloaded, or changed on disk outside
    Claude — see _probe_base): both go through show_fresh (or, in an
    adopted editor, are left alone — _leave_adopted_buffer_alone), so the
    finished content is never flashed before it's typed.
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
HANDOFF_CUE = "Claude waiting \u2014 :w releases \u00b7 S discards"
# The same cue when the handed-over buffer is readonly by the USER's own
# setting (an adopted editor): a plain `:w` fails there with E45.
HANDOFF_CUE_READONLY = "Claude waiting \u2014 :w! releases \u00b7 S discards"

# Shown when an adopted editor's buffer is not the base of Claude's edit (see
# _leave_adopted_buffer_alone). 40 characters: measured in a real 49-column
# tmux pane with pane-border-status top, it renders whole inside the border
# title (`┬──2 "…"` plus the mouse controls) where 45 lost its last letter.
BASE_DIFFERS_CUE = "buffer differs \u2014 :e! shows Claude's edit"
# Shown instead when nothing is known to differ: the probe never answered, or a
# leftover remainder recorded no partial to check against. `:e!` would not
# help (the next probe can fail the same way), so the cue does not offer it.
# 35 characters, measured whole in the same 49-column border.
PROBE_UNKNOWN_CUE = "can't check buffer \u2014 edit not shown"


def _handoff_cue(follower: Follower, file_path: str) -> str:
    """The hand-off cue for file_path's buffer as the user now holds it: a
    plain `:w` fails with E45 on a readonly the USER set (Alberto's decision,
    2026-09-28), so that buffer's cue asks for `:w!`. Asked once per hand-off
    cycle; a failure to ask is the plain cue, never a failed hook."""
    try:
        readonly = follower.user_readonly(file_path)
    except Exception:
        logger.exception("could not ask whether %s is readonly", file_path)
        readonly = False
    return HANDOFF_CUE_READONLY if readonly else HANDOFF_CUE


def _poll_until_interrupt(
    current: FollowerState,
    session: Session,
    file_path: str,
    after: str,
    partial_content: str,
    initial_mtime: int | None,
    cue: str = HANDOFF_CUE,
) -> bool:
    """One hand-off wait. True when the user pressed S again (des-interrupt);
    False when they released the turn by saving, with the matching
    notification already printed."""
    polls = 0
    while True:
        polls += 1
        if polls % _HANDOFF_REMINDER_POLLS == 0 and session.in_tmux:
            show_popup(current.target, cue)
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
        # reload_and_relock re-reads disk, so the buffer is back in sync even
        # if the interrupted animation had been a stale file's retype.
        FollowerState.clear_stale(window_id, file_path)
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
        # Same as the completed animation in _animate_edit: the replay
        # finished the file, so any stale mark on it no longer holds.
        FollowerState.clear_stale(window_id, file_path)
        FollowerState.update_current_file(window_id, file_path)
        return None
    return _rearm_handoff(follower, window_id, pending, partial_content, result.completed_count)


def _await_user_handoff(
    current: FollowerState,
    session: Session,
    file_path: str,
    after: str,
    partial_content: str,
    surface: StatusSurface | None = None,
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
    # Pass the animation's own surface: a fresh one would save the animation's
    # "Writing..." as the title to restore, and the release would leave the
    # border saying "Writing..." with nothing writing (found recording the
    # demo, 2026-09-23). The tmux surface saves only on its FIRST set_state.
    if surface is None:
        surface = status_surface_for(current)
    follower = get_follower(current.backend, current.target, window_id=window_id)
    try:
        while True:
            # Re-marked every cycle: the replay's driver marks "running".
            # The slot itself stays this hook's until the hook's own exit
            # (_handle_hook_post_edit), so a parallel hook never claims the
            # pane between cycles.
            control.mark_animating(window_id, state="handoff")
            cue = _handoff_cue(follower, file_path)
            surface.set_state(cue)
            if not _poll_until_interrupt(
                current, session, file_path, after, partial_content, initial_mtime, cue
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


class _Readable(Protocol):
    def read(self) -> str: ...


def run_hook(hook_command: str, env: dict[str, str], stdin: _Readable) -> int:
    """Entry point for the `hook pre|post|failure` CLI subcommands, and ONLY those:
    the design intent ("Hooks never fail a tool call", guide/en/faq.md) does
    not cover start/stop/status/etc, which must keep raising normally.

    This ALWAYS returns 0 — every step that can fail is inside the one try:
    `_configure_logging()` itself (an unwritable cache dir, a full disk),
    reading stdin (a UnicodeDecodeError from non-UTF-8 bytes, or any other
    I/O error — `stdin.read()` is called in here, not by the caller, so a
    raising read is caught too), malformed JSON (cli.py's old bare
    `json.loads`), and any exception raised inside cmd_hook_pre/
    cmd_hook_post. All of these used to propagate out of `main`, exiting 1
    with a traceback that Claude Code reports as a hook error (BACKLOG
    "Hooks have no top-level catch").

    The except handler's own attempt to log is wrapped in a second,
    unconditional try: if hook.log itself is what's broken (the same full
    disk, the same unwritable directory), there is nothing left to write —
    but that second failure must not escape either, or a broken log would
    defeat the one guarantee this function exists to make."""
    try:
        _configure_logging()
        payload: dict[str, Any] = json.loads(stdin.read())
        if hook_command == "pre":
            return cmd_hook_pre(env, payload)
        if hook_command == "failure":
            return cmd_hook_failure(env, payload)
        return cmd_hook_post(env, payload)
    except Exception:
        with contextlib.suppress(Exception):
            logger.exception("hook %s crashed", hook_command)
        return 0


def cmd_hook_pre(env: dict[str, str], payload: dict[str, Any]) -> int:
    _configure_logging()
    if payload.get("tool_name") not in _EDIT_TOOLS:
        return 0
    session = _resolve_session_for(env, payload)
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
    # Until this edit's post hook runs, a Read of the file must not re-read
    # it into the follower (_edit_in_flight). The writer is recorded so that
    # writer's own later Read can tell the mark is stale.
    snapshot.mark_in_flight(session.window_id, file_path, writer_cue.writer_identity(payload) or "")
    return 0


def cmd_hook_failure(env: dict[str, str], payload: dict[str, Any]) -> int:
    """PostToolUseFailure (the tool was allowed and failed while executing)
    and PermissionDenied (auto mode denied it): the Edit will never post, so
    its in-flight mark is dropped. Clearing is always safe, so neither the
    enabled flag nor the open policy is consulted — a toggle or a config
    change between pre and here must not strand the mark.

    Measured on Claude Code 2.1.284: a user answering "No" at a permission
    prompt fires neither event; that mark is dropped by the same writer's
    next Read (_edit_in_flight) or expires."""
    _configure_logging()
    if payload.get("tool_name") not in _EDIT_TOOLS:
        return 0
    session = _resolve_session_for(env, payload)
    if session is None:
        return 0
    file_path = _file_path(payload)
    if file_path is None:
        return 0
    snapshot.clear_in_flight(session.window_id, file_path)
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
    session = _resolve_session_for(env, payload)
    if session is None:
        return 0
    file_path = _file_path(payload)
    if file_path is None:
        return 0
    raw = FollowerState.read(session.window_id)
    if raw is not None and not raw.enabled:
        # The edit has posted and nothing will animate it: drop its mark, or
        # it would silence Reads of the file until it expired.
        snapshot.clear_in_flight(session.window_id, file_path)
        return 0
    cfg = config.load()
    if not _passes_policy(cfg, file_path):
        snapshot.clear_in_flight(session.window_id, file_path)
        return 0
    try:
        acquired = control.try_acquire_animating(session.window_id)
    finally:
        # The edit has posted, so a Read may re-read its file again. Cleared
        # only AFTER the acquire: from pre to here the mark keeps a Read off
        # this file (_edit_in_flight), and from here on the slot keeps every
        # Read off the pane — the gap between them would be the flash this
        # mark exists to stop. A raising acquire clears it too (the edit
        # will not be animated). Not held any longer than this: a hook killed
        # later (a hook timeout in the hand-off wait) never reaches a
        # `finally`, and its mark would then silence Reads until it expired.
        snapshot.clear_in_flight(session.window_id, file_path)
    if not acquired:
        # Another live hook already owns this window's pane. Six parallel
        # Write tool calls fire six hooks at once; the acquire is atomic, so
        # exactly one wins and animates while the rest land here and skip
        # this edit. A plain check-then-act let all six pass and interleave
        # keystrokes into garble (scripts/repro-concurrent-hooks.sh).
        #
        # The skipped file's buffer is now behind disk, so mark it stale: its
        # next touch resyncs via a fresh retype instead of animating a diff
        # over a buffer we never updated. Marking is all this branch may do.
        # Closing the tab would send keystrokes into a pane another hook is
        # mid-animation on, which is the whole reason the slot lock exists;
        # and dropping the file from open_files — what this used to do — took
        # the tab out of the eviction candidate list while Vim still held it,
        # stranding it forever (mark_stale's docstring has the measurement).
        # mark_stale handles a missing .pane file and an untracked path.
        _register_writer(session.window_id, payload)
        FollowerState.mark_stale(session.window_id, file_path)
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
) -> bool:
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
    partial rebuilds seedless. (A PendingApplyEdit ignores seeded entirely.)

    reload=False: the replay must END at the new edit's base. Disk already
    holds the new edit, so a relock that re-reads it (tmux's `:e!`) would
    show the finished file and make the base probe below answer "differs".

    True means the catch-up ran to the end, so the buffer now holds only
    follower text (_animate_edit may then reload it from disk)."""
    rebuilt = True
    if pending.partial is not None:
        rebuilt = follower.rewrite_buffer(file_path, pending.partial).outcome == "completed"
    result = follower.resume(
        dataclasses.replace(pending, pace_seconds=0.0), seeded=not pending.partial, reload=False
    )
    return rebuilt and result.outcome == "completed"


def _probe_base(follower: Follower, file_path: str, before: str) -> BufferProbe:
    """What the follower's buffer for file_path holds relative to `before`,
    the base the edit script is computed against. FollowerState cannot see
    either way the buffer stops being that base: a buffer listed but
    UNLOADED (its tab closed under 'nohidden' — navigating to it would load
    the finished file from disk first), or one that is loaded with other
    content (a formatter, `sed -i` or a checkout rewrote the file outside
    Claude; or, in an adopted editor, the user typed into it). A probe that
    raises is "unknown": hooks never fail the tool call."""
    try:
        return follower.probe_buffer(file_path, before)
    except Exception:
        logger.exception("could not ask the follower what %s's buffer holds", file_path)
        return "unknown"


def _leave_adopted_buffer_alone(
    current: FollowerState, window_id: str, file_path: str, probe: BufferProbe, why: str
) -> None:
    """An ADOPTED editor's buffer for file_path is not known to be the edit's
    base (Alberto's policy, 2026-09-25): never wipe it, never animate into it.

    A retype starts with show_fresh's `bwipeout!`, and a catch-up replay with
    rewrite_buffer — both discard what the buffer holds, and in the user's own
    editor that may be their unsaved typing. nvim follower buffers are never
    written, so 'modified' cannot tell the user's typing from the follower's,
    and the rule applies to a clean buffer changed outside Claude too. A diff
    onto the wrong base garbles it. So the file is marked stale (a no-op for
    a file the follower never opened: stale is a subset of open), the reason
    is logged, and the status surface says what happened: "differs" gets the
    cue that offers `:e!`; "unknown" gets its own, because nothing is known to
    differ and `:e!` cannot make an unanswered probe answer.

    The cue stays until the follower next animates in this window
    (_animate_edit retires it before "Writing..."), so it outlives this hook
    instead of being cleared by it. The next edit of the file probes again,
    so once `:e!` makes the buffer the base it animates as a diff."""
    FollowerState.mark_stale(window_id, file_path)
    logger.warning(
        "adopted editor: left the buffer for %s alone and marked it stale (%s: %s)",
        file_path,
        probe,
        why,
    )
    cue = PROBE_UNKNOWN_CUE if probe == "unknown" else BASE_DIFFERS_CUE
    status_surface_for(current).set_state(cue)


def _ground_caught_up_buffer(
    window_id: str,
    follower: Follower,
    current: FollowerState,
    file_path: str,
    probe: BufferProbe,
    cfg: config.Config,
) -> None:
    """A catch-up just completed, but the buffer it left is still not this
    edit's base: something outside Claude (a formatter hook, `sed -i`, a
    checkout) rewrote the file after the remainder was saved. Reload it from
    disk: Claude's finished file is shown, with no animation, no cue and no
    stale mark, in adopted and dedicated mode alike (controller ruling,
    2026-09-25).

    Safe in an adopted editor, unlike _leave_adopted_buffer_alone's case: the
    partial probe said "holds" and the follower then typed the rest, so the
    buffer holds only follower text. Left alone it would stay modified while
    disk moved on — the next `:checktime` (FocusGained with 'autoread') raises
    the blocking W12 dialog, and a `:w` writes that stale text over Claude's
    and the formatter's edits (reproduced 2026-09-25).

    "unknown" (the editor did not answer the probe) is grounded in an adopted
    editor by the same reasoning: the buffer holds nothing of the user's, and
    leaving it alone would leave it modified, the W12 hazard above. A
    dedicated follower keeps retyping on "unknown", as for any buffer it
    cannot check: show_fresh's own relock ends in the same disk sync, so it is
    grounded either way, and the edit stays visible."""
    logger.info(
        "reloaded %s from disk after a catch-up: its buffer %s",
        file_path,
        "is not the edit's base (changed outside Claude since the remainder was saved)"
        if probe == "differs"
        else "could not be checked",
    )
    try:
        follower.reload_from_disk(file_path)
    except Exception:
        logger.exception("could not reload %s after a catch-up", file_path)
        return
    # Never stale here: a stale file is fresh, and a fresh file gets no
    # catch-up. Nor is it marked: the buffer now matches disk.
    FollowerState.update_current_file(window_id, file_path)
    status_surface_for(current).retire_state(BASE_DIFFERS_CUE, PROBE_UNKNOWN_CUE)
    _touch_and_evict(window_id, follower, current, file_path, cfg.max_tabs)


_WHY = {
    "differs": "it holds other content than the edit's base",
    "unknown": "the editor did not answer the buffer probe",
}


def _animate_edit(
    payload: dict[str, Any],
    session: Session,
    file_path: str,
    cfg: config.Config,
) -> int:
    """Animate one edit (show_fresh for a new file, apply_edit for a diff).
    The caller holds this window's animation slot for the whole call and
    releases it in a finally, so no parallel hook animates the same pane."""
    current = _live_follower_healing_keys(session) or _maybe_auto_open(session, file_path, cfg)
    if current is None:
        _warn_lost_window_identity(session)
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
    # "Fresh" means the follower holds no buffer this edit's diff could be
    # applied onto, so the whole file is retyped; a diff would land on the
    # wrong base. Known here from state: the file was never opened, or its
    # tab's buffer is one a skipped edit left behind (stale_files). What state
    # cannot know is asked of the editor further down (_probe_base).
    is_fresh = file_path not in current.open_files or file_path in current.stale_files
    binary = diff_module.is_binary(raw_after)

    # Consume any paused animation BEFORE animating: replaying it at pace 0
    # brings the buffer to the state the new edit's ops were computed
    # against. When the new edit targets another file, a different pending
    # file (a stale one this hook didn't leave behind), or a binary, the
    # buffer gets wiped/replaced (or isn't animated at all) anyway —
    # consuming without replaying is the correct discard.
    #
    # The replay starts with rewrite_buffer(partial), which discards whatever
    # the buffer holds. A dedicated follower's buffer is its own; an ADOPTED
    # editor's may hold what the user typed after the hand-off or pause that
    # left this remainder behind. So there the replay runs only onto a buffer
    # that still holds the partial; otherwise the remainder is dropped (the
    # load above consumed it) and the buffer is left alone. A vanished
    # buffer ("absent") has nothing to lose and nothing to replay onto: the
    # probe below sends the new edit to the retype.
    pending = control.load_pending_animation(session.window_id)
    caught_up = False
    if (
        pending is not None
        and (pending.file_path == file_path or pending.file_path == "")
        and not is_fresh
        and not binary
    ):
        if not current.adopted:
            caught_up = _consume_pending_catchup(follower, file_path, pending)
        elif pending.partial is None:
            _leave_adopted_buffer_alone(
                current,
                session.window_id,
                file_path,
                "unknown",
                "a leftover remainder recorded no partial to check the buffer against",
            )
            return 0
        else:
            held = _probe_base(follower, file_path, pending.partial)
            if held == "holds":
                caught_up = _consume_pending_catchup(follower, file_path, pending)
            elif held != "absent":
                _leave_adopted_buffer_alone(
                    current,
                    session.window_id,
                    file_path,
                    held,
                    f"{_WHY[held]} (a leftover remainder's partial); dropped the remainder",
                )
                return 0

    if binary:
        # Binary files are never animated, so it's safe to just navigate to
        # them normally (real content shown immediately, nothing to spoil) —
        # fresh or not: an already-open one was rewritten on disk and its
        # clean buffer must be re-read, or the old bytes stay on screen.
        _ensure_buffer(session.window_id, follower, file_path)
        _touch_and_evict(session.window_id, follower, current, file_path, cfg.max_tabs)
        return 0

    after = raw_after.decode("utf-8", errors="replace")

    # After the catch-up above, never before it: the replay is what brings
    # the buffer to this edit's base, and probing first would see its
    # half-typed partial and throw the catch-up away. And before anything is
    # drawn or evicted, because an adopted editor's buffer that is not the
    # base gets no animation at all.
    #
    # A dedicated follower owns its buffers: anything but "holds" is retyped,
    # and a fresh file is retyped without asking. An ADOPTED editor is asked
    # even for a fresh file, because show_fresh's wipe would take a buffer the
    # user opened there themselves; only "absent" (nothing in it to lose) may
    # be retyped, and "holds" — including a stale file the user resynced with
    # `:e!` — is a diff onto the user's own buffer.
    before = load_snapshot(session.window_id, file_path)
    if not is_fresh or current.adopted:
        probe = _probe_base(follower, file_path, before)
        if probe == "holds":
            if file_path in current.stale_files:
                FollowerState.clear_stale(session.window_id, file_path)
            is_fresh = False
        elif caught_up and (probe == "differs" or (probe == "unknown" and current.adopted)):
            _ground_caught_up_buffer(session.window_id, follower, current, file_path, probe, cfg)
            return 0
        elif current.adopted and probe != "absent":
            _leave_adopted_buffer_alone(current, session.window_id, file_path, probe, _WHY[probe])
            return 0
        else:
            is_fresh = True

    # Show the "Writing..." cue for the whole animation, not just the
    # pause-resume/des-interrupt-replay special cases that happened to set
    # this string already. The completion refresh (_refresh_writer_cue,
    # already correct) clears it or re-asserts the writer cue once done.
    surface = status_surface_for(current)
    # An earlier edit's leave-alone cue is not this animation's to keep: left
    # in place, a tmux surface would save it as the title to restore and a
    # hand-off release would put it back on the border.
    surface.retire_state(BASE_DIFFERS_CUE, PROBE_UNKNOWN_CUE)
    surface.set_state("Writing...")

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
            # still a diff (apply_edit) against the pre-edit snapshot as long
            # as the buffer holds that snapshot once any pending catch-up has
            # run (_probe_base); otherwise it is retyped (or, in an adopted
            # editor, left alone). Even a
            # killed handoff self-heals: the partial
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
            _await_user_handoff(current, session, file_path, after, partial, surface)
        else:
            # The retype ran to the end, so whatever was stale about this
            # buffer is gone — it now holds the whole file.
            FollowerState.clear_stale(session.window_id, file_path)
            FollowerState.update_current_file(session.window_id, file_path)
            _refresh_writer_cue(session.window_id, current.target, payload)
        return 0

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
        _await_user_handoff(current, session, file_path, after, partial, surface)
    else:
        _refresh_writer_cue(session.window_id, current.target, payload)
    return 0


def _edit_in_flight(window_id: str, file_path: str, reader: str | None) -> bool:
    """True when a Read of file_path must leave the follower alone: an Edit,
    MultiEdit or Write of it has run its pre hook and written the file, and
    its post hook has not run yet (BACKLOG D2 — two agents on one window).

    Holding the animation slot from pre to post would close the same gap, but
    the pre hook's process exits at once, so nothing could hold it, and a
    tool call that never posts (denied, failed) would hold it until a timeout
    — blocking every other agent's edits in the window, not just Reads of one
    file. The mark blocks nothing: a Read that skips here still returns at
    once, and the Edit's post hook, moments away, is what brings the file on
    screen, animated from the buffer the re-read would have overwritten.

    Both backends re-read a CLEAN open buffer on a Read (tmux's
    `_RELOAD_IF_CLEAN`, nvim's `_reload_if_clean`), and a file no buffer
    holds is opened from disk; either way the finished file would be on
    screen before the Edit's post hook typed it, and its base probe would
    then find a buffer that is not its base: a dedicated follower wiped and
    retyped it, an adopted one left it alone with the "buffer differs" cue
    and never showed the edit.

    Only a file that no longer holds the snapshot is held back: before the
    write (a permission prompt still open) or after an Edit that was denied
    or failed and so never posts, a re-read shows exactly the edit's base,
    and the Read goes ahead. That keeps a stale mark nearly free until it
    expires (snapshot.IN_FLIGHT_TTL_SECONDS).

    A Read by the mark's OWN writer (`reader`, the payload's writer
    identity) proves the mark stale: one agent's tool calls run one after
    another, post hook included (measured on Claude Code 2.1.284 with an Edit
    and a Read issued in one message), so that writer's Edit tool call is
    over — it posted (which would have cleared the mark), failed, or was
    denied. A user's "No" at the permission prompt fires no hook at all, so
    this is what lets the usual fallback (`sed -i` through Bash, then a Read
    to check) resync the follower instead of being silenced until expiry.
    The mark is dropped and the Read goes ahead."""
    if not snapshot.in_flight(window_id, file_path):
        return False
    writer = snapshot.in_flight_writer(window_id, file_path)
    if reader and writer == reader:
        snapshot.clear_in_flight(window_id, file_path)
        logger.info(
            "dropped the in-flight mark on %s: its own writer read the file, so that "
            "edit's tool call is over (denied or failed)",
            file_path,
        )
        return False
    try:
        on_disk = Path(file_path).read_text()
    except (OSError, UnicodeDecodeError):
        # The pre hook snapshots an unreadable file as "", and the Read tool
        # just read it, so it exists: it is not the snapshot any more.
        return True
    return on_disk != load_snapshot(window_id, file_path)


def _handle_hook_post_read(env: dict[str, str], payload: dict[str, Any]) -> int:
    session = _resolve_session_for(env, payload)
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
    if _edit_in_flight(session.window_id, file_path, writer_cue.writer_identity(payload)):
        # What is known: another writer's Edit/MultiEdit/Write of this file
        # ran its pre hook less than IN_FLIGHT_TTL_SECONDS ago and has not
        # posted, and the file no longer holds that edit's snapshot. Whether
        # it is about to post or was denied is not known here.
        logger.info(
            "skipped the Read of %s: an Edit/Write of it is in flight or failed "
            "(another writer's pre hook ran, no post hook yet, and the file changed since)",
            file_path,
        )
        return 0
    current = _live_follower_healing_keys(session) or _maybe_auto_open(session, file_path, cfg)
    if current is None:
        _warn_lost_window_identity(session)
        return 0
    # window_id is what lets an NvimFollower read its FollowerState: without
    # it an adopted nvim looks dedicated (locked on Read, its prompts answered).
    follower = get_follower(current.backend, current.target, window_id=session.window_id)
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
