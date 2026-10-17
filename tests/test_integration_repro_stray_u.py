"""scripts/repro-stray-u.sh runs here, so the Escape half of the stray-`u`
fix is checked on every full suite rather than by hand.

The script opens a real built-in completion popup under an
`inoremap <expr> <Esc> pumvisible() ? ...` map in a hermetic Vim (VIMINIT,
noloadplugins) on a private tmux server (TMUX unset, its own TMUX_TMPDIR),
and requires that one Escape before the rollback's `u` leaves `banu` in the
buffer while two leave it empty (animate._exit_insert_mode sends two). Exit
2 means the single-Escape case no longer reproduces the bug, so the check
would prove nothing: that fails here too."""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

pytestmark = pytest.mark.integration

_SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "repro-stray-u.sh"


@pytest.mark.skipif(shutil.which("zsh") is None, reason="the script is zsh")
def test_two_escapes_clear_a_popup_that_eats_the_first() -> None:
    environment = {
        key: value for key, value in os.environ.items() if key not in ("TMUX", "TMUX_PANE")
    }
    result = subprocess.run(
        ["zsh", str(_SCRIPT)],
        env=environment,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "PASS:" in result.stdout
