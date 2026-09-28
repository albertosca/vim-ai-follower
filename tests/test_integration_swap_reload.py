"""Every tmux `:e!` the follower sends must carry the scoped (E)dit-anyway
answer: a real Vim at 49 columns whose buffer has swap ON, with an owner Vim
holding the file's `.swp`.

`:edit` re-runs Vim's swap search (see CLAUDE.md), so on such a buffer:

  - a raw `:e!` (reload_and_relock, the des-interrupt) shows the whole
    ATTENTION dialog, paged at `-- More --`;
  - the completion relock's `:silent! e! | setlocal …` hides it instead and
    stalls INVISIBLY: the next keys the follower sends answer the hidden
    dialog, and the relock's `setlocal` never runs on its own.

Measured 2026-09-28 (`scratchpad/sweep3/B2-reg4-reload-probe.txt`). A buffer
with swap on is the everyday adopted case (the user's own buffers, and the
follower's since 7df9b6b turned swap back on for adopted editors), and a
dedicated buffer a Read opened through `:tab drop`.

The probe of "no dialog is left" is the one that cannot be fooled by the
follower's own Escapes: a command typed with NO Escape before it must run.

Isolation: tmux_session unsets TMUX and points TMUX_TMPDIR at a private
directory; the socket is asserted before any in-process backend call."""

from __future__ import annotations

import os
import subprocess
from collections.abc import Callable
from pathlib import Path

import pytest

from vim_ai_follower.backends import tmux_vim
from vim_ai_follower.backends.tmux_vim import TmuxVimFollower
from vim_ai_follower.diff import compute_edit_script
from vim_ai_follower.state import FollowerState

pytestmark = pytest.mark.integration


def _tmux(*args: str) -> str:
    return subprocess.run(["tmux", *args], capture_output=True, text=True, check=True).stdout


def _keys(pane: str, command: str) -> None:
    subprocess.run(["tmux", "send-keys", "-t", pane, "-l", "--", command], check=True)
    subprocess.run(["tmux", "send-keys", "-t", pane, "Enter"], check=True)


def _assert_private_server() -> None:
    socket_path = _tmux("display-message", "-p", "#{socket_path}").strip()
    private = os.environ["TMUX_TMPDIR"]
    assert "TMUX" not in os.environ
    assert os.path.realpath(socket_path).startswith(os.path.realpath(private)), socket_path


def _setup(
    tmux_session: str, tmp_path: Path, wait_until: Callable[..., bool], content: str
) -> tuple[str, Path]:
    """An owner Vim holding `target`'s swap, and a 49-column follower Vim
    whose buffer for `target` has swap on (answered once, as goto_file does)."""
    _assert_private_server()
    target = tmp_path / "held.py"
    target.write_text(content)
    _tmux("new-window", "-d", "-t", tmux_session, f"vim -N -u NONE -i NONE {target}")
    swap = target.with_name(f".{target.name}.swp")
    assert wait_until(swap.exists, timeout=10.0), "the owner Vim took no swap"
    origin = _tmux("list-panes", "-t", tmux_session, "-F", "#{pane_id}").split()[0]
    pane = _tmux(
        "split-window",
        "-h",
        "-l",
        "49",
        "-t",
        origin,
        "-P",
        "-F",
        "#{pane_id}",
        "vim -N -u NONE -i NONE",
    ).strip()
    assert _tmux("display-message", "-p", "-t", pane, "#{pane_width}").strip() == "49"
    ready = tmp_path / "ready.txt"
    _keys(
        pane,
        ":exe 'augroup T' | exe \"autocmd SwapExists * ++once let v:swapchoice = 'e'\""
        f" | exe 'augroup END' | edit {target} | exe 'autocmd! T'"
        f" | call writefile([swapname('%')], '{ready}')",
    )
    assert wait_until(lambda: ready.exists() and ready.read_text().strip() != "", timeout=10.0)
    assert ready.read_text().strip().endswith(".swo"), "the follower's buffer has no swap"
    return pane, target


def _state_without_escape(pane: str, tmp_path: Path, wait_until: Callable[..., bool]) -> list[str]:
    """Buffer lines, &modifiable and &readonly, asked with NO Escape first:
    a hidden or visible dialog would eat this command and it would never run."""
    out = tmp_path / "state.txt"
    out.unlink(missing_ok=True)
    _keys(
        pane,
        f":call writefile(['S', &modifiable, &readonly] + getline(1, '$'), '{out}')",
    )
    screen = _tmux("capture-pane", "-p", "-t", pane)
    assert wait_until(lambda: out.exists() and out.read_text().startswith("S\n"), timeout=5.0), (
        f"a dialog ate the next command:\n{screen}"
    )
    return out.read_text().splitlines()[1:]


