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
    # These tests are about the diff path: the buffer is the edit's base.
    follower.probe_buffer.return_value = "holds"
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
    # The catch-up must end at the NEW edit's base, not re-read the file:
    # disk already holds the new edit (tmux's relock `:e!` loaded it).
    assert follower.resume.call_args.kwargs["reload"] is False


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


_DONE = AnimationResult("completed", 0)
_CUT = AnimationResult("interrupted", 0)


@pytest.mark.parametrize(
    ("rebuilt", "resumed", "partial", "expected"),
    [
        (_DONE, _DONE, "a\n", hooks._CatchUp("completed")),
        (_DONE, _CUT, "a\n", hooks._CatchUp("interrupted", "a\n")),
        (None, _DONE, None, hooks._CatchUp("completed")),
        # A legacy remainder recorded no partial: what the buffer holds is
        # not known, and the notification must not quote a guess.
        (None, _CUT, None, hooks._CatchUp("interrupted", None)),
    ],
)
def test_catchup_reports_whether_it_ran_to_the_end(
    rebuilt: AnimationResult | None,
    resumed: AnimationResult,
    partial: str | None,
    expected: hooks._CatchUp,
) -> None:
    """ "completed" only when the buffer now holds nothing but follower text,
    which is what lets _animate_edit reload it from disk after an outside
    change; "interrupted" carries what the buffer holds for the hand-off."""
    follower = _fake_follower()
    if rebuilt is not None:
        follower.rewrite_buffer.return_value = rebuilt
    follower.resume.return_value = resumed
    pending = control.PendingApplyEdit(
        ops=[_OP], pace_seconds=0.2, file_path="/tmp/f.py", partial=partial
    )

    assert hooks._consume_pending_catchup(follower, "/tmp/f.py", pending) == expected


def test_an_interrupted_apply_edit_catch_up_counts_the_ops_it_applied() -> None:
    follower = _fake_follower()
    second = EditOp(kind="replace", start_line=1, end_line=1, new_lines=("A",))
    follower.resume.return_value = AnimationResult("interrupted", 1)
    pending = control.PendingApplyEdit(
        ops=[_OP, second], pace_seconds=0.2, file_path="/tmp/f.py", partial="a\nb\n"
    )

    assert hooks._consume_pending_catchup(follower, "/tmp/f.py", pending) == hooks._CatchUp(
        "interrupted", "a\nB\n"
    )


# --- _catch_up_end ---------------------------------------------------------


def test_the_end_of_an_apply_edit_catch_up_is_its_ops_applied_to_the_partial() -> None:
    pending = control.PendingApplyEdit(
        ops=[_OP], pace_seconds=0.0, file_path="/tmp/f.py", partial="a\nb\nc\n"
    )
    assert hooks._catch_up_end(pending) == "a\nB\nc\n"


def test_the_end_of_a_show_fresh_catch_up_is_the_partial_plus_its_lines() -> None:
    """Terminated, like every partial: a trailing blank line survives."""
    pending = control.PendingShowFresh(
        lines=("y", ""), pace_seconds=0.0, continuation=True, file_path="/tmp/f.py", partial="x\n"
    )
    assert hooks._catch_up_end(pending) == "x\ny\n\n"


