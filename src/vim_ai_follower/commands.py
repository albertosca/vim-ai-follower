"""User-facing follower lifecycle commands: start, stop, status, pause, interrupt, toggle, speed."""

from __future__ import annotations

import subprocess
import sys
from typing import Literal

from vim_ai_follower import cache, config, control, keybindings, snapshot, tmux
from vim_ai_follower.backends import get_follower
from vim_ai_follower.backends.nvim_connect import launch_standalone_nvim, resolve_nvim_target
from vim_ai_follower.backends.tmux_vim import TmuxVimFollower
from vim_ai_follower.session import Session, other_live_followers, resolve_session
from vim_ai_follower.state import FollowerState
from vim_ai_follower.status_surface import status_surface_for
from vim_ai_follower.tmux import TmuxPane, adopt_target

VIM_NEEDS_TMUX = (
    "claude-follow: the vim backend requires tmux — run inside a tmux session, "
    "or set backend to nvim"
)

_NVIM_WINDOW_NEVER_WITHOUT_TMUX = (
    "claude-follow: nvim_window is 'never' but there is no tmux session — "
    "set nvim_window to auto/always, or start inside tmux"
)


def _launch_standalone_or_report(window_id: str) -> str | None:
    """Open a standalone nvim window, or print the actionable error and return
    None. The launcher shells out (osascript / nvim-qt / open -a VimR) and can
    exit non-zero — most commonly when macOS Automation permission for
    Terminal.app has not been granted yet — which must surface as the message
    the spec promises, not a raw CalledProcessError traceback."""
    try:
        return launch_standalone_nvim(window_id)
    except subprocess.CalledProcessError as exc:
        print(f"claude-follow: could not open a standalone nvim window ({exc})", file=sys.stderr)
        return None


def _unresolvable_pane(env: dict[str, str]) -> str:
    """The message for a `resolve_session` that came back None.

    That happens ONLY when TMUX_PANE is set and tmux could not resolve it —
    never when TMUX_PANE is absent, which now yields a pid-recovered or
    synthetic identity instead. The old wording ("not running inside tmux")
    said the opposite of the one condition that produces it, and reading it
    at face value cost half an hour on 2026-09-22."""
    pane = env.get("TMUX_PANE", "")
    return (
        f"claude-follow: tmux could not resolve TMUX_PANE={pane} — the pane is gone, "
        "or the tmux server was restarted since this shell started"
    )


def cmd_start(
    env: dict[str, str],
    backend: str | None = None,
    on_failure: str | None = None,
    speed: str | None = None,
) -> int:
    session = resolve_session(env)
    if session is None:
        print(_unresolvable_pane(env), file=sys.stderr)
        return 1
    if FollowerState.get(session.window_id) is not None:
        print("claude-follow: follower already running for this window")
        return 0

    defaults = config.load()
    # An explicit --backend wins; otherwise honor config.backend (tmux|nvim).
    resolved_backend = backend if backend is not None else defaults.backend
    resolved_on_failure = on_failure if on_failure is not None else defaults.on_failure
    resolved_speed = speed if speed is not None else defaults.speed

    if not session.in_tmux and resolved_backend == "tmux":
        # The tmux backend drives a tmux split — nothing to attach to
        # standalone, and no tmux session to fail gracefully into.
        print(VIM_NEEDS_TMUX)
        return 1

    if not session.in_tmux:
        # Standalone + nvim: no tmux server to bind keys on (keybindings are
        # tmux prefix-key bindings), so registration is skipped here — only
        # the in-tmux paths below register them.
        if defaults.nvim_window == "never":
            print(_NVIM_WINDOW_NEVER_WITHOUT_TMUX)
            return 1
        sock = _launch_standalone_or_report(session.window_id)
        if sock is None:
            return 1
        FollowerState.set(
            session.window_id,
            "nvim",
            sock,
            origin="",
            on_failure=resolved_on_failure,
            speed=resolved_speed,
            adopted=False,
        )
        print(f"claude-follow: attached to standalone Neovim at {sock}")
        return 0

    origin = session.origin
    assert origin is not None  # in_tmux sessions always carry TMUX_PANE
    keybindings.register()

    if resolved_backend == "nvim":
        if defaults.nvim_window == "always":
            # nvim_window=always overrides the usual tmux split even though
            # we're inside tmux: open a visible standalone window instead.
            sock = _launch_standalone_or_report(session.window_id)
            if sock is None:
                return 1
            FollowerState.set(
                session.window_id,
                "nvim",
                sock,
                origin=origin,
                on_failure=resolved_on_failure,
                speed=resolved_speed,
                adopted=False,
            )
            print(f"claude-follow: attached to standalone Neovim at {sock}")
            return 0
        # Adopt a running nvim's socket, or launch a dedicated one; either way
        # persist the socket to drive and whether we adopted (adopted nvims are
        # never relocked or killed).
        sock, launched = resolve_nvim_target(
            origin, session.window_id, adopt=defaults.adopt_existing
        )
        FollowerState.set(
            session.window_id,
            "nvim",
            sock,
            origin=origin,
            on_failure=resolved_on_failure,
            speed=resolved_speed,
            adopted=not launched,
        )
        print(f"claude-follow: attached to Neovim at {sock}")
        return 0

    if defaults.adopt_existing:
        adopt = adopt_target(origin)
        if adopt is not None:
            FollowerState.set(
                session.window_id,
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
        session.window_id,
        "tmux",
        started.pane_id,
        origin=origin,
        on_failure=resolved_on_failure,
        speed=resolved_speed,
    )
    print(f"claude-follow: started follower in pane {started.pane_id}")
    return 0


