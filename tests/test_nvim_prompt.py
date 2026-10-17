"""Unit coverage for nvim_prompt: the rules of the hit-enter sweep + watchdog
that keep a plugin message from freezing a dedicated nvim follower. The
real-nvim proof (a UI nvim in a private tmux) is
test_nvim_integration_hit_enter.py; here each rule is pinned on its own."""

from __future__ import annotations

import contextlib
import errno
import logging
import os
import threading
import time
from typing import Any
from unittest.mock import MagicMock, call, patch

import pytest

from vim_ai_follower import state
from vim_ai_follower.backends import nvim_prompt
from vim_ai_follower.backends.nvim import NvimFollower
from vim_ai_follower.backends.nvim_prompt import (
    WATCHDOG_THREAD_NAME,
    close_connection,
    dismiss_prompt,
    exec_logged,
    prompt_guard,
)

_PROMPT = {"mode": "r", "blocking": True}
_IDLE = {"mode": "n", "blocking": False}


def _nvim(*modes: dict[str, Any]) -> MagicMock:
    """A mock connection whose get_mode answers `modes` in turn, then idle. A
    prompt that gets answered is read twice: once to see it, once more under
    the answer lock."""
    nvim = MagicMock()
    answers = iter(modes)
    nvim.api.get_mode.side_effect = lambda: next(answers, _IDLE)
    nvim.api.exec2.return_value = {"output": ""}
    return nvim


def _watchdogs_alive() -> list[threading.Thread]:
    return [t for t in threading.enumerate() if t.name == WATCHDOG_THREAD_NAME]


