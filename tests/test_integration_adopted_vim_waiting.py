"""An ADOPTED Vim whose user is in the middle of something the follower's
keys would land in (final review I2) is left alone, without a question: Ex
mode (`gQ`/`Q`, mode `cv`/`ce`), where the Escape pair does nothing and every
typed line was concatenated into one Ex command at the user's prompt; a
confirm prompt (`:s///c`, `confirm()`), where the user's answer would run on
Claude's buffer; and an external command (`:!cmd`), whose stdin the keys
would go to. Answering them is the user's choice (Alberto's answer A, as on
nvim), so the hook logs "waiting on its user", marks an Edit's file stale,
and types nothing.

Real CLI, real tmux, real Vims, `HOME` isolated by the e2e harness; the
helpers are the adopted typing tests' (a VIMINIT-loaded observer timer)."""

from __future__ import annotations

import os
import time
from collections.abc import Iterator
from pathlib import Path

import pytest
from e2e_harness import E2EFollower, e2e_world, payload
from test_integration_adopted_vim_typing import (
    _adopted_state,
    _edit_answered,
    _hook_log,
    _observation,
    _observed,
    _question_seconds,
    _users_vim,
)

pytestmark = pytest.mark.integration


@pytest.fixture
def world() -> Iterator[E2EFollower]:
    with e2e_world() as live:
        yield live


def _files(world: E2EFollower) -> tuple[Path, Path]:
    users_file = world.workdir / "a.txt"
    users_file.write_text("aaa\n")
    claudes_file = world.workdir / "file.py"
    claudes_file.write_text("one\ntwo\n")
    return users_file, claudes_file


