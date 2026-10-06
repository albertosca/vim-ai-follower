"""Real tmux+vim proof that an Edit of a CRLF file is followed like any
other: Vim loads it with fileformat=dos and shows its lines without the CRs,
so the follower probes, diffs and types that form, and leaves the file on
disk alone.

Task 12 of backlog-sweep-4 (BACKLOG "CRLF files"): the hook's snapshot was
read with read_text(), whose universal newlines dropped every CR, while the
post hook decoded the file Claude wrote from its raw bytes with the CRs
kept. The hand-off wait compared the two and released Claude at its first
poll as if the user had saved (tests/test_hooks_handoff_loop.py); these
tests pin the editor side of the same file.
"""

from __future__ import annotations

import subprocess
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
from test_integration_edit_no_reload import _MARK, _hook, _last_state, _observed_follower, _states

from vim_ai_follower import diff, hooks
from vim_ai_follower.diff import EditOp
from vim_ai_follower.state import FollowerState

pytestmark = pytest.mark.integration

BEFORE = b"a\r\nb\r\n"
AFTER = b"a\r\nX\r\nb\r\n"


def _keys(pane: str, *commands: str) -> None:
    subprocess.run(["tmux", "send-keys", "-t", pane, "Escape", "Escape"], check=True)
    for command in commands:
        subprocess.run(["tmux", "send-keys", "-t", pane, "-l", "--", command], check=True)
        subprocess.run(["tmux", "send-keys", "-t", pane, "Enter"], check=True)


def _dump(
    pane: str, target: Path, tmp_path: Path, wait_until: Callable[..., bool]
) -> tuple[str, list[str]]:
    """`target`'s real buffer and its 'fileformat', dumped by Vim behind a
    sentinel line (never capture-pane, which shows the rendered screen)."""
    dump = tmp_path / "dump.txt"
    for _attempt in range(3):  # a loaded machine can swallow the Escape pair
        dump.unlink(missing_ok=True)
        _keys(
            pane,
            f":call writefile(['DUMP', getbufvar(bufnr('{target}'), '&fileformat')]"
            f" + getbufline(bufnr('{target}'), 1, '$'), '{dump}')",
        )
        if wait_until(lambda: dump.exists() and dump.read_text().startswith("DUMP\n"), 5.0):
            break
    lines = dump.read_text().splitlines()[1:]
    return lines[0], lines[1:]


def _mtime(path: Path) -> int | None:
    """None while Vim's backup-and-rename write has the file moved away."""
    try:
        return path.stat().st_mtime_ns
    except OSError:
        return None


def _spy(monkeypatch: pytest.MonkeyPatch) -> tuple[list[str], list[list[EditOp]]]:
    """Every base probe's answer and every edit script the hooks compute."""
    probes: list[str] = []
    scripts: list[list[EditOp]] = []
    real_probe = hooks._probe_base
    real_script = diff.compute_edit_script

    def probe(*args: Any) -> Any:
        answer = real_probe(*args)
        probes.append(answer)
        return answer

    def script(before: str, after: str) -> list[EditOp]:
        ops = real_script(before, after)
        scripts.append(ops)
        return ops

    monkeypatch.setattr(hooks, "_probe_base", probe)
    monkeypatch.setattr(diff, "compute_edit_script", script)
    return probes, scripts


@pytest.mark.parametrize("adopted", [False, True], ids=["dedicated", "adopted"])
def test_an_edit_of_a_crlf_file_is_one_typed_insert(
    tmux_session: str,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    wait_until: Callable[..., bool],
    adopted: bool,
) -> None:
    log, pane, window_id = _observed_follower(tmux_session, monkeypatch, tmp_path, wait_until)
    target = tmp_path / "a.py"
    if adopted:
        FollowerState.update(window_id, adopted=True)
        target.write_bytes(BEFORE)
        _keys(pane, f":tabedit {target}")
    else:
        _hook(monkeypatch, "pre", "Write", target)
        target.write_bytes(BEFORE)
        _hook(monkeypatch, "post", "Write", target)
    assert wait_until(lambda: _last_state(log, "a.py") == ["a", "b"], timeout=15.0)
    assert _dump(pane, target, tmp_path, wait_until) == ("dos", ["a", "b"])

    probes, scripts = _spy(monkeypatch)
    with log.open("a") as handle:
        handle.write(_MARK + "\n")
    _hook(monkeypatch, "pre", "Edit", target)
    target.write_bytes(AFTER)
    written = target.stat().st_mtime_ns
    _hook(monkeypatch, "post", "Edit", target)

    assert wait_until(lambda: _last_state(log, "a.py") == ["a", "X", "b"], timeout=15.0)
    assert probes == ["holds"]
    assert scripts == [[EditOp(kind="insert", start_line=2, end_line=1, new_lines=("X",))]]
    # Typed as that one insert: the kept lines stay on screen throughout (a
    # retype starts from one empty line), and no frame carries a CR.
    states = _states(log, "a.py")
    assert all(state[0] == "a" and state[-1] == "b" for state in states), states
    assert not any("\r" in line for state in states for line in state), states
    assert _dump(pane, target, tmp_path, wait_until) == ("dos", ["a", "X", "b"])
    assert target.read_bytes() == AFTER
    assert target.stat().st_mtime_ns == written


def test_a_save_of_the_dos_buffer_writes_the_crlf_bytes_back(
    tmux_session: str,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    wait_until: Callable[..., bool],
) -> None:
    """What the hand-off wait compares with Claude's file: a user's `:w` of
    the shown buffer writes CRLF again, so a save of Claude's version is
    byte-for-byte that version (the "saved it unchanged" release)."""
    log, pane, window_id = _observed_follower(tmux_session, monkeypatch, tmp_path, wait_until)
    FollowerState.update(window_id, adopted=True)
    target = tmp_path / "a.py"
    target.write_bytes(AFTER)
    _keys(pane, f":tabedit {target}")
    assert wait_until(lambda: _last_state(log, "a.py") == ["a", "X", "b"], timeout=15.0)
    written = target.stat().st_mtime_ns

    _keys(pane, ":silent write")

    assert wait_until(lambda: _mtime(target) not in (None, written), timeout=5.0)
    assert target.read_bytes() == AFTER
