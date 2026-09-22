"""The give-up path a lost window identity takes through the hooks.

Background (measured live 2026-09-22): a Claude Code session's tool execution
can be re-parented onto a `claude bg-spare` worker that carries neither
`$TMUX` nor `$TMUX_PANE`. `resolve_session` then returns a STANDALONE identity
(`term-<TERM_SESSION_ID>`) rather than None, no follower is registered under
that synthetic id, and every hook returned 0 in silence — the follower for the
real window stayed alive and enabled, edits kept flowing, and the hook log
stayed empty. A second session read that empty log as "the hook never fired".

These tests pin the diagnostic that replaces the silence, and — just as
important — pin that the diagnostic never turns into a GUESS about which
window was meant.
"""

from __future__ import annotations

import os
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from helpers import make_mock_tmux_run
from helpers import register_fake_follower as _register_fake_follower

from vim_ai_follower import hooks

# The environment a bg-spare-reparented hook really sees: TERM_SESSION_ID
# survives, TMUX and TMUX_PANE do not.
_LOST_ENV = {"TERM_SESSION_ID": "w0t0p0:87388AD3"}
_LOST_IDENTITY = "term-w0t0p0:87388AD3"


def _log_text() -> str:
    return hooks.LOG_PATH.read_text() if hooks.LOG_PATH.exists() else ""


def _warnings() -> list[str]:
    return [line for line in _log_text().splitlines() if "lost its tmux window identity" in line]


def _edit_payload(target: Path) -> dict[str, object]:
    return {"tool_name": "Edit", "tool_input": {"file_path": str(target)}}


def _live_file(tmp_path: Path) -> Path:
    target = tmp_path / "f.txt"
    target.write_text("content\n")
    return target


def test_warns_when_a_synthetic_identity_has_no_follower_but_another_window_does(
    tmp_path: Path,
) -> None:
    """The live incident, reproduced: follower alive under @18, hook resolving
    to the standalone id. The warning has to name all three facts a reader
    needs — the identity, that it is not in tmux, and where the live follower
    actually is — or the log is no more useful than the silence it replaces."""
    _register_fake_follower("@18", "%23")
    target = _live_file(tmp_path)
    with patch(
        "vim_ai_follower.tmux.subprocess.run", side_effect=make_mock_tmux_run(pane_id="%23")
    ):
        assert hooks.cmd_hook_post(_LOST_ENV, _edit_payload(target)) == 0
    warnings = _warnings()
    assert len(warnings) == 1
    assert _LOST_IDENTITY in warnings[0]
    assert "in_tmux=False" in warnings[0]
    assert "@18" in warnings[0]


def test_no_warning_when_the_hook_is_in_tmux_and_another_window_owns_the_follower(
    tmp_path: Path,
) -> None:
    """Alberto's ordinary layout: Claude running in several tmux windows with a
    follower in only one. Window @2 legitimately has no follower and must not
    log anything — this is the case that would otherwise turn the log into
    wallpaper, which is the same failure as saying nothing."""
    _register_fake_follower("@18", "%23")
    target = _live_file(tmp_path)
    with patch(
        "vim_ai_follower.tmux.subprocess.run",
        side_effect=make_mock_tmux_run(window_id="@2", pane_id="%23"),
    ):
        assert hooks.cmd_hook_post({"TMUX_PANE": "%7"}, _edit_payload(target)) == 0
    assert _warnings() == []


def test_no_warning_when_no_other_window_has_a_live_follower(tmp_path: Path) -> None:
    """A genuinely standalone run with no follower anywhere is not a lost
    identity — there is nothing to report and nothing was lost."""
    target = _live_file(tmp_path)
    with patch("vim_ai_follower.tmux.subprocess.run", side_effect=make_mock_tmux_run()):
        assert hooks.cmd_hook_post(_LOST_ENV, _edit_payload(target)) == 0
    assert _warnings() == []


def test_no_warning_when_a_follower_is_registered_for_this_very_identity(
    tmp_path: Path,
) -> None:
    """A standalone follower that IS registered under the synthetic id is the
    normal standalone-nvim case, not a lost identity."""
    _register_fake_follower(_LOST_IDENTITY, "%23")
    _register_fake_follower("@18", "%24")
    target = _live_file(tmp_path)
    with (
        patch("vim_ai_follower.tmux.subprocess.run", side_effect=make_mock_tmux_run(pane_id="%23")),
        patch("vim_ai_follower.hooks.get_follower", return_value=MagicMock()),
    ):
        assert hooks.cmd_hook_post(_LOST_ENV, _edit_payload(target)) == 0
    assert _warnings() == []


def test_the_read_navigation_give_up_path_warns_too(tmp_path: Path) -> None:
    """Read is the other hook that resolves a follower and silently returns 0.
    A fix that reconciles only the edit path would trade one silent give-up
    for another."""
    _register_fake_follower("@18", "%23")
    target = _live_file(tmp_path)
    payload: dict[str, object] = {"tool_name": "Read", "tool_input": {"file_path": str(target)}}
    with patch(
        "vim_ai_follower.tmux.subprocess.run", side_effect=make_mock_tmux_run(pane_id="%23")
    ):
        assert hooks.cmd_hook_post(_LOST_ENV, payload) == 0
    assert len(_warnings()) == 1


def test_throttle_suppresses_a_second_warning_inside_the_window(tmp_path: Path) -> None:
    _register_fake_follower("@18", "%23")
    target = _live_file(tmp_path)
    with patch(
        "vim_ai_follower.tmux.subprocess.run", side_effect=make_mock_tmux_run(pane_id="%23")
    ):
        hooks.cmd_hook_post(_LOST_ENV, _edit_payload(target))
        hooks.cmd_hook_post(_LOST_ENV, _edit_payload(target))
    assert len(_warnings()) == 1


