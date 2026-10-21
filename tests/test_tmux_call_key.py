"""The call key (T30): a Vim that wrote its ready file is sent each call as
<F37> + the 8 digits of a call file + <F37>, which a `<silent>` mapping
reads, so nothing reaches its command line; any other Vim is typed the
call, as before."""

from __future__ import annotations

import hashlib
import os
import subprocess
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from vim_ai_follower import cache
from vim_ai_follower.backends import tmux_vim
from vim_ai_follower.tmux import TmuxPane

pytestmark = pytest.mark.real_call_key

_SERVER = 4321


@pytest.fixture(autouse=True)
def _known_server() -> object:
    with patch.object(tmux_vim, "_server_pid", return_value=_SERVER):
        yield


def _dead_pid() -> int:
    child = subprocess.Popen(["true"])
    child.wait()
    return child.pid


def _ready_file(pid: int, name: str | None = None, pane: str = "%5") -> Path:
    dispatcher = name if name is not None else tmux_vim._vim_functions()[0]
    path = tmux_vim._ready_path(pane, _SERVER)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"{dispatcher} {pid}\n")
    return path


def _call_files() -> list[Path]:
    return sorted(
        path
        for path in cache.CACHE_DIRECTORY.glob("k*")
        if tmux_vim._CALL_FILE_NAME.fullmatch(path.name)
    )


def test_a_ready_vim_is_sent_the_key_and_the_call_goes_by_file() -> None:
    _ready_file(os.getpid())
    pane = MagicMock(pane_id="%5")
    tmux_vim._send(pane, tmux_vim._call_line("goto", "p5-abcdef"))
    pane.send_line.assert_not_called()
    (sent,) = pane.send_hex.call_args_list
    key = list(tmux_vim._CALL_KEY)
    assert list(sent.args[:7]) == key and list(sent.args[-7:]) == key
    digits = "".join(chr(int(byte, 16)) for byte in sent.args[7:-7])
    (call_file,) = _call_files()
    assert call_file.name == f"k{digits}"
    assert call_file.read_text() == "['goto','p5-abcdef']"


def test_a_vim_not_ready_is_typed_the_call() -> None:
    pane = MagicMock(pane_id="%5")
    line = tmux_vim._call_line("goto", "p5-abcdef")
    tmux_vim._send(pane, line)
    pane.send_line.assert_called_once_with(line)
    pane.send_hex.assert_not_called()
    assert _call_files() == []


def test_a_plain_line_goes_as_the_ex_call() -> None:
    assert tmux_vim._call_expression(":exe 12") == "['ex','exe 12']"
    assert tmux_vim._call_expression(":it's") == "['ex','it''s']"


def test_a_ready_file_counts_only_for_this_script_and_a_live_pid() -> None:
    path = _ready_file(os.getpid())
    assert tmux_vim._vim_ready("%5")
    _ready_file(os.getpid(), name="VafFollower_000000")
    assert not tmux_vim._vim_ready("%5")
    path.write_text("garbage")
    assert not tmux_vim._vim_ready("%5")
    _ready_file(_dead_pid())
    assert not tmux_vim._vim_ready("%5")
    _ready_file(0)
    assert not tmux_vim._vim_ready("%5")
    assert not tmux_vim._vim_ready("%6")


def test_a_ready_file_naming_another_users_live_process_counts() -> None:
    """kill(pid, 0) answering EPERM is a live process of another user, as
    the mode-file sweep reads it: the Vim is ready, not gone."""
    _ready_file(os.getpid())
    with patch(
        "vim_ai_follower.backends.tmux_vim.os.kill", side_effect=PermissionError("not yours")
    ):
        assert tmux_vim._vim_ready("%5")


def test_the_ready_file_of_another_server_does_not_count() -> None:
    _ready_file(os.getpid())
    with patch.object(tmux_vim, "_server_pid", return_value=_SERVER + 1):
        assert not tmux_vim._vim_ready("%5")


def test_the_define_guard_asks_for_what_the_calls_need() -> None:
    name = tmux_vim._vim_functions()[0]
    with patch.object(tmux_vim, "_home_relative", return_value="~/x.vim"):
        typed = tmux_vim._define_line("%5")
        _ready_file(os.getpid())
        keyed = tmux_vim._define_line("%5")
    assert typed.startswith(f":if !exists('*{name}')|")
    assert keyed.startswith(f":if maparg('<F37>')!~#'{name.removeprefix('VafFollower_')}'|")


def test_a_call_file_no_vim_read_is_swept() -> None:
    cache.CACHE_DIRECTORY.mkdir(parents=True, exist_ok=True)
    old = cache.CACHE_DIRECTORY / "k12345678"
    old.write_text("[]")
    stale = time.time() - tmux_vim._PATH_HANDLE_MAX_AGE_SECONDS - 5
    os.utime(old, (stale, stale))
    recent = cache.CACHE_DIRECTORY / "k87654321"
    recent.write_text("[]")
    other = cache.CACHE_DIRECTORY / "keep-me"
    other.write_text("")
    os.utime(other, (stale, stale))
    digits = tmux_vim._call_file("['ex','x']")
    assert not old.exists() and recent.exists() and other.exists()
    assert (cache.CACHE_DIRECTORY / f"k{digits}").read_text() == "['ex','x']"


