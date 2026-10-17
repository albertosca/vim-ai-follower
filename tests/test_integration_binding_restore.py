"""A user's own tmux binding on a follower key comes back after `stop`, on a
REAL tmux, or at least ours never stays bound in its place.

`keybindings.unregister` used to rebuild the saved list-keys line through
shlex into a bind-key argv: a brace block failed to bind and left the
claude-follow binding bound, `'$HOME'` came back `\\\\\\$HOME`, a literal `;`
key came back as no key, and a chain bound only its first command (T24a
review m4, 3714a9b). It now sources the line verbatim and checks the result.
"""

from __future__ import annotations

import logging
import subprocess
import tempfile
from pathlib import Path

import pytest
from test_integration_handoff_write import _prove_private

from vim_ai_follower import keybindings

pytestmark = pytest.mark.integration


def _listed(key: str) -> str | None:
    for line in subprocess.run(
        ["tmux", "list-keys", "-T", "prefix"], capture_output=True, text=True, check=True
    ).stdout.splitlines():
        if line.split()[2:4] == ["prefix", key]:
            return " ".join(line.split())
    return None


def _bind_as_the_user(config: str) -> None:
    with tempfile.NamedTemporaryFile("w", suffix=".conf", delete=False) as handle:
        handle.write(config + "\n")
    try:
        subprocess.run(["tmux", "source-file", handle.name], check=True)
    finally:
        Path(handle.name).unlink()


@pytest.mark.parametrize(
    "config",
    [
        "bind-key -T prefix P if-shell true { display-message a ; display-message b }",
        "bind-key -T prefix P display-message '$HOME'",
        r"bind-key -T prefix P send-keys '\;'",
        r"bind-key -T prefix P send-keys '\;' \; display-message b",
        r"bind-key -T prefix P send-keys x \; send-keys y",
        r"bind-key -T prefix P send-keys 'a;' \; display-message 'b;' \; send-keys c",
        "bind-key -T prefix P if-shell true {\n send-keys a\n send-keys b\n}",
        "bind-key -T prefix P if-shell -F '#{==:#{pane_mode},}'"
        " { send-keys -X cancel } { display-message x }",
        'bind-key -T prefix P run-shell "echo \\"#{pane_id}\\" > /dev/null"',
        r'bind-key -T prefix P display-message "x \\; y é"',
    ],
)
def test_the_users_binding_comes_back_after_stop(tmux_session: str, config: str) -> None:
    _prove_private()
    _bind_as_the_user(config)
    before = _listed("P")
    assert before is not None and "claude-follow" not in before
    keybindings.register()
    bound = _listed("P")
    assert bound is not None and "claude-follow" in bound
    keybindings.unregister()
    restored = _listed("P")
    assert restored is not None
    assert restored.replace(" ;; ", " ; ") == before.replace(" ;; ", " ; ")


def test_a_binding_that_cannot_be_restored_leaves_the_key_unbound_and_says_so(
    tmux_session: str, caplog: pytest.LogCaptureFixture
) -> None:
    # list-keys prints a literal `;` argument and a chain separator alike
    # (`send-keys a '\;' b` lists `send-keys a \; b`), so this one reads back
    # as a chain whose second command is `b`, which tmux refuses.
    _prove_private()
    _bind_as_the_user(r"bind-key -T prefix P send-keys a '\;' b")
    keybindings.register()
    with caplog.at_level(logging.WARNING, logger="vim_ai_follower"):
        keybindings.unregister()
    assert _listed("P") is None
    assert any(
        "could not restore your tmux binding" in record.getMessage() for record in caplog.records
    )
