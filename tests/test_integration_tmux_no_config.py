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
import shlex
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


# tmux's options that take a value (getopt "c:f:L:S:T:"), so the rest of
# a clustered word, or the next word, is that value and not the command.
_OPTIONS_WITH_A_VALUE = set("cfLST")
# Commands that start a server when none runs (and so read a config):
# measured on tmux 3.8 (T24b re-review m4 residual). tmux also takes any
# unambiguous prefix of a command name, and `new`/`a` are aliases.
_SERVER_STARTERS = ("new-session", "start-server", "attach-session", "list-commands")
_STARTER_ALIASES = {"new", "start", "attach", "a", "lscm"}
# The tmux binary as a script names it: a path ending in `tmux` (quotes
# are gone after shlex), or a variable that holds it (`$TMUX_BIN`,
# `${TMUX_BIN:-tmux}`), never `$TMUX`/`$TMUX_PANE`, which are not commands.
_TMUX_BINARY = re.compile(r"^(?:\S*/)?tmux$|^\$\{?\w*TMUX\w*(?:_BIN|_CMD)\w*(?::-[^}]*)?\}?$")
# tmux's own command separator, `\;`, kept apart from the shell's `;`.
_TMUX_SEPARATOR = "\0tmux-separator"
_SHELL_SEPARATORS = {";", ";;", "&", "&&", "|", "||", "(", ")"}


def _starts_a_server(command: str) -> bool:
    return command in _STARTER_ALIASES or (
        len(command) >= 3 and any(name.startswith(command) for name in _SERVER_STARTERS)
    )


def _words(line: str) -> list[str]:
    lexer = shlex.shlex(
        line.replace("\\;", f" {_TMUX_SEPARATOR} "), posix=True, punctuation_chars=";&|()"
    )
    lexer.whitespace_split = True
    try:
        return list(lexer)
    except ValueError:  # a line of a multi-line string or heredoc
        return line.split()


def _invocations(words: list[str]) -> list[tuple[bool, list[str]]]:
    """Each tmux invocation in `words`: whether it names a config with `-f`,
    and every command it runs (one per `\\;`-separated part)."""
    invocations = []
    index = 0
    while index < len(words):
        if not _TMUX_BINARY.match(words[index]):
            index += 1
            continue
        index += 1
        named_config = False
        while index < len(words) and len(words[index]) > 1 and words[index].startswith("-"):
            flags = words[index][1:]
            index += 1
            for position, flag in enumerate(flags):
                if flag in _OPTIONS_WITH_A_VALUE:
                    named_config = named_config or flag == "f"
                    index += 1 if position == len(flags) - 1 else 0
                    break
        commands: list[str] = []
        awaiting_command = True
        while index < len(words) and words[index] not in _SHELL_SEPARATORS:
            if words[index] == _TMUX_SEPARATOR:
                awaiting_command = True
            elif awaiting_command:
                commands.append(words[index])
                awaiting_command = False
            index += 1
        invocations.append((named_config, commands))
    return invocations


def _unconfigured_server_starts(text: str) -> list[str]:
    """Every tmux invocation in a script's text that may start a server and
    names no config with `-f`. Backslash continuations are joined first
    (T24b re-review m-B: `tmux \\` + newline + `new-session` slipped past a
    per-line grep, as did the `new` alias), and each line is read as the
    shell reads it (T24b re-review 2 m1): a quoted or variable binary, a
    wrapper (`env`, `command`), clustered flags (`-uS`) and a `\\;` chain
    inside one tmux no longer dodge it."""
    offenders = []
    joined = re.sub(r"\\\n", " ", text)
    for line in joined.splitlines():
        if line.lstrip().startswith("#"):
            continue
        for named_config, commands in _invocations(_words(line)):
            if not named_config and any(_starts_a_server(command) for command in commands):
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
        # The aliases (T24b re-review 2 m2).
        ("tmux a -t x", ["tmux a -t x"]),
        ("tmux lscm", ["tmux lscm"]),
        # The bypasses of a word-by-word scan (m1): a variable or a quoted
        # name for the binary, a wrapper, a `\;` chain inside one tmux,
        # clustered flags.
        ('"$TMUX_BIN" new -d', ['"$TMUX_BIN" new -d']),
        ("${TMUX_BIN:-tmux} new -d", ["${TMUX_BIN:-tmux} new -d"]),
        ('"tmux" new -d', ['"tmux" new -d']),
        ("env -u TMUX tmux new -d", ["env -u TMUX tmux new -d"]),
        ("command tmux new -d", ["command tmux new -d"]),
        (
            "tmux -S /tmp/s new-window -t a \\; new-session -d",
            ["tmux -S /tmp/s new-window -t a \\; new-session -d"],
        ),
        ("tmux -uS /tmp/s new -d", ["tmux -uS /tmp/s new -d"]),
        ("tmux -uf /dev/null new -d", []),
        ("tmux -f/dev/null new -d", []),
        ("tmux -S /tmp/s -f /dev/null new-window \\; new -d", []),
        ('echo "tmux new" | cat', []),
    ],
)
def test_the_script_check_finds_every_unconfigured_server_start(
    script: str, expected: list[str]
) -> None:
    assert _unconfigured_server_starts(script) == expected
