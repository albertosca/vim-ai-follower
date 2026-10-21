"""User-facing follower lifecycle commands: start, stop, status, pause, interrupt, toggle, speed."""

from __future__ import annotations

import contextlib
import functools
import os
import shlex
import signal
import subprocess
import sys
import time
from collections.abc import Callable
from typing import TYPE_CHECKING, Any, Literal, cast

from vim_ai_follower import binding, cache, config, control, hooks, keybindings, snapshot, tmux
from vim_ai_follower import state as state_module
from vim_ai_follower.backends import (
    NavigationFailed,
    get_follower,
    nvim_connect,
    nvim_prompt,
    tmux_vim,
)
from vim_ai_follower.backends.nvim_connect import (
    NvimNeverListened,
    RpcTimeout,
    StandaloneLaunchFailed,
    clear_launch_backoff,
    launch_standalone_nvim,
    resolve_nvim_target,
    rpc_deadline,
)
from vim_ai_follower.backends.tmux_vim import TmuxVimFollower
from vim_ai_follower.session import Session, other_live_followers, resolve_session
from vim_ai_follower.state import FollowerState, clear_stopped, mark_stopped, stopped_path
from vim_ai_follower.status_surface import status_surface_for
from vim_ai_follower.tmux import TmuxPane, adopt_target

if TYPE_CHECKING:
    from vim_ai_follower.backends.nvim import NvimFollower

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
    # An explicit start is the retry a failed auto-open's backoff waits for.
    clear_launch_backoff(window_id)
    try:
        return launch_standalone_nvim(window_id)
    except subprocess.CalledProcessError as error:
        print(f"claude-follow: could not open a standalone nvim window ({error})", file=sys.stderr)
        return None
    except NvimNeverListened as error:
        print(
            f"claude-follow: could not open a standalone nvim window ({error}) — check that "
            f"{error.launcher} can start nvim, then run start again",
            file=sys.stderr,
        )
        return None
    except StandaloneLaunchFailed as error:
        print(f"claude-follow: could not open a standalone nvim window ({error})", file=sys.stderr)
        return None


def _split_nvim_or_report(origin: str, window_id: str, adopt: bool) -> tuple[str, bool] | None:
    """resolve_nvim_target for `start`, or print why and return None. An
    explicit start is the retry a failed split's backoff waits for, as for a
    standalone window: it splits at once and gives nvim the patient
    START_SPLIT_WAIT_SECONDS. A split that still never answers keeps its pane
    (nvim's error is on it), records nothing, and backs auto-open off again,
    so the next hook does not split a second pane beside it."""
    clear_launch_backoff(window_id)
    try:
        return resolve_nvim_target(
            origin, window_id, adopt=adopt, wait_seconds=nvim_connect.START_SPLIT_WAIT_SECONDS
        )
    except (nvim_connect.SplitNvimNeverAnswered, StandaloneLaunchFailed) as error:
        nvim_connect.record_launch_failure(window_id)
        message = str(error)
    except subprocess.CalledProcessError as error:  # tmux refused the split: no backoff
        detail = (error.stderr or "").strip() or str(error)
        message = f"tmux could not split a pane for nvim: {detail}"
    hooks._configure_logging()
    hooks.logger.warning("start: %s", message)
    print(f"claude-follow: {message}", file=sys.stderr)
    return None


def _unresolvable_pane(environment: dict[str, str]) -> str:
    """The message for a `resolve_session` that came back None.

    That happens ONLY when TMUX_PANE is set and tmux could not resolve it —
    never when TMUX_PANE is absent, which now yields a pid-recovered or
    synthetic identity instead. The old wording ("not running inside tmux")
    said the opposite of the one condition that produces it, and reading it
    at face value cost half an hour on 2026-09-22."""
    pane = environment.get("TMUX_PANE", "")
    return (
        f"claude-follow: tmux could not resolve TMUX_PANE={pane} — the pane is gone, "
        "or the tmux server was restarted since this shell started"
    )


def _claim_keys(take_keys: bool) -> keybindings.Claim:
    """start is a repair (repair=True: a restarted tmux server drops the keys
    while the cache still names us) and the one place --take-keys applies."""
    result = keybindings.claim(force=take_keys, repair=True)
    note = keybindings.describe(result)
    if note is not None:
        print(f"claude-follow: {note}")
    return result


