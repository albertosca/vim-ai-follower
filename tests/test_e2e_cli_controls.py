"""End-to-end control tests: the real `claude-follow` CLI, real hook payloads
on stdin, a real Vim/Neovim follower, and the pause/interrupt commands the
tmux prefix keys invoke.

Tranche 1 of the machine-verifiable half of `qa/visual-battery.md`. Each test
names the battery check it replaces and the commit whose behavior it guards;
what stays in the battery for those checks is the part only a human can judge
(whether the replay LOOKS smooth, whether the cue READS right).

Everything here is `integration`-marked: it spawns real tmux servers, vims and
nvims and animates at human pace, so it is slow by nature and CI runs
`-m "not integration"`.
"""

from __future__ import annotations

import subprocess
from collections.abc import Iterator
from pathlib import Path

import pytest
from e2e_harness import E2EFollower, e2e_world, payload

pytestmark = pytest.mark.integration


def _text(*lines: str) -> str:
    """Terminated, never joined: a "\\n".join round-trip is lossy for trailing
    blank lines, and these fixtures carry consecutive blanks on purpose."""
    return "".join(line + "\n" for line in lines)


# A retype long enough to interrupt twice with content still left over, with
# PEP 8 double blanks between defs (the rows a rebuild from the persisted
# partial has to get right) and four distinct markers to wait on.
FRESH = _text(
    "import os",
    "",
    "",
    "def alpha(root):",
    "    first = 'ALPHA_MARK'",
    "    return os.path.join(root, first)",
    "",
    "",
    "def bravo(root):",
    "    second = 'BRAVO_MARK'",
    "    return second",
)

# One `replace` op of 1 line -> 3: the op deletes the single return line
# instantly, then types three in, so an interrupt landing inside it must put
# the deleted line back.
OP_BEFORE = _text(
    "def total_of(values):",
    "    return sum(values)",
)
OP_AFTER = _text(
    "def total_of(values):",
    "    present = [v for v in values]",
    "    doubled = [v * 2 for v in present]",
    "    return sum(doubled)",
)

NEXT_EDIT = _text(
    "import os",
    "",
    "",
    "def alpha(root):",
    "    first = 'ALPHA_MARK'",
    "    return os.path.join(root, first)",
    "",
    "",
    "def bravo(root):",
    "    second = 'BRAVO_MARK'",
    "    return second, 'CATCHUP OK'",
)


@pytest.fixture
def world() -> Iterator[E2EFollower]:
    with e2e_world() as live:
        yield live


def _animate_fresh(
    world: E2EFollower, backend: str, target_file: Path, content: str, speed: str
) -> tuple[str, subprocess.Popen[bytes]]:
    """Register a follower and fire one fresh retype through the CLI, exactly
    as a PreToolUse/PostToolUse pair does. Order matters: `hook pre` snapshots
    the CURRENT (absent) file before the content lands."""
    world.start(backend, speed)
    follower = world.follower_target()
    world.cli("hook", "pre", stdin=payload("Write", target_file))
    target_file.write_text(content)
    proc = world.cli_background("hook", "post", stdin=payload("Write", target_file))
    world.wait_for_animating("running")
    return follower, proc


def _wait_visible(world: E2EFollower, backend: str, target: str, path: Path, needle: str) -> None:
    world.wait_until(
        lambda: needle in world.visible_progress(backend, target, path),
        f"{needle!r} to appear in the follower",
        timeout=90.0,
    )


# --------------------------------------------------------------- Check 13


