"""The typing loop's cursor follows the FOLLOWER's window, never the user's.

T24a review m6, measured 2026-10-18 on a headless nvim with a second RPC
connection playing the user: pause an animation (an adopted nvim is the user's
own editor, and a pause is when they take it back), move to another tab or
split, resume. The text always landed in the follower's buffer (every write is
by buffer handle), but `win_set_cursor(0, …)` addressed the CURRENT window, so
the user's cursor jumped from (7, 3) to (6, 10) — the follower's typing
position applied to their own file — while the follower's window kept its
pre-pause cursor. The loop now finds the window showing the follower's buffer
by handle (the one it used, else the current one, else the current tab's, else
any tab's) and moves only that window's cursor.
"""

from __future__ import annotations

import threading
from collections.abc import Callable
from pathlib import Path

import pytest

pynvim = pytest.importorskip("pynvim")

from vim_ai_follower import cache, control, state  # noqa: E402
from vim_ai_follower.animate import AnimationResult  # noqa: E402
from vim_ai_follower.backends import nvim as nvim_backend  # noqa: E402
from vim_ai_follower.backends.nvim import NvimFollower, _animate_lines  # noqa: E402
from vim_ai_follower.control import PendingShowFresh  # noqa: E402
from vim_ai_follower.diff import compute_edit_script  # noqa: E402

pytestmark = pytest.mark.integration

_WINDOW_ID = "@pause-cursor"
_LINES = tuple(f"typed line {number} with some text" for number in range(6))


class _PausedAnimation:
    """An animation typing into the follower's buffer from its own connection,
    paused once its first line is on screen; `user` is a second connection."""

    def __init__(self, socket_path: str, base_dir: Path, follower_file: Path) -> None:
        self.socket_path = socket_path
        self.base_dir = base_dir
        self.user = pynvim.attach("socket", path=socket_path)
        self.user.command(f"edit {follower_file}")
        self.follower_buffer = self.user.api.get_current_buf().handle
        self.follower_window = self.user.api.get_current_win().handle
        self.result: list[AnimationResult] = []
        self._thread = threading.Thread(target=self._drive)

    def _drive(self) -> None:
        follower = pynvim.attach("socket", path=self.socket_path)
        try:
            namespace = follower.api.create_namespace("pause-cursor")
            self.result.append(
                _animate_lines(
                    follower,
                    self.follower_buffer,
                    _LINES,
                    0,
                    lambda: 0.02,
                    _WINDOW_ID,
                    namespace,
                    self.base_dir,
                )
            )
        finally:
            follower.close()

    def start_and_pause(self, wait_until: Callable[..., bool]) -> None:
        control.mark_animating(_WINDOW_ID, self.base_dir)
        self._thread.start()
        assert wait_until(
            lambda: self.user.api.buf_get_lines(self.follower_buffer, 0, 1, True)[0] != "",
            timeout=10.0,
            interval=0.02,
        ), "the animation never typed"
        assert control.request_pause(_WINDOW_ID, self.base_dir)
        assert wait_until(
            lambda: control.animating_state(_WINDOW_ID, self.base_dir) == "paused",
            timeout=10.0,
            interval=0.02,
        ), "the pause never landed"

    def resume_and_finish(self) -> None:
        assert control.request_pause(_WINDOW_ID, self.base_dir)
        self._thread.join(timeout=30.0)
        assert not self._thread.is_alive(), "the animation never finished"
        assert self.result == [AnimationResult("completed", len(_LINES))]


@pytest.fixture
def user_file(tmp_path: Path) -> Path:
    path = tmp_path / "user.txt"
    path.write_text("".join(f"user line {number}\n" for number in range(40)))
    return path


