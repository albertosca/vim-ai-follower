"""Whether the user of an ADOPTED Vim is typing, read without typing a key
(tmux_vim.typing_mode): its mode file once the script is sourced there, its
screen on a first contact."""

from __future__ import annotations

import os
import signal
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
    screen: str = "a\n~",
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
        if "-S" not in argv:
            return _completed(argv, screen + "\n")
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
    "  101     1   101   202 01:00:00 -zsh\n"
    "  202   101   202   202    00:10 /usr/bin/vim\n"
    "  bad   101   303   303    00:10 vim\n"
    "  404   101   404   404    zz:10 vim\n"
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
        "pid=,ppid=,pgid=,tpgid=,etime=,comm=",
    ]


def test_a_background_vim_or_two_foreground_ones_are_no_answer() -> None:
    background = "  202   101   202   999    00:10 vim\n"
    two = "  202   101   202   202    00:10 vim\n  203   101   202   202    00:10 vim\n"
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


def test_a_mode_file_caught_mid_rename_is_read_from_its_temporary() -> None:
    # Review m11: Vim's rename() deletes the target before it renames, so a
    # read during a mode change found no file (8% of reads under a 1 ms
    # mode flip) and fell back to the screen.
    started = time.time() - 60
    cache.CACHE_DIR.mkdir(parents=True, exist_ok=True)
    (cache.CACHE_DIR / ".mode-202.txt.tmp").write_text("202 i\n")
    assert tmux_vim._recorded_mode(_VimProcess(pid=202, started=started)) == "i"


def test_a_mode_file_renamed_between_the_reads_is_read_again() -> None:
    # Missing, then the temporary already renamed away: the second read of
    # the file itself finds the new mode.
    started = time.time() - 60
    path = cache.CACHE_DIR / "mode-202.txt"
    original = Path.read_text

    def read_text(self: Path, *args: object, **kwargs: object) -> str:
        if self.name == ".mode-202.txt.tmp":
            _mode_file(202, "202 R\n")
            raise FileNotFoundError(self)
        return original(self, *args, **kwargs)  # type: ignore[arg-type]

    assert not path.exists()
    with patch.object(Path, "read_text", read_text):
        assert tmux_vim._recorded_mode(_VimProcess(pid=202, started=started)) == "R"


def test_an_empty_temporary_is_not_a_mode() -> None:
    # A temporary caught while writefile() fills it.
    cache.CACHE_DIR.mkdir(parents=True, exist_ok=True)
    (cache.CACHE_DIR / ".mode-202.txt.tmp").write_text("")
    assert tmux_vim._recorded_mode(_VimProcess(pid=202, started=time.time() - 60)) is None


def _temporary(pid: int, text: str, modified: float | None = None) -> Path:
    cache.CACHE_DIR.mkdir(parents=True, exist_ok=True)
    path = cache.CACHE_DIR / f".mode-{pid}.txt.tmp"
    path.write_text(text)
    if modified is not None:
        os.utime(path, (modified, modified))
    return path


def test_the_file_is_read_before_its_temporary() -> None:
    # The temporary is only the fallback for a file caught mid-rename: a
    # temporary left by a failed rename is older than the file beside it.
    started = time.time() - 60
    _temporary(202, "202 i\n")
    _mode_file(202, "202 n\n")
    assert tmux_vim._recorded_mode(_VimProcess(pid=202, started=started)) == "n"


def test_a_temporary_naming_another_pid_is_none() -> None:
    _temporary(202, "303 i\n")
    assert tmux_vim._recorded_mode(_VimProcess(pid=202, started=time.time() - 60)) is None


def test_a_stale_temporary_under_a_reused_pid_is_none() -> None:
    # A Vim SIGKILLed between its writefile() and its rename() left the
    # temporary; a new Vim got the same pid and has no mode file yet.
    started = time.time() - 60
    _temporary(202, "202 i\n", modified=started - 3600)
    assert tmux_vim._recorded_mode(_VimProcess(pid=202, started=started)) is None


