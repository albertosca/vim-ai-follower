"""The base-mismatch guard: an Edit's diff is typed only onto a buffer that
holds the diff's base.

`compute_edit_script(before, after)` is only meaningful on a buffer holding
`before`, the PreToolUse snapshot of the file on disk. Two measured ways the
follower's buffer does not: a buffer that is listed but UNLOADED (its tab
closed without 'hidden' — the navigation then loads the finished file from
disk and the diff is typed on top of it), and a loaded buffer with other
content (a formatter, `sed -i` or a checkout rewrote the file outside Claude;
in an adopted editor, the user typed into it). The hook asks the follower
(Follower.probe_buffer) and:

- dedicated follower: anything but "holds" is retyped through show_fresh;
- ADOPTED editor (the user's own): "absent" is retyped (nothing to lose),
  "holds" is a diff even for a file the follower would call fresh, and
  "differs"/"unknown" is left alone — no wipe, no animation — with the file
  marked stale and a status cue (Alberto's policy, 2026-09-25).

These pin that decision; the backends' answers are pinned in
tests/test_tmux_probe_buffer.py and tests/test_nvim_integration_probe_buffer.py,
and the on-screen effect in tests/test_integration_edit_base_mismatch.py,
tests/test_integration_adopted_base_mismatch.py and their nvim twins.
"""

from __future__ import annotations

import os
from collections.abc import Callable
from pathlib import Path
from unittest.mock import MagicMock, call, patch

import pytest
from helpers import make_mock_tmux_run

from vim_ai_follower import control, diff, hooks, snapshot, state
from vim_ai_follower.animate import AnimationResult

BEFORE = "a\nb\nc\n"
AFTER = "a\nB\nc\n"


def _follower(probe: object) -> MagicMock:
    follower = MagicMock()
    follower.apply_edit.return_value = AnimationResult("completed", 1)
    follower.show_fresh.return_value = AnimationResult("completed", 3)
    follower.rewrite_buffer.return_value = AnimationResult("completed", 1)
    follower.resume.return_value = AnimationResult("completed", 1)
    if isinstance(probe, BaseException):
        follower.probe_buffer.side_effect = probe
    else:
        follower.probe_buffer.return_value = probe
    return follower


def _setup(
    tmp_path: Path, *, stale: bool = False, adopted: bool = False, is_open: bool = True
) -> tuple[Path, str]:
    target = tmp_path / "f.txt"
    target.write_text(AFTER)
    resolved = os.path.realpath(str(target))
    state.FollowerState.set(
        "@1",
        "nvim",
        "/tmp/x.sock",
        current_file=resolved if is_open else None,
        open_files=(resolved,) if is_open else (),
        stale_files=(resolved,) if stale else (),
        shown_any=True,
        adopted=adopted,
    )
    snapshot.save("@1", resolved, BEFORE)
    return target, resolved


def _run_hook(target: Path, follower: MagicMock, monkeypatch: pytest.MonkeyPatch) -> MagicMock:
    """Runs the post hook; returns the one status surface every call shares."""
    surface = MagicMock()
    monkeypatch.setattr(hooks, "get_follower", lambda *a, **k: follower)
    monkeypatch.setattr(hooks, "status_surface_for", lambda *a, **k: surface)
    monkeypatch.setattr(hooks, "show_popup", lambda *a, **k: None)
    payload: dict[str, object] = {"tool_name": "Edit", "tool_input": {"file_path": str(target)}}
    with (
        patch("vim_ai_follower.hooks.time.sleep"),
        patch("vim_ai_follower.tmux.subprocess.run", side_effect=make_mock_tmux_run()),
        patch("pynvim.attach", return_value=MagicMock()),
    ):
        assert hooks.cmd_hook_post({"TMUX_PANE": "%1"}, payload) == 0
    return surface


