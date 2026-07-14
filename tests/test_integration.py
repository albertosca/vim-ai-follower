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

from vim_ai_follower import cli, config, control
from vim_ai_follower.diff import compute_edit_script
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


def _pane_command(pane_id: str) -> str | None:
    result = subprocess.run(
        ["tmux", "list-panes", "-a", "-F", "#{pane_id} #{pane_current_command}"],
        capture_output=True,
        text=True,
        check=True,
    )
    for line in result.stdout.splitlines():
        found_pane, _, command = line.partition(" ")
        if found_pane == pane_id:
            return command
    return None


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


def test_completed_edit_animation_writes_cleanly_with_a_bare_w(
    tmux_session: str,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    wait_until: Callable[..., bool],
) -> None:
    # Task 11 regression: without a disk sync at relock time, the retyped
    # buffer's recorded mtime is stale by the time apply_edit finishes —
    # Claude already rewrote the file on disk before the animation even
    # started — so Vim treats a bare `:w` as writing over an externally
    # changed file ("WARNING: The file has been changed since reading
    # it!!!", W11) instead of writing cleanly. The `:silent! e!` prepended
    # to the relock reloads the identical content Claude wrote, grounding
    # the buffer's mtime with no visible flash, so a bare `:w` on the
    # completed, nomodifiable buffer must write with no warning.
    origin_pane = _pane_ids(tmux_session)[0]
    monkeypatch.setenv("TMUX_PANE", origin_pane)
    assert cli.main(["start"]) == 0
    assert wait_until(lambda: len(_pane_ids(tmux_session)) == 2)
    follower_pane_id = next(p for p in _pane_ids(tmux_session) if p != origin_pane)

    target_file = tmp_path / "synced.txt"
    target_file.write_text("alpha\n")
    first_payload = json.dumps(
        {"tool_name": "Write", "tool_input": {"file_path": str(target_file)}}
    )
    monkeypatch.setattr("sys.stdin", io.StringIO(first_payload))
    assert cli.main(["hook", "post"]) == 0
    assert wait_until(lambda: "alpha" in _capture(follower_pane_id), timeout=10.0)

    pre_payload = json.dumps({"tool_name": "Edit", "tool_input": {"file_path": str(target_file)}})
    monkeypatch.setattr("sys.stdin", io.StringIO(pre_payload))
    assert cli.main(["hook", "pre"]) == 0

    target_file.write_text("alpha\nbeta\n")
    post_payload = json.dumps({"tool_name": "Edit", "tool_input": {"file_path": str(target_file)}})
    monkeypatch.setattr("sys.stdin", io.StringIO(post_payload))
    assert cli.main(["hook", "post"]) == 0
    assert wait_until(lambda: "beta" in _capture(follower_pane_id), timeout=10.0)

    follower_pane = TmuxPane(pane_id=follower_pane_id)
    follower_pane.send_text(":w")
    follower_pane.send_key("Enter")

    def _wrote_cleanly() -> bool:
        screen = _capture(follower_pane_id)
        return "written" in screen and "WARNING" not in screen

    assert wait_until(_wrote_cleanly, timeout=5.0)
    screen = _capture(follower_pane_id)
    assert "WARNING" not in screen
    assert "E45" not in screen


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


