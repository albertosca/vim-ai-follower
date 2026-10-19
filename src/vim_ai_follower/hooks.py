"""Claude Code PreToolUse/PostToolUse hook handlers that drive the live follower animation."""

from __future__ import annotations

import contextlib
import dataclasses
import functools
import json
import logging
import os
import subprocess
import sys
import time
from collections.abc import Sequence
from pathlib import Path
from typing import Any, Literal, Protocol

from vim_ai_follower import (
    binding,
    cache,
    config,
    control,
    keybindings,
    snapshot,
    take_over,
    writer_cue,
)
from vim_ai_follower import diff as diff_module
from vim_ai_follower import state as state_module
from vim_ai_follower.backends import (
    BufferProbe,
    Follower,
    NavigationFailed,
    get_follower,
    nvim_connect,
    nvim_prompt,
    tmux_vim,
    without_bom,
)
from vim_ai_follower.backends.nvim_connect import (
    StandaloneLaunchFailed,
    launch_standalone_nvim,
    resolve_nvim_target,
)
from vim_ai_follower.backends.tmux_vim import TmuxVimFollower
from vim_ai_follower.session import Session, other_live_followers, resolve_session
from vim_ai_follower.snapshot import load as load_snapshot
from vim_ai_follower.snapshot import save as save_snapshot
from vim_ai_follower.state import FollowerState, owned_state, touch_open_files
from vim_ai_follower.status_surface import StatusSurface, status_surface_for
from vim_ai_follower.tmux import POPUP_SECONDS, adopt_target, show_popup

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
    if state_module.from_another_server(raw):
        # Left by a previous tmux server: its origin names a pane of this
        # one that is not this window's (see state.from_another_server).
        return None

    if raw.backend == "nvim":
        # Recovery never re-adopts: relaunch a fresh dedicated nvim (adopt=
        # False) so a user who closed their own editor is not silently taken
        # over again.
        target = _split_nvim_or_log(raw.origin, window_id, adopt=False)
        if target is None:
            return None
        sock, _launched = target
        recorded = FollowerState.set(
            window_id,
            "nvim",
            sock,
            current_file=None,
            origin=raw.origin,
            on_failure=raw.on_failure,
            speed=raw.speed,
            adopted=False,
        )
        return _launched_follower(window_id, "nvim", sock, recorded)

    try:
        started = TmuxVimFollower.start(raw.origin)
    except subprocess.CalledProcessError as exc:
        logger.warning("failed to reopen follower pane from origin %s: %s", raw.origin, exc)
        return None

    recorded = FollowerState.set(
        window_id,
        "tmux",
        started.pane_id,
        current_file=None,
        origin=raw.origin,
        on_failure=raw.on_failure,
        speed=raw.speed,
    )
    return _launched_follower(window_id, "tmux", started.pane_id, recorded)


def _launched_follower(
    window_id: str, backend: str, target: str, recorded: bool
) -> FollowerState | None:
    """The follower a hook just launched (an auto-open, a reopen), once its
    state is written. When the write was refused, a `claude-follow stop`
    landed between the hook's check and its write (final review of T17,
    Minor 3): the editor it launched has nothing naming it, so no later
    `stop` would close it and a later adopting `start` would take it for the
    user's own. It is closed here instead, best-effort, with one log line."""
    if recorded:
        return FollowerState.get(window_id)
    logger.warning(
        "window %s was stopped while its follower was opening; closing the %s it opened (%s)",
        window_id,
        backend,
        target,
    )
    try:
        get_follower(backend, target, window_id=window_id).stop(dedicated=True)
    except Exception as error:
        logger.warning(
            "could not close the follower %s opened for a stopped window: %s", target, error
        )
    return None


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
    answering = nvim_connect.answering_socket(window_id)
    if answering is not None:
        # Checked BEFORE the backoff: a launcher can bind nvim after the wait
        # below gave up (a Terminal `do script` behind a slow shell rc), and
        # backing off from a window that is up would show the user an nvim
        # that never animates.
        nvim_connect.clear_launch_backoff(window_id)
        return answering
    if nvim_connect.launch_backoff_active(window_id):
        return None  # a recent launch failed: already logged, not retried yet
    try:
        return launch_standalone_nvim(
            window_id, wait_seconds=nvim_connect.AUTO_OPEN_SOCKET_WAIT_SECONDS
        )
    except (subprocess.CalledProcessError, StandaloneLaunchFailed) as exc:
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


def _split_nvim_or_log(origin: str, window_id: str, adopt: bool) -> tuple[str, bool] | None:
    """resolve_nvim_target from a hook, or log the failure and return None.

    A split whose nvim never answers keeps its pane (nvim's error is on it)
    and backs the window off for LAUNCH_BACKOFF_SECONDS, the standalone
    launch's backoff: without it every hook split one more pane next to the
    first, each costing the tool call the whole wait. The backoff holds back
    only a SPLIT — adopting the user's nvim, or a split that answered after
    its caller gave up, opens nothing. A failing `tmux split-window` itself
    is quick and is only logged, as before."""
    if nvim_connect.launch_backoff_active(window_id):
        adoptable = adopt and nvim_connect.discover_adopt_socket(origin) is not None
        if not adoptable and nvim_connect.ready_split_socket(window_id) is None:
            return None  # a recent split failed: already logged, not retried yet
    try:
        target = resolve_nvim_target(origin, window_id, adopt=adopt)
    except subprocess.CalledProcessError as exc:
        logger.warning("nvim auto-open failed from origin %s: %s", origin, exc)
        return None
    except (nvim_connect.SplitNvimNeverAnswered, StandaloneLaunchFailed) as exc:
        nvim_connect.record_launch_failure(window_id)
        logger.warning(
            "%s — not auto-opening %s again for %d minutes (`claude-follow start` retries at once)",
            exc,
            window_id,
            nvim_connect.LAUNCH_BACKOFF_SECONDS // 60,
        )
        print(f"claude-follow: {exc}", file=sys.stderr)
        return None
    if target[1]:
        nvim_connect.clear_launch_backoff(window_id)
    return target


