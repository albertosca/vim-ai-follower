"""One canonical form for every persisted `partial`: terminated-newline.

A pending animation's `partial` is the fully-shown prefix the crash-fallback
catch-up (hooks._consume_pending_catchup) and the des-interrupt replay
(hooks._replay_remainder) rebuild the buffer from, via
`follower.rewrite_buffer(file_path, partial)` — and both backends
`.splitlines()` it. The show_fresh savers built it with `_terminated` (every
line followed by "\\n"), which survives that round trip; the apply_edit savers
built it with `diff.apply_ops`, whose `"\\n".join` does NOT: ['one','two',
'three',''] joins to "one\\ntwo\\nthree\\n", which splits back to three lines.
A fully-applied prefix ending in a genuine blank line lost that line.

apply_ops now returns the terminated form too, so the two pending kinds carry
one shape. These tests pin that shape at the diff level, at each of the four
savers that persist an apply_ops result, and in the notification that quotes
the partial back to Claude.
"""

from __future__ import annotations

import json
import os
from collections.abc import Callable
from pathlib import Path
from typing import Any, cast
from unittest.mock import MagicMock, patch

import pytest
from helpers import make_mock_tmux_run, register_fake_follower

from vim_ai_follower import control, hooks, snapshot, state
from vim_ai_follower.animate import AnimationResult, run_ops
from vim_ai_follower.backends.nvim import NvimFollower
from vim_ai_follower.diff import EditOp, apply_ops, compute_edit_script
from vim_ai_follower.session import Session
from vim_ai_follower.tmux import TmuxPane

# The scenario every test below shares: a two-hunk edit whose BOTTOM hunk (the
# one applied first, since compute_edit_script emits bottom-to-top) turns the
# last line into a blank one. After op0 the buffer genuinely holds four lines,
# the fourth empty — the state a "\n".join could not represent.
_BEFORE = "one\ntwo\nthree\nfour\n"
_AFTER = "ONE\ntwo\nthree\n\n"
_OPS = compute_edit_script(_BEFORE, _AFTER)
_PREFIX_LINES = ["one", "two", "three", ""]
_PREFIX = "one\ntwo\nthree\n\n"

_SESSION = Session(window_id="@1", origin="%1", in_tmux=True)


def test_the_scenario_really_has_two_ops_and_a_blank_ending_prefix() -> None:
    # Guard against a vacuous suite: a single-op script would make every
    # prefix trivially agree, and a prefix not ending in a blank line would
    # exercise none of this.
    assert len(_OPS) == 2
    assert _OPS[0].new_lines == ("",)
    assert _BEFORE.splitlines()[: len(_PREFIX_LINES)] != _PREFIX_LINES
    assert _PREFIX_LINES[-1] == ""


def test_apply_ops_returns_every_line_newline_terminated() -> None:
    assert apply_ops("a\nb\nc", []) == "a\nb\nc\n"
    assert apply_ops("a\nb\nc\n", []) == "a\nb\nc\n"


def test_apply_ops_keeps_a_trailing_blank_line_through_the_rebuild_round_trip() -> None:
    # The exact loss: rewrite_buffer splitlines() whatever the saver persisted,
    # so a partial that cannot round-trip is a line the buffer never gets back.
    partial = apply_ops(_BEFORE, _OPS[:1])
    assert partial == _PREFIX
    assert partial.splitlines() == _PREFIX_LINES


def test_apply_ops_of_an_empty_result_stays_empty() -> None:
    # "" is load-bearing, not cosmetic: both consumers derive `seeded` from
    # the partial's truthiness (`seeded=not pending.partial`), so an empty
    # prefix must not acquire a newline and start reading as one blank line.
    assert apply_ops("", []) == ""
    assert apply_ops("a\n", [EditOp(kind="delete", start_line=1, end_line=1, new_lines=())]) == ""