def test_an_empty_temporary_ends_the_reads_even_if_the_file_then_appears() -> None:
    # An empty temporary is a writefile() in progress: the answer is
    # unknown (the screen decides), never the file's next read.
    started = time.time() - 60
    _temporary(202, "")
    original = Path.read_text

    def read_text(self: Path, *args: object, **kwargs: object) -> str:
        if self.name == ".mode-202.txt.tmp":
            content = original(self, *args, **kwargs)  # type: ignore[arg-type]
            _mode_file(202, "202 i\n")
            return content
        return original(self, *args, **kwargs)  # type: ignore[arg-type]

    with patch.object(Path, "read_text", read_text):
        assert tmux_vim._recorded_mode(_VimProcess(pid=202, started=started)) is None


_COMPLETION = "in insert mode (completion)"


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
        ("-- INSERT --recording @a       1,1           All", "in insert mode"),
        # Insert completion and CTRL-X mode (review I1): the rows measured on
        # Vim 9.2 at 49 columns, then the rest of Vim's completion messages.
        ("-- Keyword completion (^N^P) match 1 of 3", _COMPLETION),
        ("-- Keyword completion (^N^P) Back at original", _COMPLETION),
        ("-- Keyword completion (^N^P) Pattern not found", _COMPLETION),
        ("-- ^X mode (^]^D^E^F^I^K^L^N^O^P^Rs^U^V^Y)", _COMPLETION),
        ("-- Whole line completion (^L^N^P) match 1 of 3", _COMPLETION),
        ("-- File name completion (^F^N^P) match 1 of 4", _COMPLETION),
        ("-- Path pattern completion (^N^P) match 1 of 3", _COMPLETION),
        ("-- Keyword Local completion (^N^P) match 1 of 3", _COMPLETION),
        ("-- Pattern not found", _COMPLETION),
        ("-- match 1 of 633", _COMPLETION),
        ("-- Omni completion (^O^N^P) Pattern not found", _COMPLETION),
        ("-- Spelling suggestion (s^N^P) match 1 of 5", _COMPLETION),
        ("-- Dictionary completion (^K^N^P)", _COMPLETION),
        ("-- Thesaurus completion (^T^N^P)", _COMPLETION),
        ("-- Tag completion (^]^N^P)", _COMPLETION),
        ("-- Definition completion (^D^N^P)", _COMPLETION),
        ("-- Command-line completion (^V^N^P)", _COMPLETION),
        ("-- User defined completion (^U^N^P)", _COMPLETION),
        ("-- Adding", _COMPLETION),
        # Measured non-typing rows: the more-prompt (also after a wrong key),
        # the no-lines message (no space after its dashes), a recording.
        ("-- More --", None),
        ("-- More -- SPACE/d/j: screen/page/line down, b/u/", None),
        ("--No lin... buffer--           0,0-1         All", None),
        ("--No lines in buffer--", None),
        ("recording @a                   1,1           All", None),
        (":term", None),
        ("", None),
        ('"f.txt" 3L, 18B', None),
    ],
)
def test_the_bottom_row_tells_a_typing_mode(bottom_row: str, reason: str | None) -> None:
    with patch("vim_ai_follower.backends.tmux_vim.subprocess.run", _world(bottom_row=bottom_row)):
        assert tmux_vim._typing_on_screen("%5") == reason


