"""A `claude-follow stop` that lands while a hook is still sending to an
ADOPTED tmux Vim leaves the user's buffers as the user had them.

The stop clears the state first and releases what the follower claimed, but
a hook already past its own checks goes on sending: a Read's lock sent after
the stop's release (final review), and a running animation whose own insert
mode the stop read as the user typing (sweep-4). The hook here runs as a
subprocess of the bundled source with one method wrapped in a barrier: it
writes `<workdir>/at-<name>`, then waits for `<workdir>/go-<name>`, so the
test can run `stop` at exactly that point.

Real CLI, real tmux, real Vims, `HOME` isolated by the e2e harness. The
user's Vim reports its own state through a VIMINIT-loaded timer, so
nothing is typed to observe it."""

from __future__ import annotations

import os
import subprocess
import sys
import time
from collections.abc import Callable, Iterator
from pathlib import Path

import pytest
from e2e_harness import REPO_ROOT, E2EFollower, e2e_world, payload

from vim_ai_follower.state import FollowerState

pytestmark = pytest.mark.integration

_SETTLE_SECONDS = 1.0

_BARRIER = """
import sys, time
from pathlib import Path
from vim_ai_follower.backends import tmux_vim
from vim_ai_follower import cli

workdir, name = Path(sys.argv[1]), sys.argv[2]
original = getattr(tmux_vim.TmuxVimFollower, name)

def held(self, *arguments, **keywords):
    (workdir / f"at-{name}").write_text("")
    deadline = time.monotonic() + 30
    while not (workdir / f"go-{name}").exists() and time.monotonic() < deadline:
        time.sleep(0.02)
    return original(self, *arguments, **keywords)

setattr(tmux_vim.TmuxVimFollower, name, held)
sys.exit(cli.main(sys.argv[3:]))
"""


@pytest.fixture
def world() -> Iterator[E2EFollower]:
    with e2e_world() as live:
        yield live


def _users_vim(world: E2EFollower, claudes_file: Path) -> tuple[str, Path, Path]:
    """The user's own Vim beside the origin pane, Claude's file already open
    in it: (pane id, the file its observer timer rewrites every 30 ms with
    its mode and the readonly and modifiable of Claude's buffer, the file the
    same timer writes that buffer's lines to)."""
    observed = world.workdir / "observed.txt"
    content = world.workdir / "content.txt"
    world.set_vimrc(
        "set nocompatible ttimeout ttimeoutlen=10 noswapfile showmode\n"
        + f"let g:claudes = '{os.path.realpath(claudes_file)}'\n"
        + f"let g:observed = '{observed}'\n"
        + f"let g:content = '{content}'\n"
        + "function! Observe(timer) abort\n"
        + "  let number = bufnr(g:claudes)\n"
        + "  call writefile(getbufline(number, 1, '$'), g:content)\n"
        + "  call writefile([mode(1),"
        + " getbufvar(number, '&readonly'), getbufvar(number, '&modifiable'), 'END'],"
        + " g:observed)\n"
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
        str(claudes_file),
    )
    pane_id = created.stdout.strip()
    world.wait_until(lambda: _observed(observed) is not None, "the user's Vim", timeout=15.0)
    return pane_id, observed, content


def _observed(observed: Path) -> dict[str, str] | None:
    try:
        lines = observed.read_text().splitlines()
    except OSError:
        return None
    if len(lines) != 4 or lines[-1] != "END":
        return None
    return dict(zip(("mode", "readonly", "modifiable"), lines, strict=False))


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


def _settled(world: E2EFollower, observed: Path) -> dict[str, str]:
    """An observation written after everything the hook sent has run: the
    hook's last keys can still be in Vim's input when it exits, and the
    unlocked buffer it found is also what the user's Vim showed before."""
    observed.unlink()
    time.sleep(_SETTLE_SECONDS)
    observed.unlink(missing_ok=True)
    return _observation(world, observed, lambda _report: True, "an observation")


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


def _held_hook(world: E2EFollower, method: str, stdin: str) -> subprocess.Popen[bytes]:
    """`claude-follow hook post` with TmuxVimFollower.<method> held at the
    barrier (see the module docstring)."""
    log = (world.workdir / f"held-{method}.log").open("wb")
    process = subprocess.Popen(
        [
            sys.executable,
            "-P",
            "-c",
            _BARRIER,
            str(world.workdir),
            method,
            "hook",
            "post",
        ],
        stdin=subprocess.PIPE,
        stdout=log,
        stderr=subprocess.STDOUT,
        env={**world.env, "PYTHONPATH": str(REPO_ROOT / "src")},
    )
    log.close()
    assert process.stdin is not None
    process.stdin.write(stdin.encode())
    process.stdin.close()
    world.wait_until(
        lambda: (world.workdir / f"at-{method}").exists() or process.poll() is not None,
        f"the hook at {method}",
        timeout=20.0,
    )
    assert process.poll() is None, (world.workdir / f"held-{method}.log").read_text()
    return process


def _release(world: E2EFollower, method: str, process: subprocess.Popen[bytes]) -> None:
    (world.workdir / f"go-{method}").write_text("")
    assert process.wait(timeout=60) == 0, (world.workdir / f"held-{method}.log").read_text()


def _hook_log(world: E2EFollower) -> str:
    log_path = world.cache_dir / "hook.log"
    return log_path.read_text() if log_path.exists() else ""


def test_a_stop_before_a_reads_lock_leaves_the_buffer_unlocked(world: E2EFollower) -> None:
    claudes_file = world.workdir / "file.py"
    claudes_file.write_text("one\n")
    pane_id, observed, _ = _users_vim(world, claudes_file)
    _adopted_state(world, pane_id, "instant")
    hook = _held_hook(world, "_send_lock", payload("Read", claudes_file))

    stopped = world.cli("stop")
    assert stopped.returncode == 0, stopped.stderr
    _release(world, "_send_lock", hook)

    report = _settled(world, observed)
    assert (report["mode"], report["readonly"], report["modifiable"]) == ("n", "0", "1"), report
    assert not list(world.cache_dir.glob("release-*.txt")), "a release mark stayed"
    assert "Traceback" not in _hook_log(world)


def test_a_stop_during_a_running_adopted_animation_leaves_the_buffer_unlocked(
    world: E2EFollower,
) -> None:
    claudes_file = world.workdir / "file.py"
    claudes_file.write_text("one\n")
    pane_id, observed, content = _users_vim(world, claudes_file)
    _adopted_state(world, pane_id, "lento", claudes_file)
    world.cli("hook", "pre", stdin=payload("Write", claudes_file))
    lines = [f"line_{index:03d} = {index}" for index in range(40)]
    claudes_file.write_text("".join(f"{line}\n" for line in lines))
    post = world.cli_background("hook", "post", stdin=payload("Write", claudes_file))
    world.wait_for_animating("running")
    _observation(world, observed, lambda report: report["mode"] == "i", "the animation typing")

    stopped = world.cli("stop")
    assert stopped.returncode == 0, stopped.stderr
    world.wait_for_hook_exit(post, timeout=120.0)

    report = _settled(world, observed)
    assert (report["mode"], report["readonly"], report["modifiable"]) == ("n", "0", "1"), report
    world.wait_until(
        lambda: content.read_text().splitlines() == lines, "the buffer equal to the file"
    )
    assert not list(world.cache_dir.glob("release-*.txt")), "a release mark stayed"
    assert "waiting on its user" not in _hook_log(world)
    assert "Traceback" not in _hook_log(world)
