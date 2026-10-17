from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

from vim_ai_follower.state import FollowerState, nvim_socket_path, touch_open_files


def test_nvim_socket_path_is_window_scoped(tmp_path: Path) -> None:
    assert nvim_socket_path("@3", tmp_path) == tmp_path / "nvim-@3.sock"


def test_nvim_socket_path_sanitizes_colons_for_nvim_listen(tmp_path: Path) -> None:
    # nvim's --listen treats ANY colon in the address as a TCP host:port
    # (parsed via getaddrinfo), so a raw iTerm TERM_SESSION_ID like
    # "term-w0t0p0:404B7BFA-65CF" must not reach the filename with its
    # colon intact — confirmed live 2026-09-03 ("unknown node or service"),
    # confirmed in isolation 2026-09-04 (nvim --listen against a plain-name
    # socket succeeds; the identical path with a colon reproduces the
    # exact same error verbatim).
    result = nvim_socket_path("term-w0t0p0:404B7BFA-65CF", tmp_path)
    assert result == tmp_path / "nvim-term-w0t0p0_404B7BFA-65CF.sock"


def test_set_writes_the_pane_atomically_via_rename(tmp_path: Path) -> None:
    # Atomicity is the property under test: the .pane is replaced by an
    # os-level rename of a fully-written temp file, never opened for an
    # in-place truncating write. That is what lets a concurrent reader
    # (another window's hook, in the now multi-follower world) see the old
    # state or the new one whole, never a half-written file.
    target = tmp_path / "@1.pane"
    observed = {"target_written_directly": False, "renamed_onto_target": False}
    real_write_text = Path.write_text
    real_replace = Path.replace

    def spy_write_text(self: Path, *args: object, **kwargs: object) -> int:
        if self == target:
            observed["target_written_directly"] = True
        return real_write_text(self, *args, **kwargs)  # type: ignore[arg-type]

    def spy_replace(self: Path, dst: object, *args: object, **kwargs: object) -> Path:
        if Path(dst) == target:  # type: ignore[arg-type]
            observed["renamed_onto_target"] = True
        return real_replace(self, dst, *args, **kwargs)  # type: ignore[arg-type]

    with (
        patch("vim_ai_follower.tmux.subprocess.run", return_value=MagicMock(stdout="%5 vim\n")),
        patch.object(Path, "write_text", spy_write_text),
        patch.object(Path, "replace", spy_replace),
    ):
        FollowerState.set("@1", "tmux", "%5", base_dir=tmp_path)
    assert observed["renamed_onto_target"] is True  # atomic rename used
    assert observed["target_written_directly"] is False  # target never truncated in place
    assert FollowerState.read("@1", base_dir=tmp_path) is not None  # and it round-trips


def test_set_does_not_leave_a_temp_file_behind(tmp_path: Path) -> None:
    with patch("vim_ai_follower.tmux.subprocess.run", return_value=MagicMock(stdout="%5 vim\n")):
        FollowerState.set("@1", "tmux", "%5", base_dir=tmp_path)
    # Only the final .pane remains; the temp file was renamed onto it. A
    # lingering temp must also never be caught by the *.pane orphan scan.
    assert [p.name for p in tmp_path.iterdir()] == ["@1.pane"]


def test_get_returns_none_when_no_state_file(tmp_path: Path) -> None:
    assert FollowerState.get("@1", base_dir=tmp_path) is None


def test_set_then_get_round_trips(tmp_path: Path) -> None:
    with patch("vim_ai_follower.tmux.subprocess.run", return_value=MagicMock(stdout="%5 vim\n")):
        FollowerState.set("@1", "tmux", "%5", current_file="/tmp/a.txt", base_dir=tmp_path)
        result = FollowerState.get("@1", base_dir=tmp_path)
    assert result is not None
    assert result.backend == "tmux"
    assert result.target == "%5"
    assert result.current_file == "/tmp/a.txt"


def test_set_defaults_origin_on_failure_and_speed(tmp_path: Path) -> None:
    with patch("vim_ai_follower.tmux.subprocess.run", return_value=MagicMock(stdout="%5 vim\n")):
        FollowerState.set("@1", "tmux", "%5", base_dir=tmp_path)
        result = FollowerState.get("@1", base_dir=tmp_path)
    assert result is not None
    assert result.origin == ""
    assert result.on_failure == "silent"
    assert result.speed == "rapido"


