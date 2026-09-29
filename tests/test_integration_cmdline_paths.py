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
    follower.ensure_showing(str(read_only))  # _GOTO_FILE's `:tab drop` path
    # A listed buffer whose tab the user closed ('nohidden' unloads it):
    # _GOTO_FILE's `:tab sbuffer`, which reads the file back in.
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
