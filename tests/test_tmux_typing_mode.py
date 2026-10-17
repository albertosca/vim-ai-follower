"""Whether the user of an ADOPTED Vim is typing, read without typing a key
(tmux_vim.typing_mode): its mode file once the script is sourced there, its
screen on a first contact."""

from __future__ import annotations

import os
import subprocess
import time
from collections.abc import Callable
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from vim_ai_follower import cache
from vim_ai_follower.backends import tmux_vim
from vim_ai_follower.backends.tmux_vim import _VimProcess

_Answer = Callable[[list[str]], "subprocess.CompletedProcess[str]"]


def _completed(
    argv: list[str], stdout: str, returncode: int = 0
) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(argv, returncode, stdout, "")


def _world(
    *,
    terminal: str = "/dev/ttys004",
    processes: str = "",
    height_and_cursor: str = "20 default",
    bottom_row: str = "",
    capture_code: int = 0,
) -> MagicMock:
    """A subprocess.run stand-in for the pane %5: its tty, the processes on
    it (ps), its height and cursor, and its bottom row."""

    def run(argv: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        if argv[0] == "ps":
            return _completed(argv, processes)
        if argv[1] == "display-message" and argv[-1] == "#{pane_tty}":
            return _completed(argv, terminal + "\n")
        if argv[1] == "display-message":
            return _completed(argv, height_and_cursor + "\n")
        assert argv[1] == "capture-pane", argv
        assert argv[-4:] == ["-S", "19", "-E", "19"], argv
        return _completed(argv, bottom_row + "\n", capture_code)

    return MagicMock(side_effect=run)


@pytest.mark.parametrize(
    ("etime", "seconds"),
    [("07", None), ("01:02", 62), ("1:01:02", 3662), ("2-01:00:00", 176400), ("x:1", None)],
)
def test_elapsed_seconds_reads_ps_etime(etime: str, seconds: int | None) -> None:
    assert tmux_vim._elapsed_seconds(etime) == seconds


def test_elapsed_seconds_refuses_too_many_fields() -> None:
    assert tmux_vim._elapsed_seconds("1:2:3:4") is None


_SHELL_AND_VIM = (
    "  101   101   202 01:00:00 -zsh\n"
    "  202   202   202    00:10 /usr/bin/vim\n"
    "  bad   303   303    00:10 vim\n"
    "  404   404   404    zz:10 vim\n"
    "  short\n"
)


def test_the_foreground_vim_is_the_one_vim_in_the_terminals_group() -> None:
    with patch(
        "vim_ai_follower.backends.tmux_vim.subprocess.run", _world(processes=_SHELL_AND_VIM)
    ) as run:
        before = time.time()
        found = tmux_vim._foreground_vim("%5")
    assert found is not None and found.pid == 202
    assert before - 11 <= found.started <= time.time() - 10
    assert run.call_args_list[1].args[0] == [
        "ps",
        "-t",
        "ttys004",
        "-o",
        "pid=,pgid=,tpgid=,etime=,comm=",
    ]


def test_a_background_vim_or_two_foreground_ones_are_no_answer() -> None:
    background = "  202   202   999    00:10 vim\n"
    two = "  202   202   202    00:10 vim\n  203   202   202    00:10 vim\n"
    for processes in (background, two):
        with patch("vim_ai_follower.backends.tmux_vim.subprocess.run", _world(processes=processes)):
            assert tmux_vim._foreground_vim("%5") is None


def test_no_terminal_is_no_answer_and_runs_no_ps() -> None:
    with patch("vim_ai_follower.backends.tmux_vim.subprocess.run", _world(terminal="")) as run:
        assert tmux_vim._foreground_vim("%5") is None
    assert len(run.call_args_list) == 1


def test_a_failed_display_message_is_no_terminal() -> None:
    with patch(
        "vim_ai_follower.backends.tmux_vim.subprocess.run",
        return_value=_completed([], "/dev/ttys004\n", returncode=1),
    ):
        assert tmux_vim._tmux_answer("%5", "#{pane_tty}") == ""


@pytest.mark.parametrize("failure", [OSError("no ps"), subprocess.TimeoutExpired("ps", 2)])
def test_a_ps_that_fails_is_no_answer(failure: Exception) -> None:
    def run(argv: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        if argv[0] == "ps":
            raise failure
        return _completed(argv, "/dev/ttys004\n")

    with patch("vim_ai_follower.backends.tmux_vim.subprocess.run", side_effect=run):
        assert tmux_vim._foreground_vim("%5") is None


def _mode_file(pid: int, text: str, modified: float | None = None) -> Path:
    cache.CACHE_DIR.mkdir(parents=True, exist_ok=True)
    path = cache.CACHE_DIR / f"mode-{pid}.txt"
    path.write_text(text)
    if modified is not None:
        os.utime(path, (modified, modified))
    return path


def test_the_recorded_mode_is_the_files_for_this_vim() -> None:
    started = time.time() - 60
    _mode_file(202, "202 niI\n")
    assert tmux_vim._recorded_mode(_VimProcess(pid=202, started=started)) == "niI"


def test_a_mode_file_written_before_the_vim_started_is_another_processes() -> None:
    started = time.time() - 60
    _mode_file(202, "202 i\n", modified=started - 3)
    assert tmux_vim._recorded_mode(_VimProcess(pid=202, started=started)) is None
    # Within ps's one-second etime rounding it is this Vim's.
    _mode_file(202, "202 i\n", modified=started - 1)
    assert tmux_vim._recorded_mode(_VimProcess(pid=202, started=started)) == "i"


@pytest.mark.parametrize("text", ["303 i\n", "202\n", ""])
def test_a_mode_file_naming_another_pid_or_no_mode_is_none(text: str) -> None:
    _mode_file(202, text)
    assert tmux_vim._recorded_mode(_VimProcess(pid=202, started=time.time() - 60)) is None


def test_no_mode_file_is_none() -> None:
    assert tmux_vim._recorded_mode(_VimProcess(pid=202, started=time.time() - 60)) is None


@pytest.mark.parametrize(
    ("bottom_row", "reason"),
    [
        ("-- INSERT --                   0,1           All", "in insert mode"),
        ("-- INSERT (paste) --", "in insert mode"),
        ("-- (insert) --", "in insert mode"),
        ("-- (insert) VISUAL --", "in visual mode"),
        ("-- VISUAL LINE --    1         0,0-1         All", "in visual mode"),
        ("-- VISUAL BLOCK --", "in visual mode"),
        ("-- SELECT --", "in select mode"),
        ("-- REPLACE --", "in replace mode"),
        ("-- VREPLACE --", "in replace mode"),
        ("-- (replace) --", "in replace mode"),
        ("-- (vreplace) --", "in replace mode"),
        ("-- TERMINAL --", "in terminal mode"),
        ("-- More --", None),
        ("-- No lines in buffer --", None),
        (":term", None),
        ("", None),
        ('"f.txt" 3L, 18B', None),
    ],
)
def test_the_bottom_row_tells_a_typing_mode(bottom_row: str, reason: str | None) -> None:
    with patch("vim_ai_follower.backends.tmux_vim.subprocess.run", _world(bottom_row=bottom_row)):
        assert tmux_vim._typing_on_screen("%5") == reason


@pytest.mark.parametrize(
    ("cursor", "reason"),
    [
        ("bar", "in insert mode (a bar cursor)"),
        ("underline", "in replace mode (an underline cursor)"),
        ("block", None),
        ("default", None),
        ("", None),
    ],
)
def test_a_typing_cursor_tells_a_typing_mode(cursor: str, reason: str | None) -> None:
    with patch(
        "vim_ai_follower.backends.tmux_vim.subprocess.run",
        _world(height_and_cursor=f"20 {cursor}"),
    ):
        assert tmux_vim._typing_on_screen("%5") == reason


def test_a_bottom_row_that_cannot_be_read_falls_back_to_the_cursor() -> None:
    with patch(
        "vim_ai_follower.backends.tmux_vim.subprocess.run",
        _world(height_and_cursor="20 bar", bottom_row="-- VISUAL --", capture_code=1),
    ):
        assert tmux_vim._typing_on_screen("%5") == "in insert mode (a bar cursor)"
    # No height: no row is read at all.
    with patch(
        "vim_ai_follower.backends.tmux_vim.subprocess.run", _world(height_and_cursor="")
    ) as run:
        assert tmux_vim._typing_on_screen("%5") is None
    assert len(run.call_args_list) == 1


def test_the_mode_file_decides_over_the_screen() -> None:
    _mode_file(202, "202 n\n")
    world = _world(processes="  202   202   202    00:10 vim\n", bottom_row="-- INSERT --")
    with patch("vim_ai_follower.backends.tmux_vim.subprocess.run", world):
        assert tmux_vim.typing_mode("%5") is None
    _mode_file(202, "202 t\n")
    with patch("vim_ai_follower.backends.tmux_vim.subprocess.run", world):
        assert tmux_vim.typing_mode("%5") == "in terminal mode"


@pytest.mark.parametrize(
    "processes", ["", "  202   202   202    00:10 vim\n"], ids=["no-vim", "no-mode-file"]
)
def test_a_first_contact_reads_the_screen(processes: str) -> None:
    world = _world(processes=processes, bottom_row="-- SELECT --")
    with patch("vim_ai_follower.backends.tmux_vim.subprocess.run", world):
        assert tmux_vim.typing_mode("%5") == "in select mode"


def test_leaving_the_typing_mode_sends_ctrl_backslash_ctrl_n_as_bytes() -> None:
    with patch("vim_ai_follower.tmux.subprocess.run") as run:
        tmux_vim.leave_typing_mode("%5")
    run.assert_called_once_with(["tmux", "send-keys", "-t", "%5", "-H", "1c", "0e"], check=True)


def _dead_pid() -> int:
    child = subprocess.Popen(["true"])
    child.wait()
    return child.pid


def test_the_sweep_deletes_only_the_mode_files_of_gone_vims() -> None:
    dead = _mode_file(_dead_pid(), "x n\n", modified=time.time())
    alive = _mode_file(os.getpid(), "x i\n", modified=time.time() - 10 * 86400)
    zero = _mode_file(0, "0 n\n")
    other = cache.CACHE_DIR / "mode-notes.txt"
    other.write_text("")
    with patch("vim_ai_follower.backends.tmux_vim.os.kill", wraps=os.kill) as kill:
        tmux_vim._sweep_scripts(cache.CACHE_DIR / "vaf-000000.vim")
    assert not dead.exists()
    assert alive.exists() and zero.exists() and other.exists()
    assert all(call.args[0] > 0 for call in kill.call_args_list)


def test_the_sweep_keeps_the_mode_file_of_another_users_vim() -> None:
    kept = _mode_file(4242, "4242 i\n")
    with patch("vim_ai_follower.backends.tmux_vim.os.kill", side_effect=PermissionError):
        tmux_vim._sweep_scripts(cache.CACHE_DIR / "vaf-000000.vim")
    assert kept.exists()
