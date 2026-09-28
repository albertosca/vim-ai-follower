"""The hand-off cue asks for `:w!` exactly when the buffer the user now holds
is readonly by their OWN setting (Alberto's decision, 2026-09-28): a plain
`:w` fails there with E45 and would leave Claude blocked behind a cue that
tells the user the wrong thing. Which buffers count is the backend's call
(Follower.user_readonly); hooks only picks the cue from the answer, once per
hand-off cycle, and falls back to the plain cue if asking fails."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from vim_ai_follower import hooks, state
from vim_ai_follower.hooks import HANDOFF_CUE, HANDOFF_CUE_READONLY
from vim_ai_follower.session import Session


def test_the_cues_differ_only_in_the_bang() -> None:
    assert HANDOFF_CUE.replace(":w ", ":w! ") == HANDOFF_CUE_READONLY


@pytest.mark.parametrize(("readonly", "cue"), [(True, HANDOFF_CUE_READONLY), (False, HANDOFF_CUE)])
def test_the_cue_follows_the_backends_answer(readonly: bool, cue: str) -> None:
    follower = MagicMock()
    follower.user_readonly.return_value = readonly
    assert hooks._handoff_cue(follower, "/tmp/f.py") == cue
    follower.user_readonly.assert_called_once_with("/tmp/f.py")


def test_a_failure_to_ask_is_the_plain_cue(caplog: pytest.LogCaptureFixture) -> None:
    follower = MagicMock()
    follower.user_readonly.side_effect = OSError("gone")
    assert hooks._handoff_cue(follower, "/tmp/f.py") == HANDOFF_CUE
    assert "could not ask whether /tmp/f.py is readonly" in caplog.text


@pytest.mark.parametrize(("readonly", "cue"), [(True, HANDOFF_CUE_READONLY), (False, HANDOFF_CUE)])
def test_the_hand_off_wait_shows_the_cue_for_its_buffer(
    tmp_path: Path, readonly: bool, cue: str
) -> None:
    target = tmp_path / "f.py"
    target.write_text("after\n")
    state.FollowerState.set("@1", "nvim", "/tmp/x.sock", adopted=True)
    current = state.FollowerState.read("@1")
    assert current is not None
    session = Session(window_id="@1", origin=None, in_tmux=False)
    follower = MagicMock()
    follower.user_readonly.return_value = readonly
    surface = MagicMock()

    def _released(*args: object) -> bool:
        return False  # the user saved: the wait is over

    with (
        patch("vim_ai_follower.hooks.get_follower", return_value=follower),
        patch("vim_ai_follower.hooks._poll_until_interrupt", side_effect=_released) as poll,
    ):
        hooks._await_user_handoff(current, session, str(target), "after\n", "", surface)

    surface.set_state.assert_called_once_with(cue)
    assert poll.call_args.args[-1] == cue  # the ~30 s reminder popup repeats it