def test_nvim_interrupt_leaves_the_half_typed_line_uncounted(world: E2EFollower) -> None:
    """Battery check 13, step 'line' — guards 1d2b5a9.

    A mid-LINE interrupt leaves the line cut exactly where it was typed (no
    snap to its full text), and `completed_count` does not count it: the
    persisted remainder starts WITH that whole line, and the partial stops
    before it. The des-interrupt then retypes it from its start and lands on
    the full content.
    """
    path = world.workdir / "fresh.py"
    sock, proc = _animate_fresh(world, "nvim", path, FRESH, "lento")
    lines = FRESH.splitlines()
    cut_at = 5  # "    return os.path.join(root, first)" — long enough to catch
    whole = lines[cut_at]

    def half_typed() -> bool:
        buffer = world.nvim_buffer_lines(sock, path)
        if len(buffer) <= cut_at:
            return False
        typed = buffer[cut_at]
        return 4 <= len(typed) < len(whole) and whole.startswith(typed)

    world.wait_until(half_typed, f"line {cut_at} to be half typed", timeout=90.0)
    at_interrupt = world.nvim_buffer_lines(sock, path)[cut_at]
    world.cli("interrupt")
    world.wait_for_animating("handoff")

    left = world.nvim_buffer_lines(sock, path)
    assert left[:cut_at] == lines[:cut_at], "the lines already typed were disturbed"
    # The line is CUT, not completed: a strict, non-empty prefix. The snap this
    # replaced would have made left[cut_at] == whole. Asserted as a prefix
    # RANGE, not an equality against the sample: between observing the buffer
    # and the interrupt actually landing, a few more characters legitimately
    # get typed — pinning the exact cut point would be a race, not a check.
    assert left[cut_at].startswith(at_interrupt), "the cut line lost characters"
    assert whole.startswith(left[cut_at]) and left[cut_at] != whole, (
        f"the half-typed line was snapped to its full text: {left[cut_at]!r}"
    )

    # completed_count excluded it: the remainder begins with the WHOLE line
    # (so the resume retypes it from its start, never mid-word), and the
    # partial stops one line short of it.
    remainder = world.pending()
    assert remainder["kind"] == "show_fresh"
    assert tuple(remainder["remaining_lines"]) == tuple(lines[cut_at:])
    assert remainder["partial"] == _text(*lines[:cut_at])

    world.cli("interrupt")  # the des-interrupt
    world.wait_for_hook_exit(proc)
    assert world.nvim_buffer_bytes(sock, path) == FRESH.encode()


def test_nvim_interrupt_rolls_back_the_op_it_was_inside(world: E2EFollower) -> None:
    """Battery check 13, step 'op' — guards 6bef225.

    An edit whose script is one `replace` of 1 line -> 3 deletes that line
    instantly before typing. An interrupt inside the op must put the deleted
    line BACK, so the buffer equals exactly apply_ops(before, ops[:k]) — here
    k == 0, i.e. the before-version — and the des-interrupt then replays the
    op from its start onto the exact final content.
    """
    path = world.workdir / "rollback.py"
    sock, first = _animate_fresh(world, "nvim", path, OP_BEFORE, "lento")
    world.wait_for_hook_exit(first)
    assert world.nvim_buffer_bytes(sock, path) == OP_BEFORE.encode()

    world.cli("hook", "pre", stdin=payload("Edit", path))
    path.write_text(OP_AFTER)
    proc = world.cli_background("hook", "post", stdin=payload("Edit", path))
    world.wait_for_animating("running")

    typed_first = OP_AFTER.splitlines()[1]

    def inside_the_op() -> bool:
        buffer = world.nvim_buffer_lines(sock, path)
        if len(buffer) < 2:
            return False
        return 4 <= len(buffer[1]) < len(typed_first) and typed_first.startswith(buffer[1])

    world.wait_until(inside_the_op, "the op's first new line to be half typed", timeout=90.0)
    world.cli("interrupt")
    world.wait_for_animating("handoff")

    assert world.nvim_buffer_bytes(sock, path) == OP_BEFORE.encode(), (
        "the interrupted op did not put its deleted line back"
    )
    remainder = world.pending()
    assert remainder["kind"] == "apply_edit"
    assert remainder["partial"] == OP_BEFORE
    assert len(remainder["remaining_ops"]) == 1, "the partly-run op was counted as done"

    world.cli("interrupt")  # the des-interrupt
    world.wait_for_hook_exit(proc)
    assert world.nvim_buffer_bytes(sock, path) == OP_AFTER.encode()


