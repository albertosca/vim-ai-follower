"""Real-nvim twin of tests/test_integration_crlf.py (Task 12 of
backlog-sweep-4, BACKLOG "CRLF files"): nvim loads a CRLF file with
fileformat=dos and shows its lines without the CRs, so an Edit of it is one
typed insert onto that buffer, which keeps 'fileformat' dos, and the file on
disk is never touched by the follower."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

pynvim = pytest.importorskip("pynvim")

from test_integration_crlf import (  # noqa: E402
    AFTER,
    BEFORE,
    FORMAT_CHANGES,
    MIXED,
    MIXED_AFTER,
    _spy,
)
from test_integration_edit_no_reload import _MARK, _last_state, _states  # noqa: E402
from test_nvim_integration_edit_base_mismatch import (  # noqa: E402
    _ENV,
    _OBSERVER_LUA,
    _WINDOW,
    _payload,
    _wait,
)

from vim_ai_follower import cache, hooks  # noqa: E402
from vim_ai_follower.diff import EditOp  # noqa: E402
from vim_ai_follower.state import FollowerState  # noqa: E402

pytestmark = pytest.mark.integration


def _buffer(nvim: Any, name: str) -> Any:
    return next(buffer for buffer in nvim.buffers if buffer.name.endswith("/" + name))


@pytest.mark.parametrize("adopted", [False, True], ids=["dedicated", "adopted"])
def test_an_edit_of_a_crlf_file_is_one_typed_insert(
    headless_nvim: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, adopted: bool
) -> None:
    monkeypatch.setattr(cache, "CACHE_DIR", tmp_path / "cache")
    FollowerState.set(_WINDOW, "nvim", headless_nvim, origin="", shown_any=False, adopted=adopted)
    log = tmp_path / "observer.log"
    nvim = pynvim.attach("socket", path=headless_nvim)
    nvim.exec_lua(_OBSERVER_LUA, str(log))
    target = (tmp_path / "a.py").resolve()
    if adopted:
        target.write_bytes(BEFORE)
        nvim.command(f"edit {target}")
    else:
        assert hooks.cmd_hook_pre(_ENV, _payload("Write", target)) == 0
        target.write_bytes(BEFORE)
        assert hooks.cmd_hook_post(_ENV, _payload("Write", target)) == 0
    assert _buffer(nvim, "a.py")[:] == ["a", "b"]
    assert _buffer(nvim, "a.py").options["fileformat"] == "dos"
    handle = _buffer(nvim, "a.py").number

    probes, scripts = _spy(monkeypatch)
    with log.open("a") as out:
        out.write(_MARK + "\n")
    assert hooks.cmd_hook_pre(_ENV, _payload("Edit", target)) == 0
    target.write_bytes(AFTER)
    written = target.stat().st_mtime_ns
    assert hooks.cmd_hook_post(_ENV, _payload("Edit", target)) == 0

    assert _wait(lambda: _last_state(log, "a.py") == ["a", "X", "b"], timeout=5.0)
    assert probes == ["holds"]
    assert scripts == [[EditOp(kind="insert", start_line=2, end_line=1, new_lines=("X",))]]
    states = _states(log, "a.py")
    assert all(state[0] == "a" and state[-1] == "b" for state in states), states
    assert not any("\r" in line for state in states for line in state), states
    buffer = _buffer(nvim, "a.py")
    assert buffer.number == handle  # a diff, not a retype
    assert buffer[:] == ["a", "X", "b"]
    assert buffer.options["fileformat"] == "dos"
    assert target.read_bytes() == AFTER
    assert target.stat().st_mtime_ns == written

    # What a hand-off save writes: the dos buffer goes back as CRLF, so a
    # user who saves Claude's version saves exactly Claude's bytes.
    copy = tmp_path / "saved.py"
    nvim.exec_lua(
        "local buf, path = ...; vim.api.nvim_buf_call(buf, function()"
        " vim.cmd.write({ args = { path }, bang = true, mods = { silent = true } }) end)",
        buffer.number,
        str(copy),
    )
    assert copy.read_bytes() == AFTER


def _saved(nvim: Any, buffer: Any, copy: Path) -> bytes:
    """What a `:w!` of `buffer` writes, to the scratch file `copy`."""
    nvim.exec_lua(
        "local buf, path = ...; vim.api.nvim_buf_call(buf, function()"
        " vim.cmd.write({ args = { path }, bang = true, mods = { silent = true } }) end)",
        buffer.number,
        str(copy),
    )
    return copy.read_bytes()


@pytest.mark.parametrize("change", list(FORMAT_CHANGES))
@pytest.mark.parametrize("adopted", [False, True], ids=["dedicated", "adopted"])
def test_a_save_after_an_edit_writes_the_line_endings_claude_wrote(
    headless_nvim: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, adopted: bool, change: str
) -> None:
    """Task 12 fix round 1: 'fileformat' follows the file Claude wrote, the
    way 'bomb' does (_format_from_disk). At f9d35d1 the buffer kept its
    pre-edit format, so this save wrote the OLD line endings."""
    before, after = FORMAT_CHANGES[change]
    monkeypatch.setattr(cache, "CACHE_DIR", tmp_path / "cache")
    FollowerState.set(_WINDOW, "nvim", headless_nvim, origin="", shown_any=False, adopted=adopted)
    nvim = pynvim.attach("socket", path=headless_nvim)
    target = (tmp_path / "a.py").resolve()
    if adopted:
        target.write_bytes(before)
        nvim.command(f"edit {target}")
    else:
        assert hooks.cmd_hook_pre(_ENV, _payload("Write", target)) == 0
        target.write_bytes(before)
        assert hooks.cmd_hook_post(_ENV, _payload("Write", target)) == 0
    assert _buffer(nvim, "a.py")[:] == ["a", "b"]
    assert _saved(nvim, _buffer(nvim, "a.py"), tmp_path / "first.py") == before

    assert hooks.cmd_hook_pre(_ENV, _payload("Edit", target)) == 0
    target.write_bytes(after)
    assert hooks.cmd_hook_post(_ENV, _payload("Edit", target)) == 0

    buffer = _buffer(nvim, "a.py")
    assert buffer[:] == after.decode().splitlines()
    assert _saved(nvim, buffer, tmp_path / "saved.py") == after


@pytest.mark.parametrize("change", ["lf-to-crlf", "crlf-to-lf"])
def test_a_read_reload_takes_the_line_endings_of_the_file_on_disk(
    headless_nvim: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, change: str
) -> None:
    """A Read re-reads a clean buffer (`_reload_if_clean`'s `edit!`), and
    nvim's own load detects 'fileformat' again."""
    before, after = FORMAT_CHANGES[change]
    monkeypatch.setattr(cache, "CACHE_DIR", tmp_path / "cache")
    FollowerState.set(_WINDOW, "nvim", headless_nvim, origin="", shown_any=False)
    nvim = pynvim.attach("socket", path=headless_nvim)
    target = (tmp_path / "a.py").resolve()
    assert hooks.cmd_hook_pre(_ENV, _payload("Write", target)) == 0
    target.write_bytes(before)
    assert hooks.cmd_hook_post(_ENV, _payload("Write", target)) == 0
    target.write_bytes(after)  # rewritten outside Claude (a formatter, `sed -i`)

    assert hooks.cmd_hook_post(_ENV, _payload("Read", target)) == 0

    buffer = _buffer(nvim, "a.py")
    assert buffer[:] == after.decode().splitlines()
    assert _saved(nvim, buffer, tmp_path / "saved.py") == after


@pytest.mark.parametrize("adopted", [False, True], ids=["dedicated", "adopted"])
def test_a_mixed_ending_file_holds_its_base(
    headless_nvim: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, adopted: bool
) -> None:
    """Task 12 fix round 1. nvim loads a file mixing CRLF and LF with
    fileformat=unix and keeps each CR in its line, so an adopted buffer holds
    ["a\\r", "b"]. A dedicated follower's own retype splits lines with
    splitlines() and holds ["a", "b"]: buffer_forms accepts that form too,
    or every Edit of the file would wipe and retype it."""
    monkeypatch.setattr(cache, "CACHE_DIR", tmp_path / "cache")
    FollowerState.set(_WINDOW, "nvim", headless_nvim, origin="", shown_any=False, adopted=adopted)
    nvim = pynvim.attach("socket", path=headless_nvim)
    target = (tmp_path / "a.py").resolve()
    if adopted:
        target.write_bytes(MIXED)
        nvim.command(f"edit {target}")
        loaded = ["a\r", "b"]
    else:
        assert hooks.cmd_hook_pre(_ENV, _payload("Write", target)) == 0
        target.write_bytes(MIXED)
        assert hooks.cmd_hook_post(_ENV, _payload("Write", target)) == 0
        loaded = ["a", "b"]
    assert _buffer(nvim, "a.py")[:] == loaded
    assert _buffer(nvim, "a.py").options["fileformat"] == "unix"
    handle = _buffer(nvim, "a.py").number

    probes, scripts = _spy(monkeypatch)
    assert hooks.cmd_hook_pre(_ENV, _payload("Edit", target)) == 0
    target.write_bytes(MIXED_AFTER)
    assert hooks.cmd_hook_post(_ENV, _payload("Edit", target)) == 0

    assert probes == ["holds"]
    assert scripts == [[EditOp(kind="insert", start_line=2, end_line=1, new_lines=("X",))]]
    assert _buffer(nvim, "a.py").number == handle  # a diff, not a retype
    assert target.read_bytes() == MIXED_AFTER


def test_an_adopted_buffer_the_user_stripped_of_its_crs_is_left_alone(
    headless_nvim: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """T12+T13 review I1: the CR-stripped form of a mixed-ending file is the
    buffer only the follower's own retype leaves; a user's modified buffer
    in that form is not the base, and an adopted editor leaves it alone."""
    monkeypatch.setattr(cache, "CACHE_DIR", tmp_path / "cache")
    FollowerState.set(_WINDOW, "nvim", headless_nvim, origin="", shown_any=False, adopted=True)
    nvim = pynvim.attach("socket", path=headless_nvim)
    target = (tmp_path / "a.py").resolve()
    target.write_bytes(MIXED)
    nvim.command(f"edit {target}")
    nvim.command("silent %s/\\r$//")
    assert _buffer(nvim, "a.py")[:] == ["a", "b"]
    assert _buffer(nvim, "a.py").options["modified"]

    probes, scripts = _spy(monkeypatch)
    assert hooks.cmd_hook_pre(_ENV, _payload("Edit", target)) == 0
    target.write_bytes(MIXED_AFTER)
    assert hooks.cmd_hook_post(_ENV, _payload("Edit", target)) == 0

    assert probes == ["differs"]
    assert scripts == []
    assert _buffer(nvim, "a.py")[:] == ["a", "b"]


def test_an_adopted_buffer_the_follower_retyped_still_holds_its_base(
    headless_nvim: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The other side of I1: a mixed-ending file the follower itself typed
    into an adopted nvim (nothing held it, so a Write retypes it) carries no
    CR, and is stamped clean at the end of that animation; the next Edit is
    a diff onto it, not a buffer left alone forever."""
    monkeypatch.setattr(cache, "CACHE_DIR", tmp_path / "cache")
    FollowerState.set(_WINDOW, "nvim", headless_nvim, origin="", shown_any=False, adopted=True)
    nvim = pynvim.attach("socket", path=headless_nvim)
    target = (tmp_path / "a.py").resolve()
    assert hooks.cmd_hook_pre(_ENV, _payload("Write", target)) == 0
    target.write_bytes(MIXED)
    assert hooks.cmd_hook_post(_ENV, _payload("Write", target)) == 0
    assert _buffer(nvim, "a.py")[:] == ["a", "b"]
    handle = _buffer(nvim, "a.py").number

    probes, scripts = _spy(monkeypatch)
    assert hooks.cmd_hook_pre(_ENV, _payload("Edit", target)) == 0
    target.write_bytes(MIXED_AFTER)
    assert hooks.cmd_hook_post(_ENV, _payload("Edit", target)) == 0

    assert probes == ["holds"]
    assert scripts == [[EditOp(kind="insert", start_line=2, end_line=1, new_lines=("X",))]]
    assert _buffer(nvim, "a.py").number == handle
    assert _buffer(nvim, "a.py")[:] == ["a", "X", "b"]


@pytest.mark.parametrize("change", ["endings-only", "lf-to-crlf"])
def test_an_adopted_clean_buffer_stays_clean_when_only_its_format_followed(
    headless_nvim: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, change: str
) -> None:
    """T12+T13 review M3: setting 'fileformat' (or 'bomb') marks a buffer
    modified. An endings-only Edit of a CLEAN adopted buffer types nothing,
    so it left the buffer shown `[+]` (and `:q` refusing with E37) while its
    lines and format equal the file. A buffer the animation typed into is
    really modified and stays so."""
    before, after = FORMAT_CHANGES[change]
    monkeypatch.setattr(cache, "CACHE_DIR", tmp_path / "cache")
    FollowerState.set(_WINDOW, "nvim", headless_nvim, origin="", shown_any=False, adopted=True)
    nvim = pynvim.attach("socket", path=headless_nvim)
    target = (tmp_path / "a.py").resolve()
    target.write_bytes(before)
    nvim.command(f"edit {target}")
    assert not _buffer(nvim, "a.py").options["modified"]

    assert hooks.cmd_hook_pre(_ENV, _payload("Edit", target)) == 0
    target.write_bytes(after)
    assert hooks.cmd_hook_post(_ENV, _payload("Edit", target)) == 0

    buffer = _buffer(nvim, "a.py")
    assert buffer.options["fileformat"] == "dos"
    assert buffer.options["modified"] is (change != "endings-only")
    assert _saved(nvim, buffer, tmp_path / "saved.py") == after
