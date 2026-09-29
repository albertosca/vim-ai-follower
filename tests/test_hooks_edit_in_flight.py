"""A Read that lands between an Edit's pre and post hooks (BACKLOG D2).

Two agents on one window: agent A's Edit ran its pre hook and wrote the file;
agent B's Read of the same file posts before A's post hook. No animation slot
is held from pre to post, so the Read re-read the clean buffer — the finished
file on screen — and A's post found a buffer that was no longer its base
(retyped on a dedicated follower, left alone with a cue on an adopted one).

The pre hook now marks the edit in flight next to its snapshot; a Read of that
file skips while the mark is live AND the file on disk is no longer the
snapshot. The mark is cleared by the post hook, and it expires, because an
Edit that is denied or fails never runs a post hook.

The real-editor proofs (a mid-animation observer on tmux+Vim and on nvim) are
in tests/test_integration_edit_no_reload.py and
tests/test_nvim_integration_edit_base_mismatch.py.
"""

from __future__ import annotations

import io
import json
import os
import time
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from helpers import make_mock_tmux_run as _mock_tmux_run
from helpers import register_fake_follower as _register_fake_follower

from vim_ai_follower import cli, config, control, hooks, snapshot, state

_ENV = {"TMUX_PANE": "%1"}


def _payload(tool: str, target: Path) -> dict[str, Any]:
    return {"tool_name": tool, "tool_input": {"file_path": str(target)}}


def _pre(target: Path, tool: str = "Edit", **extra: Any) -> None:
    with patch(
        "vim_ai_follower.tmux.subprocess.run",
        return_value=MagicMock(returncode=0, stdout="@1\n"),
    ):
        assert hooks.cmd_hook_pre(_ENV, {**_payload(tool, target), **extra}) == 0


def _read(target: Path) -> list[list[str]]:
    """Run a Read post hook against a fake tmux; the send-keys it issued."""
    with patch("vim_ai_follower.tmux.subprocess.run", side_effect=_mock_tmux_run()) as run:
        assert hooks.cmd_hook_post(_ENV, _payload("Read", target)) == 0
    return [
        c for c in (call.args[0] for call in run.call_args_list) if c[:2] == ["tmux", "send-keys"]
    ]


def _open_files() -> tuple[str, ...]:
    current = state.FollowerState.read("@1")
    assert current is not None
    return current.open_files


def _real(target: Path) -> str:
    return os.path.realpath(str(target))


@pytest.mark.parametrize("tool", ["Edit", "MultiEdit", "Write"])
def test_the_pre_hook_marks_the_edit_in_flight(tmp_path: Path, tool: str) -> None:
    target = tmp_path / "a.py"
    target.write_text("one\n")
    _pre(target, tool)
    assert snapshot.in_flight("@1", _real(target))
    assert not snapshot.in_flight("@1", _real(tmp_path / "b.py"))


def test_a_read_of_a_file_whose_edit_is_written_but_not_posted_leaves_the_follower_alone(
    tmp_path: Path,
) -> None:
    target = tmp_path / "a.py"
    target.write_text("one\n")
    _register_fake_follower("@1", "%2", shown_any=True)
    _pre(target)
    target.write_text("one\ntwo\n")  # the Edit tool wrote; its post has not run

    assert _read(target) == []
    assert _open_files() == ()  # untouched: the Edit's post brings the file on screen
    assert snapshot.in_flight("@1", _real(target))  # a Read never consumes the mark


def test_a_read_before_the_edit_writes_navigates_as_usual(tmp_path: Path) -> None:
    """Disk still holds the snapshot — the edit is waiting on a permission
    prompt, or was denied or failed and will never post. A re-read then shows
    exactly the edit's base, so there is nothing to protect."""
    target = tmp_path / "a.py"
    target.write_text("one\n")
    _register_fake_follower("@1", "%2", shown_any=True)
    _pre(target)

    assert _read(target) != []
    assert _open_files() == (_real(target),)


def test_an_expired_mark_no_longer_holds_a_read_back(tmp_path: Path) -> None:
    """An Edit that fails after writing, or whose post hook is killed, leaves
    the mark behind; it must not silence Reads of that file forever."""
    target = tmp_path / "a.py"
    target.write_text("one\n")
    _register_fake_follower("@1", "%2", shown_any=True)
    _pre(target)
    target.write_text("one\ntwo\n")
    mark = snapshot.in_flight_path("@1", _real(target))
    old = time.time() - snapshot.IN_FLIGHT_TTL_SECONDS - 1
    os.utime(mark, (old, old))

    assert not snapshot.in_flight("@1", _real(target))
    assert _read(target) != []
    assert _open_files() == (_real(target),)


