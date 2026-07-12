from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

from vim_ai_follower.state import FollowerState, touch_open_files


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


def test_state_round_trips_new_fields(tmp_path: Path) -> None:
    FollowerState.set(
        "$9",
        "tmux",
        "%5",
        base_dir=tmp_path,
        open_files=("/a.py", "/b.py"),
        enabled=False,
        adopted=True,
        shown_any=True,
    )
    raw = FollowerState.read("$9", base_dir=tmp_path)
    assert raw is not None
    assert raw.open_files == ("/a.py", "/b.py")
    assert (raw.enabled, raw.adopted, raw.shown_any) == (False, True, True)


def test_read_defaults_new_fields_for_old_state_files(tmp_path: Path) -> None:
    (tmp_path / "$9.pane").write_text('{"backend": "tmux", "target": "%5"}')
    raw = FollowerState.read("$9", base_dir=tmp_path)
    assert raw is not None
    assert (raw.open_files, raw.enabled, raw.adopted, raw.shown_any) == ((), True, False, False)


def test_update_replaces_only_given_fields(tmp_path: Path) -> None:
    FollowerState.set("$9", "tmux", "%5", speed="normal", base_dir=tmp_path)
    FollowerState.update("$9", base_dir=tmp_path, speed="lento", enabled=False)
    raw = FollowerState.read("$9", base_dir=tmp_path)
    assert raw is not None
    assert (raw.speed, raw.enabled, raw.target) == ("lento", False, "%5")


def test_update_without_state_is_a_no_op(tmp_path: Path) -> None:
    FollowerState.update("$9", base_dir=tmp_path, speed="lento")
    assert FollowerState.read("$9", base_dir=tmp_path) is None


def test_touch_open_files_recency_and_eviction() -> None:
    assert touch_open_files((), "/a.py", 5) == (("/a.py",), ())
    assert touch_open_files(("/a.py", "/b.py"), "/a.py", 5) == (("/b.py", "/a.py"), ())
    assert touch_open_files(("/a.py", "/b.py", "/c.py"), "/d.py", 3) == (
        ("/b.py", "/c.py", "/d.py"),
        ("/a.py",),
    )
    assert touch_open_files(("/a.py",), "/b.py", 1) == (("/b.py",), ("/a.py",))
