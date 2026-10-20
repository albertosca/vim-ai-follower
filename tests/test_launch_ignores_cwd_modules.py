"""Every way the follower starts Python runs with the current directory OFF
sys.path (final review I3). `python3 -m` puts the cwd first, so a `json.py`,
`select.py`, `secrets.py`, `termios.py` or `shlex.py` in Claude's project
(the hooks' cwd) or in the pane's directory (the popup's) was imported
instead of the standard library: every hook crashed, and the user's module
ran inside the take-over question. Each test runs the REAL command from a
directory of such modules, with an interpreter that cannot import the
package from its own site-packages, so PYTHONPATH must still reach it."""

from __future__ import annotations

import json
import os
import shlex
import subprocess
import sys
import tty
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from vim_ai_follower import cache, take_over

REPO = Path(__file__).resolve().parent.parent
SHADOWED = ("json", "select", "secrets", "termios", "shlex", "tty")


def _base_python() -> Path:
    """An interpreter without this package installed (the venv has it as an
    editable install, which would hide a lost PYTHONPATH)."""
    for name in ("python3", "python"):
        candidate = Path(sys.base_prefix) / "bin" / name
        if candidate.exists():
            return candidate
    pytest.skip("no base interpreter next to this venv")


@pytest.fixture
def shadowing_cwd(tmp_path: Path) -> Path:
    """A directory whose stdlib-named modules record that they ran, then exit."""
    directory = tmp_path / "project"
    directory.mkdir()
    for name in SHADOWED:
        (directory / f"{name}.py").write_text(
            f"open({str(directory / (name + '.ran'))!r}, 'w').close()\n"
            f"raise SystemExit('SHADOWED {name}')\n"
        )
    return directory


@pytest.fixture
def launch_environment(tmp_path: Path) -> dict[str, str]:
    """A throwaway HOME (the hook log), no tmux at all, and `python3` on PATH
    resolving to the base interpreter."""
    bin_directory = tmp_path / "bin"
    bin_directory.mkdir()
    (bin_directory / "python3").symlink_to(_base_python())
    home = tmp_path / "home"
    home.mkdir()
    tmux_directory = tmp_path / "tmux"
    tmux_directory.mkdir()
    environment = {
        key: value
        for key, value in os.environ.items()
        if key not in {"TMUX", "TMUX_PANE", "PYTHONPATH", "PYTHONSAFEPATH"}
    }
    environment.update(
        PATH=f"{bin_directory}{os.pathsep}{os.environ['PATH']}",
        HOME=str(home),
        TMUX_TMPDIR=str(tmux_directory),
        CLAUDE_PLUGIN_ROOT=str(REPO),
    )
    return environment


def _hook_commands() -> list[str]:
    hooks = json.loads((REPO / "hooks" / "hooks.json").read_text())["hooks"]
    return [
        hook["command"]
        for matchers in hooks.values()
        for matcher in matchers
        for hook in matcher["hooks"]
    ]


def _assert_no_module_ran(directory: Path, result: subprocess.CompletedProcess[str]) -> None:
    assert "SHADOWED" not in result.stderr, result.stderr
    assert sorted(path.name for path in directory.glob("*.ran")) == []


@pytest.mark.parametrize("command", _hook_commands())
def test_every_hook_command_ignores_modules_in_its_cwd(
    command: str, shadowing_cwd: Path, launch_environment: dict[str, str]
) -> None:
    result = subprocess.run(
        ["sh", "-c", command],
        cwd=shadowing_cwd,
        env=launch_environment,
        input=json.dumps({"tool_name": "Bash", "tool_input": {"command": "ls"}}),
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    _assert_no_module_ran(shadowing_cwd, result)
    assert result.returncode == 0, result.stderr


def test_the_claude_follow_wrapper_ignores_modules_in_its_cwd(
    shadowing_cwd: Path, launch_environment: dict[str, str]
) -> None:
    result = subprocess.run(
        [str(REPO / "bin" / "claude-follow"), "--help"],
        cwd=shadowing_cwd,
        env=launch_environment,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    _assert_no_module_ran(shadowing_cwd, result)
    assert result.returncode == 0, result.stderr
    assert "usage" in result.stdout


def test_the_take_over_popup_ignores_modules_in_its_cwd_and_starts_in_the_cache_dir(
    shadowing_cwd: Path, launch_environment: dict[str, str]
) -> None:
    """The popup's command is run for real, on a pty, from the shadowing
    directory (where display-popup would start it without `-d`: the pane's
    current path), with the base interpreter standing in for sys.executable."""
    user_side, prompt_side = os.openpty()
    tty.setraw(prompt_side)
    ran: list[subprocess.CompletedProcess[str]] = []
    real_run = subprocess.run  # take_over's `subprocess` is this module object

    def tmux(argv: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        if argv[1] == "list-clients":
            return subprocess.CompletedProcess(argv, 0, "1 /dev/ttys001\n", "")
        ran.append(
            real_run(
                ["sh", "-c", argv[-1]],
                cwd=shadowing_cwd,
                env=launch_environment,
                stdin=prompt_side,
                stdout=prompt_side,
                stderr=subprocess.PIPE,
                text=True,
                timeout=30,
                check=False,
            )
        )
        return subprocess.CompletedProcess(argv, 0, "", "")

    try:
        with (
            patch("vim_ai_follower.take_over.subprocess.run", MagicMock(side_effect=tmux)) as run,
            patch("vim_ai_follower.take_over.sys.executable", str(_base_python())),
        ):
            answer = take_over.ask("%3", "Vim", 0.3)
    finally:
        os.close(user_side)
        os.close(prompt_side)
    _assert_no_module_ran(shadowing_cwd, ran[0])
    assert answer == "answered timeout", ran[0].stderr
    popup = run.call_args_list[1].args[0]
    directory_flag = popup.index("-d")
    assert popup[directory_flag + 1] == str(cache.CACHE_DIR)
    assert shlex.split(popup[-1])[2:4] == [str(_base_python()), "-P"]
