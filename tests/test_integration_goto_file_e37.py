"""Real tmux+vim proof that goto_file never leaves a blocking hit-enter
prompt in the follower pane, whatever state the target buffer is in.

Geometry matters and is not incidental: the real follower pane is a
`tmux split-window -h` inside the suite's 100-column window, which leaves
it at 49 columns. `E37: No write since last change (add ! to override)` is
51 characters, so at this width it WRAPS, and a wrapped message is what
makes Vim escalate from a one-line message to a blocking "Press ENTER or
type command to continue" prompt. Measuring the same thing in a full-width
100-column pane hides the bug completely — every test here goes through
`cli.main(["start"])` so it gets the production geometry, never a
hand-rolled session.

Before the fix, `:tab drop {file}` raised that prompt in three distinct
states (all measured): the target being the current tab's own modified
buffer, the target modified in ANOTHER tab, and the target modified but
displayed in no window at all. The navigation itself always succeeded — the
prompt came from the `:rewind` that `:tab drop` runs last, which re-checks
the buffer it has already landed on. goto_file now wraps the drop in a
`:try`/`:catch` that swallows exactly E37, so nothing is left on screen and
nothing is waiting on a keystroke.

Each scenario asserts both halves, because either alone can pass while the
bug is live: that the pane shows no E37 and no prompt immediately after
goto_file, AND that a plain NON-colon keystroke then lands in the buffer as
a real command. The second half is what distinguishes "no prompt" from "a
prompt that the next colon-prefixed command happened to dismiss" — the old
behavior self-healed on any next keystroke, so a test that only sent Ex
commands would have stayed green throughout.
"""

from __future__ import annotations

import io
import json
import subprocess
from collections.abc import Callable
from pathlib import Path

import pytest

from vim_ai_follower import cli
from vim_ai_follower.backends.tmux_vim import TmuxVimFollower

pytestmark = pytest.mark.integration


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


def _send_text(pane_id: str, text: str) -> None:
    subprocess.run(["tmux", "send-keys", "-t", pane_id, "-l", "--", text], check=True)


def _send_key(pane_id: str, key: str) -> None:
    subprocess.run(["tmux", "send-keys", "-t", pane_id, key], check=True)


def _pane_width(pane_id: str) -> int:
    result = subprocess.run(
        ["tmux", "display-message", "-p", "-t", pane_id, "#{pane_width}"],
        capture_output=True,
        text=True,
        check=True,
    )
    return int(result.stdout.strip())


def _start_follower(
    tmux_session: str, monkeypatch: pytest.MonkeyPatch, wait_until: Callable[..., bool]
) -> str:
    origin_pane = _pane_ids(tmux_session)[0]
    monkeypatch.setenv("TMUX_PANE", origin_pane)
    assert cli.main(["start"]) == 0
    assert wait_until(lambda: len(_pane_ids(tmux_session)) == 2)
    pane_id = next(p for p in _pane_ids(tmux_session) if p != origin_pane)
    # Guard the guard: if the follower pane were ever wide enough to fit
    # E37's 51 characters on one line, none of the assertions below could
    # fail even with the fix reverted, and this file would be measuring
    # nothing. Fail loudly instead of passing vacuously.
    width = _pane_width(pane_id)
    assert width < 51, (
        f"follower pane is {width} columns: E37 would no longer wrap, so "
        "these tests could not detect the hit-enter prompt they exist for"
    )
    return pane_id


def _settle_to_normal_mode(pane_id: str, wait_until: Callable[..., bool]) -> None:
    """This file's SETUP steps send two raw Escapes back to back with no
    inter-key pace — unlike production, where send_paced sleeps between
    every KeySequence. Under load, tmux/Vim can take up to ~1-2s to
    disambiguate two back-to-back raw ESC bytes from the start of a
    modifier-key escape sequence; capture-pane shows a lingering
    '-- INSERT --' with a pending '^[^[' in showcmd meanwhile. Calling
    goto_file during that window is not the scenario under test, so setup
    waits it out rather than letting it leak into the measurement."""
    assert wait_until(
        lambda: "^[" not in _capture(pane_id) and "INSERT" not in _capture(pane_id),
        timeout=5.0,
        interval=0.05,
    )


def _dirty_row(pane_id: str, marker: str) -> tuple[int, str]:
    lines = _capture(pane_id).splitlines()
    row = next(i for i, line in enumerate(lines) if marker in line)
    return row, lines[row]


def _assert_no_prompt(pane_id: str) -> None:
    pane = _capture(pane_id)
    assert "E37" not in pane, f"E37 is on screen after goto_file:\n{pane}"
    assert "Press ENTER" not in pane, f"a hit-enter prompt is pending:\n{pane}"
    assert "-- More --" not in pane, f"a pager prompt is pending:\n{pane}"


