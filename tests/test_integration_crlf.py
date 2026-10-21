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
# A CRLF file whose last line has no line break still loads as
# fileformat=dos in both editors (measured on Vim 9.2 and nvim 0.12): typed
# with its CRs kept, it wrote every CR doubled at the next `:w` (final
# review of backlog-sweep-5, C1).
ENDINGS = {
    "final-eol": (BEFORE, AFTER),
    "no-final-eol": (b"a\r\nb", b"a\r\nX\r\nb"),
}


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
        if wait_until(lambda: dump.exists() and dump.read_bytes().startswith(b"DUMP\n"), 5.0):
            break
    # Bytes split on LF only: read_text() and splitlines() would both drop a
    # CR the buffer line really holds.
    lines = dump.read_bytes().decode().split("\n")[1:-1]
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

    def probe(*arguments: Any) -> Any:
        answer = real_probe(*arguments)
        probes.append(answer)
        return answer

    def script(before: str, after: str) -> list[EditOp]:
        ops = real_script(before, after)
        scripts.append(ops)
        return ops

    monkeypatch.setattr(hooks, "_probe_base", probe)
    monkeypatch.setattr(diff, "compute_edit_script", script)
    return probes, scripts


@pytest.mark.parametrize("ending", list(ENDINGS))
@pytest.mark.parametrize("adopted", [False, True], ids=["dedicated", "adopted"])
def test_an_edit_of_a_crlf_file_is_one_typed_insert(
    tmux_session: str,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    wait_until: Callable[..., bool],
    adopted: bool,
    ending: str,
) -> None:
    before, after = ENDINGS[ending]
    log, pane, window_id = _observed_follower(tmux_session, monkeypatch, tmp_path, wait_until)
    target = tmp_path / "a.py"
    if adopted:
        FollowerState.update(window_id, adopted=True)
        target.write_bytes(before)
        _keys(pane, f":tabedit {target}")
    else:
        _hook(monkeypatch, "pre", "Write", target)
        target.write_bytes(before)
        _hook(monkeypatch, "post", "Write", target)
    assert wait_until(lambda: _last_state(log, "a.py") == ["a", "b"], timeout=15.0)
    assert _dump(pane, target, tmp_path, wait_until) == ("dos", ["a", "b"])

    probes, scripts = _spy(monkeypatch)
    with log.open("a") as handle:
        handle.write(_MARK + "\n")
    _hook(monkeypatch, "pre", "Edit", target)
    target.write_bytes(after)
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
    assert target.read_bytes() == after
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


# The line endings of the file Claude wrote, as a save of the buffer writes
# them (Task 12 fix round 1): a buffer left on its pre-edit 'fileformat'
# writes the OLD endings at the user's next `:w`, undoing that part of the
# edit, and a hand-off save then reads as the user's own version.
FORMAT_CHANGES = {
    "lf-to-crlf": (b"a\nb\n", b"a\r\nX\r\nb\r\n"),
    "crlf-to-lf": (b"a\r\nb\r\n", b"a\nX\nb\n"),
    "endings-only": (b"a\nb\n", b"a\r\nb\r\n"),
}


@pytest.mark.parametrize("change", list(FORMAT_CHANGES))
@pytest.mark.parametrize("adopted", [False, True], ids=["dedicated", "adopted"])
def test_a_save_after_an_edit_writes_the_line_endings_claude_wrote(
    tmux_session: str,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    wait_until: Callable[..., bool],
    adopted: bool,
    change: str,
) -> None:
    before, after = FORMAT_CHANGES[change]
    log, pane, window_id = _observed_follower(tmux_session, monkeypatch, tmp_path, wait_until)
    target = tmp_path / "a.py"
    if adopted:
        FollowerState.update(window_id, adopted=True)
        target.write_bytes(before)
        _keys(pane, f":tabedit {target}")
    else:
        _hook(monkeypatch, "pre", "Write", target)
        target.write_bytes(before)
        _hook(monkeypatch, "post", "Write", target)
    assert wait_until(lambda: _last_state(log, "a.py") == ["a", "b"], timeout=15.0)

    _hook(monkeypatch, "pre", "Edit", target)
    target.write_bytes(after)
    _hook(monkeypatch, "post", "Edit", target)
    lines = after.decode().splitlines()
    assert wait_until(lambda: _last_state(log, "a.py") == lines, timeout=15.0)

    copy = tmp_path / "saved.py"
    for _attempt in range(3):  # a loaded machine can swallow the Escape pair
        _keys(pane, f":silent write! {copy}")
        if wait_until(lambda: copy.exists() and copy.read_bytes() == after, 5.0):
            break
    assert copy.read_bytes() == after


