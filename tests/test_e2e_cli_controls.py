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

import json
import os
import subprocess
from collections.abc import Iterator
from pathlib import Path

import pytest
from e2e_harness import E2EFollower, e2e_world, payload
from helpers import load_scaled

from vim_ai_follower import control
from vim_ai_follower.hooks import HANDOFF_CUE

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
    process = world.cli_background("hook", "post", stdin=payload("Write", target_file))
    world.wait_for_animating("running")
    return follower, process


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
    sock, process = _animate_fresh(world, "nvim", path, FRESH, "lento")
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
    world.wait_for_handoff()

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
    world.wait_for_hook_exit(process)
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
    process = world.cli_background("hook", "post", stdin=payload("Edit", path))
    world.wait_for_animating("running")

    typed_first = OP_AFTER.splitlines()[1]

    def inside_the_op() -> bool:
        buffer = world.nvim_buffer_lines(sock, path)
        if len(buffer) < 2:
            return False
        return 4 <= len(buffer[1]) < len(typed_first) and typed_first.startswith(buffer[1])

    world.wait_until(inside_the_op, "the op's first new line to be half typed", timeout=90.0)
    world.cli("interrupt")
    world.wait_for_handoff()

    assert world.nvim_buffer_bytes(sock, path) == OP_BEFORE.encode(), (
        "the interrupted op did not put its deleted line back"
    )
    remainder = world.pending()
    assert remainder["kind"] == "apply_edit"
    assert remainder["partial"] == OP_BEFORE
    assert len(remainder["remaining_ops"]) == 1, "the partly-run op was counted as done"

    world.cli("interrupt")  # the des-interrupt
    world.wait_for_hook_exit(process)
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
    target, process = _animate_fresh(world, backend, path, FRESH, "normal")

    _wait_visible(world, backend, target, path, "ALPHA_MARK")
    world.cli("interrupt")  # 1st press
    world.wait_for_handoff()

    world.cli("interrupt")  # 2nd press: the des-interrupt
    world.wait_for_animating("running")
    # Interrupt the REPLAY, not the gap before it: wait until content that was
    # NOT on screen at the first interrupt has been replayed.
    _wait_visible(world, backend, target, path, "BRAVO_MARK")

    world.cli("interrupt")  # 3rd press: interrupt mid-replay
    world.wait_for_handoff()

    world.cli("interrupt")  # 4th press: the one that used to do nothing
    world.wait_for_animating("running")

    world.wait_for_hook_exit(process)
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
    target, process = _animate_fresh(world, backend, path, FRESH, "normal")
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
    process.kill()
    process.wait(timeout=10)
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
    pane, process = _animate_fresh(world, "tmux", path, FRESH, "normal")
    width = world.resize_pane(pane, 49)
    assert width < 51, (
        f"the follower pane is {width} columns; at 51 or more E37 does not wrap, "
        "Vim never escalates to a hit-enter prompt, and a clean pane proves nothing"
    )

    _wait_visible(world, "tmux", pane, path, "ALPHA_MARK")
    world.cli("interrupt")
    world.wait_for_handoff()
    # A real hook timeout: the buffer stays dirty and unlocked while the
    # window is freed for the next hook.
    process.kill()
    process.wait(timeout=10)

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


# ------------------------------------------------- a P that nothing obeys


def _status_float_body(world: E2EFollower, sock: str) -> list[str] | None:
    """The nvim status float's non-blank body lines, or None with no float."""
    nvim = world._nvim(sock)
    for window in nvim.api.list_wins():
        if nvim.api.win_get_config(window)["relative"] == "":
            continue
        buffer = nvim.api.win_get_buf(window)
        if nvim.api.buf_get_name(buffer).endswith("vaf-status"):
            lines = nvim.api.buf_get_lines(buffer, 0, -1, True)
            return [line.strip() for line in lines if line.strip()]
    return None