def test_set_stores_origin_on_failure_and_speed(tmp_path: Path) -> None:
    with patch("vim_ai_follower.tmux.subprocess.run", return_value=MagicMock(stdout="%5 vim\n")):
        FollowerState.set(
            "@1",
            "tmux",
            "%5",
            origin="%1",
            on_failure="reopen",
            speed="lento",
            base_dir=tmp_path,
        )
        result = FollowerState.get("@1", base_dir=tmp_path)
    assert result is not None
    assert result.origin == "%1"
    assert result.on_failure == "reopen"
    assert result.speed == "lento"


def test_read_returns_state_even_when_pane_is_dead(tmp_path: Path) -> None:
    with patch("vim_ai_follower.tmux.subprocess.run", return_value=MagicMock(stdout="%5 vim\n")):
        FollowerState.set("@1", "tmux", "%5", origin="%1", base_dir=tmp_path)
    with patch("vim_ai_follower.tmux.subprocess.run", return_value=MagicMock(stdout="")):
        result = FollowerState.read("@1", base_dir=tmp_path)
    assert result is not None
    assert result.target == "%5"
    assert result.origin == "%1"


def test_read_returns_none_without_state_file(tmp_path: Path) -> None:
    assert FollowerState.read("@1", base_dir=tmp_path) is None


def test_get_returns_none_when_pane_is_dead(tmp_path: Path) -> None:
    with patch("vim_ai_follower.tmux.subprocess.run", return_value=MagicMock(stdout="%5 vim\n")):
        FollowerState.set("@1", "tmux", "%5", base_dir=tmp_path)
    with patch("vim_ai_follower.tmux.subprocess.run", return_value=MagicMock(stdout="")):
        assert FollowerState.get("@1", base_dir=tmp_path) is None


def test_update_current_file_preserves_target(tmp_path: Path) -> None:
    with patch("vim_ai_follower.tmux.subprocess.run", return_value=MagicMock(stdout="%5 vim\n")):
        FollowerState.set("@1", "tmux", "%5", base_dir=tmp_path)
        FollowerState.update_current_file("@1", "/tmp/b.txt", base_dir=tmp_path)
        result = FollowerState.get("@1", base_dir=tmp_path)
    assert result is not None
    assert result.current_file == "/tmp/b.txt"
    assert result.target == "%5"


def test_update_current_file_is_a_noop_without_existing_state(tmp_path: Path) -> None:
    FollowerState.update_current_file("@1", "/tmp/b.txt", base_dir=tmp_path)
    assert FollowerState.get("@1", base_dir=tmp_path) is None


def test_clear_removes_state(tmp_path: Path) -> None:
    with patch("vim_ai_follower.tmux.subprocess.run", return_value=MagicMock(stdout="%5 vim\n")):
        FollowerState.set("@1", "tmux", "%5", base_dir=tmp_path)
        FollowerState.clear("@1", base_dir=tmp_path)
        assert FollowerState.get("@1", base_dir=tmp_path) is None


def test_clear_without_existing_state_does_not_raise(tmp_path: Path) -> None:
    FollowerState.clear("@1", base_dir=tmp_path)


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
    assert (raw.open_files, raw.enabled, raw.shown_any) == ((), True, False)


def test_a_state_file_without_adopted_reads_as_adopted(tmp_path: Path) -> None:
    # Missing means unknown, and unknown must not lock a buffer or press keys:
    # every reader of `adopted` takes the user's-editor side when it is True.
    (tmp_path / "$9.pane").write_text('{"backend": "nvim", "target": "/tmp/x.sock"}')
    raw = FollowerState.read("$9", base_dir=tmp_path)
    assert raw is not None
    assert raw.adopted is True


def test_update_replaces_only_given_fields(tmp_path: Path) -> None:
    FollowerState.set("$9", "tmux", "%5", speed="normal", base_dir=tmp_path)
    FollowerState.update("$9", base_dir=tmp_path, speed="lento", enabled=False)
    raw = FollowerState.read("$9", base_dir=tmp_path)
    assert raw is not None
    assert (raw.speed, raw.enabled, raw.target) == ("lento", False, "%5")


def test_update_without_state_is_a_no_op(tmp_path: Path) -> None:
    FollowerState.update("$9", base_dir=tmp_path, speed="lento")
    assert FollowerState.read("$9", base_dir=tmp_path) is None


def test_writers_and_labels_round_trip(tmp_path: Path) -> None:
    with patch("vim_ai_follower.tmux.subprocess.run", return_value=MagicMock(stdout="%5 vim\n")):
        FollowerState.set(
            "@1",
            "tmux",
            "%5",
            writers=("$1", "a9"),
            writer_labels=("session:aaa111", "code-reviewer"),
            base_dir=tmp_path,
        )
        result = FollowerState.get("@1", base_dir=tmp_path)
    assert result is not None
    assert result.writers == ("$1", "a9")
    assert result.writer_labels == ("session:aaa111", "code-reviewer")