def _assert_plain_key_lands_as_a_command(
    pane_id: str, row: int, line_before: str, wait_until: Callable[..., bool]
) -> None:
    """Send one NON-colon key ('x', Normal-mode delete-char) and require it
    to have executed against the buffer: the marker's row must come back
    with exactly one character deleted. A key that merely dismissed a prompt
    would leave the row untouched.

    Which character goes is deliberately not pinned. 'x' deletes the one
    under the cursor, and the cursor's column depends on how the target was
    reached — end-of-line after an Escape from insert mode, but column 1
    when `:tab drop` freshly displayed the buffer. Asserting a specific
    column made the windowless-buffer case fail for a reason that had
    nothing to do with prompts."""
    deletions = {line_before[:i] + line_before[i + 1 :] for i in range(len(line_before))}
    _send_text(pane_id, "x")
    assert wait_until(
        lambda: (lambda ls: row < len(ls) and ls[row] in deletions)(_capture(pane_id).splitlines()),
        timeout=3.0,
    ), (
        "the plain 'x' keystroke never landed as a real command — row "
        f"{row} was {line_before!r} and no single-character deletion of it "
        f"appeared, pane is:\n{_capture(pane_id)}"
    )


def _dirty_the_current_buffer(
    pane_id: str, target: str, text: str, wait_until: Callable[..., bool]
) -> None:
    """Rename the follower's scratch buffer onto `target` and leave it
    modified — exactly the shape an abandoned animation leaves behind (see
    _with_unlocked: "a pause never relocks")."""
    _send_text(pane_id, f":file {target}")
    _send_key(pane_id, "Enter")
    _send_text(pane_id, f"i{text}")
    _send_key(pane_id, "Escape")
    _send_key(pane_id, "Escape")
    assert wait_until(lambda: text in _capture(pane_id), timeout=3.0)
    _settle_to_normal_mode(pane_id, wait_until)


def test_goto_file_on_the_current_dirty_buffer_shows_no_prompt(
    tmux_session: str,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    wait_until: Callable[..., bool],
) -> None:
    follower_pane_id = _start_follower(tmux_session, monkeypatch, wait_until)
    target = str(tmp_path / "current_dirty.py")
    _dirty_the_current_buffer(follower_pane_id, target, "hello world", wait_until)
    row, line_before = _dirty_row(follower_pane_id, "hello world")

    TmuxVimFollower(pane_id=follower_pane_id).goto_file(target)

    assert wait_until(lambda: "hello world" in _capture(follower_pane_id), timeout=5.0)
    _assert_no_prompt(follower_pane_id)
    _assert_plain_key_lands_as_a_command(follower_pane_id, row, line_before, wait_until)


def test_goto_file_to_a_dirty_buffer_in_another_tab_shows_no_prompt(
    tmux_session: str,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    wait_until: Callable[..., bool],
) -> None:
    follower_pane_id = _start_follower(tmux_session, monkeypatch, wait_until)
    target = str(tmp_path / "dirty_elsewhere.py")
    other = str(tmp_path / "current_tab.py")
    _dirty_the_current_buffer(follower_pane_id, target, "unsaved content", wait_until)

    # A second tab, currently focused, on a DIFFERENT (clean) file — the
    # window goto_file must leave without needing to abandon anything.
    _send_text(follower_pane_id, f":tabnew {other}")
    _send_key(follower_pane_id, "Enter")
    assert wait_until(lambda: "current_tab.py" in _capture(follower_pane_id), timeout=3.0)

    # Read tab 1's row WITH the tabline present (2 tabs), so the index
    # matches what goto_file lands on — then return to tab 2, where a real
    # "user wandered off" would leave things.
    _send_text(follower_pane_id, ":tabfirst")
    _send_key(follower_pane_id, "Enter")
    assert wait_until(lambda: "unsaved content" in _capture(follower_pane_id), timeout=3.0)
    row, line_before = _dirty_row(follower_pane_id, "unsaved content")
    _send_text(follower_pane_id, ":tablast")
    _send_key(follower_pane_id, "Enter")
    assert wait_until(lambda: "current_tab.py" in _capture(follower_pane_id), timeout=3.0)

    TmuxVimFollower(pane_id=follower_pane_id).goto_file(target)

    assert wait_until(lambda: "unsaved content" in _capture(follower_pane_id), timeout=5.0), (
        "goto_file never switched to the other tab"
    )
    _assert_no_prompt(follower_pane_id)
    _assert_plain_key_lands_as_a_command(follower_pane_id, row, line_before, wait_until)


