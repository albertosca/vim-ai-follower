"""The nvim twin of tests/test_integration_relative_names.py: every buffer
the follower names — show_fresh and goto_file (`nvim_buf_set_name`) and
ensure_showing's disk read (`bufadd`) — is named relative to nvim's working
directory when the file is under it, full otherwise, like `:e sub/a.py`.

Before (measured 2026-09-29, nvim 0.12.5 at 49 columns): the full path in the
tabline (`/p/t/v/s/a.py`), the statusline and the `:w` message, the same as
Vim. A UI nvim in a real tmux pane at the follower's width, started from the
physical and from a symlinked spelling of its cwd: getcwd() is physical in
both, and so is the realpath the hook passes.
"""

from __future__ import annotations

import os
import subprocess
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

pynvim = pytest.importorskip("pynvim")

from vim_ai_follower.backends.nvim import NvimFollower  # noqa: E402
from vim_ai_follower.state import FollowerState  # noqa: E402

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
def nvim_pane(
    request: pytest.FixtureRequest,
    tmux_session: str,
    monkeypatch: pytest.MonkeyPatch,
    project: tuple[Path, Path],
    tmp_path: Path,
) -> Iterator[tuple[str, str]]:
    """(pane id, socket) of a UI nvim at 49 columns, started in the project
    directory spelled physically or through the symlink."""
    monkeypatch.delenv("TMUX_PANE", raising=False)
    assert "TMUX" not in os.environ
    socket = _tmux("display-message", "-p", "#{socket_path}")
    assert socket.startswith(os.path.realpath(os.environ["TMUX_TMPDIR"]) + "/"), socket
    real, link = project
    cwd = real if request.param == "real-cwd" else link
    home = tmp_path / "home"
    home.mkdir()
    sock = str(Path(os.environ["TMUX_TMPDIR"]) / "nvim.sock")
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
        f"env HOME={home} XDG_STATE_HOME={home} nvim -u NONE -i NONE -n --listen {sock}",
    )
    assert _tmux("display-message", "-p", "-t", pane, "#{pane_width}") == "49"
    deadline = time.monotonic() + 10.0
    while not Path(sock).exists() and time.monotonic() < deadline:
        time.sleep(0.05)
    assert Path(sock).exists(), _tmux("capture-pane", "-p", "-t", pane)
    yield pane, sock


def _follower(pane: str, sock: str, adopted: bool) -> NvimFollower:
    window_id = _tmux("display-message", "-p", "-t", pane, "#{window_id}")
    FollowerState.set(window_id, backend="nvim", target=sock, adopted=adopted, speed="instant")
    return NvimFollower(socket_path=sock, window_id=window_id, pace_seconds=0.0)


def _names(sock: str) -> tuple[int, str, str, str]:
    """(tab count, cwd, bufname('%'), the buffer's full name)."""
    nvim: Any = pynvim.attach("socket", path=sock)
    try:
        return (
            len(nvim.api.list_tabpages()),
            nvim.funcs.getcwd(),
            nvim.funcs.bufname("%"),
            nvim.api.buf_get_name(0),
        )
    finally:
        nvim.close()


@pytest.mark.parametrize("adopted", [False, True], ids=["dedicated", "adopted"])
def test_show_fresh_names_a_file_under_the_cwd_relative_to_it(
    nvim_pane: tuple[str, str], project: tuple[Path, Path], adopted: bool
) -> None:
    pane, sock = nvim_pane
    real, _ = project
    target = real / "sub" / "a.py"
    target.write_text(CONTENT)
    follower = _follower(pane, sock, adopted)

    follower.show_fresh(str(target), CONTENT, in_new_tab=True)
    tabs, cwd, name, full = _names(sock)

    assert cwd == str(real)
    assert name == "sub/a.py"
    assert full == str(target)
    follower.goto_file(str(target))  # found by number, not reopened
    assert _names(sock)[0] == tabs


@pytest.mark.parametrize("adopted", [False, True], ids=["dedicated", "adopted"])
def test_goto_file_names_a_new_buffer_relative_to_the_cwd(
    nvim_pane: tuple[str, str], project: tuple[Path, Path], adopted: bool
) -> None:
    """goto_file of a file no buffer holds creates it empty and names it."""
    pane, sock = nvim_pane
    real, _ = project
    target = real / "sub" / "g.py"
    follower = _follower(pane, sock, adopted)

    follower.goto_file(str(target))
    tabs, _, name, full = _names(sock)

    assert name == "sub/g.py"
    assert full == str(target)
    follower.goto_file(str(target))
    assert _names(sock)[0] == tabs


@pytest.mark.parametrize("adopted", [False, True], ids=["dedicated", "adopted"])
def test_ensure_showing_names_a_file_read_from_disk_relative_to_the_cwd(
    nvim_pane: tuple[str, str], project: tuple[Path, Path], adopted: bool
) -> None:
    """ensure_showing of a file no buffer holds: _open_from_disk's bufadd."""
    pane, sock = nvim_pane
    real, _ = project
    target = real / "b.py"
    target.write_text(CONTENT)
    follower = _follower(pane, sock, adopted)

    follower.ensure_showing(str(target))
    tabs, _, name, full = _names(sock)

    assert name == "b.py"
    assert full == str(target)
    follower.ensure_showing(str(target))
    assert _names(sock)[0] == tabs


