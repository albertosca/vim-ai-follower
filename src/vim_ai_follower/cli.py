from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from pathlib import Path
from typing import Any

from vim_ai_follower import diff as diff_module
from vim_ai_follower.animate import apply, pace_for, render_keystrokes
from vim_ai_follower.snapshot import load as load_snapshot
from vim_ai_follower.snapshot import save as save_snapshot
from vim_ai_follower.state import FollowerState
from vim_ai_follower.tmux import TmuxPane, TmuxSession

LOG_PATH = Path.home() / ".cache" / "claude-vim-follower" / "hook.log"

logger = logging.getLogger("vim_ai_follower")

_EDIT_TOOLS = {"Edit", "MultiEdit", "Write"}


def cmd_start(env: dict[str, str]) -> int:
    session = TmuxSession.from_env(env)
    if session is None:
        print("claude-follow: not running inside tmux", file=sys.stderr)
        return 1
    if FollowerState.get(session.session_id) is not None:
        print("claude-follow: follower already running for this session")
        return 0
    pane = TmuxPane.split_from(env["TMUX_PANE"], "vim")
    FollowerState.set(session.session_id, pane)
    print(f"claude-follow: started follower in pane {pane.pane_id}")
    return 0


def cmd_stop(env: dict[str, str]) -> int:
    session = TmuxSession.from_env(env)
    if session is None:
        print("claude-follow: not running inside tmux", file=sys.stderr)
        return 1
    existing = FollowerState.get(session.session_id)
    if existing is not None:
        existing.pane.kill()
    FollowerState.clear(session.session_id)
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
            f"claude-follow: active, pane {existing.pane.pane_id}, showing {existing.current_file}"
        )
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


def _ensure_buffer(session_id: str, current: FollowerState, file_path: str) -> None:
    if current.current_file == file_path:
        return
    current.pane.send_text(f":e {file_path}")
    current.pane.send_key("Enter")
    FollowerState.update_current_file(session_id, file_path)


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
    current = FollowerState.get(session.session_id)
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

    _ensure_buffer(session.session_id, current, file_path)

    if diff_module.is_binary(raw_after):
        return 0

    after = raw_after.decode("utf-8", errors="replace")
    before = load_snapshot(session.session_id, file_path)
    ops = diff_module.compute_edit_script(before, after)
    apply(current.pane, render_keystrokes(ops), pace_for(ops))
    return 0


def _handle_hook_post_read(env: dict[str, str], payload: dict[str, Any]) -> int:
    session = TmuxSession.from_env(env)
    if session is None:
        return 0
    current = FollowerState.get(session.session_id)
    if current is None:
        return 0
    file_path = _file_path(payload)
    if file_path is None:
        return 0
    _ensure_buffer(session.session_id, current, file_path)
    offset = _tool_input(payload).get("offset")
    if isinstance(offset, int) and offset > 0:
        current.pane.send_text(f":{offset}")
        current.pane.send_key("Enter")
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
    subparsers.add_parser("start")
    subparsers.add_parser("stop")
    subparsers.add_parser("status")
    hook_parser = subparsers.add_parser("hook")
    hook_subparsers = hook_parser.add_subparsers(dest="hook_command", required=True)
    hook_subparsers.add_parser("pre")
    hook_subparsers.add_parser("post")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    env = dict(os.environ)

    if args.command == "start":
        return cmd_start(env)
    if args.command == "stop":
        return cmd_stop(env)
    if args.command == "status":
        return cmd_status(env)

    payload: dict[str, Any] = json.loads(sys.stdin.read())
    if args.hook_command == "pre":
        return cmd_hook_pre(env, payload)
    return cmd_hook_post(env, payload)


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
