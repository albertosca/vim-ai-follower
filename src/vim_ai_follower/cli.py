"""Argument parsing and dispatch entry point for the claude-follow CLI."""

from __future__ import annotations

import argparse
import json
import os
import sys
from typing import Any

from vim_ai_follower import commands, config, hooks


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="claude-follow")
    subparsers = parser.add_subparsers(dest="command", required=True)
    start_parser = subparsers.add_parser("start")
    # Default None (not "tmux") so a bare `start` falls back to config.backend;
    # an explicit --backend still overrides it.
    start_parser.add_argument("--backend", choices=["tmux", "nvim"], default=None)
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
        return commands.cmd_start(
            env, backend=args.backend, on_failure=args.on_failure, speed=args.speed
        )
    if args.command == "stop":
        return commands.cmd_stop(env)
    if args.command == "status":
        return commands.cmd_status(env)
    if args.command == "pause":
        return commands.cmd_pause(env)
    if args.command == "interrupt":
        return commands.cmd_interrupt(env)
    if args.command == "speed-up":
        return commands.cmd_speed(env, "up")
    if args.command == "speed-down":
        return commands.cmd_speed(env, "down")
    if args.command == "toggle":
        return commands.cmd_toggle(env)

    payload: dict[str, Any] = json.loads(sys.stdin.read())
    if args.hook_command == "pre":
        return hooks.cmd_hook_pre(env, payload)
    return hooks.cmd_hook_post(env, payload)


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