def _stopped_window(session: Session) -> bool:
    """True when `claude-follow stop` silenced this window (state.is_stopped,
    under this tmux server): auto-open must not undo an explicit stop. Said
    once per window in hook.log, so a quiet window can be explained."""
    if not state_module.stopped_path(session.window_id).exists():
        return False  # one stat, no tmux spawn, on every hook of a window never stopped
    server_pid = binding.tmux_server_pid() if session.in_tmux else None
    if not state_module.is_stopped(session.window_id, server_pid):
        return False
    note = state_module.stopped_note_path(session.window_id)
    if not note.exists():
        with contextlib.suppress(OSError):
            note.write_text("")
        logger.info(
            "window %s was stopped with `claude-follow stop`; auto-open stays off "
            "until `claude-follow start` (or toggle)",
            session.window_id,
        )
    return True


def _maybe_auto_open(session: Session, file_path: str, cfg: config.Config) -> FollowerState | None:
    if cfg.open_policy == "manual" or not _passes_policy(cfg, file_path):
        return None
    if _stopped_window(session):
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
        recorded = FollowerState.set(
            session.window_id,
            "nvim",
            sock,
            origin="",
            on_failure=cfg.on_failure,
            speed=cfg.speed,
            adopted=False,
        )
        return _launched_follower(session.window_id, "nvim", sock, recorded)
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
            recorded = FollowerState.set(
                session.window_id,
                "nvim",
                sock,
                origin=origin,
                on_failure=cfg.on_failure,
                speed=cfg.speed,
                adopted=False,
            )
            return _launched_follower(session.window_id, "nvim", sock, recorded)
        # Same adopt-or-launch selection as cmd_start, but from the hook path.
        target = _split_nvim_or_log(origin, session.window_id, adopt=cfg.adopt_existing)
        if target is None:
            return None
        sock, launched = target
        recorded = FollowerState.set(
            session.window_id,
            "nvim",
            sock,
            origin=origin,
            on_failure=cfg.on_failure,
            speed=cfg.speed,
            adopted=not launched,
        )
        if not launched:
            # Adopted: the user's own nvim, nothing to close if refused.
            return FollowerState.get(session.window_id) if recorded else None
        return _launched_follower(session.window_id, "nvim", sock, recorded)
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
        recorded = FollowerState.set(
            session.window_id,
            "tmux",
            started.pane_id,
            origin=origin,
            on_failure=cfg.on_failure,
            speed=cfg.speed,
        )
        return _launched_follower(session.window_id, "tmux", started.pane_id, recorded)
    return FollowerState.get(session.window_id)


