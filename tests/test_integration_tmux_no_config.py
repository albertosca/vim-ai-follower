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


# tmux options that take a value, so the word after them is not the command.
_OPTIONS_WITH_A_VALUE = {"-c", "-f", "-L", "-S", "-T"}
# Commands that start a server when none runs (and so read a config):
# measured on tmux 3.8 (T24b re-review m4 residual). tmux also takes any
# unambiguous prefix of a command name, and `new`/`a` are aliases.
_SERVER_STARTERS = ("new-session", "start-server", "attach-session", "list-commands")
_STARTER_ALIASES = {"new", "start", "attach", "a", "lscm"}


def _starts_a_server(command: str) -> bool:
    return command in _STARTER_ALIASES or (
        len(command) >= 3 and any(name.startswith(command) for name in _SERVER_STARTERS)
    )


def _unconfigured_server_starts(text: str) -> list[str]:
    """Every `tmux …` invocation in a script's text that may start a server
    and names no config with `-f`. Backslash continuations are joined first
    (T24b re-review m-B: `tmux \\` + newline + `new-session` slipped past a
    per-line grep, as did the `new` alias)."""
    offenders = []
    joined = re.sub(r"\\\n", " ", text)
    for line in joined.splitlines():
        if line.lstrip().startswith("#"):
            continue
        for match in re.finditer(r"\btmux\b", line):
            words = line[match.end() :].split()
            index = 0
            named_config = False
            while index < len(words) and words[index].startswith("-"):
                named_config = named_config or words[index] == "-f"
                index += 2 if words[index] in _OPTIONS_WITH_A_VALUE else 1
            if index >= len(words):
                continue
            command = words[index].rstrip(";)\"'")
            if _starts_a_server(command) and not named_config:
                offenders.append(line.strip())
    return offenders


def test_every_script_that_starts_a_private_server_passes_no_config() -> None:
    # A script on a private TMUX_TMPDIR starts its own server, and every
    # server-starting command there may be the one that starts it (a
    # kill-session of the last session ends the server). The scripts that
    # add a session to the developer's REAL server on purpose set no
    # TMUX_TMPDIR; tmux ignores `-f` for a server that already runs. A
    # script may name its own config instead (record_demo.sh's, from the
    # repo): any explicit `-f`.
    offenders = [
        f"{script.name}: {line}"
        for script in sorted(_SCRIPTS.glob("*.sh"))
        if "TMUX_TMPDIR=" in script.read_text()
        for line in _unconfigured_server_starts(script.read_text())
    ]
    assert offenders == []


@pytest.mark.parametrize(
    ("script", "expected"),
    [
        ("tmux -f /dev/null new-session -d -s a", []),
        ("tmux -S /tmp/s -f /dev/null new -d", []),
        ("tmux new -d -s a", ["tmux new -d -s a"]),
        ("tmux new-s -d", ["tmux new-s -d"]),
        ("tmux \\\n  new-session -d -s a", ["tmux    new-session -d -s a"]),
        ("tmux -S /tmp/s start-server", ["tmux -S /tmp/s start-server"]),
        ("X=1 tmux attach -t a", ["X=1 tmux attach -t a"]),
        ("tmux new-window -t a; tmux kill-server", []),
        ("# tmux new-session", []),
    ],
)
def test_the_script_check_finds_every_unconfigured_server_start(
    script: str, expected: list[str]
) -> None:
    assert _unconfigured_server_starts(script) == expected