def test_a_mark_just_inside_its_ttl_still_holds(tmp_path: Path) -> None:
    target = tmp_path / "a.py"
    target.write_text("one\n")
    _pre(target)
    mark = snapshot.in_flight_path("@1", _real(target))
    recent = time.time() - snapshot.IN_FLIGHT_TTL_SECONDS + 5
    os.utime(mark, (recent, recent))
    assert snapshot.in_flight("@1", _real(target))


def test_a_read_whose_file_cannot_be_decoded_is_held_back_while_in_flight(
    tmp_path: Path,
) -> None:
    """The pre hook snapshots an undecodable file as "" — so undecodable disk
    content is never that snapshot: the edit wrote, and the Read waits."""
    target = tmp_path / "a.bin"
    target.write_bytes(b"\xff\xfe\x00")
    _register_fake_follower("@1", "%2", shown_any=True)
    _pre(target)
    target.write_bytes(b"\xff\xfe\x01")

    assert _read(target) == []


def test_a_read_of_another_file_is_not_held_back(tmp_path: Path) -> None:
    target = tmp_path / "a.py"
    other = tmp_path / "b.py"
    target.write_text("one\n")
    other.write_text("other\n")
    _register_fake_follower("@1", "%2", shown_any=True)
    _pre(target)
    target.write_text("one\ntwo\n")

    assert _read(other) != []
    assert _open_files() == (_real(other),)


def test_the_edit_post_hook_clears_the_mark_once_it_holds_the_slot(tmp_path: Path) -> None:
    """Handed over, not dropped: at the clear the slot is already held, so a
    Read is kept off the pane at every instant (cleared first, a Read landing
    between the two steps would re-read the finished file). And the mark is
    gone before the animation starts: a hook killed later — a hook timeout in
    the hand-off wait never reaches a `finally` — cannot leave it behind to
    silence Reads of the file until it expires."""
    target = tmp_path / "a.py"
    target.write_text("one\n")
    _register_fake_follower("@1", "%2", shown_any=True)
    _pre(target)
    target.write_text("one\ntwo\n")

    slot_held_at_clear: list[bool] = []
    real_clear = snapshot.clear_in_flight

    def _clear(window_id: str, file_path: str, *args: Any, **kwargs: Any) -> None:
        slot_held_at_clear.append(control.is_animating(window_id))
        real_clear(window_id, file_path, *args, **kwargs)

    at_animation: list[tuple[bool, bool]] = []

    def _animate(*args: Any, **kwargs: Any) -> int:
        at_animation.append((snapshot.in_flight("@1", _real(target)), control.is_animating("@1")))
        return 0

    with (
        patch("vim_ai_follower.tmux.subprocess.run", side_effect=_mock_tmux_run()),
        patch.object(snapshot, "clear_in_flight", _clear),
        patch.object(hooks, "_animate_edit", _animate),
    ):
        assert hooks.cmd_hook_post(_ENV, _payload("Edit", target)) == 0

    assert slot_held_at_clear == [True]
    assert at_animation == [(False, True)]
    assert _read(target) != []  # the next Read is an ordinary one again


def test_an_edit_post_that_finds_the_slot_busy_clears_the_mark_too(tmp_path: Path) -> None:
    """That edit is never animated (its file is marked stale instead), so a
    later Read re-reading the finished file is right, not a flash."""
    target = tmp_path / "a.py"
    target.write_text("one\n")
    _register_fake_follower("@1", "%2", shown_any=True)
    _pre(target)
    target.write_text("one\ntwo\n")
    control.mark_animating("@1")  # another hook of this process's window owns the pane
    try:
        with patch("vim_ai_follower.tmux.subprocess.run", side_effect=_mock_tmux_run()):
            assert hooks.cmd_hook_post(_ENV, _payload("Edit", target)) == 0
        assert control.is_animating("@1")  # the other hook's slot is not ours to release
    finally:
        control.clear_animating("@1")
    assert not snapshot.in_flight("@1", _real(target))


def test_clearing_a_mark_that_was_never_set_is_harmless(tmp_path: Path) -> None:
    snapshot.clear_in_flight("@1", str(tmp_path / "never.py"))
    assert not snapshot.in_flight("@1", str(tmp_path / "never.py"))


# --------------------------------------------------------------- fix round 1
#
# What clears a mark whose Edit never posts, measured against Claude Code
# 2.1.284 in a scratch directory (D2-report.md, "Fix round 1"):
#   - a tool that FAILS while executing (EACCES) fires PostToolUseFailure;
#   - a user answering "No" at the permission prompt, and a headless
#     auto-deny, fire only PreToolUse — neither PostToolUseFailure nor
#     PermissionDenied (the docs scope that one to auto mode);
#   - one agent's tool calls, even two issued in one message (Edit + Read),
#     run one after the other, post hook included.
# So `hook failure` (registered for PostToolUseFailure and PermissionDenied)
# clears the mark, and a Read by the mark's OWN writer clears it too: that
# writer's Edit tool call is over, however it ended.