def test_resuming_a_crashed_animation_lands_back_on_its_own_tab_first(
    tmux_session: str,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    wait_until: Callable[..., bool],
) -> None:
    # A hook that dies mid-animation (killed, hook-timeout, ...) leaves its
    # remaining ops on disk with no live owner — `pause` recovers exactly
    # that state. If the user flipped to a DIFFERENT tab in the meantime
    # (as they naturally would, with nothing animating to hold their
    # attention), a resume that forgot to navigate back would type the
    # rest of b.py's edit straight into a.py's buffer instead.
    origin_pane = _pane_ids(tmux_session)[0]
    monkeypatch.setenv("TMUX_PANE", origin_pane)
    assert cli.main(["start"]) == 0
    assert wait_until(lambda: len(_pane_ids(tmux_session)) == 2)
    follower_pane_id = next(p for p in _pane_ids(tmux_session) if p != origin_pane)
    session_id = _session_id(origin_pane)

    a_file = tmp_path / "a.py"
    b_file = tmp_path / "b.py"
    a_file.write_text("print('a')\n")
    b_file.write_text("print('b')\n")

    write_a = json.dumps({"tool_name": "Write", "tool_input": {"file_path": str(a_file)}})
    monkeypatch.setattr("sys.stdin", io.StringIO(write_a))
    assert cli.main(["hook", "post"]) == 0
    assert wait_until(lambda: "print('a')" in _capture(follower_pane_id), timeout=10.0)

    write_b = json.dumps({"tool_name": "Write", "tool_input": {"file_path": str(b_file)}})
    monkeypatch.setattr("sys.stdin", io.StringIO(write_b))
    assert cli.main(["hook", "post"]) == 0
    assert wait_until(lambda: "print('b')" in _capture(follower_pane_id), timeout=10.0)
    # b.py's tab is active now — exactly where a real crash mid-edit of it
    # would have left things.

    ops = compute_edit_script("print('b')\n", "print('b')\nresumed line\n")
    # A real crashed animation always finds this already on disk — Claude
    # writes the file in full before the hook's animation even starts. The
    # resume's relock now does a `:silent! e!` disk sync, so leaving the
    # disk at the pre-edit content here would revert the buffer's typing.
    b_file.write_text("print('b')\nresumed line\n")
    control.save_pending_apply_edit(session_id, ops, 0.0, file_path=str(b_file))
    assert control.animating_state(session_id) is None  # no live owner: a true crash

    # The user, unaware anything is pending, flips over to a.py's tab.
    follower_pane = TmuxPane(pane_id=follower_pane_id)
    follower_pane.send_key("C-\\")
    follower_pane.send_key("C-n")
    follower_pane.send_text("gt")

    assert cli.main(["pause"]) == 0  # the recovery path: navigates then replays

    assert wait_until(lambda: "resumed line" in _capture(follower_pane_id), timeout=10.0)
    rows = [line.rstrip() for line in _capture(follower_pane_id).splitlines()]
    anchor = rows.index("print('b')")
    assert rows[anchor : anchor + 2] == ["print('b')", "resumed line"]
    assert control.has_pending_animation(session_id) is False

    # a.py's on-disk content, never touched by the resumed edit, is intact.
    assert a_file.read_text() == "print('a')\n"


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