def _ensure_buffer(window_id: str, follower: Follower, file_path: str) -> None:
    """Switches the follower to file_path's tab (a by-number buffer switch,
    or `:tab drop` for a file no buffer holds yet, on tmux; an RPC-based tab
    lookup on nvim). Used for Read navigation and binary files, where
    showing the real on-disk content is exactly what's wanted. Both backends
    re-read an already-open buffer when it is CLEAN, so a file rewritten
    outside Claude's Edits is not shown stale, and leave one holding unsaved
    user typing alone: tmux through the swap-guarded s:reload (`_VIM_RELOAD`),
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


# How a hand-off an S during the pre-edit catch-up started describes what the
# user had been shown: "shown", the earlier edit's text (the partial);
# "unknown", a legacy remainder that recorded no partial.
EarlierEdit = Literal["shown", "unknown"]


def _print_interrupt_notification(
    file_path: str, partial_content: str, earlier_edit: EarlierEdit | None = None
) -> None:
    # Every partial arrives newline-terminated, and the template already puts
    # a blank line on each side of the quote — so drop exactly the terminator,
    # not every trailing newline: a partial whose last line is genuinely blank
    # must still show that blank line here.
    quoted = partial_content.removesuffix("\n")
    if earlier_edit is not None:
        # The S came while the follower caught up on an EARLIER edit whose
        # animation had been cut off: the partial is that edit's text, never
        # "this much of your version" (Task 20 review, m1).
        shown = (
            "how much of the earlier edit they had been shown is not known. "
            if earlier_edit == "unknown"
            else f"what they had been shown was the start of your earlier, interrupted "
            f"edit of it:\n\n{quoted}\n\n"
        )
        _print_hook_context(
            f"The user interrupted the live preview of {file_path} while it was "
            "still catching up on an earlier edit of yours, edited it "
            "themselves, and SAVED their own "
            "version — it is now the file's content on disk. So none of this "
            f"edit had been typed for them, and {shown}"
            "Re-read the file from disk and build on the user's version; do not "
            "restore yours without asking."
        )
        return
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


def _print_stopped_notification(file_path: str) -> None:
    _print_hook_context(
        f"The user interrupted the live preview of {file_path} and then stopped "
        "the follower (`claude-follow stop`) without saving their own version, "
        "so your version stands as the file's content on disk. Continue normally."
    )


def _print_stopped_while_paused_notification(file_path: str) -> None:
    _print_hook_context(
        f"The user paused the live preview of {file_path} and then stopped "
        "the follower (`claude-follow stop`), so your version stands as the "
        "file's content on disk. Continue normally."
    )


def _end_stopped_animation(window_id: str, file_path: str) -> int:
    """A `claude-follow stop` ended this hook's animation while it was paused
    (final review C1): the driver sent nothing more and kept no remainder.
    The signals go (the stop cleared them too; one pressed since is nobody's),
    the cue is the stop's to take down, and Claude is released at once."""
    control.discard_pending_animation(window_id)
    control.clear_signals(window_id)
    _mark_finishing(window_id)
    _print_stopped_while_paused_notification(file_path)
    return 0


def _follower_stopped(current: FollowerState, session: Session) -> bool:
    """Whether `claude-follow stop` ended this window's follower while a
    hand-off waited on it (T20 review m3: the wait never noticed, and Claude
    stayed held until the hook's 600 s timeout). See state.follower_stopped,
    which the pause wait asks too (final review C1)."""
    return state_module.follower_stopped(session.window_id, (current.backend, current.target))


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


def _file_text(file_path: str) -> str:
    """file_path's content as the hooks hold it: its bytes decoded like the
    post hook's `after`, line endings kept. Never read_text(), whose
    universal newlines turn every CRLF into LF, so a CRLF snapshot read that
    way never equals the file again (BACKLOG "CRLF files")."""
    return Path(file_path).read_bytes().decode("utf-8", errors="replace")


def _poll_until_interrupt(
    current: FollowerState,
    session: Session,
    file_path: str,
    after: str,
    partial_content: str,
    initial_mtime: int | None,
    cue: str = HANDOFF_CUE,
    raw_after: bytes | None = None,
    earlier_edit: EarlierEdit | None = None,
) -> bool:
    """One hand-off wait. True when the user pressed S again (des-interrupt);
    False when they released the turn by saving, with the matching
    notification already printed.

    A save is told apart from Claude's file by the file's RAW bytes against
    `raw_after`, the bytes Claude wrote (None: `after` encoded, for a caller
    without them). Never decoded text: a CRLF file read with read_text()
    "differed" at the first poll (BACKLOG "CRLF files"), and two different
    undecodable bytes decode alike, so a Latin-1 save that changed only one
    read as "saved it unchanged" (T12+T13 review M1)."""
    expected = raw_after if raw_after is not None else after.encode("utf-8")
    polls = 0
    while True:
        polls += 1
        if polls % _HANDOFF_REMINDER_POLLS == 0 and session.in_tmux:
            show_popup(current.target, cue)
        if control.check_signal(session.window_id) == "interrupt":
            # The replay starts now, so a second S interrupts IT — "running"
            # before anything else, or one pressed within milliseconds of the
            # first read "handoff" and was told its typing was discarded.
            control.mark_animating(session.window_id, state="running")
            return True
        try:
            if Path(file_path).read_bytes() != expected:
                control.discard_pending_animation(session.window_id)
                _print_interrupt_notification(file_path, partial_content, earlier_edit)
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
        # After the save checks: a save that landed before the stop is
        # still the user's version, and is reported as one.
        if _follower_stopped(current, session):
            control.discard_pending_animation(session.window_id)
            control.clear_signals(session.window_id)
            _print_stopped_notification(file_path)
            return False
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
    # The replay's driver left "running"; from here on an S is the next
    # des-interrupt, never an interrupt of a replay that already returned.
    control.mark_animating(window_id, state="handoff")
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
    # The rebuild always runs to the end: an S pressed during it is resume's
    # to take, before it types anything (count 0, the remainder re-armed
    # whole), so the buffer handed back is never cut inside the partial.
    follower.rewrite_buffer(file_path, partial_content)
    # rewrite_buffer rebuilds the buffer to EXACTLY the partial, so there is
    # no seed to strip — EXCEPT when the partial is empty (interrupt before
    # any line was typed): nvim can't hold a truly empty buffer, so
    # rewrite_buffer forces a single blank line, which IS a seed the replay
    # must type in front of and drop.
    result = follower.resume(pending, seeded=partial_content == "")
    if result.outcome == "stopped":
        # Paused mid-replay, then stopped: the hand-off is over with Claude's
        # version, and nothing is re-armed onto the editor the stop took.
        control.discard_pending_animation(window_id)
        control.clear_signals(window_id)
        _print_stopped_while_paused_notification(file_path)
        return None
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
    raw_after: bytes | None = None,
    earlier_edit: EarlierEdit | None = None,
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
    nobody listening and the buffer partial until the next animation).

    earlier_edit marks a hand-off an S during the pre-edit catch-up started:
    nothing of THIS edit was typed, and partial_content is the earlier,
    interrupted edit's text ("shown"), or unknown ("unknown"); the release
    notification says so (_print_interrupt_notification)."""
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
                current,
                session,
                file_path,
                after,
                partial_content,
                initial_mtime,
                cue,
                raw_after=raw_after,
                earlier_edit=earlier_edit,
            ):
                return
            # switch the cue from "waiting" to the replay before it runs
            surface.set_state("Writing...")
            replayed = _replay_remainder(current, session, file_path, partial_content)
            if replayed is None:
                return
            partial_content = replayed
    finally:
        # Over — released by a save, or the replay ran to the end: nothing
        # is left to interrupt, and an S while the cue comes down must not
        # read "handoff" (it would be told unsaved changes were discarded).
        control.mark_animating(window_id, state="finishing")
        # A wait the stop ended leaves the cue to the stop: it took the
        # follower's surface down itself (or, for an adopted editor whose
        # user is typing, sends nothing on purpose), and a restore here
        # would put a saved border title back.
        if not _follower_stopped(current, session):
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
        before = _file_text(file_path)
    except OSError:
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
    its in-flight mark is dropped — when the mark is this writer's, or its
    writer is unknown (""). A mark another writer set stays: that writer's
    Edit of the same file is still in flight, and a payload with no identity
    proves no ownership of a known writer's mark. Neither the enabled flag
    nor the open policy is consulted — a toggle or a config change between
    pre and here must not strand the mark.

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
    # Only this writer's own mark, or one whose writer is unknown: another
    # writer's Edit of the same file may still be in flight, and its post
    # hook has yet to run (D2 residual minors).
    writer = snapshot.in_flight_writer(session.window_id, file_path)
    if writer in ("", writer_cue.writer_identity(payload) or ""):
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
    except (NavigationFailed, nvim_connect.RpcTimeout) as failure:
        # RpcTimeout: an nvim that stopped answering outside the typing (which
        # waits, see NvimFollower._drive) — at the connect or a navigation, a
        # catch-up's goto, an eviction. Nothing was typed by the request that
        # timed out, so the same skip: one line, the cue down, marked stale.
        _skip_unreachable_edit(session.window_id, file_path, payload, failure)
        return 0
    finally:
        _drain_unconsumed_signals(
            session.window_id, FollowerState.read(session.window_id), session.in_tmux
        )
        # Release this window's animation slot on every path — including the
        # ones that never start an animation (no follower, unreadable/binary
        # file), which would otherwise hold the marker until the process
        # exits and needlessly block a concurrent hook in the meantime.
        control.clear_animating(session.window_id)


def _busy_follower(current: FollowerState, question_seconds: float) -> str | None:
    """Why this hook must leave the follower alone, or None: _busy_nvim for
    an nvim, _busy_vim for a Vim."""
    if current.backend == "nvim":
        return _busy_nvim(current, question_seconds)
    return _busy_vim(current, question_seconds)


def _busy_vim(current: FollowerState, question_seconds: float) -> str | None:
    """Why this hook must leave a Vim follower alone, or None: an ADOPTED Vim
    whose user is known to be typing (tmux_vim.typing_mode: its mode file,
    or on a first contact its screen) is asked about first, as an nvim is
    (Alberto, 2026-10-17), before anything is typed: the Escape pair every
    navigation starts with would end their insert, replace, visual or select
    mode, or go to a terminal window's job. Only `y` takes it over, with
    CTRL-\\ CTRL-N (tmux_vim.leave_typing_mode), and the hook goes on.
    A dedicated follower is never asked: its Vim is the follower's."""
    if not current.adopted:
        return None
    typing = tmux_vim.typing_mode(current.target)
    if typing is None:
        return None
    waiting = f"Vim in {current.target} is waiting on its user ({typing})"
    answer = take_over.ask(current.target, "Vim", question_seconds)
    if answer != take_over.YES:
        return f"{waiting}, not taken over ({answer})"
    tmux_vim.leave_typing_mode(current.target)
    # Review I2, measured: with a CTRL-V pending in insert or replace mode
    # Vim inserts the CTRL-\ literally and the CTRL-N completes; with a
    # CTRL-K pending the digraph lookup fails and inserts ^N. The mode reads
    # `i` either way and no byte cancels a CTRL-V without inserting, so the
    # log says it may have happened.
    note = ""
    if typing.startswith(("in insert mode", "in replace mode")):
        note = "; a pending CTRL-V/CTRL-K may have inserted a character"
    logger.info("%s: taken over (answered y)%s", waiting, note)
    return None


