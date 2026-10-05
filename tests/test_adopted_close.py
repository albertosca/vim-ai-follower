"""Closing a tab in an ADOPTED editor (eviction past max_tabs, `stop`): the
verdict each backend reports and the hook.log line it leaves. The editors'
side runs for real in tests/test_integration_adopted_close.py and
tests/test_nvim_integration_adopted_close.py; here Vim is faked by a
send_text side effect that writes s:evict's answer file, and nvim by a mock.
"""

from __future__ import annotations

import logging
from unittest.mock import MagicMock, patch

import pytest
from helpers import evict_spelled, resolve_typed_paths, wipe_spelled

from vim_ai_follower import cache
from vim_ai_follower.backends import log_adopted_close, tmux_vim
from vim_ai_follower.backends.nvim import _EVICT_LUA, _FIND_BUFFER_LUA, NvimFollower
from vim_ai_follower.backends.tmux_vim import TmuxVimFollower
from vim_ai_follower.state import FollowerState

HANDLE_HEX = "abc123"
TOKEN = f"p3-{HANDLE_HEX}"


@pytest.mark.parametrize(
    ("verdict", "level", "words"),
    [
        ("kept", logging.WARNING, "unsaved changes"),
        ("closed", logging.INFO, "closed the follower's tab"),
        ("forgotten", logging.INFO, "the user had it open"),
        ("unread", logging.WARNING, "could not confirm"),
        (None, logging.WARNING, "no answer"),
    ],
)
def test_a_close_that_leaves_the_buffer_says_so(
    caplog: pytest.LogCaptureFixture, verdict: str | None, level: int, words: str
) -> None:
    caplog.set_level(logging.INFO, logger="vim_ai_follower")
    log_adopted_close("/w/f.py", verdict)
    [record] = caplog.records
    assert record.levelno == level
    assert "/w/f.py" in record.getMessage()
    assert words in record.getMessage()


@pytest.mark.parametrize("verdict", ["wiped", "absent"])
def test_a_close_that_left_nothing_behind_logs_nothing(
    caplog: pytest.LogCaptureFixture, verdict: str
) -> None:
    caplog.set_level(logging.INFO, logger="vim_ai_follower")
    log_adopted_close("/w/f.py", verdict)
    assert caplog.records == []


def _tmux_close(adopted: bool, answer: str | None) -> list[str]:
    window_id = "@5"
    FollowerState.set(window_id, backend="tmux", target="%3", adopted=adopted)
    sent: list[str] = []

    def _record(self: object, text: str) -> None:
        sent.append(resolve_typed_paths(text))
        if "('evict'," in text and answer is not None:
            (cache.CACHE_DIR / "evict-3.txt").write_text(f"{answer}\n{TOKEN}\n")

    with (
        patch("vim_ai_follower.backends.tmux_vim.TmuxPane.send_text", _record),
        patch("vim_ai_follower.backends.tmux_vim.TmuxPane.send_key"),
        patch("vim_ai_follower.backends.tmux_vim.secrets.token_hex", return_value=HANDLE_HEX),
    ):
        TmuxVimFollower(pane_id="%3", window_id=window_id).close_tab("/w/f.py")
    return sent


def test_a_dedicated_follower_still_wipes() -> None:
    sent = _tmux_close(adopted=False, answer=None)
    assert sent[-1] == wipe_spelled("/w/f.py")
    assert not any("'evict'" in line for line in sent)


def test_an_adopted_vim_asks_s_evict_and_logs_its_verdict(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.INFO, logger="vim_ai_follower")
    sent = _tmux_close(adopted=True, answer="kept")
    assert sent[-1] == evict_spelled("/w/f.py")
    assert not any("'wipe'" in line for line in sent)
    assert "unsaved changes" in caplog.text
    assert not (cache.CACHE_DIR / "evict-3.txt").exists()


def test_an_adopted_vim_that_never_answers_is_logged(
    caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(tmux_vim, "_PROBE_TIMEOUT_SECONDS", 0.01)
    record = cache.CACHE_DIR / "home-3"
    record.parent.mkdir(parents=True, exist_ok=True)
    record.write_text("123 h3-aaaaaa\n")
    (cache.CACHE_DIR / "h3-aaaaaa").write_text("")
    caplog.set_level(logging.INFO, logger="vim_ai_follower")
    _tmux_close(adopted=True, answer=None)
    assert "asking about its HOME again" in caplog.text
    assert "could not confirm" in caplog.text
    assert not record.exists()


def test_an_adopted_vim_that_never_answers_keeps_a_home_it_never_had(
    caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(tmux_vim, "_PROBE_TIMEOUT_SECONDS", 0.01)
    caplog.set_level(logging.INFO, logger="vim_ai_follower")
    _tmux_close(adopted=True, answer=None)
    assert "asking about its HOME again" not in caplog.text
    assert "could not confirm" in caplog.text


def _nvim_close(adopted: bool, found: int, verdict: str = "kept") -> MagicMock:
    FollowerState.set("@6", backend="nvim", target="/tmp/s.sock", adopted=adopted)
    nvim = MagicMock()

    def answer(code: str, *args: object) -> object:
        return {_FIND_BUFFER_LUA: found, _EVICT_LUA: verdict}.get(code, 0)

    nvim.exec_lua.side_effect = answer
    with patch("vim_ai_follower.backends.nvim.pynvim.attach", return_value=nvim):
        NvimFollower(socket_path="/tmp/s.sock", window_id="@6").close_tab("/w/f.py")
    return nvim


def test_an_adopted_nvim_runs_the_eviction_rules_and_logs_its_verdict(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.INFO, logger="vim_ai_follower")
    nvim = _nvim_close(adopted=True, found=4, verdict="forgotten")
    nvim.exec_lua.assert_any_call(_EVICT_LUA, 4)
    nvim.command.assert_not_called()  # no goto_file, no bwipeout from Python
    assert "the user had it open" in caplog.text


def test_an_adopted_nvim_with_no_buffer_for_the_file_does_nothing() -> None:
    nvim = _nvim_close(adopted=True, found=-1)
    assert all(call.args[0] != _EVICT_LUA for call in nvim.exec_lua.call_args_list)
    nvim.command.assert_not_called()
