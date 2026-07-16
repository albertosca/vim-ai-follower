"""User-facing follower lifecycle commands: start, stop, status, pause, interrupt, toggle, speed."""

from __future__ import annotations

import sys
from typing import Literal

from vim_ai_follower import config, control, keybindings, tmux
from vim_ai_follower.backends import get_follower
from vim_ai_follower.backends.tmux_vim import TmuxVimFollower
from vim_ai_follower.state import FollowerState, nvim_socket_path
from vim_ai_follower.tmux import TmuxPane, TmuxSession, adopt_target


def _require_session(env: dict[str, str]) -> TmuxSession | None:
    """Resolve the tmux session, or print the standard not-in-tmux error to
    stderr and return None (callers that get None return exit code 1).
    cmd_status deliberately does not use this: it reports absence on stdout
    and exits 0, so it keeps its own inline check."""
    session = TmuxSession.from_env(env)
    if session is None:
        print("claude-follow: not running inside tmux", file=sys.stderr)
    return session


def cmd_start(
    env: dict[str, str],
    backend: str = "tmux",
    on_failure: str | None = None,
    speed: str | None = None,
) -> int:
    session = _require_session(env)
    if session is None:
        return 1
    if FollowerState.get(session.session_id) is not None:
        print("claude-follow: follower already running for this session")
        return 0

    defaults = config.load()
    resolved_on_failure = on_failure if on_failure is not None else defaults.on_failure
    resolved_speed = speed if speed is not None else defaults.speed
    origin = env["TMUX_PANE"]
    keybindings.register()

    if backend == "nvim_rpc":
        socket_path = nvim_socket_path(session.session_id)
        follower = get_follower("nvim_rpc", str(socket_path))
        if not follower.is_alive():
            print(
                "claude-follow: no Neovim RPC socket found at "
                f"{socket_path} — open Neovim in this tmux session first "
                "(needs vim.fn.serverstart() wired to that path)",
                file=sys.stderr,
            )
            return 1
        FollowerState.set(
            session.session_id,
            "nvim_rpc",
            str(socket_path),
            origin=origin,
            on_failure=resolved_on_failure,
            speed=resolved_speed,
        )
        print(f"claude-follow: attached to Neovim at {socket_path}")
        return 0

    if defaults.adopt_existing:
        adopt = adopt_target(origin)
        if adopt is not None:
            FollowerState.set(
                session.session_id,
                "tmux",
                adopt,
                origin=origin,
                on_failure=resolved_on_failure,
                speed=resolved_speed,
                adopted=True,
                shown_any=True,
            )
            print(f"claude-follow: adopted existing vim pane {adopt}")
            return 0

    started = TmuxVimFollower.start(origin)
    FollowerState.set(
        session.session_id,
        "tmux",
        started.pane_id,
        origin=origin,
        on_failure=resolved_on_failure,
        speed=resolved_speed,
    )
    print(f"claude-follow: started follower in pane {started.pane_id}")
    return 0


def cmd_stop(env: dict[str, str]) -> int:
    session = _require_session(env)
    if session is None:
        return 1
    existing = FollowerState.get(session.session_id)
    if existing is not None:
        if existing.adopted:
            # Adoption never took ownership of the pane — killing the
            # user's own Vim on stop would be destructive. Only close the
            # tabs the follower itself opened there.
            follower = TmuxVimFollower(pane_id=existing.target, session_id=session.session_id)
            for path in existing.open_files:
                follower.close_tab(path)
        else:
            get_follower(existing.backend, existing.target).stop()
    FollowerState.clear(session.session_id)
    control.clear_signals(session.session_id)
    control.discard_pending_animation(session.session_id)
    keybindings.unregister()
    print("claude-follow: stopped")
    return 0


def cmd_status(env: dict[str, str]) -> int:
    session = TmuxSession.from_env(env)
    if session is None:
        print("claude-follow: not running inside tmux")
        return 0
    existing = FollowerState.get(session.session_id)
    if existing is None:
        print("claude-follow: no follower active")
    else:
        print(
            f"claude-follow: active, backend {existing.backend} "
            f"({existing.target}), showing {existing.current_file}, "
            f"on_failure={existing.on_failure}, speed={existing.speed}"
        )
    return 0


def _show_popup(current: FollowerState | None, message: str) -> None:
    """Popup on the follower pane confirming a pause/resume/interrupt —
    feedback for the keybinding press itself, not a guarantee that an
    animation was actually running to be affected. Only the tmux backend
    has a pane to target."""
    if current is None or current.backend != "tmux":
        return
    tmux.show_popup(current.target, message)


def _resave_pending(
    session_id: str, pending: control.PendingApplyEdit | control.PendingShowFresh
) -> None:
    """Put a loaded-but-unusable pending animation back on disk (loading
    consumes the file, and e.g. a dead follower shouldn't cost the user
    their resumable state)."""
    if isinstance(pending, control.PendingApplyEdit):
        control.save_pending_apply_edit(
            session_id, pending.ops, pending.pace_seconds, file_path=pending.file_path
        )
    else:
        control.save_pending_show_fresh(
            session_id,
            pending.lines,
            pending.pace_seconds,
            continuation=pending.continuation,
            file_path=pending.file_path,
        )