@pytest.mark.parametrize(
    "pending",
    [
        control.PendingApplyEdit(ops=[_OP], pace_seconds=0.0, file_path="/tmp/f.py"),
        control.PendingShowFresh(lines=("y",), pace_seconds=0.0, file_path="/tmp/f.py"),
    ],
    ids=["apply-edit", "show-fresh"],
)
def test_a_catch_up_without_a_recorded_partial_has_no_known_end(
    pending: control.PendingApplyEdit | control.PendingShowFresh,
) -> None:
    assert hooks._catch_up_end(pending) is None


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
    monkeypatch.setattr(hooks, "get_follower", lambda *arguments, **keyword_arguments: follower)
    monkeypatch.setattr(
        hooks, "status_surface_for", lambda *arguments, **keyword_arguments: MagicMock()
    )
    monkeypatch.setattr(hooks, "show_popup", lambda *arguments, **keyword_arguments: None)
    seen: dict[str, object] = {}
    real_save = control.save_pending_show_fresh

    def spy(*arguments: object, **keyword_arguments: object) -> None:
        seen.update(keyword_arguments)
        real_save(*arguments, **keyword_arguments)  # type: ignore[arg-type]

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
        patch("vim_ai_follower.backends.nvim_connect.open_session", return_value=MagicMock()),
        patch("vim_ai_follower.backends.nvim.probe_nvim", return_value="answers"),
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
    monkeypatch.setattr(hooks, "get_follower", lambda *arguments, **keyword_arguments: follower)
    monkeypatch.setattr(
        hooks, "status_surface_for", lambda *arguments, **keyword_arguments: MagicMock()
    )
    monkeypatch.setattr(hooks, "show_popup", lambda *arguments, **keyword_arguments: None)
    seen: dict[str, object] = {}
    real_save = control.save_pending_apply_edit

    def spy(*arguments: object, **keyword_arguments: object) -> None:
        seen.update(keyword_arguments)
        real_save(*arguments, **keyword_arguments)  # type: ignore[arg-type]

    monkeypatch.setattr(control, "save_pending_apply_edit", spy)
    payload: dict[str, object] = {"tool_name": "Edit", "tool_input": {"file_path": str(target)}}
    with (
        # "interrupt" on every poll: the hand-off wait des-interrupts at once
        # and the (completed) mock replay ends it, so the test never blocks.
        patch("vim_ai_follower.control.check_signal", return_value="interrupt"),
        patch("vim_ai_follower.hooks.time.sleep"),
        patch("vim_ai_follower.tmux.subprocess.run", side_effect=make_mock_tmux_run()),
        patch("vim_ai_follower.backends.nvim_connect.open_session", return_value=MagicMock()),
        patch("vim_ai_follower.backends.nvim.probe_nvim", return_value="answers"),
    ):
        assert hooks.cmd_hook_post({"TMUX_PANE": "%1"}, payload) == 0

    # The ops run bottom-to-top, so the one completed op is the LAST line.
    assert seen["partial"] == "a\nb\nC\n"


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

    assert grown == "a\nB\n"
    saved = control.load_pending_animation("@1")
    assert isinstance(saved, control.PendingApplyEdit)
    assert saved.partial == "a\nB\n"
    assert saved.ops == [second]


def test_rearm_handoff_reads_handoff_only_after_hand_over_and_keeps_an_s_sent_meanwhile(
    tmp_path: Path,
) -> None:
    """T20 re-review m5: a user who takes "handoff" as "the buffer is mine"
    types at once, and their keys landed inside hand_over's typed lines. So
    the marker still reads the replay's "running" while hand_over sends, and
    an S pressed then is addressed to this process all the same: the hand-off
    wait consumes it as the next des-interrupt."""
    control.mark_animating("@1", state="running")
    follower = _fake_follower()
    during: dict[str, object] = {}

    def hand_over() -> None:
        during["state"] = control.animating_state("@1")
        during["signal written"] = control.request_interrupt("@1")

    follower.hand_over.side_effect = hand_over
    pending = control.PendingShowFresh(
        lines=("b", "c"), pace_seconds=0.2, continuation=True, file_path="/tmp/f.py", partial="a\n"
    )

    hooks._rearm_handoff(follower, "@1", pending, "a\n", 1)

    assert during == {"state": "running", "signal written": True}
    assert control.animating_state("@1") == "handoff"
    assert control.check_signal("@1") == "interrupt"


def test_resaving_an_unusable_pending_keeps_its_partial(tmp_path: Path) -> None:
    # cmd_pause puts a loaded-but-unusable pending back on disk; dropping the
    # partial there would silently re-arm the old live-buffer behavior.
    cache_directory = tmp_path / "cache"
    cache_directory.mkdir()
    from vim_ai_follower import commands

    with patch.object(cache, "CACHE_DIRECTORY", cache_directory):
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
    monkeypatch.setattr(hooks, "get_follower", lambda *arguments, **keyword_arguments: follower)
    monkeypatch.setattr(
        hooks, "status_surface_for", lambda *arguments, **keyword_arguments: MagicMock()
    )
    payload: dict[str, object] = {"tool_name": "Edit", "tool_input": {"file_path": str(target)}}
    with (
        patch("vim_ai_follower.control.check_signal", return_value=None),
        patch("vim_ai_follower.backends.nvim_connect.open_session", return_value=MagicMock()),
        patch("vim_ai_follower.backends.nvim.probe_nvim", return_value="answers"),
        patch("vim_ai_follower.tmux.subprocess.run", side_effect=make_mock_tmux_run()),
    ):
        assert hooks.cmd_hook_post({"TMUX_PANE": "%1"}, payload) == 0

    follower.rewrite_buffer.assert_called_once_with(os.path.realpath(str(target)), "a\nb\n")
    names = [recorded_call[0] for recorded_call in follower.mock_calls]
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
        recorded_call.args[0][6]
        for recorded_call in run_mock.call_args_list
        if recorded_call.args[0][:4] == ["tmux", "send-keys", "-t", "%2"]
        and "-l" in recorded_call.args[0]
    ]
    assert sends.index(":%d") < sends.index("PARTIAL") < sends.index("leftover")
    assert sends.index("leftover") < sends.index("NEW")  # ...all before the new edit


