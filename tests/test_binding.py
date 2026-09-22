from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

from vim_ai_follower import binding


def test_recall_returns_the_window_remembered_under_the_same_server(tmp_path: Path) -> None:
    with (
        patch("vim_ai_follower.cache.CACHE_DIR", tmp_path),
        patch("vim_ai_follower.binding._tmux_server_pid", return_value=64091),
    ):
        binding.remember("sess-abc", "@18")
        assert binding.recall("sess-abc") == "@18"


def test_recall_discards_a_binding_from_a_previous_tmux_server(tmp_path: Path) -> None:
    # Window ids are reassigned when the server restarts, so @18 after a
    # restart is a DIFFERENT window — using the binding would animate into
    # someone else's project.
    with patch("vim_ai_follower.cache.CACHE_DIR", tmp_path):
        with patch("vim_ai_follower.binding._tmux_server_pid", return_value=64091):
            binding.remember("sess-abc", "@18")
        with patch("vim_ai_follower.binding._tmux_server_pid", return_value=70001):
            assert binding.recall("sess-abc") is None
        # the stale file must not accumulate across restarts
        assert list((tmp_path / "bindings").iterdir()) == []


def test_recall_is_none_for_an_unknown_session(tmp_path: Path) -> None:
    with patch("vim_ai_follower.cache.CACHE_DIR", tmp_path):
        assert binding.recall("never-seen") is None


def test_a_session_id_with_path_characters_cannot_escape_the_directory(tmp_path: Path) -> None:
    with (
        patch("vim_ai_follower.cache.CACHE_DIR", tmp_path),
        patch("vim_ai_follower.binding._tmux_server_pid", return_value=1),
    ):
        binding.remember("../../etc/passwd", "@1")
        assert list((tmp_path / "bindings").iterdir()) != []
        assert not (tmp_path.parent / "passwd").exists()


def test_remember_writes_nothing_when_the_tmux_server_pid_is_unknown(tmp_path: Path) -> None:
    # A binding with no server pid can never be validated on recall — storing
    # one would either be dead weight or tempt a future recall into treating
    # "no pid" as "matches", the exact wrong-window hole the guard closes.
    with (
        patch("vim_ai_follower.cache.CACHE_DIR", tmp_path),
        patch("vim_ai_follower.binding._tmux_server_pid", return_value=None),
    ):
        binding.remember("sess-no-server", "@1")
    assert not (tmp_path / "bindings").exists()


def test_remember_skips_the_write_when_nothing_changed(tmp_path: Path) -> None:
    with (
        patch("vim_ai_follower.cache.CACHE_DIR", tmp_path),
        patch("vim_ai_follower.binding._tmux_server_pid", return_value=64091),
    ):
        binding.remember("sess-steady", "@18")
        path = next((tmp_path / "bindings").iterdir())
        first_write = path.read_text()
        with patch.object(Path, "write_text", side_effect=AssertionError("must not rewrite")):
            binding.remember("sess-steady", "@18")  # same window, same server: no-op
        assert path.read_text() == first_write


def test_remember_rewrites_when_the_window_changed(tmp_path: Path) -> None:
    with (
        patch("vim_ai_follower.cache.CACHE_DIR", tmp_path),
        patch("vim_ai_follower.binding._tmux_server_pid", return_value=64091),
    ):
        binding.remember("sess-moved", "@18")
        binding.remember("sess-moved", "@19")
        assert binding.recall("sess-moved") == "@19"


def test_remember_swallows_an_os_error_while_writing(tmp_path: Path) -> None:
    with (
        patch("vim_ai_follower.cache.CACHE_DIR", tmp_path),
        patch("vim_ai_follower.binding._tmux_server_pid", return_value=64091),
        patch.object(Path, "write_text", side_effect=OSError("disk full")),
    ):
        binding.remember("sess-broken-write", "@1")  # must not raise
    assert binding.recall("sess-broken-write") is None


def test_recall_returns_none_when_the_tmux_server_pid_cannot_be_measured(
    tmp_path: Path,
) -> None:
    with patch("vim_ai_follower.cache.CACHE_DIR", tmp_path):
        with patch("vim_ai_follower.binding._tmux_server_pid", return_value=64091):
            binding.remember("sess-abc", "@18")
        with patch("vim_ai_follower.binding._tmux_server_pid", return_value=None):
            assert binding.recall("sess-abc") is None
        # unable to prove the server, so the binding is neither used NOR
        # deleted — it might still be valid once tmux answers again.
        assert list((tmp_path / "bindings").iterdir()) != []


def test_recall_swallows_an_os_error_when_deleting_a_stale_binding(tmp_path: Path) -> None:
    with patch("vim_ai_follower.cache.CACHE_DIR", tmp_path):
        with patch("vim_ai_follower.binding._tmux_server_pid", return_value=64091):
            binding.remember("sess-abc", "@18")
        with (
            patch("vim_ai_follower.binding._tmux_server_pid", return_value=70001),
            patch.object(Path, "unlink", side_effect=OSError("permission denied")),
        ):
            assert binding.recall("sess-abc") is None  # must not raise


def test_tmux_server_pid_returns_none_when_tmux_is_missing() -> None:
    with patch("vim_ai_follower.binding.subprocess.run", side_effect=FileNotFoundError("no tmux")):
        assert binding._tmux_server_pid() is None


def test_tmux_server_pid_returns_none_on_a_nonzero_exit() -> None:
    with patch(
        "vim_ai_follower.binding.subprocess.run",
        return_value=MagicMock(returncode=1, stdout=""),
    ):
        assert binding._tmux_server_pid() is None


def test_tmux_server_pid_returns_none_on_unparseable_output() -> None:
    with patch(
        "vim_ai_follower.binding.subprocess.run",
        return_value=MagicMock(returncode=0, stdout="not-a-pid\n"),
    ):
        assert binding._tmux_server_pid() is None


def test_tmux_server_pid_returns_the_pid_on_success() -> None:
    with patch(
        "vim_ai_follower.binding.subprocess.run",
        return_value=MagicMock(returncode=0, stdout="64091\n"),
    ):
        assert binding._tmux_server_pid() == 64091