def test_nvim_a_pause_on_a_write_that_never_animates_is_taken_back(
    world: E2EFollower,
) -> None:
    """Final M6 — guards the hook's exit drain.

    A binary Write never reaches a driver: the hook holds the slot as
    "running" while it navigates, so `claude-follow pause` there says "pause
    requested" and puts "Paused" on the float, and nothing ever pauses. The
    hook's exit consumes the signal and takes "Paused" down. A BufReadPre
    `:sleep` in the follower's nvim holds the hook inside its navigation, so
    the press lands while it holds the slot, not by luck of timing."""
    world.start("nvim", "lento")
    sock = world.follower_target()
    world._nvim(sock).command("autocmd BufReadPre *.bin sleep 6")
    path = world.workdir / "blob.bin"
    world.cli("hook", "pre", stdin=payload("Write", path))
    path.write_bytes(b"\x00\x01\x02 binary\n")
    process = world.cli_background("hook", "post", stdin=payload("Write", path))
    world.wait_for_animating("running")

    pressed = world.cli("pause")
    assert pressed.stdout == "claude-follow: pause requested\n"
    # The feedback the drain has to take back really went up — without it the
    # check below would pass with nothing to retract.
    assert _status_float_body(world, sock) == ["Paused"]

    world.wait_for_hook_exit(process)
    assert _status_float_body(world, sock) in (None, []), "a 'Paused' outlived the hook"
    leftovers = sorted(entry.name for entry in world.cache_dir.iterdir() if ".pause" in entry.name)
    assert leftovers == [], f"a signal was left behind: {leftovers}"
    assert world.animating_state() is None


# ------------------------------------------------- stop sticks (sweep-4 T17)


def _write_config(world: E2EFollower, settings: dict[str, str]) -> None:
    path = world.home / ".config" / "claude-vim-follower" / "config.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(settings))


def _write_through_hooks(world: E2EFollower, path: Path) -> None:
    world.cli("hook", "pre", stdin=payload("Write", path))
    path.write_text(_text("print('written')"))
    world.cli("hook", "post", stdin=payload("Write", path))


def test_a_stopped_window_stays_stopped_until_start_or_toggle(world: E2EFollower) -> None:
    """`stop` used to last until the next tool call: with `open_policy`
    auto-opening, the next Write's hook opened a fresh follower in the window
    the user had just stopped. Now the stop holds until `start` (or toggle,
    which turns auto-open back on without starting anything)."""
    _write_config(world, {"backend": "nvim", "open_policy": "always"})
    state_path = world.cache_dir / f"{world.window_id}.pane"
    world.start("nvim")
    socket_path = Path(world.follower_target())
    world.cli("stop")
    world.wait_until(lambda: not socket_path.exists(), "the follower nvim to quit", timeout=10.0)

    _write_through_hooks(world, world.workdir / "after_stop.py")
    assert not state_path.exists(), "auto-open reopened a stopped window"
    assert not socket_path.exists()
    assert (world.cache_dir / "hook.log").read_text().count("claude-follow stop") == 1

    world.start("nvim")
    assert state_path.exists(), "start did not lift the stop"
    world.cli("stop")
    world.wait_until(lambda: not socket_path.exists(), "the follower nvim to quit", timeout=10.0)

    toggled = world.cli("toggle")
    assert "auto-open is back on" in toggled.stdout, toggled.stdout
    _write_through_hooks(world, world.workdir / "after_toggle.py")
    assert state_path.exists(), "auto-open stayed off after toggle"


def test_a_stop_under_another_tmux_server_does_not_hold_this_window(world: E2EFollower) -> None:
    """Window ids are reused across tmux restarts: a tombstone left by a stop
    under a previous server (here pid 1, which no tmux server has) is not this
    window's, and auto-open opens it as usual."""
    _write_config(world, {"backend": "nvim", "open_policy": "always"})
    world.cache_dir.mkdir(parents=True, exist_ok=True)
    (world.cache_dir / f"{world.window_id}.stopped").write_text("1")
    _write_through_hooks(world, world.workdir / "reused_window.py")
    assert (world.cache_dir / f"{world.window_id}.pane").exists()


