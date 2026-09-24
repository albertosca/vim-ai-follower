"""The base-mismatch guard: an Edit's diff is typed only onto a buffer that
holds the diff's base.

`compute_edit_script(before, after)` is only meaningful on a buffer holding
`before`, the PreToolUse snapshot of the file on disk. Two measured ways the
follower's buffer does not: a tmux buffer that is listed but UNLOADED (its tab
closed without 'hidden' — the navigation then loads the finished file from
disk and the diff is typed on top of it), and an open clean buffer whose file
was rewritten outside Claude (a formatter, `sed -i`, a checkout). The hook
asks the follower whether the buffer holds `before` (Follower.buffer_holds)
and, when it does not — or the answer never comes — retypes the whole file
through show_fresh instead. These pin that decision; the backends' answers are
pinned in tests/test_tmux_buffer_holds.py and
tests/test_nvim_integration_buffer_holds.py, and the on-screen effect in
tests/test_integration_edit_base_mismatch.py.
"""

from __future__ import annotations

import os
from collections.abc import Callable
from pathlib import Path
from unittest.mock import MagicMock, call, patch

import pytest
from helpers import make_mock_tmux_run

from vim_ai_follower import control, hooks, snapshot, state
from vim_ai_follower.animate import AnimationResult

BEFORE = "a\nb\nc\n"
AFTER = "a\nB\nc\n"


def _follower(holds: object) -> MagicMock:
    follower = MagicMock()
    follower.apply_edit.return_value = AnimationResult("completed", 1)
    follower.show_fresh.return_value = AnimationResult("completed", 3)
    follower.rewrite_buffer.return_value = AnimationResult("completed", 1)
    follower.resume.return_value = AnimationResult("completed", 1)
    if isinstance(holds, BaseException):
        follower.buffer_holds.side_effect = holds
    else:
        follower.buffer_holds.return_value = holds
    return follower


def _setup(tmp_path: Path, *, stale: bool = False) -> tuple[Path, str]:
    target = tmp_path / "f.txt"
    target.write_text(AFTER)
    resolved = os.path.realpath(str(target))
    state.FollowerState.set(
        "@1",
        "nvim",
        "/tmp/x.sock",
        current_file=resolved,
        open_files=(resolved,),
        stale_files=(resolved,) if stale else (),
        shown_any=True,
    )
    snapshot.save("@1", resolved, BEFORE)
    return target, resolved


def _run_hook(target: Path, follower: MagicMock, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(hooks, "get_follower", lambda *a, **k: follower)
    monkeypatch.setattr(hooks, "status_surface_for", lambda *a, **k: MagicMock())
    monkeypatch.setattr(hooks, "show_popup", lambda *a, **k: None)
    payload: dict[str, object] = {"tool_name": "Edit", "tool_input": {"file_path": str(target)}}
    with (
        patch("vim_ai_follower.hooks.time.sleep"),
        patch("vim_ai_follower.tmux.subprocess.run", side_effect=make_mock_tmux_run()),
        patch("pynvim.attach", return_value=MagicMock()),
    ):
        assert hooks.cmd_hook_post({"TMUX_PANE": "%1"}, payload) == 0


def test_a_buffer_holding_the_base_gets_the_diff(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target, resolved = _setup(tmp_path)
    follower = _follower(True)
    _run_hook(target, follower, monkeypatch)

    # Asked about the pre-edit snapshot, not the finished file on disk.
    assert follower.buffer_holds.call_args == call(resolved, BEFORE)
    assert follower.apply_edit.call_args.args[0] == resolved
    assert follower.show_fresh.call_args_list == []


def test_a_buffer_not_holding_the_base_is_retyped_in_full(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target, resolved = _setup(tmp_path)
    follower = _follower(False)
    _run_hook(target, follower, monkeypatch)

    assert follower.apply_edit.call_args_list == []
    assert follower.show_fresh.call_args == call(resolved, AFTER, in_new_tab=True)
    fs = state.FollowerState.read("@1")
    assert fs is not None
    assert fs.current_file == resolved
    assert resolved in fs.open_files


def test_a_probe_that_raises_falls_back_to_the_retype(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Hooks never fail the tool call, and an unknown base is never diffed
    onto: the safe side of a broken probe is the full retype."""
    target, resolved = _setup(tmp_path)
    follower = _follower(OSError("socket gone"))
    _run_hook(target, follower, monkeypatch)

    assert follower.apply_edit.call_args_list == []
    assert follower.show_fresh.call_args == call(resolved, AFTER, in_new_tab=True)


def test_an_already_fresh_edit_skips_the_probe(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A stale-marked (or never-opened) file is retyped anyway; asking the
    editor would be a wasted round-trip."""
    target, _ = _setup(tmp_path, stale=True)
    follower = _follower(True)
    _run_hook(target, follower, monkeypatch)

    assert follower.buffer_holds.call_args_list == []
    assert follower.show_fresh.call_args_list != []


def test_the_probe_runs_after_the_pending_catch_up(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A killed hook's remainder is replayed first, because the replay is
    what brings the buffer to the new edit's base; probing before it would
    see the half-typed partial and throw the catch-up away."""
    target, resolved = _setup(tmp_path)
    control.save_pending_show_fresh(
        "@1", ("b", "c"), 0.01, continuation=True, file_path=resolved, partial="a\n"
    )
    order: list[str] = []

    def _step(name: str, answer: object) -> Callable[..., object]:
        def _record(*args: object, **kwargs: object) -> object:
            order.append(name)
            return answer

        return _record

    follower = _follower(True)
    follower.rewrite_buffer.side_effect = _step("rewrite", AnimationResult("completed", 1))
    follower.resume.side_effect = _step("resume", AnimationResult("completed", 2))
    follower.buffer_holds.side_effect = _step("probe", True)
    _run_hook(target, follower, monkeypatch)

    assert order == ["rewrite", "resume", "probe"]
    assert follower.apply_edit.call_args_list != []


def test_a_retype_after_a_mismatch_that_is_interrupted_persists_a_show_fresh_remainder(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The retype the guard chose rides the ordinary fresh path, interrupt
    machinery included: the pending is a show_fresh remainder of `after`,
    not an apply_edit one built on a base the buffer never held."""
    target, resolved = _setup(tmp_path)
    follower = _follower(False)
    follower.show_fresh.return_value = AnimationResult("interrupted", 1)
    saved: dict[str, object] = {}
    monkeypatch.setattr(
        control, "save_pending_show_fresh", lambda *a, **k: saved.update(args=a, kwargs=k)
    )
    monkeypatch.setattr(hooks, "_await_user_handoff", lambda *a, **k: None)
    _run_hook(target, follower, monkeypatch)

    assert saved["args"] == ("@1", ("B", "c"), saved["args"][2])  # type: ignore[index]
    assert saved["kwargs"] == {
        "continuation": True,
        "file_path": resolved,
        "partial": "a\n",
    }