# --- an S pressed DURING the catch-up (final review M5) ---------------------


def _orphaned_remainder_and_edit(
    tmp_path: Path, adopted: bool = False, partial: str | None = "a\n"
) -> tuple[str, dict[str, object]]:
    """A file the follower already shows, a crash-orphaned show_fresh
    remainder for it (partial "a\\n", three lines left), and the NEW edit
    already on disk with its pre-hook snapshot: the catch-up runs first."""
    target = tmp_path / "f.txt"
    target.write_text("a\nx\ny\nz\nNEW\n")
    resolved = os.path.realpath(str(target))
    state.FollowerState.set(
        "@1",
        "nvim",
        "/tmp/x.sock",
        current_file=resolved,
        open_files=(resolved,),
        shown_any=True,
        adopted=adopted,
    )
    snapshot.save("@1", resolved, "a\nx\ny\nz\n")
    control.save_pending_show_fresh(
        "@1", ("x", "y", "z"), 0.03, continuation=True, file_path=resolved, partial=partial
    )
    payload: dict[str, object] = {"tool_name": "Edit", "tool_input": {"file_path": resolved}}
    return resolved, payload


def _run_post_hook(payload: dict[str, object], signal: str | None) -> None:
    with (
        patch("vim_ai_follower.control.check_signal", return_value=signal),
        patch("vim_ai_follower.hooks.time.sleep"),
        patch("vim_ai_follower.backends.nvim_connect.open_session", return_value=MagicMock()),
        patch("vim_ai_follower.backends.nvim.probe_nvim", return_value="answers"),
        patch("vim_ai_follower.tmux.subprocess.run", side_effect=make_mock_tmux_run()),
    ):
        assert hooks.cmd_hook_post({"TMUX_PANE": "%1"}, payload) == 0


@pytest.mark.parametrize("adopted", [False, True], ids=["dedicated", "adopted"])
@pytest.mark.parametrize(
    ("resumed", "expected_partial"),
    [
        # Cut inside the replay: the partial plus the one line it typed.
        (AnimationResult("interrupted", 1), "a\nx\n"),
        # An S pressed during the rebuild, which leaves it for the replay's
        # first check: the whole partial, nothing of the remainder.
        (AnimationResult("interrupted", 0), "a\n"),
    ],
    ids=["during-the-replay", "during-the-rebuild"],
)
def test_s_during_a_catch_up_hands_the_buffer_over_as_it_is(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    resumed: AnimationResult,
    expected_partial: str,
    adopted: bool,
) -> None:
    """Alberto's answer A (Task 20): the S is obeyed. This edit's hand-off
    wait runs with the catch-up's partial, no remainder is saved, and the new
    edit is not animated (the user holds the buffer now). Adopted too: there
    the catch-up runs once the probe finds the partial ("holds")."""
    resolved, payload = _orphaned_remainder_and_edit(tmp_path, adopted)
    follower = _fake_follower()
    follower.resume.return_value = resumed
    # "handoff" only after hand_over's sends (T20 re-review m5).
    states_during_hand_over: list[str | None] = []
    follower.hand_over.side_effect = lambda: states_during_hand_over.append(
        control.animating_state("@1")
    )
    monkeypatch.setattr(hooks, "get_follower", lambda *arguments, **keyword_arguments: follower)
    monkeypatch.setattr(
        hooks, "status_surface_for", lambda *arguments, **keyword_arguments: MagicMock()
    )
    seen: dict[str, object] = {}

    def fake_wait(
        current: object,
        session: object,
        file_path: str,
        after: str,
        partial_content: str,
        surface: object = None,
        raw_after: bytes | None = None,
        earlier_edit: str | None = None,
    ) -> None:
        seen["file_path"] = file_path
        seen["earlier_edit"] = earlier_edit
        seen["after"] = after
        seen["partial"] = partial_content
        seen["raw_after"] = raw_after
        seen["pending"] = control.has_pending_animation("@1")
        states_at_the_wait.append(control.animating_state("@1"))

    states_at_the_wait: list[str | None] = []
    monkeypatch.setattr(hooks, "_await_user_handoff", fake_wait)

    _run_post_hook(payload, None)

    assert states_during_hand_over == ["running"]
    assert states_at_the_wait == ["handoff"]

    assert seen == {
        "file_path": resolved,
        "after": "a\nx\ny\nz\nNEW\n",
        "partial": expected_partial,
        "raw_after": b"a\nx\ny\nz\nNEW\n",
        "pending": False,
        "earlier_edit": "shown",
    }
    follower.hand_over.assert_called_once_with()
    follower.apply_edit.assert_not_called()
    follower.show_fresh.assert_not_called()
    assert not control.has_pending_animation("@1")


