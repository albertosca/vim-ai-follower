"""An ADOPTED Vim whose user is typing is asked about before the follower
types anything into it (Alberto, 2026-10-17): a tmux popup, and only `y`
takes it over. `n`, or no answer within the configured time, skips the edit
and marks the file stale, leaving the user's half-typed text and their mode
alone.

How the hook knows, without typing a key (tmux_vim.typing_mode): once the
follower's script is sourced in that Vim, its ModeChanged autocommand writes
the mode to a file, and that file decides; on a first contact (no file) the
hook reads the screen, where 'showmode' puts `-- INSERT --` on the bottom row.

Real CLI, real tmux, real Vims in tmux panes, `HOME` isolated by the e2e
harness. The user's Vim reports its own state through a VIMINIT-loaded timer
(mode, current buffer, lines), so nothing is typed to observe it."""

from __future__ import annotations

import os
import time
from collections.abc import Callable, Iterator
from contextlib import ExitStack
from pathlib import Path

import pytest
from e2e_harness import (
    AttachedClient,
    E2EFollower,
    attached_client,
    e2e_world,
    payload,
    wait_for_take_over_question,
)

from vim_ai_follower.state import FollowerState

pytestmark = pytest.mark.integration


@pytest.fixture
def world() -> Iterator[E2EFollower]:
    with e2e_world() as live:
        yield live