def _busy_nvim(
    current: FollowerState,
    question_seconds: float = config.DEFAULT_TAKE_OVER_QUESTION_SECONDS,
) -> str | None:
    """Why this hook must leave an nvim follower alone, or None: the liveness
    probe FollowerState.get just ran found it accepting and SILENT — busy in
    `:!make`, `system()` or a Lua loop, or stopped — or found an ADOPTED nvim
    answering `blocking: true` in a mode the follower does not answer (keys
    pending in normal mode, half of a mapping), which holds every other
    request until the user finishes it. Either is alive (so nothing reopens or
    replaces it), but every request would wait out the RPC deadline — the
    cue, the base probe and the cue's refresh cost 30 s; the hook skips
    instead, before raising any cue.

    An adopted nvim blocking at a hit-enter or more-prompt is swept here
    instead, and the hook goes on (decision (a)): the hook is about to act,
    and the Vim follower's Escape pair dismisses a prompt the user has up at
    that moment. It has to be here, before the cue, because the cue does not
    act (no sweep of its own) and would wait behind the prompt. A dedicated
    follower's prompts are its guard's to sweep.

    An adopted nvim at a confirm (`r?`: `confirm()`, `:s///c`) or on the
    command line (`c…`: `input()`, a half-typed `:` command, a search) is
    skipped too, though neither blocks (measured, nvim 0.12.6): the user's
    answer or Enter runs against whatever buffer is current when it lands,
    and a follower that opened Claude's file meanwhile had the user's
    substitute, or their `:let g:who = bufname('%')`, run on Claude's buffer
    (task 22 review I1). Answering them is the user's choice (Alberto's
    answer A), so the edit is skipped and the file marked stale.

    An adopted nvim whose user is TYPING (insert, replace, terminal, visual
    or select mode) is asked about instead (Alberto, 2026-10-17): a popup
    over the follower's window, `question_seconds` long, and only a `y`
    takes it over (_take_over_nvim); any other answer skips, as above."""
    if nvim_connect.last_probe_verdict(current.target) == "silent":
        return (
            f"nvim at {current.target} did not answer within "
            f"{nvim_connect.LIVENESS_TIMEOUT_SECONDS:g} s (busy in a command, or stopped)"
        )
    if not current.adopted:
        return None
    pending = adopted_user_pending(current.target)
    if pending in TYPING_MODES:
        return _take_over_nvim(current, pending, question_seconds)
    if pending is not None:
        return f"nvim at {current.target} is waiting on its user ({pending})"
    if nvim_connect.last_probe_blocking(current.target):
        return sweep_adopted_prompt(current.target, "the hook")
    return None


# The adopted_user_pending reasons that mean the user is typing: acting under
# one puts their next keys into Claude's buffer, so the hook asks first.
TYPING_MODES = take_over.TYPING_REASONS

# How long a taken-over nvim may take to leave its typing mode.
_LEAVE_TYPING_SECONDS = 2.0


def _take_over_nvim(current: FollowerState, pending: str, question_seconds: float) -> str | None:
    """Ask whether to take over an adopted nvim whose user is typing. None
    once it is taken over (back in Normal mode, as the Vim follower's Escape
    pair leaves a Vim); otherwise why the hook must skip."""
    waiting = f"nvim at {current.target} is waiting on its user ({pending})"
    answer = take_over.ask(current.origin, "Neovim", question_seconds)
    if answer != take_over.YES:
        return f"{waiting}, not taken over ({answer})"
    left = _leave_typing_mode(current.target)
    if left is not None:
        return f"{waiting}, answered y but {left}"
    logger.info("%s: taken over (answered y)", waiting)
    return None


