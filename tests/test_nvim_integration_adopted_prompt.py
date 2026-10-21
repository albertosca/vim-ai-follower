"""An adopted nvim's hit-enter prompt meets the follower the way a Vim's
meets its Escape pair (decision (a)): raised mid-animation (a plugin's
message), the watchdog answers it and the typing finishes; already up when a
hook starts, the hook sweeps it and goes on. Blocked in any other way (keys
pending), or with its user at a confirm or on the command line, the hook
skips in one line and types nothing.

Real CLI, real tmux, real nvims in tmux panes, `HOME` isolated by the e2e
harness; every CLI run has a subprocess timeout so a regression FAILS."""

from __future__ import annotations

import os
import subprocess
import time
from collections.abc import Iterator
from pathlib import Path

import pytest
from e2e_harness import (
    CLI,
    E2EFollower,
    attached_client,
    e2e_world,
    payload,
    wait_for_take_over_question,
)

from vim_ai_follower.backends.nvim_connect import probe_nvim
from vim_ai_follower.state import FollowerState

pytestmark = pytest.mark.integration

_HANG_GUARD_SECONDS = 120.0


@pytest.fixture
def world() -> Iterator[E2EFollower]:
    with e2e_world() as live:
        yield live


def _timed_cli(
    world: E2EFollower, *arguments: str, pane_id: str, stdin: str | None = None
) -> tuple[subprocess.CompletedProcess[str], float]:
    started = time.monotonic()
    result = subprocess.run(
        [str(CLI), *arguments],
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


def _hook_log(world: E2EFollower) -> str:
    log_path = world.cache_dir / "hook.log"
    return log_path.read_text() if log_path.exists() else ""


def _adopted_state(
    world: E2EFollower,
    window_id: str,
    pane_id: str,
    socket_path: Path,
    target: Path,
    speed: str = "rapido",
) -> None:
    FollowerState.set(
        window_id,
        "nvim",
        str(socket_path),
        origin=pane_id,
        server_pid=world.server_pid,
        adopted=True,
        shown_any=True,
        # The hook names the file by its realpath (/tmp is /private/tmp).
        open_files=(os.path.realpath(target),),
        speed=speed,
        base_directory=world.cache_dir,
    )


def _buffer_lines(world: E2EFollower, socket_path: Path, target: Path) -> list[str]:
    observer = world._nvim(str(socket_path))
    lines: list[str] = observer.api.buf_get_lines(observer.call("bufnr", str(target)), 0, -1, False)
    return lines


def test_a_hit_enter_prompt_mid_animation_is_answered_and_the_typing_finishes(
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
    _adopted_state(world, window_id, pane_id, socket_path, target, speed="lento")
    edit = payload("Edit", target)
    pre, _seconds = _timed_cli(world, "hook", "pre", pane_id=pane_id, stdin=edit)
    assert pre.returncode == 0, pre.stderr
    target.write_text(after)
    # A plugin's multi-line message 1.5 s into the animation: a hit-enter
    # prompt that blocks every request but nvim_get_mode until answered.
    world._nvim(str(socket_path)).exec_lua(
        "vim.defer_fn(function() vim.cmd('echo \"plugin says\\\\nsomething\\\\nlong\"') end, 1500)",
        [],
    )
    post, _seconds = _timed_cli(world, "hook", "post", pane_id=pane_id, stdin=edit)
    assert post.returncode == 0, post.stderr
    log = _hook_log(world)
    assert "dismissed a hit-enter prompt during" in log, log
    assert "Traceback" not in log, log
    assert "crash" not in log.lower(), log
    assert _buffer_lines(world, socket_path, target) == after.splitlines()
    assert world._nvim(str(socket_path)).api.get_mode()["mode"] == "n"
    observer = world._nvim(str(socket_path))
    floats = [
        observer.api.buf_get_lines(observer.api.win_get_buf(window), 0, -1, False)
        for window in observer.api.list_wins()
        if observer.api.win_get_config(window).get("relative")
    ]
    assert not any("Writing" in line for text in floats for line in text), floats


def test_a_prompt_already_up_when_the_hook_starts_is_swept_and_the_edit_goes_on(
    world: E2EFollower,
) -> None:
    # Was T16 review m1's skip: a hit-enter prompt up before the hook
    # connects. Now the hook answers it first (as the Vim's Escape pair
    # would), raises the cue and types the edit — no 10 s timeouts.
    socket_path = world.cache_dir / "nvim-adopted.sock"
    world.cache_dir.mkdir(parents=True, exist_ok=True)
    target = world.workdir / "file.py"
    target.write_text("one\ntwo\n")
    window_id, pane_id = _nvim_window(world, socket_path, target)
    _adopted_state(world, window_id, pane_id, socket_path, target)
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
    assert post.returncode == 0, post.stderr
    assert seconds < 8.0, f"the hook waited {seconds:.1f} s on a prompt already up"
    log = _hook_log(world)
    assert "Traceback" not in log, log
    assert "is waiting on its user" not in log, log
    assert "left before the hook (sweep)" in log, log
    assert _buffer_lines(world, socket_path, target) == ["one", "TWO"]
    state = FollowerState.read(window_id, base_directory=world.cache_dir)
    assert state is not None and state.stale_files == ()


def test_an_nvim_blocked_in_a_key_sequence_skips_the_edit_in_one_line(
    world: E2EFollower,
) -> None:
    # `q` waiting for its register: blocking, mode "n" — nothing the
    # follower answers. The hook skips at once (T16 review m1), logs one
    # line, marks the file stale and types nothing.
    socket_path = world.cache_dir / "nvim-adopted.sock"
    world.cache_dir.mkdir(parents=True, exist_ok=True)
    target = world.workdir / "file.py"
    before = "one\ntwo\n"
    target.write_text(before)
    window_id, pane_id = _nvim_window(world, socket_path, target)
    _adopted_state(world, window_id, pane_id, socket_path, target)
    edit = payload("Edit", target)
    pre, _seconds = _timed_cli(world, "hook", "pre", pane_id=pane_id, stdin=edit)
    assert pre.returncode == 0, pre.stderr
    target.write_text("one\nTWO\n")
    world.tmux("send-keys", "-t", pane_id, "q")
    world.wait_until(
        lambda: world._nvim(str(socket_path)).api.get_mode() == {"mode": "n", "blocking": True},
        "the pending q",
        timeout=10.0,
    )
    try:
        post, seconds = _timed_cli(world, "hook", "post", pane_id=pane_id, stdin=edit)
        assert post.returncode == 0, post.stderr
        assert seconds < 8.0, f"the hook waited {seconds:.1f} s on a pending key"
        log = _hook_log(world)
        assert "Traceback" not in log, log
        skipped = [line for line in log.splitlines() if "is waiting on its user" in line]
        assert len(skipped) == 1, log
        assert "(sweep)" not in log, log
        state = FollowerState.read(window_id, base_directory=world.cache_dir)
        assert state is not None and state.stale_files == (os.path.realpath(target),)
    finally:
        world.tmux("send-keys", "-t", pane_id, "Escape")
    assert _buffer_lines(world, socket_path, target) == before.splitlines()


# The user in the middle of a confirm or a command line (task 22 review I1).
# Neither is `blocking` (measured 2026-10-17, nvim 0.12.6: requests complete
# while one is up), but the user's answer or Enter runs against whatever buffer
# is current WHEN it lands: a follower that opened Claude's file meanwhile had
# the user's `:s///c` substitute Claude's buffer, and their `:let` read Claude's
# name. Alberto's answer A: the edit is skipped, the file marked stale. An
# operator waiting for its motion (`d`: mode "no", not blocking either) is the
# same: the user's second `d` deleted Claude's line (T22 re-review I2).
_USER_PENDING = {
    # (keys typed into the user's nvim, "Enter" after them?, mode it waits in,
    #  keys that finish it, what the log names)
    "substitute-confirm": (":%s/aaa/ZZZ/gc", True, "r?", "a", "a confirm prompt"),
    "half-typed-command": (":let g:who = bufname('%')", False, "c", "Enter", "on the command line"),
    "input": (":let g:who = input('Name: ')", True, "c", "bob Enter", "on the command line"),
    "operator-pending": ("d", False, "no", "d", "an operator pending"),
}


def _wait_for_mode(world: E2EFollower, socket_path: Path, mode: str) -> None:
    world.wait_until(
        lambda: world._nvim(str(socket_path)).api.get_mode() == {"mode": mode, "blocking": False},
        f"the user's nvim in mode {mode}",
        timeout=10.0,
    )


@pytest.mark.parametrize("tool", ["Write", "Read"])
@pytest.mark.parametrize("pending", sorted(_USER_PENDING))
def test_an_nvim_whose_user_is_mid_confirm_or_command_line_is_skipped(
    world: E2EFollower, pending: str, tool: str
) -> None:
    keys, enter, mode, finish, named = _USER_PENDING[pending]
    socket_path = world.cache_dir / "nvim-adopted.sock"
    world.cache_dir.mkdir(parents=True, exist_ok=True)
    users_file = world.workdir / "a.txt"
    users_file.write_text("aaa\nbbb aaa\nccc\n")
    claudes_file = world.workdir / "new.py"
    claudes_file.write_text("def aaa():\n    return aaa\n")
    window_id, pane_id = _nvim_window(world, socket_path, users_file)
    _adopted_state(world, window_id, pane_id, socket_path, users_file)
    hook = payload(tool, claudes_file)
    if tool == "Write":
        pre, _seconds = _timed_cli(world, "hook", "pre", pane_id=pane_id, stdin=hook)
        assert pre.returncode == 0, pre.stderr
    world.tmux("send-keys", "-t", pane_id, keys, *(["Enter"] if enter else []))
    _wait_for_mode(world, socket_path, mode)
    try:
        post, seconds = _timed_cli(world, "hook", "post", pane_id=pane_id, stdin=hook)
        assert post.returncode == 0, post.stderr
        assert seconds < 8.0, f"the hook took {seconds:.1f} s"
        observer = world._nvim(str(socket_path))
        # The follower did not act: still the user's prompt, no buffer for
        # Claude's file, the user's buffer untouched.
        assert observer.call("bufnr", str(claudes_file)) == -1
        assert observer.api.get_mode() == {"mode": mode, "blocking": False}
        assert _buffer_lines(world, socket_path, users_file) == ["aaa", "bbb aaa", "ccc"]
        log = _hook_log(world)
        assert "Traceback" not in log, log
        skipped = [line for line in log.splitlines() if "is waiting on its user" in line]
        assert len(skipped) == 1, log
        assert f"({named})" in skipped[0], skipped
        world.tmux("send-keys", "-t", pane_id, *finish.split())
        world.wait_until(
            lambda: world._nvim(str(socket_path)).api.get_mode()["mode"] == "n",
            "the user's command finished",
            timeout=10.0,
        )
        observer = world._nvim(str(socket_path))
        if pending == "substitute-confirm":
            assert _buffer_lines(world, socket_path, users_file) == ["ZZZ", "bbb ZZZ", "ccc"]
        elif pending == "operator-pending":
            assert _buffer_lines(world, socket_path, users_file) == ["bbb aaa", "ccc"]
        elif pending == "half-typed-command":
            assert os.path.realpath(observer.eval("g:who")) == os.path.realpath(users_file)
        else:
            assert observer.eval("g:who") == "bob"
            assert os.path.realpath(observer.call("bufname", "%")) == os.path.realpath(users_file)
        assert claudes_file.read_text() == "def aaa():\n    return aaa\n"
    finally:
        world.tmux("send-keys", "-t", pane_id, "Escape")
        world.tmux("send-keys", "-t", pane_id, "Escape")


@pytest.mark.parametrize(("inside", "mode"), [("", "n"), ("i", "i"), ("v", "v")])
@pytest.mark.parametrize("tool", ["Edit", "Write"])
def test_an_nvim_whose_user_is_in_the_command_line_window_is_skipped(
    world: E2EFollower, tool: str, inside: str, mode: str
) -> None:
    # `q:` reads mode "n", not blocking, but every window change in it raises
    # E11: show_fresh crashed the hook with a traceback and left an empty
    # buffer behind (T22 re-review m2). An adopted nvim in the cmdwin is
    # skipped like one on the command line, whatever mode the user is in
    # inside it (`q:` then `i` read "in insert mode": review m1 of T24c).
    socket_path = world.cache_dir / "nvim-adopted.sock"
    world.cache_dir.mkdir(parents=True, exist_ok=True)
    users_file = world.workdir / "a.txt"
    users_file.write_text("aaa\n")
    claudes_file = world.workdir / "file.py"
    before = "one\ntwo\n"
    claudes_file.write_text(before)
    opened = (users_file, claudes_file) if tool == "Edit" else (users_file,)
    window_id, pane_id = _nvim_window(world, socket_path, *opened)
    tracked = claudes_file if tool == "Edit" else users_file
    _adopted_state(world, window_id, pane_id, socket_path, tracked)
    hook = payload(tool, claudes_file)
    pre, _seconds = _timed_cli(world, "hook", "pre", pane_id=pane_id, stdin=hook)
    assert pre.returncode == 0, pre.stderr
    claudes_file.write_text("one\nTWO\n")
    world.tmux("send-keys", "-t", pane_id, "q:")
    world.wait_until(
        lambda: world._nvim(str(socket_path)).call("getcmdwintype") == ":",
        "the user's command-line window",
        timeout=10.0,
    )
    if inside:
        world.tmux("send-keys", "-t", pane_id, inside)
    world.wait_until(
        lambda: world._nvim(str(socket_path)).api.get_mode()["mode"] == mode,
        f"mode {mode} inside the command-line window",
        timeout=10.0,
    )
    try:
        buffers_before = len(world._nvim(str(socket_path)).call("getbufinfo"))
        # `nvim a.txt file.py` loads only the first argument: file.py's buffer
        # is listed but unloaded (no lines) until something visits it.
        claudes_lines_before = (
            _buffer_lines(world, socket_path, claudes_file) if tool == "Edit" else []
        )
        post, seconds = _timed_cli(world, "hook", "post", pane_id=pane_id, stdin=hook)
        assert post.returncode == 0, post.stderr
        assert seconds < 8.0, f"the hook took {seconds:.1f} s"
        observer = world._nvim(str(socket_path))
        assert len(observer.call("getbufinfo")) == buffers_before
        assert observer.call("getcmdwintype") == ":"
        if tool == "Edit":
            assert _buffer_lines(world, socket_path, claudes_file) == claudes_lines_before
        else:
            assert observer.call("bufnr", str(claudes_file)) == -1
        log = _hook_log(world)
        assert "Traceback" not in log, log
        skipped = [line for line in log.splitlines() if "is waiting on its user" in line]
        assert len(skipped) == 1, log
        assert "(in the command-line window)" in skipped[0], skipped
        state = FollowerState.read(window_id, base_directory=world.cache_dir)
        assert state is not None
        if tool == "Edit":
            assert state.stale_files == (os.path.realpath(claudes_file),)
    finally:
        world.tmux("send-keys", "-t", pane_id, "C-c")
        world.tmux("send-keys", "-t", pane_id, "Escape")


@pytest.mark.parametrize("mode_key", ["A", "R"], ids=["insert", "replace"])
def test_an_nvim_whose_user_is_typing_is_skipped_and_the_edit_marked_stale(
    world: E2EFollower, mode_key: str
) -> None:
    # A navigation keeps insert or replace mode: the user's next keys were
    # typed into Claude's buffer (measured, nvim 0.12.6). Alberto 2026-10-17:
    # skip and mark. The user's half-typed text and their mode survive, and
    # the Edit marks the file stale.
    socket_path = world.cache_dir / "nvim-adopted.sock"
    world.cache_dir.mkdir(parents=True, exist_ok=True)
    users_file = world.workdir / "a.txt"
    users_file.write_text("aaa\n")
    claudes_file = world.workdir / "file.py"
    claudes_file.write_text("one\ntwo\n")
    window_id, pane_id = _nvim_window(world, socket_path, users_file, claudes_file)
    _adopted_state(world, window_id, pane_id, socket_path, claudes_file)
    hook = payload("Edit", claudes_file)
    pre, _seconds = _timed_cli(world, "hook", "pre", pane_id=pane_id, stdin=hook)
    assert pre.returncode == 0, pre.stderr
    claudes_file.write_text("one\nTWO\n")
    world.tmux("send-keys", "-t", pane_id, mode_key)
    world.tmux("send-keys", "-t", pane_id, "-l", "half")
    mode = "i" if mode_key == "A" else "R"
    _wait_for_mode(world, socket_path, mode)
    world.wait_until(
        lambda: _buffer_lines(world, socket_path, users_file) != ["aaa"],
        "the user's half-typed text",
        timeout=10.0,
    )
    typed = _buffer_lines(world, socket_path, users_file)
    try:
        claudes_lines_before = _buffer_lines(world, socket_path, claudes_file)
        post, seconds = _timed_cli(world, "hook", "post", pane_id=pane_id, stdin=hook)
        assert post.returncode == 0, post.stderr
        assert seconds < 8.0, f"the hook took {seconds:.1f} s"
        observer = world._nvim(str(socket_path))
        assert observer.api.get_mode() == {"mode": mode, "blocking": False}
        assert os.path.realpath(observer.call("bufname", "%")) == os.path.realpath(users_file)
        assert _buffer_lines(world, socket_path, users_file) == typed
        assert _buffer_lines(world, socket_path, claudes_file) == claudes_lines_before
        log = _hook_log(world)
        assert "Traceback" not in log, log
        skipped = [line for line in log.splitlines() if "is waiting on its user" in line]
        assert len(skipped) == 1, log
        named = "in insert mode" if mode_key == "A" else "in replace mode"
        assert f"({named})" in skipped[0], skipped
        state = FollowerState.read(window_id, base_directory=world.cache_dir)
        assert state is not None
        assert state.stale_files == (os.path.realpath(claudes_file),)
    finally:
        world.tmux("send-keys", "-t", pane_id, "Escape")


def _question_seconds(world: E2EFollower, seconds: float) -> None:
    config_directory = world.home / ".config" / "claude-vim-follower"
    config_directory.mkdir(parents=True, exist_ok=True)
    (config_directory / "config.json").write_text(f'{{"take_over_question_seconds": {seconds}}}')


@pytest.mark.parametrize("answer", ["y", "n", "none", "y-in-flight"])
def test_an_nvim_whose_user_is_typing_is_asked_and_only_y_takes_it_over(
    world: E2EFollower, answer: str
) -> None:
    # Alberto 2026-10-17: a user known to be typing is ASKED, in a popup over
    # the follower's window. `y` takes the editor over (Normal mode, then the
    # edit animates); `n`, or no answer within the configured time, skips and
    # marks the file stale, leaving the half-typed text and the mode alone.
    # A `y` typed as the question opens (the user still typing) is a "no".
    _question_seconds(world, 3)
    socket_path = world.cache_dir / "nvim-adopted.sock"
    world.cache_dir.mkdir(parents=True, exist_ok=True)
    users_file = world.workdir / "a.txt"
    users_file.write_text("aaa\n")
    claudes_file = world.workdir / "file.py"
    claudes_file.write_text("one\ntwo\n")
    window_id, pane_id = _nvim_window(world, socket_path, users_file, claudes_file)
    _adopted_state(world, window_id, pane_id, socket_path, claudes_file, speed="instant")
    hook = payload("Edit", claudes_file)
    pre, _seconds = _timed_cli(world, "hook", "pre", pane_id=pane_id, stdin=hook)
    assert pre.returncode == 0, pre.stderr
    claudes_file.write_text("one\nTWO\n")
    world.tmux("send-keys", "-t", pane_id, "A")
    world.tmux("send-keys", "-t", pane_id, "-l", "half")
    _wait_for_mode(world, socket_path, "i")
    world.wait_until(
        lambda: _buffer_lines(world, socket_path, users_file) == ["aaahalf"],
        "the user's half-typed text",
        timeout=10.0,
    )
    claudes_lines_before = _buffer_lines(world, socket_path, claudes_file)
    try:
        with attached_client(world) as client:
            started = time.monotonic()
            post = world.cli_background(
                "hook", "post", stdin=hook, environment=world.env_with(TMUX_PANE=pane_id)
            )
            wait_for_take_over_question(world, armed=answer != "y-in-flight")
            if answer != "none":
                client.type(answer[0].encode())
            world.wait_for_hook_exit(post, timeout=30.0)
            seconds = time.monotonic() - started
        assert post.returncode == 0, world.background_log(post)
        log = _hook_log(world)
        assert "Traceback" not in log, log
        observer = world._nvim(str(socket_path))
        state = FollowerState.read(window_id, base_directory=world.cache_dir)
        assert state is not None
        if answer == "y":
            assert "(in insert mode): taken over (answered y)" in log, log
            assert observer.api.get_mode() == {"mode": "n", "blocking": False}
            assert _buffer_lines(world, socket_path, claudes_file) == ["one", "TWO"]
            assert state.stale_files == ()
        else:
            named = "answered timeout" if answer == "none" else "answered no"
            skipped = [line for line in log.splitlines() if "is waiting on its user" in line]
            assert len(skipped) == 1, log
            assert f"(in insert mode), not taken over ({named})" in skipped[0], skipped
            assert observer.api.get_mode() == {"mode": "i", "blocking": False}
            assert os.path.realpath(observer.call("bufname", "%")) == os.path.realpath(users_file)
            assert _buffer_lines(world, socket_path, users_file) == ["aaahalf"]
            assert _buffer_lines(world, socket_path, claudes_file) == claudes_lines_before
            assert state.stale_files == (os.path.realpath(claudes_file),)
            if answer == "none":
                assert seconds >= 3.0, f"the question closed after {seconds:.1f} s"
    finally:
        world.tmux("send-keys", "-t", pane_id, "Escape")


_STORM_LUA = """
local stop_file = ...
vim.g.storm_answered = 0
local message = string.rep('storm line\\n', 80)
vim.fn.timer_start(150, function(timer)
  if vim.uv.fs_stat(stop_file) then
    vim.fn.timer_stop(timer)
    return
  end
  vim.api.nvim_echo({{message}}, false, {})
  vim.g.storm_answered = vim.g.storm_answered + 1
end, {['repeat'] = -1})
"""


def test_a_prompt_storm_is_skipped_after_three_rounds(world: E2EFollower) -> None:
    # T22 review M3: a plugin raising a more-prompt every 150 ms. The sweep
    # answered ~25 of them into the user's editor and an adopted navigation
    # took 15 s. Now the sweep answers at most three rounds, then the hook
    # skips (A3 = A) and leaves the prompt to its user.
    socket_path = world.cache_dir / "nvim-adopted.sock"
    world.cache_dir.mkdir(parents=True, exist_ok=True)
    target = world.workdir / "file.py"
    target.write_text("one\ntwo\n")
    stop_file = world.workdir / "stop-the-storm"
    window_id, pane_id = _nvim_window(world, socket_path, target)
    _adopted_state(world, window_id, pane_id, socket_path, target)
    edit = payload("Edit", target)
    pre, _seconds = _timed_cli(world, "hook", "pre", pane_id=pane_id, stdin=edit)
    assert pre.returncode == 0, pre.stderr
    target.write_text("one\nTWO\n")
    user = world._nvim(str(socket_path))
    user.exec_lua(_STORM_LUA, str(stop_file))
    world.wait_until(
        lambda: user.api.get_mode()["blocking"], "the first storm prompt", timeout=10.0
    )
    try:
        post, seconds = _timed_cli(world, "hook", "post", pane_id=pane_id, stdin=edit)
    finally:
        stop_file.touch()
    assert post.returncode == 0, post.stderr
    log = _hook_log(world)
    assert "Traceback" not in log, log
    assert seconds < 3.0, f"the hook took {seconds:.1f} s in the storm\n{log}"
    assert "keeps raising prompts" in log, log
    # The test answers the prompt still up, then counts the rounds answered.
    for _attempt in range(20):
        if not user.api.get_mode()["blocking"]:
            break
        user.api.input("<Esc>")
        time.sleep(0.1)
    answered = user.vars["storm_answered"]
    assert answered <= 3 + 1, f"{answered} storm prompts answered (≤ 3 by the hook + 1 here)"
    state = FollowerState.read(window_id, base_directory=world.cache_dir)
    assert state is not None and state.stale_files == (os.path.realpath(target),)
    assert _buffer_lines(world, socket_path, target) == ["one", "two"]
