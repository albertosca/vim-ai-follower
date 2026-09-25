"""Unit coverage for nvim_prompt: the rules of the hit-enter sweep + watchdog
that keep a plugin message from freezing a dedicated nvim follower. The
real-nvim proof (a UI nvim in a private tmux) is
test_nvim_integration_hit_enter.py; here each rule is pinned on its own."""

from __future__ import annotations

import logging
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
    dismiss_hit_enter,
    exec_logged,
    prompt_guard,
)

_PROMPT = {"mode": "r", "blocking": True}
_IDLE = {"mode": "n", "blocking": False}


def _nvim(*modes: dict[str, Any]) -> MagicMock:
    """A mock connection whose get_mode answers `modes` in turn, then idle."""
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


# --- dismiss_hit_enter: only a blocking "r" ----------------------------------


def test_a_blocking_hit_enter_prompt_is_answered_with_enter() -> None:
    nvim = _nvim(_PROMPT)
    assert dismiss_hit_enter(nvim) == _PROMPT
    nvim.api.input.assert_called_once_with("<CR>")


@pytest.mark.parametrize(
    "mode",
    [
        {"mode": "r?", "blocking": True},  # a confirm prompt: Enter would pick a choice
        {"mode": "rm", "blocking": True},  # the more-prompt
        {"mode": "n", "blocking": True},  # blocked, but not on a prompt (pending keys)
        {"mode": "r", "blocking": False},
        _IDLE,
    ],
)
def test_anything_but_a_blocking_r_is_left_alone(mode: dict[str, Any]) -> None:
    nvim = _nvim(mode)
    assert dismiss_hit_enter(nvim) is None
    nvim.api.input.assert_not_called()


# --- prompt_guard -------------------------------------------------------------


def test_an_adopted_nvim_gets_no_sweep_and_no_watchdog() -> None:
    # The user's own editor: its prompts are the user's to read.
    nvim = _nvim(_PROMPT)
    connect = MagicMock()
    with prompt_guard(nvim, connect, key="/s", adopted=True, label="x"):
        pass
    nvim.api.get_mode.assert_not_called()
    nvim.api.input.assert_not_called()
    connect.assert_not_called()


def test_the_sweep_dismisses_a_leftover_prompt_and_logs_the_messages_tail(
    caplog: pytest.LogCaptureFixture,
) -> None:
    nvim = _nvim(_PROMPT)
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
    dog = _nvim(_IDLE, _PROMPT)
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
        inner = _nvim(_PROMPT)
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
    nvim = _nvim(_PROMPT)
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
    nvim = _nvim(_PROMPT)
    nvim.exec_lua.return_value = -1  # probe_buffer: "absent", no further reads
    return nvim


def test_a_dedicated_follower_sweeps_and_watches_its_entry_points() -> None:
    nvim = _follower_nvim()
    follower = NvimFollower(socket_path="/tmp/x.sock", window_id="@1")
    with patch("vim_ai_follower.backends.nvim.pynvim.attach", return_value=nvim) as attach:
        assert follower.probe_buffer("/tmp/f.py", "x\n") == "absent"
    nvim.api.input.assert_called_once_with("<CR>")
    assert attach.call_count == 2  # the entry point's connection + the watchdog's


def test_an_adopted_follower_never_sweeps_or_watches() -> None:
    state.FollowerState.set("@1", backend="nvim", target="/tmp/x.sock", adopted=True)
    nvim = _follower_nvim()
    follower = NvimFollower(socket_path="/tmp/x.sock", window_id="@1")
    with patch("vim_ai_follower.backends.nvim.pynvim.attach", return_value=nvim) as attach:
        assert follower.probe_buffer("/tmp/f.py", "x\n") == "absent"
    nvim.api.get_mode.assert_not_called()
    nvim.api.input.assert_not_called()
    assert attach.call_count == 1


def test_is_alive_asks_a_fast_request_that_a_prompt_cannot_block() -> None:
    nvim = MagicMock()
    with patch("vim_ai_follower.backends.nvim.pynvim.attach", return_value=nvim):
        assert NvimFollower(socket_path="/tmp/x.sock").is_alive() is True
    nvim.api.get_mode.assert_called_once_with()
    nvim.api.get_current_buf.assert_not_called()