def _stale_state(world: E2EFollower, target: str, adopted: bool, server_pid: int | None) -> None:
    """A `.pane` for this window as an earlier follower left it: after a tmux
    restart the new server numbers its panes from %0 again, so its target
    names one of THIS server's panes, which is not the follower's."""
    world.cache_dir.mkdir(parents=True, exist_ok=True)
    (world.cache_dir / f"{world.window_id}.pane").write_text(
        json.dumps(
            {
                "backend": "tmux",
                "target": target,
                "current_file": None,
                "origin": world.origin_pane,
                "on_failure": "silent",
                "speed": "rapido",
                "open_files": [str(world.workdir / "stale.py")],
                "stale_files": [],
                "enabled": True,
                "adopted": adopted,
                "shown_any": True,
                "writers": [],
                "writer_labels": [],
                "server_pid": server_pid,
            }
        )
    )


def _panes(world: E2EFollower) -> dict[str, str]:
    listing = world.tmux("list-panes", "-a", "-F", "#{pane_id} #{pane_current_command}").stdout
    return dict(line.split(" ", 1) for line in listing.splitlines())


@pytest.mark.parametrize(
    ("aim", "adopted", "recorded"),
    [
        ("origin", False, "another"),
        ("cat", False, "another"),
        ("cat", True, "another"),
        ("cat", False, "none"),
        ("cat", False, "this"),
        ("cat", True, "this"),
    ],
)
def test_stop_never_kills_or_types_into_a_pane_a_stale_state_names(
    world: E2EFollower, aim: str, adopted: bool, recorded: str
) -> None:
    """Final review of T17, C1 and C2: a stale state's target was killed
    (Claude's own pane, when it named the origin) or typed into (Vim script
    and Enter, into a shell). "another" is a previous server's pid, "none" a
    state from before the pid was recorded, and "this" this server's: the
    state is proven, but the pane runs `cat`, not the follower's Vim."""
    cat_pane = world.tmux(
        "split-window", "-t", world.origin_pane, "-P", "-F", "#{pane_id}", "cat"
    ).stdout.strip()
    world.wait_until(lambda: _panes(world).get(cat_pane) == "cat", what="the cat pane")
    target = world.origin_pane if aim == "origin" else cat_pane
    pids = {"another": 1, "none": None, "this": world.server_pid}
    _stale_state(world, target, adopted, pids[recorded])
    before = _panes(world)

    world.cli("stop")

    after = _panes(world)
    assert after == before, f"a pane was closed or changed: {before} -> {after}"
    typed = world.tmux("capture-pane", "-p", "-t", cat_pane).stdout.strip()
    assert typed == "", f"keys reached the cat pane: {typed!r}"
    assert not (world.cache_dir / f"{world.window_id}.pane").exists()
    log = (world.cache_dir / "hook.log").read_text()
    reason = "no longer runs vim" if recorded == "this" else "is not proven to be the follower's"
    assert reason in log, log


# ------------------------------------- an S during a catch-up (sweep-4 T20)

CATCH_UP_BASE = _text("def base():", "    return 'BASE_MARK'")
USER_LINE = "USER_MARK = 1"
# Long enough that a pace-0 catch-up is still typing when the S lands: tmux
# sends one batch per line (400 lines take seconds), nvim sets a whole line in
# two requests (400 lines were done before the first poll saw one).
_CATCH_UP_LENGTH = {"tmux": 400, "nvim": 30000}


def _catch_up_lines(backend: str) -> tuple[str, ...]:
    return tuple(
        f"catchup_line_{index:05d} = {index}" for index in range(_CATCH_UP_LENGTH[backend])
    )


def _catch_up_edit(backend: str) -> str:
    return CATCH_UP_BASE + _text(*_catch_up_lines(backend)) + _text("NEW_EDIT_MARK = True")