def test_reload_and_relock_answers_the_swap_dialog(
    tmux_session: str, tmp_path: Path, wait_until: Callable[..., bool]
) -> None:
    pane, target = _setup(tmux_session, tmp_path, wait_until, "x = 1\n")
    _keys(pane, ":setlocal modifiable | call append('$', 'typed by the user')")
    target.write_text("x = 2\n")

    TmuxVimFollower(pane_id=pane).reload_and_relock(str(target))

    assert _state_without_escape(pane, tmp_path, wait_until) == ["0", "1", "x = 2"]


@pytest.mark.parametrize("entry", ["apply_edit", "show_fresh"])
def test_the_completion_relock_answers_the_swap_dialog(
    entry: str, tmux_session: str, tmp_path: Path, wait_until: Callable[..., bool]
) -> None:
    pane, target = _setup(tmux_session, tmp_path, wait_until, "x = 1\n")
    target.write_text("x = 1\ny = 2\n")  # Claude's write, before hook post

    if entry == "apply_edit":
        follower = TmuxVimFollower(pane_id=pane, pace_seconds=0.0)
        ops = compute_edit_script("x = 1\n", "x = 1\ny = 2\n")
        result = follower.apply_edit(str(target), ops, before="x = 1\n")
        readonly = "0"
    else:
        # show_fresh wipes the buffer and names a new one with swap off; only
        # an ADOPTED Vim gets swap back on (7df9b6b), which is what makes its
        # relock's `:e!` meet the owner's swap.
        FollowerState.set("@1", "tmux", pane, adopted=True)
        follower = TmuxVimFollower(pane_id=pane, pace_seconds=0.0, window_id="@1")
        result = follower.show_fresh(str(target), "x = 1\ny = 2\n")
        readonly = "1"

    assert result.outcome == "completed"
    assert _state_without_escape(pane, tmp_path, wait_until) == ["0", readonly, "x = 1", "y = 2"]


@pytest.mark.parametrize("writable", [False, True], ids=["E303", "writable"])
def test_the_adopted_swap_back_on_line_never_leaves_a_prompt(
    writable: bool, tmux_session: str, tmp_path: Path, wait_until: Callable[..., bool]
) -> None:
    """show_fresh's swap re-enable in an adopted Vim, at 49 columns. A swap Vim
    cannot create (E303, no writable 'directory') made Vim set its own
    need_wait_return: "Press ENTER" swallowed show_fresh's next keys. The
    writable case proves the line still makes the swap."""
    _assert_private_server()
    swap_dir = tmp_path / ("swap" if writable else "locked")
    swap_dir.mkdir()
    if not writable:
        swap_dir.chmod(0o500)
    target = tmp_path / "retyped.py"
    target.write_text("x = 1\n")
    origin = _tmux("list-panes", "-t", tmux_session, "-F", "#{pane_id}").split()[0]
    pane = _tmux(
        "split-window",
        "-h",
        "-l",
        "49",
        "-t",
        origin,
        "-P",
        "-F",
        "#{pane_id}",
        "vim -N -u NONE -i NONE",
    ).strip()
    assert _tmux("display-message", "-p", "-t", pane, "#{pane_width}").strip() == "49"
    try:
        ready = tmp_path / "ready.txt"
        _keys(
            pane,
            f":set directory={swap_dir}// | enew | setlocal noswapfile"
            f" | exe 'silent file ' . fnameescape('{target}') | call writefile(['R'], '{ready}')",
        )
        assert wait_until(ready.exists, timeout=5.0)
        subprocess.run(["tmux", "send-keys", "-t", pane, "Escape", "Escape"], check=True)

        _keys(pane, tmux_vim._SWAP_BACK_ON)
        wait_until(lambda: "Press ENTER" in _tmux("capture-pane", "-p", "-t", pane), timeout=1.5)
        screen = _tmux("capture-pane", "-p", "-t", pane)
        assert "Press ENTER" not in screen, screen
        assert "E303" not in screen, screen

        out = tmp_path / "after.txt"
        _keys(pane, f":call writefile(['S', &shortmess, swapname('%')], '{out}')")
        assert wait_until(lambda: out.exists() and out.read_text().startswith("S\n"), timeout=5.0)
        shortmess, swapname = [*out.read_text().splitlines()[1:], ""][:2]
        assert "A" not in shortmess
        assert (swapname != "") is writable
    finally:
        swap_dir.chmod(0o700)
