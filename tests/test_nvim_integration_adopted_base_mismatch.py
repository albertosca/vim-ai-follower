"""The base-mismatch guard in an ADOPTED nvim: the user's own editor.

A dedicated follower that finds its buffer is not the diff's base retypes the
whole file, which starts by wiping the buffer (`bwipeout!`). In an adopted
nvim that buffer may hold the user's unsaved typing, and nvim follower
buffers are never written, so 'modified' cannot tell the user's typing from
the follower's. Policy (Alberto, 2026-09-25): in an adopted editor a buffer
that is not the base is never wiped and never animated; the file is marked
stale, the status surface says so, and hook.log records it. Once the buffer
is the base again (the user ran `:e!`), the next edit animates normally.

Reproduced first by the B2 task reviewer (a user's unsaved line discarded by
the retype B2 introduced). The same wipe predates B2 for a file the follower
treats as fresh while the user has it loaded: show_fresh wipes by number
whatever buffer holds the path.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

pynvim = pytest.importorskip("pynvim")

from test_integration_edit_no_reload import _MARK, _last_state, _states  # noqa: E402
from test_nvim_integration_edit_base_mismatch import (  # noqa: E402
    _ENV,
    _OBSERVER_LUA,
    _WINDOW,
    _payload,
    _wait,
)

from vim_ai_follower import cache, hooks  # noqa: E402
from vim_ai_follower.hooks import BASE_DIFFERS_CUE  # noqa: E402
from vim_ai_follower.state import FollowerState  # noqa: E402

pytestmark = pytest.mark.integration

USER_LINE = "USER_UNSAVED = 1"


def _adopted_nvim(
    headless_nvim: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> tuple[Path, Any]:
    monkeypatch.setattr(cache, "CACHE_DIR", tmp_path / "cache")
    FollowerState.set(_WINDOW, "nvim", headless_nvim, origin="", shown_any=False, adopted=True)
    log = tmp_path / "observer.log"
    nvim = pynvim.attach("socket", path=headless_nvim)
    nvim.exec_lua(_OBSERVER_LUA, str(log))
    return log, nvim


def _buffer(nvim: Any, name: str) -> Any:
    return next(b for b in nvim.buffers if b.name.endswith("/" + name))


def _status_text(nvim: Any) -> str:
    """The status float's body, as one string (the cue is centered in it)."""
    for win in nvim.api.list_wins():
        if nvim.api.win_get_config(win).get("relative", "") == "":
            continue
        buf = nvim.api.win_get_buf(win)
        if nvim.api.buf_get_name(buf).endswith("vaf-status"):
            return " ".join(line.strip() for line in nvim.api.buf_get_lines(buf, 0, -1, True))
    return ""


def _edit(target: Path, content: str) -> None:
    assert hooks.cmd_hook_pre(_ENV, _payload("Edit", target)) == 0
    target.write_text(content)
    assert hooks.cmd_hook_post(_ENV, _payload("Edit", target)) == 0


def _assert_left_alone(nvim: Any, tmp_path: Path, target: Path, expected: list[str]) -> None:
    assert _buffer(nvim, target.name)[:] == expected, "the user's buffer was touched"
    assert BASE_DIFFERS_CUE in _status_text(nvim), f"no cue: {_status_text(nvim)!r}"
    state = FollowerState.read(_WINDOW)
    assert state is not None
    if str(target) in state.open_files:
        assert str(target) in state.stale_files
    assert str(target) in (tmp_path / "hook.log").read_text()


def test_the_users_unsaved_line_survives_an_edit_of_an_animated_buffer(
    headless_nvim: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The reviewer's reproduction: the follower animated a.py, the user then
    typed an unsaved line into it, and Claude edits a.py."""
    _, nvim = _adopted_nvim(headless_nvim, tmp_path, monkeypatch)
    target = (tmp_path / "a.py").resolve()
    target.write_text("x = 1\ny = 2\n")
    assert hooks.cmd_hook_post(_ENV, _payload("Write", target)) == 0
    _buffer(nvim, "a.py").append(USER_LINE, 0)

    _edit(target, "x = 1\ny = 3\n")

    _assert_left_alone(nvim, tmp_path, target, [USER_LINE, "x = 1", "y = 2"])


def test_a_file_the_user_opened_themselves_is_not_wiped_by_a_fresh_write(
    headless_nvim: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Pre-dates B2: the follower never opened b.py, so the Write is "fresh",
    and show_fresh used to wipe the user's own loaded buffer by number."""
    _, nvim = _adopted_nvim(headless_nvim, tmp_path, monkeypatch)
    target = (tmp_path / "b.py").resolve()
    target.write_text("a = 1\n")
    nvim.command(f"edit {target}")
    _buffer(nvim, "b.py").append(USER_LINE, 0)

    assert hooks.cmd_hook_pre(_ENV, _payload("Write", target)) == 0
    target.write_text("a = 2\n")
    assert hooks.cmd_hook_post(_ENV, _payload("Write", target)) == 0

    _assert_left_alone(nvim, tmp_path, target, [USER_LINE, "a = 1"])


def test_after_the_user_reloads_the_next_edit_animates_normally(
    headless_nvim: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The stale mark must not stick: once `:e!` makes the buffer the base
    again, the next edit is animated as a diff onto it — not retyped (which
    would wipe) and not left alone forever."""
    log, nvim = _adopted_nvim(headless_nvim, tmp_path, monkeypatch)
    target = (tmp_path / "a.py").resolve()
    target.write_text("x = 1\ny = 2\n")
    assert hooks.cmd_hook_post(_ENV, _payload("Write", target)) == 0
    _buffer(nvim, "a.py").append(USER_LINE, 0)
    _edit(target, "x = 1\ny = 3\n")  # left alone, marked stale

    nvim.command("edit!")  # the user takes Claude's version
    assert _buffer(nvim, "a.py")[:] == ["x = 1", "y = 3"]
    handle = _buffer(nvim, "a.py").number
    with log.open("a") as out:
        out.write(_MARK + "\n")
    _edit(target, "x = 1\ny = 3\nz = 4\n")

    after = ["x = 1", "y = 3", "z = 4"]
    assert _wait(lambda: _last_state(log, "a.py") == after, timeout=5.0)
    # A diff onto the reloaded buffer, not a retype: the same buffer survives
    # (show_fresh wipes and recreates it) and no frame lost the kept lines.
    assert _buffer(nvim, "a.py").number == handle
    assert all(s[:2] == ["x = 1", "y = 3"] for s in _states(log, "a.py"))
    state = FollowerState.read(_WINDOW)
    assert state is not None
    assert str(target) not in state.stale_files
    assert BASE_DIFFERS_CUE not in _status_text(nvim)


def test_a_dedicated_follower_still_retypes(
    headless_nvim: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Not adopted: the follower's buffers are its own, so B2's full retype
    stands."""
    monkeypatch.setattr(cache, "CACHE_DIR", tmp_path / "cache")
    FollowerState.set(_WINDOW, "nvim", headless_nvim, origin="", shown_any=False)
    nvim = pynvim.attach("socket", path=headless_nvim)
    target = (tmp_path / "a.py").resolve()
    target.write_text("x = 1\ny = 2\n")
    assert hooks.cmd_hook_post(_ENV, _payload("Write", target)) == 0
    target.write_text("x = 1\nFORMATTED = 2\n")  # outside Claude
    _edit(target, "x = 1\nFORMATTED = 3\n")
    assert _buffer(nvim, "a.py")[:] == ["x = 1", "FORMATTED = 3"]
    assert BASE_DIFFERS_CUE not in _status_text(nvim)