def _failure(target: Path, tool: str = "Edit") -> None:
    with patch(
        "vim_ai_follower.tmux.subprocess.run",
        return_value=MagicMock(returncode=0, stdout="@1\n"),
    ):
        assert hooks.cmd_hook_failure(_ENV, _payload(tool, target)) == 0


@pytest.mark.parametrize("tool", ["Edit", "MultiEdit", "Write"])
def test_the_failure_hook_clears_the_mark(tmp_path: Path, tool: str) -> None:
    target = tmp_path / "a.py"
    target.write_text("one\n")
    _pre(target, tool)
    _failure(target, tool)
    assert not snapshot.in_flight("@1", _real(target))


def test_the_failure_hook_ignores_other_tools(tmp_path: Path) -> None:
    target = tmp_path / "a.py"
    target.write_text("one\n")
    _pre(target)
    _failure(target, "Bash")
    assert snapshot.in_flight("@1", _real(target))


def test_the_failure_hook_clears_even_while_the_follower_is_disabled(tmp_path: Path) -> None:
    """Clearing is always safe, and a toggle between pre and failure must not
    strand the mark."""
    target = tmp_path / "a.py"
    target.write_text("one\n")
    _register_fake_follower("@1", "%2", shown_any=True)
    _pre(target)
    state.FollowerState.update("@1", enabled=False)
    _failure(target)
    assert not snapshot.in_flight("@1", _real(target))


def test_the_failure_hook_without_a_window_leaves_every_mark_alone(tmp_path: Path) -> None:
    """Nothing resolved at all (no pane, no synthetic identity): the hook
    cannot tell which window's mark is meant, so it touches none — and still
    never fails."""
    target = tmp_path / "a.py"
    target.write_text("one\n")
    _pre(target)
    with patch("vim_ai_follower.hooks.resolve_session", return_value=None):
        assert hooks.cmd_hook_failure(_ENV, _payload("Edit", target)) == 0
    assert snapshot.in_flight("@1", _real(target))


def test_the_failure_hook_without_a_file_path_leaves_every_mark_alone(tmp_path: Path) -> None:
    target = tmp_path / "a.py"
    target.write_text("one\n")
    _pre(target)
    with patch(
        "vim_ai_follower.tmux.subprocess.run",
        return_value=MagicMock(returncode=0, stdout="@1\n"),
    ):
        assert hooks.cmd_hook_failure(_ENV, {"tool_name": "Edit", "tool_input": {}}) == 0
    assert snapshot.in_flight("@1", _real(target))


def test_an_unreadable_mark_has_no_writer(tmp_path: Path) -> None:
    """A mark whose bytes aren't text reads as "no writer", so no Read can
    claim it as its own — it waits for the post/failure hook or the TTL."""
    target = tmp_path / "a.py"
    target.write_text("one\n")
    _pre(target)
    snapshot.in_flight_path("@1", _real(target)).write_bytes(b"\xff\xfe")
    assert snapshot.in_flight_writer("@1", _real(target)) == ""


def test_hook_failure_is_a_cli_subcommand_that_never_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tmp_path / "a.py"
    target.write_text("one\n")
    _pre(target)
    body = (
        '{"hook_event_name": "PostToolUseFailure", "tool_name": "Edit", '
        f'"tool_input": {{"file_path": "{target}"}}, "error": "EACCES"}}'
    )
    monkeypatch.setattr("sys.stdin", io.StringIO(body))
    monkeypatch.setenv("TMUX_PANE", "%1")
    with patch(
        "vim_ai_follower.tmux.subprocess.run",
        return_value=MagicMock(returncode=0, stdout="@1\n"),
    ):
        assert cli.main(["hook", "failure"]) == 0
    assert not snapshot.in_flight("@1", _real(target))

    monkeypatch.setattr("sys.stdin", io.StringIO("not json"))
    assert cli.main(["hook", "failure"]) == 0
    assert "hook failure crashed" in hooks.LOG_PATH.read_text()


def test_the_plugin_registers_the_failure_hook_for_edit_tools() -> None:
    manifest = json.loads((Path(__file__).parent.parent / "hooks" / "hooks.json").read_text())
    for event in ("PostToolUseFailure", "PermissionDenied"):
        entries = manifest["hooks"][event]
        assert [e["matcher"] for e in entries] == ["Edit|MultiEdit|Write"], event
        (command,) = [h["command"] for h in entries[0]["hooks"]]
        assert command.endswith("python3 -m vim_ai_follower.cli hook failure"), command


