"""Real tmux+vim proof that the tmux backend never shows the target file's
absolute path on Vim's command line.

The backend drives Vim by TYPING Ex commands (`tmux send-keys`), and Vim echoes
every typed command on its command line before it runs it. Found on a demo
recording (2026-09-29, OCR on the MP4): every edit and every navigation
flashed `/private/tmp/…/fib.py` in the follower's footer, up to five screen
rows of it at 49 columns (the wipe's `fnamemodify('/…', ':p')`, the rename's
`:exe 'file ' . fnameescape(…)` with the path three times, goto_file's
`let g:vaf_p = '/…'`, the probe's lookup).

The instrument logs EVERY command-line change, not samples of the screen: a
`CmdlineChanged` autocmd (loaded through VIMINIT, so no keys are sent to set it
up) appends `getcmdline()` to a list after each typed character, so a flash
of any length is on record whether or not it was ever painted. The list is
dumped once at the end together with `:messages`, which holds what Vim printed
on the same line (a `:file` rename prints the file's name there).
"""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
import time
from collections.abc import Callable
from pathlib import Path

import pytest

from vim_ai_follower import cache
from vim_ai_follower.backends.tmux_vim import TmuxVimFollower
from vim_ai_follower.diff import compute_edit_script
from vim_ai_follower.state import FollowerState

pytestmark = pytest.mark.integration

BEFORE = "x = 1\ny = 2\n"
AFTER = "x = 1\nz = 3\ny = 2\n"

_LOGGER = (
    "set nocompatible noloadplugins\n"
    "let g:vaf_test_log = []\n"
    "autocmd CmdlineEnter * call add(g:vaf_test_log, 'ENTER')\n"
    "autocmd CmdlineChanged * call add(g:vaf_test_log, getcmdline())\n"
)


def _tmux(*args: str) -> str:
    return subprocess.run(
        ["tmux", *args], capture_output=True, text=True, check=True
    ).stdout.strip()


def _logged_vim(
    tmux_session: str, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, home: Path
) -> str:
    """A real Vim at the follower's width (49), started in `proj` with HOME
    `home`, that logs every command-line change."""
    monkeypatch.delenv("TMUX_PANE", raising=False)
    assert "TMUX" not in os.environ
    socket = _tmux("display-message", "-p", "#{socket_path}")
    assert socket.startswith(os.path.realpath(os.environ["TMUX_TMPDIR"]) + "/"), socket
    init = tmp_path / "init.vim"
    init.write_text(_LOGGER)
    pane = _tmux(
        "split-window",
        "-h",
        "-t",
        tmux_session,
        "-c",
        str(tmp_path / "proj"),
        "-e",
        f"VIMINIT=source {init}",
        "-e",
        f"HOME={home}",
        "-P",
        "-F",
        "#{pane_id}",
        "vim",
    )
    assert _tmux("display-message", "-p", "-t", pane, "#{pane_width}") == "49"
    return pane


# Two screen rows at the follower pane's 49 columns, as getcmdline() reports
# a line (without the typed `:`). Measured on Vim 9.2 in a 49-column tmux
# pane (2026-10-05): `:` + 96 characters fills both rows; `:` + 97 puts the
# cursor on a third.
MAXIMUM_LINE_LENGTH = 96


def _whole_lines(typed: list[str]) -> list[str]:
    """Each command line as it stood when Enter ran it: the last logged state
    before the next CmdlineEnter (states are logged per typed character)."""
    lines: list[str] = []
    current: str | None = None
    for state in [*typed, "ENTER"]:
        if state == "ENTER":
            if current is not None:
                lines.append(current)
            current = None
        else:
            current = state
    return lines


