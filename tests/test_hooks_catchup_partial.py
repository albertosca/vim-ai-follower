"""Hook-level contract for the crash-fallback pace-0 catch-up.

Two halves, both backend-agnostic (mocked follower): every hook-side saver
must RECORD the partial it already knows, and _consume_pending_catchup must
rebuild from it before replaying the remainder instead of trusting the live
buffer's shape. tests/test_nvim_integration_catchup_partial.py proves the
resulting buffer against real nvim; these pin the wiring."""

from __future__ import annotations

import json
import os
from pathlib import Path
from unittest.mock import MagicMock, call, patch

import pytest
from helpers import make_mock_tmux_run, register_fake_follower

from vim_ai_follower import cache, control, hooks, snapshot, state
from vim_ai_follower.animate import AnimationResult
from vim_ai_follower.diff import EditOp

_OP = EditOp(kind="replace", start_line=2, end_line=2, new_lines=("B",))


def _fake_follower() -> MagicMock:
    follower = MagicMock()
    follower.rewrite_buffer.return_value = AnimationResult("completed", 1)
    follower.resume.return_value = AnimationResult("completed", 1)
    return follower


# --- _consume_pending_catchup --------------------------------------------


def test_catchup_rebuilds_from_the_persisted_partial_before_resuming() -> None:
    follower = _fake_follower()
    pending = control.PendingShowFresh(
        lines=("b", "c"), pace_seconds=0.2, continuation=True, file_path="/tmp/f.py", partial="a\n"
    )

    hooks._consume_pending_catchup(follower, "/tmp/f.py", pending)

    follower.rewrite_buffer.assert_called_once_with("/tmp/f.py", "a\n")
    # Ordering is the whole point: replaying onto an unrepaired buffer is the
    # bug. assert_has_calls on the shared parent mock pins the sequence.
    assert follower.mock_calls[0] == call.rewrite_buffer("/tmp/f.py", "a\n")
    assert follower.mock_calls[1][0] == "resume"
    consumed = follower.resume.call_args.args[0]
    assert consumed.pace_seconds == 0.0  # the catch-up stays silent
    assert consumed.lines == ("b", "c")
    # rewrite_buffer rebuilt the buffer to exactly the partial, so there is no
    # seed blank for the replay to type in front of.
    assert follower.resume.call_args.kwargs["seeded"] is False


def test_catchup_treats_an_empty_partial_as_seeded() -> None:
    # Nothing had been typed yet. rewrite_buffer cannot leave a truly empty
    # buffer (nvim always keeps one line), so the forced blank IS a seed.
    follower = _fake_follower()
    pending = control.PendingShowFresh(
        lines=("a",), pace_seconds=0.2, continuation=False, file_path="/tmp/f.py", partial=""
    )

    hooks._consume_pending_catchup(follower, "/tmp/f.py", pending)

    follower.rewrite_buffer.assert_called_once_with("/tmp/f.py", "")
    assert follower.resume.call_args.kwargs["seeded"] is True


def test_catchup_without_a_recorded_partial_keeps_trusting_the_live_buffer() -> None:
    # Backward compatibility: an on-disk pending file written before `partial`
    # existed, or one saved by the tmux animation drivers (which never see the
    # content already on screen). No rebuild, and the live interrupted buffer
    # still carries show_fresh's trailing seed blank.
    follower = _fake_follower()
    pending = control.PendingApplyEdit(ops=[_OP], pace_seconds=0.2, file_path="/tmp/f.py")
    assert pending.partial is None

    hooks._consume_pending_catchup(follower, "/tmp/f.py", pending)

    follower.rewrite_buffer.assert_not_called()
    assert follower.resume.call_args.kwargs["seeded"] is True


def test_a_pending_file_written_without_the_partial_key_loads_as_none(tmp_path: Path) -> None:
    # The on-disk half of the same guarantee, against a literal legacy file:
    # a missing key must mean "not recorded", never a crash.
    legacy = {
        "kind": "show_fresh",
        "remaining_lines": ["b"],
        "pace_seconds": 0.2,
        "continuation": True,
        "file_path": "/tmp/f.py",
    }
    path = tmp_path / "@1.pending_animation.json"
    path.write_text(json.dumps(legacy))

    loaded = control.load_pending_animation("@1", tmp_path)

    assert loaded == control.PendingShowFresh(
        lines=("b",), pace_seconds=0.2, continuation=True, file_path="/tmp/f.py", partial=None
    )


# --- the hook-side savers record the partial ------------------------------


def _register_nvim_follower(target: Path) -> state.FollowerState:
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
    current = state.FollowerState.read("@1")
    assert current is not None
    return current


