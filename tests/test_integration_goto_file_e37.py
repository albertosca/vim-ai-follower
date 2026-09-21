"""Real tmux+vim measurement of goto_file's `:tab drop {file}` against a
dirty target buffer — the exact state an abandoned/killed hook (or a live
pause never followed by a relock, see _with_unlocked's docstring) leaves
behind. Full write-up: scratchpad/backlog/M-tmux-e37-report.md.

Headline findings, measured against the REAL follower pane geometry (a
`tmux split-window -h` inside a 100-col window leaves the follower at 49
columns — narrower than the 51-character E37 message, which is what makes
this a genuine BLOCKING "Press ENTER or type command to continue" hit-enter
prompt here; an earlier pass of this investigation measured against a
full-width 100-col pane, where the message never wraps and no block
appears, and wrongly concluded the prompt was a timing-race artifact in an
unrelated repro script. Both things are true: that OTHER script did have a
genuine settle-time bug of its own — see _settle_to_normal_mode below — but
independently, pane width decides whether E37 blocks at all. Reproduced 3+
times at each pane width to confirm both readings before settling on this
one, which matches production geometry):

1. `:tab drop {file}` DOES raise a real, BLOCKING `E37: No write since last
   change` hit-enter prompt whenever the TARGET's buffer is dirty and the
   pane is narrow enough to wrap the message (the real follower pane always
   is). Vim does not redraw the buffer while the prompt is pending.

2. The prompt is dismissed by the very next keystroke sent, WHICHEVER key
   it is — measured with both a colon-prefixed Ex command (what every real
   caller sends today: _UNLOCK_FOR_ANIMATION, _LOCK_READONLY, `:e!`,
   `:silent! CocDisable`, `:silent! bwipeout!`, all colon-prefixed) and a
   plain non-colon key ('x'). Both cases dismiss AND execute correctly: the
   colon command runs as if typed fresh; the plain key runs as a normal
   Normal-mode command against the buffer. No keystroke gets silently
   eaten. This is more robust than assumed going in — the "self-healing"
   isn't a coincidence that only survives colon-prefixed dismissals, it
   survives ANY next keystroke, as long as there IS one.

3. E37 is NOT specific to "the target is the CURRENT tab's own buffer" —
   dropping onto a dirty target already shown in ANOTHER tab ALSO raises
   E37 (the brief's own assumption that a tab switch wouldn't need an
   abandon check is false), but is equally harmless: the switch completes,
   the other tab's unsaved content survives untouched, and the same
   next-keystroke dismissal applies.

4. A target with no buffer at all (never opened) hits neither E37 nor any
   prompt — `:tab drop` just opens a fresh tab on the real disk content.

No src/ fix was applied: every viable guard (see the report) requires
changing goto_file's Ex line, embedding a Vim-side conditional Python has no
way to evaluate itself (TmuxPane is write-only — no capture-pane, no
`:redir`, nothing reads the pane back). That line is pinned byte-for-byte,
unconditionally, by tests outside this task's scope: test_tmux_vim.py (this
file's own sibling — goto_file/ensure_showing/reload_and_relock/apply_edit/
resume/close_tab all assert the exact keystroke sequence) AND
test_cli_hooks.py (grepped directly: `assert _literal_sends(run) == [f":tab
drop {target}", ...]` appears repeatedly there too, through hooks.py's own
navigation calls — the file the hooks.py agent in this sweep owns). Given
finding 2 above — the current design already self-heals for ANY next
keystroke, not just a lucky colon-prefixed one — the measured severity is
low enough that documenting and regression-testing the current behavior,
rather than forcing a fix that can only land by breaking pinned tests in
someone else's file, is the responsible tradeoff here."""

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


def _start_follower(
    tmux_session: str, monkeypatch: pytest.MonkeyPatch, wait_until: Callable[..., bool]
) -> str:
    origin_pane = _pane_ids(tmux_session)[0]
    monkeypatch.setenv("TMUX_PANE", origin_pane)
    assert cli.main(["start"]) == 0
    assert wait_until(lambda: len(_pane_ids(tmux_session)) == 2)
    return next(p for p in _pane_ids(tmux_session) if p != origin_pane)