def test_goto_file_to_a_dirty_buffer_shown_in_no_window_shows_no_prompt(
    tmux_session: str,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    wait_until: Callable[..., bool],
) -> None:
    """The third dirty state, and the one a `win_gotoid`-style guard would
    miss: the target's buffer is loaded and modified but displayed nowhere,
    so there is no window to jump to. In adopt mode this is the user's own
    Vim, where `:hide`-ing a modified buffer is ordinary."""
    follower_pane_id = _start_follower(tmux_session, monkeypatch, wait_until)
    target = str(tmp_path / "hidden_dirty.py")
    filler = tmp_path / "filler.py"
    filler.write_text("filler body\n")
    _dirty_the_current_buffer(follower_pane_id, target, "hidden unsaved", wait_until)

    # `:hide edit` swaps the window onto another file and leaves the
    # modified target buffer alive, loaded, and windowless.
    _send_text(follower_pane_id, f":hide edit {filler}")
    _send_key(follower_pane_id, "Enter")
    assert wait_until(lambda: "filler body" in _capture(follower_pane_id), timeout=3.0)

    TmuxVimFollower(pane_id=follower_pane_id).goto_file(target)

    assert wait_until(lambda: "hidden unsaved" in _capture(follower_pane_id), timeout=5.0), (
        "goto_file never brought the hidden buffer back on screen"
    )
    _assert_no_prompt(follower_pane_id)
    row, line_before = _dirty_row(follower_pane_id, "hidden unsaved")
    _assert_plain_key_lands_as_a_command(follower_pane_id, row, line_before, wait_until)


def test_goto_file_to_a_file_with_no_buffer_yet_opens_it_with_no_prompt(
    tmux_session: str,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    wait_until: Callable[..., bool],
) -> None:
    follower_pane_id = _start_follower(tmux_session, monkeypatch, wait_until)
    target = tmp_path / "never_opened.py"
    target.write_text("real disk content\n")

    TmuxVimFollower(pane_id=follower_pane_id).goto_file(str(target))

    assert wait_until(lambda: "real disk content" in _capture(follower_pane_id), timeout=5.0)
    _assert_no_prompt(follower_pane_id)


def test_goto_file_to_a_clean_buffer_in_another_tab_still_switches(
    tmux_session: str,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    wait_until: Callable[..., bool],
) -> None:
    """Parity guard for the unchanged path: the E37 wrapper must not alter
    how a CLEAN target already open in another tab is reached — it is still
    a plain `:tab drop`, still switches to the existing tab, and still does
    not open a duplicate."""
    follower_pane_id = _start_follower(tmux_session, monkeypatch, wait_until)
    target = tmp_path / "clean_elsewhere.py"
    target.write_text("clean elsewhere body\n")
    other = tmp_path / "current_tab2.py"

    _send_text(follower_pane_id, f":edit {target}")
    _send_key(follower_pane_id, "Enter")
    assert wait_until(lambda: "clean elsewhere body" in _capture(follower_pane_id), timeout=3.0)
    _send_text(follower_pane_id, f":tabnew {other}")
    _send_key(follower_pane_id, "Enter")
    assert wait_until(lambda: "current_tab2.py" in _capture(follower_pane_id), timeout=3.0)

    TmuxVimFollower(pane_id=follower_pane_id).goto_file(str(target))

    assert wait_until(lambda: "clean elsewhere body" in _capture(follower_pane_id), timeout=5.0)
    _assert_no_prompt(follower_pane_id)
    # Two tabs, not three: the existing tab was reused.
    tabline = _capture(follower_pane_id).splitlines()[0]
    assert tabline.count("current_tab2.py") == 1
    assert "clean_elsewhere.py" in tabline


def test_hook_post_on_a_target_left_dirty_by_a_pause_shows_no_prompt(
    tmux_session: str,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    wait_until: Callable[..., bool],
) -> None:
    """End-to-end through the real hook path, not a raw goto_file() call: a
    pause leaves the buffer dirty (see _with_unlocked's docstring), and the
    NEXT hook call navigates back to that file via goto_file."""
    follower_pane_id = _start_follower(tmux_session, monkeypatch, wait_until)

    target = tmp_path / "resumed.py"
    target.write_text("alpha\nbeta\n")
    post_payload = json.dumps({"tool_name": "Write", "tool_input": {"file_path": str(target)}})
    monkeypatch.setattr("sys.stdin", io.StringIO(post_payload))
    assert cli.main(["hook", "post"]) == 0
    assert wait_until(lambda: "beta" in _capture(follower_pane_id), timeout=10.0)

    # Dirty the now-relocked buffer, as a user typing into a handed-over
    # pane (or leftover keystrokes from an abandoned run) would.
    _send_text(follower_pane_id, ":setlocal modifiable")
    _send_key(follower_pane_id, "Enter")
    _send_text(follower_pane_id, "Go")  # G: last line, o: open a line below
    _send_text(follower_pane_id, "another line unsaved")
    _send_key(follower_pane_id, "Escape")
    _send_key(follower_pane_id, "Escape")
    assert wait_until(lambda: "another line unsaved" in _capture(follower_pane_id), timeout=3.0)
    _settle_to_normal_mode(follower_pane_id, wait_until)
    row, line_before = _dirty_row(follower_pane_id, "another line unsaved")

    TmuxVimFollower(pane_id=follower_pane_id).goto_file(str(target))

    assert wait_until(lambda: "another line unsaved" in _capture(follower_pane_id), timeout=5.0)
    _assert_no_prompt(follower_pane_id)
    _assert_plain_key_lands_as_a_command(follower_pane_id, row, line_before, wait_until)