def test_live_pause_resume_renavigates_to_the_animating_files_tab(
    tmux_session: str,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    wait_until: Callable[..., bool],
) -> None:
    # A live in-hook pause (control.request_pause, not a crash-recovered
    # one) must also re-select the animating file's tab on resume — the
    # user is free to wander to a different tab while paused, since nothing
    # is animating to hold their attention, and a resume that forgot to
    # navigate back would type straight into whatever tab they landed on.
    origin_pane = _pane_ids(tmux_session)[0]
    monkeypatch.setenv("TMUX_PANE", origin_pane)
    assert cli.main(["start", "--speed", "lento"]) == 0
    assert wait_until(lambda: len(_pane_ids(tmux_session)) == 2)
    follower_pane_id = next(p for p in _pane_ids(tmux_session) if p != origin_pane)
    session_id = _session_id(origin_pane)

    a_file = tmp_path / "a.py"
    b_file = tmp_path / "b.py"
    a_file.write_text("print('a')\n")
    b_file.write_text("\n".join(f"line {i}" for i in range(15)) + "\n")

    write_a = json.dumps({"tool_name": "Write", "tool_input": {"file_path": str(a_file)}})
    monkeypatch.setattr("sys.stdin", io.StringIO(write_a))
    assert cli.main(["hook", "post"]) == 0
    assert wait_until(lambda: "print('a')" in _capture(follower_pane_id), timeout=10.0)

    write_b = json.dumps({"tool_name": "Write", "tool_input": {"file_path": str(b_file)}})
    monkeypatch.setattr("sys.stdin", io.StringIO(write_b))

    # The first pause is forced deterministically, right at the boundary
    # between line 0 and line 1 (4 signal checks: line 0's opener, text,
    # Escape, then line 1's opener) — landing it mid-keystroke instead would
    # race Vim's own Escape-key disambiguation delay before the "u" rollback,
    # an unrelated flakiness this test isn't about. Every check after the
    # forced one falls through to the real signal file, so the real second
    # control.request_pause below drives the actual resume.
    real_check_signal = control.check_signal
    calls = {"n": 0}

    def _check(session_id_arg: str, base_dir: object = None) -> str | None:
        calls["n"] += 1
        if calls["n"] == 4:
            return "pause"
        return real_check_signal(session_id_arg, base_dir)  # type: ignore[arg-type]

    monkeypatch.setattr(control, "check_signal", _check)

    thread = threading.Thread(target=lambda: cli.main(["hook", "post"]))
    thread.start()
    assert wait_until(lambda: control.animating_state(session_id) == "paused", timeout=10.0)

    # The user, unaware anything is paused, flips over to a.py's tab.
    follower_pane = TmuxPane(pane_id=follower_pane_id)
    follower_pane.send_key("C-\\")
    follower_pane.send_key("C-n")
    follower_pane.send_text("gt")
    assert wait_until(lambda: "print('a')" in _capture(follower_pane_id), timeout=5.0)

    control.request_pause(session_id)  # the second press: resume
    thread.join(timeout=30.0)
    assert not thread.is_alive()

    assert wait_until(lambda: "line 14" in _capture(follower_pane_id), timeout=15.0)
    rows = [line.rstrip() for line in _capture(follower_pane_id).splitlines()]
    expected = [f"line {i}" for i in range(15)]
    anchor = rows.index("line 0")
    assert rows[anchor : anchor + len(expected)] == expected
    assert control.has_pending_animation(session_id) is False

    # the active buffer, not just the pane content, is b.py — ask Vim itself
    # rather than inferring it from the rendered screen.
    marker_file = tmp_path / "active_buffer.txt"
    follower_pane.send_text(f":call writefile([expand('%')], '{marker_file}')")
    follower_pane.send_key("Enter")
    assert wait_until(lambda: marker_file.exists() and marker_file.read_text().strip(), timeout=5.0)
    assert marker_file.read_text().strip() == str(b_file)


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


def test_auto_open_adopts_a_pre_existing_vim_pane(
    tmux_session: str,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    wait_until: Callable[..., bool],
) -> None:
    # A second pane already runs Vim BEFORE any hook fires — no cli.main
    # "start" involved. adopt_existing must find it and drive it directly,
    # instead of splitting a fresh follower pane.
    origin_pane = _pane_ids(tmux_session)[0]
    subprocess.run(["tmux", "split-window", "-h", "-t", origin_pane, "vim"], check=True)
    assert wait_until(lambda: len(_pane_ids(tmux_session)) == 2)
    vim_pane_id = next(p for p in _pane_ids(tmux_session) if p != origin_pane)
    assert wait_until(lambda: _pane_command(vim_pane_id) == "vim", timeout=5.0)

    config_path = tmp_path / "config.json"
    config_path.write_text('{"open_policy": "always", "adopt_existing": true}')
    monkeypatch.setattr(config, "CONFIG_PATH", config_path)
    monkeypatch.setenv("TMUX_PANE", origin_pane)

    target_file = tmp_path / "greeting.txt"
    target_file.write_text("hello\n")

    pre_payload = json.dumps({"tool_name": "Write", "tool_input": {"file_path": str(target_file)}})
    monkeypatch.setattr("sys.stdin", io.StringIO(pre_payload))
    assert cli.main(["hook", "pre"]) == 0

    target_file.write_text("hello\nvim ai follower\n")
    post_payload = json.dumps({"tool_name": "Write", "tool_input": {"file_path": str(target_file)}})
    monkeypatch.setattr("sys.stdin", io.StringIO(post_payload))
    assert cli.main(["hook", "post"]) == 0

    assert wait_until(lambda: "vim ai follower" in _capture(vim_pane_id), timeout=5.0)
    assert len(_pane_ids(tmux_session)) == 2  # adopted the pre-existing pane, no split