def _settle_to_normal_mode(pane_id: str, wait_until: Callable[..., bool]) -> None:
    """This test file's own setup steps send two raw Escapes back to back
    (mirroring animate._EXIT_INSERT) with NO inter-key pace — unlike
    production, where send_paced sleeps pace_seconds between every
    KeySequence whenever pace_seconds > 0 (the default). Measured while
    writing these tests: under load, tmux/Vim can take a while (observed up
    to ~1-2s) to disambiguate two back-to-back raw ESC bytes from the start
    of a modifier-key escape sequence — capture-pane shows a lingering
    '-- INSERT --' with a pending '^[^[' in showcmd during that window, even
    though the typed TEXT is already fully in the buffer. Calling goto_file
    while that disambiguation is still pending is not the scenario under
    test (a real cold hook process always calls goto_file well after its
    own setup settled) — so setup here waits it out explicitly instead of
    letting it leak into the goto_file() measurement that follows."""
    assert wait_until(
        lambda: "^[" not in _capture(pane_id) and "INSERT" not in _capture(pane_id),
        timeout=5.0,
        interval=0.05,
    )


def _dirty_row(pane_id: str, marker: str) -> tuple[int, str]:
    lines = _capture(pane_id).splitlines()
    row = next(i for i, line in enumerate(lines) if marker in line)
    return row, lines[row]


def _dismisses_the_prompt_and_lands_as_a_real_command(
    pane_id: str, row: int, line_before: str, wait_until: Callable[..., bool]
) -> bool:
    """`row`/`line_before` must be captured BEFORE calling goto_file() —
    E37's hit-enter prompt hides the buffer entirely while pending (the
    whole pane shows the message, not the content), so looking the marker
    line up AFTER confirming the prompt is showing always raises
    StopIteration (measured mistake from this investigation's own first
    draft: the marker's row genuinely is not on screen at that instant,
    which is not the same as "blocked").

    Waits for the prompt to actually be showing, sends a single non-colon
    key ('x', Normal-mode delete-char), and proves it both dismissed the
    prompt AND executed: the marker's row must come back exactly one
    character shorter once the pane redraws, not merely present again
    (which would also happen if the key were silently discarded and the
    original content just got restored on dismiss)."""
    assert wait_until(lambda: "Press ENTER" in _capture(pane_id), timeout=3.0)
    _send_text(pane_id, "x")
    if not wait_until(lambda: "Press ENTER" not in _capture(pane_id), timeout=3.0):
        return False
    lines_after = _capture(pane_id).splitlines()
    line_after = lines_after[row] if row < len(lines_after) else ""
    return line_after == line_before[:-1]


def test_goto_file_on_the_current_dirty_buffer_dismisses_and_lands_cleanly(
    tmux_session: str,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    wait_until: Callable[..., bool],
) -> None:
    follower_pane_id = _start_follower(tmux_session, monkeypatch, wait_until)
    target = str(tmp_path / "current_dirty.py")

    # Bypass the hook entirely (like the prior investigation's own repro):
    # rename the follower's scratch buffer onto `target` and dirty it via
    # raw keystrokes, standing in for an abandoned animation's leftover
    # unsaved state (see _with_unlocked's docstring: "A pause never
    # relocks" — this is exactly that buffer shape).
    _send_text(follower_pane_id, f":file {target}")
    _send_key(follower_pane_id, "Enter")
    _send_text(follower_pane_id, "ihello world")
    _send_key(follower_pane_id, "Escape")
    _send_key(follower_pane_id, "Escape")
    assert wait_until(lambda: "hello world" in _capture(follower_pane_id), timeout=2.0)
    _settle_to_normal_mode(follower_pane_id, wait_until)
    row, line_before = _dirty_row(follower_pane_id, "hello world")

    follower = TmuxVimFollower(pane_id=follower_pane_id)
    follower.goto_file(target)  # no artificial delay: real cold-call timing

    assert _dismisses_the_prompt_and_lands_as_a_real_command(
        follower_pane_id, row, line_before, wait_until
    )