@pytest.mark.parametrize("where", ["another tab", "a split"])
def test_a_resumed_animation_leaves_the_users_cursor_alone(
    headless_nvim: str,
    tmp_path: Path,
    user_file: Path,
    wait_until: Callable[..., bool],
    where: str,
) -> None:
    animation = _PausedAnimation(headless_nvim, tmp_path, tmp_path / "follower.txt")
    user = animation.user
    if where == "another tab":
        user.command(f"tabnew {user_file}")
        user_window = user.api.get_current_win().handle
        user.command("tabfirst")
    animation.start_and_pause(wait_until)
    if where == "another tab":
        user.api.set_current_win(user_window)
    else:
        user.command(f"split {user_file}")
        user_window = user.api.get_current_win().handle
    user.api.win_set_cursor(user_window, [7, 3])

    animation.resume_and_finish()

    assert user.api.get_current_win().handle == user_window
    assert user.api.win_get_cursor(user_window) == [7, 3], "the follower moved the user's cursor"
    assert user.api.buf_get_lines(animation.follower_buffer, 0, -1, True) == [*_LINES, ""]
    assert user.api.buf_get_lines(user.api.win_get_buf(user_window), 0, 1, True) == ["user line 0"]
    # The cursor the loop moves is the follower's own: it ends on the last
    # typed character (Normal mode clamps the past-the-end column to it).
    assert user.api.win_get_cursor(animation.follower_window) == [
        len(_LINES),
        len(_LINES[-1]) - 1,
    ]
    user.close()


def test_a_user_buffer_put_in_the_followers_window_keeps_its_cursor(
    headless_nvim: str,
    tmp_path: Path,
    user_file: Path,
    wait_until: Callable[..., bool],
) -> None:
    # The follower's buffer no longer shows anywhere: there is no window
    # whose cursor is the follower's, so none moves.
    animation = _PausedAnimation(headless_nvim, tmp_path, tmp_path / "follower.txt")
    user = animation.user
    animation.start_and_pause(wait_until)
    user.command(f"edit {user_file}")  # the follower's buffer is hidden
    user.api.win_set_cursor(0, [7, 3])

    animation.resume_and_finish()

    assert user.api.get_current_win().handle == animation.follower_window
    assert user.api.win_get_cursor(0) == [7, 3], "the follower moved the user's cursor"
    assert user.api.buf_get_lines(animation.follower_buffer, 0, -1, True) == [*_LINES, ""]
    user.close()


def test_the_followers_buffer_moved_into_the_users_window_keeps_their_cursor(
    headless_nvim: str,
    tmp_path: Path,
    wait_until: Callable[..., bool],
) -> None:
    # T24c review m2: during the pause the user shows the follower's buffer
    # in a window of their own and closes the follower's window. The buffer
    # still shows, but only in the USER's current window; an adopted follower
    # moves no cursor but its own, so it types by buffer handle alone.
    animation = _PausedAnimation(headless_nvim, tmp_path, tmp_path / "follower.txt")
    user = animation.user
    animation.start_and_pause(wait_until)
    user.command("tabnew")
    user_window = user.api.get_current_win().handle
    user.api.win_set_buf(user_window, animation.follower_buffer)
    user.api.win_close(animation.follower_window, True)
    user.api.win_set_cursor(user_window, [1, 0])

    animation.resume_and_finish()

    assert user.api.get_current_win().handle == user_window
    assert user.api.win_get_cursor(user_window) == [1, 0], "the follower moved the user's cursor"
    assert user.api.buf_get_lines(animation.follower_buffer, 0, -1, True) == [*_LINES, ""]
    user.close()