# --------------------------------------------------------------- Check 14


@pytest.mark.parametrize("backend", ["nvim", "tmux"])
def test_handoff_cycles_past_the_press_that_used_to_do_nothing(
    world: E2EFollower, backend: str
) -> None:
    """Battery check 14 — guards 5b87c34, both backends.

    interrupt -> des-interrupt -> interrupt MID-REPLAY -> des-interrupt again.
    Before the fix the hand-off wait ended at the first replay stop, so the
    FOURTH press had nobody listening: the hook stayed held and the buffer
    stayed partial. Every press must land — the `.animating` marker cycles
    running/handoff — and the last des-interrupt must run to the full content.
    """
    path = world.workdir / "cycles.py"
    target, proc = _animate_fresh(world, backend, path, FRESH, "normal")

    _wait_visible(world, backend, target, path, "ALPHA_MARK")
    world.cli("interrupt")  # 1st press
    world.wait_for_animating("handoff")

    world.cli("interrupt")  # 2nd press: the des-interrupt
    world.wait_for_animating("running")
    # Interrupt the REPLAY, not the gap before it: wait until content that was
    # NOT on screen at the first interrupt has been replayed.
    _wait_visible(world, backend, target, path, "BRAVO_MARK")

    world.cli("interrupt")  # 3rd press: interrupt mid-replay
    world.wait_for_animating("handoff")

    world.cli("interrupt")  # 4th press: the one that used to do nothing
    world.wait_for_animating("running")

    world.wait_for_hook_exit(proc)
    assert world.buffer_bytes(backend, target, path) == FRESH.encode()


# --------------------------------------------------------------- Check 15


@pytest.mark.parametrize("backend", ["nvim", "tmux"])
def test_crash_fallback_catches_up_after_the_hook_is_killed(
    world: E2EFollower, backend: str
) -> None:
    """Battery check 15 — guards d1620ff / 7a470c1 / 4362526, both backends.

    Pause mid-animation (the same `claude-follow pause` the P keybinding
    runs), kill -9 the hook so the remainder is orphaned exactly as a hook
    timeout leaves it, then fire the NEXT edit: it must rebuild from the
    PERSISTED partial and converge on the new content with nothing duplicated
    and nothing stranded.

    The pending file and its `partial` are asserted BEFORE the kill: without
    that this test passes vacuously whenever the pause never lands, because
    there would be no remainder for the catch-up to get wrong.
    """
    path = world.workdir / "catchup.py"
    target, proc = _animate_fresh(world, backend, path, FRESH, "normal")
    _wait_visible(world, backend, target, path, "ALPHA_MARK")

    world.cli("pause")
    world.wait_for_animating("paused")
    world.wait_until(world.pending_path.exists, "the crash-fallback remainder to be persisted")
    remainder = world.pending()
    assert remainder["partial"], (
        "the pause left no persisted partial, so the catch-up below would have "
        "nothing to rebuild from and this test would measure nothing"
    )

    # -9 on purpose: the wait loop's `finally` discards the pending file on any
    # orderly exit, so a catchable signal would clean up the very state a hook
    # timeout leaves behind.
    proc.kill()
    proc.wait(timeout=10)
    assert world.pending_path.exists(), "the remainder did not survive the kill"
    assert world.animating_state() is None, "the dead hook left a live-looking marker"

    world.cli("hook", "pre", stdin=payload("Write", path))
    path.write_text(NEXT_EDIT)
    catchup = world.cli_background("hook", "post", stdin=payload("Write", path))
    world.wait_for_hook_exit(catchup)

    assert world.buffer_bytes(backend, target, path) == NEXT_EDIT.encode()


# --------------------------------------------------------------- Check 16