def _interrupt_a_catch_up(
    world: E2EFollower, backend: str
) -> tuple[str, Path, subprocess.Popen[bytes], bytes]:
    """Show a file, orphan a 400-line remainder for it the way a killed hook
    leaves one, fire the next edit and press S while its catch-up replays the
    remainder. Returns (target, path, hook, the buffer as handed over)."""
    path = world.workdir / "catchup_s.py"
    world.start(backend, "muito_rapido")
    target = world.follower_target()
    world.cli("hook", "pre", stdin=payload("Write", path))
    path.write_text(CATCH_UP_BASE)
    world.cli("hook", "post", stdin=payload("Write", path))
    assert world.buffer_bytes(backend, target, path) == CATCH_UP_BASE.encode()

    lines = _catch_up_lines(backend)
    control.save_pending_show_fresh(
        world.window_id,
        lines,
        0.08,
        continuation=True,
        base_directory=world.cache_dir,
        file_path=os.path.realpath(path),
        partial=CATCH_UP_BASE,
    )
    world.cli("hook", "pre", stdin=payload("Edit", path))
    path.write_text(_catch_up_edit(backend))
    process = world.cli_background("hook", "post", stdin=payload("Edit", path))
    world.wait_until(
        lambda: "catchup_line_0002" in world.visible_progress(backend, target, path),
        "the catch-up to be typing the remainder",
        timeout=60.0,
    )
    assert world.cli("interrupt").stdout == "claude-follow: interrupt requested\n"
    world.wait_until(
        lambda: world.animating_state() == "handoff",
        "the hand-off after an S during the catch-up",
        timeout=20.0,
    )
    # The cue goes up just after the marker turns "handoff", once the hook
    # has sent its hand-off lines: a dump typed before that interleaves with
    # them.
    if backend == "tmux":
        world.wait_until(
            lambda: (
                world.tmux("display-message", "-p", "-t", target, "#{pane_title}").stdout.strip()
                == HANDOFF_CUE
            ),
            "the hand-off cue on the pane border",
        )
    else:
        world.wait_until(
            lambda: HANDOFF_CUE in (_status_float_body(world, target) or []),
            "the hand-off cue in the status float",
        )
    held = world.buffer_bytes(backend, target, path)
    assert held is not None
    assert held.startswith(CATCH_UP_BASE.encode()), "the rebuild was disturbed"
    assert lines[-1].encode() not in held, "the S landed after the catch-up, measuring nothing"
    assert b"NEW_EDIT_MARK" not in held, "the new edit was animated over the user's hand-off"
    # Answer A: no remainder is saved, so the second S reloads Claude's file.
    assert not world.pending_path.exists(), "an interrupted catch-up saved a remainder"
    return target, path, process, held


@pytest.mark.parametrize("backend", ["tmux", "nvim"])
def test_an_s_during_a_catch_up_hands_over_and_w_releases_the_users_version(
    world: E2EFollower, backend: str
) -> None:
    """Final review M5, answer A — both backends.

    The S used to be consumed by the catch-up's driver and then ignored: no
    hand-off, the new edit animated anyway and the remainder was dropped. Now
    the hand-off cue is up for THIS edit, and a save releases Claude with the
    user's version on disk."""
    target, path, process, held = _interrupt_a_catch_up(world, backend)

    if backend == "tmux":
        world.tmux("send-keys", "-t", target, "Escape", "Escape")
        world.tmux("send-keys", "-t", target, "-l", "--", f":call append('$', '{USER_LINE}')")
        world.tmux("send-keys", "-t", target, "Enter")
        world.tmux("send-keys", "-t", target, "-l", "--", ":w")
        world.tmux("send-keys", "-t", target, "Enter")
        # Claude wrote the file after the buffer was last read, so Vim asks
        # "changed since reading it" (W11), as it does after ANY interrupted
        # Edit (measured 2026-10-17, not this task's): answer it as a user would.
        world.wait_until(
            lambda: (
                "changed since reading" in world.capture_pane(target) or process.poll() is not None
            ),
            "the save to ask or to release Claude",
        )
        if process.poll() is None:
            world.tmux("send-keys", "-t", target, "y")
    else:
        nvim = world._nvim(target)
        nvim.command(f"call append('$', '{USER_LINE}')")
        # The same W11 question would block a bare `:w` sent over RPC.
        nvim.command("write!")

    world.wait_for_hook_exit(process, timeout=load_scaled(30.0))
    assert path.read_bytes() == held + (USER_LINE + "\n").encode()
    assert "SAVED their own version" in world.background_log(process)


@pytest.mark.parametrize("backend", ["tmux", "nvim"])
def test_a_second_s_after_an_interrupted_catch_up_shows_claudes_file(
    world: E2EFollower, backend: str
) -> None:
    """Answer A's other half: S again discards the hand-off and shows the
    file Claude wrote (a reload, nothing replayed)."""
    target, path, process, _ = _interrupt_a_catch_up(world, backend)

    assert world.cli("interrupt").stdout.startswith("claude-follow: hand-off cancelled")
    world.wait_for_hook_exit(process, timeout=load_scaled(30.0))
    assert world.buffer_bytes(backend, target, path) == _catch_up_edit(backend).encode()
    assert path.read_text() == _catch_up_edit(backend)