def _dump(pane: str, out: Path, wait_until: Callable[..., bool]) -> tuple[list[str], list[str]]:
    """(every logged command line, `:messages`), straight out of Vim — deleted
    first and waited on a sentinel so a stale or half-written dump cannot
    pass. Typing the dump is logged too (the log is copied when the line
    runs), so the states that are prefixes of the dump line are dropped."""
    command = (
        "call writefile(copy(g:vaf_test_log) + ['END-OF-LOG']"
        " + split(execute('messages'), \"\\n\") + ['END-OF-DUMP'],"
        f" '{out}')"
    )

    def answered() -> bool:
        try:
            return out.read_text().endswith("END-OF-DUMP\n")
        except OSError:
            return False

    for _ in range(3):
        out.unlink(missing_ok=True)
        subprocess.run(["tmux", "send-keys", "-t", pane, "Escape", "Escape"], check=True)
        subprocess.run(
            [
                "tmux",
                "send-keys",
                "-t",
                pane,
                "-l",
                "--",
                ":" + command,
            ],
            check=True,
        )
        subprocess.run(["tmux", "send-keys", "-t", pane, "Enter"], check=True)
        if wait_until(answered, timeout=20.0):
            break
    else:
        screen = _tmux("capture-pane", "-p", "-t", pane)
        raise AssertionError(f"Vim never answered. Pane:\n{screen}")
    lines = out.read_text().splitlines()
    end = lines.index("END-OF-LOG")
    typed = [line for line in lines[:end] if line == "ENTER" or not command.startswith(line)]
    return typed, lines[end + 1 : -1]


@pytest.mark.parametrize("vim_home", ["same-home", "other-home"])
@pytest.mark.parametrize("adopted", [False, True], ids=["dedicated", "adopted"])
def test_no_absolute_path_ever_reaches_the_command_line(
    tmux_session: str,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    wait_until: Callable[..., bool],
    adopted: bool,
    vim_home: str,
    request: pytest.FixtureRequest,
) -> None:
    """The hook's HOME is spelled through the /tmp symlink, as in the demo
    (HOME under /tmp -> /private/tmp), and CACHE_DIR is under it. With the Vim
    on the same HOME, the typed lines name the cache HOME-relative (`~/…`),
    so no absolute directory at all reaches the command line. With the Vim on
    ANOTHER HOME, `~` would be a different directory: the follower must
    notice and type the cache path in full, and everything still works."""
    root = Path(os.path.realpath(tmp_path))
    (root / "proj" / "sub").mkdir(parents=True)
    (root / "outside").mkdir()
    # Short and under /tmp, never tmp_path: /tmp is itself the symlink.
    home_parent = Path(tempfile.mkdtemp(prefix="vafh-", dir="/tmp"))
    request.addfinalizer(lambda: shutil.rmtree(home_parent, ignore_errors=True))
    home = home_parent / "home"
    home.mkdir()
    assert os.path.realpath(home) != str(home)  # really a symlinked spelling
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setattr(cache, "CACHE_DIR", home / ".cache" / "claude-vim-follower")
    vim_home_dir = home
    if vim_home == "other-home":
        vim_home_dir = root / "other-home"
        vim_home_dir.mkdir()
    pane = _logged_vim(tmux_session, monkeypatch, tmp_path, vim_home_dir)
    window_id = _tmux("display-message", "-p", "-t", pane, "#{window_id}")
    FollowerState.set(window_id, backend="tmux", target=pane, adopted=adopted, speed="instant")
    follower = TmuxVimFollower(pane_id=pane, pace_seconds=0.0, window_id=window_id)
    # Under the cwd (a short name) and outside it with every pattern and
    # quoting hazard (the full-name fallback, which also used to be the
    # `:file` message).
    under = root / "proj" / "sub" / "a.py"
    outside = root / "outside" / "b [x] {1,2} 'q' %#$HOME.py"
    read_only = root / "outside" / "r.py"
    read_only.write_text(BEFORE)

    for target in (under, outside):
        target.write_text(BEFORE)
        follower.show_fresh(str(target), BEFORE, in_new_tab=True)
        target.write_text(AFTER)
        assert follower.probe_buffer(str(target), BEFORE) == "holds"
        follower.apply_edit(str(target), compute_edit_script(BEFORE, AFTER), BEFORE)
        follower.goto_file(str(target))
        follower.ensure_showing(str(target))
        assert follower.probe_buffer(str(target), AFTER) == "holds"
    follower.ensure_showing(str(read_only))  # s:goto's `:tab drop` path
    # A listed buffer whose tab the user closed ('nohidden' unloads it):
    # s:goto's `:tab sbuffer`, which reads the file back in.
    follower.goto_file(str(outside))
    subprocess.run(["tmux", "send-keys", "-t", pane, "-l", "--", ":tabclose"], check=True)
    subprocess.run(["tmux", "send-keys", "-t", pane, "Enter"], check=True)
    assert follower.probe_buffer(str(outside), AFTER) == "absent"
    follower.goto_file(str(outside))
    assert follower.probe_buffer(str(outside), AFTER) == "holds"
    assert follower.probe_buffer(str(read_only), BEFORE) == "holds"
    follower.close_tab(str(under))
    assert follower.probe_buffer(str(under), AFTER) == "absent"

    typed, messages = _dump(pane, tmp_path / "dump.txt", wait_until)

    # The instrument saw the follower's commands at all (a broken logger
    # would pass the absence checks below).
    assert typed.count("ENTER") >= 20, typed
    assert any("setlocal" in line for line in typed), typed
    # No absolute spelling of the targets' directories, ever.
    for base in {root, tmp_path}:
        for directory in (base / "proj", base / "outside"):
            leaked = [line for line in typed if str(directory) in line]
            assert leaked == [], leaked[:3]
            shown = [line for line in messages if str(directory) in line]
            assert shown == [], shown
    home_relative = [line for line in typed if "~/.cache/claude-vim-follower/" in line]
    if vim_home == "same-home":
        # Nor of the cache or HOME, in either spelling: the targets live
        # under tmp_path and the cache under HOME.
        for base in {root, tmp_path, home_parent, Path(os.path.realpath(home_parent))}:
            leaked = [line for line in typed if str(base) in line]
            assert leaked == [], leaked[:3]
            shown = [line for line in messages if str(base) in line]
            assert shown == [], shown
        assert home_relative, typed
    else:
        # The cache in full: `~` in this Vim is not the hook's HOME.
        assert any(str(cache.CACHE_DIR) in line for line in typed), typed
        assert [line for line in home_relative if "readfile(" in line] == []

    # Every line is short: one or two screen rows at the pane's 49 columns,
    # never the old 12-30-row block of Vim script. The logic lives in Vim
    # functions defined once; a line only calls them. A Vim on another HOME
    # gets its one source line with the cache directory in full, so that
    # line may be longer by exactly the difference between the spellings.
    longest = MAXIMUM_LINE_LENGTH
    if vim_home == "other-home":
        longest += len(str(cache.CACHE_DIR)) - len("~/.cache/claude-vim-follower")
    too_long = [line for line in _whole_lines(typed) if len(line) > longest]
    assert too_long == [], [(len(line), line) for line in too_long]


