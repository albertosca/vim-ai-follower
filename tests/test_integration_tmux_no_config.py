"""Every private tmux server the tests and scripts start loads NO config.

Without `-f /dev/null` a server started under the developer's HOME reads
their real `~/.config/tmux/tmux.conf` — on this machine TPM, resurrect,
continuum with restore on, and `session-created`/`window-linked` hooks that
run `~/.claude/hooks/tmux-pending.sh`, the class of the 2026-09-01 incident
in which a test server's hooks cost real data (T24b review m4). tmux reports
the files a server loaded in `#{config_files}`: `/dev/null` under
`-f /dev/null`, and with no `-f` the default files that exist (none on CI,
which is why the assertion names `/dev/null` rather than "no user file")."""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

import pytest
from e2e_harness import e2e_world
from test_integration_handoff_write import _prove_private

pytestmark = pytest.mark.integration

_SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"


def test_the_tmux_session_server_loads_no_config(tmux_session: str) -> None:
    _prove_private()
    loaded = subprocess.run(
        ["tmux", "display-message", "-p", "#{config_files}"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    assert loaded == "/dev/null"


def test_the_e2e_server_loads_no_config() -> None:
    with e2e_world() as world:
        loaded = world.tmux("display-message", "-p", "#{config_files}").stdout.strip()
    assert loaded == "/dev/null"


def test_every_script_that_starts_a_private_server_passes_no_config() -> None:
    # A script on a private TMUX_TMPDIR starts its own server, and every
    # new-session there may be the one that starts it (a kill-session of
    # the last session ends the server). The scripts that add a session to
    # the developer's REAL server on purpose set no TMUX_TMPDIR; tmux
    # ignores `-f` for a server that already runs. A script may name its own
    # config instead (record_demo.sh's, from the repo): any explicit `-f`.
    offenders = []
    for script in sorted(_SCRIPTS.glob("*.sh")):
        text = script.read_text()
        if "TMUX_TMPDIR=" not in text:
            continue
        for number, line in enumerate(text.splitlines(), start=1):
            if re.search(r"\btmux\b.*\bnew-session\b", line) and not re.search(r"\s-f\s", line):
                offenders.append(f"{script.name}:{number}: {line.strip()}")
    assert offenders == []
