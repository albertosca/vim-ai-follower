from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

from vim_ai_follower.state import FollowerState
from vim_ai_follower.tmux import TmuxPane


def _always_exists_pane(pane_id: str = "%5") -> TmuxPane:
    return TmuxPane(pane_id=pane_id)


def test_get_returns_none_when_no_state_file(tmp_path: Path) -> None:
    assert FollowerState.get("$1", base_dir=tmp_path) is None


def test_set_then_get_round_trips(tmp_path: Path) -> None:
    pane = _always_exists_pane()
    with patch("vim_ai_follower.tmux.subprocess.run", return_value=MagicMock(stdout="%5\n")):
        FollowerState.set("$1", pane, current_file="/tmp/a.txt", base_dir=tmp_path)
        result = FollowerState.get("$1", base_dir=tmp_path)
    assert result is not None
    assert result.pane.pane_id == "%5"
    assert result.current_file == "/tmp/a.txt"


def test_get_returns_none_when_pane_is_dead(tmp_path: Path) -> None:
    pane = _always_exists_pane()
    with patch("vim_ai_follower.tmux.subprocess.run", return_value=MagicMock(stdout="%5\n")):
        FollowerState.set("$1", pane, base_dir=tmp_path)
    with patch("vim_ai_follower.tmux.subprocess.run", return_value=MagicMock(stdout="")):
        assert FollowerState.get("$1", base_dir=tmp_path) is None


def test_update_current_file_preserves_pane(tmp_path: Path) -> None:
    pane = _always_exists_pane()
    with patch("vim_ai_follower.tmux.subprocess.run", return_value=MagicMock(stdout="%5\n")):
        FollowerState.set("$1", pane, base_dir=tmp_path)
        FollowerState.update_current_file("$1", "/tmp/b.txt", base_dir=tmp_path)
        result = FollowerState.get("$1", base_dir=tmp_path)
    assert result is not None
    assert result.current_file == "/tmp/b.txt"
    assert result.pane.pane_id == "%5"


def test_update_current_file_is_a_noop_without_existing_state(tmp_path: Path) -> None:
    FollowerState.update_current_file("$1", "/tmp/b.txt", base_dir=tmp_path)
    assert FollowerState.get("$1", base_dir=tmp_path) is None


def test_clear_removes_state(tmp_path: Path) -> None:
    pane = _always_exists_pane()
    with patch("vim_ai_follower.tmux.subprocess.run", return_value=MagicMock(stdout="%5\n")):
        FollowerState.set("$1", pane, base_dir=tmp_path)
        FollowerState.clear("$1", base_dir=tmp_path)
        assert FollowerState.get("$1", base_dir=tmp_path) is None


def test_clear_without_existing_state_does_not_raise(tmp_path: Path) -> None:
    FollowerState.clear("$1", base_dir=tmp_path)
