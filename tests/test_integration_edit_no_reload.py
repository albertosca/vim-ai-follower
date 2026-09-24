"""Real tmux+vim proof that an Edit of an already-open file animates FROM the
pre-edit buffer, instead of first reloading the finished file from disk —
and that a Read, where reading disk IS the intent, still does reload.

The bug (diagnosed 2026-09-23, shipped since 3923bd0): apply_edit's goto_file
navigated with `:tab drop {file}`, whose trailing `:rewind` re-edits the
buffer it lands on. Every completed animation ends with a silent `:e!`, so
the buffer is UNMODIFIED — and re-editing an unmodified buffer re-reads it
from disk, where Claude has already written the post-Edit content. The ops,
computed against the pre-Edit snapshot, were then typed on top of the
finished file (def count 1 -> 3 -> 4 -> 5), and the closing `:e!` snapped it
back to 3.

Every other apply_edit test checks only the END state, which that closing
`:e!` makes correct by construction — the mechanism hiding the bug is the one
causing it. So these tests watch the MIDDLE: a Vim timer, loaded through
VIMINIT into the follower's own Vim, appends every distinct (file, content)
state of the real buffer to a log. It never sends a key into the pane, so
the animation under test is untouched.

goto_file reaches an open buffer three ways — it is already current, it is
shown in another tab (`win_gotoid`), or it is loaded but in no window
(`:tab sbuffer`) — and each gets its own Edit test, because each branch
would bring the reload back on its own if it ever went through `:tab drop`.

What a missed sample means: the timer polls every 15 ms and logs only
changes, so it can skip a short-lived state but never invent one. The
invariants are "this bad state must never be seen", so a missed sample can
only hide a violation, never fabricate one — the tests cannot go red on a
correct run by chance. Two independent symptoms of the bug are checked (the
finished content showing up before the last state, and more `def`s than the
finished file has) and the duplicated states last for whole lines of typing,
so the red is not hostage to one lucky frame.
"""

from __future__ import annotations

import io
import json
import subprocess
import threading
from collections.abc import Callable
from pathlib import Path

import pytest

from vim_ai_follower import cli, control

pytestmark = pytest.mark.integration

ONE_DEF = "import os\n\n\ndef alpha(root):\n    return os.path.join(root, 'a')\n"
THREE_DEFS = (
    "import os\n\n\ndef alpha(root):\n    return os.path.join(root, 'a')\n"
    "\n\ndef bravo(root):\n    return 'b'\n"
    "\n\ndef charlie(root):\n    return 'c'\n"
)
OTHER = "x = 1\n"

# Logs one JSON line per distinct (buffer name, content) pair. json_encode
# keeps the content exact (no separator can collide with it), and the file
# name is the tail only so /tmp vs /private/tmp never matters.
_OBSERVER = r"""
let g:vaf_obs_last = ''
function! VafObsTick(timer) abort
  let l:state = json_encode({'name': expand('%:t'), 'lines': getline(1, '$')})
  if l:state !=# g:vaf_obs_last
    let g:vaf_obs_last = l:state
    call writefile([l:state], g:vaf_obs_log, 'a')
  endif
endfunction
call timer_start(15, 'VafObsTick', {'repeat': -1})
"""

_MARK = "=== EDIT START ==="


def _pane_ids(session_name: str) -> list[str]:
    result = subprocess.run(
        ["tmux", "list-panes", "-t", session_name, "-F", "#{pane_id}"],
        capture_output=True,
        text=True,
        check=True,
    )
    return result.stdout.splitlines()


def _window_id(pane_id: str) -> str:
    result = subprocess.run(
        ["tmux", "display-message", "-p", "-t", pane_id, "#{window_id}"],
        capture_output=True,
        text=True,
        check=True,
    )
    return result.stdout.strip()


def _states(log: Path, name: str) -> list[list[str]]:
    """Every logged state of `name`'s buffer after the edit marker, in order."""
    if not log.exists():
        return []
    after_mark = log.read_text().split(_MARK + "\n", 1)
    if len(after_mark) < 2:
        return []
    states: list[list[str]] = []
    for raw in after_mark[1].splitlines():
        entry = json.loads(raw)
        if entry["name"] == name:
            states.append(entry["lines"])
    return states


def _last_state(log: Path, name: str) -> list[str] | None:
    if not log.exists():
        return None
    for raw in reversed(log.read_text().splitlines()):
        if raw == _MARK:
            continue
        entry = json.loads(raw)
        if entry["name"] == name:
            lines: list[str] = entry["lines"]
            return lines
    return None


def _defs(lines: list[str]) -> int:
    return sum(1 for line in lines if line.lstrip().startswith("def "))