def _leave_typing_mode(socket_path: str) -> str | None:
    """Send CTRL-\\ CTRL-N (Normal mode from insert, replace, visual, select;
    Terminal-Normal from terminal) and wait, bounded, for the probe to read a
    mode the follower may act under. None when it did; otherwise why not."""
    try:
        nvim = nvim_connect.bounded_attach(socket_path)
        try:
            nvim.input("<C-\\><C-N>")
        finally:
            nvim.close()
    except (OSError, EOFError) as error:  # RpcTimeout is an OSError
        return f"it could not be sent CTRL-\\ CTRL-N: {error}"
    deadline = time.monotonic() + _LEAVE_TYPING_SECONDS
    while True:
        nvim_connect.probe_nvim(socket_path)
        still = adopted_user_pending(socket_path)
        if still is None and not nvim_connect.last_probe_blocking(socket_path):
            return None
        if time.monotonic() >= deadline:
            return f"it was still {still or 'blocking'} after {_LEAVE_TYPING_SECONDS:g} s"
        time.sleep(0.05)


def adopted_user_pending(socket_path: str) -> str | None:
    """What the user of an ADOPTED nvim is in the middle of, by the last
    probe's mode, when it is something the follower must not act under: a
    confirm (`r?`), the command line (any `c…` mode: `input()`, a `:`
    command or a search being typed), an operator waiting for its motion
    (any `no…` mode: the user's second `d` deleted Claude's line, T22
    re-review I2), insert, replace or terminal mode (`i…`, `R…`, `t…`, and
    the `niI`/`niR`/`niV`/`ntT` of a CTRL-O from one: a navigation keeps
    the mode, so the user's next keys were typed into Claude's buffer,
    measured on nvim 0.12.6; Alberto 2026-10-17), visual or select mode
    (`v`/`V`/CTRL-V, `s`/`S`/CTRL-S and the `vs`/`Vs`/CTRL-V `s` of a
    CTRL-O from select: a navigation leaves the user in Normal mode in
    Claude's buffer and their next `x`/`d` acted there, review I1 of
    T24a), or a blocking key
    sequence (`q` waiting for its register reads `n`, blocking). An nvim
    that answered not blocking is first asked whether its command-line
    window is open, in every mode (`q:` reads "n", `q:` then `i` reads "i",
    and every window change in it raises E11: show_fresh crashed the hook,
    re-review m2; a typing reason won over it, review m1 of T24c) — one
    more bounded raw request; no answer counts as not open. None
    for an idle nvim or one at a hit-enter or more-prompt, which the
    follower answers when it acts."""
    mode = nvim_connect.last_probe_mode(socket_path)
    # Asked FIRST, whatever the mode: a user typing inside the command-line
    # window (`q:` then `i` or `v`) is in it as much as one in its Normal
    # mode, and every window change there raises E11 (review m1 of T24c).
    if (
        not nvim_connect.last_probe_blocking(socket_path)
        and nvim_connect.last_probe_verdict(socket_path) == "answers"
        and nvim_connect.in_command_line_window(socket_path)
    ):
        return "in the command-line window"
    if mode == "r?":
        return "a confirm prompt"
    if mode is not None and mode.startswith("c"):
        return "on the command line"
    if mode is not None and mode.startswith("no"):
        return "an operator pending"
    typing = take_over.typing_reason(mode)
    if typing is not None:
        return typing
    if nvim_connect.last_probe_blocking(socket_path) and not nvim_prompt.answerable(mode):
        return "in the middle of a key sequence"
    return None


def sweep_adopted_prompt(socket_path: str, label: str) -> str | None:
    """Answer the hit-enter or more-prompt an ADOPTED nvim at `socket_path`
    has up, on a bounded connection of its own, before `label` acts there.
    None when done (or when another guard answered it first); otherwise why
    the prompt could not be answered, for the caller to skip with."""
    try:
        nvim_prompt.sweep_socket(
            functools.partial(nvim_connect.bounded_attach, socket_path), socket_path, label
        )
    except (OSError, EOFError) as error:  # RpcTimeout is an OSError
        return f"nvim at {socket_path} is at a prompt that could not be answered: {error}"
    return None


def _skip_unreachable_edit(
    window_id: str,
    file_path: str,
    payload: dict[str, Any],
    failure: NavigationFailed | nvim_connect.RpcTimeout,
) -> None:
    """The follower could not confirm it reached file_path's buffer, so the
    backend sent nothing that would act on another one. Skip the edit: one
    log line, the "Writing..." cue taken down, and the file marked stale so
    its next touch retypes it (or, in an adopted editor, probes it) instead
    of typing a diff onto a buffer this edit never reached."""
    logger.warning("skipped the animation of %s: %s", file_path, failure)
    FollowerState.mark_stale(window_id, file_path)
    current = FollowerState.read(window_id)
    if current is not None:
        _refresh_writer_cue(window_id, current.target, payload)


@dataclasses.dataclass(frozen=True)
class _CatchUp:
    """How a pre-edit catch-up ended. "interrupted" is an S the catch-up's
    driver consumed, and `partial` is then what the buffer holds: the
    persisted partial plus whatever of the remainder the replay typed, in the
    terminated form every partial uses, or None when the remainder recorded
    no partial (a legacy one) and so what the buffer holds is not known."""

    outcome: Literal["completed", "interrupted", "stopped"]
    partial: str | None = ""


