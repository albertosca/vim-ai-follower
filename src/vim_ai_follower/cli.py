from __future__ import annotations

import argparse
import dataclasses
import json
import logging
import os
import shlex
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Literal

from vim_ai_follower import cache, config, control
from vim_ai_follower import diff as diff_module
from vim_ai_follower.backends import Follower, get_follower
from vim_ai_follower.backends.tmux_vim import TmuxVimFollower
from vim_ai_follower.snapshot import load as load_snapshot
from vim_ai_follower.snapshot import save as save_snapshot
from vim_ai_follower.state import FollowerState, nvim_socket_path, touch_open_files
from vim_ai_follower.tmux import TmuxPane, TmuxSession

LOG_PATH = cache.CACHE_DIR / "hook.log"

logger = logging.getLogger("vim_ai_follower")

_EDIT_TOOLS = {"Edit", "MultiEdit", "Write"}


_KEYBINDINGS: tuple[tuple[str, str], ...] = (
    ("P", "pause"),
    ("S", "interrupt"),
    ("+", "speed-up"),
    ("_", "speed-down"),
    ("F", "toggle"),
)


def _claude_follow_executable() -> str:
    """Absolute path to this venv's claude-follow entry point. tmux
    run-shell commands execute with the tmux SERVER's environment, whose
    PATH never includes this project's virtualenv — a bare name exits 127
    there, so the keybinding must embed the resolved path."""
    candidate = Path(sys.executable).parent / "claude-follow"
    if candidate.exists():
        return str(candidate)
    located = shutil.which("claude-follow")
    return located if located is not None else "claude-follow"


def _saved_bindings_path() -> Path:
    return cache.CACHE_DIR / "saved-keybindings.json"


