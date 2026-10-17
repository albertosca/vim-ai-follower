"""While the follower drives an animation, waiting for the user is right: a
hit-enter prompt raised mid-animation in an adopted nvim (a plugin's message,
the user's own `:ls`) holds the typing until Enter, and then it finishes —
the requests that type are not cut at the RPC deadline (review I2).

Real CLI, real tmux, real nvims in tmux panes, `HOME` isolated by the e2e
harness; every CLI run has a subprocess timeout so a regression FAILS."""

from __future__ import annotations

import os
import subprocess
import time
from collections.abc import Iterator
from pathlib import Path

import pytest
from e2e_harness import CLI, E2EFollower, e2e_world, payload

from vim_ai_follower.backends.nvim_connect import probe_nvim
from vim_ai_follower.state import FollowerState

pytestmark = pytest.mark.integration

_HANG_GUARD_SECONDS = 120.0


@pytest.fixture
def world() -> Iterator[E2EFollower]:
    with e2e_world() as live:
        yield live


def _timed_cli(
    world: E2EFollower, *args: str, pane_id: str, stdin: str | None = None
) -> tuple[subprocess.CompletedProcess[str], float]:
    started = time.monotonic()
    result = subprocess.run(
        [str(CLI), *args],
        input=stdin,
        env=world.env_with(TMUX_PANE=pane_id),
        capture_output=True,
        text=True,
        check=False,
        timeout=_HANG_GUARD_SECONDS,
    )
    return result, time.monotonic() - started


def _nvim_window(world: E2EFollower, socket_path: Path, *files: Path) -> tuple[str, str]:
    """A new tmux window running a real (TUI) nvim listening on socket_path:
    (window id, pane id), once it answers."""
    created = world.tmux(
        "new-window",
        "-d",
        "-P",
        "-F",
        "#{window_id} #{pane_id}",
        "nvim",
        "-n",
        "-u",
        "NONE",
        "-i",
        "NONE",
        "--listen",
        str(socket_path),
        *(str(file) for file in files),
    )
    window_id, pane_id = created.stdout.split()
    world.wait_until(
        lambda: probe_nvim(str(socket_path)) == "answers", f"nvim on {socket_path}", timeout=15.0
    )
    return window_id, pane_id