def _wait_for(predicate: Any, timeout: float = 3.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return False


# --- dismiss_prompt: only a blocking "r" ----------------------------------


def test_a_blocking_hit_enter_prompt_is_answered_with_enter() -> None:
    nvim = _nvim(_PROMPT, _PROMPT)
    assert dismiss_prompt(nvim, "/s") == _PROMPT
    nvim.api.input.assert_called_once_with("<CR>")


def test_a_blocking_more_prompt_is_quit_with_esc() -> None:
    # A message taller than the screen pages with the more-prompt: it offers
    # no choice, and Enter would only advance it one line, so <Esc> ends it.
    # Never `q`: a spare `q` starts macro recording and blocks nvim (see
    # test_nvim_integration_hit_enter.py's spare-answer tests).
    more = {"mode": "rm", "blocking": True}
    nvim = _nvim(more, more)
    assert dismiss_prompt(nvim, "/s") == more
    nvim.api.input.assert_called_once_with("<Esc>")


@pytest.mark.parametrize(
    "mode",
    [
        {"mode": "r?", "blocking": True},  # a confirm prompt: Enter would pick a choice
        {"mode": "n", "blocking": True},  # blocked, but not on a prompt (pending keys)
        {"mode": "r", "blocking": False},
        {"mode": "rm", "blocking": False},
        _IDLE,
    ],
)
def test_anything_but_a_blocking_r_or_rm_is_left_alone(mode: dict[str, Any]) -> None:
    nvim = _nvim(mode)
    assert dismiss_prompt(nvim, "/s") is None
    nvim.api.input.assert_not_called()


def test_after_answering_it_waits_for_the_prompt_to_go() -> None:
    # Returning while nvim has not yet processed the key would let the next
    # poll see the same prompt and send a spare key into normal mode.
    nvim = _nvim(_PROMPT, _PROMPT, _PROMPT, _PROMPT)
    assert dismiss_prompt(nvim, "/s") == _PROMPT
    nvim.api.input.assert_called_once_with("<CR>")
    # seen, re-read under the answer lock, 2 still up, then gone
    assert nvim.api.get_mode.call_count == 5


def test_a_prompt_that_stays_up_is_answered_once_per_settle_window() -> None:
    nvim = MagicMock()
    nvim.api.get_mode.return_value = _PROMPT
    started = time.monotonic()
    with patch.object(nvim_prompt, "_SETTLE_SECONDS", 0.1):
        assert dismiss_prompt(nvim, "/s") == _PROMPT
    assert time.monotonic() - started >= 0.1
    nvim.api.input.assert_called_once_with("<CR>")


def test_the_watchdog_never_sends_a_second_key_while_nvim_is_still_on_the_first() -> None:
    # A slow nvim: the prompt stays up after the watchdog's <CR>. Within the
    # settle window no second <CR> may follow it.
    dog = MagicMock()
    dog.api.get_mode.return_value = _PROMPT
    with (
        patch.object(nvim_prompt, "_SETTLE_SECONDS", 0.4),
        prompt_guard(_nvim(), lambda: dog, key="/s", adopted=False, label="x"),
    ):
        assert _wait_for(lambda: dog.api.input.called)
        time.sleep(0.2)
    dog.api.input.assert_called_once_with("<CR>")


# --- the cross-process answer lock ------------------------------------------


def test_a_busy_answer_lock_means_no_key() -> None:
    # Another guard (thread or process) is answering this socket's prompt:
    # a second answer would land spare, in normal mode, through the user's
    # mappings. Not answering is safe — the next poll looks again.
    nvim = _nvim(_PROMPT, _PROMPT)
    with patch("vim_ai_follower.backends.nvim_prompt.fcntl.flock", side_effect=BlockingIOError()):
        assert dismiss_prompt(nvim, "/s") is None
    nvim.api.input.assert_not_called()


def test_a_real_second_holder_blocks_the_answer() -> None:
    nvim = _nvim(_PROMPT, _PROMPT)
    with nvim_prompt._answer_lock("/s") as first:
        assert first is True
        assert dismiss_prompt(nvim, "/s") is None
    nvim.api.input.assert_not_called()


def test_a_guard_that_gets_the_lock_after_the_answer_sends_nothing() -> None:
    # It saw the prompt, but by the time it holds the lock the other guard's
    # answer has landed: the re-read under the lock finds it gone.
    nvim = _nvim(_PROMPT, _IDLE)
    assert dismiss_prompt(nvim, "/s") is None
    nvim.api.input.assert_not_called()


def test_an_unusable_lock_file_still_answers_and_logs_once(
    caplog: pytest.LogCaptureFixture,
) -> None:
    # EACCES on the lock file (a 0400 file left by `sudo`, a read-only
    # answer-locks/). Not answering would bring the freeze right back: the
    # hook hangs on the prompt forever. So it answers, and says so once.
    # seen + re-read, answered (idle), then the same for a second prompt
    nvim = _nvim(_PROMPT, _PROMPT, _IDLE, _PROMPT, _PROMPT)
    denied = OSError(errno.EACCES, "Permission denied")
    with (
        caplog.at_level(logging.WARNING, logger="vim_ai_follower"),
        patch("vim_ai_follower.backends.nvim_prompt.os.open", side_effect=denied),
    ):
        assert dismiss_prompt(nvim, "/ro") == _PROMPT
        assert dismiss_prompt(nvim, "/ro") == _PROMPT
    assert nvim.api.input.call_args_list == [call("<CR>"), call("<CR>")]
    assert caplog.text.count("answer lock unusable") == 1


def test_a_filesystem_without_flock_still_answers_and_closes_the_file() -> None:
    # ENOLCK: the file opened, flock itself is not supported there.
    nvim = _nvim(_PROMPT, _PROMPT)
    closed: list[int] = []
    real_close = os.close

    def recording_close(descriptor: int) -> None:
        closed.append(descriptor)
        real_close(descriptor)

    with (
        patch(
            "vim_ai_follower.backends.nvim_prompt.fcntl.flock",
            side_effect=OSError(errno.ENOLCK, "No locks available"),
        ),
        patch(
            "vim_ai_follower.backends.nvim_prompt.os.close",
            side_effect=recording_close,
        ),
    ):
        assert dismiss_prompt(nvim, "/nolck") == _PROMPT
    nvim.api.input.assert_called_once_with("<CR>")
    assert len(closed) == 1


def test_an_unusable_lock_still_lets_only_one_guard_in_the_process_answer() -> None:
    # Without the file lock, an in-process lock keyed by socket keeps two
    # guards of ONE process (the sweep and a watchdog) from both answering.
    nvim = _nvim(_PROMPT, _PROMPT)
    with patch(
        "vim_ai_follower.backends.nvim_prompt.os.open",
        side_effect=OSError(errno.EACCES, "Permission denied"),
    ):
        with nvim_prompt._answer_lock("/ro-busy") as first:
            assert first is True
            assert dismiss_prompt(nvim, "/ro-busy") is None
        nvim.api.input.assert_not_called()
        # Released on exit: the next guard can answer.
        assert dismiss_prompt(_nvim(_PROMPT, _PROMPT), "/ro-busy") == _PROMPT


def test_the_answer_lock_is_released_after_answering_and_after_a_raise() -> None:
    assert dismiss_prompt(_nvim(_PROMPT, _PROMPT), "/s") == _PROMPT
    with nvim_prompt._answer_lock("/s") as held:
        assert held is True
    boom = _nvim(_PROMPT, _PROMPT)
    boom.api.input.side_effect = OSError("socket gone")
    with pytest.raises(OSError, match="socket gone"):
        dismiss_prompt(boom, "/s")
    with nvim_prompt._answer_lock("/s") as held:
        assert held is True


def test_answer_locks_are_per_socket() -> None:
    with nvim_prompt._answer_lock("/one") as one, nvim_prompt._answer_lock("/two") as two:
        assert (one, two) == (True, True)


def test_the_lock_file_is_opened_read_only() -> None:
    # flock needs no write access, and read-only keeps a same-owner 0400 file
    # usable (measured in sweep 3).
    opened_flags: list[int] = []
    real_open = os.open

    def recording_open(path: Any, flags: int, mode: int = 0o777) -> int:
        opened_flags.append(flags)
        return real_open(path, flags, mode)

    with (
        patch("vim_ai_follower.backends.nvim_prompt.os.open", recording_open),
        nvim_prompt._answer_lock("/s") as held,
    ):
        assert held
    assert opened_flags
    assert opened_flags[0] & os.O_ACCMODE == os.O_RDONLY


def test_taking_the_lock_refreshes_its_mtime() -> None:
    path = nvim_prompt._answer_lock_path("/s")
    path.parent.mkdir(parents=True)
    path.touch()
    os.utime(path, (0, 0))
    with nvim_prompt._answer_lock("/s"):
        pass
    assert time.time() - path.stat().st_mtime < 60


def test_the_sweep_removes_only_old_locks() -> None:
    old = nvim_prompt._answer_lock_path("/old")
    fresh = nvim_prompt._answer_lock_path("/fresh")
    old.parent.mkdir(parents=True)
    old.touch()
    fresh.touch()
    os.utime(old, (0, 0))
    assert nvim_prompt.sweep_answer_locks() == 1
    assert not old.exists()
    assert fresh.exists()


def test_the_sweep_with_no_lock_directory_removes_nothing() -> None:
    assert nvim_prompt.sweep_answer_locks() == 0


def test_the_sweep_skips_a_lock_it_cannot_remove() -> None:
    old = nvim_prompt._answer_lock_path("/old")
    old.parent.mkdir(parents=True)
    old.touch()
    os.utime(old, (0, 0))
    with patch("pathlib.Path.unlink", side_effect=PermissionError(errno.EPERM, "denied")):
        assert nvim_prompt.sweep_answer_locks() == 0


def test_two_spellings_of_one_socket_share_one_lock(tmp_path: Any) -> None:
    real = tmp_path / "real"
    real.mkdir()
    (tmp_path / "link").symlink_to(real)
    assert nvim_prompt._answer_lock_path(str(real / "n.sock")) == nvim_prompt._answer_lock_path(
        str(tmp_path / "link" / "n.sock")
    )


def test_a_non_utf8_socket_path_does_not_raise(tmp_path: Any) -> None:
    weird = os.fsdecode(os.fsencode(tmp_path) + b"/\xff.sock")
    with nvim_prompt._answer_lock(weird) as held:
        assert held is True


def test_the_log_once_set_is_guarded() -> None:
    # The check-then-add on the log-once set runs INSIDE the guard: the
    # recording lock sees the socket absent on entry and present on exit.
    # A guard that exists but is not used records nothing.
    seen: list[tuple[str, bool]] = []
    logged: set[str] = set()

    class RecordingLock:
        def __enter__(self) -> None:
            seen.append(("enter", "/guarded" in logged))

        def __exit__(self, *exc_info: object) -> None:
            seen.append(("exit", "/guarded" in logged))

    with (
        patch.object(nvim_prompt, "_lock_errors_guard", RecordingLock()),
        patch.object(nvim_prompt, "_lock_errors_logged", logged),
        patch(
            "vim_ai_follower.backends.nvim_prompt.os.open",
            side_effect=OSError(errno.EACCES, "Permission denied"),
        ),
        nvim_prompt._answer_lock("/guarded") as held,
    ):
        assert held is True
    assert seen == [("enter", False), ("exit", True)]


# --- prompt_guard -------------------------------------------------------------


def test_a_non_acting_adopted_guard_gets_no_sweep_and_no_watchdog() -> None:
    # The status float in the user's own editor: it does not act on a buffer
    # (the tmux cue is a border and sends nothing into Vim), so the user's
    # prompts stay theirs to read.
    nvim = _nvim(_PROMPT, _PROMPT)
    connect = MagicMock()
    with prompt_guard(nvim, connect, key="/s", adopted=True, label="x", acting=False):
        pass
    nvim.api.get_mode.assert_not_called()
    nvim.api.input.assert_not_called()
    connect.assert_not_called()


def test_an_acting_adopted_guard_sweeps_and_watches(caplog: pytest.LogCaptureFixture) -> None:
    # The counterpart of the tmux backend's Escape pair: when the follower is
    # about to act in the user's own editor, a hit-enter prompt left up is
    # dismissed, and one raised while it acts is answered by the watchdog.
    main = _nvim(_PROMPT, _PROMPT)
    dog = _nvim(_IDLE, _PROMPT, _PROMPT)
    with (
        caplog.at_level(logging.WARNING, logger="vim_ai_follower"),
        prompt_guard(main, lambda: dog, key="/s", adopted=True, label="apply_edit"),
    ):
        main.api.input.assert_called_once_with("<CR>")  # the sweep, before the body
        assert _wait_for(lambda: dog.api.input.called)
    dog.api.input.assert_called_once_with("<CR>")
    assert "left before apply_edit (sweep)" in caplog.text
    assert "during apply_edit (watchdog)" in caplog.text


def test_an_acting_guard_is_the_default() -> None:
    main = _nvim(_PROMPT, _PROMPT)
    with prompt_guard(main, lambda: _nvim(), key="/s", adopted=True, label="x"):
        pass
    main.api.input.assert_called_once_with("<CR>")


@pytest.mark.parametrize(
    "prompt", [{"mode": "r?", "blocking": False}, {"mode": "c", "blocking": False}]
)
def test_an_acting_adopted_guard_never_answers_a_confirm_or_an_input(
    prompt: dict[str, Any],
) -> None:
    # What nvim 0.12 answers at confirm() and input() (measured 2026-10-17):
    # a choice the user is making, never ours to make.
    main = _nvim(prompt, prompt)
    dog = MagicMock()
    dog.api.get_mode.return_value = prompt
    with prompt_guard(main, lambda: dog, key="/s", adopted=True, label="x"):
        assert _wait_for(lambda: dog.api.get_mode.call_count >= 3)
    main.api.input.assert_not_called()
    dog.api.input.assert_not_called()


def test_an_adopted_watchdog_answers_nothing_while_paused_then_answers_on_resume() -> None:
    # A pause in the user's own editor: the Vim follower sends nothing while
    # paused, and the user may be reading their own `:ls`.
    # The pause is entered before the guard, so the watchdog's very first
    # poll already sees it (the sweep goes to the follower's own connection,
    # which shows no prompt).
    dog = MagicMock()
    dog.api.get_mode.return_value = _PROMPT
    with contextlib.ExitStack() as pause:
        pause.enter_context(nvim_prompt.paused("/s"))
        with (
            patch.object(nvim_prompt, "_SETTLE_SECONDS", 0.05),
            prompt_guard(_nvim(), lambda: dog, key="/s", adopted=True, label="x"),
        ):
            time.sleep(0.3)
            dog.api.input.assert_not_called()
            pause.close()
            assert nvim_prompt.is_paused("/s") is False
            assert _wait_for(lambda: dog.api.input.called)
    dog.api.input.assert_any_call("<CR>")


def test_a_dedicated_watchdog_answers_even_while_paused() -> None:
    # Its prompts are never the user's (sweep-3 behaviour, unchanged).
    dog = _nvim(_IDLE, _PROMPT, _PROMPT)
    with (
        prompt_guard(_nvim(), lambda: dog, key="/s", adopted=False, label="x"),
        nvim_prompt.paused("/s"),
    ):
        assert _wait_for(lambda: dog.api.input.called)
    dog.api.input.assert_called_once_with("<CR>")


def test_a_pause_is_per_socket_and_ends_with_its_block() -> None:
    with nvim_prompt.paused("/a"):
        assert nvim_prompt.is_paused("/a") is True
        assert nvim_prompt.is_paused("/b") is False
    assert nvim_prompt.is_paused("/a") is False


def test_a_pause_ends_even_when_its_body_raises() -> None:
    with pytest.raises(ValueError, match="boom"), nvim_prompt.paused("/a"):
        raise ValueError("boom")
    assert nvim_prompt.is_paused("/a") is False


# --- sweep_socket: the hook's sweep before it raises the cue -----------------


def test_sweep_socket_answers_a_prompt_logs_the_tail_and_closes(
    caplog: pytest.LogCaptureFixture,
) -> None:
    nvim = _nvim(_PROMPT, _PROMPT)
    nvim.api.exec2.return_value = {"output": "one\ntwo\n"}
    dog = _nvim()
    connections = iter([nvim, dog])
    with caplog.at_level(logging.WARNING, logger="vim_ai_follower"):
        assert nvim_prompt.sweep_socket(lambda: next(connections), "/s", "the Edit hook") == _PROMPT
    nvim.api.input.assert_called_once_with("<CR>")
    nvim.close.assert_called_once_with()
    dog.api.input.assert_not_called()
    assert "dismissed a hit-enter prompt left before the Edit hook (sweep)" in caplog.text
    assert ":messages after the Edit hook: one | two" in caplog.text


def test_sweep_socket_answers_a_prompt_queued_behind_the_one_it_answered() -> None:
    # Measured with a real nvim whose FileType callback ran twice: the second
    # deferred message raised its prompt as soon as the sweep answered the
    # first, and the `:messages` read waited behind it. The read runs under a
    # watchdog, which answers it.
    nvim = _nvim(_PROMPT, _PROMPT)
    dog = MagicMock()
    dog.api.get_mode.side_effect = [_PROMPT, _PROMPT] + [_IDLE] * 1000

    def read_behind_the_second_prompt(*_arguments: Any) -> dict[str, str]:
        assert _wait_for(lambda: dog.api.input.called)
        return {"output": "first\nsecond\n"}

    nvim.api.exec2.side_effect = read_behind_the_second_prompt
    connections = iter([nvim, dog])
    assert nvim_prompt.sweep_socket(lambda: next(connections), "/s", "the hook") == _PROMPT
    nvim.api.input.assert_called_once_with("<CR>")
    dog.api.input.assert_called_once_with("<CR>")


def test_sweep_socket_with_nothing_up_sends_nothing_and_closes(
    caplog: pytest.LogCaptureFixture,
) -> None:
    nvim = _nvim()
    with caplog.at_level(logging.WARNING, logger="vim_ai_follower"):
        assert nvim_prompt.sweep_socket(lambda: nvim, "/s", "x") is None
    nvim.api.input.assert_not_called()
    nvim.api.exec2.assert_not_called()
    nvim.close.assert_called_once_with()
    assert caplog.records == []


def test_answerable_names_only_the_hit_enter_and_more_prompts() -> None:
    assert nvim_prompt.answerable("r") is True
    assert nvim_prompt.answerable("rm") is True
    for mode in ("r?", "c", "n", "i", None):
        assert nvim_prompt.answerable(mode) is False


def test_the_sweep_dismisses_a_leftover_prompt_and_logs_the_messages_tail(
    caplog: pytest.LogCaptureFixture,
) -> None:
    nvim = _nvim(_PROMPT, _PROMPT)
    nvim.api.exec2.return_value = {"output": "one\ntwo\nruff failed: -86\n"}
    with (
        caplog.at_level(logging.WARNING, logger="vim_ai_follower"),
        prompt_guard(nvim, lambda: _nvim(), key="/s", adopted=False, label="show_fresh"),
    ):
        nvim.api.input.assert_called_once_with("<CR>")  # before the body runs
    nvim.api.exec2.assert_called_once_with("messages", {"output": True})
    text = caplog.text
    assert "left before show_fresh (sweep)" in text
    assert ":messages after show_fresh: one | two | ruff failed: -86" in text


def test_nothing_dismissed_means_no_messages_read_and_no_log(
    caplog: pytest.LogCaptureFixture,
) -> None:
    nvim = _nvim()
    with (
        caplog.at_level(logging.WARNING, logger="vim_ai_follower"),
        prompt_guard(nvim, lambda: _nvim(), key="/s", adopted=False, label="x"),
    ):
        pass
    nvim.api.exec2.assert_not_called()
    assert caplog.records == []


def test_the_watchdog_dismisses_a_prompt_raised_inside_the_body(
    caplog: pytest.LogCaptureFixture,
) -> None:
    main = _nvim()
    main.api.exec2.return_value = {"output": "vaf-probe: long message\n"}
    dog = _nvim(_IDLE, _PROMPT, _PROMPT)
    with (
        caplog.at_level(logging.WARNING, logger="vim_ai_follower"),
        prompt_guard(main, lambda: dog, key="/s", adopted=False, label="apply_edit"),
    ):
        # The body "blocks" until the watchdog has answered the prompt on
        # its own connection — the main one is the blocked one.
        assert _wait_for(lambda: dog.api.input.called)
    dog.api.input.assert_called_once_with("<CR>")
    main.api.input.assert_not_called()
    assert "during apply_edit (watchdog)" in caplog.text
    assert ":messages after apply_edit: vaf-probe: long message" in caplog.text


def test_a_dismissal_counts_before_nvim_has_left_the_prompt(
    caplog: pytest.LogCaptureFixture,
) -> None:
    # The blocked body resumes as soon as the watchdog answers, while the
    # watchdog still waits for nvim to leave the prompt (up to
    # _SETTLE_SECONDS). A body that ends inside that wait must still see the
    # dismissal, or the :messages tail is never logged (a CI runner hit it).
    main = _nvim()
    main.api.exec2.return_value = {"output": "vaf-probe: long message\n"}
    dog = _nvim(_IDLE, *([_PROMPT] * 60))
    with (
        caplog.at_level(logging.WARNING, logger="vim_ai_follower"),
        prompt_guard(main, lambda: dog, key="/s", adopted=False, label="apply_edit"),
    ):
        assert _wait_for(lambda: dog.api.input.called)
    assert ":messages after apply_edit: vaf-probe: long message" in caplog.text


def test_the_watchdog_stops_and_closes_its_connection_when_the_body_raises() -> None:
    dog = _nvim()
    with (
        pytest.raises(ValueError, match="boom"),
        prompt_guard(_nvim(), lambda: dog, key="/s", adopted=False, label="x"),
    ):
        assert _wait_for(lambda: dog.api.get_mode.called)
        raise ValueError("boom")
    assert _watchdogs_alive() == []
    dog.close.assert_called_once_with()


def test_the_watchdog_stops_and_closes_its_connection_on_a_normal_exit() -> None:
    dog = _nvim()
    with prompt_guard(_nvim(), lambda: dog, key="/s", adopted=False, label="x"):
        assert _wait_for(lambda: dog.api.get_mode.called)
        assert len(_watchdogs_alive()) == 1
    assert _watchdogs_alive() == []
    dog.close.assert_called_once_with()


def test_a_nested_guard_on_the_same_socket_starts_no_second_watchdog() -> None:
    # apply_edit -> goto_file: two watchdogs would answer one prompt twice and
    # send the spare Enter to normal mode.
    connect = MagicMock(side_effect=lambda: _nvim())
    with prompt_guard(_nvim(), connect, key="/s", adopted=False, label="outer"):
        inner = _nvim(_PROMPT, _PROMPT)
        with prompt_guard(inner, connect, key="/s", adopted=False, label="inner"):
            pass
        inner.api.get_mode.assert_not_called()  # no second sweep either
    assert connect.call_count == 1
    # ...and the key is released afterwards: the next entry point guards again.
    with prompt_guard(_nvim(), connect, key="/s", adopted=False, label="next"):
        pass
    assert connect.call_count == 2


def test_a_watchdog_that_cannot_connect_is_logged_never_raised(
    caplog: pytest.LogCaptureFixture,
) -> None:
    ran = []
    with (
        caplog.at_level(logging.WARNING, logger="vim_ai_follower"),
        prompt_guard(
            _nvim(),
            MagicMock(side_effect=OSError("socket gone")),
            key="/s",
            adopted=False,
            label="goto_file",
        ),
    ):
        ran.append(True)
    assert ran == [True]
    assert "prompt watchdog could not connect (goto_file): socket gone" in caplog.text
    assert _watchdogs_alive() == []


def test_a_watchdog_poll_that_raises_is_logged_never_raised(
    caplog: pytest.LogCaptureFixture,
) -> None:
    dog = MagicMock()
    dog.api.get_mode.side_effect = EOFError("channel closed")
    with (
        caplog.at_level(logging.WARNING, logger="vim_ai_follower"),
        prompt_guard(_nvim(), lambda: dog, key="/s", adopted=False, label="resume"),
    ):
        assert _wait_for(lambda: "prompt watchdog stopped" in caplog.text)
    assert "prompt watchdog stopped (resume): channel closed" in caplog.text
    dog.close.assert_called_once_with()
    assert _watchdogs_alive() == []


def test_a_poll_failing_after_stop_is_the_expected_shutdown_not_logged(
    caplog: pytest.LogCaptureFixture,
) -> None:
    # A socket dropping as the guard ends (qall!, nvim exiting) is shutdown,
    # not a watchdog failure worth a hook.log line.
    polled = threading.Event()

    def slow_then_closed() -> dict[str, Any]:
        polled.set()
        time.sleep(0.2)  # the guard exits and sets stop meanwhile
        raise EOFError("channel closed")

    dog = MagicMock()
    dog.api.get_mode.side_effect = slow_then_closed
    with (
        caplog.at_level(logging.WARNING, logger="vim_ai_follower"),
        prompt_guard(_nvim(), lambda: dog, key="/s", adopted=False, label="x"),
    ):
        assert polled.wait(3)
    assert "prompt watchdog" not in caplog.text
    dog.close.assert_called_once_with()
    assert _watchdogs_alive() == []


def test_a_failing_messages_read_is_logged_never_raised(
    caplog: pytest.LogCaptureFixture,
) -> None:
    nvim = _nvim(_PROMPT, _PROMPT)
    nvim.api.exec2.side_effect = OSError("dead")
    with (
        caplog.at_level(logging.WARNING, logger="vim_ai_follower"),
        prompt_guard(nvim, lambda: _nvim(), key="/s", adopted=False, label="x"),
    ):
        pass
    assert "could not read :messages after x: dead" in caplog.text


def test_a_watchdog_that_will_not_stop_is_logged(caplog: pytest.LogCaptureFixture) -> None:
    release = threading.Event()
    dog = MagicMock()
    dog.api.get_mode.side_effect = lambda: (release.wait(5), _IDLE)[1]
    with (
        patch.object(nvim_prompt, "_JOIN_SECONDS", 0.05),
        caplog.at_level(logging.WARNING, logger="vim_ai_follower"),
    ):
        with prompt_guard(_nvim(), lambda: dog, key="/s", adopted=False, label="x"):
            assert _wait_for(lambda: dog.api.get_mode.called)
        assert "prompt watchdog did not stop (x)" in caplog.text
    release.set()
    assert _wait_for(lambda: _watchdogs_alive() == [])


# --- close_connection -------------------------------------------------------


def test_close_connection_closes_the_transport_and_runs_the_loop_before_close() -> None:
    nvim = MagicMock()
    close_connection(nvim)
    loop = nvim.loop
    assert nvim.mock_calls[:4] == [
        call._session.loop._transport.close(),
        call.loop.call_soon(loop.stop),
        call.loop.run_forever(),
        call.close(),
    ]


def test_close_connection_still_closes_when_pynvim_internals_are_missing() -> None:
    nvim = MagicMock()
    nvim._session.loop._transport.close.side_effect = AttributeError("no _transport")
    close_connection(nvim)
    nvim.loop.run_forever.assert_not_called()
    nvim.close.assert_called_once_with()


# --- exec_logged --------------------------------------------------------------


def test_a_dedicated_follower_captures_and_logs_what_an_event_command_printed(
    caplog: pytest.LogCaptureFixture,
) -> None:
    nvim = _nvim()
    nvim.api.exec2.return_value = {"output": "\nSpawning language server failed: -86\n"}
    with caplog.at_level(logging.WARNING, logger="vim_ai_follower"):
        exec_logged(nvim, "filetype detect", adopted=False)
    nvim.api.exec2.assert_called_once_with("filetype detect", {"output": True})
    nvim.command.assert_not_called()
    assert "`filetype detect` printed: Spawning language server failed: -86" in caplog.text


def test_a_silent_event_command_logs_nothing(caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.WARNING, logger="vim_ai_follower"):
        exec_logged(_nvim(), "edit!", adopted=False)
    assert caplog.records == []


def test_an_adopted_nvim_runs_event_commands_plainly() -> None:
    # Its messages are the user's to see in their own editor.
    nvim = _nvim()
    exec_logged(nvim, "edit!", adopted=True)
    nvim.command.assert_called_once_with("edit!")
    nvim.api.exec2.assert_not_called()


# --- NvimFollower wiring ------------------------------------------------------


def _follower_nvim() -> MagicMock:
    nvim = _nvim(_PROMPT, _PROMPT)
    nvim.exec_lua.return_value = -1  # probe_buffer: "absent", no further reads
    return nvim


def test_a_dedicated_follower_sweeps_and_watches_its_entry_points() -> None:
    state.FollowerState.set("@1", backend="nvim", target="/tmp/x.sock")
    nvim = _follower_nvim()
    follower = NvimFollower(socket_path="/tmp/x.sock", window_id="@1")
    with patch("vim_ai_follower.backends.nvim.pynvim.attach", return_value=nvim) as attach:
        assert follower.probe_buffer("/tmp/f.py", "x\n") == "absent"
    nvim.api.input.assert_called_once_with("<CR>")
    assert attach.call_count == 2  # the entry point's connection + the watchdog's


def test_an_adopted_follower_sweeps_and_watches_its_entry_points() -> None:
    # Decision (a), 2026-10-05: an adopted nvim answers hit-enter prompts
    # the way the Vim does — when the follower acts.
    state.FollowerState.set("@1", backend="nvim", target="/tmp/x.sock", adopted=True)
    nvim = _follower_nvim()
    follower = NvimFollower(socket_path="/tmp/x.sock", window_id="@1")
    with patch("vim_ai_follower.backends.nvim.pynvim.attach", return_value=nvim) as attach:
        assert follower.probe_buffer("/tmp/f.py", "x\n") == "absent"
    nvim.api.input.assert_called_once_with("<CR>")
    assert attach.call_count == 2  # the entry point's connection + the watchdog's


def test_is_alive_asks_a_fast_request_that_a_prompt_cannot_block() -> None:
    # Liveness is a raw, bounded `nvim_get_mode` (answered even at a blocking
    # prompt), never a pynvim session whose requests a prompt can hold.
    with (
        patch("vim_ai_follower.backends.nvim.pynvim.attach") as attach,
        patch("vim_ai_follower.backends.nvim.probe_nvim", return_value="answers") as probe,
    ):
        assert NvimFollower(socket_path="/tmp/x.sock").is_alive() is True
    probe.assert_called_once_with("/tmp/x.sock")
    attach.assert_not_called()


def test_a_follower_with_no_state_acts_as_adopted_and_still_sweeps() -> None:
    # No FollowerState (a concurrent `claude-follow stop` removed it between
    # two entry points, or no window id at all) cannot prove the nvim is a
    # DEDICATED one, so it takes the adopted rules — which, since decision
    # (a), still sweep an acting entry point: the sweep presses a key only AT
    # a hit-enter or more-prompt, under the answer lock.
    nvim = _follower_nvim()
    follower = NvimFollower(socket_path="/tmp/x.sock", window_id="@1")
    with patch("vim_ai_follower.backends.nvim.pynvim.attach", return_value=nvim) as attach:
        assert follower.probe_buffer("/tmp/f.py", "x\n") == "absent"
    nvim.api.input.assert_called_once_with("<CR>")
    assert attach.call_count == 2


@pytest.mark.parametrize("adopted", [True, False])
def test_an_nvim_pause_waits_under_the_sockets_pause(adopted: bool) -> None:
    # Every pause the nvim backend waits out is marked for its socket, so an
    # adopted watchdog stops answering for its duration.
    from vim_ai_follower.backends import nvim as nvim_backend

    state.FollowerState.set("@1", backend="nvim", target="/tmp/x.sock", adopted=adopted)
    seen: list[bool] = []

    def waiting(*_arguments: Any) -> bool:
        seen.append(nvim_prompt.is_paused("/tmp/x.sock"))
        return False  # interrupted while paused

    signals = iter(["pause"])
    with (
        patch.object(nvim_backend, "_wait_while_paused", side_effect=waiting),
        patch(
            "vim_ai_follower.control.check_signal",
            side_effect=lambda *_arguments: next(signals, None),
        ),
    ):
        result = nvim_backend._animate_lines(
            MagicMock(), 1, ("abc",), 0, lambda: 0.0, "@1", 1, prompt_key="/tmp/x.sock"
        )
    assert result.outcome == "interrupted"
    assert seen == [True]
    assert nvim_prompt.is_paused("/tmp/x.sock") is False


def test_the_sweep_names_a_more_prompt_in_the_log(caplog: pytest.LogCaptureFixture) -> None:
    nvim = _nvim(*[{"mode": "rm", "blocking": True}] * 2)
    with (
        caplog.at_level(logging.WARNING, logger="vim_ai_follower"),
        prompt_guard(nvim, lambda: _nvim(), key="/s", adopted=False, label="show_fresh"),
    ):
        pass
    nvim.api.input.assert_called_once_with("<Esc>")
    assert "dismissed a more-prompt left before show_fresh (sweep)" in caplog.text


def test_stop_sweeps_a_dedicated_follower_before_quitting() -> None:
    state.FollowerState.set("@1", backend="nvim", target="/tmp/x.sock")
    nvim = _nvim(_PROMPT, _PROMPT)
    with patch("vim_ai_follower.backends.nvim.pynvim.attach", return_value=nvim):
        NvimFollower(socket_path="/tmp/x.sock", window_id="@1").stop()
    assert nvim.mock_calls.index(call.api.input("<CR>")) < nvim.mock_calls.index(
        call.command("qall!")
    )


def test_stop_presses_no_key_and_quits_nothing_when_the_state_is_gone() -> None:
    # `qall!` is the most destructive thing this backend sends: without a
    # state proving the nvim is the follower's own, it may be the user's.
    nvim = _nvim(_PROMPT, _PROMPT)
    with patch("vim_ai_follower.backends.nvim.pynvim.attach", return_value=nvim) as attach:
        NvimFollower(socket_path="/tmp/x.sock", window_id="@1").stop()
    nvim.api.input.assert_not_called()
    nvim.command.assert_not_called()
    attach.assert_not_called()