def test_apply_ops_reads_a_terminated_and_an_unterminated_base_identically() -> None:
    # Consumers feed a partial BACK in as a base (hooks._rearm_handoff,
    # animate.run_ops/nvim._run_ops with base_content/initial), so a terminated
    # base must not shift any op's line numbering.
    ops = [EditOp(kind="replace", start_line=2, end_line=2, new_lines=("Z",))]
    assert apply_ops("a\nb\nc", ops) == apply_ops("a\nb\nc\n", ops) == "a\nZ\nc\n"
    # ...and a base whose last line is genuinely blank keeps that line, which
    # is only expressible in the terminated form.
    assert apply_ops("a\nb\n\n", ops) == "a\nZ\n\n"


def test_an_apply_ops_result_chains_as_the_base_of_the_next_call() -> None:
    # The hand-off loop's shape: op0 persisted, then the remainder replayed
    # onto that partial. Going through the blank-ending intermediate must land
    # exactly where applying both ops to the original base does.
    chained = apply_ops(apply_ops(_BEFORE, _OPS[:1]), _OPS[1:])
    assert chained == apply_ops(_BEFORE, _OPS) == _AFTER


# --------------------------------------------------------------------------
# The four savers that persist an apply_ops result.
# --------------------------------------------------------------------------


def _pause_at(check: int, base_dir: Path, seen: list[object]) -> Callable[..., str | None]:
    """A check_signal double pausing at the `check`-th call, snapshotting the
    pending the wait loop just wrote, then resuming (pause is a toggle)."""
    calls = {"n": 0}

    def _check(window_id: str, bd: Path | None = None) -> str | None:
        calls["n"] += 1
        if calls["n"] == check:
            return "pause"
        if calls["n"] == check + 1:
            seen.append(control.load_pending_animation("@1", base_dir))
            return "pause"
        return None

    return _check


def test_the_tmux_driver_persists_the_blank_ending_prefix_in_terminated_form(
    tmp_path: Path,
) -> None:
    # Check 9 is op1's boundary (measured with a throwaway probe, not guessed:
    # op0 spans checks 1-8). op0 has landed, so the buffer holds four lines
    # with the fourth blank.
    pane = cast(TmuxPane, MagicMock())
    seen: list[object] = []
    with patch("vim_ai_follower.control.check_signal", side_effect=_pause_at(9, tmp_path, seen)):
        result = run_ops(
            pane, "@1", _OPS, pace_seconds=0.0, base_dir=tmp_path, base_content=_BEFORE
        )
    assert result == AnimationResult("completed", 2)
    # Proof the pause landed at the op boundary and not somewhere that would
    # make the expected partial right for the wrong reason.
    sent = [c.args[0] for c in pane.send_text.call_args_list]  # type: ignore[attr-defined]
    assert sent == [":4d", ":3", "o", "", ":1d", "gg", "O", "ONE"]
    assert seen == [control.PendingApplyEdit(_OPS[1:], 0.0, partial=_PREFIX)]


def test_the_nvim_backend_persists_the_blank_ending_prefix_in_terminated_form(
    tmp_path: Path,
) -> None:
    follower = NvimFollower(socket_path="/tmp/x.sock", window_id="@1", pace_seconds=0.0)
    nvim = MagicMock()
    nvim.api.get_current_buf.return_value.handle = 7
    nvim.funcs.bufnr.return_value = 3
    nvim.api.buf_get_lines.return_value = _BEFORE.splitlines()
    nvim.api.buf_line_count.return_value = 4
    saved: list[str | None] = []

    def _spy(
        window_id: str,
        ops: list[EditOp],
        pace: float,
        base_dir: Path | None = None,
        file_path: str = "",
        partial: str | None = None,
    ) -> None:
        saved.append(partial)

    with (
        patch("vim_ai_follower.backends.nvim.pynvim.attach", return_value=nvim),
        patch("vim_ai_follower.backends.nvim.time.sleep"),
        # op0 boundary + its one line; pause at op1's boundary; resume; op1.
        patch(
            "vim_ai_follower.control.check_signal",
            side_effect=[None, None, "pause", "pause", None, None],
        ),
        patch("vim_ai_follower.control.save_pending_apply_edit", _spy),
        patch("vim_ai_follower.cache.CACHE_DIR", tmp_path),
    ):
        assert follower.apply_edit("/tmp/f.py", _OPS) == AnimationResult("completed", 2)

    assert saved == [_PREFIX]