def _settling(rows: list[str]) -> MagicMock:
    """A pane whose bottom row reads each of `rows` in turn (the last one
    from then on): a Vim still redrawing its screen."""
    remaining = list(rows)

    def run(argv: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        if argv[1] == "display-message":
            return _completed(argv, "20 default\n")
        if "-S" not in argv:
            return _completed(argv, "a\n~\n")
        row = remaining.pop(0) if len(remaining) > 1 else remaining[0]
        return _completed(argv, row + "\n")

    return MagicMock(side_effect=run)


def test_a_filler_bottom_row_is_read_again_until_vim_has_redrawn() -> None:
    # Measured in the integration suite: `~` on the bottom row for 0.6 s
    # before `-- INSERT --` came back; acting on the first read typed into
    # the user's insert mode.
    with (
        patch(
            "vim_ai_follower.backends.tmux_vim.subprocess.run",
            _settling(["~", "~", "-- INSERT --"]),
        ) as run,
        patch("vim_ai_follower.backends.tmux_vim.time.sleep") as sleep,
    ):
        assert tmux_vim._typing_on_screen("%5") == "in insert mode"
    assert sleep.call_args_list == [((tmux_vim._SETTLE_POLL_SECONDS,),)] * 2
    assert [call.args[0][1] for call in run.call_args_list].count("capture-pane") == 3


def test_every_filler_bottom_row_sends_the_vim_a_resize_signal() -> None:
    # Measured 2026-10-17 on Vim 9.2: a Vim with a repeating timer (the
    # test's observer; any plugin's) missed 9-11 of 60 pane resizes for
    # 2.5 s to over 15 s, its screen left at the old size with `~` on the
    # pane's bottom row. A SIGWINCH redrew it in 0.044-0.056 s, but can be
    # missed the same way: 11 missed resizes of 150 took 1, 1, 1, 1, 1, 3, 1,
    # 1, 1, 1 and 2 signals, so one goes with every filler read. A Vim with
    # no timer missed none.
    with (
        patch(
            "vim_ai_follower.backends.tmux_vim.subprocess.run",
            _settling(["~", "~", "-- INSERT --"]),
        ),
        patch("vim_ai_follower.backends.tmux_vim.time.sleep"),
        patch("vim_ai_follower.backends.tmux_vim.os.kill") as kill,
    ):
        assert tmux_vim._typing_on_screen("%5", vim_pid=202) == "in insert mode"
    assert kill.call_args_list == [((202, signal.SIGWINCH),)] * 2


def test_a_settled_bottom_row_sends_no_signal() -> None:
    with (
        patch("vim_ai_follower.backends.tmux_vim.subprocess.run", _settling(["-- INSERT --"])),
        patch("vim_ai_follower.backends.tmux_vim.os.kill") as kill,
    ):
        assert tmux_vim._typing_on_screen("%5", vim_pid=202) == "in insert mode"
    kill.assert_not_called()


def test_a_resize_signal_that_fails_still_re_reads() -> None:
    # No Vim known (pid None): nothing is signalled; a Vim gone meanwhile:
    # the signal's error is swallowed. Both keep re-reading.
    for vim_pid, failure in ((None, None), (202, ProcessLookupError())):
        with (
            patch(
                "vim_ai_follower.backends.tmux_vim.subprocess.run",
                _settling(["~", "-- VISUAL --"]),
            ),
            patch("vim_ai_follower.backends.tmux_vim.time.sleep"),
            patch("vim_ai_follower.backends.tmux_vim.os.kill", side_effect=failure) as kill,
        ):
            assert tmux_vim._typing_on_screen("%5", vim_pid=vim_pid) == "in visual mode"
        assert kill.call_count == (0 if vim_pid is None else 1)


def test_typing_mode_passes_the_foreground_vims_pid_to_the_screen_read() -> None:
    vim = _VimProcess(pid=202, started=time.time() - 60)
    with (
        patch("vim_ai_follower.backends.tmux_vim._foreground_vim", return_value=vim),
        patch("vim_ai_follower.backends.tmux_vim._typing_on_screen", return_value=None) as read,
    ):
        assert tmux_vim.typing_mode("%5") is None
    read.assert_called_once_with("%5", vim_pid=202)


def test_a_filler_bottom_row_that_never_settles_asks() -> None:
    with (
        patch("vim_ai_follower.backends.tmux_vim.subprocess.run", _settling(["~"])),
        patch("vim_ai_follower.backends.tmux_vim.time.sleep"),
        patch(
            "vim_ai_follower.backends.tmux_vim.time.monotonic",
            MagicMock(side_effect=[100.0, 100.0, 101.0, 102.0]),
        ),
    ):
        assert tmux_vim._typing_on_screen("%5") == tmux_vim._UNSETTLED_REASON


def test_a_filler_bottom_row_settling_on_an_empty_row_is_not_typing() -> None:
    with (
        patch("vim_ai_follower.backends.tmux_vim.subprocess.run", _settling(["~", ""])),
        patch("vim_ai_follower.backends.tmux_vim.time.sleep"),
    ):
        assert tmux_vim._typing_on_screen("%5") is None


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


def test_a_typing_mode_file_decides_without_the_screen() -> None:
    _mode_file(202, "202 t\n")
    world = _world(processes="  202   101   202   202    00:10 vim\n", bottom_row="-- INSERT --")
    with patch("vim_ai_follower.backends.tmux_vim.subprocess.run", world):
        assert tmux_vim.typing_mode("%5") == "in terminal mode"
    assert [call.args[0][0] for call in world.call_args_list] == ["tmux", "ps"]


@pytest.mark.parametrize(
    ("bottom_row", "reason"),
    [("-- INSERT --", "in insert mode"), ("", None)],
)
def test_a_non_typing_mode_file_still_reads_the_screen(bottom_row: str, reason: str | None) -> None:
    # Review m9: a file that stopped updating ('eventignore' holding
    # ModeChanged, the user's `:au! vim_ai_follower_mode`) keeps its last
    # `n` while the user types; only a typing mode in it skips the screen.
    _mode_file(202, "202 n\n")
    world = _world(processes="  202   101   202   202    00:10 vim\n", bottom_row=bottom_row)
    with patch("vim_ai_follower.backends.tmux_vim.subprocess.run", world):
        assert tmux_vim.typing_mode("%5") == reason


def test_the_mode_file_is_written_whole_or_not_at_all() -> None:
    # Review m9: writefile() truncates then writes, so a reader could see an
    # empty file; the script writes a temporary in the same directory and
    # renames it over the mode file.
    script = tmux_vim._VIM_MODE
    written = "s:dir . '.mode-' . getpid() . '.txt.tmp'"
    assert f"call writefile([getpid() . ' ' . mode], {written}, 'S')" in script
    assert f"call rename({written}, s:dir . 'mode-' . getpid() . '.txt')" in script
    assert script.count("writefile(") == 1


@pytest.mark.parametrize(
    "processes", ["", "  202   101   202   202    00:10 vim\n"], ids=["no-vim", "no-mode-file"]
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
    dead_temporary = cache.CACHE_DIR / f".mode-{_dead_pid()}.txt.tmp"
    dead_temporary.write_text("x i\n")
    alive_temporary = cache.CACHE_DIR / f".mode-{os.getpid()}.txt.tmp"
    alive_temporary.write_text("x i\n")
    other = cache.CACHE_DIR / "mode-notes.txt"
    other.write_text("")
    with patch("vim_ai_follower.backends.tmux_vim.os.kill", wraps=os.kill) as kill:
        tmux_vim._sweep_scripts(cache.CACHE_DIR / "vaf-000000.vim")
    assert not dead.exists() and not dead_temporary.exists()
    assert alive.exists() and zero.exists() and other.exists() and alive_temporary.exists()
    assert all(call.args[0] > 0 for call in kill.call_args_list)


def test_the_sweep_keeps_the_mode_file_of_another_users_vim() -> None:
    kept = _mode_file(4242, "4242 i\n")
    with patch("vim_ai_follower.backends.tmux_vim.os.kill", side_effect=PermissionError):
        tmux_vim._sweep_scripts(cache.CACHE_DIR / "vaf-000000.vim")
    assert kept.exists()


# --- Ex mode, a confirm, an external command (final review I2) ------------

_VIM_ALONE = "  202   101   202   202    00:10 vim\n"


@pytest.mark.parametrize("mode", ["cv", "ce"])
def test_a_recorded_ex_mode_is_waiting_on_its_user(mode: str) -> None:
    # The Escape pair does nothing in Ex mode: every typed line was joined
    # into one Ex command at the user's prompt.
    _mode_file(202, f"202 {mode}\n")
    with patch("vim_ai_follower.backends.tmux_vim.subprocess.run", _world(processes=_VIM_ALONE)):
        assert tmux_vim.typing_mode("%5") == "in Ex mode"
    assert "in Ex mode" in tmux_vim.WAITING_REASONS


_EX_SCREEN = 'a\nEntering Ex mode.  Type "visual" to go to Normal mode.\n:s/fo'


@pytest.mark.parametrize(
    ("bottom_row", "screen", "reason"),
    [
        ("replace with ZZZ (y/n/a/q/l/^E/^Y)?   1,1  All", "", "a confirm prompt"),
        ("replace with .../a/q/l/^E/^Y)?           1,1           All", "", "a confirm prompt"),
        ("[Y]es, (N)o:", "", "a confirm prompt"),
        ("(S)ave, Do(n)'t save, [C]ancel:", "", "a confirm prompt"),
        (":", "~\n:", "at a ':' prompt (Ex mode or the command line)"),
        (":s/fo", _EX_SCREEN, "in Ex mode"),
        (":", _EX_SCREEN.replace(":s/fo", ":"), "in Ex mode"),
        (":set ts=4                           1,1           All", "~\n:set ts=4", None),
        # Ex mode after a resize: Vim clears the screen and draws nothing.
        ("", "\n\n\n", "a blank screen (as Ex mode leaves it after a resize)"),
        ("", "a\n~\n", None),
        ("Yes, no:", "", None),
    ],
)
def test_the_screen_shows_a_prompt_the_user_is_waiting_at(
    bottom_row: str, screen: str, reason: str | None
) -> None:
    world = _world(processes=_VIM_ALONE, bottom_row=bottom_row, screen=screen)
    with patch("vim_ai_follower.backends.tmux_vim.subprocess.run", world):
        assert tmux_vim.typing_mode("%5") == reason
    if reason is not None:
        assert reason in tmux_vim.WAITING_REASONS


def test_a_failed_full_capture_is_no_ex_mode() -> None:
    def run(argv: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        if argv[0] != "ps" and argv[1] == "capture-pane" and "-S" not in argv:
            return _completed(argv, _EX_SCREEN, returncode=1)
        answered: subprocess.CompletedProcess[str] = _world(
            processes=_VIM_ALONE, bottom_row=":s/fo"
        )(argv, **kwargs)
        return answered

    with patch("vim_ai_follower.backends.tmux_vim.subprocess.run", side_effect=run):
        assert tmux_vim.typing_mode("%5") is None


@pytest.mark.parametrize(
    "processes",
    [
        # `:!make`: the shell Vim forks shares its process group.
        _VIM_ALONE + "  303   202   202   202    00:01 sh\n",
        # `:!vim x`: the inner Vim is the outer one's descendant.
        _VIM_ALONE + "  303   202   202   202    00:01 sh\n  404   303   202   202 00:01 vim\n",
    ],
    ids=["shell", "nested-vim"],
)
def test_a_vim_whose_foreground_group_holds_its_child_runs_an_external_command(
    processes: str,
) -> None:
    # The keys would go to the command's stdin; and no ModeChanged fires.
    _mode_file(202, "202 n\n")
    with patch("vim_ai_follower.backends.tmux_vim.subprocess.run", _world(processes=processes)):
        found = tmux_vim._foreground_vim("%5")
        assert found is not None and found.pid == 202 and found.running_command
        assert tmux_vim.typing_mode("%5") == "running an external command"
    assert "running an external command" in tmux_vim.WAITING_REASONS


def test_a_vim_started_by_another_foreground_program_is_its_own() -> None:
    # `git commit` runs the editor in its own process group: git is Vim's
    # parent, not a command Vim runs.
    processes = "  150   101   150   150    00:20 git\n  202   150   150   150    00:10 vim\n"
    with patch("vim_ai_follower.backends.tmux_vim.subprocess.run", _world(processes=processes)):
        found = tmux_vim._foreground_vim("%5")
        assert found is not None and found.pid == 202 and not found.running_command
        assert tmux_vim.typing_mode("%5") is None


# --- a release a typing stop left pending (final review I5) ---------------


def _release_mark(pid: int, text: str, modified: float | None = None) -> Path:
    cache.CACHE_DIR.mkdir(parents=True, exist_ok=True)
    path = cache.CACHE_DIR / f"release-{pid}.txt"
    path.write_text(text)
    if modified is not None:
        os.utime(path, (modified, modified))
    return path


def test_a_release_mark_names_the_panes_foreground_vim() -> None:
    with patch("vim_ai_follower.backends.tmux_vim.subprocess.run", _world(processes=_VIM_ALONE)):
        assert tmux_vim.mark_release_pending("%5")
    assert (cache.CACHE_DIR / "release-202.txt").read_text() == "202\n"
    assert not list(cache.CACHE_DIR.glob(".release-*"))


def test_no_foreground_vim_records_no_release() -> None:
    with patch("vim_ai_follower.backends.tmux_vim.subprocess.run", _world(processes="")):
        assert not tmux_vim.mark_release_pending("%5")
    assert not list(cache.CACHE_DIR.glob("*release-*"))


def test_a_release_mark_that_cannot_be_written_is_no_release() -> None:
    cache.CACHE_DIR.parent.mkdir(parents=True, exist_ok=True)
    cache.CACHE_DIR.write_text("a file where the directory should be")
    with patch("vim_ai_follower.backends.tmux_vim.subprocess.run", _world(processes=_VIM_ALONE)):
        assert not tmux_vim.mark_release_pending("%5")


def test_the_release_mark_is_taken_once_for_its_own_vim() -> None:
    mark = _release_mark(202, "202\n")
    with patch("vim_ai_follower.backends.tmux_vim.subprocess.run", _world(processes=_VIM_ALONE)):
        assert tmux_vim._take_release_mark("%5")
        assert not mark.exists()
        assert not tmux_vim._take_release_mark("%5")


def test_no_release_mark_anywhere_costs_no_process_lookup() -> None:
    run = _world(processes=_VIM_ALONE)
    with patch("vim_ai_follower.backends.tmux_vim.subprocess.run", run):
        assert not tmux_vim._take_release_mark("%5")
        cache.CACHE_DIR.parent.mkdir(parents=True, exist_ok=True)
        cache.CACHE_DIR.write_text("not a directory")
        assert not tmux_vim._take_release_mark("%5")
    run.assert_not_called()


def test_another_vims_release_mark_is_left_for_it() -> None:
    other = _release_mark(303, "303\n")
    with patch("vim_ai_follower.backends.tmux_vim.subprocess.run", _world(processes=_VIM_ALONE)):
        assert not tmux_vim._take_release_mark("%5")
    with patch("vim_ai_follower.backends.tmux_vim.subprocess.run", _world(processes="")):
        assert not tmux_vim._take_release_mark("%5")
    assert other.exists()


@pytest.mark.parametrize("text", ["303\n", ""])
def test_a_release_mark_naming_another_pid_is_dropped_unused(text: str) -> None:
    mark = _release_mark(202, text)
    with patch("vim_ai_follower.backends.tmux_vim.subprocess.run", _world(processes=_VIM_ALONE)):
        assert not tmux_vim._take_release_mark("%5")
    assert not mark.exists()


def test_a_release_mark_older_than_its_vim_was_a_dead_vims() -> None:
    # The Vim started ten seconds ago (ps etime 00:10): a mark from before
    # that was left by a Vim that had the same pid.
    mark = _release_mark(202, "202\n", modified=time.time() - 60)
    with patch("vim_ai_follower.backends.tmux_vim.subprocess.run", _world(processes=_VIM_ALONE)):
        assert not tmux_vim._take_release_mark("%5")
    assert not mark.exists()


def test_the_sweep_deletes_only_the_release_marks_of_gone_vims() -> None:
    dead = _release_mark(_dead_pid(), "x\n")
    alive = _release_mark(os.getpid(), "x\n", modified=time.time() - 10 * 86400)
    dead_temporary = cache.CACHE_DIR / f".release-{_dead_pid()}.txt.tmp"
    dead_temporary.write_text("x\n")
    tmux_vim._sweep_scripts(cache.CACHE_DIR / "vaf-000000.vim")
    assert not dead.exists() and not dead_temporary.exists()
    assert alive.exists()


def test_a_release_mark_cut_off_between_listing_and_reading_is_no_release() -> None:
    _release_mark(202, "202\n")
    real_read = Path.read_text

    def vanished(path: Path, *arguments: object, **keywords: object) -> str:
        if path.name == "release-202.txt":
            raise FileNotFoundError(path)
        return real_read(path, *arguments, **keywords)  # type: ignore[arg-type]

    with (
        patch("vim_ai_follower.backends.tmux_vim.subprocess.run", _world(processes=_VIM_ALONE)),
        patch.object(Path, "read_text", vanished),
    ):
        assert not tmux_vim._take_release_mark("%5")
