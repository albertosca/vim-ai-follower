"""The saved-keybindings record and its restore at the last `stop` (final
review M5 to M8): a damaged record never fails `stop`, the first registration's
record is never overwritten by a racing one, a failed `source-file` asks
tmux nothing more, and a saved line restores only the key it was saved for."""

from __future__ import annotations

import functools
import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from helpers import make_mock_tmux_run

from vim_ai_follower import commands, keybindings

pytestmark = pytest.mark.usefixtures("temporaries_in_tmp_path")

_mock_tmux_run = functools.partial(make_mock_tmux_run, pane_id="%9", other_panes=("%1",))


def _write_saved(content: bytes) -> None:
    path = keybindings._saved_bindings_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)


def _unbound(run: MagicMock) -> list[str]:
    return [
        call.args[0][-1]
        for call in run.call_args_list
        if call.args[0][:2] == ["tmux", "unbind-key"]
    ]


_DAMAGED = [
    pytest.param(b"[]", id="a list"),
    pytest.param(b"null", id="null"),
    pytest.param(b'{"P": 5, "S": ["x"]}', id="non-string values"),
    pytest.param(b'{"P": "bind-key -T prefix P send-keys \xff"}', id="not UTF-8"),
    pytest.param(b'"bind-key"', id="a string"),
]


@pytest.mark.parametrize("content", _DAMAGED)
def test_unregister_with_a_damaged_record_unbinds_every_key_and_removes_it(
    content: bytes,
) -> None:
    _write_saved(content)
    with patch("vim_ai_follower.tmux.subprocess.run", side_effect=_mock_tmux_run()) as run:
        keybindings.unregister()
    assert sorted(_unbound(run)) == sorted(key for key, _ in keybindings._KEYBINDINGS)
    assert not keybindings._saved_bindings_path().exists()


@pytest.mark.parametrize("content", _DAMAGED)
def test_the_last_stop_with_a_damaged_record_exits_zero(content: bytes) -> None:
    with patch("vim_ai_follower.tmux.subprocess.run", side_effect=_mock_tmux_run()):
        assert commands.cmd_start({"TMUX_PANE": "%1"}) == 0
        _write_saved(content)
        assert commands.cmd_stop({"TMUX_PANE": "%1"}) == 0


def test_the_last_stop_survives_an_unregister_that_raises(
    capsys: pytest.CaptureFixture[str],
) -> None:
    with patch("vim_ai_follower.tmux.subprocess.run", side_effect=_mock_tmux_run()):
        assert commands.cmd_start({"TMUX_PANE": "%1"}) == 0
        with patch(
            "vim_ai_follower.commands.keybindings.unregister", side_effect=RuntimeError("boom")
        ):
            assert commands.cmd_stop({"TMUX_PANE": "%1"}) == 0
    assert "claude-follow: stopped" in capsys.readouterr().out


def test_a_racing_registration_never_replaces_the_first_record() -> None:
    """Two registers at once: both find no record, the first saves the
    user's bindings and binds ours, and the second lists OURS and saved them
    as the user's — whose keys were then lost for good at `stop`."""
    first = {
        key: f"bind-key -T prefix {key} send-keys user-{key}" for key, _ in keybindings._KEYBINDINGS
    }

    def other_register_wins(key: str) -> str:
        # The other register writes its record while this one is listing.
        path = keybindings._saved_bindings_path()
        if not path.exists():
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(first))
        return f"bind-key -T prefix {key} run-shell -b 'claude-follow pause'"

    with (
        patch("vim_ai_follower.tmux.subprocess.run", side_effect=_mock_tmux_run()),
        patch.object(keybindings, "_existing_binding", side_effect=other_register_wins),
    ):
        keybindings.register()
    assert json.loads(keybindings._saved_bindings_path().read_text()) == first
    leftovers = [
        path.name
        for path in keybindings._saved_bindings_path().parent.iterdir()
        if path.name.startswith(".")
    ]
    assert leftovers == []


