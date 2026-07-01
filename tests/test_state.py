from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

from vim_ai_follower.state import FollowerState


def test_get_returns_none_when_no_state_file(tmp_path: Path) -> None:
    assert FollowerState.get("$1", base_dir=tmp_path) is None


def test_set_then_get_round_trips(tmp_path: Path) -> None:
    with patch("vim_ai_follower.tmux.subprocess.run", return_value=MagicMock(stdout="%5 vim\n")):
        FollowerState.set("$1", "tmux", "%5", current_file="/tmp/a.txt", base_dir=tmp_path)
        result = FollowerState.get("$1", base_dir=tmp_path)
    assert result is not None
    assert result.backend == "tmux"
    assert result.target == "%5"
    assert result.current_file == "/tmp/a.txt"


def test_set_defaults_origin_on_failure_and_speed(tmp_path: Path) -> None:
    with patch("vim_ai_follower.tmux.subprocess.run", return_value=MagicMock(stdout="%5 vim\n")):
        FollowerState.set("$1", "tmux", "%5", base_dir=tmp_path)
        result = FollowerState.get("$1", base_dir=tmp_path)
    assert result is not None
    assert result.origin == ""
    assert result.on_failure == "silent"
    assert result.speed == "rapido"


def test_set_stores_origin_on_failure_and_speed(tmp_path: Path) -> None:
    with patch("vim_ai_follower.tmux.subprocess.run", return_value=MagicMock(stdout="%5 vim\n")):
        FollowerState.set(
            "$1",
            "tmux",
            "%5",
            origin="%1",
            on_failure="reopen",
            speed="lento",
            base_dir=tmp_path,
        )
        result = FollowerState.get("$1", base_dir=tmp_path)
    assert result is not None
    assert result.origin == "%1"
    assert result.on_failure == "reopen"
    assert result.speed == "lento"


def test_read_returns_state_even_when_pane_is_dead(tmp_path: Path) -> None:
    with patch("vim_ai_follower.tmux.subprocess.run", return_value=MagicMock(stdout="%5 vim\n")):
        FollowerState.set("$1", "tmux", "%5", origin="%1", base_dir=tmp_path)
    with patch("vim_ai_follower.tmux.subprocess.run", return_value=MagicMock(stdout="")):
        result = FollowerState.read("$1", base_dir=tmp_path)
    assert result is not None
    assert result.target == "%5"
    assert result.origin == "%1"


def test_read_returns_none_without_state_file(tmp_path: Path) -> None:
    assert FollowerState.read("$1", base_dir=tmp_path) is None


def test_get_returns_none_when_pane_is_dead(tmp_path: Path) -> None:
    with patch("vim_ai_follower.tmux.subprocess.run", return_value=MagicMock(stdout="%5 vim\n")):
        FollowerState.set("$1", "tmux", "%5", base_dir=tmp_path)
    with patch("vim_ai_follower.tmux.subprocess.run", return_value=MagicMock(stdout="")):
        assert FollowerState.get("$1", base_dir=tmp_path) is None


def test_update_current_file_preserves_target(tmp_path: Path) -> None:
    with patch("vim_ai_follower.tmux.subprocess.run", return_value=MagicMock(stdout="%5 vim\n")):
        FollowerState.set("$1", "tmux", "%5", base_dir=tmp_path)
        FollowerState.update_current_file("$1", "/tmp/b.txt", base_dir=tmp_path)
        result = FollowerState.get("$1", base_dir=tmp_path)
    assert result is not None
    assert result.current_file == "/tmp/b.txt"
    assert result.target == "%5"


def test_update_current_file_is_a_noop_without_existing_state(tmp_path: Path) -> None:
    FollowerState.update_current_file("$1", "/tmp/b.txt", base_dir=tmp_path)
    assert FollowerState.get("$1", base_dir=tmp_path) is None


def test_clear_removes_state(tmp_path: Path) -> None:
    with patch("vim_ai_follower.tmux.subprocess.run", return_value=MagicMock(stdout="%5 vim\n")):
        FollowerState.set("$1", "tmux", "%5", base_dir=tmp_path)
        FollowerState.clear("$1", base_dir=tmp_path)
        assert FollowerState.get("$1", base_dir=tmp_path) is None


def test_clear_without_existing_state_does_not_raise(tmp_path: Path) -> None:
    FollowerState.clear("$1", base_dir=tmp_path)