def _users_vim(
    world: E2EFollower, users_file: Path, claudes_file: Path, *, showmode: bool
) -> tuple[str, Path]:
    """The user's own Vim, split beside the origin pane: (pane id, the file
    its observer timer rewrites every 30 ms)."""
    observed = world.workdir / "observed.txt"
    world.set_vimrc(
        "set nocompatible ttimeout ttimeoutlen=10 noswapfile\n"
        + ("set showmode\n" if showmode else "set noshowmode\n")
        + f"let g:claudes = '{os.path.realpath(claudes_file)}'\n"
        + f"let g:observed = '{observed}'\n"
        + "function! Observe(timer) abort\n"
        + "  call writefile([mode(1), fnamemodify(bufname('%'), ':p'),"
        + " join(getline(1, '$'), '|'),"
        + " join(getbufline(bufnr(g:claudes), 1, '$'), '|'),"
        + " join(map(term_list(), {_, number -> join(getbufline(number, 1, '$'), '/')}), '#'),"
        + " 'END'], g:observed)\n"
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
        str(claudes_file),
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
    return {
        "mode": lines[0],
        "buffer": lines[1],
        "lines": lines[2],
        "claudes": lines[3],
        "terminals": lines[4],
    }


def _observation(
    world: E2EFollower,
    observed: Path,
    matches: Callable[[dict[str, str]], bool],
    what: str,
    timeout: float,
) -> dict[str, str]:
    """Wait for an observation that `matches`, and return THAT one: a second
    read can land while the observer's writefile() has truncated the file
    (it failed `_settled` 1 run in 55, 2026-10-17)."""
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
    time.sleep(0.3)
    return _observation(world, observed, lambda _report: True, "an observation", 5.0)


def _adopted_state(world: E2EFollower, pane_id: str, claudes_file: Path) -> None:
    FollowerState.set(
        world.window_id,
        "tmux",
        pane_id,
        origin=world.origin_pane,
        server_pid=world.server_pid,
        adopted=True,
        shown_any=True,
        open_files=(os.path.realpath(claudes_file),),
        speed="instant",
        base_directory=world.cache_dir,
    )


def _question_seconds(world: E2EFollower, seconds: float) -> None:
    config_directory = world.home / ".config" / "claude-vim-follower"
    config_directory.mkdir(parents=True, exist_ok=True)
    (config_directory / "config.json").write_text(f'{{"take_over_question_seconds": {seconds}}}')


def _hook_log(world: E2EFollower) -> str:
    log_path = world.cache_dir / "hook.log"
    return log_path.read_text() if log_path.exists() else ""


def _type_half(
    world: E2EFollower, pane_id: str, observed: Path, users_file: Path
) -> dict[str, str]:
    """The user, back in their own file, appends `half` and stays in insert."""
    world.tmux("send-keys", "-t", pane_id, "-l", "Ahalf")
    report = _observation(
        world,
        observed,
        lambda report: report["lines"] == "aaahalf" and report["mode"] == "i",
        "the user's half-typed text, in insert mode",
        10.0,
    )
    assert report["buffer"] == os.path.realpath(users_file), report
    return report


def _edit_answered(
    world: E2EFollower,
    claudes_file: Path,
    answer: str,
    client: AttachedClient | None = None,
) -> tuple[float, FollowerState]:
    """Run Claude's Edit of claudes_file (one -> TWO) through the real hooks,
    answer the take-over question on `client` (default: a client attached
    for it; `none`: no key; `unasked`: no question is expected), and return
    how long the post hook took and the state after it."""
    hook = payload("Edit", claudes_file)
    pre = world.cli("hook", "pre", stdin=hook)
    assert pre.returncode == 0, pre.stderr
    claudes_file.write_text("one\nTWO\n")
    with ExitStack() as stack:
        if client is None:
            client = stack.enter_context(attached_client(world))
        started = time.monotonic()
        post = world.cli_background("hook", "post", stdin=hook)
        if answer != "unasked":
            wait_for_take_over_question(world, armed=answer != "y-in-flight")
        if answer not in ("none", "unasked"):
            client.type(answer[0].encode())
        world.wait_for_hook_exit(post, timeout=40.0)
        seconds = time.monotonic() - started
    assert post.returncode == 0, world.background_log(post)
    state = FollowerState.read(world.window_id, base_directory=world.cache_dir)
    assert state is not None
    return seconds, state


@pytest.mark.parametrize("answer", ["y", "n", "none", "y-in-flight"])
def test_a_vim_whose_user_is_typing_is_asked_and_only_y_takes_it_over(
    world: E2EFollower, answer: str
) -> None:
    # 'noshowmode', and no cursor shapes: the screen says nothing, so only
    # the mode file (written since the first hook sourced the script) can.
    _question_seconds(world, 3)
    users_file = world.workdir / "a.txt"
    users_file.write_text("aaa\n")
    claudes_file = world.workdir / "file.py"
    claudes_file.write_text("one\ntwo\n")
    pane_id, observed = _users_vim(world, users_file, claudes_file, showmode=False)
    _adopted_state(world, pane_id, claudes_file)
    # A first hook while the user is in Normal mode: acts as always, and
    # leaves the script (and its ModeChanged autocommand) in the user's Vim.
    read = world.cli("hook", "post", stdin=payload("Read", claudes_file))
    assert read.returncode == 0, read.stderr
    world.wait_until(
        lambda: (_observed(observed) or {}).get("buffer") == os.path.realpath(claudes_file),
        "the Read's navigation",
        timeout=10.0,
    )
    assert list(world.cache_dir.glob("mode-*.txt")), "no mode file after the first hook"
    world.tmux("send-keys", "-t", pane_id, "-l", ":tabfirst\r")
    world.wait_until(
        lambda: (_observed(observed) or {}).get("buffer") == os.path.realpath(users_file),
        "the user back in their file",
        timeout=10.0,
    )
    before = _type_half(world, pane_id, observed, users_file)
    try:
        seconds, state = _edit_answered(world, claudes_file, answer)
        log = _hook_log(world)
        assert "Traceback" not in log, log
        report = _settled(world, observed)
        if answer == "y":
            assert "(in insert mode): taken over (answered y)" in log, log
            assert report["mode"] == "n", report
            assert report["buffer"] == os.path.realpath(claudes_file), report
            assert report["claudes"] == "one|TWO", report
            assert state.stale_files == ()
        else:
            named = "answered timeout" if answer == "none" else "answered no"
            skipped = [line for line in log.splitlines() if "is waiting on its user" in line]
            assert len(skipped) == 1, log
            assert f"(in insert mode), not taken over ({named})" in skipped[0], skipped
            assert report["mode"] == "i", report
            assert report["buffer"] == os.path.realpath(users_file), report
            assert report["lines"] == "aaahalf", report
            assert report["claudes"] == before["claudes"] == "one|two", report
            assert state.stale_files == (os.path.realpath(claudes_file),)
            if answer == "none":
                assert seconds >= 3.0, f"the question closed after {seconds:.1f} s"
    finally:
        world.tmux("send-keys", "-t", pane_id, "Escape")


def test_a_first_contact_with_showmode_reads_the_screen_and_asks(world: E2EFollower) -> None:
    # No hook has reached this Vim yet, so it has no mode file: the screen
    # decides, and 'showmode' (Vim's default) puts `-- INSERT --` there.
    _question_seconds(world, 3)
    users_file = world.workdir / "a.txt"
    users_file.write_text("aaa\n")
    claudes_file = world.workdir / "file.py"
    claudes_file.write_text("one\ntwo\n")
    pane_id, observed = _users_vim(world, users_file, claudes_file, showmode=True)
    _adopted_state(world, pane_id, claudes_file)
    before = _type_half(world, pane_id, observed, users_file)
    assert not list(world.cache_dir.glob("mode-*.txt"))
    try:
        _seconds, state = _edit_answered(world, claudes_file, "n")
        log = _hook_log(world)
        assert "Traceback" not in log, log
        skipped = [line for line in log.splitlines() if "is waiting on its user" in line]
        assert len(skipped) == 1, log
        assert "(in insert mode), not taken over (answered no)" in skipped[0], skipped
        report = _settled(world, observed)
        assert report["mode"] == "i", report
        assert report["lines"] == "aaahalf", report
        # Listed but never loaded: no lines, before and after.
        assert report["claudes"] == before["claudes"] == "", report
        assert state.stale_files == (os.path.realpath(claudes_file),)
        # Nothing was typed, so the script was never sourced there.
        assert not list(world.cache_dir.glob("mode-*.txt"))
    finally:
        world.tmux("send-keys", "-t", pane_id, "Escape")


def test_a_yes_in_a_terminal_window_leaves_terminal_job_mode_first(world: E2EFollower) -> None:
    # In a terminal window's job, Escape goes to the job: without CTRL-\
    # CTRL-N first, the Escape pair and every call line would be typed into
    # the user's shell (tmux_vim.leave_typing_mode).
    _question_seconds(world, 3)
    users_file = world.workdir / "a.txt"
    users_file.write_text("aaa\n")
    claudes_file = world.workdir / "file.py"
    claudes_file.write_text("one\ntwo\n")
    pane_id, observed = _users_vim(world, users_file, claudes_file, showmode=False)
    _adopted_state(world, pane_id, claudes_file)
    read = world.cli("hook", "post", stdin=payload("Read", claudes_file))
    assert read.returncode == 0, read.stderr
    world.wait_until(
        lambda: (_observed(observed) or {}).get("buffer") == os.path.realpath(claudes_file),
        "the Read's navigation",
        timeout=10.0,
    )
    world.tmux("send-keys", "-t", pane_id, "-l", ":term\r")
    world.wait_until(
        lambda: (_observed(observed) or {}).get("mode") == "t",
        "the user in a terminal window's job",
        timeout=10.0,
    )
    try:
        _seconds, state = _edit_answered(world, claudes_file, "y")
        log = _hook_log(world)
        assert "Traceback" not in log, log
        assert "(in terminal mode): taken over (answered y)" in log, log
        report = _settled(world, observed)
        assert report["buffer"] == os.path.realpath(claudes_file), report
        assert report["claudes"] == "one|TWO", report
        assert state.stale_files == ()
        assert "exists(" not in report["terminals"], report
        assert "VafFollower" not in report["terminals"], report
    finally:
        world.tmux("send-keys", "-t", pane_id, "Escape")


@pytest.mark.parametrize(("keys", "mode"), [("C-n", "ic"), ("C-x", "ix")])
def test_a_first_contact_in_insert_completion_reads_the_screen_and_asks(
    world: E2EFollower, keys: str, mode: str
) -> None:
    # Review I1: completion and CTRL-X mode show `-- Keyword completion
    # (^N^P) ...` / `-- ^X mode (...)` on the bottom row, no mode word; with
    # no mode file yet, the follower took the Vim over without asking.
    _question_seconds(world, 3)
    users_file = world.workdir / "a.txt"
    users_file.write_text("aaa\n")
    claudes_file = world.workdir / "file.py"
    claudes_file.write_text("one\ntwo\n")
    pane_id, observed = _users_vim(world, users_file, claudes_file, showmode=True)
    _adopted_state(world, pane_id, claudes_file)
    world.tmux("send-keys", "-t", pane_id, "-l", "oa")
    world.tmux("send-keys", "-t", pane_id, keys)
    before = _observation(
        world,
        observed,
        lambda report: report["mode"] == mode,
        f"the user's Vim in mode {mode}",
        10.0,
    )
    assert before["buffer"] == os.path.realpath(users_file), before
    assert not list(world.cache_dir.glob("mode-*.txt"))
    try:
        _seconds, state = _edit_answered(world, claudes_file, "n")
        log = _hook_log(world)
        assert "Traceback" not in log, log
        skipped = [line for line in log.splitlines() if "is waiting on its user" in line]
        assert len(skipped) == 1, log
        assert "(in insert mode (completion)), not taken over (answered no)" in skipped[0]
        report = _settled(world, observed)
        assert report["mode"] == mode, report
        assert report["buffer"] == os.path.realpath(users_file), report
        assert report["lines"] == before["lines"], report
        assert state.stale_files == (os.path.realpath(claudes_file),)
    finally:
        world.tmux("send-keys", "-t", pane_id, "Escape")


def _typed_on_session(world: E2EFollower, session: str) -> None:
    """A second session for an unrelated client to sit on."""
    world.tmux("new-session", "-d", "-s", session, "-x", "80", "-y", "20")


def _activity(world: E2EFollower, client: AttachedClient) -> int:
    listed = world.tmux("list-clients", "-F", "#{client_pid} #{client_activity}").stdout
    for line in listed.splitlines():
        pid, _, activity = line.partition(" ")
        if pid == str(client.pid):
            return int(activity)
    raise AssertionError(listed)


def test_the_question_shows_on_the_targets_session_client_most_recently_active(
    world: E2EFollower,
) -> None:
    # Review m7: with no `-c`, the popup went to whichever client tmux chose:
    # one on an unrelated session, or the other client of the same session.
    # Here the watched client is the follower session's most recently active
    # one, attached FIRST; a client of the same session attached later and a
    # client of an unrelated session attached last must not get the popup.
    _question_seconds(world, 3)
    users_file = world.workdir / "a.txt"
    users_file.write_text("aaa\n")
    claudes_file = world.workdir / "file.py"
    claudes_file.write_text("one\ntwo\n")
    pane_id, observed = _users_vim(world, users_file, claudes_file, showmode=True)
    _adopted_state(world, pane_id, claudes_file)
    _type_half(world, pane_id, observed, users_file)
    _typed_on_session(world, "unrelated")
    try:
        with (
            attached_client(world) as watched,
            attached_client(world) as later,
        ):
            time.sleep(1.1)
            with attached_client(world, session="unrelated") as elsewhere:
                # Prefix, then an unbound key (F12): moves a client's
                # activity and does nothing else. The watched client is then
                # its session's most recently active one, and the unrelated
                # client the most recently active of all.
                time.sleep(1.1)
                watched.type(b"\x02\x1b[24~")
                world.wait_until(
                    lambda: _activity(world, watched) > _activity(world, later),
                    "the watched client the most recently active of its session",
                    timeout=5.0,
                )
                time.sleep(1.1)
                elsewhere.type(b"\x02\x1b[24~")
                world.wait_until(
                    lambda: _activity(world, elsewhere) > _activity(world, watched),
                    "the unrelated client the most recently active of all",
                    timeout=5.0,
                )
                _seconds, state = _edit_answered(world, claudes_file, "y", client=watched)
        log = _hook_log(world)
        assert "Traceback" not in log, log
        assert "(in insert mode): taken over (answered y)" in log, log
        report = _settled(world, observed)
        assert report["claudes"] == "one|TWO", report
        assert state.stale_files == ()
    finally:
        world.tmux("send-keys", "-t", pane_id, "Escape")


def test_no_client_on_the_targets_session_asks_nothing_and_skips(world: E2EFollower) -> None:
    # Review m7: the only client sits on an unrelated session, where the
    # question was shown and timed out; now nothing is asked.
    _question_seconds(world, 3)
    users_file = world.workdir / "a.txt"
    users_file.write_text("aaa\n")
    claudes_file = world.workdir / "file.py"
    claudes_file.write_text("one\ntwo\n")
    pane_id, observed = _users_vim(world, users_file, claudes_file, showmode=True)
    _adopted_state(world, pane_id, claudes_file)
    _type_half(world, pane_id, observed, users_file)
    _typed_on_session(world, "unrelated")
    try:
        with attached_client(world, session="unrelated") as elsewhere:
            seconds, state = _edit_answered(world, claudes_file, "unasked", client=elsewhere)
        log = _hook_log(world)
        assert "Traceback" not in log, log
        skipped = [line for line in log.splitlines() if "is waiting on its user" in line]
        assert len(skipped) == 1, log
        assert (
            f"not taken over (not asked: no tmux client is attached to the session of {pane_id}"
            in skipped[0]
        ), skipped
        assert seconds < 3.0, f"the hook waited {seconds:.1f} s"
        report = _settled(world, observed)
        assert report["mode"] == "i" and report["lines"] == "aaahalf", report
        assert state.stale_files == (os.path.realpath(claudes_file),)
    finally:
        world.tmux("send-keys", "-t", pane_id, "Escape")