def test_a_read_by_the_marks_own_writer_clears_it_and_navigates(tmp_path: Path) -> None:
    """The reviewer's case: a denied Edit (no post, no failure hook), the same
    agent's `sed -i` through Bash, then its Read to check — C5's resync must
    show the file."""
    target = tmp_path / "a.py"
    target.write_text("one\n")
    _register_fake_follower("@1", "%2", shown_any=True)
    _pre(target, session_id="sess-a")
    target.write_text("one\nsed\n")

    with patch("vim_ai_follower.tmux.subprocess.run", side_effect=_mock_tmux_run()) as run:
        assert hooks.cmd_hook_post(_ENV, {**_payload("Read", target), "session_id": "sess-a"}) == 0
    assert [
        c for c in (call.args[0] for call in run.call_args_list) if c[:2] == ["tmux", "send-keys"]
    ]
    assert _open_files() == (_real(target),)
    assert not snapshot.in_flight("@1", _real(target))


def test_a_read_by_another_writer_is_still_held_back(tmp_path: Path) -> None:
    target = tmp_path / "a.py"
    target.write_text("one\n")
    _register_fake_follower("@1", "%2", shown_any=True)
    _pre(target, session_id="sess-a", agent_id="agent-1")
    target.write_text("one\ntwo\n")

    with patch("vim_ai_follower.tmux.subprocess.run", side_effect=_mock_tmux_run()) as run:
        # same session, different subagent: a concurrent writer
        payload = {**_payload("Read", target), "session_id": "sess-a", "agent_id": "agent-2"}
        assert hooks.cmd_hook_post(_ENV, payload) == 0
    assert not [
        c for c in (call.args[0] for call in run.call_args_list) if c[:2] == ["tmux", "send-keys"]
    ]
    assert snapshot.in_flight("@1", _real(target))
    log = hooks.LOG_PATH.read_text()
    assert "an Edit/Write of it is in flight or failed" in log
    assert "written but not yet animated" not in log


def test_a_mark_without_a_writer_is_never_cleared_by_a_read(tmp_path: Path) -> None:
    """No identity on either side is no evidence the Read came from the
    writer: an empty identity must not match an empty identity."""
    target = tmp_path / "a.py"
    target.write_text("one\n")
    _register_fake_follower("@1", "%2", shown_any=True)
    _pre(target)  # no session_id / agent_id in the payload
    target.write_text("one\ntwo\n")
    assert _read(target) == []
    assert snapshot.in_flight("@1", _real(target))


def test_the_post_hook_clears_the_mark_when_the_follower_is_disabled(tmp_path: Path) -> None:
    target = tmp_path / "a.py"
    target.write_text("one\n")
    _register_fake_follower("@1", "%2", shown_any=True)
    _pre(target)
    state.FollowerState.update("@1", enabled=False)
    target.write_text("one\ntwo\n")
    with patch("vim_ai_follower.tmux.subprocess.run", side_effect=_mock_tmux_run()):
        assert hooks.cmd_hook_post(_ENV, _payload("Edit", target)) == 0
    assert not snapshot.in_flight("@1", _real(target))


def test_the_post_hook_clears_the_mark_when_the_policy_skips_the_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A config change between pre and post (open_policy=code, a non-code
    file) must not strand the mark."""
    target = tmp_path / "notes.txt"
    target.write_text("one\n")
    _register_fake_follower("@1", "%2", shown_any=True)
    _pre(target)
    config_path = tmp_path / "config.json"
    config_path.write_text('{"open_policy": "code"}')
    monkeypatch.setattr(config, "CONFIG_PATH", config_path)
    target.write_text("one\ntwo\n")
    with patch("vim_ai_follower.tmux.subprocess.run", side_effect=_mock_tmux_run()):
        assert hooks.cmd_hook_post(_ENV, _payload("Edit", target)) == 0
    assert not snapshot.in_flight("@1", _real(target))


def test_the_post_hook_clears_the_mark_when_the_slot_acquire_raises(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tmp_path / "a.py"
    target.write_text("one\n")
    _register_fake_follower("@1", "%2", shown_any=True)
    _pre(target)
    target.write_text("one\ntwo\n")

    def _boom(*args: Any, **kwargs: Any) -> bool:
        raise PermissionError("cache dir went read-only")

    monkeypatch.setattr(control, "try_acquire_animating", _boom)
    body = json.dumps(_payload("Edit", target))
    monkeypatch.setattr("sys.stdin", io.StringIO(body))
    monkeypatch.setenv("TMUX_PANE", "%1")
    with patch("vim_ai_follower.tmux.subprocess.run", side_effect=_mock_tmux_run()):
        assert cli.main(["hook", "post"]) == 0
    assert not snapshot.in_flight("@1", _real(target))