def cmd_start(
    environment: dict[str, str],
    backend: str | None = None,
    on_failure: str | None = None,
    speed: str | None = None,
    take_keys: bool = False,
) -> int:
    with contextlib.suppress(OSError):
        nvim_prompt.sweep_answer_locks()
    with contextlib.suppress(OSError):
        nvim_connect.sweep_stale_sockets()
    session = resolve_session(environment)
    if session is None:
        print(_unresolvable_pane(environment), file=sys.stderr)
        return 1
    if FollowerState.get(session.window_id) is not None:
        # A live follower under a tombstone (a stop that raced its start):
        # this start is the user asking for it, so the stop no longer holds.
        clear_stopped(session.window_id)
        message = "claude-follow: follower already running for this window"
        if session.in_tmux:
            # The keys are tmux SERVER-global while follower state is
            # per-window, so they can be dead (bound to a path that no longer
            # exists, exit 127 into /dev/null) while this window's state is
            # perfectly healthy. `start` is precisely what a user retries
            # then, and returning here before register() made that retry a
            # no-op. Re-registering is safe: register() records the user's
            # previous bindings only when the saved file does not exist yet,
            # so a second pass cannot overwrite them with our own.
            # claim() refuses to take keys from a live foreign installation;
            # then nothing was refreshed and the message must not say so.
            result = _claim_keys(take_keys)
            if result.outcome is not keybindings.ClaimOutcome.KEPT_FOREIGN:
                message += " — keybindings refreshed"
        print(message)
        return 0

    defaults = config.load()
    # An explicit --backend wins; otherwise honor config.backend (tmux|nvim).
    resolved_backend = backend if backend is not None else defaults.backend
    resolved_on_failure = on_failure if on_failure is not None else defaults.on_failure
    resolved_speed = speed if speed is not None else defaults.speed

    if not session.in_tmux and resolved_backend == "tmux":
        # The tmux backend drives a tmux split — nothing to attach to
        # standalone, and no tmux session to fail gracefully into.
        print(VIM_NEEDS_TMUX, file=sys.stderr)
        return 1

    if not session.in_tmux:
        # Standalone + nvim: no tmux server to bind keys on (keybindings are
        # tmux prefix-key bindings), so registration is skipped here — only
        # the in-tmux paths below register them.
        if defaults.nvim_window == "never":
            print(_NVIM_WINDOW_NEVER_WITHOUT_TMUX, file=sys.stderr)
            return 1
        sock = _launch_standalone_or_report(session.window_id)
        if sock is None:
            return 1
        if not _record_start(
            session.window_id,
            "nvim",
            sock,
            origin="",
            on_failure=resolved_on_failure,
            speed=resolved_speed,
            adopted=False,
        ):
            return _start_overtaken(session.window_id, "nvim", sock, launched=True)
        print(f"claude-follow: attached to standalone Neovim at {sock}")
        return 0

    origin = session.origin
    assert origin is not None  # in_tmux sessions always carry TMUX_PANE
    _claim_keys(take_keys)

    if resolved_backend == "nvim":
        if defaults.nvim_window == "always":
            # nvim_window=always overrides the usual tmux split even though
            # we're inside tmux: open a visible standalone window instead.
            sock = _launch_standalone_or_report(session.window_id)
            if sock is None:
                return 1
            if not _record_start(
                session.window_id,
                "nvim",
                sock,
                origin=origin,
                on_failure=resolved_on_failure,
                speed=resolved_speed,
                adopted=False,
            ):
                return _start_overtaken(session.window_id, "nvim", sock, launched=True)
            print(f"claude-follow: attached to standalone Neovim at {sock}")
            return 0
        # Adopt a running nvim's socket, or launch a dedicated one; either way
        # persist the socket to drive and whether we adopted (adopted nvims are
        # never relocked or killed).
        target = _split_nvim_or_report(origin, session.window_id, defaults.adopt_existing)
        if target is None:
            return 1
        sock, launched = target
        if not _record_start(
            session.window_id,
            "nvim",
            sock,
            origin=origin,
            on_failure=resolved_on_failure,
            speed=resolved_speed,
            adopted=not launched,
        ):
            return _start_overtaken(session.window_id, "nvim", sock, launched=launched)
        print(f"claude-follow: attached to Neovim at {sock}")
        return 0

    if defaults.adopt_existing:
        adopt = adopt_target(origin)
        if adopt is not None:
            if not _record_start(
                session.window_id,
                "tmux",
                adopt,
                origin=origin,
                on_failure=resolved_on_failure,
                speed=resolved_speed,
                adopted=True,
                shown_any=True,
            ):
                return _start_overtaken(session.window_id, "tmux", adopt, launched=False)
            print(f"claude-follow: adopted existing vim pane {adopt}")
            return 0

    started = TmuxVimFollower.start(origin)
    if not _record_start(
        session.window_id,
        "tmux",
        started.pane_id,
        origin=origin,
        on_failure=resolved_on_failure,
        speed=resolved_speed,
    ):
        return _start_overtaken(session.window_id, "tmux", started.pane_id, launched=True)
    print(f"claude-follow: started follower in pane {started.pane_id}")
    return 0


def _record_start(window_id: str, backend: str, target: str, **fields: Any) -> bool:
    """Record the follower a `start` just attached to. An explicit start is
    what lifts a `stop` (see cmd_stop), so the tombstone goes here: before
    the write it would refuse, and only once the start has something to
    record, since a start that failed must leave auto-open off. False when
    the write was refused anyway: a `stop` landed after the tombstone went."""
    clear_stopped(window_id)
    return FollowerState.set(window_id, backend, target, **fields)


def _close_unrecorded(window_id: str, backend: str, target: str) -> str:
    """Close an editor this command launched but could not record (a `stop`
    overtook the write): nothing names it, so no later `stop` would close it
    and a later adopting `start` would take it for the user's own. Returns
    what to tell the user. Best-effort, like every teardown step."""
    try:
        get_follower(backend, target, window_id=window_id).stop(dedicated=True)
    except Exception as error:
        hooks.logger.warning("could not close the unrecorded follower %s: %s", target, error)
        return f"; could not close the {backend} it opened ({target})"
    return f"; closed the {backend} it opened ({target})"