def _consume_pending_catchup(
    follower: Follower,
    file_path: str,
    pending: control.PendingApplyEdit | control.PendingShowFresh,
) -> _CatchUp:
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

    "completed" means the catch-up ran to the end, so the buffer holds only
    follower text, unless the user typed into an adopted editor while it ran
    (_caught_up_buffer_held asks before _animate_edit reloads it from disk).
    "interrupted" is an S pressed while it ran (final review M5): the replay
    consumed it (the rebuild never does: one pressed during it is left for
    the replay's first check, so the buffer holds at least the whole
    partial), nothing more is typed, and the caller hands the buffer over
    (_hand_over_interrupted_catch_up). A legacy remainder without a partial
    reports None: what its buffer holds is not known."""
    if pending.partial is not None:
        # Never cut: an S pressed during it is the replay's, at its start.
        follower.rewrite_buffer(file_path, pending.partial)
    result = follower.resume(
        dataclasses.replace(pending, pace_seconds=0.0), seeded=not pending.partial, reload=False
    )
    if result.outcome == "completed":
        return _CatchUp("completed")
    if result.outcome == "stopped":
        return _CatchUp("stopped")
    if pending.partial is None:
        return _CatchUp("interrupted", None)
    if isinstance(pending, control.PendingShowFresh):
        shown = pending.partial + _terminated(pending.lines[: result.completed_count])
    else:
        shown = diff_module.apply_ops(pending.partial, pending.ops[: result.completed_count])
    return _CatchUp("interrupted", shown)


def _hand_over_interrupted_catch_up(
    current: FollowerState,
    session: Session,
    follower: Follower,
    file_path: str,
    raw_after: bytes,
    partial: str | None,
) -> int:
    """An S pressed during the pre-edit catch-up (Alberto's answer A,
    2026-10-17, final review M5): hand the buffer over as it is now. It used
    to be consumed by the catch-up's driver and then ignored: no hand-off,
    the new edit animated anyway (or, adopted, the buffer was left alone),
    and the remainder was gone.

    The hand-off is THIS edit's: the normal wait, with the catch-up's partial
    as the partial and NO saved remainder (the catch-up consumed the old one,
    and a pause during it deletes the crash fallback it wrote on every exit).
    So `:w` releases Claude with the user's version, told that none of THIS
    edit had been typed (earlier_edit), and S again takes _replay_remainder's
    `pending is None` path: the finished file is reloaded, nothing replayed.
    hand_over re-asserts the unlock and turns CoC back on, as _rearm_handoff
    does after a replay's interrupt."""
    window_id = session.window_id
    # The driver returned: an S from here on is the des-interrupt.
    control.mark_animating(window_id, state="handoff")
    follower.hand_over()
    FollowerState.update_current_file(window_id, None)
    surface = status_surface_for(current)
    # As before an animation: an earlier leave-alone cue is not this hand-off's
    # to restore when it ends.
    surface.retire_state(BASE_DIFFERS_CUE, PROBE_UNKNOWN_CUE)
    after = raw_after.decode("utf-8", errors="replace")
    _await_user_handoff(
        current,
        session,
        file_path,
        after,
        partial or "",
        surface,
        raw_after=raw_after,
        earlier_edit="unknown" if partial is None else "shown",
    )
    _mark_finishing(window_id)
    return 0


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
    except nvim_connect.RpcTimeout as timeout:
        # A prompt raised after the liveness probe: one line, not a traceback.
        logger.warning("could not ask the follower what %s's buffer holds: %s", file_path, timeout)
        return "unknown"
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
    partial probe said "holds", the follower then typed the rest, and a
    second probe (_caught_up_buffer_held) found the buffer still holding
    exactly what the catch-up typed — only follower text, nothing the user
    typed while it ran. Left alone it would stay modified while disk moved
    on — the next `:checktime` (FocusGained with 'autoread') raises the
    blocking W12 dialog, and a `:w` writes that stale text over Claude's
    and the formatter's edits (reproduced 2026-09-25).

    "unknown" (the editor did not answer the base probe) is grounded in an
    adopted editor by the same reasoning once that second probe answers: the
    buffer holds nothing of the user's, and leaving it alone would leave it
    modified, the W12 hazard above. A
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

_WHY_AFTER_CATCH_UP: dict[BufferProbe, str] = {
    "differs": "it no longer holds what the catch-up typed: the user typed during the catch-up",
    "unknown": "the editor did not answer whether it still holds what the catch-up typed",
    "absent": "its buffer went away during the catch-up",
}


def _catch_up_end(pending: control.PendingApplyEdit | control.PendingShowFresh) -> str | None:
    """What the buffer holds once a catch-up of `pending` ran to the end: its
    partial (which _consume_pending_catchup rebuilds first) with the
    remainder typed on top, in the terminated form every partial uses. None
    when no partial was recorded: that catch-up starts from the live buffer,
    whose shape nothing here knows."""
    if pending.partial is None:
        return None
    if isinstance(pending, control.PendingApplyEdit):
        return diff_module.apply_ops(pending.partial, list(pending.ops))
    return pending.partial + _terminated(pending.lines)


def _caught_up_buffer_held(
    window_id: str,
    follower: Follower,
    current: FollowerState,
    file_path: str,
    pending: control.PendingApplyEdit | control.PendingShowFresh,
) -> BufferProbe:
    """Whether the buffer a catch-up just completed still holds nothing but
    the follower's text — what permits _ground_caught_up_buffer's `:e!`.

    A follower its state PROVES dedicated (state.owned_state: readable, names
    this follower, not adopted) is "holds" without asking: its buffer is
    never the user's. Anything else fails closed onto the user's side and is
    asked, against the catch-up's own end (_catch_up_end): in an adopted
    editor the user can type into the buffer while the catch-up runs, which
    takes seconds for a long partial (one tmux batch per line even at pace
    0; BACKLOG "text typed DURING a catch-up is dropped by the grounding
    reload"), and the reload would discard that typing. No recorded partial
    is "unknown": nothing to compare with (an adopted catch-up always has
    one; only a legacy remainder replayed by a follower whose state stopped
    proving it dedicated mid-hook gets here without).

    What remains: typing that lands between this probe's answer and the
    reload's `:e!`, a few milliseconds (one more round trip on tmux, one RPC
    request on nvim)."""
    proven = owned_state(window_id, current.backend, current.target)
    if proven is not None and not proven.adopted:
        return "holds"
    end = _catch_up_end(pending)
    if end is None:
        return "unknown"
    return _probe_base(follower, file_path, end)


def _mark_finishing(window_id: str) -> None:
    """The hook's last animation has returned; it keeps the slot for its
    bookkeeping until its own exit releases it. "finishing" tells a P or S
    pressed now that there is nothing left to pause or interrupt — as
    "running" it drew a signal addressed to this hook that nothing would
    ever consume, and a popup claiming it had worked."""
    control.mark_animating(window_id, state="finishing")


# What a press that nothing obeyed is answered with, per signal: the message a
# tmux popup shows, and the cue the press put up that must come down.
_NOTHING_TO = {
    "pause": ("Nothing to pause", "Paused"),
    "interrupt": ("Nothing to interrupt", "Interrupted"),
}