def cmd_pause(env: dict[str, str]) -> int:
    session = _require_session(env)
    if session is None:
        return 1
    current = FollowerState.get(session.session_id)

    state = control.animating_state(session.session_id)
    if state == "running":
        control.request_pause(session.session_id)
        print("claude-follow: pause requested")
        _show_popup(current, "Paused")
        return 0
    if state == "paused":
        control.request_pause(session.session_id)  # the toggle: resumes the waiting hook
        print("claude-follow: resume requested")
        _show_popup(current, "Resuming")
        return 0
    if state == "handoff":
        print("claude-follow: interrupted — save (:w!) to release Claude, or press S again")
        return 0

    pending = control.load_pending_animation(session.session_id)
    if pending is None:
        # Nothing running and nothing recoverable — a pause press must not
        # fake feedback: no signal, no popup.
        print("claude-follow: nothing to pause")
        return 0

    if current is None:
        _resave_pending(session.session_id, pending)
        print("claude-follow: no follower registered to resume", file=sys.stderr)
        return 1

    follower = get_follower(
        current.backend,
        current.target,
        config.pace_seconds_for(current.speed),
        session_id=session.session_id,
    )
    assert isinstance(follower, TmuxVimFollower)  # only tmux ever persists pending state
    _show_popup(current, "Resuming")
    result = follower.resume(pending)
    print(f"claude-follow: resumed ({result.outcome})")
    if result.outcome == "interrupted":
        _show_popup(current, "Interrupted")
    return 0


def cmd_interrupt(env: dict[str, str]) -> int:
    session = _require_session(env)
    if session is None:
        return 1
    current = FollowerState.get(session.session_id)

    state = control.animating_state(session.session_id)
    if state in ("running", "paused"):
        control.request_interrupt(session.session_id)
        print("claude-follow: interrupt requested")
        _show_popup(current, "Interrupted")
        return 0
    if state == "handoff":
        # the des-interrupt: discard the user's unsaved typing and release
        # Claude as if the interrupt had not happened
        control.request_interrupt(session.session_id)
        print("claude-follow: hand-off cancelled, unsaved changes discarded")
        _show_popup(current, "Discarded")
        return 0

    pending = control.load_pending_animation(session.session_id)
    if pending is not None:
        # A crash-orphaned remainder (its hook died): nothing to signal —
        # discard it and hand the buffer to the user, like a live interrupt.
        if current is not None and current.backend == "tmux":
            TmuxVimFollower(pane_id=current.target, session_id=session.session_id).hand_over()
        FollowerState.update_current_file(session.session_id, None)
        print("claude-follow: paused animation discarded, buffer handed over")
        _show_popup(current, "Interrupted")
        return 0

    print("claude-follow: nothing to interrupt")
    return 0


def cmd_speed(env: dict[str, str], direction: Literal["up", "down"]) -> int:
    session = _require_session(env)
    if session is None:
        return 1
    current = FollowerState.get(session.session_id)
    if current is None:
        print("claude-follow: no follower active")
        return 0
    new_speed = config.next_speed(current.speed, direction)
    FollowerState.update(session.session_id, speed=new_speed)
    # Saturating scale: label the ends so a press that changed nothing
    # reads as "already at the limit", not as a silent miss.
    if new_speed == config.SPEED_ORDER[-1]:
        label = f"{new_speed} (fastest)"
    elif new_speed == config.SPEED_ORDER[0]:
        label = f"{new_speed} (slowest)"
    else:
        label = new_speed
    print(f"claude-follow: speed {label}")
    # Status line, not popup: a popup grabs the keyboard and blocks the
    # next +/_ press until it closes.
    tmux.show_status(current.target, f"Speed: {label}")
    return 0


def cmd_toggle(env: dict[str, str]) -> int:
    session = _require_session(env)
    if session is None:
        return 1
    raw = FollowerState.read(session.session_id)
    if raw is None:
        print("claude-follow: no follower to toggle")
        return 0
    if raw.enabled:
        FollowerState.update(session.session_id, enabled=False)
        if raw.origin:
            TmuxPane(pane_id=raw.origin).set_zoomed(True)
        print("claude-follow: follower muted")
        return 0
    # Re-enable. open_files is cleared so every next touch resyncs via a
    # fresh retype — the disk moved while we were muted, and animating a
    # diff over a stale buffer would produce garbage. Tabs stay for reading.
    FollowerState.update(session.session_id, enabled=True, open_files=())
    if raw.origin:
        TmuxPane(pane_id=raw.origin).set_zoomed(False)
    if raw.backend == "tmux" and FollowerState.get(session.session_id) is None and raw.origin:
        started = TmuxVimFollower.start(raw.origin)
        FollowerState.set(
            session.session_id,
            "tmux",
            started.pane_id,
            origin=raw.origin,
            on_failure=raw.on_failure,
            speed=raw.speed,
        )  # shown_any defaults False, so the first file reuses (renames) the
        # new pane's start screen instead of opening a redundant tab.
    print("claude-follow: follower resumed")
    return 0
