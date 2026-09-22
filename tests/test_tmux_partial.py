"""The tmux backend supplying its drivers the base content they need to record
a crash-fallback `partial`.

The drivers themselves are covered in test_animate_partial.py; what is pinned
here is which base each entry point hands over, since getting that wrong would
persist a partial that looks plausible and rebuilds the buffer to the wrong
content. Two of these go end-to-end (a real pause writes a real pending file);
the rest read the base_content off a patched driver, which is the whole of the
contribution those entry points make.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, cast
from unittest.mock import MagicMock, patch

from vim_ai_follower import control
from vim_ai_follower.animate import AnimationResult
from vim_ai_follower.backends.tmux_vim import TmuxVimFollower
from vim_ai_follower.diff import EditOp

_OP = EditOp(kind="replace", start_line=2, end_line=2, new_lines=("XX",))


def _pause_once(tmp_path: Path, at: int, seen: list[object]) -> Any:
    """check_signal double: pause at the `at`-th call, snapshot what the wait
    loop persisted on the next call, then resume."""
    calls = {"n": 0}

    def _check(window_id: str, base_dir: Path | None = None) -> str | None:
        calls["n"] += 1
        if calls["n"] == at:
            return "pause"
        if calls["n"] == at + 1:
            seen.append(control.load_pending_animation("@1", tmp_path))
            return "pause"
        return None

    return _check


def test_show_fresh_pause_persists_only_the_lines_already_retyped(tmp_path: Path) -> None:
    # End-to-end: show_fresh wipes the buffer with `:%d` before typing, so
    # nothing of the content is on screen and the partial is exactly what this
    # run has typed. Pause at check 5 = line 1's first keystroke.
    follower = TmuxVimFollower(pane_id="%2", window_id="@1")
    seen: list[object] = []
    with (
        patch("vim_ai_follower.tmux.subprocess.run"),
        patch("vim_ai_follower.cache.CACHE_DIR", tmp_path),
        patch("vim_ai_follower.control.check_signal", side_effect=_pause_once(tmp_path, 5, seen)),
    ):
        result = follower.show_fresh("/tmp/f.txt", "one\ntwo\nthree\n")
    assert result.outcome == "completed"
    assert seen == [
        control.PendingShowFresh(
            ("two", "three"),
            0.05,
            continuation=True,
            file_path="/tmp/f.txt",
            partial="one\n",
        )
    ]


def test_apply_edit_pause_persists_the_ops_already_applied_to_before(tmp_path: Path) -> None:
    # End-to-end: the caller's `before` is the base, so the partial is the
    # pre-edit content with op0 applied. Check 11 lands inside op1 (after its
    # `gg`/`O` opener), which the rollback undoes — op-granular, so the
    # partly-typed op contributes nothing either way.
    follower = TmuxVimFollower(pane_id="%2", window_id="@1")
    ops = [_OP, EditOp(kind="insert", start_line=1, end_line=0, new_lines=("ZZ",))]
    seen: list[object] = []
    with (
        patch("vim_ai_follower.tmux.subprocess.run"),
        patch("vim_ai_follower.cache.CACHE_DIR", tmp_path),
        patch("vim_ai_follower.control.check_signal", side_effect=_pause_once(tmp_path, 11, seen)),
    ):
        result = follower.apply_edit("/tmp/f.txt", ops, before="a\nb\nc\n")
    assert result.outcome == "completed"
    assert seen == [
        control.PendingApplyEdit(ops[1:], 0.05, file_path="/tmp/f.txt", partial="a\nXX\nc\n")
    ]


def test_apply_edit_without_a_before_still_records_no_partial(tmp_path: Path) -> None:
    # The optional argument must stay honest: no snapshot, no partial, and the
    # consumer's live-buffer fallback survives.
    follower = TmuxVimFollower(pane_id="%2", window_id="@1")
    ops = [_OP, EditOp(kind="insert", start_line=1, end_line=0, new_lines=("ZZ",))]
    seen: list[object] = []
    with (
        patch("vim_ai_follower.tmux.subprocess.run"),
        patch("vim_ai_follower.cache.CACHE_DIR", tmp_path),
        patch("vim_ai_follower.control.check_signal", side_effect=_pause_once(tmp_path, 11, seen)),
    ):
        follower.apply_edit("/tmp/f.txt", ops)
    assert seen == [control.PendingApplyEdit(ops[1:], 0.05, file_path="/tmp/f.txt", partial=None)]


def test_rewrite_buffer_rebuilds_from_nothing() -> None:
    # rewrite_buffer sends its own `:%d` first, so a pause inside it must not
    # claim any pre-existing content as already shown.
    follower = TmuxVimFollower(pane_id="%2", window_id="@1")
    with (
        patch("vim_ai_follower.tmux.subprocess.run"),
        patch("vim_ai_follower.backends.tmux_vim.run_lines") as run_lines,
    ):
        run_lines.return_value = AnimationResult("completed", 2)
        follower.rewrite_buffer("/tmp/f.txt", "one\ntwo\n")
    assert run_lines.call_args.kwargs["base_content"] == ""


def test_resume_show_fresh_continues_from_the_pendings_own_partial() -> None:
    # Every caller that resumes a pending with a partial rebuilt the buffer to
    # exactly that partial first (hooks' _consume_pending_catchup and
    # _replay_remainder both call rewrite_buffer), so it IS what is on screen.
    follower = TmuxVimFollower(pane_id="%2", window_id="@1")
    pending = control.PendingShowFresh(
        lines=("two", "three"), pace_seconds=0.0, continuation=True, partial="one\n"
    )
    with (
        patch("vim_ai_follower.tmux.subprocess.run"),
        patch("vim_ai_follower.backends.tmux_vim.run_lines") as run_lines,
    ):
        run_lines.return_value = AnimationResult("completed", 2)
        follower.resume(pending)
    assert run_lines.call_args.kwargs["base_content"] == "one\n"


def test_resume_apply_edit_continues_from_the_pendings_own_partial() -> None:
    follower = TmuxVimFollower(pane_id="%2", window_id="@1")
    pending = control.PendingApplyEdit(ops=[_OP], pace_seconds=0.0, partial="a\nb\nc\n")
    with (
        patch("vim_ai_follower.tmux.subprocess.run"),
        patch("vim_ai_follower.backends.tmux_vim.run_ops") as run_ops,
    ):
        run_ops.return_value = AnimationResult("completed", 1)
        follower.resume(pending)
    assert run_ops.call_args.kwargs["base_content"] == "a\nb\nc\n"


def test_resume_of_a_legacy_pending_keeps_the_base_unknown() -> None:
    # A pending written before the field existed has no partial, so nothing
    # rebuilt the buffer and its shape is genuinely unknown. Re-pausing such a
    # run must keep saying "not recorded" rather than inventing a base.
    follower = TmuxVimFollower(pane_id="%2", window_id="@1")
    pending = control.PendingShowFresh(lines=("two",), pace_seconds=0.0, continuation=True)
    with (
        patch("vim_ai_follower.tmux.subprocess.run"),
        patch("vim_ai_follower.backends.tmux_vim.run_lines") as run_lines,
    ):
        run_lines.return_value = AnimationResult("completed", 1)
        follower.resume(pending)
    assert run_lines.call_args.kwargs["base_content"] is None


def test_nvim_backend_accepts_and_ignores_the_before_argument() -> None:
    # Protocol parity: hooks passes `before` to whatever backend is active.
    # The nvim backend reads its own buffer at run start, so the argument must
    # be accepted and make no difference.
    from vim_ai_follower.backends.nvim import NvimFollower

    follower = NvimFollower(socket_path="/tmp/s", window_id="@1")
    nvim = MagicMock()
    nvim.exec_lua.return_value = -1  # the disk-load short circuit
    with (
        patch.object(NvimFollower, "_connect", return_value=cast(Any, nvim)),
        patch.object(NvimFollower, "_open_from_disk") as open_from_disk,
    ):
        assert follower.apply_edit("/tmp/f.py", [_OP], before="a\nb\nc\n") == AnimationResult(
            "completed", 1
        )
    open_from_disk.assert_called_once()