def _other_live_follower(window_id: str) -> bool:
    """True when any OTHER window still has a live follower — the keys are
    server-global, so only the last stop may unregister them. Dead
    candidates found on the way are collected (state, signals, pending,
    animating marker, snapshots) so a stale .pane can never hold the keys
    hostage. Keys are window ids (@N) since the window-scoping change;
    anything else is legacy session-keyed state whose scope no longer
    exists, collected regardless of pane liveness."""
    found_live = False
    for path in sorted(cache.CACHE_DIR.glob("*.pane")):
        key = path.stem
        if key == window_id:
            continue
        if key.startswith("@") and FollowerState.get(key) is not None:
            found_live = True
            continue
        FollowerState.clear(key)
        control.clear_signals(key)
        control.clear_animating(key)
        control.discard_pending_animation(key)
        snapshot.clear(key)
    return found_live


def cmd_stop(env: dict[str, str]) -> int:
    session = resolve_session(env)
    if session is None:
        print(_unresolvable_pane(env), file=sys.stderr)
        return 1
    existing = FollowerState.get(session.window_id)
    if existing is not None:
        # Restore the border BEFORE teardown, while the pane is still alive:
        # a non-adopted stop kills the follower pane, and pane-border-status
        # is a WINDOW option — `set-option -wu` aimed at the killed pane
        # fails to resolve its target, leaving the whole window's
        # pane-border-status stuck on "top". Unsetting it (and the per-pane
        # color) while the pane still exists lands cleanly. This dispatches
        # on existing.backend (TmuxStatusSurface vs NvimStatusSurface), so a
        # standalone nvim follower's floating status window is still cleared
        # here without touching tmux — no separate in_tmux gate needed.
        status_surface_for(existing).clear()
        if existing.adopted:
            # Adoption never took ownership of the pane — killing the
            # user's own Vim on stop would be destructive. Only close the
            # tabs the follower itself opened there.
            follower = get_follower(existing.backend, existing.target, window_id=session.window_id)
            for path in existing.open_files:
                follower.close_tab(path)
        else:
            # Runs standalone too: nvim's stop() is an RPC `qall!`, not a
            # tmux operation, so it must fire regardless of session.in_tmux.
            get_follower(existing.backend, existing.target, window_id=session.window_id).stop()
    # Scan before clearing this window's own state: FollowerState.clear
    # below deletes this window's .pane, and scanning after that would make
    # the "skip my own key" check below unreachable — the glob would never
    # see it in the first place.
    other_live = _other_live_follower(session.window_id)
    FollowerState.clear(session.window_id)
    control.clear_signals(session.window_id)
    control.discard_pending_animation(session.window_id)
    if session.in_tmux and not other_live:
        # Keybindings are tmux prefix-key bindings — standalone never
        # registered them (cmd_start skips registration outside tmux), so
        # unregistering here would either no-op against a foreign tmux
        # server or, worse, unbind real keys on one that happens to be
        # running on the machine.
        keybindings.unregister()
    print("claude-follow: stopped")
    return 0


def _identity_label(session: Session) -> str:
    return f"{session.window_id} ({'tmux' if session.in_tmux else 'not in tmux'})"