@pytest.mark.parametrize("adopted", [False, True])
def test_every_entry_point_types_with_the_followers_own_kind(
    headless_nvim: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    adopted: bool,
) -> None:
    # Only a dedicated follower may fall back to another window showing its
    # buffer; show_fresh, apply_edit and resume each say which one it is.
    monkeypatch.setattr(cache, "CACHE_DIR", tmp_path / "cache")
    state.FollowerState.set("@1", "nvim", headless_nvim, adopted=adopted)
    follower = NvimFollower(socket_path=headless_nvim, window_id="@1", pace_seconds=0.0)
    real_animate = nvim_backend._animate_lines
    seen: list[bool] = []

    def recording(*args: object, **kwargs: object) -> AnimationResult:
        seen.append(bool(kwargs["dedicated"]))
        return real_animate(*args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(nvim_backend, "_animate_lines", recording)
    target = tmp_path / "kind.py"
    target.write_text("one\n")
    assert follower.show_fresh(str(target), "one\n").outcome == "completed"
    ops = compute_edit_script("one\n", "one\ntwo\n")
    assert follower.apply_edit(str(target), ops, before="one\n").outcome == "completed"
    pending = PendingShowFresh(
        lines=("three",), pace_seconds=0.0, continuation=True, file_path=str(target)
    )
    assert follower.resume(pending, reload=False).outcome == "completed"

    assert seen == [not adopted] * 3


_TWO_HUNK_BEFORE = "".join(f"line {number}\n" for number in range(20))
_TWO_HUNK_AFTER_LINES = [f"line {number}" for number in range(20)]
_TWO_HUNK_AFTER_LINES[15:16] = [f"NEWB {number} some typed text here" for number in range(3)]
_TWO_HUNK_AFTER_LINES[3:4] = [f"NEWA {number} some typed text here" for number in range(3)]
_TWO_HUNK_AFTER = "".join(line + "\n" for line in _TWO_HUNK_AFTER_LINES)


@pytest.mark.parametrize("scenario", ["move and close", "split"])
def test_a_pause_in_one_op_never_hands_the_next_op_the_users_window(
    headless_nvim: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    wait_until: Callable[..., bool],
    scenario: str,
) -> None:
    # T24c re-review m2': an adopted Edit with two typed hunks, paused in the
    # first one typed (the bottom one). Each op used to record "the window
    # current now" as the follower's own, so the second op took the window
    # the user had left current and dragged its cursor ([1, 0] -> [6, 26]).
    monkeypatch.setattr(cache, "CACHE_DIR", tmp_path / "cache")
    state.FollowerState.set(_WINDOW_ID, "nvim", headless_nvim, adopted=True)
    target = tmp_path / "two_hunks.py"
    target.write_text(_TWO_HUNK_BEFORE)
    quick = NvimFollower(socket_path=headless_nvim, window_id=_WINDOW_ID, pace_seconds=0.0)
    assert quick.show_fresh(str(target), _TWO_HUNK_BEFORE).outcome == "completed"
    user = pynvim.attach("socket", path=headless_nvim)
    follower_window = user.api.get_current_win().handle
    follower_buffer = user.api.get_current_buf().handle
    target.write_text(_TWO_HUNK_AFTER)
    ops = compute_edit_script(_TWO_HUNK_BEFORE, _TWO_HUNK_AFTER)
    assert len(ops) == 2
    follower = NvimFollower(socket_path=headless_nvim, window_id=_WINDOW_ID, pace_seconds=0.03)
    results: list[AnimationResult] = []
    control.mark_animating(_WINDOW_ID, tmp_path / "cache")
    thread = threading.Thread(
        target=lambda: results.append(
            follower.apply_edit(str(target), ops, before=_TWO_HUNK_BEFORE)
        )
    )
    thread.start()
    try:
        assert wait_until(
            lambda: any(
                line.startswith("NEWB 0 s")
                for line in user.api.buf_get_lines(follower_buffer, 0, -1, True)
            ),
            timeout=10.0,
            interval=0.02,
        ), "the first op never typed"
        assert control.request_pause(_WINDOW_ID)
        assert wait_until(
            lambda: control.animating_state(_WINDOW_ID) == "paused",
            timeout=10.0,
            interval=0.02,
        ), "the pause never landed"
        if scenario == "move and close":
            user.command("tabnew")
            user_window = user.api.get_current_win().handle
            user.api.win_set_buf(user_window, follower_buffer)
            user.api.win_close(follower_window, True)
        else:
            user.command("split")
            user_window = user.api.get_current_win().handle
        user.api.win_set_cursor(user_window, [1, 0])
    finally:
        assert control.request_pause(_WINDOW_ID)
        thread.join(timeout=60.0)
    assert not thread.is_alive(), "the animation never finished"
    assert results == [AnimationResult("completed", 2)]

    assert user.api.get_current_win().handle == user_window
    assert user.api.win_get_cursor(user_window) == [1, 0], "the follower moved the user's cursor"
    assert user.api.buf_get_lines(follower_buffer, 0, -1, True) == _TWO_HUNK_AFTER_LINES
    if scenario == "split":
        # The follower's own window kept following into the second op.
        assert user.api.win_get_cursor(follower_window) == [6, len(_TWO_HUNK_AFTER_LINES[5]) - 1]
    user.close()
