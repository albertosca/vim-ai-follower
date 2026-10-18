"""No test Vim, nvim or shell writes into the developer's HOME.

`tmux_session` runs its server under the developer's real HOME, so every
Vim it ran wrote their `~/.viminfo` on exit and every pane's login shell
appended to their `~/.zsh_history` (T24b re-review I-A, measured with a
throwaway HOME: both files appeared after a single bom test). The fixture's
VIMINIT now carries `viminfofile=NONE` and its shells a private ZDOTDIR
whose `.zshrc` unsets HISTFILE (macOS's `/etc/zshrc` sets it, so the
environment's HISTFILE alone loses).

The pin points the server's panes at a throwaway HOME, starts the follower's
own Vim (production `TmuxPane.split_from`) and a default shell with a
command in its history, and kills the server — the way every test ends.
Beside them, in another throwaway HOME, a Vim and a zsh started WITHOUT the
fixture's levers are the positive control: they must write their files, or
the absence below proves nothing (the kill or the wait did not reach them).
"""

from __future__ import annotations

import os
import re
import subprocess
import uuid
from collections.abc import Callable
from pathlib import Path

import pytest
from test_integration_handoff_write import _prove_private

from vim_ai_follower.tmux import TmuxPane

pytestmark = pytest.mark.integration


def _tmux(*arguments: str) -> str:
    return subprocess.run(
        ["tmux", *arguments], capture_output=True, text=True, check=True
    ).stdout.strip()


def _split(target: str, *command: str) -> str:
    return _tmux("split-window", "-t", target, "-P", "-F", "#{pane_id}", *command)


def _runs(pane: str, command: str) -> Callable[[], bool]:
    return lambda: _tmux("display-message", "-p", "-t", pane, "#{pane_current_command}") == command


@pytest.mark.skipif(
    not os.environ.get("SHELL", "").endswith("zsh"), reason="the shell lever measured is zsh's"
)
def test_no_test_vim_or_shell_writes_viminfo_or_history(
    tmux_session: str, tmp_path: Path, wait_until: Callable[..., bool]
) -> None:
    _prove_private()
    subject = tmp_path / "subject-home"
    control = tmp_path / "control-home"
    subject.mkdir()
    control.mkdir()
    _tmux("set-environment", "-g", "HOME", str(subject))
    origin = _tmux("list-panes", "-t", tmux_session, "-F", "#{pane_id}").splitlines()[0]
    _tmux("resize-window", "-t", tmux_session, "-x", "200", "-y", "60")

    follower_vim = TmuxPane.split_from(origin, "vim").pane_id
    shell = _split(origin)
    control_vim = _split(origin, f"env HOME={control} vim -u NONE -N")
    control_shell = _split(origin, f"env -u ZDOTDIR -u HISTFILE HOME={control} zsh -l")
    for pane in (follower_vim, control_vim):
        assert wait_until(_runs(pane, "vim"), timeout=10.0), f"vim never started in {pane}"
    token = f"hermetic-{uuid.uuid4().hex[:8]}"
    for pane in (shell, control_shell):
        assert wait_until(_runs(pane, "zsh"), timeout=10.0), f"zsh never started in {pane}"
        _tmux("send-keys", "-t", pane, "-l", "--", f"echo {token}")
        _tmux("send-keys", "-t", pane, "Enter")
        assert wait_until(
            lambda pane=pane: _tmux("capture-pane", "-p", "-t", pane).count(token) >= 2,
            timeout=10.0,
        ), f"the shell in {pane} never ran the command"

    subprocess.run(["tmux", "kill-server"], check=False)

    # The control proves the kill reached the writers and that the wait was
    # long enough for them to write.
    assert wait_until(
        lambda: {".viminfo", ".zsh_history"} <= {path.name for path in control.iterdir()},
        timeout=10.0,
    ), f"the control wrote only {sorted(path.name for path in control.iterdir())}"
    assert sorted(path.name for path in subject.iterdir()) == []


