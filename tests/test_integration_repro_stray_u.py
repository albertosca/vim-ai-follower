"""scripts/repro-stray-u.sh runs here, so the Escape half of the stray-`u`
fix is checked on every full suite rather than by hand.

The script opens a real built-in completion popup under an
`inoremap <expr> <Esc> pumvisible() ? ...` map in a hermetic Vim (VIMINIT,
noloadplugins) on a private tmux server (TMUX unset, its own TMUX_TMPDIR),
and requires that one Escape before the rollback's `u` leaves `banu` in the
buffer while two leave it empty (animate._exit_insert_mode sends two). Exit
2 means the single-Escape case no longer reproduces the bug, so the check
would prove nothing: that fails here too.

T24b review m5: every run also leaves nothing behind. Its Vims run under
a HOME of their own (a killed Vim wrote `.viminfo` there before `-i NONE`),
and its `/tmp/vaf-strayu-*` directory goes, a normal run's and an
interrupted one's (the trap stopped at a failing `kill-server` under
`set -e`, and zsh runs no EXIT trap for a signal), with its server gone."""

from __future__ import annotations

import os
import shutil
import signal
import subprocess
import time
from pathlib import Path

import pytest

pytestmark = pytest.mark.integration

_SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "repro-stray-u.sh"


def _directories() -> set[Path]:
    return set(Path("/tmp").glob("vaf-strayu-*"))


def _environment(home: Path) -> dict[str, str]:
    environment = {
        key: value for key, value in os.environ.items() if key not in ("TMUX", "TMUX_PANE")
    }
    environment["HOME"] = str(home)
    return environment


def _servers() -> list[int]:
    found = subprocess.run(
        ["pgrep", "-f", "new-session -d -s strayu"], capture_output=True, text=True, check=False
    )
    return [int(pid) for pid in found.stdout.split()]


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


@pytest.mark.skipif(shutil.which("zsh") is None, reason="the script is zsh")
def test_two_escapes_clear_a_popup_that_eats_the_first(tmp_path: Path) -> None:
    before = _directories()
    result = subprocess.run(
        ["zsh", str(_SCRIPT)],
        env=_environment(tmp_path),
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "PASS:" in result.stdout
    assert not (tmp_path / ".viminfo").exists()
    assert _directories() - before == set()


@pytest.mark.skipif(shutil.which("zsh") is None, reason="the script is zsh")
def test_an_interrupted_run_leaves_no_server_and_no_directory(tmp_path: Path) -> None:
    before = _directories()
    process = subprocess.Popen(
        ["zsh", str(_SCRIPT)],
        env=_environment(tmp_path),
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )
    servers: list[int] = []
    deadline = time.monotonic() + 20
    while not servers and time.monotonic() < deadline:
        servers = _servers()
        time.sleep(0.05)
    process.send_signal(signal.SIGTERM)
    output, _ = process.communicate(timeout=30)
    assert servers, f"the script never started its tmux server: {output!r}"
    assert process.returncode == 143, output
    assert [pid for pid in servers if _alive(pid)] == []
    assert _directories() - before == set()