def test_writers_default_empty_and_update_preserves_them(tmp_path: Path) -> None:
    with patch("vim_ai_follower.tmux.subprocess.run", return_value=MagicMock(stdout="%5 vim\n")):
        FollowerState.set("@1", "tmux", "%5", base_dir=tmp_path)
        assert FollowerState.read("@1", base_dir=tmp_path).writers == ()  # type: ignore[union-attr]
        FollowerState.update("@1", base_dir=tmp_path, writers=("$1",), writer_labels=("session:x",))
        FollowerState.update("@1", base_dir=tmp_path, speed="lento")  # unrelated change
        result = FollowerState.read("@1", base_dir=tmp_path)
    assert result is not None
    assert result.writers == ("$1",)
    assert result.writer_labels == ("session:x",)
    assert result.speed == "lento"


def test_touch_open_files_recency_and_eviction() -> None:
    assert touch_open_files((), "/a.py", 5) == (("/a.py",), ())
    assert touch_open_files(("/a.py", "/b.py"), "/a.py", 5) == (("/b.py", "/a.py"), ())
    assert touch_open_files(("/a.py", "/b.py", "/c.py"), "/d.py", 3) == (
        ("/b.py", "/c.py", "/d.py"),
        ("/a.py",),
    )
    assert touch_open_files(("/a.py",), "/b.py", 1) == (("/b.py",), ("/a.py",))


# --- the stop tombstone: a stopped window stays stopped until `start` -------


def _set_follower(window_id: str) -> None:
    FollowerState.set(window_id, "nvim", "/tmp/nvim-x.sock", origin="%1", adopted=False)


def test_mark_stopped_records_the_tmux_server_pid() -> None:
    from vim_ai_follower import state

    state.mark_stopped("@4", 64091)
    assert state.stopped_path("@4").read_text() == "64091"
    assert state.is_stopped("@4", 64091) is True


def test_a_tombstone_from_another_tmux_server_does_not_count() -> None:
    # Window ids are reused across tmux restarts: @4 today is not the @4 a
    # stop under the previous server silenced.
    from vim_ai_follower import state

    state.mark_stopped("@4", 64091)
    assert state.is_stopped("@4", 70001) is False
    assert state.is_stopped("@4", None) is False


def test_a_standalone_tombstone_holds_whatever_the_server() -> None:
    from vim_ai_follower import state

    state.mark_stopped("term-x", None)
    assert state.stopped_path("term-x").read_text() == ""
    assert state.is_stopped("term-x", None) is True
    assert state.is_stopped("term-x", 64091) is True


def test_no_tombstone_is_not_stopped() -> None:
    from vim_ai_follower import state

    assert state.is_stopped("@4", 64091) is False


def test_clear_stopped_removes_the_tombstone_and_tolerates_none() -> None:
    from vim_ai_follower import state

    state.clear_stopped("@4")
    state.mark_stopped("@4", 64091)
    state.clear_stopped("@4")
    assert not state.stopped_path("@4").exists()


def test_set_and_update_write_nothing_under_a_tombstone_of_this_server() -> None:
    from vim_ai_follower import state

    _set_follower("@4")
    held = FollowerState.read("@4")
    assert held is not None
    FollowerState.clear("@4")
    state.mark_stopped("@4", 64091)
    with patch("vim_ai_follower.binding.tmux_server_pid", return_value=64091):
        _set_follower("@4")
        # A hook that read the state before the stop: update's own read
        # comes back with that state (the stop landed between its read and
        # its write).
        with patch.object(FollowerState, "read", return_value=held):
            FollowerState.update("@4", current_file="/tmp/a.py")
    assert not state._state_path("@4", None).exists()


def test_set_writes_again_under_a_tombstone_of_another_server() -> None:
    from vim_ai_follower import state

    state.mark_stopped("@4", 64091)
    with patch("vim_ai_follower.binding.tmux_server_pid", return_value=70001):
        _set_follower("@4")
    assert FollowerState.read("@4") is not None


def test_set_never_asks_tmux_when_there_is_no_tombstone() -> None:
    with patch("vim_ai_follower.binding.tmux_server_pid") as server_pid:
        _set_follower("@4")
    server_pid.assert_not_called()
    assert FollowerState.read("@4") is not None
