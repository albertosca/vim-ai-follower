from __future__ import annotations

import io
import json
import subprocess
from collections.abc import Callable
from pathlib import Path

import pytest

from vim_ai_follower import cli, snapshot, state

pytestmark = pytest.mark.integration


@pytest.fixture(autouse=True)
def isolated_dirs(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(state, "STATE_DIR", tmp_path / "state")
    monkeypatch.setattr(snapshot, "SNAPSHOT_DIR", tmp_path / "snapshots")
    monkeypatch.setattr(cli, "LOG_PATH", tmp_path / "hook.log")


def _pane_ids(session_name: str) -> list[str]:
    result = subprocess.run(
        ["tmux", "list-panes", "-t", session_name, "-F", "#{pane_id}"],
        capture_output=True,
        text=True,
        check=True,
    )
    return result.stdout.splitlines()


def _capture(pane_id: str) -> str:
    result = subprocess.run(
        ["tmux", "capture-pane", "-t", pane_id, "-p"],
        capture_output=True,
        text=True,
        check=True,
    )
    return result.stdout


def test_start_creates_a_side_by_side_vim_pane(
    tmux_session: str, monkeypatch: pytest.MonkeyPatch, wait_until: Callable[..., bool]
) -> None:
    monkeypatch.setenv("TMUX_PANE", _pane_ids(tmux_session)[0])
    assert cli.main(["start"]) == 0
    assert wait_until(lambda: len(_pane_ids(tmux_session)) == 2)


def test_start_is_idempotent(
    tmux_session: str, monkeypatch: pytest.MonkeyPatch, wait_until: Callable[..., bool]
) -> None:
    monkeypatch.setenv("TMUX_PANE", _pane_ids(tmux_session)[0])
    assert cli.main(["start"]) == 0
    assert wait_until(lambda: len(_pane_ids(tmux_session)) == 2)
    assert cli.main(["start"]) == 0
    assert len(_pane_ids(tmux_session)) == 2


def test_stop_kills_the_follower_pane(
    tmux_session: str, monkeypatch: pytest.MonkeyPatch, wait_until: Callable[..., bool]
) -> None:
    monkeypatch.setenv("TMUX_PANE", _pane_ids(tmux_session)[0])
    cli.main(["start"])
    assert wait_until(lambda: len(_pane_ids(tmux_session)) == 2)
    assert cli.main(["stop"]) == 0
    assert wait_until(lambda: len(_pane_ids(tmux_session)) == 1)


def test_hook_pre_and_post_animate_an_edit(
    tmux_session: str,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    wait_until: Callable[..., bool],
) -> None:
    origin_pane = _pane_ids(tmux_session)[0]
    monkeypatch.setenv("TMUX_PANE", origin_pane)
    assert cli.main(["start"]) == 0
    assert wait_until(lambda: len(_pane_ids(tmux_session)) == 2)
    follower_pane_id = next(p for p in _pane_ids(tmux_session) if p != origin_pane)

    target_file = tmp_path / "greeting.txt"
    target_file.write_text("hello\nworld\n")

    pre_payload = json.dumps({"tool_name": "Write", "tool_input": {"file_path": str(target_file)}})
    monkeypatch.setattr("sys.stdin", io.StringIO(pre_payload))
    assert cli.main(["hook", "pre"]) == 0

    target_file.write_text("hello\nvim ai follower\n")
    post_payload = json.dumps({"tool_name": "Write", "tool_input": {"file_path": str(target_file)}})
    monkeypatch.setattr("sys.stdin", io.StringIO(post_payload))
    assert cli.main(["hook", "post"]) == 0

    assert wait_until(lambda: "vim ai follower" in _capture(follower_pane_id), timeout=5.0)


def test_hook_post_read_navigates_the_follower_pane(
    tmux_session: str,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    wait_until: Callable[..., bool],
) -> None:
    origin_pane = _pane_ids(tmux_session)[0]
    monkeypatch.setenv("TMUX_PANE", origin_pane)
    assert cli.main(["start"]) == 0
    assert wait_until(lambda: len(_pane_ids(tmux_session)) == 2)
    follower_pane_id = next(p for p in _pane_ids(tmux_session) if p != origin_pane)

    target_file = tmp_path / "readme.txt"
    target_file.write_text("first line\nsecond line\n")

    read_payload = json.dumps({"tool_name": "Read", "tool_input": {"file_path": str(target_file)}})
    monkeypatch.setattr("sys.stdin", io.StringIO(read_payload))
    assert cli.main(["hook", "post"]) == 0

    assert wait_until(lambda: "first line" in _capture(follower_pane_id), timeout=5.0)
