"""Real tmux server: the prefix keys follow a plugin update on the next hook."""

from __future__ import annotations

import shutil
import subprocess
from collections.abc import Callable
from pathlib import Path

import pytest
from test_integration_tab_eviction import _pane_ids, _start_follower

from vim_ai_follower import hooks

pytestmark = pytest.mark.integration


def _install(root: Path, version: str) -> Path:
    plugin_root = root / "vim-ai-follower" / version
    exe = plugin_root / "bin" / "claude-follow"
    exe.parent.mkdir(parents=True)
    exe.write_text("#!/bin/sh\n")
    exe.chmod(0o755)
    return plugin_root


def _bound_keys() -> dict[str, str]:
    out = subprocess.run(
        ["tmux", "list-keys", "-T", "prefix"], capture_output=True, text=True, check=True
    ).stdout
    # Measured on this tmux: tokens are `bind-key -T prefix <key> ...`, padded
    # with runs of spaces, so match on the split tokens, never a substring.
    return {
        tokens[3]: line
        for line in out.splitlines()
        if (tokens := line.split())[:3] == ["bind-key", "-T", "prefix"]
        and tokens[3] in {"P", "S", "+", "_", "F"}
    }


@pytest.mark.parametrize("old_survives", [False, True])
def test_keys_follow_a_plugin_update_on_the_next_hook(
    tmux_session: str,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    wait_until: Callable[..., bool],
    old_survives: bool,
) -> None:
    old = _install(tmp_path, "0.1.0")
    new = _install(tmp_path, "0.2.0")
    monkeypatch.setenv("CLAUDE_PLUGIN_ROOT", str(old))
    _start_follower(tmux_session, monkeypatch, wait_until)
    keys = _bound_keys()
    assert len(keys) == 5 and all("0.1.0" in line for line in keys.values()), keys

    if not old_survives:
        shutil.rmtree(old)
    monkeypatch.setenv("CLAUDE_PLUGIN_ROOT", str(new))
    target = tmp_path / "f.py"
    target.write_text("x\n")
    origin = _pane_ids(tmux_session)[0]
    payload = {"tool_name": "Read", "tool_input": {"file_path": str(target)}}
    assert hooks.cmd_hook_post({"TMUX_PANE": origin}, payload) == 0

    keys = _bound_keys()
    assert len(keys) == 5
    assert all(str(new / "bin" / "claude-follow") in line for line in keys.values()), keys