def test_show_fresh_interrupt_persists_the_typed_prefix_as_the_partial(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tmp_path / "f.txt"
    target.write_text("a\nb\nc\n")
    state.FollowerState.set("@1", "nvim", "/tmp/x.sock")

    follower = _fake_follower()
    follower.show_fresh.return_value = AnimationResult("interrupted", 1)
    monkeypatch.setattr(hooks, "get_follower", lambda *a, **k: follower)
    monkeypatch.setattr(hooks, "status_surface_for", lambda *a, **k: MagicMock())
    monkeypatch.setattr(hooks, "show_popup", lambda *a, **k: None)
    seen: dict[str, object] = {}
    real_save = control.save_pending_show_fresh

    def spy(*args: object, **kwargs: object) -> None:
        seen.update(kwargs)
        real_save(*args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(control, "save_pending_show_fresh", spy)
    payload: dict[str, object] = {"tool_name": "Write", "tool_input": {"file_path": str(target)}}
    with (
        # "interrupt" on every poll: the hand-off wait des-interrupts at once
        # and the (completed) mock replay ends it, so the test never blocks.
        patch("vim_ai_follower.control.check_signal", return_value="interrupt"),
        patch("vim_ai_follower.hooks.time.sleep"),
        # resolve_session maps TMUX_PANE -> window id through a real tmux call.
        patch("vim_ai_follower.tmux.subprocess.run", side_effect=make_mock_tmux_run()),
        # FollowerState.get liveness-checks an nvim target for real.
        patch("pynvim.attach", return_value=MagicMock()),
    ):
        assert hooks.cmd_hook_post({"TMUX_PANE": "%1"}, payload) == 0

    # completed_count == 1: only line "a" had landed.
    assert seen["partial"] == "a\n"


def test_apply_edit_interrupt_persists_the_applied_prefix_as_the_partial(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tmp_path / "f.txt"
    # Non-adjacent changes, so difflib emits TWO ops instead of merging them
    # into one block — otherwise "one op completed" would mean the whole edit.
    target.write_text("A\nb\nC\n")
    _register_nvim_follower(target)
    snapshot.save("@1", os.path.realpath(str(target)), "a\nb\nc\n")

    follower = _fake_follower()
    follower.apply_edit.return_value = AnimationResult("interrupted", 1)
    monkeypatch.setattr(hooks, "get_follower", lambda *a, **k: follower)
    monkeypatch.setattr(hooks, "status_surface_for", lambda *a, **k: MagicMock())
    monkeypatch.setattr(hooks, "show_popup", lambda *a, **k: None)
    seen: dict[str, object] = {}
    real_save = control.save_pending_apply_edit

    def spy(*args: object, **kwargs: object) -> None:
        seen.update(kwargs)
        real_save(*args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(control, "save_pending_apply_edit", spy)
    payload: dict[str, object] = {"tool_name": "Edit", "tool_input": {"file_path": str(target)}}
    with (
        # "interrupt" on every poll: the hand-off wait des-interrupts at once
        # and the (completed) mock replay ends it, so the test never blocks.
        patch("vim_ai_follower.control.check_signal", return_value="interrupt"),
        patch("vim_ai_follower.hooks.time.sleep"),
        patch("vim_ai_follower.tmux.subprocess.run", side_effect=make_mock_tmux_run()),
        patch("pynvim.attach", return_value=MagicMock()),
    ):
        assert hooks.cmd_hook_post({"TMUX_PANE": "%1"}, payload) == 0

    # The ops run bottom-to-top, so the one completed op is the LAST line.
    assert seen["partial"] == "a\nb\nC"


def test_rearm_handoff_persists_the_grown_partial(tmp_path: Path) -> None:
    # A des-interrupt replay interrupted again: the partial must grow by what
    # the replay managed to show, so a kill right after still catches up to
    # the right place.
    follower = _fake_follower()
    pending = control.PendingShowFresh(
        lines=("b", "c"),
        pace_seconds=0.2,
        continuation=True,
        file_path="/tmp/f.py",
        partial="a\n",
    )

    grown = hooks._rearm_handoff(follower, "@1", pending, "a\n", 1)

    assert grown == "a\nb\n"
    saved = control.load_pending_animation("@1")
    assert saved == control.PendingShowFresh(
        lines=("c",), pace_seconds=0.2, continuation=True, file_path="/tmp/f.py", partial="a\nb\n"
    )


def test_rearm_handoff_persists_the_grown_partial_for_ops(tmp_path: Path) -> None:
    follower = _fake_follower()
    second = EditOp(kind="replace", start_line=1, end_line=1, new_lines=("A",))
    pending = control.PendingApplyEdit(
        ops=[_OP, second], pace_seconds=0.2, file_path="/tmp/f.py", partial="a\nb\n"
    )

    grown = hooks._rearm_handoff(follower, "@1", pending, "a\nb\n", 1)

    assert grown == "a\nB"
    saved = control.load_pending_animation("@1")
    assert isinstance(saved, control.PendingApplyEdit)
    assert saved.partial == "a\nB"
    assert saved.ops == [second]


def test_resaving_an_unusable_pending_keeps_its_partial(tmp_path: Path) -> None:
    # cmd_pause puts a loaded-but-unusable pending back on disk; dropping the
    # partial there would silently re-arm the old live-buffer behavior.
    monkeypatch_dir = tmp_path / "cache"
    monkeypatch_dir.mkdir()
    from vim_ai_follower import commands

    with patch.object(cache, "CACHE_DIR", monkeypatch_dir):
        commands._resave_pending(
            "@1",
            control.PendingShowFresh(
                lines=("b",), pace_seconds=0.2, continuation=True, file_path="/f", partial="a\n"
            ),
        )
        assert control.load_pending_animation("@1") == control.PendingShowFresh(
            lines=("b",), pace_seconds=0.2, continuation=True, file_path="/f", partial="a\n"
        )
        commands._resave_pending(
            "@1",
            control.PendingApplyEdit(ops=[_OP], pace_seconds=0.2, file_path="/f", partial="a\nb\n"),
        )
        reloaded = control.load_pending_animation("@1")
        assert isinstance(reloaded, control.PendingApplyEdit)
        assert reloaded.partial == "a\nb\n"


def test_catchup_runs_on_the_hook_path_with_the_partial_it_saved(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # End-to-end through cmd_hook_post: a pending left by a killed hook is
    # rebuilt from its partial before the new edit animates.
    target = tmp_path / "f.txt"
    target.write_text("a\nB\n")
    _register_nvim_follower(target)
    snapshot.save("@1", str(target), "a\nb\n")
    control.save_pending_apply_edit(
        "@1", [_OP], 0.03, file_path=os.path.realpath(str(target)), partial="a\nb\n"
    )

    follower = _fake_follower()
    follower.apply_edit.return_value = AnimationResult("completed", 1)
    monkeypatch.setattr(hooks, "get_follower", lambda *a, **k: follower)
    monkeypatch.setattr(hooks, "status_surface_for", lambda *a, **k: MagicMock())
    payload: dict[str, object] = {"tool_name": "Edit", "tool_input": {"file_path": str(target)}}
    with (
        patch("vim_ai_follower.control.check_signal", return_value=None),
        patch("pynvim.attach", return_value=MagicMock()),
        patch("vim_ai_follower.tmux.subprocess.run", side_effect=make_mock_tmux_run()),
    ):
        assert hooks.cmd_hook_post({"TMUX_PANE": "%1"}, payload) == 0

    follower.rewrite_buffer.assert_called_once_with(os.path.realpath(str(target)), "a\nb\n")
    names = [c[0] for c in follower.mock_calls]
    assert names.index("rewrite_buffer") < names.index("resume") < names.index("apply_edit")


def test_catchup_rebuild_is_backend_agnostic_for_tmux(tmp_path: Path) -> None:
    # The catch-up lives in the hook, so the tmux backend gets the rebuild
    # too. Its own animation drivers never record a partial, but a remainder
    # saved by the HOOK does — and rewrite_buffer is the same primitive the
    # des-interrupt replay already drives on this backend. Proof it stays
    # coherent: the buffer is wiped (":%d") and the partial retyped before the
    # remainder, so nothing the dead hook left behind survives.
    resolved = os.path.realpath(str(tmp_path / "f.txt"))
    (tmp_path / "f.txt").write_text("a\nb\nNEW\n")
    register_fake_follower(
        "@1", "%2", current_file=resolved, open_files=(resolved,), shown_any=True
    )
    snapshot.save("@1", resolved, "a\nb\nold\n")
    control.save_pending_show_fresh(
        "@1", ("leftover",), 0.03, continuation=True, file_path=resolved, partial="PARTIAL\n"
    )

    payload: dict[str, object] = {"tool_name": "Edit", "tool_input": {"file_path": resolved}}
    with patch("vim_ai_follower.tmux.subprocess.run", side_effect=make_mock_tmux_run()) as run_mock:
        assert hooks.cmd_hook_post({"TMUX_PANE": "%1"}, payload) == 0

    sends = [
        c.args[0][6]
        for c in run_mock.call_args_list
        if c.args[0][:4] == ["tmux", "send-keys", "-t", "%2"] and "-l" in c.args[0]
    ]
    assert sends.index(":%d") < sends.index("PARTIAL") < sends.index("leftover")
    assert sends.index("leftover") < sends.index("NEW")  # ...all before the new edit
