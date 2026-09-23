"""Keybinding ownership: which installation the server-global prefix keys
belong to, and the rule for re-pointing or taking them (spec:
2026-09-22-keybinding-ownership-design)."""

from __future__ import annotations

import functools
import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from helpers import make_mock_tmux_run

from vim_ai_follower import keybindings

_mock_tmux_run = functools.partial(make_mock_tmux_run, pane_id="%9", other_panes=("%1",))


def _fake_install(root: Path, version: str) -> Path:
    """A plugin-cache-shaped install: <root>/vim-ai-follower/<version>/bin/claude-follow."""
    plugin_root = root / "vim-ai-follower" / version
    exe = plugin_root / "bin" / "claude-follow"
    exe.parent.mkdir(parents=True)
    exe.write_text("#!/bin/sh\n")
    exe.chmod(0o755)
    return plugin_root


def test_plugin_installation_id_strips_the_version_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    plugin_root = _fake_install(tmp_path, "0.2.0")
    monkeypatch.setenv("CLAUDE_PLUGIN_ROOT", str(plugin_root))
    owner = keybindings.current_owner()
    assert owner.executable == str(plugin_root / "bin" / "claude-follow")
    assert owner.installation == str(plugin_root.parent)


def test_non_plugin_installation_id_is_the_executable_itself(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("CLAUDE_PLUGIN_ROOT", raising=False)
    exe = tmp_path / "checkout" / "bin" / "claude-follow"
    monkeypatch.setattr(keybindings, "_claude_follow_executable", lambda: str(exe))
    assert keybindings.current_owner() == keybindings.Owner(str(exe), str(exe))


def test_register_records_the_owner_and_unregister_deletes_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    plugin_root = _fake_install(tmp_path, "0.2.0")
    monkeypatch.setenv("CLAUDE_PLUGIN_ROOT", str(plugin_root))
    with patch("vim_ai_follower.tmux.subprocess.run", side_effect=_mock_tmux_run()):
        keybindings.register()
        assert json.loads(keybindings._owner_path().read_text()) == {
            "executable": str(plugin_root / "bin" / "claude-follow"),
            "installation": str(plugin_root.parent),
        }
        keybindings.unregister()
    assert not keybindings._owner_path().exists()


@pytest.mark.parametrize("content", ["", "{not json", "[]", '{"executable": "/x"}'])
def test_a_malformed_owner_record_reads_as_absent(content: str) -> None:
    path = keybindings._owner_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)
    assert keybindings.read_owner() is None