def test_throttle_lets_the_warning_through_again_once_the_interval_has_passed(
    tmp_path: Path,
) -> None:
    """Backdates the real marker file rather than faking the clock, so the
    mtime arithmetic itself is what is under test."""
    _register_fake_follower("@18", "%23")
    target = _live_file(tmp_path)
    with patch(
        "vim_ai_follower.tmux.subprocess.run", side_effect=make_mock_tmux_run(pane_id="%23")
    ):
        hooks.cmd_hook_post(_LOST_ENV, _edit_payload(target))
        marker = hooks._identity_warning_marker(_LOST_IDENTITY)
        stale = time.time() - hooks.IDENTITY_WARN_INTERVAL_SECONDS - 1
        os.utime(marker, (stale, stale))
        hooks.cmd_hook_post(_LOST_ENV, _edit_payload(target))
    assert len(_warnings()) == 2


@pytest.mark.parametrize(
    ("backdate_seconds", "expected_warnings"),
    [(300, 1), (900, 2)],
)
def test_the_throttle_interval_sits_between_five_and_fifteen_minutes(
    tmp_path: Path, backdate_seconds: int, expected_warnings: int
) -> None:
    """Pins the interval against the wall clock, not against its own constant.

    The expiry test above backdates by IDENTITY_WARN_INTERVAL_SECONDS + 1, so
    it reads the very value it is checking and would keep passing if the
    interval were changed to a day. This one uses fixed real durations: quiet
    at five minutes, speaking again at fifteen."""
    _register_fake_follower("@18", "%23")
    target = _live_file(tmp_path)
    with patch(
        "vim_ai_follower.tmux.subprocess.run", side_effect=make_mock_tmux_run(pane_id="%23")
    ):
        hooks.cmd_hook_post(_LOST_ENV, _edit_payload(target))
        marker = hooks._identity_warning_marker(_LOST_IDENTITY)
        stale = time.time() - backdate_seconds
        os.utime(marker, (stale, stale))
        hooks.cmd_hook_post(_LOST_ENV, _edit_payload(target))
    assert len(_warnings()) == expected_warnings


def test_a_thirty_edit_session_logs_exactly_one_warning(tmp_path: Path) -> None:
    """The log-volume measurement, kept as a guard: each hook is its own
    process, so an unthrottled warning is one line per edit. Thirty edits is a
    perfectly ordinary Claude turn."""
    _register_fake_follower("@18", "%23")
    target = _live_file(tmp_path)
    with patch(
        "vim_ai_follower.tmux.subprocess.run", side_effect=make_mock_tmux_run(pane_id="%23")
    ):
        for _ in range(30):
            assert hooks.cmd_hook_post(_LOST_ENV, _edit_payload(target)) == 0
    assert len(_warnings()) == 1


def test_a_sole_other_follower_is_never_borrowed(tmp_path: Path) -> None:
    """The Part 3 decision, pinned as a guard.

    `$TMUX` was measured (2026-09-22) NOT to survive the bg-spare
    re-parenting, so there is no signal to recover the window from and no
    recovery shipped. Even the tempting case — exactly one live follower, so
    "obviously" the one meant — must not be animated into: a wrong inference
    here writes one project's file into another project's editor, which
    Alberto hit before and which is strictly worse than not animating. This
    test fails the moment someone adds a heuristic that picks a window.
    """
    _register_fake_follower("@18", "%23")
    target = _live_file(tmp_path)
    follower = MagicMock()
    with (
        patch("vim_ai_follower.tmux.subprocess.run", side_effect=make_mock_tmux_run(pane_id="%23")),
        patch("vim_ai_follower.hooks.get_follower", return_value=follower) as get_follower,
    ):
        assert hooks.cmd_hook_post(_LOST_ENV, _edit_payload(target)) == 0
    # Nothing was driven into @18's pane: no follower was built for the
    # animation, and the mock therefore recorded no navigation or typing.
    assert get_follower.call_args_list == []
    assert follower.method_calls == []
    assert len(_warnings()) == 1


def test_two_other_followers_are_reported_but_neither_is_chosen(tmp_path: Path) -> None:
    _register_fake_follower("@18", "%23")
    _register_fake_follower("@19", "%24")
    target = _live_file(tmp_path)
    with (
        patch(
            "vim_ai_follower.tmux.subprocess.run",
            side_effect=make_mock_tmux_run(pane_id="%23", vim_panes=("%24",)),
        ),
        patch("vim_ai_follower.hooks.get_follower") as get_follower,
    ):
        assert hooks.cmd_hook_post(_LOST_ENV, _edit_payload(target)) == 0
    assert get_follower.call_args_list == []
    warnings = _warnings()
    assert len(warnings) == 1
    assert "@18" in warnings[0]
    assert "@19" in warnings[0]


def test_the_warning_never_reaches_stdout_or_stderr(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A hook's stdout is a protocol channel Claude Code parses, and its
    stderr is surfaced to the user. The diagnostic belongs in the log file
    only, and the hook still exits 0."""
    _register_fake_follower("@18", "%23")
    target = _live_file(tmp_path)
    with patch(
        "vim_ai_follower.tmux.subprocess.run", side_effect=make_mock_tmux_run(pane_id="%23")
    ):
        assert hooks.cmd_hook_post(_LOST_ENV, _edit_payload(target)) == 0
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == ""
    assert len(_warnings()) == 1