def test_goto_file_to_a_dirty_buffer_in_another_tab_switches_and_preserves_it(
    tmux_session: str,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    wait_until: Callable[..., bool],
) -> None:
    follower_pane_id = _start_follower(tmux_session, monkeypatch, wait_until)
    target = str(tmp_path / "dirty_elsewhere.py")
    other = str(tmp_path / "current_tab.py")

    _send_text(follower_pane_id, f":file {target}")
    _send_key(follower_pane_id, "Enter")
    _send_text(follower_pane_id, "iunsaved content")
    _send_key(follower_pane_id, "Escape")
    _send_key(follower_pane_id, "Escape")
    assert wait_until(lambda: "unsaved content" in _capture(follower_pane_id), timeout=2.0)
    _settle_to_normal_mode(follower_pane_id, wait_until)
    # A second tab, currently focused, on a DIFFERENT (clean) file — the
    # window goto_file must leave without needing to abandon anything of
    # its own.
    _send_text(follower_pane_id, f":tabnew {other}")
    _send_key(follower_pane_id, "Enter")
    assert wait_until(lambda: other.rsplit("/", 1)[-1] in _capture(follower_pane_id), timeout=2.0)

    # Peek at tab 1's row/content with the tabline now present (2 tabs), so
    # the row index matches what goto_file() will land on below — then
    # return to tab 2, exactly where a real "user wandered off" would leave
    # things before the next hook call navigates back.
    _send_text(follower_pane_id, ":tabfirst")
    _send_key(follower_pane_id, "Enter")
    assert wait_until(lambda: "unsaved content" in _capture(follower_pane_id), timeout=2.0)
    row, line_before = _dirty_row(follower_pane_id, "unsaved content")
    _send_text(follower_pane_id, ":tablast")
    _send_key(follower_pane_id, "Enter")
    assert wait_until(lambda: other.rsplit("/", 1)[-1] in _capture(follower_pane_id), timeout=2.0)

    follower = TmuxVimFollower(pane_id=follower_pane_id)
    follower.goto_file(target)

    # The brief's own assumption ("tab drop switches tabs without abandoning
    # anything there") turned out to only be half right: E37 DOES still
    # fire (measured, not assumed) — but the switch itself is clean and the
    # dismiss-and-land guarantee holds here too, content fully preserved.
    assert _dismisses_the_prompt_and_lands_as_a_real_command(
        follower_pane_id, row, line_before, wait_until
    )


def test_goto_file_to_a_file_with_no_buffer_yet_opens_a_new_tab_with_no_e37(
    tmux_session: str,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    wait_until: Callable[..., bool],
) -> None:
    follower_pane_id = _start_follower(tmux_session, monkeypatch, wait_until)
    target = tmp_path / "never_opened.py"
    target.write_text("real disk content\n")

    follower = TmuxVimFollower(pane_id=follower_pane_id)
    follower.goto_file(str(target))

    assert wait_until(lambda: "real disk content" in _capture(follower_pane_id), timeout=5.0)
    assert "E37" not in _capture(follower_pane_id)
    assert "Press ENTER" not in _capture(follower_pane_id)


def test_hook_post_on_a_target_left_dirty_by_a_pause_never_shows_a_stuck_prompt(
    tmux_session: str,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    wait_until: Callable[..., bool],
) -> None:
    """End-to-end sanity check through the real hook path (not just the raw
    goto_file() call above): a real pause leaves the buffer dirty per
    _with_unlocked's own docstring ("A pause never relocks"); the NEXT
    unrelated hook call navigates back to that file via goto_file and must
    still animate correctly, with no lingering prompt visible at the end."""
    origin_pane = _pane_ids(tmux_session)[0]
    monkeypatch.setenv("TMUX_PANE", origin_pane)
    assert cli.main(["start"]) == 0
    assert wait_until(lambda: len(_pane_ids(tmux_session)) == 2)
    follower_pane_id = next(p for p in _pane_ids(tmux_session) if p != origin_pane)

    target = tmp_path / "resumed.py"
    target.write_text("alpha\nbeta\n")
    post_payload = json.dumps({"tool_name": "Write", "tool_input": {"file_path": str(target)}})
    monkeypatch.setattr("sys.stdin", io.StringIO(post_payload))
    assert cli.main(["hook", "post"]) == 0
    assert wait_until(lambda: "beta" in _capture(follower_pane_id), timeout=10.0)

    # Manually dirty the now-relocked buffer (simulating a user typing into
    # a handed-over pane, or leftover keystrokes from an abandoned run) —
    # the next goto_file this test drives directly must not get stuck.
    _send_text(follower_pane_id, ":setlocal modifiable")
    _send_key(follower_pane_id, "Enter")
    _send_text(follower_pane_id, "Go")  # G: last line, o: open a new line below it
    _send_text(follower_pane_id, "another line unsaved")
    _send_key(follower_pane_id, "Escape")
    _send_key(follower_pane_id, "Escape")
    assert wait_until(lambda: "another line unsaved" in _capture(follower_pane_id), timeout=2.0)
    _settle_to_normal_mode(follower_pane_id, wait_until)
    row, line_before = _dirty_row(follower_pane_id, "another line unsaved")

    follower = TmuxVimFollower(pane_id=follower_pane_id)
    follower.goto_file(str(target))
    assert _dismisses_the_prompt_and_lands_as_a_real_command(
        follower_pane_id, row, line_before, wait_until
    )