def _start_overtaken(window_id: str, backend: str, target: str, launched: bool) -> int:
    """A `start` whose write a racing `stop` refused (final review M11): it
    used to print "started" over no state and leave the editor it launched
    orphaned. An adopted editor is the user's and is never closed."""
    detail = _close_unrecorded(window_id, backend, target) if launched else ""
    print(
        f"claude-follow: window {window_id} was stopped while it was starting{detail}",
        file=sys.stderr,
    )
    return 1


def _other_live_follower() -> bool:
    """True when any OTHER window still has a live follower — the keys are
    server-global, so only the last stop may unregister them. Called after
    cmd_stop has cleared its own window's state, so every .pane left is
    another window's. Dead
    candidates found on the way are collected (state, signals, pending,
    animating marker, snapshots) so a stale .pane can never hold the keys
    hostage. Keys are window ids (@N) since the window-scoping change;
    anything else is legacy session-keyed state whose scope no longer
    exists, collected regardless of pane liveness."""
    found_live = False
    for path in sorted(cache.CACHE_DIRECTORY.glob("*.pane")):
        key = path.stem
        if key.startswith("@") and control.animating_state(key) is not None:
            # A live hook animates or waits there: its editor only LOOKS dead
            # (a dedicated Vim running `:!pytest` mid-hand-off shows the
            # shell as the pane's command), and collecting it took the
            # window's follower and remainder away (final review I4).
            found_live = True
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


_QUIT_GRACE_SECONDS = 1.0
_REAP_GRACE_SECONDS = 1.0
_POLL_SECONDS = 0.05


def cmd_stop(environment: dict[str, str]) -> int:
    """Stop this window's follower, in an order that cannot be left half done.

    The state goes FIRST, before anything talks to the editor: a teardown
    that hangs or fails (a frozen nvim, one at a hit-enter prompt) used to
    exit 1 with the `.pane` still there, so the follower was still "running"
    and the next hook drove it again. A tombstone (state.mark_stopped) keeps
    it stopped: no hook that read the state before this can write it back,
    and auto-open does not reopen the window, until `start` or toggle.

    The teardown is then best-effort and bounded, every step logged in
    hook.log rather than raised: an nvim gets one liveness probe and at most
    CONNECT_TIMEOUT_SECONDS + RPC_TIMEOUT_SECONDS for all its steps, and is
    sent nothing at all when it is busy, absent, or (adopted) waiting on its
    user. A DEDICATED nvim that does not quit gets its pane closed, since the
    pane is the follower's own; an adopted one's pane is never touched."""
    with contextlib.suppress(OSError):
        nvim_prompt.sweep_answer_locks()
    with contextlib.suppress(OSError):
        nvim_connect.sweep_stale_sockets()
    session = resolve_session(environment)
    if session is None:
        print(_unresolvable_pane(environment), file=sys.stderr)
        return 1
    window_id = session.window_id
    existing = _state_to_tear_down(window_id)
    mark_stopped(
        window_id,
        binding.tmux_server_pid_from(environment) if session.in_tmux else None,
        session.in_tmux,
        server_started=binding.tmux_server_started() if session.in_tmux else None,
    )
    FollowerState.clear(window_id)
    control.clear_signals(window_id)
    control.discard_pending_animation(window_id)
    if existing is not None:
        hooks._configure_logging()
        if existing.backend == "nvim":
            _tear_down_nvim(existing, session)
        else:
            _tear_down_tmux(existing, window_id)
        # A writer that passed set()'s tombstone check just before the
        # mark_stopped above may have landed its write during the teardown.
        FollowerState.clear(window_id)
    state_module.stopping_path(window_id).unlink(missing_ok=True)
    other_live = _other_live_follower()
    if session.in_tmux and not other_live:
        # Keybindings are tmux prefix-key bindings — standalone never
        # registered them (cmd_start skips registration outside tmux), so
        # unregistering here would either no-op against a foreign tmux
        # server or, worse, unbind real keys on one that happens to be
        # running on the machine.
        hooks._configure_logging()  # a binding it cannot restore is logged
        _teardown_step("keybinding restore", keybindings.unregister)
    print("claude-follow: stopped")
    return 0


def _state_to_tear_down(window_id: str) -> FollowerState | None:
    """The state this stop tears down. It is copied to the window's
    `.stopping` record first, which the stop removes only once the teardown
    is over: the state itself is cleared BEFORE the teardown, so a stop
    killed mid-teardown (SIGKILL, a closed terminal) would otherwise leave
    the follower's pane running with nothing naming it, and a second `stop`
    could not find it. With no state, that record is what a second stop
    finishes."""
    record = state_module.stopping_path(window_id)
    try:
        existing = FollowerState.read(window_id)
    except state_module.UNREADABLE_STATE_ERRORS:
        return None  # unreadable: nothing to tear down, still cleared by the caller
    if existing is None:
        try:
            return FollowerState.read_path(record)
        except state_module.UNREADABLE_STATE_ERRORS:
            return None
    state_module.keep_stopping_record(window_id)
    return existing


def _teardown_step(what: str, action: Callable[[], object]) -> bool:
    """One best-effort teardown step: False, and one hook.log line, when it
    raised. `stop` has already cleared the state, so nothing is left to
    retry, and a failure must not turn into an exit status."""
    try:
        action()
    except Exception as error:
        hooks.logger.warning("stop: %s failed: %s", what, error)
        return False
    return True