def _register_nvim_follower(target: Path) -> None:
    # realpath: the hook canonicalizes every file_path, so open_files must be
    # recorded in that spelling or the edit reads as "fresh" (show_fresh).
    resolved = os.path.realpath(str(target))
    state.FollowerState.set(
        "@1",
        "nvim",
        "/tmp/x.sock",
        current_file=resolved,
        open_files=(resolved,),
        shown_any=True,
    )


def _fake_follower() -> MagicMock:
    # Bare MagicMocks hang the hand-off loop forever: it compares `.outcome`,
    # which never equals anything on a mock.
    follower = MagicMock()
    follower.rewrite_buffer.return_value = AnimationResult("completed", 1)
    follower.resume.return_value = AnimationResult("completed", 1)
    return follower


def test_the_hook_interrupt_path_persists_the_blank_ending_prefix_in_terminated_form(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tmp_path / "f.txt"
    target.write_text(_AFTER)
    _register_nvim_follower(target)
    snapshot.save("@1", os.path.realpath(str(target)), _BEFORE)

    follower = _fake_follower()
    follower.apply_edit.return_value = AnimationResult("interrupted", 1)
    seen: dict[str, Any] = {}
    real_save = control.save_pending_apply_edit

    def spy(*args: Any, **kwargs: Any) -> None:
        seen.update(kwargs)
        real_save(*args, **kwargs)

    monkeypatch.setattr(control, "save_pending_apply_edit", spy)
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

    assert len(follower.apply_edit.call_args.args[1]) == 2  # the two-op script, not one
    assert seen["partial"] == _PREFIX
    # What the consumer actually gets back out of it.
    assert follower.rewrite_buffer.call_args_list[0].args[1].splitlines() == _PREFIX_LINES


def test_rearm_handoff_grows_an_apply_edit_partial_in_terminated_form(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A des-interrupt replay interrupted after its FIRST op: the grown partial
    # is apply_ops(previous partial, replayed ops) and must stay terminated,
    # because the next cycle rebuilds the buffer from it.
    target = tmp_path / "f.txt"
    target.write_text(_BEFORE)
    register_fake_follower("@1", "%2", open_files=(str(target),), shown_any=True)
    current = state.FollowerState.read("@1")
    assert current is not None

    follower = _fake_follower()
    follower.resume.return_value = AnimationResult("interrupted", 1)
    pending = control.PendingApplyEdit(_OPS, 0.0, file_path=str(target), partial=_BEFORE)
    monkeypatch.setattr(hooks, "get_follower", lambda *a, **k: follower)

    with patch("vim_ai_follower.tmux.subprocess.run", side_effect=make_mock_tmux_run()):
        grown = hooks._rearm_handoff(follower, "@1", pending, _BEFORE, 1)

    assert grown == _PREFIX
    reloaded = control.load_pending_animation("@1")
    assert reloaded is not None
    assert reloaded.partial == _PREFIX


def test_the_interrupt_notification_quotes_the_partial_without_a_doubled_blank_line(
    capsys: pytest.CaptureFixture[str],
) -> None:
    # The template already wraps the quote in blank lines ("...took over:\n\n"
    # ... "\n\nRe-read..."), so the partial's own terminator would add a second
    # one. Strip exactly the terminator — not every trailing newline — so a
    # genuine blank last line still shows.
    hooks._print_interrupt_notification("/tmp/f.txt", "one\ntwo\n")
    context = json.loads(capsys.readouterr().out)["hookSpecificOutput"]["additionalContext"]
    assert "took over:\n\none\ntwo\n\nRe-read" in context

    hooks._print_interrupt_notification("/tmp/f.txt", _PREFIX)
    context = json.loads(capsys.readouterr().out)["hookSpecificOutput"]["additionalContext"]
    assert "took over:\n\none\ntwo\nthree\n\n\nRe-read" in context