def cmd_status(env: dict[str, str]) -> int:
    """Always says WHICH identity it is answering about.

    A bare "no follower active" is what made the 2026-09-22 incident cost half
    an hour: it was answering truthfully about the synthetic term-<id> this run
    had fallen back to, and was read as "there is no follower" while the real
    window's follower was alive the whole time. Naming the identity, and
    listing the live followers found elsewhere, makes those two states
    impossible to confuse.

    The other-followers list is printed only when this identity has no
    follower. On the happy path it would be noise — Alberto routinely has
    followers in several windows at once — and this is a CLI he reads often."""
    session = resolve_session(env)
    if session is None:
        print(_unresolvable_pane(env))
        return 0
    existing = FollowerState.get(session.window_id)
    if existing is not None:
        print(
            f"claude-follow: active for {_identity_label(session)}, "
            f"backend {existing.backend} ({existing.target}), "
            f"showing {existing.current_file}, "
            f"on_failure={existing.on_failure}, speed={existing.speed}"
        )
        return 0
    print(f"claude-follow: no follower for {_identity_label(session)}")
    others = other_live_followers(session.window_id)
    if others:
        print(
            f"claude-follow: {len(others)} live follower(s) under other identities — "
            "this session may have lost its window identity:"
        )
        for other in others:
            print(
                f"  {other.window_id}: {other.backend} {other.target}, showing {other.current_file}"
            )
    return 0


def _show_popup(current: FollowerState | None, message: str, in_tmux: bool) -> None:
    """Popup on the follower pane confirming a pause/resume/interrupt —
    feedback for the keybinding press itself, not a guarantee that an
    animation was actually running to be affected. Only the tmux backend
    has a pane to target, and only when the run itself is inside tmux —
    standalone has no tmux server to shell out to."""
    if not in_tmux or current is None or current.backend != "tmux":
        return
    tmux.show_popup(current.target, message)


def _pause_feedback(current: FollowerState | None, in_tmux: bool, *, paused: bool) -> None:
    """Feedback for a pause/resume keypress. tmux flashes a popup on the
    follower pane; nvim has no pane to target (its target is an RPC socket),
    so it shows "Paused" in the floating status window instead, cleared on
    resume — that nvim path works standalone as well as in tmux."""
    if current is None:
        return
    if current.backend == "nvim":
        # Resuming restores the "Writing..." activity line rather than closing
        # the box, so a writer cue's title/border survive the pause.
        status_surface_for(current).set_state("Paused" if paused else "Writing...")
        return
    _show_popup(current, "Paused" if paused else "Resuming", in_tmux)


def _resave_pending(
    window_id: str, pending: control.PendingApplyEdit | control.PendingShowFresh
) -> None:
    """Put a loaded-but-unusable pending animation back on disk (loading
    consumes the file, and e.g. a dead follower shouldn't cost the user
    their resumable state)."""
    if isinstance(pending, control.PendingApplyEdit):
        control.save_pending_apply_edit(
            window_id,
            pending.ops,
            pending.pace_seconds,
            file_path=pending.file_path,
            partial=pending.partial,
        )
    else:
        control.save_pending_show_fresh(
            window_id,
            pending.lines,
            pending.pace_seconds,
            continuation=pending.continuation,
            file_path=pending.file_path,
            partial=pending.partial,
        )


def cmd_pause(env: dict[str, str]) -> int:
    session = resolve_session(env)
    if session is None:
        print(_unresolvable_pane(env), file=sys.stderr)
        return 1
    current = FollowerState.get(session.window_id)

    state = control.animating_state(session.window_id)
    if state == "running":
        control.request_pause(session.window_id)
        print("claude-follow: pause requested")
        _pause_feedback(current, session.in_tmux, paused=True)
        return 0
    if state == "paused":
        control.request_pause(session.window_id)  # the toggle: resumes the waiting hook
        print("claude-follow: resume requested")
        _pause_feedback(current, session.in_tmux, paused=False)
        return 0
    if state == "handoff":
        print("claude-follow: interrupted — save (:w!) to release Claude, or press S again")
        return 0

    pending = control.load_pending_animation(session.window_id)
    if pending is None:
        # Nothing running and nothing recoverable — a pause press must not
        # fake feedback: no signal, no popup.
        print("claude-follow: nothing to pause")
        return 0

    if current is None:
        _resave_pending(session.window_id, pending)
        print("claude-follow: no follower registered to resume", file=sys.stderr)
        return 1

    if current.backend != "tmux":
        # The keyboard replay below is inherently tmux-only (send-keys). A
        # crash-orphaned nvim pending has no keyboard resume path yet, so
        # discard it rather than crash on the tmux-only resume — mirrors the
        # backend guard cmd_interrupt already applies to this same path.
        control.discard_pending_animation(session.window_id)
        print("claude-follow: nothing to resume")
        return 0

    follower = get_follower(
        current.backend,
        current.target,
        config.pace_seconds_for(current.speed),
        window_id=session.window_id,
    )
    assert isinstance(follower, TmuxVimFollower)  # only tmux ever persists pending state
    _show_popup(current, "Resuming", session.in_tmux)
    result = follower.resume(pending)
    print(f"claude-follow: resumed ({result.outcome})")
    if result.outcome == "interrupted":
        _show_popup(current, "Interrupted", session.in_tmux)
    return 0