def _tear_down_tmux(existing: FollowerState, window_id: str) -> None:
    """Nothing reaches the pane unless it is proven to be this follower's
    pane now (final review of T17, C1 and C2): the `.pane` file outlives a
    tmux restart, and the new server numbers its panes from %0 again, so a
    stale state's target can be Claude's own pane or the user's shell, and
    a kill or typed Vim script would land there. The proof is the server
    the state was written under (state.proven_here; tmux never reuses a pane
    id within one server), and then a pane that still runs vim."""
    if not state_module.proven_here(existing):
        hooks.logger.warning(
            "stop: pane %s is not proven to be the follower's: its state was not written "
            "under this tmux server (recorded %s, this one %s); nothing was sent to it",
            existing.target,
            existing.server_pid,
            state_module.current_server_pid(),
        )
        return
    follower = get_follower("tmux", existing.target, window_id=window_id)
    # Restore the border BEFORE the pane goes: pane-border-status is a
    # WINDOW option, and `set-option -wu` aimed at a killed pane fails to
    # resolve its target, leaving the whole window's border status stuck on
    # "top". The state is already cleared, so the surface works from the
    # copy read above.
    _teardown_step("restoring the pane border", status_surface_for(existing).clear)
    if not follower.is_alive():
        hooks.logger.warning(
            "stop: pane %s no longer runs vim; nothing was sent to it", existing.target
        )
        return
    if existing.adopted:
        # A user typing in their own Vim is never asked and never typed into:
        # the Escape pair close_tab starts with would end their mode (or
        # reach a terminal window's job), and `stop` takes no answer
        # (Alberto, 2026-10-17). Its tabs stay, and so do the follower's
        # locks and the user's 'paste' the follower holds (final review I5):
        # a "release pending" mark for that Vim makes the next contact with it
        # while it is not typing (a later hook, or another stop) give them
        # back first (TmuxVimFollower._define).
        if control.animating_state(window_id) == "running":
            _defer_to_the_running_hook(existing)
            return
        try:
            typing = tmux_vim.typing_mode(existing.target)
        except Exception as error:  # never raised by stop; unknown sends nothing
            typing = f"its mode could not be read: {error}"
        if typing is not None:
            try:
                pending = tmux_vim.mark_release_pending(existing.target)
            except Exception:  # never raised by stop
                pending = False
            hooks.logger.warning(
                "stop: Vim in %s is waiting on its user (%s); nothing was sent to it, so the "
                "follower's locks stay on %s %s",
                existing.target,
                typing,
                ", ".join(existing.open_files) or "no file it read",
                "until the next contact with it while it is not typing"
                if pending
                else "(no release could be recorded: its Vim process was not found)",
            )
            return
        # Every claim first (s:release_all): the evictions below forget the
        # records a paused animation's buffer still needs, and none of them
        # gives 'paste' back.
        tmux_follower = cast(tmux_vim.TmuxVimFollower, follower)  # get_follower("tmux")
        _teardown_step("giving back the follower's locks", tmux_follower.release_claims)
        # Adoption never took ownership of the pane: only close the tabs the
        # follower opened, and never the user's work (close_tab leaves a
        # modified buffer, or one the user had open, and says so in hook.log).
        for path in existing.open_files:
            _teardown_step(
                f"closing {path}", functools.partial(follower.close_tab, path, dedicated=False)
            )
        return
    _teardown_step("closing the follower pane", functools.partial(follower.stop, dedicated=True))


def _defer_to_the_running_hook(existing: FollowerState) -> None:
    """A hook is animating an edit into this adopted Vim (sweep-4): its own
    insert mode read as the user typing, and the release's keys landed
    between its paced keys. So nothing is sent: a "release pending" mark is
    left, and the hook's last line to the Vim, once it finds its state gone,
    gives the claims back and takes the mark (TmuxVimFollower._release_if_
    stopped); a hook that dies first leaves it for the next contact. The
    follower's tabs stay open."""
    try:
        pending = tmux_vim.mark_release_pending(existing.target)
    except Exception:  # never raised by stop
        pending = False
    hooks.logger.warning(
        "stop: Vim in %s is being animated by a hook; nothing was sent to it, so the hook "
        "gives the follower's locks back when it finishes%s, and its tabs stay open",
        existing.target,
        "" if pending else " (no release could be recorded for a hook that dies first)",
    )


def _tear_down_nvim(existing: FollowerState, session: Session) -> None:
    """Bounded: the probe, every RPC step and the pane close all within
    CONNECT + RPC timeouts. The RPC steps end early enough to leave the pane
    close its two graces (the wait for a `qall!` to land and the reap).

    The RPC steps go to the state's socket whatever the tmux server: a
    dedicated socket is the follower's own file in the cache dir, and in an
    adopted nvim close_tab touches only what the follower created there (an
    editor variable, gone with the editor). The PANE close is tmux's, so it
    needs the state to be proven under this server, like the tmux teardown."""
    socket_path = existing.target
    ends_at = time.monotonic() + (
        nvim_connect.CONNECT_TIMEOUT_SECONDS + nvim_connect.RPC_TIMEOUT_SECONDS
    )
    rpc_ends_at = ends_at - _QUIT_GRACE_SECONDS - _REAP_GRACE_SECONDS
    verdict = nvim_connect.probe_nvim(socket_path)
    quit_sent = False
    if verdict != "answers":
        hooks.logger.warning(
            "stop: nvim %s did not answer (%s); nothing was sent to it", socket_path, verdict
        )
    elif existing.adopted and (
        (pending := hooks.adopted_user_pending(socket_path)) is not None
        or nvim_connect.last_probe_blocking(socket_path)
    ):
        quit_sent = _tear_down_blocked_adopted_nvim(
            existing, session.window_id, rpc_ends_at, pending
        )
    else:
        quit_sent = _run_nvim_teardown(existing, session.window_id, rpc_ends_at)
    if existing.adopted or not session.in_tmux or not existing.origin:
        return
    if not state_module.proven_here(existing):
        hooks.logger.warning(
            "stop: the pane of nvim %s is left alone: its state was not written under this "
            "tmux server (recorded %s, this one %s)",
            socket_path,
            existing.server_pid,
            state_module.current_server_pid(),
        )
        return
    _close_dedicated_nvim_pane(existing.origin, socket_path, wait_for_quit=quit_sent)