# ---------------------------------------- Task 20, fix round 1 (tmux only)


def _adopt(world: E2EFollower) -> None:
    """Make the running dedicated follower read as an ADOPTED Vim: the
    user's own editor, whose settings the follower must give back."""
    state_file = world.cache_dir / f"{world.window_id}.pane"
    state = json.loads(state_file.read_text())
    state["adopted"] = True
    state_file.write_text(json.dumps(state))


def _vim_typed(world: E2EFollower, pane: str, command: str) -> None:
    world.tmux("send-keys", "-t", pane, "Escape", "Escape")
    world.tmux("send-keys", "-t", pane, "-l", "--", command)
    world.tmux("send-keys", "-t", pane, "Enter")


def _vim_option(world: E2EFollower, pane: str, option: str) -> str:
    """One option's value in the follower's Vim, written out by Vim itself
    (writefile, never the rendered screen)."""
    out = world.workdir / f"option-{option}-{os.urandom(4).hex()}.txt"
    _vim_typed(world, pane, f":call writefile(['option', &{option}], '{out}')")
    world.wait_until(
        lambda: out.exists() and len(out.read_text().splitlines()) == 2,
        f"vim to write out '{option}'",
    )
    return out.read_text().splitlines()[1]


def _wait_for_handoff_cue(world: E2EFollower, pane: str) -> None:
    world.wait_until(
        lambda: world.animating_state() == "handoff",
        "the marker to read 'handoff'",
        timeout=load_scaled(30.0),
    )
    world.wait_until(
        lambda: (
            world.tmux("display-message", "-p", "-t", pane, "#{pane_title}")
            .stdout.strip()
            .startswith("Claude waits")
        ),
        "the hand-off cue on the pane border",
    )


def _save_and_release(world: E2EFollower, pane: str, process: subprocess.Popen[bytes]) -> None:
    """`:w!` the handed-over buffer, answering Vim's W11 as a user would."""
    _vim_typed(world, pane, ":w!")
    world.wait_until(
        lambda: "changed since reading" in world.capture_pane(pane) or process.poll() is not None,
        "the save to ask or to release Claude",
    )
    if process.poll() is None:
        world.tmux("send-keys", "-t", pane, "y")
    world.wait_for_hook_exit(process, timeout=load_scaled(30.0))


REBUILD_BASE = _text(*(f"base_line_{index:05d} = {index}" for index in range(400)))


def test_an_s_during_the_catch_up_rebuild_hands_over_the_whole_partial(
    world: E2EFollower,
) -> None:
    """Fix round 1, I1. tmux's rewrite_buffer wipes the buffer (`:%d`) and
    retypes the partial; an S that landed inside it used to cut it, so the
    buffer handed over held a few lines of a 400-line partial while Claude
    was told all of it had been shown, and `:w` saved the stub. The rebuild
    now runs to the end and the S is honoured right after it: the buffer
    handed over is the whole partial, and the notification quotes exactly
    that."""
    path = world.workdir / "rebuild_s.py"
    world.start("tmux", "muito_rapido")
    pane = world.follower_target()
    world.cli("hook", "pre", stdin=payload("Write", path))
    path.write_text(REBUILD_BASE)
    world.cli("hook", "post", stdin=payload("Write", path))
    # The end of the file on screen: its disappearance is the rebuild's `:%d`.
    _vim_typed(world, pane, ":$")
    world.wait_until(lambda: "base_line_00399" in world.capture_pane(pane), "the file's end")
    control.save_pending_show_fresh(
        world.window_id,
        ("tail = 1",),
        0.08,
        continuation=True,
        base_directory=world.cache_dir,
        file_path=os.path.realpath(path),
        partial=REBUILD_BASE,
    )
    world.cli("hook", "pre", stdin=payload("Edit", path))
    path.write_text(REBUILD_BASE + _text("tail = 1", "NEW_EDIT_MARK = True"))
    process = world.cli_background("hook", "post", stdin=payload("Edit", path))
    world.wait_until(
        lambda: (
            "base_line_00399" not in (screen := world.capture_pane(pane))
            and "base_line_000" in screen
        ),
        "the rebuild to be retyping the partial",
        timeout=load_scaled(30.0),
        interval=0.01,
    )
    assert world.cli("interrupt").stdout == "claude-follow: interrupt requested\n"
    _wait_for_handoff_cue(world, pane)

    held = world.vim_buffer_bytes(pane)
    assert held == REBUILD_BASE.encode(), (
        f"handed over {len(held.splitlines())} of the partial's "
        f"{len(REBUILD_BASE.splitlines())} lines"
    )
    _save_and_release(world, pane, process)
    assert path.read_bytes() == held
    log = world.background_log(process)
    quoted = json.loads(log.strip().splitlines()[-1])["hookSpecificOutput"]["additionalContext"]
    assert REBUILD_BASE.removesuffix("\n") in quoted
    assert "tail = 1" not in quoted