def test_a_file_outside_the_cwd_keeps_its_full_name(
    nvim_pane: tuple[str, str], tmp_path: Path
) -> None:
    pane, sock = nvim_pane
    outside = Path(os.path.realpath(tmp_path)) / "outside.py"
    outside.write_text(CONTENT)
    other = Path(os.path.realpath(tmp_path)) / "other.py"
    other.write_text(CONTENT)
    follower = _follower(pane, sock, adopted=False)

    follower.show_fresh(str(outside), CONTENT)
    assert _names(sock)[2] == str(outside)
    follower.ensure_showing(str(other))
    assert _names(sock)[2] == str(other)


def test_a_relative_name_that_would_start_with_a_tilde_stays_full(
    nvim_pane: tuple[str, str], project: tuple[Path, Path]
) -> None:
    """Parity with the tmux backend, where `~x/a.py` reads back through `:p`
    as user x's home."""
    pane, sock = nvim_pane
    real, _ = project
    target = real / "~x" / "a.py"
    target.write_text(CONTENT)
    follower = _follower(pane, sock, adopted=False)

    follower.show_fresh(str(target), CONTENT, in_new_tab=True)
    tabs, _, name, full = _names(sock)

    assert name == str(target)
    assert full == str(target)
    follower.goto_file(str(target))
    assert _names(sock)[0] == tabs


@pytest.fixture(params=["real-cwd", "symlinked-cwd"])
def mixed_case(
    request: pytest.FixtureRequest,
    tmux_session: str,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> Iterator[tuple[str, str, Path]]:
    """(pane, socket, the project dir spelled `…/proj`) for a UI nvim started
    in `…/Proj` — the same directory on a case-insensitive filesystem."""
    real = Path(os.path.realpath(tmp_path)) / "case" / "Proj"
    (real / "sub").mkdir(parents=True)
    wrong_case = real.parent / "proj"
    if not wrong_case.exists():
        pytest.skip("needs a case-insensitive filesystem")
    (tmp_path / "caselink").symlink_to(real.parent)
    cwd = real if request.param == "real-cwd" else tmp_path / "caselink" / "Proj"
    monkeypatch.delenv("TMUX_PANE", raising=False)
    assert "TMUX" not in os.environ
    socket = _tmux("display-message", "-p", "#{socket_path}")
    assert socket.startswith(os.path.realpath(os.environ["TMUX_TMPDIR"]) + "/"), socket
    home = tmp_path / "home"
    home.mkdir()
    sock = str(Path(os.environ["TMUX_TMPDIR"]) / "nvim.sock")
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
        f"env HOME={home} XDG_STATE_HOME={home} nvim -u NONE -i NONE -n --listen {sock}",
    )
    assert _tmux("display-message", "-p", "-t", pane, "#{pane_width}") == "49"
    deadline = time.monotonic() + 10.0
    while not Path(sock).exists() and time.monotonic() < deadline:
        time.sleep(0.05)
    assert Path(sock).exists(), _tmux("capture-pane", "-p", "-t", pane)
    yield pane, sock, wrong_case


@pytest.mark.parametrize("adopted", [False, True], ids=["dedicated", "adopted"])
def test_a_wrong_case_path_keeps_its_full_name_and_is_found_again(
    mixed_case: tuple[str, str, Path], adopted: bool
) -> None:
    """Parity with the tmux backend's round-trip guard: nvim's `:.` shortens
    `…/proj/sub/a.py` against cwd `…/Proj` case-insensitively, and the short
    form's `:p` comes back as `…/Proj/sub/a.py`, so the full path is kept.
    (_buffer_number's realpath compare would find either name; the tmux
    lookups, comparing `:p` with `==#`, would not.)"""
    pane, sock, wrong_case = mixed_case
    target = wrong_case / "sub" / "a.py"
    target.write_text(CONTENT)
    other = wrong_case / "other.py"
    other.write_text(CONTENT)
    follower = _follower(pane, sock, adopted)

    follower.ensure_showing(str(other))
    follower.show_fresh(str(target), CONTENT, in_new_tab=True)
    assert follower.probe_buffer(str(target), CONTENT) == "holds"
    tabs, _, name, full = _names(sock)
    assert name == str(target)
    # nvim itself stores the FULL name in the directory's on-disk case
    # (measured: `…/Proj/sub/a.py`), whatever case it was given.
    assert full.lower() == str(target).lower()

    follower.show_fresh(str(target), CONTENT, in_new_tab=True)
    assert _names(sock)[0] == tabs
    follower.close_tab(str(target))
    assert _names(sock)[0] == tabs - 1
    assert follower.probe_buffer(str(target), CONTENT) == "absent"
