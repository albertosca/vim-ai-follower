"""No test Vim or shell writes into the developer's HOME.

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
