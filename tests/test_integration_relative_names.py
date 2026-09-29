"""Real tmux+vim proof that the follower names its buffers the way Vim names a
file opened by RELATIVE path: relative to the editor's working directory when
the file is under it, full otherwise.

Before (measured 2026-09-29 on Vim 9.2 at 49 columns, cwd /private/tmp/vafn):
show_fresh's `:file /private/tmp/vafn/sub/a.py` left the tabline at
`/p/t/v/s/a.py`, the statusline at the full path and the `:w` message at
`<te/tmp/vafn/sub/a.py" 1L, 6B written`, because Vim only shortens a name it
was given in full when a `:cd` happens. `:file sub/a.py` shows `s/a.py`,
`sub/a.py` and `"sub/a.py" 1L, 6B written`, exactly like `:e sub/a.py`.

The symlinked working directory: the hook hands the backend a realpath
(hooks._file_path), and Vim's getcwd() is the PHYSICAL directory even when
Vim was started from a symlinked spelling of it (and even after `:cd` to the
symlink — measured, Vim and nvim alike), so a plain `:.` compare lines the two
up. It is tested here by starting Vim from a symlinked spelling of its cwd.

Every name is also checked to still resolve: `expand('%:p')` is the real path
and a second navigation finds the buffer by number instead of opening a
duplicate tab.
"""

from __future__ import annotations

import os
import subprocess
from collections.abc import Callable, Iterator
from pathlib import Path

import pytest

from vim_ai_follower.backends.tmux_vim import TmuxVimFollower
from vim_ai_follower.state import FollowerState

pytestmark = pytest.mark.integration

CONTENT = "x = 1\n"


def _tmux(*args: str) -> str:
    return subprocess.run(
        ["tmux", *args], capture_output=True, text=True, check=True
    ).stdout.strip()


@pytest.fixture
def project(tmp_path: Path) -> tuple[Path, Path]:
    """(the project's real directory, a symlinked spelling of it)."""
    real = tmp_path / "real" / "proj"
    (real / "sub").mkdir(parents=True)
    (real / "~x").mkdir()
    link = tmp_path / "link"
    link.symlink_to(tmp_path / "real")
    return Path(os.path.realpath(real)), link / "proj"


@pytest.fixture(params=["real-cwd", "symlinked-cwd"])
def vim_pane(
    request: pytest.FixtureRequest,
    tmux_session: str,
    monkeypatch: pytest.MonkeyPatch,
    project: tuple[Path, Path],
    tmp_path: Path,
) -> Iterator[str]:
    """A real Vim at the follower's real width (49), started in the project
    directory — spelled physically or through the symlink."""
    monkeypatch.delenv("TMUX_PANE", raising=False)
    assert "TMUX" not in os.environ
    socket = _tmux("display-message", "-p", "#{socket_path}")
    assert socket.startswith(os.path.realpath(os.environ["TMUX_TMPDIR"]) + "/"), socket
    real, link = project
    cwd = real if request.param == "real-cwd" else link
    home = tmp_path / "home"
    home.mkdir()
    pane = _tmux(
        "split-window",
        "-h",
        "-t",
        tmux_session,
        "-c",
        str(cwd),
        "-P",
        "-F",
        "#{pane_id}",
        f"env HOME={home} vim",
    )
    assert _tmux("display-message", "-p", "-t", pane, "#{pane_width}") == "49"
    yield pane


def _names(pane: str, out: Path, wait_until: Callable[..., bool]) -> tuple[int, str, str, str]:
    """(tab count, cwd, bufname('%'), expand('%:p')) straight out of Vim —
    deleted first and waited on a sentinel so a stale answer cannot pass."""

    def answered() -> bool:
        try:
            return out.read_text().startswith("TABS=")
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
                ":call writefile(['TABS=' . tabpagenr('$'), getcwd(), bufname('%'),"
                f" expand('%:p')], '{out}')",
            ],
            check=True,
        )
        subprocess.run(["tmux", "send-keys", "-t", pane, "Enter"], check=True)
        if wait_until(answered, timeout=10.0):
            break
    else:
        screen = _tmux("capture-pane", "-p", "-t", pane)
        raise AssertionError(f"Vim never answered. Pane:\n{screen}")
    tabs, cwd, name, full = out.read_text().splitlines()
    return int(tabs.removeprefix("TABS=")), cwd, name, full