def _hook(monkeypatch: pytest.MonkeyPatch, phase: str, tool: str, target: Path) -> None:
    body = json.dumps({"tool_name": tool, "tool_input": {"file_path": str(target)}})
    monkeypatch.setattr("sys.stdin", io.StringIO(body))
    assert cli.main(["hook", phase]) == 0


def _observed_follower(
    tmux_session: str,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    wait_until: Callable[..., bool],
    *start_args: str,
) -> tuple[Path, str, str]:
    """Start a follower whose Vim carries the observer. Returns the log path,
    the follower pane id and the window id."""
    log = tmp_path / "observer.log"
    observer = tmp_path / "observer.vim"
    observer.write_text(f"let g:vaf_obs_log = '{log}'\n{_OBSERVER}")
    # The server already exists (tmux_session set VIMINIT before starting
    # it); the follower's split-window takes the GLOBAL environment, so this
    # extends the suite's hermetic VIMINIT with the observer and nothing else.
    subprocess.run(
        [
            "tmux",
            "set-environment",
            "-g",
            "VIMINIT",
            f"set nocompatible noloadplugins | source {observer}",
        ],
        check=True,
    )
    origin_pane = _pane_ids(tmux_session)[0]
    monkeypatch.setenv("TMUX_PANE", origin_pane)
    assert cli.main(["start", *start_args]) == 0
    assert wait_until(lambda: log.exists(), timeout=10.0), "observer never started"
    follower_pane = next(p for p in _pane_ids(tmux_session) if p != origin_pane)
    return log, follower_pane, _window_id(origin_pane)


def _write(
    monkeypatch: pytest.MonkeyPatch,
    log: Path,
    wait_until: Callable[..., bool],
    target: Path,
    content: str,
) -> None:
    _hook(monkeypatch, "pre", "Write", target)
    target.write_text(content)
    _hook(monkeypatch, "post", "Write", target)
    expected = content.splitlines()
    assert wait_until(lambda: _last_state(log, target.name) == expected, timeout=15.0)


def _edit_and_assert_typed_from_the_pre_edit_buffer(
    monkeypatch: pytest.MonkeyPatch,
    log: Path,
    wait_until: Callable[..., bool],
    target: Path,
) -> None:
    """Edit `target` from ONE_DEF to THREE_DEFS and assert the middle."""
    after = THREE_DEFS.splitlines()
    with log.open("a") as handle:
        handle.write(_MARK + "\n")
    _hook(monkeypatch, "pre", "Edit", target)
    target.write_text(THREE_DEFS)
    _hook(monkeypatch, "post", "Edit", target)
    assert wait_until(lambda: _last_state(log, target.name) == after, timeout=15.0)

    states = _states(log, target.name)
    trace = "\n".join(f"defs={_defs(s)} lines={len(s)}" for s in states)
    # The instrument saw the animation itself, not just its end: a run where
    # the observer caught nothing but the final frame would pass vacuously.
    assert len(states) >= 2, f"observer caught no intermediate state:\n{trace}"
    assert states[-1] == after
    # The finished file may appear only as the LAST state — never before
    # typing, and never as a frame the animation then types over.
    assert after not in states[:-1], f"finished file shown before the animation ended:\n{trace}"
    # Typing the diff onto the pre-edit buffer can never exceed the finished
    # file's def count; typing it onto the reloaded finished file does.
    assert max(_defs(s) for s in states) <= _defs(after), f"duplicated defs:\n{trace}"


def test_edit_of_an_open_file_never_shows_the_finished_file_before_typing_it(
    tmux_session: str,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    wait_until: Callable[..., bool],
) -> None:
    """The edited file is the CURRENT buffer (goto_file's no-op branch)."""
    log, _, _ = _observed_follower(tmux_session, monkeypatch, tmp_path, wait_until)
    target = tmp_path / "sample.py"
    _write(monkeypatch, log, wait_until, target, ONE_DEF)
    _edit_and_assert_typed_from_the_pre_edit_buffer(monkeypatch, log, wait_until, target)


def test_edit_of_a_file_open_in_another_tab_is_typed_from_the_pre_edit_buffer(
    tmux_session: str,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    wait_until: Callable[..., bool],
) -> None:
    """a.py sits in another tab behind b.py — goto_file's `win_gotoid`
    branch. Through `:tab drop` this is a reload too."""
    log, _, _ = _observed_follower(tmux_session, monkeypatch, tmp_path, wait_until)
    target = tmp_path / "a.py"
    _write(monkeypatch, log, wait_until, target, ONE_DEF)
    _write(monkeypatch, log, wait_until, tmp_path / "b.py", OTHER)
    _edit_and_assert_typed_from_the_pre_edit_buffer(monkeypatch, log, wait_until, target)