def _existing_binding(key: str) -> str | None:
    # List the whole prefix table and filter ourselves: tmux 3.7b returns
    # empty output for `list-keys -T prefix <key>` even when the binding
    # exists, so per-key filtering can't be trusted across versions.
    result = subprocess.run(
        ["tmux", "list-keys", "-T", "prefix"],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        return None
    for line in result.stdout.splitlines():
        try:
            tokens = shlex.split(line)
        except ValueError:
            continue
        if "-T" in tokens:
            table_index = tokens.index("-T")
            if tokens[table_index + 1 : table_index + 3] == ["prefix", key]:
                return line.strip()
    return None


def _register_keybindings() -> None:
    saved_path = _saved_bindings_path()
    if not saved_path.exists():
        # Only the FIRST registration records "previous": re-registering
        # after a crash would otherwise save our own still-bound key as the
        # user's original binding.
        saved_path.parent.mkdir(parents=True, exist_ok=True)
        saved_path.write_text(json.dumps({key: _existing_binding(key) for key, _ in _KEYBINDINGS}))
    executable = shlex.quote(_claude_follow_executable())
    for key, subcommand in _KEYBINDINGS:
        subprocess.run(
            [
                "tmux",
                "bind-key",
                "-T",
                "prefix",
                key,
                "run-shell",
                # -b: run in background — a plain run-shell blocks ALL tmux
                # input until the command exits, and a resume replay lasts
                # tens of seconds. Backgrounding also makes the toggle
                # honest: a second press DURING the replay delivers a pause
                # signal the replay actually consumes.
                "-b",
                # tmux pre-expands #{pane_id} against the pane that triggered
                # the binding, so assign it directly. Nesting a
                # $(tmux display-message -p ...) around the pre-expanded
                # "%N" would format-expand it AGAIN, eating the "%" and
                # producing an invalid pane target.
                # >/dev/null: any stdout from run-shell throws the active
                # pane into tmux's view-mode overlay until dismissed.
                f"TMUX_PANE=#{{pane_id}} {executable} {subcommand} >/dev/null 2>&1",
            ],
            check=True,
        )


def _unregister_keybindings() -> None:
    # check=False everywhere: stop after a crash that skipped registration
    # must still clean up without erroring. Keybindings are SERVER-global
    # while follower state is per-session: running two followers in two
    # tmux sessions at once is unsupported (the first stop takes the keys
    # down for both).
    saved_path = _saved_bindings_path()
    try:
        saved: dict[str, str | None] = json.loads(saved_path.read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        saved = {}
    for key, _ in _KEYBINDINGS:
        previous = saved.get(key)
        if previous and "claude-follow" not in previous:
            subprocess.run(["tmux", *shlex.split(previous)], check=False)
        else:
            subprocess.run(["tmux", "unbind-key", "-T", "prefix", key], check=False)
    saved_path.unlink(missing_ok=True)


def _adopt_target(origin: str) -> str | None:
    for pane_id, command in TmuxPane(pane_id=origin).window_panes():
        if pane_id != origin and command == "vim":
            return pane_id
    return None


def _passes_policy(cfg: config.Config, file_path: str) -> bool:
    return cfg.open_policy != "code" or config.is_code_file(file_path)


def cmd_start(
    env: dict[str, str],
    backend: str = "tmux",
    on_failure: str | None = None,
    speed: str | None = None,
) -> int:
    session = TmuxSession.from_env(env)
    if session is None:
        print("claude-follow: not running inside tmux", file=sys.stderr)
        return 1
    if FollowerState.get(session.session_id) is not None:
        print("claude-follow: follower already running for this session")
        return 0

    defaults = config.load()
    resolved_on_failure = on_failure if on_failure is not None else defaults.on_failure
    resolved_speed = speed if speed is not None else defaults.speed
    origin = env["TMUX_PANE"]
    _register_keybindings()

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
        adopt = _adopt_target(origin)
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
    session = TmuxSession.from_env(env)
    if session is None:
        print("claude-follow: not running inside tmux", file=sys.stderr)
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
    _unregister_keybindings()
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
    """Brief, self-dismissing tmux popup on the follower pane confirming a
    pause/resume/interrupt — feedback for the keybinding press itself, not a
    guarantee that an animation was actually running to be affected. Only
    the tmux backend has a pane to target. Fire-and-forget via Popen:
    display-popup -E only exits when the popup closes, and the caller must
    not stall 1.5s (nor delay a resume replay) waiting for it."""
    if current is None or current.backend != "tmux":
        return
    subprocess.Popen(
        [
            "tmux",
            "display-popup",
            "-t",
            current.target,
            "-E",
            "-w",
            "30",
            "-h",
            "3",
            f"echo {shlex.quote(message)}; sleep 1.5",
        ]
    )


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
    session = TmuxSession.from_env(env)
    if session is None:
        print("claude-follow: not running inside tmux", file=sys.stderr)
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
    session = TmuxSession.from_env(env)
    if session is None:
        print("claude-follow: not running inside tmux", file=sys.stderr)
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
    session = TmuxSession.from_env(env)
    if session is None:
        print("claude-follow: not running inside tmux", file=sys.stderr)
        return 1
    current = FollowerState.get(session.session_id)
    if current is None:
        print("claude-follow: no follower active")
        return 0
    new_speed = config.next_speed(current.speed, direction)
    FollowerState.update(session.session_id, speed=new_speed)
    print(f"claude-follow: speed {new_speed}")
    _show_popup(current, f"Speed: {new_speed}")
    return 0


def cmd_toggle(env: dict[str, str]) -> int:
    session = TmuxSession.from_env(env)
    if session is None:
        print("claude-follow: not running inside tmux", file=sys.stderr)
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
        )  # fresh pane: shown_any=False → first file renames the start screen
    print("claude-follow: follower resumed")
    return 0


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


def _maybe_auto_open(session_id: str, env: dict[str, str], file_path: str) -> FollowerState | None:
    cfg = config.load()
    if cfg.open_policy == "manual" or not _passes_policy(cfg, file_path):
        return None
    origin = env.get("TMUX_PANE", "")
    if not origin:
        return None
    _register_keybindings()
    adopt = _adopt_target(origin) if cfg.adopt_existing else None
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
        session.session_id, env, file_path
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
        new_open, evicted = touch_open_files(current.open_files, file_path, cfg.max_tabs)
        for old in evicted:
            assert isinstance(follower, TmuxVimFollower)  # only tmux ever tracks tabs
            follower.close_tab(old)
        FollowerState.update(session.session_id, open_files=new_open, shown_any=True)
        return 0

    after = raw_after.decode("utf-8", errors="replace")

    # Evict/persist BEFORE animating so the tab shuffle never lands
    # mid-typing — touch_open_files never puts the just-touched file_path in
    # the evicted slice, so the file about to be animated can never be the
    # one just closed.
    new_open, evicted = touch_open_files(current.open_files, file_path, cfg.max_tabs)
    for old in evicted:
        assert isinstance(follower, TmuxVimFollower)  # only tmux ever tracks tabs
        follower.close_tab(old)
    FollowerState.update(session.session_id, open_files=new_open, shown_any=True)

    if is_fresh:
        result = follower.show_fresh(file_path, after, in_new_tab=current.shown_any)
        if result.outcome == "interrupted":
            # Forget the file so the next edit resyncs via a full retype —
            # the user owns the buffer now and may change it under us.
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
        session.session_id, env, file_path
    )
    if current is None:
        return 0
    follower = get_follower(current.backend, current.target)
    _ensure_buffer(session.session_id, follower, file_path)
    new_open, evicted = touch_open_files(current.open_files, file_path, cfg.max_tabs)
    for old in evicted:
        assert isinstance(follower, TmuxVimFollower)  # only tmux ever tracks tabs
        follower.close_tab(old)
    FollowerState.update(session.session_id, open_files=new_open, shown_any=True)
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


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="claude-follow")
    subparsers = parser.add_subparsers(dest="command", required=True)
    start_parser = subparsers.add_parser("start")
    start_parser.add_argument("--backend", choices=["tmux", "nvim_rpc"], default="tmux")
    start_parser.add_argument("--on-failure", choices=["silent", "reopen"], default=None)
    start_parser.add_argument(
        "--speed",
        choices=list(config.SPEED_PACE_SECONDS),
        default=None,
    )
    subparsers.add_parser("stop")
    subparsers.add_parser("status")
    hook_parser = subparsers.add_parser("hook")
    hook_subparsers = hook_parser.add_subparsers(dest="hook_command", required=True)
    hook_subparsers.add_parser("pre")
    hook_subparsers.add_parser("post")
    subparsers.add_parser("pause")
    subparsers.add_parser("interrupt")
    subparsers.add_parser("speed-up")
    subparsers.add_parser("speed-down")
    subparsers.add_parser("toggle")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    env = dict(os.environ)

    if args.command == "start":
        return cmd_start(env, backend=args.backend, on_failure=args.on_failure, speed=args.speed)
    if args.command == "stop":
        return cmd_stop(env)
    if args.command == "status":
        return cmd_status(env)
    if args.command == "pause":
        return cmd_pause(env)
    if args.command == "interrupt":
        return cmd_interrupt(env)
    if args.command == "speed-up":
        return cmd_speed(env, "up")
    if args.command == "speed-down":
        return cmd_speed(env, "down")
    if args.command == "toggle":
        return cmd_toggle(env)

    payload: dict[str, Any] = json.loads(sys.stdin.read())
    if args.hook_command == "pre":
        return cmd_hook_pre(env, payload)
    return cmd_hook_post(env, payload)


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