# The adopted_user_pending reasons under which `stop` still closes the
# follower's tabs the user is NOT in (T24c item 6, measured 2026-10-18 on a
# TUI nvim: the float's clear and the eviction outside the current tab left
# the user's mode, tab, window, buffer, cursor, visual start and half-typed
# command line as they were, and the user's next keys finished their work in
# their own buffer). Not the command-line window (the wipe is refused with
# E11 behind `silent!`) nor keys pending (every request waits for the user).
_STOP_CLOSES_OUTSIDE_THE_USERS_TAB = hooks.TYPING_MODES | {
    "a confirm prompt",
    "on the command line",
    "an operator pending",
}


def _tear_down_blocked_adopted_nvim(
    existing: FollowerState, window_id: str, ends_at: float, pending: str | None
) -> bool:
    """The user's own nvim answered `blocking: true`, or its user is busy:
    typing (insert, replace, terminal, visual or select mode), at a confirm,
    on the command line, with an operator pending, or in its command-line
    window. At a hit-enter or more-prompt the stop acts like any hook
    (decision (a)): it dismisses the prompt, as the Vim follower's Escape
    pair would, then tears down. A busy user is not interrupted: the stop
    closes only the follower's tabs the user is not in, one hook.log line
    for a follower buffer shown in the user's tab (_EVICT_LUA's "current").
    In the command-line window or with keys pending nothing is sent, and the
    follower's tabs stay; a prompt that cannot be answered leaves the same.
    The state is already gone either way. `pending` is the caller's
    `hooks.adopted_user_pending` answer, asked once: it can cost a raw
    request (the command-line window)."""
    socket_path = existing.target
    if pending in _STOP_CLOSES_OUTSIDE_THE_USERS_TAB:
        hooks.logger.warning(
            "stop: nvim %s is waiting on its user (%s); closing only the follower's tabs it is "
            "not in",
            socket_path,
            pending,
        )
        return _run_nvim_teardown(existing, window_id, ends_at, spare_current_tab=True)
    if pending is not None:
        hooks.logger.warning(
            "stop: nvim %s is waiting on its user (%s); the follower's tabs there are left open",
            socket_path,
            pending,
        )
        return False
    with rpc_deadline(max(ends_at - time.monotonic(), 0.001)):
        failure = hooks.sweep_adopted_prompt(socket_path, "stop")
    if failure is not None:
        hooks.logger.warning("stop: %s; the follower's tabs there are left open", failure)
        return False
    return _run_nvim_teardown(existing, window_id, ends_at)


def _run_nvim_teardown(
    existing: FollowerState, window_id: str, ends_at: float, spare_current_tab: bool = False
) -> bool:
    """The RPC steps, each under what is left of the budget; the first
    timeout or lost connection gives up the rest (the nvim stopped answering
    mid-teardown). True when a dedicated follower's `qall!` went out.
    `spare_current_tab` leaves an adopted nvim's follower buffers shown in
    the user's current tab (see _tear_down_blocked_adopted_nvim)."""
    follower = cast("NvimFollower", get_follower("nvim", existing.target, window_id=window_id))
    steps: list[tuple[str, Callable[[], object]]] = [
        ("clearing the status float", status_surface_for(existing).clear)
    ]
    if existing.adopted:
        steps += [
            (
                f"closing {path}",
                functools.partial(
                    follower.close_tab,
                    path,
                    dedicated=False,
                    spare_current_tab=spare_current_tab,
                ),
            )
            for path in existing.open_files
        ]
    else:
        steps.append(("quitting nvim", functools.partial(follower.stop, dedicated=True)))
    for what, action in steps:
        remaining = ends_at - time.monotonic()
        if remaining <= 0:
            hooks.logger.warning(
                "stop: gave up on nvim %s before %s: out of time", existing.target, what
            )
            return False
        try:
            with rpc_deadline(remaining):
                action()
        except (OSError, RpcTimeout) as error:
            hooks.logger.warning("stop: gave up on nvim %s at %s: %s", existing.target, what, error)
            return False
        except Exception as error:
            hooks.logger.warning("stop: %s failed: %s", what, error)
            continue
    return not existing.adopted