def test_a_hit_enter_prompt_mid_animation_holds_the_typing_until_enter(
    world: E2EFollower,
) -> None:
    socket_path = world.cache_dir / "nvim-adopted.sock"
    world.cache_dir.mkdir(parents=True, exist_ok=True)
    target = world.workdir / "file.py"
    before = "".join(f"line {number}\n" for number in range(1, 11))
    after = before.replace(
        "line 5\n",
        "line 5\n" + "".join(f"NEW {number} with some text here\n" for number in range(1, 7)),
    )
    target.write_text(before)
    window_id, pane_id = _nvim_window(world, socket_path, target)
    FollowerState.set(
        window_id,
        "nvim",
        str(socket_path),
        origin=pane_id,
        adopted=True,
        shown_any=True,
        open_files=(str(target),),
        speed="lento",
        base_dir=world.cache_dir,
    )
    edit = payload("Edit", target)
    pre, _seconds = _timed_cli(world, "hook", "pre", pane_id=pane_id, stdin=edit)
    assert pre.returncode == 0, pre.stderr
    target.write_text(after)
    # A plugin's multi-line message 1.5 s into the animation: a hit-enter
    # prompt that blocks every request but nvim_get_mode until Enter.
    world._nvim(str(socket_path)).exec_lua(
        "vim.defer_fn(function() vim.cmd('echo \"plugin says\\\\nsomething\\\\nlong\"') end, 1500)",
        [],
    )
    hook = subprocess.Popen(
        [str(CLI), "hook", "post"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=world.env_with(TMUX_PANE=pane_id),
    )
    try:
        assert hook.stdin is not None
        hook.stdin.write(edit.encode())
        hook.stdin.close()
        # Hold the prompt well past the RPC deadline, then answer it.
        world.wait_until(
            lambda: world._nvim(str(socket_path)).api.get_mode()["blocking"],
            "the hit-enter prompt",
            timeout=10.0,
        )
        time.sleep(12.0)
        assert hook.poll() is None, "the hook gave up while the user had a prompt up"
        world.tmux("send-keys", "-t", pane_id, "Enter")
        hook.wait(timeout=_HANG_GUARD_SECONDS)
    finally:
        if hook.poll() is None:
            hook.kill()
            hook.wait(timeout=10)
    assert hook.returncode == 0
    observer = world._nvim(str(socket_path))
    buffer_number = observer.call("bufnr", str(target))
    lines = observer.api.buf_get_lines(buffer_number, 0, -1, False)
    assert lines == after.splitlines()
    floats = [
        observer.api.buf_get_lines(observer.api.win_get_buf(window), 0, -1, False)
        for window in observer.api.list_wins()
        if observer.api.win_get_config(window).get("relative")
    ]
    assert not any("Writing" in line for text in floats for line in text), floats
    log_path = world.cache_dir / "hook.log"
    log = log_path.read_text() if log_path.exists() else ""
    assert "crash" not in log.lower(), log
    assert "Traceback" not in log, log


def test_a_prompt_already_up_when_the_hook_starts_skips_the_edit_in_one_line(
    world: E2EFollower,
) -> None:
    # Review m1 of T16: the user's nvim shows a hit-enter prompt before the
    # hook connects. The probe's answer says `blocking: true`, so the hook
    # skips at once, logs one line and marks the file stale — not three
    # 10 s timeouts (cue, base probe, cue refresh) and a traceback.
    socket_path = world.cache_dir / "nvim-adopted.sock"
    world.cache_dir.mkdir(parents=True, exist_ok=True)
    target = world.workdir / "file.py"
    before = "one\ntwo\n"
    target.write_text(before)
    window_id, pane_id = _nvim_window(world, socket_path, target)
    FollowerState.set(
        window_id,
        "nvim",
        str(socket_path),
        origin=pane_id,
        adopted=True,
        shown_any=True,
        # The hook names the file by its realpath (/tmp is /private/tmp).
        open_files=(os.path.realpath(target),),
        base_dir=world.cache_dir,
    )
    edit = payload("Edit", target)
    pre, _seconds = _timed_cli(world, "hook", "pre", pane_id=pane_id, stdin=edit)
    assert pre.returncode == 0, pre.stderr
    target.write_text("one\nTWO\n")
    world._nvim(str(socket_path)).exec_lua(
        "vim.schedule(function() vim.cmd('echo \"plugin says\\\\nsomething\\\\nlong\"') end)", []
    )
    world.wait_until(
        lambda: world._nvim(str(socket_path)).api.get_mode()["blocking"],
        "the hit-enter prompt",
        timeout=10.0,
    )
    post, seconds = _timed_cli(world, "hook", "post", pane_id=pane_id, stdin=edit)
    try:
        assert post.returncode == 0, post.stderr
        assert seconds < 8.0, f"the hook waited {seconds:.1f} s on a prompt already up"
        log_path = world.cache_dir / "hook.log"
        log = log_path.read_text() if log_path.exists() else ""
        assert "Traceback" not in log, log
        skipped = [line for line in log.splitlines() if "is waiting on its user" in line]
        assert len(skipped) == 1, log
        after = FollowerState.read(window_id, base_dir=world.cache_dir)
        assert after is not None and after.stale_files == (os.path.realpath(target),)
    finally:
        world.tmux("send-keys", "-t", pane_id, "Enter")
    observer = world._nvim(str(socket_path))
    lines = observer.api.buf_get_lines(observer.call("bufnr", str(target)), 0, -1, False)
    assert lines == before.splitlines()  # nothing typed into the user's buffer
