"""Argument parsing and dispatch entry point for the claude-follow CLI."""

from __future__ import annotations

import argparse
import os
import sys

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
    start_parser.add_argument("--take-keys", action="store_true")
    subparsers.add_parser("stop")
    subparsers.add_parser("status")
    hook_parser = subparsers.add_parser("hook")
    hook_subparsers = hook_parser.add_subparsers(dest="hook_command", required=True)
    hook_subparsers.add_parser("pre")
    hook_subparsers.add_parser("post")
    hook_subparsers.add_parser("failure")
    subparsers.add_parser("pause")
    subparsers.add_parser("interrupt")
    subparsers.add_parser("speed-up")
    subparsers.add_parser("speed-down")
    subparsers.add_parser("toggle")
    return parser


def main(argv: list[str] | None = None) -> int:
    arguments = _build_parser().parse_args(argv)
    environment = dict(os.environ)

    if arguments.command == "start":
        return commands.cmd_start(
            environment,
            backend=arguments.backend,
            on_failure=arguments.on_failure,
            speed=arguments.speed,
            take_keys=arguments.take_keys,
        )
    if arguments.command == "stop":
        return commands.cmd_stop(environment)
    if arguments.command == "status":
        return commands.cmd_status(environment)
    if arguments.command == "pause":
        return commands.cmd_pause(environment)
    if arguments.command == "interrupt":
        return commands.cmd_interrupt(environment)
    if arguments.command == "speed-up":
        return commands.cmd_speed(environment, "up")
    if arguments.command == "speed-down":
        return commands.cmd_speed(environment, "down")
    if arguments.command == "toggle":
        return commands.cmd_toggle(environment)

    # sys.stdin is passed as-is, unread: run_hook reads it inside its own
    # try, so a raising .read() (non-UTF-8 bytes, a broken pipe) is caught
    # there too, not left to escape from this call site.
    return hooks.run_hook(arguments.hook_command, environment, sys.stdin)


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
