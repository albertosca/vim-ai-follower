"""What a completed tmux animation leaves the user: their own global 'paste'
(the relock used to turn it off whatever the user had), and, in an ADOPTED
Vim, a 'nomodifiable' the user set while the follower's relock claim was
held (the release used to turn 'modifiable' back on).

Real CLI, real tmux, real Vims, `HOME` isolated by the e2e harness. Each Vim
reports its own state through a VIMINIT-loaded timer, so nothing is typed
to observe it."""

from __future__ import annotations

import os
import time
from collections.abc import Iterator
from pathlib import Path

import pytest
from e2e_harness import E2EFollower, e2e_world, payload

from vim_ai_follower.state import FollowerState

pytestmark = pytest.mark.integration

_SETTLE_SECONDS = 1.0


@pytest.fixture
def world() -> Iterator[E2EFollower]:
    with e2e_world() as live:
        yield live


def _observing_vimrc(world: E2EFollower, claudes_file: Path) -> Path:
    """A vimrc with the user's 'paste' on and a 30 ms timer that writes the
    global 'paste' and the 'modifiable' of Claude's buffer to a file."""
    observed = world.workdir / "observed.txt"
    world.set_vimrc(
        "set nocompatible ttimeout ttimeoutlen=10 noswapfile paste\n"
        + f"let g:claudes = '{os.path.realpath(claudes_file)}'\n"
        + f"let g:observed = '{observed}'\n"
        + "function! Observe(timer) abort\n"
        + "  let number = bufnr(g:claudes)\n"
        + "  call writefile([&paste, getbufvar(number, '&modifiable', -1), 'END'], g:observed)\n"
        + "endfunction\n"
        + "call timer_start(30, 'Observe', {'repeat': -1})\n"
    )
    return observed


def _observed(observed: Path) -> dict[str, str] | None:
    try:
        lines = observed.read_text().splitlines()
    except OSError:
        return None
    if len(lines) != 3 or lines[-1] != "END":
        return None
    return {"paste": lines[0], "modifiable": lines[1]}


def _settled(world: E2EFollower, observed: Path) -> dict[str, str]:
    """An observation written after everything the follower sent has run."""
    observed.unlink(missing_ok=True)
    time.sleep(_SETTLE_SECONDS)
    observed.unlink(missing_ok=True)
    found: list[dict[str, str]] = []

    def written() -> bool:
        report = _observed(observed)
        if report is not None:
            found.append(report)
        return report is not None

    world.wait_until(written, "an observation", timeout=10.0)
    return found[-1]


def _users_vim(world: E2EFollower, claudes_file: Path) -> str:
    """The user's own Vim beside the origin pane, Claude's file open in it."""
    created = world.tmux(
        "split-window",
        "-d",
        "-h",
        "-t",
        world.origin_pane,
        "-P",
        "-F",
        "#{pane_id}",
        "vim",
        "-i",
        "NONE",
        str(claudes_file),
    )
    return created.stdout.strip()


def _adopt(world: E2EFollower, pane_id: str, *open_files: Path) -> None:
    """What an adopting `start` writes for the user's Vim."""
    FollowerState.set(
        world.window_id,
        "tmux",
        pane_id,
        origin=world.origin_pane,
        server_pid=world.server_pid,
        adopted=True,
        shown_any=True,
        open_files=tuple(os.path.realpath(path) for path in open_files),
        speed="instant",
        base_directory=world.cache_dir,
    )


def _edit_through_hooks(world: E2EFollower, path: Path, content: str) -> None:
    world.cli("hook", "pre", stdin=payload("Write", path))
    path.write_text(content)
    world.cli("hook", "post", stdin=payload("Write", path))


def _hook_log(world: E2EFollower) -> str:
    log_path = world.cache_dir / "hook.log"
    return log_path.read_text() if log_path.exists() else ""


@pytest.mark.parametrize("adopted", [False, True], ids=["dedicated", "adopted"])
def test_a_completed_animation_gives_the_users_paste_back(
    world: E2EFollower, adopted: bool
) -> None:
    claudes_file = world.workdir / "file.py"
    claudes_file.write_text("one\n")
    observed = _observing_vimrc(world, claudes_file)
    if adopted:
        _adopt(world, _users_vim(world, claudes_file), claudes_file)
    else:
        world.start("tmux", speed="instant")
    world.wait_until(lambda: _observed(observed) is not None, "the Vim", timeout=15.0)
    assert _settled(world, observed)["paste"] == "1"

    _edit_through_hooks(world, claudes_file, "one\ntwo\n")

    report = _settled(world, observed)
    assert report["paste"] == "1", report
    assert "Traceback" not in _hook_log(world)


def test_a_nomodifiable_the_user_sets_during_a_claim_survives_release(
    world: E2EFollower,
) -> None:
    claudes_file = world.workdir / "file.py"
    claudes_file.write_text("one\n")
    observed = _observing_vimrc(world, claudes_file)
    pane_id = _users_vim(world, claudes_file)
    _adopt(world, pane_id, claudes_file)
    world.wait_until(lambda: _observed(observed) is not None, "the user's Vim", timeout=15.0)
    _edit_through_hooks(world, claudes_file, "one\ntwo\n")
    assert _settled(world, observed)["modifiable"] == "0", "the relock claim was not taken"

    world.tmux("send-keys", "-t", pane_id, ":setlocal nomodifiable", "Enter")
    _settled(world, observed)
    stopped = world.cli("stop")
    assert stopped.returncode == 0, stopped.stderr

    report = _settled(world, observed)
    assert report["modifiable"] == "0", report
    assert "Traceback" not in _hook_log(world)