def test_edit_of_a_loaded_file_shown_in_no_window_is_typed_from_the_pre_edit_buffer(
    tmux_session: str,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    wait_until: Callable[..., bool],
) -> None:
    """a.py is loaded but its tab was closed under 'hidden' (a common vimrc
    setting — the user's config loads in a real follower) — goto_file's
    `:tab sbuffer` branch, which must not re-read a loaded buffer."""
    log, follower_pane, _ = _observed_follower(tmux_session, monkeypatch, tmp_path, wait_until)
    target = tmp_path / "a.py"
    _write(monkeypatch, log, wait_until, target, ONE_DEF)
    _write(monkeypatch, log, wait_until, tmp_path / "b.py", OTHER)
    # a.py is tab 1, b.py tab 2 (current). Close a.py's tab, keeping it loaded.
    for command in (":set hidden", ":1tabclose"):
        subprocess.run(["tmux", "send-keys", "-t", follower_pane, "-l", "--", command], check=True)
        subprocess.run(["tmux", "send-keys", "-t", follower_pane, "Enter"], check=True)
    probe = tmp_path / "probe.txt"
    subprocess.run(
        [
            "tmux",
            "send-keys",
            "-t",
            follower_pane,
            "-l",
            "--",
            f":call writefile([tabpagenr('$'), bufloaded('{target}')], '{probe}')",
        ],
        check=True,
    )
    subprocess.run(["tmux", "send-keys", "-t", follower_pane, "Enter"], check=True)
    assert wait_until(lambda: probe.exists() and probe.read_text() == "1\n1\n", timeout=5.0), (
        "setup failed: a.py should be loaded with its tab closed"
    )
    _edit_and_assert_typed_from_the_pre_edit_buffer(monkeypatch, log, wait_until, target)


def test_interrupted_edit_hands_over_the_persisted_partial_not_the_finished_file(
    tmux_session: str,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    wait_until: Callable[..., bool],
) -> None:
    """What the user is handed at an interrupt must be exactly what the
    pending remainder was computed against. With the reload, the interrupt
    rollback landed on the RELOADED finished file while `partial` said the
    pre-edit prefix (measured: 13 lines on screen, 5 in `partial`)."""
    log, _, window_id = _observed_follower(
        tmux_session, monkeypatch, tmp_path, wait_until, "--speed", "lento"
    )
    target = tmp_path / "sample.py"
    _write(monkeypatch, log, wait_until, target, ONE_DEF)
    after = THREE_DEFS.splitlines()

    with log.open("a") as handle:
        handle.write(_MARK + "\n")
    _hook(monkeypatch, "pre", "Edit", target)
    target.write_text(THREE_DEFS)
    body = json.dumps({"tool_name": "Edit", "tool_input": {"file_path": str(target)}})
    monkeypatch.setattr("sys.stdin", io.StringIO(body))
    thread = threading.Thread(target=lambda: cli.main(["hook", "post"]))
    thread.start()
    try:
        assert wait_until(lambda: control.animating_state(window_id) == "running", timeout=10.0)
        # Let some of the edit type (lento), then interrupt mid-animation.
        assert wait_until(lambda: len(_states(log, target.name)) >= 3, timeout=15.0)
        control.request_interrupt(window_id)
        assert wait_until(lambda: control.animating_state(window_id) == "handoff", timeout=15.0)
        pending = control.load_pending_animation(window_id)
        assert pending is not None
        assert pending.partial is not None
        partial = pending.partial.splitlines()
        assert partial != after, "interrupt landed after the edit finished; nothing to measure"
        # Rollback and hand-off keystrokes may still be landing: wait for the
        # buffer to reach the partial, and fail if it never does.
        settled = wait_until(lambda: _last_state(log, target.name) == partial, timeout=5.0)
        states = _states(log, target.name)
        trace = "\n".join(f"defs={_defs(s)} lines={len(s)}" for s in states)
        assert settled, f"hand-off buffer never matched the persisted partial:\n{trace}"
        assert after not in states, f"the finished file was on screen before hand-off:\n{trace}"
    finally:
        # Release the held turn the way a user save does.
        target.write_text("the user's own version\n")
        thread.join(timeout=15.0)
    assert not thread.is_alive()