def test_no_test_nvim_writes_its_state_into_home(
    tmux_session: str, tmp_path: Path, wait_until: Callable[..., bool]
) -> None:
    # Even `nvim -n -i NONE` writes ~/.local/state/nvim/nvim.log (an nvim
    # without -n its swap files there): conftest's autouse
    # nvim_state_outside_home points XDG_STATE_HOME elsewhere. The subjects
    # are a pane nvim (tmux_session's server inherits the variable) and one
    # this process starts itself, as headless_nvim does; the control, with
    # the variable removed, must write, or the empty subject proves nothing.
    _prove_private()
    subject = tmp_path / "subject-home"
    control = tmp_path / "control-home"
    subject.mkdir()
    control.mkdir()
    edited = tmp_path / "edited.txt"
    edited.write_text("x\n")
    _tmux("set-environment", "-g", "HOME", str(subject))
    origin = _tmux("list-panes", "-t", tmux_session, "-F", "#{pane_id}").splitlines()[0]
    _tmux("resize-window", "-t", tmux_session, "-x", "200", "-y", "60")

    pane_nvim = TmuxPane.split_from(origin, f"nvim -u NONE -i NONE {edited}").pane_id
    control_nvim = _split(origin, f"env -u XDG_STATE_HOME HOME={control} nvim -u NONE -i NONE -n")
    in_process = subprocess.Popen(
        ["nvim", "--headless", "-n", "-u", "NONE", "-i", "NONE"],
        env={**os.environ, "HOME": str(subject)},
    )
    try:
        for pane in (pane_nvim, control_nvim):
            assert wait_until(_runs(pane, "nvim"), timeout=10.0), f"nvim never started in {pane}"
        assert wait_until(
            lambda: (control / ".local" / "state" / "nvim" / "nvim.log").exists(), timeout=10.0
        ), f"the control wrote only {sorted(str(path) for path in control.rglob('*'))}"
    finally:
        in_process.terminate()
        in_process.wait(timeout=5)
        subprocess.run(["tmux", "kill-server"], check=False)
    assert sorted(str(path.relative_to(subject)) for path in subject.rglob("*")) == []


_ROOT = Path(__file__).resolve().parents[1]


def _vims_without_viminfo_off(text: str, pattern: str) -> list[str]:
    return [
        match.group(0) for match in re.finditer(pattern, text) if "-i NONE" not in match.group(0)
    ]


def test_every_vim_that_skips_the_hermetic_viminit_turns_viminfo_off() -> None:
    # `-u NONE` skips VIMINIT, so the fixture's viminfofile=NONE does not
    # reach that Vim (test_integration_typed_semicolon.py wrote ~/.viminfo
    # that way). The scripts on a private TMUX_TMPDIR start their Vims under
    # the developer's HOME with a VIMINIT of their own, so each says `-i NONE`.
    offenders = [
        f"{path.name}: {command}"
        for path in sorted((_ROOT / "tests").glob("*.py"))
        if path.name != Path(__file__).name  # its control writes on purpose
        for command in _vims_without_viminfo_off(
            path.read_text(), r"\bvim\b[^\"'\n]*?-u NONE[^\"'\n]*"
        )
    ] + [
        f"{path.name}: {command}"
        for path in sorted((_ROOT / "scripts").glob("*.sh"))
        if "TMUX_TMPDIR=" in path.read_text()
        for command in _vims_without_viminfo_off(path.read_text(), r"[\"']vim\b[^\"'\n]*[\"']")
    ]
    assert offenders == []


# A script that runs the follower's CLI under the developer's real HOME reads
# and writes their live followers' state: a private tmux server numbers its
# windows from @0 like theirs, and the state files are named by window id
# (smoke-autonomous.sh could overwrite or stop a live follower; T24b re-review).
_FOLLOWER_CALL = re.compile(r'"\$CF"|vim_ai_follower\.cli|bin/claude-follow"? ')
_TMUX_CALL = re.compile(r"\btmux\b")


def _first_line(lines: list[str], pattern: re.Pattern[str]) -> int | None:
    return next(
        (
            index
            for index, line in enumerate(lines)
            if not line.lstrip().startswith("#") and pattern.search(line)
        ),
        None,
    )


def _calls_before_its_own_home(text: str) -> bool:
    """Whether a script runs tmux or the follower's CLI before it exports a
    HOME of its own (or never exports one) — when it runs the CLI at all."""
    lines = text.splitlines()
    first_follower_call = _first_line(lines, _FOLLOWER_CALL)
    if first_follower_call is None:
        return False
    home = _first_line(lines, re.compile(r"^\s*export HOME="))
    first_call = min(
        index
        for index in (first_follower_call, _first_line(lines, _TMUX_CALL))
        if index is not None
    )
    return home is None or home > first_call


def test_every_script_that_runs_the_follower_on_a_private_server_has_its_own_home() -> None:
    offenders = [
        script.name
        for script in sorted((_ROOT / "scripts").glob("*.sh"))
        if "TMUX_TMPDIR=" in script.read_text() and _calls_before_its_own_home(script.read_text())
    ]
    assert offenders == []


@pytest.mark.parametrize(
    ("script", "offends"),
    [
        ('export HOME=$(mktemp -d)\ntmux -f /dev/null new -d\n"$CF" start', False),
        ('tmux -f /dev/null new -d\nexport HOME=$(mktemp -d)\n"$CF" start', True),
        ('tmux -f /dev/null new -d\n"$CF" start', True),
        ("CLI() { python3 -m vim_ai_follower.cli; }\ntmux new -d", True),
        ('# export HOME=x\ntmux new -d\n"$CF" start', True),
        ("tmux -f /dev/null new -d\nvim -i NONE", False),
    ],
)
def test_the_home_check_finds_a_call_before_the_home(script: str, offends: bool) -> None:
    assert _calls_before_its_own_home(script) is offends
