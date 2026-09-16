"""More than ONE interrupt/des-interrupt cycle per animation.

Live finding (2026-09-16): a third S — landing mid-replay — used to hand the
buffer over and RETURN, so nobody listened for S any more. A fourth press did
nothing and the buffer stayed partial and modifiable until the next completed
animation's relock healed it. `_await_user_handoff` now loops: every
interrupted replay re-arms the wait with a shortened remainder and a grown
partial.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, call, patch

import pytest
from helpers import make_mock_tmux_run as _mock_tmux_run
from helpers import register_fake_follower as _register_fake_follower

from vim_ai_follower import control, hooks, state
from vim_ai_follower.animate import AnimationResult
from vim_ai_follower.diff import EditOp
from vim_ai_follower.session import Session

_OPS = [
    EditOp(kind="replace", start_line=2, end_line=2, new_lines=("B",)),
    EditOp(kind="replace", start_line=3, end_line=3, new_lines=("C",)),
    EditOp(kind="replace", start_line=4, end_line=4, new_lines=("D",)),
]


def _scripted(signals: list[str | None]) -> Callable[..., str | None]:
    """check_signal double: walks `signals`, then None forever — with a hard
    ceiling so a hand-off that never releases fails loudly instead of hanging."""
    calls = {"n": 0}

    def _check(window_id: str, base_dir: Path | None = None) -> str | None:
        calls["n"] += 1
        if calls["n"] > len(signals) + 200:
            raise AssertionError("hand-off wait never released within the poll budget")
        return signals[calls["n"] - 1] if calls["n"] <= len(signals) else None

    return _check


def _spy_saves(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, Any, float, Any]]:
    """Record every pending remainder the hand-off persists, while still
    writing it to disk — the next cycle loads it back."""
    saved: list[tuple[str, Any, float, Any]] = []
    real_fresh = control.save_pending_show_fresh
    real_ops = control.save_pending_apply_edit

    def _fresh(
        window_id: str,
        lines: tuple[str, ...],
        pace_seconds: float,
        continuation: bool = False,
        base_dir: Path | None = None,
        file_path: str = "",
        partial: str | None = None,
    ) -> None:
        saved.append(("show_fresh", lines, pace_seconds, continuation))
        real_fresh(window_id, lines, pace_seconds, continuation, base_dir, file_path, partial)

    def _ops(
        window_id: str,
        ops: list[EditOp],
        pace_seconds: float,
        base_dir: Path | None = None,
        file_path: str = "",
        partial: str | None = None,
    ) -> None:
        saved.append(("apply_edit", list(ops), pace_seconds, file_path))
        real_ops(window_id, ops, pace_seconds, base_dir, file_path, partial)

    monkeypatch.setattr(control, "save_pending_show_fresh", _fresh)
    monkeypatch.setattr(control, "save_pending_apply_edit", _ops)
    return saved


def _install_fake_follower(monkeypatch: pytest.MonkeyPatch, fake: MagicMock) -> None:
    monkeypatch.setattr(hooks, "get_follower", lambda *a, **k: fake)
    monkeypatch.setattr(hooks, "status_surface_for", lambda *a, **k: MagicMock())
    monkeypatch.setattr(hooks, "show_popup", lambda *a, **k: None)


def _handoff_state(tmp_path: Path, content: str) -> tuple[state.FollowerState, str]:
    target = tmp_path / "f.txt"
    target.write_text(content)
    _register_fake_follower("@1", "%2", open_files=(str(target),), shown_any=True)
    current = state.FollowerState.read("@1")
    assert current is not None
    return current, str(target)


_SESSION = Session(window_id="@1", origin="%1", in_tmux=True)


def test_show_fresh_replay_interrupted_midway_re_enters_the_handoff(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    after = "l0\nl1\nl2\nl3\n"
    current, target = _handoff_state(tmp_path, after)
    # The live interrupt already happened: l0 is on screen, l1..l3 pending.
    control.save_pending_show_fresh(
        "@1", ("l1", "l2", "l3"), 0.03, continuation=True, file_path=target
    )
    saved = _spy_saves(monkeypatch)

    fake = MagicMock()
    fake.rewrite_buffer.return_value = AnimationResult("completed", 1)
    # first replay types l1 and l2 then the user hits S again; the second one
    # finishes the single remaining line.
    fake.resume.side_effect = [
        AnimationResult("interrupted", 2),
        AnimationResult("completed", 1),
    ]
    _install_fake_follower(monkeypatch, fake)

    with (
        # FollowerState.get gates update_current_file on the pane being alive
        patch("vim_ai_follower.tmux.subprocess.run", side_effect=_mock_tmux_run()),
        patch("vim_ai_follower.control.check_signal", _scripted(["interrupt", "interrupt"])),
        patch("vim_ai_follower.hooks.time.sleep"),
    ):
        hooks._await_user_handoff(current, _SESSION, target, after, "l0\n")

    first, second = fake.resume.call_args_list
    assert first.args[0].lines == ("l1", "l2", "l3")
    assert second.args[0].lines == ("l3",)  # shortened by the two lines replayed
    assert second.kwargs["seeded"] is False
    # the buffer is rebuilt to the ACCUMULATED partial before the second replay
    assert fake.rewrite_buffer.call_args_list == [
        call(target, "l0\n"),
        call(target, "l0\nl1\nl2\n"),
    ]
    assert saved == [("show_fresh", ("l3",), 0.03, True)]
    assert fake.hand_over.call_count == 1
    refreshed = state.FollowerState.read("@1")
    assert refreshed is not None
    assert refreshed.current_file == target  # following resumed once it finished
    assert capsys.readouterr().out == ""


def test_apply_edit_replay_interrupted_midway_re_enters_the_handoff(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    after = "a\nB\nC\nD\n"
    current, target = _handoff_state(tmp_path, after)
    control.save_pending_apply_edit("@1", _OPS, 0.03, file_path=target)
    saved = _spy_saves(monkeypatch)

    fake = MagicMock()
    fake.rewrite_buffer.return_value = AnimationResult("completed", 4)
    fake.resume.side_effect = [
        AnimationResult("interrupted", 1),  # one op landed, then S again
        AnimationResult("completed", 2),
    ]
    _install_fake_follower(monkeypatch, fake)

    with (
        # FollowerState.get gates update_current_file on the pane being alive
        patch("vim_ai_follower.tmux.subprocess.run", side_effect=_mock_tmux_run()),
        patch("vim_ai_follower.control.check_signal", _scripted(["interrupt", "interrupt"])),
        patch("vim_ai_follower.hooks.time.sleep"),
    ):
        hooks._await_user_handoff(current, _SESSION, target, after, "a\nb\nc\nd")

    first, second = fake.resume.call_args_list
    assert first.args[0].ops == _OPS
    assert second.args[0].ops == _OPS[1:]  # shortened by the one op replayed
    # the rebuilt partial is the previous one with that op applied
    assert fake.rewrite_buffer.call_args_list == [
        call(target, "a\nb\nc\nd"),
        call(target, "a\nB\nc\nd"),
    ]
    assert saved == [("apply_edit", _OPS[1:], 0.03, target)]
    assert fake.hand_over.call_count == 1
    refreshed = state.FollowerState.read("@1")
    assert refreshed is not None
    assert refreshed.current_file == target
    assert capsys.readouterr().out == ""


def test_a_save_after_the_second_interrupt_reports_the_accumulated_partial(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    # The release path still works from a LATER cycle, and the notification
    # must quote everything shown so far — not just the first cycle's partial.
    after = "l0\nl1\nl2\nl3\n"
    current, target = _handoff_state(tmp_path, after)
    control.save_pending_show_fresh(
        "@1", ("l1", "l2", "l3"), 0.03, continuation=True, file_path=target
    )
    _spy_saves(monkeypatch)

    fake = MagicMock()
    fake.rewrite_buffer.return_value = AnimationResult("completed", 1)
    fake.resume.return_value = AnimationResult("interrupted", 2)
    _install_fake_follower(monkeypatch, fake)

    def _check(window_id: str, base_dir: Path | None = None) -> str | None:
        calls["n"] += 1
        if calls["n"] == 1:
            return "interrupt"
        Path(target).write_text("the user's own version\n")
        return None

    calls = {"n": 0}
    with (
        patch("vim_ai_follower.tmux.subprocess.run", side_effect=_mock_tmux_run()),
        patch("vim_ai_follower.control.check_signal", _check),
        patch("vim_ai_follower.hooks.time.sleep"),
    ):
        hooks._await_user_handoff(current, _SESSION, target, after, "l0\n")

    out = json.loads(capsys.readouterr().out)
    context = out["hookSpecificOutput"]["additionalContext"]
    assert "l0\nl1\nl2\n" in context  # the grown partial, not just "l0"
    assert "SAVED their own version" in context
    assert not control.has_pending_animation("@1")  # the remainder is dropped on release


def test_a_mid_replay_interrupt_with_nothing_typed_keeps_the_remainder_unchanged(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # completed_count == 0: the user hit S before the replay typed anything.
    # The remainder must come back byte-identical (and the empty partial keeps
    # the seed provenance, so the next replay still passes seeded=True).
    after = "l0\nl1\n"
    current, target = _handoff_state(tmp_path, after)
    control.save_pending_show_fresh("@1", ("l0", "l1"), 0.03, continuation=False, file_path=target)
    saved = _spy_saves(monkeypatch)

    fake = MagicMock()
    fake.rewrite_buffer.return_value = AnimationResult("completed", 0)
    fake.resume.side_effect = [
        AnimationResult("interrupted", 0),
        AnimationResult("completed", 2),
    ]
    _install_fake_follower(monkeypatch, fake)

    with (
        # FollowerState.get gates update_current_file on the pane being alive
        patch("vim_ai_follower.tmux.subprocess.run", side_effect=_mock_tmux_run()),
        patch("vim_ai_follower.control.check_signal", _scripted(["interrupt", "interrupt"])),
        patch("vim_ai_follower.hooks.time.sleep"),
    ):
        hooks._await_user_handoff(current, _SESSION, target, after, "")

    first, second = fake.resume.call_args_list
    assert second.args[0].lines == first.args[0].lines == ("l0", "l1")
    assert second.kwargs["seeded"] is True  # still nothing on screen: seed stands
    assert saved == [("show_fresh", ("l0", "l1"), 0.03, False)]
    assert fake.rewrite_buffer.call_args_list == [call(target, ""), call(target, "")]


def test_an_interrupted_rebuild_re_arms_with_the_untouched_remainder(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # rewrite_buffer animates on the tmux backend (run_lines at pace 0), so it
    # can be interrupted too. None of the remainder ran, so it must be re-armed
    # whole — and resume must not have been reached at all for that cycle.
    after = "l0\nl1\nl2\n"
    current, target = _handoff_state(tmp_path, after)
    control.save_pending_show_fresh("@1", ("l1", "l2"), 0.03, continuation=True, file_path=target)
    saved = _spy_saves(monkeypatch)

    fake = MagicMock()
    fake.rewrite_buffer.side_effect = [
        AnimationResult("interrupted", 0),
        AnimationResult("completed", 1),
    ]
    fake.resume.return_value = AnimationResult("completed", 2)
    _install_fake_follower(monkeypatch, fake)

    with (
        # FollowerState.get gates update_current_file on the pane being alive
        patch("vim_ai_follower.tmux.subprocess.run", side_effect=_mock_tmux_run()),
        patch("vim_ai_follower.control.check_signal", _scripted(["interrupt", "interrupt"])),
        patch("vim_ai_follower.hooks.time.sleep"),
    ):
        hooks._await_user_handoff(current, _SESSION, target, after, "l0\n")

    assert saved == [("show_fresh", ("l1", "l2"), 0.03, True)]  # untouched
    fake.resume.assert_called_once()
    assert fake.resume.call_args.args[0].lines == ("l1", "l2")
    assert fake.hand_over.call_count == 1


@pytest.mark.integration
def test_two_replay_cycles_land_at_the_exact_content_in_a_real_nvim(
    headless_nvim: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # End-to-end through the real hook orchestration and a real Neovim: type,
    # interrupt, des-interrupt, interrupt the replay, des-interrupt again. The
    # buffer must end up holding EXACTLY the file's lines — no dropped line, no
    # leftover seed blank, no duplicated partial.
    pynvim = pytest.importorskip("pynvim")
    from vim_ai_follower.backends.nvim import NvimFollower

    target = tmp_path / "a.py"
    content = "l0\nl1\nl2\nl3\n"
    target.write_text(content)
    file_a = str(target)
    # speed=instant pins the pace to 0.0, so check_signal is called exactly
    # once per line and the script below is deterministic.
    state.FollowerState.set(
        "@1", "nvim", headless_nvim, open_files=(file_a,), shown_any=True, speed="instant"
    )
    current = state.FollowerState.read("@1")
    assert current is not None

    follower = NvimFollower(socket_path=headless_nvim, window_id="@1", pace_seconds=0.0)
    script: list[str | None] = [
        None,  # l0 types
        None,  # l1 types
        "interrupt",  # stop before l2
        "interrupt",  # hand-off poll: des-interrupt
        None,  # replay types l2
        "interrupt",  # stop mid-replay -> must RE-ARM, not return
        "interrupt",  # second hand-off poll: des-interrupt again
        None,  # replay types l3 -> completed
    ]
    monkeypatch.setattr(control, "check_signal", _scripted(script))
    monkeypatch.setattr(hooks, "status_surface_for", lambda *a, **k: MagicMock())

    assert follower.show_fresh(file_a, content) == AnimationResult("interrupted", 2)
    partial = hooks._reconstruct_partial_fresh(content, 2)
    control.save_pending_show_fresh("@1", ("l2", "l3"), 0.0, continuation=True, file_path=file_a)

    # Every poll here answers "interrupt" immediately, so no idle sleep is
    # expected — patching it turns a hypothetical hang into a fast failure.
    with patch("vim_ai_follower.hooks.time.sleep"):
        hooks._await_user_handoff(
            current, Session(window_id="@1", origin=None, in_tmux=False), file_a, content, partial
        )

    nvim = pynvim.attach("socket", path=headless_nvim)
    assert nvim.current.buffer[:] == ["l0", "l1", "l2", "l3"]
    refreshed = state.FollowerState.read("@1")
    assert refreshed is not None
    assert refreshed.current_file == file_a