def test_two_calls_never_share_a_file() -> None:
    with patch("vim_ai_follower.backends.tmux_vim.secrets.randbelow", side_effect=[7, 7, 8]):
        first = tmux_vim._call_file("['a']")
        second = tmux_vim._call_file("['b']")
    assert (first, second) == ("00000007", "00000008")


def test_the_ready_wait_ends_when_the_file_appears_or_gives_up() -> None:
    with patch.object(tmux_vim, "_READY_WAIT_SECONDS", 0.05):
        started = time.monotonic()
        tmux_vim._await_ready("%5")
        assert time.monotonic() - started >= 0.05
        _ready_file(os.getpid())
        started = time.monotonic()
        tmux_vim._await_ready("%5")
        assert time.monotonic() - started < 0.05


def test_a_vim_that_never_got_ready_is_waited_for_once_per_pane_process() -> None:
    # A Vim that can never write its ready file (no TMUX_PANE, no +eval) is
    # typed the define on every contact; waiting the full second each time
    # cost every Edit two seconds (final review of backlog-sweep-5, I1).
    def elapsed(pane_pid: int | None) -> float:
        with (
            patch.object(tmux_vim, "_READY_WAIT_SECONDS", 0.2),
            patch.object(TmuxPane, "pane_pid", return_value=pane_pid),
        ):
            started = time.monotonic()
            tmux_vim._await_ready("%5")
            return time.monotonic() - started

    assert elapsed(111) >= 0.2
    assert elapsed(111) < 0.1
    assert elapsed(222) >= 0.2  # another process in the pane: a new Vim
    assert elapsed(None) >= 0.2  # tmux cannot say: waited, as before
    assert elapsed(None) >= 0.2


def test_the_sweep_deletes_only_the_ready_files_of_gone_vims() -> None:
    dead = _ready_file(_dead_pid(), pane="%7")
    # An older plugin's dispatcher: still swept by its pid.
    dead_old = _ready_file(_dead_pid(), name="VafFollower_000000", pane="%8")
    alive = _ready_file(os.getpid(), pane="%9")
    long_ago = time.time() - 10 * 86400
    os.utime(alive, (long_ago, long_ago))
    unreadable = cache.CACHE_DIRECTORY / "ready-1-2"
    unreadable.write_text("nonsense")
    zero = cache.CACHE_DIRECTORY / "ready-3"
    zero.write_text("VafFollower_000000 0\n")
    undecodable = cache.CACHE_DIRECTORY / "ready-5-6"
    undecodable.write_bytes(b"\xff\xfe 1\n")
    old_record = cache.CACHE_DIRECTORY / "keyed-4321-7"
    old_record.write_text("12345678")
    os.utime(old_record, (long_ago, long_ago))
    recent_record = cache.CACHE_DIRECTORY / "keyed-4321-9"
    recent_record.write_text("12345678")
    with patch("vim_ai_follower.backends.tmux_vim.os.kill", wraps=os.kill) as kill:
        tmux_vim._sweep_scripts(cache.CACHE_DIRECTORY / "vaf-000000.vim")
    assert not old_record.exists() and recent_record.exists()
    assert not dead.exists() and not dead_old.exists()
    assert alive.exists() and unreadable.exists() and zero.exists() and undecodable.exists()
    assert all(call.args[0] > 0 for call in kill.call_args_list)


def _defined(follower: tmux_vim.TmuxVimFollower) -> MagicMock:
    pane = MagicMock(pane_id="%5")
    # The Vim shares the hook's HOME, so the define types `~/.cache/…`: typed
    # as the test's absolute tmp path, the line's form followed that path's
    # length (a CI path of 80-88 characters dropped the guard, by design).
    with (
        patch.object(
            tmux_vim,
            "_home_relative",
            side_effect=lambda pane_id, path: f"~/.cache/claude-vim-follower/{path.name}",
        ),
        patch.object(tmux_vim, "_take_release_mark", return_value=False),
        patch.object(tmux_vim, "_take_pending_evictions", return_value=[]),
        patch.object(tmux_vim, "_READY_WAIT_SECONDS", 0.0),
    ):
        follower._define(pane)
    return pane


def test_a_ready_vim_is_not_typed_the_define_line() -> None:
    # The define runs once per Vim: typed on every entry point, it was
    # still the line on the command line (115 echoes in 23 rounds).
    _ready_file(os.getpid())
    pane = _defined(tmux_vim.TmuxVimFollower(pane_id="%5"))
    pane.send_line.assert_not_called()
    pane.send_hex.assert_not_called()