def _sourced(world: E2EFollower, pane_id: str, observed: Path, users_file: Path) -> None:
    """A first hook while the user is in Normal mode (it acts, and leaves the
    script and its mode file in the user's Vim), then the user back in their
    own file."""
    claudes = Path(world.workdir / "file.py")
    read = world.cli("hook", "post", stdin=payload("Read", claudes))
    assert read.returncode == 0, read.stderr
    world.wait_until(
        lambda: (_observed(observed) or {}).get("buffer") == os.path.realpath(claudes),
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


def _quiet(world: E2EFollower, pane_id: str) -> bool:
    """No prompt on the pane's last non-empty row."""
    rows = world.tmux("capture-pane", "-p", "-t", pane_id).stdout.rstrip().splitlines()
    return not rows or not (rows[-1].endswith("Options:") or "Press ENTER" in rows[-1])


def _skipped_unasked(world: E2EFollower, claudes_file: Path, *reasons: str) -> None:
    seconds, state = _edit_answered(world, claudes_file, "unasked")
    log = _hook_log(world)
    assert "Traceback" not in log, log
    skipped = [line for line in log.splitlines() if "is waiting on its user" in line]
    assert len(skipped) == 1, log
    assert any(f"is waiting on its user ({reason})" in skipped[0] for reason in reasons), skipped
    assert "taken over" not in skipped[0], skipped
    assert seconds < 3.0, f"the hook waited {seconds:.1f} s"
    assert state.stale_files == (os.path.realpath(claudes_file),)


@pytest.mark.parametrize("contact", ["mode-file", "first-contact"])
def test_a_vim_in_ex_mode_is_left_alone(world: E2EFollower, contact: str) -> None:
    _question_seconds(world, 3)
    users_file, claudes_file = _files(world)
    pane_id, observed = _users_vim(world, users_file, claudes_file, showmode=True)
    _adopted_state(world, pane_id, claudes_file)
    if contact == "mode-file":
        _sourced(world, pane_id, observed, users_file)
    world.tmux("send-keys", "-t", pane_id, "g", "Q")
    if contact == "mode-file":
        world.wait_until(
            lambda: any(
                path.read_text().split()[1:] == ["cv"]
                for path in world.cache_dir.glob("mode-*.txt")
            ),
            "the mode file reading cv",
            timeout=10.0,
        )
    else:
        # The observer timer runs in Ex mode too (measured).
        _observation(world, observed, lambda report: report["mode"] == "cv", "Ex mode", 10.0)
        assert not list(world.cache_dir.glob("mode-*.txt"))
    try:
        # Attaching the client that would show a question resizes the pane,
        # and a Vim in Ex mode then clears its screen and draws nothing
        # (measured): with no mode file, a blank screen is all there is.
        _skipped_unasked(
            world,
            claudes_file,
            "in Ex mode",
            *(
                ()
                if contact == "mode-file"
                else ("a blank screen (as Ex mode leaves it after a resize)",)
            ),
        )
        # Leave Ex mode: nothing the follower typed may be waiting in it.
        world.tmux("send-keys", "-t", pane_id, "-l", "visual\r")
        report = _observation(world, observed, lambda report: report["mode"] == "n", "Normal", 10.0)
        assert report["buffer"] == os.path.realpath(users_file), report
        assert report["lines"] == "aaa", report
        assert report["claudes"] in ("", "one|two"), report
        messages = world.workdir / "messages.txt"
        world.tmux(
            "send-keys",
            "-t",
            pane_id,
            "-l",
            f":redir! > {messages} | silent messages | redir END\r",
        )
        world.wait_until(messages.exists, "the user's :messages", timeout=10.0)
        time.sleep(0.2)
        assert "E492" not in messages.read_text(), messages.read_text()
    finally:
        world.tmux("send-keys", "-t", pane_id, "Escape")


def test_a_vim_at_a_substitute_confirm_is_left_alone(world: E2EFollower) -> None:
    # No ModeChanged fires for the confirm (measured): the mode file still
    # names Normal mode, and the screen decides.
    _question_seconds(world, 3)
    users_file, claudes_file = _files(world)
    pane_id, observed = _users_vim(world, users_file, claudes_file, showmode=True)
    _adopted_state(world, pane_id, claudes_file)
    _sourced(world, pane_id, observed, users_file)
    world.tmux("send-keys", "-t", pane_id, "-l", ":s/aaa/ZZZ/c\r")
    # At the pane's width Vim truncates the prompt in the middle, keeping
    # its start and its end: `replace with .../a/q/l/^E/^Y)?` (measured).
    world.wait_until(
        lambda: "^E/^Y)?" in world.tmux("capture-pane", "-p", "-t", pane_id).stdout,
        "the substitute's confirm",
        timeout=10.0,
    )
    try:
        _skipped_unasked(world, claudes_file, "a confirm prompt")
        # The user answers: their own buffer, not Claude's, is substituted.
        world.tmux("send-keys", "-t", pane_id, "y")
        report = _observation(
            world, observed, lambda report: report["lines"] == "ZZZ", "the substitute", 10.0
        )
        assert report["buffer"] == os.path.realpath(users_file), report
    finally:
        world.tmux("send-keys", "-t", pane_id, "Escape")


def test_a_vim_running_an_external_command_is_left_alone(world: E2EFollower) -> None:
    _question_seconds(world, 3)
    users_file, claudes_file = _files(world)
    pane_id, observed = _users_vim(world, users_file, claudes_file, showmode=True)
    _adopted_state(world, pane_id, claudes_file)
    _sourced(world, pane_id, observed, users_file)
    marker = world.workdir / "ran.txt"
    world.tmux("send-keys", "-t", pane_id, "-l", f":!touch {marker}; sleep 4\r")
    world.wait_until(marker.exists, "the external command", timeout=10.0)
    try:
        _skipped_unasked(world, claudes_file, "running an external command")
        # When the command ends, Vim finds Claude's loaded file changed on
        # disk (W11, a confirm of its own) and then asks for Enter: the
        # user answers both.
        world.wait_until(
            lambda: not _quiet(world, pane_id), "the external command's end", timeout=15.0
        )
        deadline = time.monotonic() + 20.0
        while not _quiet(world, pane_id) or (_observed(observed) or {}).get("mode") != "n":
            rows = world.tmux("capture-pane", "-p", "-t", pane_id).stdout.rstrip().splitlines()
            assert time.monotonic() < deadline, rows
            if rows and rows[-1].endswith("Options:"):
                world.tmux("send-keys", "-t", pane_id, "o")
            elif rows and "Press ENTER" in rows[-1]:
                world.tmux("send-keys", "-t", pane_id, "Enter")
            time.sleep(0.3)
        report = _observation(world, observed, lambda report: report["mode"] == "n", "Normal", 10.0)
        assert report["buffer"] == os.path.realpath(users_file), report
        assert report["lines"] == "aaa", report
    finally:
        world.tmux("send-keys", "-t", pane_id, "Escape")