def _set_users_options(world: E2EFollower, pane: str, modifiable: bool) -> None:
    _vim_typed(
        world, pane, f":setlocal noreadonly {'' if modifiable else 'no'}modifiable | set paste"
    )
    assert _vim_option(world, pane, "paste") == "1"
    assert _vim_option(world, pane, "modifiable") == ("1" if modifiable else "0")


@pytest.mark.parametrize(
    ("adopted", "modifiable"),
    [(True, True), (False, True), (True, False)],
    ids=["adopted", "dedicated", "adopted-nomodifiable"],
)
def test_an_s_during_the_catch_up_replay_gives_the_users_options_back(
    world: E2EFollower, adopted: bool, modifiable: bool
) -> None:
    """Fix round 1, I2. The interrupt gives the user's 'paste' (and, adopted,
    a recorded 'nomodifiable') back and forgets the record; hand_over then
    gave back AGAIN from no record, setting 'paste' to 0 and turning
    'modifiable' on. The user's values must survive the hand-off."""
    path = world.workdir / "options_catch_up.py"
    world.start("tmux", "muito_rapido")
    pane = world.follower_target()
    world.cli("hook", "pre", stdin=payload("Write", path))
    path.write_text(CATCH_UP_BASE)
    world.cli("hook", "post", stdin=payload("Write", path))
    if adopted:
        _adopt(world)
    _set_users_options(world, pane, modifiable)
    lines = _catch_up_lines("tmux")
    control.save_pending_show_fresh(
        world.window_id,
        lines,
        0.08,
        continuation=True,
        base_directory=world.cache_dir,
        file_path=os.path.realpath(path),
        partial=CATCH_UP_BASE,
    )
    world.cli("hook", "pre", stdin=payload("Edit", path))
    path.write_text(_catch_up_edit("tmux"))
    process = world.cli_background("hook", "post", stdin=payload("Edit", path))
    world.wait_until(
        lambda: "catchup_line_0002" in world.capture_pane(pane),
        "the catch-up to be typing the remainder",
        timeout=60.0,
    )
    assert world.cli("interrupt").stdout == "claude-follow: interrupt requested\n"
    _wait_for_handoff_cue(world, pane)

    assert _vim_option(world, pane, "paste") == "1"
    if adopted:
        assert _vim_option(world, pane, "modifiable") == ("1" if modifiable else "0")
    world.cli("interrupt")  # S again: Claude's file, and the hook ends
    world.wait_for_hook_exit(process, timeout=load_scaled(30.0))


