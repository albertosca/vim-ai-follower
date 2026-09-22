"""Wiring the session→window binding into the three hook entrypoints.

The storage layer (`binding.remember`/`binding.recall`, with its tmux-server-pid
guard and its staleness deletion) is covered by tests/test_binding.py. What is
under test here is only the wiring: WHEN the hooks write a binding, WHEN they
read one back, what they do with the answer, and — just as important — what the
recovery must never cost on the paths that cannot use it.

Background (measured live 2026-09-22): a session's tool execution can be
re-parented onto a worker whose environment carries neither `$TMUX` nor
`$TMUX_PANE`. `resolve_session` then returns a synthetic `term-…` identity,
nothing is registered under it, and every hook returns 0 doing nothing. A
binding written earlier — while the same session could still see its own pane —
is the one signal that survives that transition.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

from helpers import make_mock_tmux_run as _mock_tmux_run
from helpers import register_fake_follower as _register_fake_follower

from vim_ai_follower import hooks, snapshot

# The environment a re-parented hook really sees: TERM_SESSION_ID survives,
# TMUX and TMUX_PANE do not. Same constants as
# tests/test_hook_identity_diagnostics.py, deliberately duplicated — these two
# files pin opposite outcomes for the same input and must not drift together
# through a shared edit.
_LOST_ENV = {"TERM_SESSION_ID": "w0t0p0:87388AD3"}
_LOST_IDENTITY = "term-w0t0p0:87388AD3"
_SESSION_ID = "sess-abc123"


def _log_text() -> str:
    return hooks.LOG_PATH.read_text() if hooks.LOG_PATH.exists() else ""


def _recovery_lines() -> list[str]:
    return [line for line in _log_text().splitlines() if "stored session" in line]


def _identity_warnings() -> list[str]:
    return [line for line in _log_text().splitlines() if "lost its tmux window identity" in line]


def _live_file(tmp_path: Path) -> Path:
    target = tmp_path / "f.txt"
    target.write_text("content\n")
    return target


def _edit_payload(target: Path, **identity: str) -> dict[str, object]:
    return {"tool_name": "Edit", "tool_input": {"file_path": str(target)}, **identity}


def _read_payload(target: Path, **identity: str) -> dict[str, object]:
    return {"tool_name": "Read", "tool_input": {"file_path": str(target)}, **identity}


# --------------------------------------------------------------------------
# Writing the binding: the healthy half, while the window can still be proved
# --------------------------------------------------------------------------


def test_an_in_tmux_hook_remembers_the_window_it_proved(tmp_path: Path) -> None:
    """The half that makes the other half possible. A hook that still has
    TMUX_PANE knows its window for certain; that measurement is what gets
    persisted, so a later blind hook recalls a fact rather than guessing."""
    _register_fake_follower("@1", "%2", shown_any=True)
    target = _live_file(tmp_path)
    with (
        patch("vim_ai_follower.tmux.subprocess.run", side_effect=_mock_tmux_run()),
        patch("vim_ai_follower.hooks.binding.remember") as remember,
    ):
        payload = _edit_payload(target, session_id=_SESSION_ID)
        assert hooks.cmd_hook_post({"TMUX_PANE": "%1"}, payload) == 0
    assert remember.call_args_list == [((_SESSION_ID, "@1"), {})]


def test_a_subagents_edit_is_remembered_under_its_parent_session(tmp_path: Path) -> None:
    """Bind on session_id, never agent_id.

    writer_cue.writer_identity prefers agent_id, and that is right for the
    writer cue — two agents in one window need two colors. It is wrong here: a
    subagent shares its parent's process and window, so a per-agent binding
    would be a second copy of the same fact, ageing on its own schedule and
    inventing a way for the two to disagree."""
    _register_fake_follower("@1", "%2", shown_any=True)
    target = _live_file(tmp_path)
    with (
        patch("vim_ai_follower.tmux.subprocess.run", side_effect=_mock_tmux_run()),
        patch("vim_ai_follower.hooks.binding.remember") as remember,
    ):
        hooks.cmd_hook_post(
            {"TMUX_PANE": "%1"},
            _edit_payload(target, session_id=_SESSION_ID, agent_id="a9", agent_type="explore"),
        )
    assert remember.call_args_list == [((_SESSION_ID, "@1"), {})]


def test_a_payload_without_a_session_id_is_never_remembered(tmp_path: Path) -> None:
    """No key to file the binding under, so there is nothing to write. A hook
    payload always carries session_id in practice; this pins that a malformed
    one degrades instead of inventing a key."""
    _register_fake_follower("@1", "%2", shown_any=True)
    target = _live_file(tmp_path)
    with (
        patch("vim_ai_follower.tmux.subprocess.run", side_effect=_mock_tmux_run()),
        patch("vim_ai_follower.hooks.binding") as binding,
    ):
        hooks.cmd_hook_post({"TMUX_PANE": "%1"}, _edit_payload(target))
    assert binding.remember.call_args_list == []
    assert binding.recall.call_args_list == []


def test_an_empty_session_id_is_never_remembered(tmp_path: Path) -> None:
    _register_fake_follower("@1", "%2", shown_any=True)
    target = _live_file(tmp_path)
    with (
        patch("vim_ai_follower.tmux.subprocess.run", side_effect=_mock_tmux_run()),
        patch("vim_ai_follower.hooks.binding") as binding,
    ):
        hooks.cmd_hook_post({"TMUX_PANE": "%1"}, _edit_payload(target, session_id=""))
    assert binding.remember.call_args_list == []
    assert binding.recall.call_args_list == []


# --------------------------------------------------------------------------
# Reading it back: the blind half
# --------------------------------------------------------------------------


def test_a_blind_edit_recovers_the_remembered_window_and_animates_there(
    tmp_path: Path,
) -> None:
    """The whole feature. Without the binding this hook is the silent no-op
    that cost a live session half an hour on 2026-09-22."""
    _register_fake_follower("@18", "%23", shown_any=True)
    target = _live_file(tmp_path)
    with (
        patch("vim_ai_follower.tmux.subprocess.run", side_effect=_mock_tmux_run(pane_id="%23")),
        patch("vim_ai_follower.hooks.binding.recall", return_value="@18") as recall,
        patch("vim_ai_follower.hooks.get_follower", return_value=MagicMock()) as get_follower,
    ):
        assert hooks.cmd_hook_post(_LOST_ENV, _edit_payload(target, session_id=_SESSION_ID)) == 0
    assert recall.call_args_list == [((_SESSION_ID,), {})]
    assert get_follower.call_args_list != []
    assert get_follower.call_args_list[0].args[:2] == ("tmux", "%23")


def test_a_window_remembered_by_a_healthy_hook_is_recovered_by_a_blind_one(
    tmp_path: Path,
) -> None:
    """The round trip, with the REAL binding module in the middle.

    Every other recovery test here stubs binding.recall, which pins what the
    hooks do with an answer but would keep passing if the hooks never wrote a
    binding at all — recall would simply be answering out of a stub. This one
    fails if either half of the wiring goes missing, which is the only version
    of the feature that is worth anything: the two halves are the feature."""
    _register_fake_follower("@18", "%23", shown_any=True)
    target = _live_file(tmp_path)
    with patch("vim_ai_follower.binding._tmux_server_pid", return_value=64091):
        with (
            patch(
                "vim_ai_follower.tmux.subprocess.run",
                side_effect=_mock_tmux_run(window_id="@18", pane_id="%23"),
            ),
            patch("vim_ai_follower.hooks.get_follower", return_value=MagicMock()),
        ):
            # The healthy hook: still has TMUX_PANE, so it proves @18.
            hooks.cmd_hook_post({"TMUX_PANE": "%99"}, _edit_payload(target, session_id=_SESSION_ID))
        with (
            patch("vim_ai_follower.tmux.subprocess.run", side_effect=_mock_tmux_run(pane_id="%23")),
            patch("vim_ai_follower.hooks.get_follower", return_value=MagicMock()) as get_follower,
        ):
            # The blind one, same session: no TMUX_PANE anywhere.
            assert (
                hooks.cmd_hook_post(_LOST_ENV, _edit_payload(target, session_id=_SESSION_ID)) == 0
            )
    assert get_follower.call_args_list[0].args[:2] == ("tmux", "%23")
    assert len(_recovery_lines()) == 1
    assert _identity_warnings() == []


def test_a_recovery_says_so_at_warning_level(tmp_path: Path) -> None:
    """A recovery that turns out wrong writes one project's file into another
    project's editor. That has to be legible in the log after the fact, which
    means the line has to name all three: the session, the window it chose,
    and that a stored binding is what chose it."""
    _register_fake_follower("@18", "%23", shown_any=True)
    target = _live_file(tmp_path)
    with (
        patch("vim_ai_follower.tmux.subprocess.run", side_effect=_mock_tmux_run(pane_id="%23")),
        patch("vim_ai_follower.hooks.binding.recall", return_value="@18"),
        patch("vim_ai_follower.hooks.get_follower", return_value=MagicMock()),
    ):
        hooks.cmd_hook_post(_LOST_ENV, _edit_payload(target, session_id=_SESSION_ID))
    lines = _recovery_lines()
    assert len(lines) == 1
    assert "WARNING" in lines[0]
    assert _SESSION_ID in lines[0]
    assert "@18" in lines[0]


def test_a_successful_recovery_silences_the_lost_identity_warning(tmp_path: Path) -> None:
    """The identity warning means "this session went blind and nothing
    animated". After a recovery the second half is false, so the line would be
    wrong — and a log that cries lost on a session that is animating fine is
    the wallpaper failure the warning was throttled to avoid."""
    _register_fake_follower("@18", "%23", shown_any=True)
    target = _live_file(tmp_path)
    with (
        patch("vim_ai_follower.tmux.subprocess.run", side_effect=_mock_tmux_run(pane_id="%23")),
        patch("vim_ai_follower.hooks.binding.recall", return_value="@18"),
        patch("vim_ai_follower.hooks.get_follower", return_value=MagicMock()),
    ):
        hooks.cmd_hook_post(_LOST_ENV, _edit_payload(target, session_id=_SESSION_ID))
    assert _identity_warnings() == []


def test_a_subagents_blind_edit_recovers_to_its_parents_window(tmp_path: Path) -> None:
    """The mirror of the remember-side test: recall is keyed on session_id, so
    a subagent lands in the same window its parent proved, not in a window of
    its own (it has none) and not nowhere."""
    _register_fake_follower("@18", "%23", shown_any=True)
    target = _live_file(tmp_path)
    with (
        patch("vim_ai_follower.tmux.subprocess.run", side_effect=_mock_tmux_run(pane_id="%23")),
        patch("vim_ai_follower.hooks.binding.recall", return_value="@18") as recall,
        patch("vim_ai_follower.hooks.get_follower", return_value=MagicMock()) as get_follower,
    ):
        hooks.cmd_hook_post(
            _LOST_ENV,
            _edit_payload(target, session_id=_SESSION_ID, agent_id="a9", agent_type="explore"),
        )
    assert recall.call_args_list == [((_SESSION_ID,), {})]
    assert get_follower.call_args_list[0].args[:2] == ("tmux", "%23")


def test_the_read_hook_recovers_too(tmp_path: Path) -> None:
    """Read is the other entrypoint that resolves a follower and silently
    returns 0. Wiring only the edit path would leave navigation blind while
    edits animate — the two would disagree about which window this session is
    in, which is worse than both being blind."""
    _register_fake_follower("@18", "%23", shown_any=True)
    target = _live_file(tmp_path)
    with (
        patch("vim_ai_follower.tmux.subprocess.run", side_effect=_mock_tmux_run(pane_id="%23")),
        patch("vim_ai_follower.hooks.binding.recall", return_value="@18"),
        patch("vim_ai_follower.hooks.get_follower", return_value=MagicMock()) as get_follower,
    ):
        assert hooks.cmd_hook_post(_LOST_ENV, _read_payload(target, session_id=_SESSION_ID)) == 0
    assert get_follower.call_args_list[0].args[:2] == ("tmux", "%23")
    assert _identity_warnings() == []


def test_hook_pre_snapshots_under_the_recovered_window(tmp_path: Path) -> None:
    """pre and post must agree on the window or the diff is computed against
    an empty "before" and the whole file is retyped on every edit. Routing
    only post through the helper would produce exactly that."""
    _register_fake_follower("@18", "%23", shown_any=True)
    target = _live_file(tmp_path)
    with (
        patch("vim_ai_follower.tmux.subprocess.run", side_effect=_mock_tmux_run(pane_id="%23")),
        patch("vim_ai_follower.hooks.binding.recall", return_value="@18"),
    ):
        assert hooks.cmd_hook_pre(_LOST_ENV, _edit_payload(target, session_id=_SESSION_ID)) == 0
    assert snapshot.load("@18", str(target)) == "content\n"
    assert snapshot.load(_LOST_IDENTITY, str(target)) == ""


# --------------------------------------------------------------------------
# The guards: when recovery must NOT happen
# --------------------------------------------------------------------------


def test_no_recovery_when_the_remembered_window_has_no_live_follower(
    tmp_path: Path,
) -> None:
    """A binding outlives the follower it was written for: the user quits Vim,
    or `claude-follow stop` runs. Recalling a window whose follower is gone
    would animate into whatever occupies that pane now."""
    _register_fake_follower("@19", "%24", shown_any=True)  # a live follower elsewhere
    target = _live_file(tmp_path)
    with (
        patch(
            "vim_ai_follower.tmux.subprocess.run",
            side_effect=_mock_tmux_run(pane_id="%24", vim_panes=()),
        ),
        patch("vim_ai_follower.hooks.binding.recall", return_value="@18") as recall,
        patch("vim_ai_follower.hooks.get_follower") as get_follower,
    ):
        assert hooks.cmd_hook_post(_LOST_ENV, _edit_payload(target, session_id=_SESSION_ID)) == 0
    assert recall.call_args_list == [((_SESSION_ID,), {})]
    assert get_follower.call_args_list == []
    assert _recovery_lines() == []
    assert len(_identity_warnings()) == 1


def test_no_recovery_when_there_is_nothing_remembered(tmp_path: Path) -> None:
    """A session blind from its very first hook never wrote a binding. It keeps
    today's behavior — the warning — and the feature adds nothing but a cheap
    file-miss to its cost."""
    _register_fake_follower("@18", "%23", shown_any=True)
    target = _live_file(tmp_path)
    with (
        patch("vim_ai_follower.tmux.subprocess.run", side_effect=_mock_tmux_run(pane_id="%23")),
        patch("vim_ai_follower.hooks.binding.recall", return_value=None),
        patch("vim_ai_follower.hooks.get_follower") as get_follower,
    ):
        assert hooks.cmd_hook_post(_LOST_ENV, _edit_payload(target, session_id=_SESSION_ID)) == 0
    assert get_follower.call_args_list == []
    assert len(_identity_warnings()) == 1


def test_a_blind_payload_without_a_session_id_never_recalls(tmp_path: Path) -> None:
    _register_fake_follower("@18", "%23", shown_any=True)
    target = _live_file(tmp_path)
    with (
        patch("vim_ai_follower.tmux.subprocess.run", side_effect=_mock_tmux_run(pane_id="%23")),
        patch("vim_ai_follower.hooks.binding") as binding,
        patch("vim_ai_follower.hooks.get_follower") as get_follower,
    ):
        assert hooks.cmd_hook_post(_LOST_ENV, _edit_payload(target)) == 0
    assert binding.recall.call_args_list == []
    assert get_follower.call_args_list == []
    assert len(_identity_warnings()) == 1


# --------------------------------------------------------------------------
# What the recovery must not cost
# --------------------------------------------------------------------------


def test_the_healthy_path_never_probes_a_follower_for_recovery() -> None:
    """FollowerState.get costs a real backend probe — a tmux shell-out or an
    nvim RPC connect — on every call. The recovery branch is the only place
    the helper may pay it; an in-tmux hook already knows its window."""
    with (
        patch("vim_ai_follower.tmux.subprocess.run", side_effect=_mock_tmux_run()),
        patch("vim_ai_follower.hooks.binding"),
        patch("vim_ai_follower.hooks.FollowerState") as follower_state,
    ):
        session = hooks._resolve_session_for({"TMUX_PANE": "%1"}, {"session_id": _SESSION_ID})
    assert session is not None
    assert session.window_id == "@1"
    assert follower_state.get.call_args_list == []


def test_a_blind_session_with_nothing_to_recall_never_probes_a_follower() -> None:
    """The steady state for a session that is blind from the start: one cheap
    missing-file read per hook, never a liveness probe per edit."""
    with (
        patch("vim_ai_follower.hooks.binding.recall", return_value=None),
        patch("vim_ai_follower.hooks.FollowerState") as follower_state,
    ):
        session = hooks._resolve_session_for(_LOST_ENV, {"session_id": _SESSION_ID})
    assert session is not None
    assert session.window_id == _LOST_IDENTITY
    assert follower_state.get.call_args_list == []


def test_an_unresolvable_session_stays_unresolvable() -> None:
    """resolve_session returns None when TMUX_PANE is set but the window
    cannot be read back. The helper must pass that through untouched — every
    entrypoint already exits 0 on it."""
    with (
        patch("vim_ai_follower.hooks.binding") as binding,
        patch("vim_ai_follower.session.TmuxWindow.from_env", return_value=None),
    ):
        assert hooks._resolve_session_for({"TMUX_PANE": "%1"}, {"session_id": _SESSION_ID}) is None
    assert binding.remember.call_args_list == []
    assert binding.recall.call_args_list == []
