from __future__ import annotations

import io
import json
import os
import shlex
import subprocess
import threading
import time
from collections.abc import Callable
from pathlib import Path

import pytest

from vim_ai_follower import cli, control
from vim_ai_follower.tmux import TmuxPane

pytestmark = pytest.mark.integration


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


def test_two_files_get_two_tabs_and_edits_return_to_the_right_tab(
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

    a_file = tmp_path / "a.py"
    b_file = tmp_path / "b.py"

    # First file: fresh, renames the start screen in place (no new tab yet).
    a_file.write_text("print('a')\n")
    write_a = json.dumps({"tool_name": "Write", "tool_input": {"file_path": str(a_file)}})
    monkeypatch.setattr("sys.stdin", io.StringIO(write_a))
    assert cli.main(["hook", "post"]) == 0
    assert wait_until(lambda: "print('a')" in _capture(follower_pane_id), timeout=10.0)

    # Second file: also fresh, but now shown_any is True — gets its own tab.
    b_file.write_text("print('b')\n")
    write_b = json.dumps({"tool_name": "Write", "tool_input": {"file_path": str(b_file)}})
    monkeypatch.setattr("sys.stdin", io.StringIO(write_b))
    assert cli.main(["hook", "post"]) == 0
    assert wait_until(lambda: "print('b')" in _capture(follower_pane_id), timeout=10.0)

    # Edit a.py again — already tracked, so it's not fresh: apply_edit
    # navigates back to a's own tab (the backend's goto_file preamble) and
    # animates the diff there, not into b's currently-active tab.
    pre_a = json.dumps({"tool_name": "Edit", "tool_input": {"file_path": str(a_file)}})
    monkeypatch.setattr("sys.stdin", io.StringIO(pre_a))
    assert cli.main(["hook", "pre"]) == 0

    a_file.write_text("print('a updated')\n")
    post_a = json.dumps({"tool_name": "Edit", "tool_input": {"file_path": str(a_file)}})
    monkeypatch.setattr("sys.stdin", io.StringIO(post_a))
    assert cli.main(["hook", "post"]) == 0

    assert wait_until(lambda: "print('a updated')" in _capture(follower_pane_id), timeout=10.0)

    # The tabline (top row of the pane capture) lists both files' tabs.
    top_row = _capture(follower_pane_id).splitlines()[0]
    assert "a.py" in top_row
    assert "b.py" in top_row


def test_apply_edit_produces_exact_final_content(
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

    target_file = tmp_path / "edit.txt"
    before_lines = ["line 1", "line 2", "line 3", "line 4"]
    target_file.write_text("\n".join(before_lines) + "\n")
    first_payload = json.dumps(
        {"tool_name": "Write", "tool_input": {"file_path": str(target_file)}}
    )
    monkeypatch.setattr("sys.stdin", io.StringIO(first_payload))
    assert cli.main(["hook", "post"]) == 0
    assert wait_until(lambda: "line 4" in _capture(follower_pane_id), timeout=10.0)

    pre_payload = json.dumps({"tool_name": "Edit", "tool_input": {"file_path": str(target_file)}})
    monkeypatch.setattr("sys.stdin", io.StringIO(pre_payload))
    assert cli.main(["hook", "pre"]) == 0

    after_lines = ["line 1", "replacement A", "replacement B", "line 4"]
    target_file.write_text("\n".join(after_lines) + "\n")
    post_payload = json.dumps({"tool_name": "Edit", "tool_input": {"file_path": str(target_file)}})
    monkeypatch.setattr("sys.stdin", io.StringIO(post_payload))
    assert cli.main(["hook", "post"]) == 0

    assert wait_until(lambda: "replacement B" in _capture(follower_pane_id), timeout=10.0)
    rows = [line.rstrip() for line in _capture(follower_pane_id).splitlines()]
    anchor = rows.index("line 1")
    assert rows[anchor : anchor + 4] == after_lines


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

    # the hook now HOLDS Claude's turn waiting for the user's save
    time.sleep(1.0)
    assert thread.is_alive()

    target_file.write_text("the user's own version\n")  # simulate :w!
    thread.join(timeout=10.0)
    assert not thread.is_alive()

    out = capsys.readouterr().out
    assert '"hookSpecificOutput"' in out
    assert str(target_file) in out
    assert "SAVED their own version" in out

    # buffer must be left modifiable — prove it by typing into it for real
    follower = TmuxPane(pane_id=follower_pane_id)
    follower.send_text("ohand-edited")
    follower.send_key("Escape")
    assert wait_until(lambda: "hand-edited" in _capture(follower_pane_id), timeout=5.0)


# The edit under test produces two replace ops (lines 3 and 16); each op's
# keystroke stream is [":N,Nd", Enter] then [":N-1", Enter, "o", text,
# Escape], with a signal check before every sequence. Pausing before check
# 2 lands inside the delete half (typed but not executed — cancel path),
# before check 4 inside the insert's positioning prefix (delete already ran
# and must be rolled back alone), and before check 6 inside the insert text
# (both halves applied — the double-undo path). These are exactly the spots
# where a pause used to leave the delete half applied so the resume
# re-deleted shifted lines.
@pytest.mark.parametrize("pause_at_check", [2, 4, 6])
def test_pause_persists_state_and_resume_finishes_the_edit(
    tmux_session: str,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    wait_until: Callable[..., bool],
    pause_at_check: int,
) -> None:
    origin_pane = _pane_ids(tmux_session)[0]
    monkeypatch.setenv("TMUX_PANE", origin_pane)
    assert cli.main(["start"]) == 0
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

    # Deterministic pause: fire it right before the Nth keystroke sequence
    # instead of racing a signal file against wall-clock timing — the exact
    # landing point is what decides which undo-compensation path runs. The
    # hook now WAITS while paused, so the next check (the wait loop's first
    # poll) delivers the resume; the hook only returns once fully done.
    calls = {"count": 0}

    def _pause_then_resume(session_id_arg: str, base_dir: Path | None = None) -> str | None:
        calls["count"] += 1
        if calls["count"] in (pause_at_check, pause_at_check + 1):
            return "pause"
        return None

    monkeypatch.setattr(control, "check_signal", _pause_then_resume)
    assert cli.main(["hook", "post"]) == 0
    assert control.has_pending_animation(session_id) is False  # fallback disarmed on resume
    assert wait_until(lambda: "CHANGED FIFTEEN" in _capture(follower_pane_id), timeout=10.0)

    # Exact-content check, not substrings: a pause landing inside a replace
    # op used to leave its delete half applied, and the resume re-ran the
    # delete against shifted lines — the changed lines still appeared, but
    # NEIGHBORING lines were silently destroyed. Only comparing the whole
    # buffer catches that. Polled: right after the last keystroke Vim may
    # still render a transient "^[" placeholder at the cursor while its
    # ttimeout disambiguates ESC — a real stray character never settles.
    def _buffer_matches() -> bool:
        rows = [line.rstrip() for line in _capture(follower_pane_id).splitlines()]
        if "line 0" not in rows:
            return False
        anchor = rows.index("line 0")
        return rows[anchor : anchor + len(after_lines)] == after_lines

    assert wait_until(_buffer_matches, timeout=10.0)


def test_registered_keybinding_command_pauses_via_run_shell(
    tmux_session: str,
    monkeypatch: pytest.MonkeyPatch,
    wait_until: Callable[..., bool],
) -> None:
    # Executes the EXACT command string cmd_start registered, through
    # `tmux run-shell -t <pane>` — the same expansion context a real
    # keypress gets (formats pre-expanded against the triggering pane).
    # This is the test that catches double-expansion bugs: nesting
    # display-message around the pre-expanded "%N" eats the "%" and
    # resolves the wrong target, so claude-follow exits 1 and no signal
    # file ever appears.
    #
    # Burn a few global pane ids first, so the pane's numeric id has no
    # matching window index — on a pristine server "%0" eaten down to "0"
    # accidentally resolves as window 0 and masks the bug.
    for _ in range(6):
        subprocess.run(["tmux", "new-window", "-t", tmux_session], check=True)
    window_ids = subprocess.run(
        ["tmux", "list-windows", "-t", tmux_session, "-F", "#{window_id}"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.split()
    for window_id in window_ids[1:]:  # by @id: immune to base-index settings
        subprocess.run(["tmux", "kill-window", "-t", window_id], check=True)
    origin_pane = _pane_ids(tmux_session)[0]
    monkeypatch.setenv("TMUX_PANE", origin_pane)
    assert cli.main(["start"]) == 0
    assert wait_until(lambda: len(_pane_ids(tmux_session)) == 2)
    session_id = _session_id(origin_pane)

    # Full-table listing filtered by hand: tmux 3.7b returns empty output
    # for `list-keys -T prefix P` even when the binding exists.
    listing = next(
        line
        for line in subprocess.run(
            ["tmux", "list-keys", "-T", "prefix"],
            capture_output=True,
            text=True,
            check=True,
        ).stdout.splitlines()
        if shlex.split(line)[:4] == ["bind-key", "-T", "prefix", "P"]
    )
    bound_command = shlex.split(listing)[-1]

    # The spawned claude-follow is a separate process using the REAL home
    # cache dir (the in-process cache.CACHE_DIR patch can't reach it), so
    # assert there and clean up in a finally.
    real_cache = Path.home() / ".cache" / "claude-vim-follower"
    signal_path = real_cache / f"{session_id}.pause"
    marker_path = real_cache / f"{session_id}.animating"
    signal_path.unlink(missing_ok=True)
    real_cache.mkdir(parents=True, exist_ok=True)
    marker_path.write_text(str(os.getpid()))  # else pause is an honest no-op
    try:
        subprocess.run(
            ["tmux", "run-shell", "-t", origin_pane, bound_command],
            check=True,
        )
        assert wait_until(signal_path.exists, timeout=5.0), (
            "the registered binding's command did not produce a pause signal "
            f"for its own session ({session_id})"
        )
    finally:
        signal_path.unlink(missing_ok=True)
        marker_path.unlink(missing_ok=True)
