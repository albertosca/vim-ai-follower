"""The hook handing `before` to apply_edit.

The tmux driver cannot read the buffer, so the only way its pause-time crash
fallback can record an applied prefix is for the hook to pass the base down.
`before` is the pre-edit snapshot the ops were computed against — the same
string the hook's own interrupt path replays ops onto — so the two reconstructions
can never disagree. tests/test_tmux_partial.py pins what the backend then does
with it; these pin that it arrives at all, and that it is the right string.
"""

from __future__ import annotations

import os
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from helpers import make_mock_tmux_run

from vim_ai_follower import control, diff, hooks, snapshot, state
from vim_ai_follower.animate import AnimationResult


def _fake_follower() -> MagicMock:
    # The des-interrupt hand-off replays through these two; left as bare
    # MagicMocks their `.outcome` never compares equal to anything and the
    # hand-off loop spins forever.
    follower = MagicMock()
    follower.rewrite_buffer.return_value = AnimationResult("completed", 1)
    follower.resume.return_value = AnimationResult("completed", 1)
    return follower


def _register_nvim_follower(target: Path) -> None:
    # realpath: the hook canonicalizes every file_path, so open_files must be
    # recorded in that same spelling or the edit reads as "fresh".
    resolved = os.path.realpath(str(target))
    state.FollowerState.set(
        "@1",
        "nvim",
        "/tmp/x.sock",
        current_file=resolved,
        open_files=(resolved,),
        shown_any=True,
    )


def _run_hook(target: Path, follower: MagicMock, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(hooks, "get_follower", lambda *a, **k: follower)
    monkeypatch.setattr(hooks, "status_surface_for", lambda *a, **k: MagicMock())
    monkeypatch.setattr(hooks, "show_popup", lambda *a, **k: None)
    payload: dict[str, object] = {"tool_name": "Edit", "tool_input": {"file_path": str(target)}}
    with (
        patch("vim_ai_follower.control.check_signal", return_value="interrupt"),
        patch("vim_ai_follower.hooks.time.sleep"),
        patch("vim_ai_follower.tmux.subprocess.run", side_effect=make_mock_tmux_run()),
        patch("pynvim.attach", return_value=MagicMock()),
    ):
        assert hooks.cmd_hook_post({"TMUX_PANE": "%1"}, payload) == 0


def test_animate_edit_passes_the_pre_edit_snapshot_to_apply_edit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tmp_path / "f.txt"
    target.write_text("A\nb\nC\n")
    _register_nvim_follower(target)
    # Deliberately different from what is on disk: if the hook passed the
    # post-edit content (or re-read the file) instead of the snapshot, the
    # assertion below would catch it.
    snapshot.save("@1", os.path.realpath(str(target)), "a\nb\nc\n")

    follower = _fake_follower()
    follower.apply_edit.return_value = AnimationResult("completed", 2)
    _run_hook(target, follower, monkeypatch)

    assert follower.apply_edit.call_args.kwargs["before"] == "a\nb\nc\n"


def test_the_before_it_passes_is_the_base_its_own_interrupt_path_uses(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The driver computes apply_ops(before, ops[:k]) at pause time; the hook
    # computes apply_ops(before, ops[:completed_count]) at interrupt time. One
    # shared base is what makes those the same reconstruction — this pins that
    # the two really are handed the same string, rather than each going its own
    # way to something that merely looks right.
    target = tmp_path / "f.txt"
    # Non-adjacent changes, so difflib emits TWO ops rather than one block.
    target.write_text("A\nb\nC\n")
    _register_nvim_follower(target)
    snapshot.save("@1", os.path.realpath(str(target)), "a\nb\nc\n")

    follower = _fake_follower()
    follower.apply_edit.return_value = AnimationResult("interrupted", 1)
    seen: dict[str, object] = {}
    real_save = control.save_pending_apply_edit

    def spy(*args: object, **kwargs: object) -> None:
        seen.update(kwargs)
        real_save(*args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(control, "save_pending_apply_edit", spy)
    _run_hook(target, follower, monkeypatch)

    before = follower.apply_edit.call_args.kwargs["before"]
    ops = follower.apply_edit.call_args.args[1]
    assert len(ops) == 2  # not vacuous: a one-op script would make any prefix agree
    assert diff.apply_ops(before, ops[:1]) == seen["partial"]
    assert seen["partial"] == "a\nb\nC"
