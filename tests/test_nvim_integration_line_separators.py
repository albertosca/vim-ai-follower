"""Real-nvim proof that the follower types a file's lines as nvim holds them:
split on LF only (BACKLOG final review "Mixed line endings", Review Focus 1
of backlog-sweep-5). str.splitlines() also breaks on a form feed, \\x0b,
\\x1c-\\x1e, U+0085 and U+2028/9, which stay inside an editor's line, and it
drops the CR of a CRLF line in a MIXED file, which nvim loads with
fileformat=unix and keeps: the buffer then held lines the file does not
have, and a `:w` wrote bytes Claude never wrote."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

pynvim = pytest.importorskip("pynvim")

from test_integration_crlf import MIXED, MIXED_AFTER, _spy  # noqa: E402
from test_nvim_integration_crlf import _buffer, _saved  # noqa: E402
from test_nvim_integration_edit_base_mismatch import _ENV, _WINDOW, _payload  # noqa: E402

from vim_ai_follower import cache, hooks  # noqa: E402
from vim_ai_follower.diff import EditOp  # noqa: E402
from vim_ai_follower.state import FollowerState  # noqa: E402

pytestmark = pytest.mark.integration

SEPARATORS = {
    "form-feed": "a\x0cb\n",
    "vertical-tab": "a\x0bb\n",
    "file-separator": "a\x1cb\x1dc\x1ed\n",
    "next-line": "a\x85b\n",
    "line-separator-in-a-string": 'text = "a\u2028b\u2029c"\n',
}


def _follower(
    headless_nvim: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, adopted: bool
) -> Any:
    monkeypatch.setattr(cache, "CACHE_DIRECTORY", tmp_path / "cache")
    FollowerState.set(_WINDOW, "nvim", headless_nvim, origin="", shown_any=False, adopted=adopted)
    return pynvim.attach("socket", path=headless_nvim)


@pytest.mark.parametrize("content", list(SEPARATORS))
def test_a_form_feed_line_is_typed_as_one_line(
    headless_nvim: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, content: str
) -> None:
    text = SEPARATORS[content]
    nvim = _follower(headless_nvim, tmp_path, monkeypatch, adopted=False)
    target = (tmp_path / "a.py").resolve()
    assert hooks.cmd_hook_pre(_ENV, _payload("Write", target)) == 0
    target.write_text(text)
    assert hooks.cmd_hook_post(_ENV, _payload("Write", target)) == 0

    buffer = _buffer(nvim, "a.py")
    assert buffer[:] == [text[:-1]]
    assert _saved(nvim, buffer, tmp_path / "saved.py") == text.encode()


@pytest.mark.parametrize("adopted", [False, True], ids=["dedicated", "adopted"])
def test_a_mixed_endings_edit_keeps_its_crs(
    headless_nvim: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, adopted: bool
) -> None:
    nvim = _follower(headless_nvim, tmp_path, monkeypatch, adopted)
    target = (tmp_path / "a.py").resolve()
    if adopted:
        target.write_bytes(MIXED)
        nvim.command(f"edit {target}")
    else:
        assert hooks.cmd_hook_pre(_ENV, _payload("Write", target)) == 0
        target.write_bytes(MIXED)
        assert hooks.cmd_hook_post(_ENV, _payload("Write", target)) == 0
    assert _buffer(nvim, "a.py")[:] == ["a\r", "b"]
    handle = _buffer(nvim, "a.py").number

    probes, scripts = _spy(monkeypatch)
    assert hooks.cmd_hook_pre(_ENV, _payload("Edit", target)) == 0
    target.write_bytes(MIXED_AFTER)
    assert hooks.cmd_hook_post(_ENV, _payload("Edit", target)) == 0

    assert probes == ["holds"]
    assert scripts == [[EditOp(kind="insert", start_line=2, end_line=1, new_lines=("X\r",))]]
    buffer = _buffer(nvim, "a.py")
    assert buffer.number == handle  # a diff, not a retype
    assert buffer[:] == ["a\r", "X\r", "b"]
    assert buffer.options["fileformat"] == "unix"
    assert _saved(nvim, buffer, tmp_path / "saved.py") == MIXED_AFTER


@pytest.mark.parametrize("adopted", [False, True], ids=["dedicated", "adopted"])
def test_a_crlf_file_an_edit_makes_mixed_saves_claudes_bytes(
    headless_nvim: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, adopted: bool
) -> None:
    """A CRLF file loads as fileformat=dos with no CR in its lines; an Edit
    that leaves it MIXED types the new lines with their CRs (they are the
    lines nvim would load the file as), so the buffer must end unix, or the
    next `:w` writes each typed CR again before its CRLF."""
    nvim = _follower(headless_nvim, tmp_path, monkeypatch, adopted)
    target = (tmp_path / "a.py").resolve()
    before, after = b"a\r\nb\r\n", b"a\r\nX\nb\r\n"
    target.write_bytes(before)
    nvim.command(f"edit {target}")
    assert _buffer(nvim, "a.py").options["fileformat"] == "dos"

    assert hooks.cmd_hook_pre(_ENV, _payload("Edit", target)) == 0
    target.write_bytes(after)
    assert hooks.cmd_hook_post(_ENV, _payload("Edit", target)) == 0

    buffer = _buffer(nvim, "a.py")
    assert buffer[:] == ["a\r", "X", "b\r"]
    assert buffer.options["fileformat"] == "unix"
    assert _saved(nvim, buffer, tmp_path / "saved.py") == after