def _close_dedicated_nvim_pane(origin: str, socket_path: str, wait_for_quit: bool) -> None:
    """Close the dedicated follower nvim's own tmux pane when its `qall!` did
    not land (busy in `:!cmd`, frozen, or never sent). The pane is found by
    its start command, exactly the `nvim --listen <socket>` resolve_nvim_target
    launches, in the origin's window; the origin itself, and any other pane,
    are never touched.

    Closing the pane is not always enough: an nvim busy in `:!cmd` leaves its
    `--embed` server running when its terminal goes (measured: it outlived
    SIGHUP and SIGTERM). So the pane's processes are recorded before the
    kill and any survivor whose command line names the follower's socket is
    SIGKILLed after a short grace."""
    pane = _pane_running(origin, socket_path)
    if pane is not None and wait_for_quit:
        give_up_at = time.monotonic() + _QUIT_GRACE_SECONDS
        while pane is not None and time.monotonic() < give_up_at:
            time.sleep(_POLL_SECONDS)
            pane = _pane_running(origin, socket_path)
    if pane is None:
        return
    pane_id, pane_pid = pane
    processes = [pane_pid, *_child_pids(pane_pid)]
    hooks.logger.warning(
        "stop: closing the follower's own pane %s (nvim %s did not quit)", pane_id, socket_path
    )
    TmuxPane(pane_id=pane_id).kill()
    give_up_at = time.monotonic() + _REAP_GRACE_SECONDS
    survivors = [pid for pid in processes if _process_alive(pid)]
    while survivors and time.monotonic() < give_up_at:
        time.sleep(_POLL_SECONDS)
        survivors = [pid for pid in survivors if _process_alive(pid)]
    for pid in survivors:
        if socket_path in _command_line(pid):
            hooks.logger.warning(
                "stop: killing nvim process %d, left running by its closed pane", pid
            )
            _send_signal(pid, signal.SIGKILL)