def _follower(pane: str, adopted: bool) -> TmuxVimFollower:
    window_id = _tmux("display-message", "-p", "-t", pane, "#{window_id}")
    FollowerState.set(window_id, backend="tmux", target=pane, adopted=adopted, speed="instant")
    return TmuxVimFollower(pane_id=pane, pace_seconds=0.0, window_id=window_id)


@pytest.mark.parametrize("adopted", [False, True], ids=["dedicated", "adopted"])
def test_show_fresh_names_a_file_under_the_cwd_relative_to_it(
    vim_pane: str,
    project: tuple[Path, Path],
    tmp_path: Path,
    wait_until: Callable[..., bool],
    adopted: bool,
) -> None:
    real, _ = project
    target = real / "sub" / "a.py"
    target.write_text(CONTENT)
    follower = _follower(vim_pane, adopted)

    follower.show_fresh(str(target), CONTENT)
    tabs, cwd, name, full = _names(vim_pane, tmp_path / "names.txt", wait_until)

    assert cwd == str(real)
    assert name == "sub/a.py"
    assert full == str(target)
    follower.goto_file(str(target))  # found by number, not reopened
    assert _names(vim_pane, tmp_path / "names.txt", wait_until)[0] == tabs


@pytest.mark.parametrize("adopted", [False, True], ids=["dedicated", "adopted"])
def test_opening_a_file_no_buffer_holds_names_it_relative_to_the_cwd(
    vim_pane: str,
    project: tuple[Path, Path],
    tmp_path: Path,
    wait_until: Callable[..., bool],
    adopted: bool,
) -> None:
    """ensure_showing of a file no buffer holds: _GOTO_FILE's `:tab drop`."""
    real, _ = project
    target = real / "b.py"
    target.write_text(CONTENT)
    follower = _follower(vim_pane, adopted)

    follower.ensure_showing(str(target))
    tabs, _, name, full = _names(vim_pane, tmp_path / "names.txt", wait_until)

    assert name == "b.py"
    assert full == str(target)
    follower.ensure_showing(str(target))
    assert _names(vim_pane, tmp_path / "names.txt", wait_until)[0] == tabs


def test_a_file_outside_the_cwd_keeps_its_full_name(
    vim_pane: str, tmp_path: Path, wait_until: Callable[..., bool]
) -> None:
    outside = Path(os.path.realpath(tmp_path)) / "outside.py"
    outside.write_text(CONTENT)
    other = Path(os.path.realpath(tmp_path)) / "other.py"
    other.write_text(CONTENT)
    follower = _follower(vim_pane, adopted=False)

    follower.show_fresh(str(outside), CONTENT)
    assert _names(vim_pane, tmp_path / "names.txt", wait_until)[2] == str(outside)
    follower.ensure_showing(str(other))
    assert _names(vim_pane, tmp_path / "names.txt", wait_until)[2] == str(other)


def test_a_relative_name_that_would_start_with_a_tilde_stays_full(
    vim_pane: str,
    project: tuple[Path, Path],
    tmp_path: Path,
    wait_until: Callable[..., bool],
) -> None:
    """`~x/a.py` as a buffer name reads back through `:p` as user x's home
    (measured: `fnamemodify('~x/a.py', ':p')` is not the file), so the
    by-number lookup would miss the buffer and open a duplicate tab."""
    real, _ = project
    target = real / "~x" / "a.py"
    target.write_text(CONTENT)
    follower = _follower(vim_pane, adopted=False)

    follower.show_fresh(str(target), CONTENT)
    tabs, _, name, full = _names(vim_pane, tmp_path / "names.txt", wait_until)

    assert name == str(target)
    assert full == str(target)
    follower.goto_file(str(target))
    assert _names(vim_pane, tmp_path / "names.txt", wait_until)[0] == tabs