@pytest.mark.parametrize("adopted", [True, False], ids=["adopted", "dedicated"])
def test_an_s_during_a_des_interrupt_replay_gives_the_users_paste_back(
    world: E2EFollower, adopted: bool
) -> None:
    """Fix round 1, I2, through the pre-existing _rearm_handoff: a replay
    (S again) that is itself interrupted hands over with hand_over too."""
    path = world.workdir / "options_rearm.py"
    world.start("tmux", "muito_rapido")
    pane = world.follower_target()
    world.cli("hook", "pre", stdin=payload("Write", path))
    path.write_text(CATCH_UP_BASE)
    world.cli("hook", "post", stdin=payload("Write", path))
    if adopted:
        _adopt(world)
    _set_users_options(world, pane, modifiable=True)
    world.cli("hook", "pre", stdin=payload("Edit", path))
    path.write_text(_catch_up_edit("tmux"))
    process = world.cli_background("hook", "post", stdin=payload("Edit", path))
    world.wait_until(
        lambda: "catchup_line_0000" in world.capture_pane(pane), "the edit to be typing"
    )
    assert world.cli("interrupt").stdout == "claude-follow: interrupt requested\n"
    _wait_for_handoff_cue(world, pane)
    assert world.cli("interrupt").stdout.startswith("claude-follow: hand-off cancelled")
    # Past the rebuild (the first S came within the first ten lines): the S
    # is the replay's, so the interrupt's give-back runs before hand_over's.
    world.wait_until(
        lambda: "catchup_line_0004" in world.capture_pane(pane),
        "the replay to be typing past the rebuilt partial",
        timeout=60.0,
    )
    assert world.cli("interrupt").stdout == "claude-follow: interrupt requested\n"
    _wait_for_handoff_cue(world, pane)

    assert _vim_option(world, pane, "paste") == "1"
    _save_and_release(world, pane, process)


# ------------------------------------------- Task 24c, item 4 (T20 review m3)


def _assert_the_stop_released_the_hook(
    world: E2EFollower, process: subprocess.Popen[bytes]
) -> None:
    world.cli("stop")
    # The poll sleeps 0.2 s between checks; "a few seconds" is the bar, the
    # old behaviour held the hook for its whole 600 s timeout.
    world.wait_for_hook_exit(process, timeout=5.0)
    assert "stopped the follower" in world.background_log(process)
    assert not world.pending_path.exists(), "the stopped hand-off left a remainder"
    leftovers = sorted(
        path.name
        for path in world.cache_dir.iterdir()
        if path.name in {f"{world.window_id}.pause", f"{world.window_id}.interrupt"}
    )
    assert leftovers == [], f"the stopped hand-off left signals {leftovers}"


@pytest.mark.parametrize("backend", ["nvim", "tmux"])
def test_a_stop_during_the_hand_off_wait_releases_claude_at_once(
    world: E2EFollower, backend: str
) -> None:
    """T20 review m3: `claude-follow stop` while the hook waits for the user's
    save used to leave the poll waiting for a save nobody would make — Claude
    held up to the hook's 600 s timeout. The poll now sees the stop."""
    path = world.workdir / "stopped_handoff.py"
    target, process = _animate_fresh(world, backend, path, FRESH, "lento")
    _wait_visible(world, backend, target, path, "ALPHA_MARK")
    world.cli("interrupt")
    world.wait_for_handoff()

    _assert_the_stop_released_the_hook(world, process)
    assert path.read_text() == FRESH, "the stop changed Claude's file"


@pytest.mark.parametrize("backend", ["nvim", "tmux"])
def test_a_stop_during_a_catch_up_hand_off_releases_claude_at_once(
    world: E2EFollower, backend: str
) -> None:
    """The same through `_hand_over_interrupted_catch_up`."""
    _, path, process, _ = _interrupt_a_catch_up(world, backend)

    _assert_the_stop_released_the_hook(world, process)
    assert path.read_text() == _catch_up_edit(backend), "the stop changed Claude's file"


# ------------------------------------------------- final review C1 (pause + stop)


@pytest.mark.parametrize("backend", ["nvim", "tmux"])
def test_a_stop_while_the_animation_is_paused_releases_claude_at_once(
    world: E2EFollower, backend: str
) -> None:
    """Final review C1: the pause wait never looked at the state or the
    tombstone, so a P followed by `claude-follow stop` held Claude until the
    hook's 600 s timeout (the stop clears the signals and, last follower, the
    P/S keys, so nothing could resume it either). The pause wait now sees the
    stop as the hand-off wait does."""
    path = world.workdir / "stopped_pause.py"
    target, process = _animate_fresh(world, backend, path, FRESH, "lento")
    _wait_visible(world, backend, target, path, "ALPHA_MARK")
    world.cli("pause")
    world.wait_for_animating("paused")

    _assert_the_stop_released_the_hook(world, process)
    assert path.read_text() == FRESH, "the stop changed Claude's file"