def _pane_running(origin: str, socket_path: str) -> tuple[str, int] | None:
    """(pane id, pane pid) of the pane in origin's window whose start command
    is the follower's `nvim --listen <socket_path>`, else None."""
    listing = subprocess.run(
        [
            "tmux",
            "list-panes",
            "-t",
            origin,
            "-F",
            "#{pane_id}\t#{pane_pid}\t#{pane_start_command}",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    if listing.returncode != 0:
        return None
    for line in listing.stdout.splitlines():
        fields = line.split("\t", 2)
        if len(fields) != 3 or fields[0] == origin:
            continue
        pane_id, pane_pid, command = fields
        try:
            words = shlex.split(command)
        except ValueError:
            continue
        if words == ["nvim", "--listen", socket_path] and pane_pid.isdigit():
            return pane_id, int(pane_pid)
    return None


def _child_pids(pid: int) -> list[int]:
    found = subprocess.run(["pgrep", "-P", str(pid)], capture_output=True, text=True, check=False)
    return [int(word) for word in found.stdout.split() if word.isdigit()]


def _command_line(pid: int) -> str:
    found = subprocess.run(
        ["ps", "-o", "command=", "-p", str(pid)], capture_output=True, text=True, check=False
    )
    return found.stdout.strip() if found.returncode == 0 else ""


def _process_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _send_signal(pid: int, signal_number: signal.Signals) -> None:
    with contextlib.suppress(OSError):
        os.kill(pid, signal_number)


def _answering_note(backend: str, target: str) -> str:
    """Marks a follower whose nvim accepted the liveness probe but answered
    nothing (nvim_connect.probe_nvim's "silent"): alive, but busy in a
    `:!cmd`/`system()`/Lua loop, or stopped. FollowerState.get has just
    probed it in this process, so nothing is asked a second time."""
    if backend == "nvim" and nvim_connect.last_probe_verdict(target) == "silent":
        return " — not answering (busy in a command, or stopped)"
    return ""


def _identity_label(session: Session) -> str:
    return f"{session.window_id} ({'tmux' if session.in_tmux else 'not in tmux'})"


def cmd_status(environment: dict[str, str]) -> int:
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
    with contextlib.suppress(OSError):
        nvim_connect.sweep_stale_sockets()
    session = resolve_session(environment)
    if session is None:
        print(_unresolvable_pane(environment))
        return 0
    existing = FollowerState.get(session.window_id)
    if existing is not None:
        print(
            f"claude-follow: active for {_identity_label(session)}, "
            f"backend {existing.backend} ({existing.target}), "
            f"showing {existing.current_file}, "
            f"on_failure={existing.on_failure}, speed={existing.speed}"
            f"{_answering_note(existing.backend, existing.target)}"
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
                f"  {other.window_id}: {other.backend} {other.target}, "
                f"showing {other.current_file}{_answering_note(other.backend, other.target)}"
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


def _show_busy(current: FollowerState | None, in_tmux: bool) -> None:
    """Feedback for a press refused because another animation owns the
    window: its stderr line is discarded by the keybinding's run-shell, so
    it must be SEEN. tmux: a popup. nvim has no pane to target: "Busy" on the
    status float for as long as a popup would stay, then taken down — unless
    something else replaced it meanwhile — and only on a float showing no
    other cue: the hook that owns the window has its own up (final m6). That
    wait holds this process, which the keybinding runs in the background
    (run-shell -b)."""
    if current is None:
        return
    if current.backend == "nvim":
        surface = status_surface_for(current)
        if surface.set_state_if_idle("Busy"):
            time.sleep(tmux.POPUP_SECONDS)
            surface.retire_state("Busy")
        return
    _show_popup(current, "Busy", in_tmux)


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


def cmd_pause(environment: dict[str, str]) -> int:
    session = resolve_session(environment)
    if session is None:
        print(_unresolvable_pane(environment), file=sys.stderr)
        return 1
    current = FollowerState.get(session.window_id)
    # A drain's late "Nothing to …" correction must not cover this
    # press's own popup: tmux drops a popup opened over another (T22).
    control.withdraw_corrections(session.window_id)

    state = control.animating_state(session.window_id)
    presses = 0
    if state in ("running", "paused"):
        presses = control.request_pause(session.window_id)
        if presses == 0:
            # Its owner died after the state was read: nothing was written, so
            # this press is one with no live animation (D1 residual).
            state = None
    if presses % 2 == 0 and state in ("running", "paused"):
        # The presses not read yet pair off (T20 m4): this one undoes the last.
        state = "paused" if state == "running" else "running"
    if state == "running":
        print("claude-follow: pause requested")
        _pause_feedback(current, session.in_tmux, paused=True)
        if control.animating_state(session.window_id) not in ("running", "paused"):
            # The hook's last animation returned meanwhile: its exit drain
            # consumes this P and takes "Paused" back — possibly before this
            # press put it up, leaving it for nobody to take down.
            hooks.retract_signal_feedback(current, session.in_tmux, ["pause"], session.window_id)
        return 0
    if state == "paused":
        # The toggle: the waiting hook consumes the same signal as a resume.
        print("claude-follow: resume requested")
        _pause_feedback(current, session.in_tmux, paused=False)
        return 0
    if state == "handoff":
        print("claude-follow: interrupted — save (:w!) to release Claude, or press S again")
        return 0
    if state == "finishing":
        # The hook's last animation already returned: no signal (nothing would
        # consume it), no popup, and its slot is not ours to resume into.
        print("claude-follow: nothing to pause — the animation is finishing")
        return 0

    if not control.has_pending_animation(session.window_id):
        # Nothing running and nothing recoverable — a pause press must not
        # fake feedback: no signal, no popup. A peek, so an idle press never
        # holds the slot a hook may be about to take.
        print("claude-follow: nothing to pause")
        return 0

    # A crash-orphaned remainder, resumable from here. The replay animates
    # from THIS process, so it owns the window's slot like a hook — taken
    # BEFORE the remainder is loaded: loading consumes the file, and a hook
    # holding the slot consumes the same file as its catch-up.
    if not control.try_acquire_animating(session.window_id):
        print(
            "claude-follow: another animation owns this window — resume it after that one ends",
            file=sys.stderr,
        )
        _show_busy(current, session.in_tmux)
        return 1
    try:
        pending = control.load_pending_animation(session.window_id)
        if pending is None:
            print("claude-follow: nothing to pause")  # a hook's catch-up took it
            return 0

        if current is None:
            _resave_pending(session.window_id, pending)
            print("claude-follow: no follower registered to resume", file=sys.stderr)
            return 1

        if current.backend != "tmux":
            # The keyboard replay below is inherently tmux-only (send-keys). A
            # crash-orphaned nvim pending has no keyboard resume path yet, so
            # discard it rather than crash on the tmux-only resume — mirrors
            # the backend guard cmd_interrupt applies to this same path.
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
        try:
            result = follower.resume(pending)
        except NavigationFailed as failure:
            # A failed first navigation typed nothing: the remainder is still
            # whole, so it stays recoverable. A goto that failed after a pause
            # inside the replay already saved what was left at that point
            # (animate._resume), and that one is kept: the original would
            # replay ops already typed (final M1).
            # Nor when a `claude-follow stop` ran meanwhile: the follower is
            # gone, and so is anything to resume into (review of T6+T7, m3).
            stopped = FollowerState.get(session.window_id) is None
            if not stopped and not control.has_pending_animation(session.window_id):
                _resave_pending(session.window_id, pending)
            print(f"claude-follow: could not resume: {failure}", file=sys.stderr)
            return 1
    finally:
        # A P or S addressed to this replay after its last signal check, as
        # a hook's exit drains them (final m3).
        hooks.drain_unconsumed_signals(session.window_id, current, session.in_tmux)
        control.clear_animating(session.window_id)
    if result.outcome == "stopped":
        # Paused again inside the replay, then `claude-follow stop`: the
        # pause wait saw it (final review C1) and kept no remainder, and the
        # follower's pane is the stop's, so no popup goes to it.
        print("claude-follow: resume ended by `claude-follow stop`")
        return 0
    print(f"claude-follow: resumed ({result.outcome})")
    if result.outcome == "interrupted":
        _show_popup(current, "Interrupted", session.in_tmux)
    return 0


def cmd_interrupt(environment: dict[str, str]) -> int:
    session = resolve_session(environment)
    if session is None:
        print(_unresolvable_pane(environment), file=sys.stderr)
        return 1
    current = FollowerState.get(session.window_id)
    # A drain's late "Nothing to …" correction must not cover this
    # press's own popup: tmux drops a popup opened over another (T22).
    control.withdraw_corrections(session.window_id)

    state = control.animating_state(session.window_id)
    if state in ("running", "paused", "handoff") and not control.request_interrupt(
        session.window_id
    ):
        # Its owner died after the state was read: nothing was written, so
        # this press is one with no live animation (D1 residual).
        state = None
    if state in ("running", "paused"):
        print("claude-follow: interrupt requested")
        _show_popup(current, "Interrupted", session.in_tmux)
        return 0
    if state == "handoff":
        # the des-interrupt: discard the user's unsaved typing and release
        # Claude as if the interrupt had not happened
        print("claude-follow: hand-off cancelled, unsaved changes discarded")
        _show_popup(current, "Discarded", session.in_tmux)
        return 0
    if state == "finishing":
        print("claude-follow: nothing to interrupt — the animation is finishing")
        return 0

    if not control.has_pending_animation(session.window_id):
        print("claude-follow: nothing to interrupt")
        return 0
    # A crash-orphaned remainder (its hook died): nothing to signal — discard
    # it and hand the buffer to the user, like a live interrupt. The hand-over
    # sends keys, so it holds the window's slot like a hook — taken BEFORE the
    # remainder is loaded (a hook holding the slot consumes the same file as
    # its catch-up), "finishing" so a P or S meanwhile has nothing to act on.
    if not control.try_acquire_animating(session.window_id, state="finishing"):
        print(
            "claude-follow: another animation owns this window — press S again to interrupt it",
            file=sys.stderr,
        )
        _show_busy(current, session.in_tmux)
        return 1
    try:
        if control.load_pending_animation(session.window_id) is None:
            print("claude-follow: nothing to interrupt")  # a hook's catch-up took it
            return 0
        if current is not None and current.backend == "tmux":
            TmuxVimFollower(pane_id=current.target, window_id=session.window_id).hand_over()
        FollowerState.update_current_file(session.window_id, None)
    finally:
        control.clear_animating(session.window_id)
    print("claude-follow: paused animation discarded, buffer handed over")
    _show_popup(current, "Interrupted", session.in_tmux)
    return 0


# A `stop` of this window landed while the command ran, and `set` refused its
# write (final review M11): the command says so instead of reporting a change
# that was never saved.
_STOPPED_MEANWHILE = "claude-follow: this window's follower was stopped meanwhile; {what}"


def cmd_speed(environment: dict[str, str], direction: Literal["up", "down"]) -> int:
    session = resolve_session(environment)
    if session is None:
        print(_unresolvable_pane(environment), file=sys.stderr)
        return 1
    current = FollowerState.get(session.window_id)
    if current is None:
        print("claude-follow: no follower active")
        return 0
    new_speed = config.next_speed(current.speed, direction)
    if not FollowerState.update(session.window_id, speed=new_speed):
        print(_STOPPED_MEANWHILE.format(what="speed not changed"), file=sys.stderr)
        return 1
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


def cmd_toggle(environment: dict[str, str]) -> int:
    session = resolve_session(environment)
    if session is None:
        print(_unresolvable_pane(environment), file=sys.stderr)
        return 1
    raw = FollowerState.read(session.window_id)
    if raw is not None and state_module.is_legacy(raw):
        print(f"claude-follow: {state_module.LEGACY_STATE_MESSAGE}")
        return 0
    if raw is not None and state_module.unproven_here(raw):
        # Maybe left by a previous tmux server (a pid that differs, or none
        # recorded): its origin and target may name panes of this one that
        # are not the follower's, which a toggle would zoom or split beside.
        raw = None
    if raw is None:
        if stopped_path(session.window_id).exists():
            # A `stop` silenced auto-open for this window; toggling is the
            # user asking for the follower back without starting it now (a
            # tombstone from another tmux server goes too: it holds nothing).
            clear_stopped(session.window_id)
            print("claude-follow: no follower to toggle; auto-open is back on for this window")
            return 0
        print("claude-follow: no follower to toggle")
        return 0
    if raw.enabled:
        if not FollowerState.update(session.window_id, enabled=False):
            print(_STOPPED_MEANWHILE.format(what="not muted"), file=sys.stderr)
            return 1
        if raw.origin:
            TmuxPane(pane_id=raw.origin).set_zoomed(True)
        print("claude-follow: follower muted")
        return 0
    # Re-enable. open_files is cleared so every next touch resyncs via a
    # fresh retype — the disk moved while we were muted, and animating a
    # diff over a stale buffer would produce garbage. Tabs stay for reading.
    # Turning on lifts a `stop` tombstone, before the write it would refuse.
    clear_stopped(session.window_id)
    if not FollowerState.update(session.window_id, enabled=True, open_files=()):
        print(_STOPPED_MEANWHILE.format(what="not resumed"), file=sys.stderr)
        return 1
    if raw.origin:
        TmuxPane(pane_id=raw.origin).set_zoomed(False)
    if raw.backend == "tmux" and FollowerState.get(session.window_id) is None and raw.origin:
        started = TmuxVimFollower.start(raw.origin)
        if not FollowerState.set(
            session.window_id,
            "tmux",
            started.pane_id,
            origin=raw.origin,
            on_failure=raw.on_failure,
            speed=raw.speed,
        ):  # shown_any defaults False, so the first file reuses (renames) the
            # new pane's start screen instead of opening a redundant tab.
            detail = _close_unrecorded(session.window_id, "tmux", started.pane_id)
            print(_STOPPED_MEANWHILE.format(what=f"not resumed{detail}"), file=sys.stderr)
            return 1
    print("claude-follow: follower resumed")
    return 0
