"""Keys the user types at the moment of a hand-off never mix with the lines
the hand-off itself sends (T20 re-review m5, a real tmux + Vim at the
follower pane's 49 columns).

A replay the user interrupts again (_rearm_handoff) hands the buffer back
with hand_over's typed lines (`:silent! CocEnable`, the define line, the
hand_over call). The marker read "handoff" before those sends, so a user
(or a test) who took it as "the buffer is mine" typed into the middle of
them: `:silent! CocEnable` landed in the buffer 2 of 3 runs. The marker now
reads "handoff" only after them.

VAF_HANDOFF_RACE_RUNS repeats it (the measurement ran 20).
"""

from __future__ import annotations

import io
import json
import os
import subprocess
import threading
from collections.abc import Callable
from pathlib import Path

import pytest
from test_integration_edit_no_reload import _MARK, _observed_follower, _states

from vim_ai_follower import cli, control

pytestmark = pytest.mark.integration

CONTENT = "".join(f"line_{number} = {number}\n" for number in range(1, 13))
RUNS = int(os.environ.get("VAF_HANDOFF_RACE_RUNS", "1"))


def _screen(pane: str) -> str:
    return subprocess.run(
        ["tmux", "capture-pane", "-p", "-t", pane], capture_output=True, text=True, check=True
    ).stdout


def _keys(pane: str, *commands: str) -> None:
    for command in commands:
        subprocess.run(["tmux", "send-keys", "-t", pane, "-l", "--", command], check=True)
        subprocess.run(["tmux", "send-keys", "-t", pane, "Enter"], check=True)


def _dump(
    pane: str, target: Path, tmp_path: Path, wait_until: Callable[..., bool]
) -> tuple[list[str], list[str]]:
    """(the buffer's lines, Vim's error messages since the run began)."""
    dump = tmp_path / "dump.txt"
    dump.unlink(missing_ok=True)
    _keys(
        pane,
        "\x1b\x1b",
        f":call writefile(['DUMP'] + getbufline(bufnr('{target}'), 1, '$') + ['MESSAGES']"
        f" + split(execute('messages'), \"\\n\"), '{dump}')",
    )
    assert wait_until(
        lambda: dump.exists() and dump.read_text().startswith("DUMP\n"), timeout=5.0
    ), _screen(pane)
    lines = dump.read_text().splitlines()[1:]
    split = lines.index("MESSAGES")
    errors = [line for line in lines[split + 1 :] if line.startswith("E") and ":" in line]
    return lines[:split], errors


@pytest.mark.parametrize("run", range(RUNS))
def test_keys_typed_at_a_hand_off_never_mix_with_its_lines(
    run: int,
    tmux_session: str,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    wait_until: Callable[..., bool],
) -> None:
    log, pane, window_id = _observed_follower(
        tmux_session, monkeypatch, tmp_path, wait_until, "--speed", "lento"
    )
    target = tmp_path / f"new_{run}.py"
    with log.open("a") as handle:
        handle.write(_MARK + "\n")
    body = json.dumps({"tool_name": "Write", "tool_input": {"file_path": str(target)}})
    monkeypatch.setattr("sys.stdin", io.StringIO(body))
    assert cli.main(["hook", "pre"]) == 0
    target.write_text(CONTENT)
    monkeypatch.setattr("sys.stdin", io.StringIO(body))
    thread = threading.Thread(target=lambda: cli.main(["hook", "post"]))
    thread.start()
    full = CONTENT.splitlines()
    try:
        assert wait_until(lambda: control.animating_state(window_id) == "running", timeout=10.0)
        assert wait_until(
            lambda: any(2 <= len(lines) < len(full) for lines in _states(log, target.name)),
            timeout=15.0,
        )
        control.request_interrupt(window_id)
        assert wait_until(
            lambda: (
                control.animating_state(window_id) == "handoff"
                and control.has_pending_animation(window_id)
            ),
            timeout=15.0,
        )
        # The des-interrupt: the replay runs, and is interrupted in turn.
        control.request_interrupt(window_id)
        assert wait_until(lambda: control.animating_state(window_id) == "running", timeout=10.0)
        control.request_interrupt(window_id)
        assert wait_until(lambda: control.animating_state(window_id) == "handoff", timeout=15.0)
        # The user takes "handoff" as theirs and types at once.
        _keys(pane, ":echo 1", ":echo 1", ":echo 1")
        assert wait_until(lambda: control.has_pending_animation(window_id), timeout=10.0)
        pending = control.load_pending_animation(window_id)
        assert pending is not None
        assert pending.partial is not None
        buffer, errors = _dump(pane, target, tmp_path, wait_until)
        assert errors == [], errors
        assert buffer == pending.partial.splitlines(), buffer
    finally:
        target.write_text("released by the test\n")
        thread.join(timeout=15.0)
    assert not thread.is_alive()
