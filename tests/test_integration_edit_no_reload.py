"""Real tmux+vim proof that an Edit of an already-open file animates FROM the
pre-edit buffer, instead of first reloading the finished file from disk.

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
causing it. So this test watches the MIDDLE: a Vim timer, loaded through
VIMINIT into the follower's own Vim, appends every distinct (file, content)
state of the real buffer to a log. It never sends a key into the pane, so
the animation under test is untouched.

What a missed sample means: the timer polls every 15 ms and logs only
changes, so it can skip a short-lived state but never invent one. Both
invariants below are "this bad state must never be seen", so a missed
sample can only hide a violation, never fabricate one — the test cannot go
red on a correct run by chance. Two independent symptoms of the bug are
checked (the finished content showing up before the last state, and more
`def`s than the finished file has) and the duplicated states last for
whole lines of typing, so the red is not hostage to one lucky frame.
"""

from __future__ import annotations

import io
import json
import subprocess
from collections.abc import Callable
from pathlib import Path

import pytest

from vim_ai_follower import cli

pytestmark = pytest.mark.integration

ONE_DEF = "import os\n\n\ndef alpha(root):\n    return os.path.join(root, 'a')\n"
THREE_DEFS = (
    "import os\n\n\ndef alpha(root):\n    return os.path.join(root, 'a')\n"
    "\n\ndef bravo(root):\n    return 'b'\n"
    "\n\ndef charlie(root):\n    return 'c'\n"
)

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


def test_edit_of_an_open_file_never_shows_the_finished_file_before_typing_it(
    tmux_session: str,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    wait_until: Callable[..., bool],
) -> None:
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
    assert cli.main(["start"]) == 0
    assert wait_until(lambda: log.exists(), timeout=10.0), "observer never started"

    target = tmp_path / "sample.py"
    before = ONE_DEF.splitlines()
    after = THREE_DEFS.splitlines()

    _hook(monkeypatch, "pre", "Write", target)
    target.write_text(ONE_DEF)
    _hook(monkeypatch, "post", "Write", target)
    assert wait_until(lambda: _last_state(log, target.name) == before, timeout=15.0)

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