def test_a_vim_not_ready_is_typed_the_define_line() -> None:
    pane = _defined(tmux_vim.TmuxVimFollower(pane_id="%5"))
    (sent,) = pane.send_line.call_args_list
    assert sent.args[0].startswith(":if !exists(")


def test_an_unread_keyed_call_brings_the_define_back() -> None:
    # A user's `:mapclear` took the key's mapping: the key's call file is
    # never read, so the next contact types the define, whose maparg()
    # guard re-sources the script.
    ready = _ready_file(os.getpid())
    long_ago = time.time() - 30
    os.utime(ready, (long_ago, long_ago))
    pane = MagicMock(pane_id="%5")
    # The Vim started before its ready file (this test process may not have).
    with patch.object(tmux_vim, "_process_started", return_value=long_ago - 1):
        tmux_vim._send(pane, tmux_vim._call_line("goto", "p5-abcdef"))
        (call_file,) = _call_files()
        assert not _defined(tmux_vim.TmuxVimFollower(pane_id="%5")).send_line.called
        unread_since = time.time() - tmux_vim._UNREAD_CALL_SECONDS - 1
        os.utime(call_file, (unread_since, unread_since))
        (sent,) = _defined(tmux_vim.TmuxVimFollower(pane_id="%5")).send_line.call_args_list
    assert sent.args[0].startswith(":if maparg(")


def test_an_unread_call_older_than_the_ready_file_is_not_a_lost_mapping() -> None:
    # The define just re-sourced the script and the Vim wrote its ready
    # file again: the call sent before that is no sign any more.
    pane = MagicMock(pane_id="%5")
    _ready_file(os.getpid())
    tmux_vim._send(pane, tmux_vim._call_line("goto", "p5-abcdef"))
    (call_file,) = _call_files()
    unread_since = time.time() - tmux_vim._UNREAD_CALL_SECONDS - 1
    os.utime(call_file, (unread_since, unread_since))
    _ready_file(os.getpid())
    assert not _defined(tmux_vim.TmuxVimFollower(pane_id="%5")).send_line.called


def test_the_script_is_built_once_per_cache_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Every call reads it (ready check, define line): hashing the whole
    # script each time cost every call (final review of backlog-sweep-5, M3).
    with patch.object(hashlib, "sha256", wraps=hashlib.sha256) as digest:
        first = tmux_vim._vim_functions()
        assert tmux_vim._vim_functions() == first
        builds = digest.call_count
        monkeypatch.setattr(cache, "CACHE_DIRECTORY", tmp_path / "other")
        other = tmux_vim._vim_functions()
    assert builds <= 1
    assert other[0] != first[0]


def test_a_ready_file_older_than_its_process_does_not_count() -> None:
    """A Vim that crashed leaves its ready file (VimLeave never ran), and a
    later process can get its pid: a file written before that process
    started names a Vim that is gone, and keys sent for it would reach
    whatever runs in the pane now (final review of backlog-sweep-5, M1)."""
    path = _ready_file(os.getpid())
    with patch.object(tmux_vim, "_process_started", return_value=time.time() - 60):
        assert tmux_vim._vim_ready("%5")
        before_the_process = time.time() - 600
        os.utime(path, (before_the_process, before_the_process))
        assert not tmux_vim._vim_ready("%5")
    with patch.object(tmux_vim, "_process_started", return_value=None):
        assert not tmux_vim._vim_ready("%5")  # ps could not say: typed, as for no file


def test_the_start_of_a_process_is_read_from_ps() -> None:
    started = tmux_vim._process_started(os.getpid())
    assert started is not None and started <= time.time()
    assert tmux_vim._process_started(_dead_pid()) is None


def test_a_ready_file_removed_while_it_is_checked_does_not_count() -> None:
    # The Vim quits between the read and the stat: its VimLeave removes the
    # file, and keys sent now would reach the shell left in the pane.
    path = _ready_file(os.getpid())

    def quit_meanwhile(pid: int) -> float:
        path.unlink()
        return time.time() - 60

    with patch.object(tmux_vim, "_process_started", side_effect=quit_meanwhile):
        assert not tmux_vim._vim_ready("%5")


@pytest.mark.parametrize(
    "failure",
    [OSError("ps missing"), subprocess.TimeoutExpired("ps", 2)],
    ids=["os-error", "timeout"],
)
def test_a_ps_that_fails_says_nothing_and_is_asked_again(
    monkeypatch: pytest.MonkeyPatch, failure: Exception
) -> None:
    monkeypatch.setattr(tmux_vim, "_PROCESS_STARTS", {})
    with patch.object(subprocess, "run", side_effect=failure):
        assert tmux_vim._process_started(os.getpid()) is None
    assert tmux_vim._PROCESS_STARTS == {}
    assert tmux_vim._process_started(os.getpid()) is not None