def _keys(pane: str, *keys: str) -> None:
    subprocess.run(["tmux", "send-keys", "-t", pane, *keys], check=True)


def _buffer_state(pane: str, out: Path, wait_until: Callable[..., bool]) -> list[str]:
    """The current buffer's name and flags, user.txt's lines and modified
    flag, and whether a hit-enter prompt is pending — straight out of Vim,
    deleted first and waited on a sentinel."""
    out.unlink(missing_ok=True)
    _keys(pane, "Escape", "Escape")
    _keys(
        pane,
        "-l",
        "--",
        ":call writefile(['BUF=' . bufname('%'), 'MA=' . &modifiable, 'RO=' . &readonly,"
        " 'TABS=' . tabpagenr('$')] + getbufline('user.txt', 1, '$')"
        f" + [getbufvar('user.txt', '&modified') . '', 'END'], '{out}')",
    )
    _keys(pane, "Enter")
    assert wait_until(lambda: out.exists() and out.read_text().endswith("END\n"), timeout=10.0), (
        _tmux("capture-pane", "-p", "-t", pane)
    )
    return out.read_text().splitlines()[:-1]


@pytest.mark.parametrize("loss", ["functions-deleted", "vim-restarted"])
def test_a_vim_that_lost_the_functions_gets_them_again_on_the_next_call(
    tmux_session: str,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    wait_until: Callable[..., bool],
    loss: str,
) -> None:
    """The lines only CALL functions defined once per Vim, so a Vim that no
    longer has them — the user's `:delfunction`, or an adopted Vim restarted
    in the same shell (same pane process, so nothing on the tmux side
    changed) — must get them again on the very next call: that call lands,
    leaves no prompt (no E117) and never touches the user's own buffer."""
    monkeypatch.delenv("TMUX_PANE", raising=False)
    assert "TMUX" not in os.environ
    socket = _tmux("display-message", "-p", "#{socket_path}")
    assert socket.startswith(os.path.realpath(os.environ["TMUX_TMPDIR"]) + "/"), socket
    root = Path(os.path.realpath(tmp_path))
    home, proj = root / "home", root / "proj"
    home.mkdir()
    proj.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setattr(cache, "CACHE_DIR", home / ".cache" / "claude-vim-follower")
    target = proj / "target.py"
    target.write_text(BEFORE)
    user = proj / "user.txt"
    user.write_text("user line\n")
    # The user's shell at the follower pane's width: an adopted Vim runs in it.
    pane = _tmux(
        "split-window", "-h", "-t", tmux_session, "-c", str(proj), "-P", "-F", "#{pane_id}",
        "env", "-i", f"HOME={home}", f"PATH={os.environ['PATH']}", "TERM=xterm-256color",
        "bash", "--norc", "--noprofile",
    )  # fmt: skip
    assert _tmux("display-message", "-p", "-t", pane, "#{pane_width}") == "49"

    def start_vim() -> None:
        _keys(pane, "-l", "--", "vim -N -u NONE -i NONE user.txt")
        _keys(pane, "Enter")
        assert wait_until(
            lambda: _tmux("display-message", "-p", "-t", pane, "#{pane_current_command}") == "vim",
            timeout=10.0,
        )

    start_vim()
    window_id = _tmux("display-message", "-p", "-t", pane, "#{window_id}")
    FollowerState.set(window_id, backend="tmux", target=pane, adopted=True, speed="instant")
    follower = TmuxVimFollower(pane_id=pane, pace_seconds=0.0, window_id=window_id)
    follower.goto_file(str(target))
    assert _buffer_state(pane, tmp_path / "s0.txt", wait_until)[0] == "BUF=target.py"

    if loss == "functions-deleted":
        _keys(
            pane,
            "-l",
            "--",
            ":for name in getcompletion('VafFollower', 'function')"
            " | exe 'delfunction ' . matchstr(name, '^[^(]*') | endfor",
        )
        _keys(pane, "Enter")
        _keys(pane, "-l", "--", ":tabfirst")
        _keys(pane, "Enter")
    else:
        _keys(pane, "-l", "--", ":qa!")
        _keys(pane, "Enter")
        assert wait_until(
            lambda: _tmux("display-message", "-p", "-t", pane, "#{pane_current_command}") == "bash",
            timeout=10.0,
        )
        start_vim()
    _keys(pane, "-l", "--", "GoUSER UNSAVED WORK")
    _keys(pane, "Escape")
    before = _buffer_state(pane, tmp_path / "s1.txt", wait_until)
    assert before[:3] == ["BUF=user.txt", "MA=1", "RO=0"], before
    assert before[4:] == ["user line", "USER UNSAVED WORK", "1"], before

    started = time.monotonic()
    follower.ensure_showing(str(target))  # raises NavigationFailed if it did not land
    assert time.monotonic() - started < 5.0
    after = _buffer_state(pane, tmp_path / "s2.txt", wait_until)
    screen = _tmux("capture-pane", "-p", "-t", pane)
    assert after[:4] == ["BUF=target.py", "MA=0", "RO=1", "TABS=2"], after
    # user.txt is still the user's: text, modified flag and all.
    assert after[4:] == ["user line", "USER UNSAVED WORK", "1"], after
    assert user.read_text() == "user line\n"
    assert "Press ENTER" not in screen, screen
    assert "E117" not in screen, screen
    assert follower.probe_buffer(str(target), BEFORE) == "holds"