def cmd_interrupt(env: dict[str, str]) -> int:
    session = resolve_session(env)
    if session is None:
        print(_unresolvable_pane(env), file=sys.stderr)
        return 1
    current = FollowerState.get(session.window_id)

    state = control.animating_state(session.window_id)
    if state in ("running", "paused"):
        control.request_interrupt(session.window_id)
        print("claude-follow: interrupt requested")
        _show_popup(current, "Interrupted", session.in_tmux)
        return 0
    if state == "handoff":
        # the des-interrupt: discard the user's unsaved typing and release
        # Claude as if the interrupt had not happened
        control.request_interrupt(session.window_id)
        print("claude-follow: hand-off cancelled, unsaved changes discarded")
        _show_popup(current, "Discarded", session.in_tmux)
        return 0

    pending = control.load_pending_animation(session.window_id)
    if pending is not None:
        # A crash-orphaned remainder (its hook died): nothing to signal —
        # discard it and hand the buffer to the user, like a live interrupt.
        if current is not None and current.backend == "tmux":
            TmuxVimFollower(pane_id=current.target, window_id=session.window_id).hand_over()
        FollowerState.update_current_file(session.window_id, None)
        print("claude-follow: paused animation discarded, buffer handed over")
        _show_popup(current, "Interrupted", session.in_tmux)
        return 0

    print("claude-follow: nothing to interrupt")
    return 0


def cmd_speed(env: dict[str, str], direction: Literal["up", "down"]) -> int:
    session = resolve_session(env)
    if session is None:
        print(_unresolvable_pane(env), file=sys.stderr)
        return 1
    current = FollowerState.get(session.window_id)
    if current is None:
        print("claude-follow: no follower active")
        return 0
    new_speed = config.next_speed(current.speed, direction)
    FollowerState.update(session.window_id, speed=new_speed)
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
    # next +/_ press until it closes. Deliberately not routed through
    # StatusSurface.set_state: that method save-and-restores the pane's
    # title/border, which is the right behavior for a persistent cue
    # (handoff) but would wrongly latch this transient status-line message
    # as something to restore later.
    if session.in_tmux:  # a standalone follower has no tmux status line to flash
        tmux.show_status(current.target, f"Speed: {label}")
    return 0


def cmd_toggle(env: dict[str, str]) -> int:
    session = resolve_session(env)
    if session is None:
        print(_unresolvable_pane(env), file=sys.stderr)
        return 1
    raw = FollowerState.read(session.window_id)
    if raw is None:
        print("claude-follow: no follower to toggle")
        return 0
    if raw.enabled:
        FollowerState.update(session.window_id, enabled=False)
        if raw.origin:
            TmuxPane(pane_id=raw.origin).set_zoomed(True)
        print("claude-follow: follower muted")
        return 0
    # Re-enable. open_files is cleared so every next touch resyncs via a
    # fresh retype — the disk moved while we were muted, and animating a
    # diff over a stale buffer would produce garbage. Tabs stay for reading.
    FollowerState.update(session.window_id, enabled=True, open_files=())
    if raw.origin:
        TmuxPane(pane_id=raw.origin).set_zoomed(False)
    if raw.backend == "tmux" and FollowerState.get(session.window_id) is None and raw.origin:
        started = TmuxVimFollower.start(raw.origin)
        FollowerState.set(
            session.window_id,
            "tmux",
            started.pane_id,
            origin=raw.origin,
            on_failure=raw.on_failure,
            speed=raw.speed,
        )  # shown_any defaults False, so the first file reuses (renames) the
        # new pane's start screen instead of opening a redundant tab.
    print("claude-follow: follower resumed")
    return 0
