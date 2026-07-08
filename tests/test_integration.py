from __future__ import annotations

import io
import json
import subprocess
import threading
import time
from collections.abc import Callable
from pathlib import Path

import pytest

from vim_ai_follower import cli, control, snapshot, state
from vim_ai_follower.tmux import TmuxPane

pytestmark = pytest.mark.integration


@pytest.fixture(autouse=True)
def isolated_dirs(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(state, "STATE_DIR", tmp_path / "state")
    monkeypatch.setattr(snapshot, "SNAPSHOT_DIR", tmp_path / "snapshots")
    monkeypatch.setattr(control, "CONTROL_DIR", tmp_path / "control")
    monkeypatch.setattr(cli, "LOG_PATH", tmp_path / "hook.log")


def _session_id(pane_id: str) -> str:
    result = subprocess.run(
        ["tmux", "display-message", "-p", "-t", pane_id, "#{session_id}"],
        capture_output=True,
        text=True,
        check=True,
    )
    return result.stdout.strip()


def _pane_ids(session_name: str) -> list[str]:
    result = subprocess.run(
        ["tmux", "list-panes", "-t", session_name, "-F", "#{pane_id}"],
        capture_output=True,
        text=True,
        check=True,
    )
    return result.stdout.splitlines()


def _capture(pane_id: str) -> str:
    result = subprocess.run(
        ["tmux", "capture-pane", "-t", pane_id, "-p"],
        capture_output=True,
        text=True,
        check=True,
    )
    return result.stdout


def test_start_creates_a_side_by_side_vim_pane(
    tmux_session: str, monkeypatch: pytest.MonkeyPatch, wait_until: Callable[..., bool]
) -> None:
    monkeypatch.setenv("TMUX_PANE", _pane_ids(tmux_session)[0])
    assert cli.main(["start"]) == 0
    assert wait_until(lambda: len(_pane_ids(tmux_session)) == 2)


def test_start_is_idempotent(
    tmux_session: str, monkeypatch: pytest.MonkeyPatch, wait_until: Callable[..., bool]
) -> None:
    monkeypatch.setenv("TMUX_PANE", _pane_ids(tmux_session)[0])
    assert cli.main(["start"]) == 0
    assert wait_until(lambda: len(_pane_ids(tmux_session)) == 2)
    assert cli.main(["start"]) == 0
    assert len(_pane_ids(tmux_session)) == 2


def test_stop_kills_the_follower_pane(
    tmux_session: str, monkeypatch: pytest.MonkeyPatch, wait_until: Callable[..., bool]
) -> None:
    monkeypatch.setenv("TMUX_PANE", _pane_ids(tmux_session)[0])
    cli.main(["start"])
    assert wait_until(lambda: len(_pane_ids(tmux_session)) == 2)
    assert cli.main(["stop"]) == 0
    assert wait_until(lambda: len(_pane_ids(tmux_session)) == 1)


def test_hook_pre_and_post_animate_an_edit(
    tmux_session: str,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    wait_until: Callable[..., bool],
) -> None:
    origin_pane = _pane_ids(tmux_session)[0]
    monkeypatch.setenv("TMUX_PANE", origin_pane)
    assert cli.main(["start"]) == 0
    assert wait_until(lambda: len(_pane_ids(tmux_session)) == 2)
    follower_pane_id = next(p for p in _pane_ids(tmux_session) if p != origin_pane)

    target_file = tmp_path / "greeting.txt"
    target_file.write_text("hello\nworld\n")

    pre_payload = json.dumps({"tool_name": "Write", "tool_input": {"file_path": str(target_file)}})
    monkeypatch.setattr("sys.stdin", io.StringIO(pre_payload))
    assert cli.main(["hook", "pre"]) == 0

    target_file.write_text("hello\nvim ai follower\n")
    post_payload = json.dumps({"tool_name": "Write", "tool_input": {"file_path": str(target_file)}})
    monkeypatch.setattr("sys.stdin", io.StringIO(post_payload))
    assert cli.main(["hook", "post"]) == 0

    assert wait_until(lambda: "vim ai follower" in _capture(follower_pane_id), timeout=5.0)


def test_show_fresh_renders_each_line_separately(
    tmux_session: str,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    wait_until: Callable[..., bool],
) -> None:
    # Regression net for the line-joining bug: per-line typing without a
    # newline between lines used to collapse the whole file into one line
    # ("hellworldo"). Substring asserts survive that mangling; exact row
    # comparison does not.
    origin_pane = _pane_ids(tmux_session)[0]
    monkeypatch.setenv("TMUX_PANE", origin_pane)
    assert cli.main(["start"]) == 0
    assert wait_until(lambda: len(_pane_ids(tmux_session)) == 2)
    follower_pane_id = next(p for p in _pane_ids(tmux_session) if p != origin_pane)

    target_file = tmp_path / "fresh.txt"
    target_file.write_text("alpha\nbeta\ngamma\n")
    post_payload = json.dumps({"tool_name": "Write", "tool_input": {"file_path": str(target_file)}})
    monkeypatch.setattr("sys.stdin", io.StringIO(post_payload))
    assert cli.main(["hook", "post"]) == 0

    assert wait_until(lambda: "gamma" in _capture(follower_pane_id), timeout=5.0)
    rows = [line.rstrip() for line in _capture(follower_pane_id).splitlines()]
    # Anchor on the first content row instead of row 0: the user's vimrc may
    # render a tabline above the buffer. A mangled single-line buffer has no
    # row equal to "alpha", so .index() still fails loudly on the bug.
    start = rows.index("alpha")
    assert rows[start : start + 3] == ["alpha", "beta", "gamma"]


def test_hook_post_read_navigates_the_follower_pane(
    tmux_session: str,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    wait_until: Callable[..., bool],
) -> None:
    origin_pane = _pane_ids(tmux_session)[0]
    monkeypatch.setenv("TMUX_PANE", origin_pane)
    assert cli.main(["start"]) == 0
    assert wait_until(lambda: len(_pane_ids(tmux_session)) == 2)
    follower_pane_id = next(p for p in _pane_ids(tmux_session) if p != origin_pane)

    target_file = tmp_path / "readme.txt"
    target_file.write_text("first line\nsecond line\n")

    read_payload = json.dumps({"tool_name": "Read", "tool_input": {"file_path": str(target_file)}})
    monkeypatch.setattr("sys.stdin", io.StringIO(read_payload))
    assert cli.main(["hook", "post"]) == 0

    assert wait_until(lambda: "first line" in _capture(follower_pane_id), timeout=5.0)


def test_interrupt_mid_animation_leaves_buffer_unlocked_with_partial_content(
    tmux_session: str,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    wait_until: Callable[..., bool],
    capsys: pytest.CaptureFixture[str],
) -> None:
    origin_pane = _pane_ids(tmux_session)[0]
    monkeypatch.setenv("TMUX_PANE", origin_pane)
    assert cli.main(["start", "--speed", "lento"]) == 0
    assert wait_until(lambda: len(_pane_ids(tmux_session)) == 2)
    follower_pane_id = next(p for p in _pane_ids(tmux_session) if p != origin_pane)
    session_id = _session_id(origin_pane)

    target_file = tmp_path / "big.txt"
    target_file.write_text("\n".join(f"line {i}" for i in range(30)) + "\n")
    post_payload = json.dumps({"tool_name": "Write", "tool_input": {"file_path": str(target_file)}})
    monkeypatch.setattr("sys.stdin", io.StringIO(post_payload))

    thread = threading.Thread(target=lambda: cli.main(["hook", "post"]))
    thread.start()
    time.sleep(1.0)  # let a handful of lines type at "lento" (0.15s/keystroke)
    control.request_interrupt(session_id)
    thread.join(timeout=10.0)
    assert not thread.is_alive()

    out = capsys.readouterr().out
    assert '"hookSpecificOutput"' in out
    assert str(target_file) in out

    # buffer must be left modifiable — prove it by typing into it for real
    follower = TmuxPane(pane_id=follower_pane_id)
    follower.send_text("ohand-edited")
    follower.send_key("Escape")
    assert wait_until(lambda: "hand-edited" in _capture(follower_pane_id), timeout=5.0)


def test_pause_persists_state_and_resume_finishes_the_edit(
    tmux_session: str,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    wait_until: Callable[..., bool],
) -> None:
    origin_pane = _pane_ids(tmux_session)[0]
    monkeypatch.setenv("TMUX_PANE", origin_pane)
    assert cli.main(["start", "--speed", "lento"]) == 0
    assert wait_until(lambda: len(_pane_ids(tmux_session)) == 2)
    follower_pane_id = next(p for p in _pane_ids(tmux_session) if p != origin_pane)
    session_id = _session_id(origin_pane)

    target_file = tmp_path / "edit.txt"
    before_lines = [f"line {i}" for i in range(20)]
    target_file.write_text("\n".join(before_lines) + "\n")

    # First view: treated as fresh regardless of hook_pre, since the
    # follower has never shown this file before — retypes it via show_fresh.
    first_payload = json.dumps(
        {"tool_name": "Write", "tool_input": {"file_path": str(target_file)}}
    )
    monkeypatch.setattr("sys.stdin", io.StringIO(first_payload))
    assert cli.main(["hook", "post"]) == 0
    assert wait_until(lambda: "line 19" in _capture(follower_pane_id), timeout=10.0)

    # Snapshot the current content, then apply the real edit under test
    # directly to disk (simulating the tool having already written it)
    # before firing hook post.
    pre_payload = json.dumps({"tool_name": "Edit", "tool_input": {"file_path": str(target_file)}})
    monkeypatch.setattr("sys.stdin", io.StringIO(pre_payload))
    assert cli.main(["hook", "pre"]) == 0

    after_lines = list(before_lines)
    after_lines[2] = "CHANGED TWO"
    after_lines[15] = "CHANGED FIFTEEN"
    target_file.write_text("\n".join(after_lines) + "\n")
    post_payload = json.dumps({"tool_name": "Edit", "tool_input": {"file_path": str(target_file)}})
    monkeypatch.setattr("sys.stdin", io.StringIO(post_payload))

    thread = threading.Thread(target=lambda: cli.main(["hook", "post"]))
    thread.start()
    time.sleep(0.5)
    control.request_pause(session_id)
    thread.join(timeout=10.0)
    assert not thread.is_alive()

    assert control.has_pending_animation(session_id) is True

    assert cli.main(["pause"]) == 0
    assert control.has_pending_animation(session_id) is False
    assert wait_until(lambda: "CHANGED TWO" in _capture(follower_pane_id), timeout=10.0)
    assert wait_until(lambda: "CHANGED FIFTEEN" in _capture(follower_pane_id), timeout=10.0)