def test_navigating_to_a_dirty_buffer_raises_no_e37_prompt(world: E2EFollower) -> None:
    """Battery check 16 — guards 511ceb6, tmux backend.

    `:tab drop` ends with a `:rewind` whose abandon check raises E37 on a
    modified buffer — the ordinary state after an interrupt hand-off or a
    killed hook. The message is 51 characters, so it only WRAPS (and Vim only
    escalates a wrapped message into a blocking hit-enter prompt) in a pane
    NARROWER than that: at a wide pane this check passes vacuously, so the
    follower pane is narrowed to the real 49 columns and the width asserted.

    This verdict IS about the screen, so `capture-pane` is the right
    instrument here — the one place in this file where it is. Two more probes
    back it up, both measured against a real prompt rather than assumed
    (forced one with a long `:echo` in a 49-column pane, 2026-09-22):

      - Vim parks the cursor on the pane's BOTTOM row while a hit-enter
        prompt is up (cursor_y 38 of a 39-row pane) and leaves it in the text
        area when it is live (cursor_y 3). The bottom row is therefore a
        direct read of "is the pane blocked", independent of any keystroke.
      - A prompt swallows exactly one key, so the first NON-colon keystroke is
        the honest probe. `:` is not a valid one — it is a real key AT a
        hit-enter prompt, so an Ex command would dismiss the very prompt it
        was sent to detect. `gg` is used rather than something that types:
        `ensure_showing` deliberately relocks the buffer `readonly
        nomodifiable`, so nothing CAN land as content there, and a probe that
        needed it to would be testing the lock, not the prompt.
    """
    path = world.workdir / "dirty.py"
    pane, proc = _animate_fresh(world, "tmux", path, FRESH, "normal")
    width = world.resize_pane(pane, 49)
    assert width < 51, (
        f"the follower pane is {width} columns; at 51 or more E37 does not wrap, "
        "Vim never escalates to a hit-enter prompt, and a clean pane proves nothing"
    )

    _wait_visible(world, "tmux", pane, path, "ALPHA_MARK")
    world.cli("interrupt")
    world.wait_for_animating("handoff")
    # A real hook timeout: the buffer stays dirty and unlocked while the
    # window is freed for the next hook.
    proc.kill()
    proc.wait(timeout=10)

    world.cli("hook", "post", stdin=payload("Read", path))

    screen = world.capture_pane(pane)
    assert "E37" not in screen, f"E37 reached the pane:\n{screen}"
    assert "Press ENTER" not in screen, f"a hit-enter prompt is blocking the pane:\n{screen}"
    assert "-- More --" not in screen, f"the pager is blocking the pane:\n{screen}"

    # Read before anything that could dismiss a prompt.
    row, height = world.cursor_row(pane)
    assert row != height - 1, (
        f"the cursor is parked on the pane's bottom row ({row} of {height}) — "
        f"that is where Vim puts it while a prompt is blocking:\n{screen}"
    )
    # Guards the probe below against passing vacuously: `gg` proves nothing if
    # the cursor is already on line 1.
    assert row > 0, f"the animation left the cursor on line 1 ({row}); the probe below is vacuous"

    world.tmux("send-keys", "-t", pane, "gg")
    world.wait_until(
        lambda: world.cursor_row(pane)[0] == 0,
        "the first non-colon keystroke to reach Vim's normal mode",
        timeout=10.0,
    )

    # The discriminating assertion, and the reason the three screen checks
    # above are not enough on their own: `ensure_showing` ends with its own
    # `:setlocal` + Enter, which dismisses the hit-enter prompt milliseconds
    # after it appears, so a reverted catch still leaves a clean-looking,
    # responsive pane by the time a test can look. Vim's message history does
    # not forget (measured both ways, 2026-09-22).
    messages = world.vim_messages(pane)
    assert "E37" not in messages, f"the navigation raised E37:\n{messages}"

    # The navigation must not have reloaded from disk: disk holds the whole of
    # FRESH, the buffer must still hold only the prefix the interrupt left.
    dump = world.vim_buffer_bytes(pane)
    assert FRESH.encode().startswith(dump) and dump != FRESH.encode(), (
        f"the navigation replaced the interrupted buffer instead of leaving it: {dump!r}"
    )