@pytest.mark.parametrize("change", ["lf-to-crlf", "crlf-to-lf"])
def test_a_read_reload_takes_the_line_endings_of_the_file_on_disk(
    tmux_session: str,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    wait_until: Callable[..., bool],
    change: str,
) -> None:
    before, after = FORMAT_CHANGES[change]
    log, pane, _ = _observed_follower(tmux_session, monkeypatch, tmp_path, wait_until)
    target = tmp_path / "a.py"
    _hook(monkeypatch, "pre", "Write", target)
    target.write_bytes(before)
    _hook(monkeypatch, "post", "Write", target)
    assert wait_until(lambda: _last_state(log, "a.py") == ["a", "b"], timeout=15.0)
    target.write_bytes(after)  # rewritten outside Claude (a formatter, `sed -i`)

    _hook(monkeypatch, "post", "Read", target)
    lines = after.decode().splitlines()
    assert wait_until(lambda: _last_state(log, "a.py") == lines, timeout=15.0)

    copy = tmp_path / "saved.py"
    for _attempt in range(3):  # a loaded machine can swallow the Escape pair
        _keys(pane, f":silent write! {copy}")
        if wait_until(lambda: copy.exists() and copy.read_bytes() == after, 5.0):
            break
    assert copy.read_bytes() == after


MIXED = b"a\r\nb\n"
MIXED_AFTER = b"a\r\nX\r\nb\n"


@pytest.mark.parametrize("adopted", [False, True], ids=["dedicated", "adopted"])
def test_a_mixed_ending_file_holds_its_base(
    tmux_session: str,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    wait_until: Callable[..., bool],
    adopted: bool,
) -> None:
    """Task 12 fix round 1: Vim loads a file that mixes CRLF and LF with
    fileformat=unix and keeps each CR in its line (shown as ^M). The
    snapshot now keeps those CRs too, so the probe says "holds" (at
    bbc4668 the snapshot dropped them and an adopted Vim left every Edit of
    such a file alone) and the Edit is the one insert, its CR typed too
    (backlog-sweep-5 T24: splitlines() used to drop it)."""
    log, pane, window_id = _observed_follower(tmux_session, monkeypatch, tmp_path, wait_until)
    target = tmp_path / "a.py"
    if adopted:
        FollowerState.update(window_id, adopted=True)
        target.write_bytes(MIXED)
        _keys(pane, f":tabedit {target}")
        loaded = ["a\r", "b"]
    else:
        _hook(monkeypatch, "pre", "Write", target)
        target.write_bytes(MIXED)
        _hook(monkeypatch, "post", "Write", target)
        loaded = ["a\r", "b"]  # the relock's `:e!` re-reads the file
    assert wait_until(lambda: _last_state(log, "a.py") == loaded, timeout=15.0)
    assert _dump(pane, target, tmp_path, wait_until) == ("unix", loaded)

    probes, scripts = _spy(monkeypatch)
    _hook(monkeypatch, "pre", "Edit", target)
    target.write_bytes(MIXED_AFTER)
    _hook(monkeypatch, "post", "Edit", target)

    assert probes == ["holds"]
    assert scripts == [[EditOp(kind="insert", start_line=2, end_line=1, new_lines=("X\r",))]]
    assert target.read_bytes() == MIXED_AFTER


def test_an_adopted_buffer_the_user_stripped_of_its_crs_is_left_alone(
    tmux_session: str,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    wait_until: Callable[..., bool],
) -> None:
    """T12+T13 review I1 (reproduced): the user loaded a mixed-ending file and
    ran `:%s/\\r$//`, unsaved. Its lines equal the file's CR-stripped form,
    which buffer_forms accepted for ANY buffer: the probe said "holds", the
    insert was typed, and the relock's `:e!` threw the user's edit away."""
    log, pane, window_id = _observed_follower(tmux_session, monkeypatch, tmp_path, wait_until)
    FollowerState.update(window_id, adopted=True)
    target = tmp_path / "a.py"
    target.write_bytes(MIXED)
    _keys(pane, f":tabedit {target}", ":silent %s/\\r$//")
    assert wait_until(lambda: _last_state(log, "a.py") == ["a", "b"], timeout=15.0)

    probes, scripts = _spy(monkeypatch)
    _hook(monkeypatch, "pre", "Edit", target)
    target.write_bytes(MIXED_AFTER)
    _hook(monkeypatch, "post", "Edit", target)

    assert probes == ["differs"]
    assert scripts == []
    assert _dump(pane, target, tmp_path, wait_until) == ("unix", ["a", "b"])
