"""Real-headless-nvim coverage for opening a file whose swap file is held by
a LIVE second Neovim. Only reproduces against real nvim: E325 is raised by
nvim itself, from inside `bufload`, and no mock can be trusted to model it.

These tests cannot use the shared `headless_nvim` fixture. It launches with
`-n` (noswapfile), which suppresses the swap CHECK outright, so every
assertion here would pass with the fix reverted — a green that measures
nothing. The local `swap_aware_nvim` factory drops `-n` and pins `directory`
to one shared throwaway dir, because nvim's default swap location is
`~/.local/state/nvim/swap//` (NOT `.`, as in Vim): two nvims left on defaults
would write the user's real swap dir, and a test file under tmp_path would
still collide correctly but litter outside the sandbox.

`test_an_unrelated_swapped_file_still_hits_the_swap_check` is the anti-vacuity
canary for this whole module: it proves the swap machinery in the follower's
own nvim is still armed, so the other tests' silence is a real suppression
and not a swap that never existed."""

from __future__ import annotations

import shutil
import subprocess
import tempfile
import time
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import pytest

pynvim = pytest.importorskip("pynvim")

from vim_ai_follower import state  # noqa: E402
from vim_ai_follower.animate import AnimationResult  # noqa: E402
from vim_ai_follower.backends.nvim import NvimFollower  # noqa: E402
from vim_ai_follower.diff import EditOp  # noqa: E402


@pytest.fixture
def swap_aware_nvim() -> Iterator[Callable[[], str]]:
    """Factory for throwaway headless nvims that DO honour swap files, all
    sharing one `directory` so a swap written by one is seen by the next.
    Deliberately NOT a variant of `headless_nvim`: that fixture's `-n` is
    load-bearing for every other nvim test and must not change.

    Uses tempfile directly rather than tmp_path for the same reason
    tmux_session does — pytest's per-test tmp_path nests the socket too
    deeply for the ~104-char Unix domain socket limit."""
    work = Path(tempfile.mkdtemp(prefix="cf-swap-"))
    (work / "swap").mkdir()
    procs: list[subprocess.Popen[bytes]] = []

    def _launch() -> str:
        sock = str(work / f"nvim{len(procs)}.sock")
        procs.append(
            subprocess.Popen(
                [
                    "nvim",
                    "--headless",
                    "-u",
                    "NONE",
                    "-i",
                    "NONE",
                    "--cmd",
                    f"set directory={work / 'swap'}//",
                    "--listen",
                    sock,
                ],
                stderr=subprocess.DEVNULL,
            )
        )
        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline and not Path(sock).exists():
            time.sleep(0.05)
        return sock

    try:
        yield _launch
    finally:
        for proc in procs:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:  # pragma: no cover - defensive cleanup
                proc.kill()
        shutil.rmtree(work, ignore_errors=True)


def _owner_holding(sock: str, path: Path) -> Any:
    """Attach to `sock`, open `path` there, and assert it really took out a
    swap file. Without that assert the caller's test is vacuous: a swap that
    never appeared would make every suppression check trivially pass."""
    owner = pynvim.attach("socket", path=sock)
    owner.command(f"edit {path}")
    swapname = owner.funcs.swapname(owner.funcs.bufnr(str(path)))
    assert swapname, f"precondition failed: no swap taken out for {path}"
    assert Path(swapname).exists()
    return owner


@pytest.mark.integration
def test_ensure_showing_opens_a_file_a_live_nvim_holds_the_swap_for(
    swap_aware_nvim: Callable[[], str], tmp_path: Path
) -> None:
    target = tmp_path / "held.py"
    target.write_text("disk one\ndisk two\ndisk three\n")
    _owner_holding(swap_aware_nvim(), target)

    follower_sock = swap_aware_nvim()
    follower = NvimFollower(socket_path=follower_sock, window_id="@1", pace_seconds=0.0)
    follower.ensure_showing(str(target))

    nvim = pynvim.attach("socket", path=follower_sock)
    assert nvim.current.buffer[:] == ["disk one", "disk two", "disk three"]
    # The follower's own buffer took out no swap of its own — it is never
    # written, and a second .swo would litter the user's swap dir and make
    # the NEXT editor to open the file see a stale-swap prompt.
    assert nvim.funcs.swapname(nvim.funcs.bufnr(str(target))) == ""


@pytest.mark.integration
def test_apply_edits_vanished_buffer_branch_opens_a_swap_held_file(
    swap_aware_nvim: Callable[[], str], tmp_path: Path
) -> None:
    target = tmp_path / "vanished.py"
    target.write_text("post edit one\npost edit two\n")
    _owner_holding(swap_aware_nvim(), target)

    follower_sock = swap_aware_nvim()
    follower = NvimFollower(socket_path=follower_sock, window_id="@1", pace_seconds=0.0)
    nvim = pynvim.attach("socket", path=follower_sock)
    assert nvim.funcs.bufnr(str(target)) == -1

    ops = [EditOp(kind="replace", start_line=1, end_line=1, new_lines=("post edit one",))]
    assert follower.apply_edit(str(target), ops) == AnimationResult("completed", 1)
    assert nvim.current.buffer[:] == ["post edit one", "post edit two"]


@pytest.mark.integration
def test_the_other_editor_still_writes_the_file_afterwards(
    swap_aware_nvim: Callable[[], str], tmp_path: Path
) -> None:
    target = tmp_path / "shared.py"
    target.write_text("before\n")
    owner = _owner_holding(swap_aware_nvim(), target)

    follower = NvimFollower(socket_path=swap_aware_nvim(), window_id="@1", pace_seconds=0.0)
    follower.ensure_showing(str(target))

    owner.api.buf_set_lines(owner.funcs.bufnr(str(target)), 0, -1, True, ["after"])
    owner.command("write")
    assert target.read_text() == "after\n"
    # and it still owns its swap: the follower opted ITS OWN buffer out, not
    # the file, so the other editor's crash protection is intact.
    assert owner.funcs.swapname(owner.funcs.bufnr(str(target))) != ""


@pytest.mark.integration
def test_an_unrelated_swapped_file_still_hits_the_swap_check(
    swap_aware_nvim: Callable[[], str], tmp_path: Path
) -> None:
    """Adopt-mode scoping. After the follower opens `shown` in the user's own
    nvim, a file the follower never touched must behave exactly as before:
    the swap check still fires for it. A global `set noswapfile` (the obvious
    wrong fix) would load `untouched` silently and turn this red."""
    shown = tmp_path / "shown.py"
    shown.write_text("shown\n")
    untouched = tmp_path / "untouched.py"
    untouched.write_text("untouched\n")

    owner_sock = swap_aware_nvim()
    owner = _owner_holding(owner_sock, shown)
    owner.command(f"split {untouched}")
    assert owner.funcs.swapname(owner.funcs.bufnr(str(untouched)))

    follower_sock = swap_aware_nvim()
    follower = NvimFollower(socket_path=follower_sock, window_id="@1", pace_seconds=0.0)
    state.FollowerState.set("@1", "nvim", follower_sock, adopted=True)
    follower.ensure_showing(str(shown))

    nvim = pynvim.attach("socket", path=follower_sock)
    assert nvim.api.get_option_value("swapfile", {"scope": "global"}) is True
    fresh = nvim.funcs.bufadd(str(untouched))
    assert nvim.api.buf_get_option(fresh, "swapfile") is True
    with pytest.raises(pynvim.NvimError, match="E325"):
        nvim.funcs.bufload(fresh)
