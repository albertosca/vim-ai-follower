"""`claude-follow stop` gives an ADOPTED Vim back everything the follower
claimed in it (final review I5 and the Wave A paste residual): the readonly
and 'nomodifiable' a Read put on the user's buffers, an Edit relock's
'nomodifiable', and the user's 'paste', which a paused animation leaves on.

A stop that finds the user TYPING sends nothing (their keys would land in
the follower's calls), so it leaves a "release pending" mark for that Vim;
the next contact with it while it is not typing runs the release first.

Real CLI, real tmux, real Vims, `HOME` isolated by the e2e harness. The
user's Vim reports its own options through a VIMINIT-loaded timer, so
nothing is typed to observe it."""

from __future__ import annotations

import os
from collections.abc import Callable, Iterator
from pathlib import Path

import pytest
from e2e_harness import E2EFollower, e2e_world, payload

from vim_ai_follower.state import FollowerState, clear_stopped

pytestmark = pytest.mark.integration


@pytest.fixture
def world() -> Iterator[E2EFollower]:
    with e2e_world() as live:
        yield live


def _users_vim(world: E2EFollower, users_file: Path, claudes_file: Path) -> tuple[str, Path]:
    """The user's own Vim beside the origin pane: (pane id, the file its
    observer timer rewrites every 30 ms with its mode, 'paste', and the
    readonly and modifiable of Claude's buffer)."""
    observed = world.workdir / "observed.txt"
    world.set_vimrc(
        "set nocompatible ttimeout ttimeoutlen=10 noswapfile showmode\n"
        + f"let g:claudes = '{os.path.realpath(claudes_file)}'\n"
        + f"let g:observed = '{observed}'\n"
        + "function! Observe(timer) abort\n"
        + "  let number = bufnr(g:claudes)\n"
        + "  call writefile([mode(1), &paste,"
        + " getbufvar(number, '&readonly'), getbufvar(number, '&modifiable'),"
        + " fnamemodify(bufname('%'), ':p'), 'END'], g:observed)\n"
        + "endfunction\n"
        + "call timer_start(30, 'Observe', {'repeat': -1})\n"
    )
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
        str(users_file),
    )
    pane_id = created.stdout.strip()
    world.wait_until(lambda: _observed(observed) is not None, "the user's Vim", timeout=15.0)
    return pane_id, observed


def _observed(observed: Path) -> dict[str, str] | None:
    try:
        lines = observed.read_text().splitlines()
    except OSError:
        return None
    if len(lines) != 6 or lines[-1] != "END":
        return None
    return dict(zip(("mode", "paste", "readonly", "modifiable", "buffer"), lines, strict=False))


def _observation(
    world: E2EFollower,
    observed: Path,
    matches: Callable[[dict[str, str]], bool],
    what: str,
    timeout: float = 10.0,
) -> dict[str, str]:
    found: list[dict[str, str]] = []

    def matching() -> bool:
        report = _observed(observed)
        if report is not None and matches(report):
            found.append(report)
            return True
        return False

    world.wait_until(matching, what, timeout=timeout)
    return found[-1]


def _adopted_state(world: E2EFollower, pane_id: str, speed: str, *open_files: Path) -> None:
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
        speed=speed,
        base_directory=world.cache_dir,
    )


def _hook_log(world: E2EFollower) -> str:
    log_path = world.cache_dir / "hook.log"
    return log_path.read_text() if log_path.exists() else ""


def _read(world: E2EFollower, path: Path) -> None:
    read = world.cli("hook", "post", stdin=payload("Read", path))
    assert read.returncode == 0, read.stderr


def test_a_stop_after_a_pause_gives_the_users_paste_back(world: E2EFollower) -> None:
    users_file = world.workdir / "a.txt"
    users_file.write_text("aaa\n")
    claudes_file = world.workdir / "file.py"
    claudes_file.write_text("one\n")
    pane_id, observed = _users_vim(world, users_file, claudes_file)
    _adopted_state(world, pane_id, "lento")
    world.cli("hook", "pre", stdin=payload("Write", claudes_file))
    claudes_file.write_text("".join(f"line_{index:03d} = {index}\n" for index in range(60)))
    post = world.cli_background("hook", "post", stdin=payload("Write", claudes_file))
    _observation(world, observed, lambda report: report["paste"] == "1", "the animation's paste")
    world.wait_for_animating("running")
    world.cli("pause")
    world.wait_for_animating("paused")
    _observation(world, observed, lambda report: report["mode"] == "n", "Normal mode while paused")

    stopped = world.cli("stop")
    assert stopped.returncode == 0, stopped.stderr
    world.wait_for_hook_exit(post, timeout=10.0)

    report = _observation(
        world, observed, lambda report: report["paste"] == "0", "the user's paste back"
    )
    assert report["mode"] == "n", report
    assert "Traceback" not in _hook_log(world)


def test_a_stop_while_the_user_types_releases_at_the_next_contact(world: E2EFollower) -> None:
    users_file = world.workdir / "a.txt"
    users_file.write_text("aaa\n")
    claudes_file = world.workdir / "file.py"
    claudes_file.write_text("one\n")
    other_file = world.workdir / "other.py"
    other_file.write_text("other\n")
    pane_id, observed = _users_vim(world, users_file, claudes_file)
    _adopted_state(world, pane_id, "instant")
    _read(world, claudes_file)
    _observation(
        world,
        observed,
        lambda report: report["readonly"] == "1" and report["modifiable"] == "0",
        "the Read's lock on Claude's buffer",
    )
    # The user goes back to their own file and types, staying in insert.
    world.tmux("send-keys", "-t", pane_id, "-l", ":tabfirst\r")
    world.tmux("send-keys", "-t", pane_id, "-l", "Ahalf")
    _observation(world, observed, lambda report: report["mode"] == "i", "the user in insert mode")

    stopped = world.cli("stop")
    assert stopped.returncode == 0, stopped.stderr
    log = _hook_log(world)
    waiting = [line for line in log.splitlines() if "waiting on its user" in line]
    assert len(waiting) == 1, log
    assert os.path.realpath(claudes_file) in waiting[0], waiting
    report = _observation(world, observed, lambda _report: True, "an observation")
    assert report["mode"] == "i", report
    assert (report["readonly"], report["modifiable"]) == ("1", "0"), report
    assert list(world.cache_dir.glob("release-*.txt")), "no release pending after the stop"

    # The user leaves insert mode; a new adopting start, then a Read of
    # another file: the first contact gives the old locks back.
    world.tmux("send-keys", "-t", pane_id, "Escape")
    _observation(world, observed, lambda report: report["mode"] == "n", "the user in Normal")
    clear_stopped(world.window_id, base_directory=world.cache_dir)  # as `start` does
    _adopted_state(world, pane_id, "instant")
    _read(world, other_file)
    report = _observation(
        world,
        observed,
        lambda report: report["readonly"] == "0" and report["modifiable"] == "1",
        "Claude's old buffer released",
    )
    assert report["buffer"] == os.path.realpath(other_file), report
    assert not list(world.cache_dir.glob("release-*.txt")), "the release mark stayed"
    assert "Traceback" not in _hook_log(world)