def retract_signal_feedback(
    current: FollowerState | None, in_tmux: bool, signal: Literal["pause", "interrupt"]
) -> None:
    """Undo the feedback a P or S press gave for a hook that then exited
    without obeying it (final M6, PF round 2). nvim: take its cue off the
    status float — "Paused" otherwise stays until something overwrites it.
    tmux: say so in a popup, once the press's own popup has closed (tmux
    drops a popup opened while another is up). Best-effort, like any cue."""
    if current is None:
        return
    message, cue = _NOTHING_TO[signal]
    if current.backend == "nvim":
        status_surface_for(current).retire_state(cue)
        return
    if in_tmux:
        show_popup(current.target, message, after_seconds=POPUP_SECONDS + 0.1)


_MAXIMUM_DRAINED_SIGNALS = 8


def _drain_unconsumed_signals(window_id: str, current: FollowerState | None, in_tmux: bool) -> None:
    """At the hook's exit, BEFORE it releases the slot: a P or S addressed to
    this hook that no driver consumed — pressed while it held the slot as
    "running" on a path that never animates (an adopted buffer left alone, a
    grounding reload, a binary file, a navigation that failed), or between a
    driver's last signal check and the "finishing" mark — is consumed here,
    and the feedback it drew is taken back. Left on disk it was only dropped
    as stale by the next hook, with "Paused" still on screen.

    "finishing" first: a press from now on is told there is nothing to act
    on, and cmd_pause, re-reading the state after showing its feedback, takes
    it back itself when this drain ran before that feedback appeared."""
    try:
        control.mark_animating(window_id, state="finishing")
        # Every one: check_signal returns at the first file it consumes
        # (interrupt before pause), and a P next to an S left "Paused" up.
        # Bounded — a signal can only be written to this process while it
        # holds the slot, and from "finishing" on no press writes one.
        drained: list[Literal["pause", "interrupt"]] = []
        for _ in range(_MAXIMUM_DRAINED_SIGNALS):
            signal = control.check_signal(window_id)
            if signal is None:
                break
            drained.append(signal)
        # Retracted after every file is gone, once per kind.
        for signal in dict.fromkeys(drained):
            logger.info(
                "a %s press reached this hook after its last animation: none obeyed", signal
            )
            retract_signal_feedback(current, in_tmux, signal)
    except Exception:
        logger.exception("could not drain the signals left for this hook")


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
    busy = _busy_follower(current, cfg.take_over_question_seconds)
    if busy is not None:
        # Skipped like an unreachable edit, minus the cue: none was raised,
        # and taking one down would wait on the same silent nvim.
        logger.warning("skipped the animation of %s: %s; marked it stale", file_path, busy)
        FollowerState.mark_stale(session.window_id, file_path)
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
    catch_up: _CatchUp | None = None
    if (
        pending is not None
        and (pending.file_path == file_path or pending.file_path == "")
        and not is_fresh
        and not binary
    ):
        if not current.adopted:
            catch_up = _consume_pending_catchup(follower, file_path, pending)
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
                catch_up = _consume_pending_catchup(follower, file_path, pending)
            elif held != "absent":
                _leave_adopted_buffer_alone(
                    current,
                    session.window_id,
                    file_path,
                    held,
                    f"{_WHY[held]} (a leftover remainder's partial); dropped the remainder",
                )
                return 0
    if catch_up is not None and catch_up.outcome == "stopped":
        return _end_stopped_animation(session.window_id, file_path)
    if catch_up is not None and catch_up.outcome == "interrupted":
        return _hand_over_interrupted_catch_up(
            current, session, follower, file_path, raw_after, catch_up.partial
        )
    caught_up = catch_up is not None and catch_up.outcome == "completed"

    if binary:
        # Binary files are never animated, so it's safe to just navigate to
        # them normally (real content shown immediately, nothing to spoil) —
        # fresh or not: an already-open one was rewritten on disk and its
        # clean buffer must be re-read, or the old bytes stay on screen.
        _ensure_buffer(session.window_id, follower, file_path)
        _touch_and_evict(session.window_id, follower, current, file_path, cfg.max_tabs)
        return 0

    after = raw_after.decode("utf-8", errors="replace")
    # What is TYPED (and what a partial is built from) leaves out a leading
    # UTF-8 BOM, which the editors keep in 'bomb' instead of the buffer; the
    # hand-off wait still compares disk with `after` itself (see without_bom).
    # A CRLF file's CRs need no stripping here: every consumer of the typed
    # text splits it with str.splitlines(), which drops them (BACKLOG "CRLF
    # files"; the editors keep them in 'fileformat').
    typed_after = without_bom(after)

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
    typed_before = without_bom(before)
    if not is_fresh or current.adopted:
        probe = _probe_base(follower, file_path, before)
        if probe == "holds":
            if file_path in current.stale_files:
                FollowerState.clear_stale(session.window_id, file_path)
            is_fresh = False
        elif (
            caught_up
            and pending is not None
            and (probe == "differs" or (probe == "unknown" and current.adopted))
        ):
            held = _caught_up_buffer_held(session.window_id, follower, current, file_path, pending)
            if held == "holds":
                _ground_caught_up_buffer(
                    session.window_id, follower, current, file_path, probe, cfg
                )
            else:
                _leave_adopted_buffer_alone(
                    current, session.window_id, file_path, held, _WHY_AFTER_CATCH_UP[held]
                )
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
        result = follower.show_fresh(file_path, typed_after, in_new_tab=current.shown_any)
        if result.outcome == "stopped":
            return _end_stopped_animation(session.window_id, file_path)
        if result.outcome == "interrupted":
            # The driver returned: an S from here on is the des-interrupt.
            control.mark_animating(session.window_id, state="handoff")
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
            partial = _reconstruct_partial_fresh(typed_after, result.completed_count)
            # Keep the remainder around: a des-interrupt replays it from
            # the interrupt point instead of flashing the finished file.
            control.save_pending_show_fresh(
                session.window_id,
                tuple(typed_after.splitlines())[result.completed_count :],
                config.pace_seconds_for(current.speed),
                continuation=result.completed_count > 0,
                file_path=file_path,
                partial=partial,
            )
            _await_user_handoff(
                current, session, file_path, after, partial, surface, raw_after=raw_after
            )
            _mark_finishing(session.window_id)
        else:
            _mark_finishing(session.window_id)
            # The retype ran to the end, so whatever was stale about this
            # buffer is gone — it now holds the whole file.
            FollowerState.clear_stale(session.window_id, file_path)
            FollowerState.update_current_file(session.window_id, file_path)
            _refresh_writer_cue(session.window_id, current.target, payload)
        return 0

    ops = diff_module.compute_edit_script(typed_before, typed_after)
    # `before` goes along for the tmux backend: its driver cannot read the
    # buffer, so this is the only way its pause-time crash fallback can record
    # the applied prefix. It is the same string the interrupt path below
    # replays the ops onto, so the two can never disagree.
    result = follower.apply_edit(file_path, ops, before=typed_before)
    if result.outcome == "stopped":
        return _end_stopped_animation(session.window_id, file_path)
    if result.outcome == "interrupted":
        # The driver returned: an S from here on is the des-interrupt.
        control.mark_animating(session.window_id, state="handoff")
        FollowerState.update_current_file(session.window_id, None)
        # completed_count indexes the very ops list the animation walked —
        # computing the script once keeps this reconstruction truthful.
        partial = diff_module.apply_ops(typed_before, ops[: result.completed_count])
        control.save_pending_apply_edit(
            session.window_id,
            ops[result.completed_count :],
            config.pace_seconds_for(current.speed),
            file_path=file_path,
            partial=partial,
        )
        _await_user_handoff(
            current, session, file_path, after, partial, surface, raw_after=raw_after
        )
        _mark_finishing(session.window_id)
    else:
        _mark_finishing(session.window_id)
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
    s:reload, nvim's `_reload_if_clean`), and a file no buffer
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
    The mark is dropped and the Read goes ahead.

    An identity is writer_cue.writer_identity's (agent_id, else session_id),
    and a payload with neither has NONE — the mark records "" and the reader
    is None, never a shared fallback identity — so two callers without one
    never match: neither's Read drops the other's mark (pinned by
    test_a_mark_without_a_writer_is_never_cleared_by_a_read)."""
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
        on_disk = Path(file_path).read_bytes()
    except OSError:
        # The pre hook snapshots an unreadable file as "", and the Read tool
        # just read it, so it exists: it is not the snapshot any more.
        return True
    # Byte for byte: a CRLF file must not equal its LF snapshot (an Edit that
    # only changed line endings wrote), and an undecodable file, whose
    # snapshot holds replacement characters, never equals its snapshot.
    return on_disk != load_snapshot(window_id, file_path).encode("utf-8")


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
    # The Read holds this window's animation slot for its whole navigation,
    # taken with the same try-acquire as an Edit (final review of
    # backlog-sweep-3, M2). A check-then-navigate left a gap: the navigation
    # is several sends (define, goto, Escape pair, landed, a wait of up to
    # 10 s, reload, lock), and an Edit hook that took the slot meanwhile had
    # its insert mode ended by the Read's Escape pair and the rest of its
    # text typed as normal-mode commands — in an adopted Vim, into the
    # user's editor. "finishing", never "running": no driver runs here, so a
    # P or S pressed meanwhile is told there is nothing to pause, instead of
    # drawing a signal addressed to this process that nothing would read.
    if not control.try_acquire_animating(session.window_id, state="finishing"):
        # Another live hook owns this window's pane. Skip without waiting and
        # leave the state alone: unlike a skipped Edit, a skipped Read leaves
        # no buffer behind disk, so there is nothing to mark stale.
        return 0
    try:
        return _navigate_for_read(session, payload, file_path, cfg)
    except nvim_connect.RpcTimeout as timeout:
        # An nvim that stopped answering mid-Read (a prompt the user has up in
        # an adopted nvim, a `:!make` started meanwhile): the Read changes no
        # buffer content, so one line and nothing else.
        logger.warning("skipped the Read of %s: %s", file_path, timeout)
        return 0
    finally:
        _drain_unconsumed_signals(
            session.window_id, FollowerState.read(session.window_id), session.in_tmux
        )
        control.clear_animating(session.window_id)


def _navigate_for_read(
    session: Session, payload: dict[str, Any], file_path: str, cfg: config.Config
) -> int:
    """The Read's navigation; the caller holds the window's slot."""
    reader = writer_cue.writer_identity(payload)
    if _edit_in_flight(session.window_id, file_path, reader):
        # What is known: an Edit/MultiEdit/Write of this file ran its pre
        # hook less than IN_FLIGHT_TTL_SECONDS ago and has not posted, and
        # the file no longer holds that edit's snapshot. Whether it is about
        # to post or was denied is not known here. It is ANOTHER writer's
        # only when both identities are known: a payload without one could
        # be the writer itself.
        writer = snapshot.in_flight_writer(session.window_id, file_path)
        whose = "another writer's pre hook ran" if reader and writer else "writer unknown"
        logger.info(
            "skipped the Read of %s: an Edit/Write of it is in flight or failed "
            "(%s, no post hook yet, and the file changed since)",
            file_path,
            whose,
        )
        return 0
    current = _live_follower_healing_keys(session) or _maybe_auto_open(session, file_path, cfg)
    if current is None:
        _warn_lost_window_identity(session)
        return 0
    busy = _busy_follower(current, cfg.take_over_question_seconds)
    if busy is not None:
        logger.warning("skipped the Read of %s: %s", file_path, busy)
        return 0
    # window_id is what lets an NvimFollower read its FollowerState: without
    # it an adopted nvim looks dedicated (locked on Read, its prompts answered).
    follower = get_follower(current.backend, current.target, window_id=session.window_id)
    try:
        _ensure_buffer(session.window_id, follower, file_path)
    except NavigationFailed as failure:
        # Nothing after the failed navigation may run: the offset jump would
        # move the cursor in whatever buffer is current.
        logger.warning("skipped the Read of %s: %s", file_path, failure)
        return 0
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