def test_pause_then_resume_never_shows_the_finished_file_before_typing_it(
    tmux_session: str,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    wait_until: Callable[..., bool],
) -> None:
    """Pause mid-Edit-animation, then resume: the same two invariants as
    the plain Edit test above, now exercised across a pause/resume cycle —
    previously only measured by hand (docs/superpowers/evidence/
    2026-09-23-tmux-edit-reload/fix/m-fixed-src-pausemid.txt) with no
    committed test (BACKLOG.md item (e), Edit-reload fix residuals).

    This guards the invariant ACROSS a pause/resume, not pause's own logic —
    on a revert to v0.2.8's tmux_vim.py the failure fires on the very first
    assertion, `after not in states_at_pause`, i.e. the finished file is
    already on screen before the pause signal is even sent. The reload bug
    (goto_file's `:tab drop`) precedes and subsumes the pause path here; it
    is not this test proving anything pause-specific. Do not read a pass
    here as evidence the pause/resume mechanics themselves are exercised
    beyond "the fix still holds while they run".

    No sleeps for synchronization: every wait is on a marker
    (`control.animating_state`) or on the observer's own log. The `finally`
    block deliberately resumes whenever it observes "paused" (a no-op once
    it hasn't) so a failed assertion between the pause and the resume can
    never leave the hook thread parked in `_wait_while_paused` forever —
    measured the hard way in an earlier draft, where exactly that ordering
    hung the whole pytest process for minutes with a live, un-resumable
    animation thread."""
    log, _, window_id = _observed_follower(
        tmux_session, monkeypatch, tmp_path, wait_until, "--speed", "lento"
    )
    target = tmp_path / "sample.py"
    _write(monkeypatch, log, wait_until, target, ONE_DEF)
    after = THREE_DEFS.splitlines()

    with log.open("a") as handle:
        handle.write(_MARK + "\n")
    _hook(monkeypatch, "pre", "Edit", target)
    target.write_text(THREE_DEFS)
    body = json.dumps({"tool_name": "Edit", "tool_input": {"file_path": str(target)}})
    monkeypatch.setattr("sys.stdin", io.StringIO(body))
    # daemon=True: a backstop only — see the docstring's last paragraph for
    # the real release (the finally block below).
    thread = threading.Thread(target=lambda: cli.main(["hook", "post"]), daemon=True)
    thread.start()
    try:
        assert wait_until(lambda: control.animating_state(window_id) == "running", timeout=10.0)
        # Let some of the edit type (lento) before pausing mid-animation.
        assert wait_until(lambda: len(_states(log, target.name)) >= 2, timeout=15.0)
        control.request_pause(window_id)
        assert wait_until(lambda: control.animating_state(window_id) == "paused", timeout=10.0)
        states_at_pause = _states(log, target.name)
    finally:

        def _release() -> bool:
            # Fires the P toggle's resume exactly once, only while genuinely
            # paused — a no-op once it has (state moves on to "running" and
            # then None), so this converges regardless of where the try
            # block above stopped.
            state = control.animating_state(window_id)
            if state == "paused":
                control.request_pause(window_id)
            return state is None

        wait_until(_release, timeout=30.0)
        thread.join(timeout=10.0)
    assert not thread.is_alive()

    states = _states(log, target.name)
    trace = "\n".join(f"defs={_defs(s)} lines={len(s)}" for s in states)
    assert after not in states_at_pause, f"finished file shown before pausing:\n{trace}"
    # Resume must have actually continued typing, not just fast-forwarded to
    # the end while nobody was looking — otherwise the invariants below would
    # pass vacuously on a pause that landed after the animation was done.
    assert len(states) > len(states_at_pause), f"resume typed nothing new:\n{trace}"
    assert states[-1] == after
    assert after not in states[:-1], f"finished file shown before the animation ended:\n{trace}"
    assert max(_defs(s) for s in states) <= _defs(after), f"duplicated defs:\n{trace}"


def test_read_of_an_open_clean_file_changed_outside_claude_shows_the_new_content(
    tmux_session: str,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    wait_until: Callable[..., bool],
) -> None:
    """Reading disk is the intent of a Read. A file open and clean in the
    follower that something else rewrote (a formatter, `sed -i`, a
    checkout) must show the new content on the next Read, not the stale
    buffer — goto_file no longer reloads, so ensure_showing has to."""
    log, _, _ = _observed_follower(tmux_session, monkeypatch, tmp_path, wait_until)
    target = tmp_path / "a.py"
    _write(monkeypatch, log, wait_until, target, ONE_DEF)
    _write(monkeypatch, log, wait_until, tmp_path / "b.py", OTHER)

    target.write_text(THREE_DEFS)  # changed on disk by something that is not Claude's Edit
    body = json.dumps({"tool_name": "Read", "tool_input": {"file_path": str(target)}})
    monkeypatch.setattr("sys.stdin", io.StringIO(body))
    assert cli.main(["hook", "post"]) == 0

    after = THREE_DEFS.splitlines()
    assert wait_until(lambda: _last_state(log, target.name) == after, timeout=5.0), (
        f"the Read left stale content on screen: {_last_state(log, target.name)!r}"
    )