def test_a_first_registration_saves_the_users_bindings() -> None:
    listed = "bind-key -T prefix P paste-buffer\n"

    def run(command: list[str], **options: object) -> MagicMock:
        if command[:4] == ["tmux", "list-keys", "-T", "prefix"]:
            return MagicMock(returncode=0, stdout=listed)
        return _mock_tmux_run()(command, **options)

    with patch("vim_ai_follower.tmux.subprocess.run", side_effect=run):
        keybindings.register()
    saved = json.loads(keybindings._saved_bindings_path().read_text())
    assert saved["P"] == "bind-key -T prefix P paste-buffer"
    assert saved["S"] is None


def test_a_failed_source_asks_tmux_nothing_more() -> None:
    """M7: on a dead server `list-keys` STARTS one, which loads the user's
    real tmux.conf (run-shell, resurrect, continuum). A source-file that
    failed proves nothing anyway, so no listing follows it."""
    _write_saved(json.dumps({"P": "bind-key    -T prefix P       send-keys a"}).encode())
    commands_seen: list[list[str]] = []

    def run(command: list[str], **_options: object) -> MagicMock:
        commands_seen.append(command)
        if command[:2] == ["tmux", "source-file"]:
            return MagicMock(returncode=1, stdout="", stderr="no server running")
        return MagicMock(returncode=0, stdout="", stderr="")

    with patch("vim_ai_follower.tmux.subprocess.run", side_effect=run):
        keybindings.unregister()
    assert [command[1] for command in commands_seen if command[1] == "list-keys"] == []
    assert ["tmux", "unbind-key", "-T", "prefix", "P"] in commands_seen


@pytest.mark.parametrize(
    "previous",
    [
        "bind-key -T root C-c send-keys x",
        "bind-key    -T prefix S       send-keys x",
        "bind-key -n C-c send-keys x",
        "bind-key -T copy-mode P send-keys x",
        "bind-key P send-keys x",
        "bind-key -r -T root P send-keys x",
    ],
)
def test_a_saved_line_for_another_key_is_never_sourced(previous: str) -> None:
    """M8: a tampered record that binds a DIFFERENT key under P passed the
    one-binding check and was sourced, rebinding root C-c for good."""
    _write_saved(json.dumps({"P": previous}).encode())
    sourced: list[str] = []

    def run(command: list[str], **_options: object) -> MagicMock:
        if command[:2] == ["tmux", "source-file"]:
            sourced.append(Path(command[2]).read_text())
        if command[:2] == ["tmux", "list-keys"]:
            return MagicMock(returncode=0, stdout=previous + "\n", stderr="")
        return MagicMock(returncode=0, stdout="", stderr="")

    with patch("vim_ai_follower.tmux.subprocess.run", side_effect=run) as tmux:
        keybindings.unregister()
    assert sourced == []
    assert "P" in _unbound(tmux)


@pytest.mark.parametrize(
    "previous",
    [
        "bind-key    -T prefix P       send-keys x",
        "bind-key -r -T prefix P send-keys x",
    ],
)
def test_a_saved_line_for_its_own_key_is_sourced(previous: str) -> None:
    _write_saved(json.dumps({"P": previous}).encode())
    sourced: list[str] = []

    def run(command: list[str], **_options: object) -> MagicMock:
        if command[:2] == ["tmux", "source-file"]:
            sourced.append(Path(command[2]).read_text())
        if command[:2] == ["tmux", "list-keys"]:
            return MagicMock(returncode=0, stdout=previous + "\n", stderr="")
        return MagicMock(returncode=0, stdout="", stderr="")

    with patch("vim_ai_follower.tmux.subprocess.run", side_effect=run) as tmux:
        keybindings.unregister()
    assert sourced == [previous + "\n"]
    assert "P" not in _unbound(tmux)
