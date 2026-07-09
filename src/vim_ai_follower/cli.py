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
from pathlib import Path
from typing import Any

from vim_ai_follower import cache, config, control
from vim_ai_follower import diff as diff_module
from vim_ai_follower.backends import Follower, get_follower
from vim_ai_follower.backends.tmux_vim import TmuxVimFollower
from vim_ai_follower.snapshot import load as load_snapshot
from vim_ai_follower.snapshot import save as save_snapshot
from vim_ai_follower.state import FollowerState, nvim_socket_path
from vim_ai_follower.tmux import TmuxSession

LOG_PATH = cache.CACHE_DIR / "hook.log"

logger = logging.getLogger("vim_ai_follower")

_EDIT_TOOLS = {"Edit", "MultiEdit", "Write"}


_KEYBINDINGS: tuple[tuple[str, str], ...] = (("P", "pause"), ("S", "interrupt"))


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
    result = subprocess.run(
        ["tmux", "list-keys", "-T", "prefix", key],
        capture_output=True,
        text=True,
        check=False,
    )
    line = result.stdout.strip()
    return line if result.returncode == 0 and line else None


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

    default_on_failure, default_speed = config.load_defaults()
    resolved_on_failure = on_failure if on_failure is not None else default_on_failure
    resolved_speed = speed if speed is not None else default_speed
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
        control.save_pending_apply_edit(session_id, pending.ops, pending.pace_seconds)
    else:
        control.save_pending_show_fresh(
            session_id,
            pending.lines,
            pending.pace_seconds,
            continuation=pending.continuation,
        )


def cmd_pause(env: dict[str, str]) -> int:
    session = TmuxSession.from_env(env)
    if session is None:
        print("claude-follow: not running inside tmux", file=sys.stderr)
        return 1
    current = FollowerState.get(session.session_id)

    pending = control.load_pending_animation(session.session_id)
    if pending is None:
        if not control.is_animating(session.session_id):
            # After an interrupt — or with nothing running at all — a pause
            # press must not fake feedback: no signal, no popup.
            print("claude-follow: nothing to pause")
            return 0
        control.request_pause(session.session_id)
        print("claude-follow: pause requested")
        _show_popup(current, "Paused")
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
    if result.outcome == "paused":
        _show_popup(current, "Paused")
    elif result.outcome == "interrupted":
        _show_popup(current, "Interrupted")
    return 0


def cmd_interrupt(env: dict[str, str]) -> int:
    session = TmuxSession.from_env(env)
    if session is None:
        print("claude-follow: not running inside tmux", file=sys.stderr)
        return 1
    current = FollowerState.get(session.session_id)

    pending = control.load_pending_animation(session.session_id)
    if pending is not None:
        # A pending file only exists while no animation is running, so there
        # is nothing to signal — discard the paused remainder and hand the
        # buffer to the user, exactly like a live interrupt would.
        if current is not None and current.backend == "tmux":
            TmuxVimFollower(pane_id=current.target, session_id=session.session_id).hand_over()
        FollowerState.update_current_file(session.session_id, None)
        print("claude-follow: paused animation discarded, buffer handed over")
        _show_popup(current, "Interrupted")
        return 0

    if not control.is_animating(session.session_id):
        print("claude-follow: nothing to interrupt")
        return 0

    control.request_interrupt(session.session_id)
    print("claude-follow: interrupt requested")
    _show_popup(current, "Interrupted")
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


def _ensure_buffer(
    session_id: str, follower: Follower, current: FollowerState, file_path: str
) -> bool:
    """Switches the follower to file_path via `:e` if needed, returning True
    when a switch happened. Used for Read navigation and binary files, where
    showing the real on-disk content immediately is exactly what's wanted —
    unlike a fresh text edit, which goes through show_fresh instead so the
    finished content is never flashed before it's typed."""
    if current.current_file == file_path:
        return False
    follower.ensure_showing(file_path)
    FollowerState.update_current_file(session_id, file_path)
    return True


def _reconstruct_partial_fresh(content: str, completed_count: int) -> str:
    return "\n".join(content.splitlines()[:completed_count])


def _print_interrupt_notification(file_path: str, partial_content: str) -> None:
    context = (
        f"The user interrupted the live preview of {file_path} while it was "
        "being written and is now editing it directly. Only this much had "
        f"been shown before they took over:\n\n{partial_content}\n\n"
        "This reflects neither their edits since nor necessarily the file's "
        "current state — re-read it from disk before assuming anything "
        "about its contents, and reconcile your next steps with whatever "
        "you find there."
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


def cmd_hook_pre(env: dict[str, str], payload: dict[str, Any]) -> int:
    _configure_logging()
    if payload.get("tool_name") not in _EDIT_TOOLS:
        return 0
    session = TmuxSession.from_env(env)
    if session is None:
        return 0
    file_path = _file_path(payload)
    if file_path is None:
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
    current = _get_active_follower(session.session_id)
    if current is None:
        return 0
    file_path = _file_path(payload)
    if file_path is None:
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
    is_fresh = current.current_file != file_path
    binary = diff_module.is_binary(raw_after)

    # Consume any paused animation BEFORE animating: replaying it at pace 0
    # brings the buffer to the state the new edit's ops were computed
    # against. When the new edit targets another file (or a binary), the
    # buffer gets wiped/replaced anyway — consuming without replaying is
    # the correct discard.
    pending = control.load_pending_animation(session.session_id)
    if pending is not None and not is_fresh and not binary:
        assert isinstance(follower, TmuxVimFollower)  # only tmux ever persists pending state
        follower.resume(dataclasses.replace(pending, pace_seconds=0.0))

    if binary:
        # Binary files are never animated, so it's safe to just navigate to
        # them normally (real content shown immediately, nothing to spoil).
        if is_fresh:
            _ensure_buffer(session.session_id, follower, current, file_path)
        return 0

    after = raw_after.decode("utf-8", errors="replace")
    if is_fresh:
        result = follower.show_fresh(file_path, after)
        if result.outcome == "interrupted":
            # Forget the file so the next edit resyncs via a full retype —
            # the user owns the buffer now and may change it under us.
            FollowerState.update_current_file(session.session_id, None)
            partial = _reconstruct_partial_fresh(after, result.completed_count)
            _print_interrupt_notification(file_path, partial)
        else:
            FollowerState.update_current_file(session.session_id, file_path)
        return 0

    before = load_snapshot(session.session_id, file_path)
    ops = diff_module.compute_edit_script(before, after)
    result = follower.apply_edit(ops)
    if result.outcome == "interrupted":
        FollowerState.update_current_file(session.session_id, None)
        # completed_count indexes the very ops list the animation walked —
        # computing the script once keeps this reconstruction truthful.
        partial = diff_module.apply_ops(before, ops[: result.completed_count])
        _print_interrupt_notification(file_path, partial)
    return 0


def _handle_hook_post_read(env: dict[str, str], payload: dict[str, Any]) -> int:
    session = TmuxSession.from_env(env)
    if session is None:
        return 0
    current = _get_active_follower(session.session_id)
    if current is None:
        return 0
    file_path = _file_path(payload)
    if file_path is None:
        return 0
    follower = get_follower(current.backend, current.target)
    _ensure_buffer(session.session_id, follower, current, file_path)
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

    payload: dict[str, Any] = json.loads(sys.stdin.read())
    if args.hook_command == "pre":
        return cmd_hook_pre(env, payload)
    return cmd_hook_post(env, payload)


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
