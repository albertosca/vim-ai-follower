"""Real tmux+vim proof that the follower types a file's lines as Vim holds
them: split on LF only (BACKLOG final review "Mixed line endings", Review
Focus 1 of backlog-sweep-5). str.splitlines() also breaks on a form feed,
\\x0b, \\x1c-\\x1e, U+0085 and U+2028/9, which stay inside Vim's line, and it
drops the CR of a CRLF line in a MIXED file, which Vim loads with
fileformat=unix and keeps. The completion relock's `:e!` hid both at the
end of a run, but every frame before it (and the buffer an interrupt hands
over) held lines the file does not have. A control character inside a line
is typed after a CTRL-V, so insert mode takes it literally (a CR would
otherwise break the line, a CTRL-K start a digraph)."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import pytest
from test_integration_crlf import MIXED, MIXED_AFTER, _dump, _keys, _spy
from test_integration_edit_no_reload import _MARK, _hook, _last_state, _observed_follower, _states
from test_nvim_integration_line_separators import SEPARATORS

from vim_ai_follower.diff import EditOp
from vim_ai_follower.state import FollowerState

pytestmark = pytest.mark.integration


@pytest.mark.parametrize("content", list(SEPARATORS))
def test_a_form_feed_line_is_typed_as_one_line(
    tmux_session: str,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    wait_until: Callable[..., bool],
    content: str,
) -> None:
    text = SEPARATORS[content]
    log, pane, _window_id = _observed_follower(tmux_session, monkeypatch, tmp_path, wait_until)
    target = tmp_path / "a.py"
    with log.open("a") as handle:
        handle.write(_MARK + "\n")
    _hook(monkeypatch, "pre", "Write", target)
    target.write_text(text)
    _hook(monkeypatch, "post", "Write", target)

    assert wait_until(lambda: _last_state(log, "a.py") == [text[:-1]], timeout=15.0)
    # Every frame, not only the end the relock's `:e!` gives: one line,
    # growing towards the file's.
    states = _states(log, "a.py")
    assert all(len(state) == 1 and text.startswith(state[0]) for state in states), states
    assert _dump(pane, target, tmp_path, wait_until) == ("unix", [text[:-1]])


@pytest.mark.parametrize("adopted", [False, True], ids=["dedicated", "adopted"])
def test_a_mixed_endings_edit_keeps_its_crs(
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
        target.write_bytes(MIXED)
        _keys(pane, f":tabedit {target}")
    else:
        with log.open("a") as handle:
            handle.write(_MARK + "\n")
        _hook(monkeypatch, "pre", "Write", target)
        target.write_bytes(MIXED)
        _hook(monkeypatch, "post", "Write", target)
    assert wait_until(lambda: _last_state(log, "a.py") == ["a\r", "b"], timeout=15.0)
    if not adopted:
        # The retype: a line is finished, its CR included, before the next
        # one opens (a CR typed without CTRL-V would break the line instead).
        states = _states(log, "a.py")
        assert all(state[:-1] == ["a\r", "b"][: len(state) - 1] for state in states), states

    probes, scripts = _spy(monkeypatch)
    with log.open("a") as handle:
        handle.write(_MARK + "\n")
    _hook(monkeypatch, "pre", "Edit", target)
    target.write_bytes(MIXED_AFTER)
    _hook(monkeypatch, "post", "Edit", target)

    assert wait_until(lambda: _last_state(log, "a.py") == ["a\r", "X\r", "b"], timeout=15.0)
    assert probes == ["holds"]
    assert scripts == [[EditOp(kind="insert", start_line=2, end_line=1, new_lines=("X\r",))]]
    assert _dump(pane, target, tmp_path, wait_until) == ("unix", ["a\r", "X\r", "b"])
    copy = tmp_path / "saved.py"
    for _attempt in range(3):  # a loaded machine can swallow the Escape pair
        _keys(pane, f":silent write! {copy}")
        if wait_until(lambda: copy.exists() and copy.read_bytes() == MIXED_AFTER, 5.0):
            break
    assert copy.read_bytes() == MIXED_AFTER