def test_a_buffer_holding_the_base_gets_the_diff(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target, resolved = _setup(tmp_path)
    follower = _follower("holds")
    _run_hook(target, follower, monkeypatch)

    # Asked about the pre-edit snapshot, not the finished file on disk.
    assert follower.probe_buffer.call_args == call(resolved, BEFORE)
    assert follower.apply_edit.call_args.args[0] == resolved
    assert follower.show_fresh.call_args_list == []


def test_a_buffer_not_holding_the_base_is_retyped_in_full(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target, resolved = _setup(tmp_path)
    follower = _follower("differs")
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
    follower = _follower("holds")
    _run_hook(target, follower, monkeypatch)

    assert follower.probe_buffer.call_args_list == []
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

    follower = _follower("holds")
    follower.rewrite_buffer.side_effect = _step("rewrite", AnimationResult("completed", 1))
    follower.resume.side_effect = _step("resume", AnimationResult("completed", 2))
    follower.probe_buffer.side_effect = _step("probe", "holds")
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
    follower = _follower("differs")
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


@pytest.mark.parametrize("probe", ["absent", "unknown"])
def test_a_dedicated_follower_retypes_on_every_answer_but_holds(
    probe: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target, resolved = _setup(tmp_path)
    follower = _follower(probe)
    _run_hook(target, follower, monkeypatch)
    assert follower.apply_edit.call_args_list == []
    assert follower.show_fresh.call_args == call(resolved, AFTER, in_new_tab=True)


@pytest.mark.parametrize("probe", ["differs", "unknown", OSError("socket gone")])
def test_an_adopted_buffer_that_is_not_the_base_is_left_alone(
    probe: object, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Never wipe (show_fresh), never animate (apply_edit): mark stale, say
    so on the surface, log it — and draw nothing else on the surface."""
    target, resolved = _setup(tmp_path, adopted=True)
    follower = _follower(probe)
    surface = _run_hook(target, follower, monkeypatch)

    assert follower.show_fresh.call_args_list == []
    assert follower.apply_edit.call_args_list == []
    assert follower.close_tab.call_args_list == []
    fs = state.FollowerState.read("@1")
    assert fs is not None
    assert fs.stale_files == (resolved,)
    # The cue is the surface's last word: nothing clears or overwrites it.
    cue = hooks.BASE_DIFFERS_CUE if probe == "differs" else hooks.PROBE_UNKNOWN_CUE
    assert surface.method_calls[-1] == call.set_state(cue)
    assert call.set_state("Writing...") not in surface.method_calls
    assert resolved in hooks.LOG_PATH.read_text()


def test_an_adopted_fresh_file_the_user_has_loaded_is_left_alone_untracked(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The pre-B2 analog: the follower never opened the file, so the edit is
    fresh — but the user has it loaded with other content. Not wiped, and not
    pulled into open_files (where eviction would later wipe it); nothing to
    mark stale, since stale is a subset of open."""
    target, resolved = _setup(tmp_path, adopted=True, is_open=False)
    follower = _follower("differs")
    surface = _run_hook(target, follower, monkeypatch)

    assert follower.probe_buffer.call_args == call(resolved, BEFORE)
    assert follower.show_fresh.call_args_list == []
    fs = state.FollowerState.read("@1")
    assert fs is not None
    assert fs.open_files == ()
    assert surface.method_calls[-1] == call.set_state(hooks.BASE_DIFFERS_CUE)


def test_an_adopted_fresh_file_with_nothing_loaded_is_retyped(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target, resolved = _setup(tmp_path, adopted=True, is_open=False)
    follower = _follower("absent")
    _run_hook(target, follower, monkeypatch)
    assert follower.show_fresh.call_args == call(resolved, AFTER, in_new_tab=True)


def test_an_adopted_fresh_file_the_user_has_loaded_as_the_base_gets_a_diff(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A buffer the user opened that holds exactly the base is animated into
    like any other — the retype would wipe it (undo history, marks) for
    nothing."""
    target, resolved = _setup(tmp_path, adopted=True, is_open=False)
    follower = _follower("holds")
    _run_hook(target, follower, monkeypatch)
    assert follower.show_fresh.call_args_list == []
    assert follower.apply_edit.call_args.args[0] == resolved


def test_an_adopted_stale_file_the_user_resynced_gets_a_diff_and_loses_the_mark(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """After `:e!` the buffer is the base again: the stale mark must not keep
    routing it to the retype (which in an adopted editor is the wipe)."""
    target, resolved = _setup(tmp_path, adopted=True, stale=True)
    follower = _follower("holds")
    surface = _run_hook(target, follower, monkeypatch)

    assert follower.show_fresh.call_args_list == []
    assert follower.apply_edit.call_args.args[0] == resolved
    fs = state.FollowerState.read("@1")
    assert fs is not None
    assert fs.stale_files == ()
    # The earlier cues come down before this animation's own cue goes up.
    retire = surface.method_calls.index(
        call.retire_state(hooks.BASE_DIFFERS_CUE, hooks.PROBE_UNKNOWN_CUE)
    )
    assert retire < surface.method_calls.index(call.set_state("Writing..."))


def _pending_apply_edit(resolved: str, partial: str | None) -> None:
    control.save_pending_apply_edit(
        "@1",
        diff.compute_edit_script(BEFORE, "a\nX\nc\n"),
        0.01,
        file_path=resolved,
        partial=partial,
    )


@pytest.mark.parametrize(
    ("probe", "partial", "cue"),
    [
        ("differs", "a\nb\nc\n", "BASE_DIFFERS_CUE"),
        ("unknown", "a\nb\nc\n", "PROBE_UNKNOWN_CUE"),
        (OSError("gone"), "a\nb\nc\n", "PROBE_UNKNOWN_CUE"),
        ("holds", None, "PROBE_UNKNOWN_CUE"),  # tmux can leave no partial
    ],
)
def test_an_adopted_catch_up_never_rewrites_a_buffer_not_holding_the_partial(
    probe: object, partial: str | None, cue: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A remainder left by a killed hand-off or pause is replayed with
    rewrite_buffer(partial), which discards whatever the buffer holds. In an
    adopted editor that may be the user's typing since: replay only onto a
    buffer that still holds the partial, else drop the remainder and leave
    the buffer alone. Without a recorded partial nothing can be checked."""
    target, resolved = _setup(tmp_path, adopted=True)
    _pending_apply_edit(resolved, partial)
    follower = _follower(probe)
    surface = _run_hook(target, follower, monkeypatch)

    assert follower.rewrite_buffer.call_args_list == []
    assert follower.resume.call_args_list == []
    assert follower.show_fresh.call_args_list == []
    assert follower.apply_edit.call_args_list == []
    assert control.load_pending_animation("@1") is None
    fs = state.FollowerState.read("@1")
    assert fs is not None
    assert fs.stale_files == (resolved,)
    assert surface.method_calls[-1] == call.set_state(getattr(hooks, cue))
    if partial is not None:
        assert follower.probe_buffer.call_args_list[0] == call(resolved, partial)
    else:
        assert follower.probe_buffer.call_args_list == []


def test_an_adopted_catch_up_replays_onto_a_buffer_holding_the_partial(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target, resolved = _setup(tmp_path, adopted=True)
    _pending_apply_edit(resolved, "a\nb\nc\n")
    follower = _follower("holds")
    _run_hook(target, follower, monkeypatch)

    assert follower.rewrite_buffer.call_args == call(resolved, "a\nb\nc\n")
    assert follower.resume.call_args_list != []
    assert follower.apply_edit.call_args_list != []


def test_an_adopted_catch_up_onto_a_vanished_buffer_is_dropped_and_retyped(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """ "absent": nothing in the buffer to lose and nothing to replay onto."""
    target, resolved = _setup(tmp_path, adopted=True)
    _pending_apply_edit(resolved, "a\nb\nc\n")
    follower = _follower("absent")
    _run_hook(target, follower, monkeypatch)

    assert follower.rewrite_buffer.call_args_list == []
    assert follower.show_fresh.call_args == call(resolved, AFTER, in_new_tab=True)


def test_a_dedicated_catch_up_is_unchanged(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A dedicated follower owns its buffers: the catch-up replays without
    asking, exactly as before."""
    target, resolved = _setup(tmp_path)
    _pending_apply_edit(resolved, "a\nb\nc\n")
    follower = _follower("holds")
    _run_hook(target, follower, monkeypatch)

    assert follower.rewrite_buffer.call_args == call(resolved, "a\nb\nc\n")
    assert follower.probe_buffer.call_args_list == [call(resolved, BEFORE)]


def test_an_unanswered_probe_gets_its_own_honest_cue(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """ "unknown" is not "differs": nothing is known to differ, and `:e!`
    cannot make the probe answer."""
    target, _ = _setup(tmp_path, adopted=True)
    surface = _run_hook(target, _follower("unknown"), monkeypatch)
    assert surface.method_calls[-1] == call.set_state(hooks.PROBE_UNKNOWN_CUE)
    assert ":e!" not in hooks.PROBE_UNKNOWN_CUE
    assert "did not answer" in hooks.LOG_PATH.read_text()


def test_both_cues_fit_the_follower_pane() -> None:
    """Measured in a real 49-column tmux pane (border status top, mouse on);
    see the B2 report. 41 characters is the longest measured whole."""
    assert len(hooks.BASE_DIFFERS_CUE) <= 41
    assert len(hooks.PROBE_UNKNOWN_CUE) <= 41


# --- after a completed catch-up: ground, don't leave alone ----------------


def _caught_up_follower(adopted: bool, base_answer: object) -> MagicMock:
    """An adopted catch-up asks about the partial first ("holds"); both then
    ask about the new edit's base."""
    follower = _follower("holds")
    answers: list[object] = ["holds", base_answer] if adopted else [base_answer]
    follower.probe_buffer.side_effect = answers
    return follower


@pytest.mark.parametrize(
    ("adopted", "answer"),
    [(True, "differs"), (False, "differs"), (True, "unknown")],
    ids=["adopted-differs", "dedicated-differs", "adopted-unknown"],
)
def test_a_completed_catch_up_that_is_not_the_base_is_reloaded_from_disk(
    adopted: bool, answer: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The catch-up just rebuilt the buffer from the partial and typed the
    rest, so it holds only follower text. An outside change since (a
    formatter, `sed -i`) makes it "differs": reload it from disk, with no
    animation, no cue and no stale mark. Left alone it stays modified while
    disk moved on, and `:checktime` raises W12 (controller ruling,
    2026-09-25). An adopted "unknown" is grounded by the same reasoning."""
    target, resolved = _setup(tmp_path, adopted=adopted)
    _pending_apply_edit(resolved, "a\nb\nc\n")
    follower = _caught_up_follower(adopted, answer)
    surface = _run_hook(target, follower, monkeypatch)

    assert follower.resume.call_args_list != []
    assert follower.reload_from_disk.call_args_list == [call(resolved)]
    assert follower.apply_edit.call_args_list == []
    assert follower.show_fresh.call_args_list == []
    assert call.set_state(hooks.BASE_DIFFERS_CUE) not in surface.method_calls
    assert call.set_state(hooks.PROBE_UNKNOWN_CUE) not in surface.method_calls
    assert call.set_state("Writing...") not in surface.method_calls
    assert surface.method_calls[-1] == call.retire_state(
        hooks.BASE_DIFFERS_CUE, hooks.PROBE_UNKNOWN_CUE
    )
    fs = state.FollowerState.read("@1")
    assert fs is not None
    assert resolved not in fs.stale_files
    assert fs.current_file == resolved
    assert "reloaded" in hooks.LOG_PATH.read_text()


def test_a_dedicated_catch_up_whose_base_probe_goes_unanswered_still_retypes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Dedicated "unknown" keeps the retype: show_fresh's relock grounds the
    buffer on disk anyway, and the edit stays visible."""
    target, resolved = _setup(tmp_path)
    _pending_apply_edit(resolved, "a\nb\nc\n")
    follower = _caught_up_follower(False, "unknown")
    _run_hook(target, follower, monkeypatch)

    assert follower.reload_from_disk.call_args_list == []
    assert follower.show_fresh.call_args == call(resolved, AFTER, in_new_tab=True)


@pytest.mark.parametrize("stopped", ["rewrite", "resume"])
@pytest.mark.parametrize("adopted", [True, False], ids=["adopted", "dedicated"])
def test_a_catch_up_that_did_not_complete_is_never_grounded(
    stopped: str, adopted: bool, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An interrupted catch-up is not a buffer of follower text only: the
    usual policy applies (adopted: left alone with the cue; dedicated:
    retyped)."""
    target, resolved = _setup(tmp_path, adopted=adopted)
    _pending_apply_edit(resolved, "a\nb\nc\n")
    follower = _caught_up_follower(adopted, "differs")
    getattr(
        follower, f"{stopped}_buffer" if stopped == "rewrite" else stopped
    ).return_value = AnimationResult("interrupted", 0)
    surface = _run_hook(target, follower, monkeypatch)

    assert follower.reload_from_disk.call_args_list == []
    if adopted:
        assert surface.method_calls[-1] == call.set_state(hooks.BASE_DIFFERS_CUE)
    else:
        assert follower.show_fresh.call_args == call(resolved, AFTER, in_new_tab=True)


def test_a_reload_that_raises_never_fails_the_hook(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target, resolved = _setup(tmp_path, adopted=True)
    _pending_apply_edit(resolved, "a\nb\nc\n")
    follower = _caught_up_follower(True, "differs")
    follower.reload_from_disk.side_effect = OSError("pane gone")
    _run_hook(target, follower, monkeypatch)  # asserts the hook returned 0

    assert follower.apply_edit.call_args_list == []
    assert "could not reload" in hooks.LOG_PATH.read_text()