def test_s_again_after_an_interrupted_catch_up_shows_claudes_finished_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The second S of answer A: no remainder was saved, so the des-interrupt
    takes _replay_remainder's `pending is None` path and reloads the file
    Claude wrote, with no animation."""
    resolved, payload = _orphaned_remainder_and_edit(tmp_path)
    follower = _fake_follower()
    follower.resume.return_value = AnimationResult("interrupted", 1)
    monkeypatch.setattr(hooks, "get_follower", lambda *arguments, **keyword_arguments: follower)
    monkeypatch.setattr(
        hooks, "status_surface_for", lambda *arguments, **keyword_arguments: MagicMock()
    )
    monkeypatch.setattr(hooks, "show_popup", lambda *arguments, **keyword_arguments: None)

    # "interrupt" on every poll: the hand-off wait des-interrupts at once.
    _run_post_hook(payload, "interrupt")

    follower.reload_and_relock.assert_called_once_with(resolved)
    follower.rewrite_buffer.assert_called_once_with(resolved, "a\n")  # the catch-up's only
    follower.resume.assert_called_once()
    follower.apply_edit.assert_not_called()
    follower.show_fresh.assert_not_called()
    current = state.FollowerState.read("@1")
    assert current is not None
    assert current.current_file == resolved


@pytest.mark.parametrize(
    ("partial", "quoted"),
    [("a\n", "a\nx"), (None, None)],
    ids=["known-partial", "legacy-no-partial"],
)
def test_a_save_after_an_interrupted_catch_up_says_what_was_really_shown(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    partial: str | None,
    quoted: str | None,
) -> None:
    """Fix round 1, m1. The S came before any of THIS edit was typed: what
    the user had seen was the earlier, interrupted edit's text (or, for a
    legacy remainder that recorded no partial, nobody knows how much), and
    the release notification must say so, not present it as "this much of
    your version"."""
    resolved, payload = _orphaned_remainder_and_edit(tmp_path, partial=partial)
    follower = _fake_follower()
    follower.resume.return_value = AnimationResult("interrupted", 1)
    monkeypatch.setattr(hooks, "get_follower", lambda *arguments, **keyword_arguments: follower)
    monkeypatch.setattr(
        hooks, "status_surface_for", lambda *arguments, **keyword_arguments: MagicMock()
    )

    def the_user_saves(seconds: float) -> None:
        Path(resolved).write_text("a\nx\nUSER\n")

    monkeypatch.setattr("vim_ai_follower.hooks.time.sleep", the_user_saves)
    with (
        patch("vim_ai_follower.control.check_signal", return_value=None),
        patch("vim_ai_follower.backends.nvim_connect.open_session", return_value=MagicMock()),
        patch("vim_ai_follower.backends.nvim.probe_nvim", return_value="answers"),
        patch("vim_ai_follower.tmux.subprocess.run", side_effect=make_mock_tmux_run()),
    ):
        assert hooks.cmd_hook_post({"TMUX_PANE": "%1"}, payload) == 0

    printed = capsys.readouterr().out.strip().splitlines()[-1]
    context = json.loads(printed)["hookSpecificOutput"]["additionalContext"]
    assert "SAVED their own version" in context
    assert "none of this edit had been typed" in context
    assert "Only this much of your version" not in context
    if quoted is None:
        assert "how much of the earlier edit they had been shown is not known" in context
    else:
        assert f"your earlier, interrupted edit of it:\n\n{quoted}\n\n" in context
