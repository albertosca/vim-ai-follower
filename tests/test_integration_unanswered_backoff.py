"""A follower Vim that does not answer stalls one hook, not every hook (final
M3), on a real tmux+vim at the follower pane's real 49 columns.

A Vim that is blocked (an adopted Vim running `:!make`; here a `:sleep`)
never confirms a navigation: each call waited the whole landing
timeout (~10 s) and every hook after it waited again. After one unanswered
landing the pane is in a backoff (`unanswered-<pane>` in the cache
directory): for _UNANSWERED_BACKOFF_SECONDS a navigation raises
NavigationFailed at once and a probe is "unknown" at once, with no key sent
into the pane. A landing after it removes the mark.

Isolation: tmux_session unsets TMUX and points TMUX_TMPDIR at a private
directory; the socket is asserted before any in-process backend call. The
cache directory is the test's own (conftest's isolated_dirs)."""

from __future__ import annotations

import os
import subprocess
import time
from collections.abc import Callable
from pathlib import Path
from unittest.mock import patch

import pytest

from vim_ai_follower.backends import NavigationFailed, tmux_vim
from vim_ai_follower.backends.tmux_vim import TmuxVimFollower
from vim_ai_follower.state import FollowerState
from vim_ai_follower.tmux import TmuxPane

pytestmark = pytest.mark.integration

# How long the follower's Vim is blocked: past the first call's landing wait.
_BLOCKED_SECONDS = 14


def _tmux(*args: str) -> str:
    return subprocess.run(["tmux", *args], capture_output=True, text=True, check=True).stdout


def _keys(pane: str, *keys: str) -> None:
    subprocess.run(["tmux", "send-keys", "-t", pane, *keys], check=True)


def _assert_private_server() -> None:
    socket_path = _tmux("display-message", "-p", "#{socket_path}").strip()
    assert "TMUX" not in os.environ
    private = os.path.realpath(os.environ["TMUX_TMPDIR"])
    assert os.path.realpath(socket_path).startswith(private), socket_path


def _vim_pane(tmux_session: str, tmp_path: Path, wait_until: Callable[..., bool]) -> str:
    _assert_private_server()
    origin = _tmux("list-panes", "-t", tmux_session, "-F", "#{pane_id}").split()[0]
    pane = _tmux(
        "split-window", "-h", "-l", "49", "-t", origin, "-P", "-F", "#{pane_id}",
        "vim -N -u NONE -i NONE",
    ).strip()  # fmt: skip
    ready = tmp_path / "ready.txt"
    _keys(pane, "-l", "--", f":call writefile(['R'], '{ready}')")
    _keys(pane, "Enter")
    assert wait_until(ready.exists, timeout=10.0), _tmux("capture-pane", "-p", "-t", pane)
    return pane


class _KeyCounter:
    """Counts every key the backend sends into the pane, still sending it."""

    def __init__(self) -> None:
        self.count = 0
        send_text = TmuxPane.send_text
        send_key = TmuxPane.send_key

        def counted_text(pane: TmuxPane, text: str) -> None:
            self.count += 1
            send_text(pane, text)

        def counted_key(pane: TmuxPane, key: str) -> None:
            self.count += 1
            send_key(pane, key)

        self.send_text = counted_text
        self.send_key = counted_key


def test_one_unanswered_landing_backs_the_pane_off_until_a_landing(
    tmux_session: str, tmp_path: Path, wait_until: Callable[..., bool]
) -> None:
    pane = _vim_pane(tmux_session, tmp_path, wait_until)
    FollowerState.set("@1", "tmux", pane, adopted=False, speed="instant")
    follower = TmuxVimFollower(pane_id=pane, pace_seconds=0.0, window_id="@1")
    target = tmp_path / "t.py"
    target.write_text("a = 1\n")
    marker = tmux_vim._unanswered_path(pane)

    # Vim blocks, as in `:!make`; the keys sent meanwhile wait in its input.
    blocked_until = time.monotonic() + _BLOCKED_SECONDS
    _keys(pane, "-l", "--", f":sleep {_BLOCKED_SECONDS}")
    _keys(pane, "Enter")

    started = time.monotonic()
    with pytest.raises(NavigationFailed, match="no answer"):
        follower.goto_file(str(target))
    assert time.monotonic() - started >= tmux_vim._LANDING_TIMEOUT_SECONDS - 0.5

    counter = _KeyCounter()
    with (
        patch.object(TmuxPane, "send_text", counter.send_text),
        patch.object(TmuxPane, "send_key", counter.send_key),
    ):
        started = time.monotonic()
        with pytest.raises(NavigationFailed, match="not retrying"):
            follower.goto_file(str(target))
        assert time.monotonic() - started < 0.5
        started = time.monotonic()
        assert follower.probe_buffer(str(target), "a = 1\n") == "unknown"
        with pytest.raises(NavigationFailed, match="not retrying"):
            follower.show_fresh(str(target), "a = 1\n")
        follower.close_tab(str(target))
        assert time.monotonic() - started < 0.5
    assert counter.count == 0
    assert marker.exists()

    # Vim is free again (and has run the keys the first call queued); the
    # backoff runs out.
    time.sleep(max(0.0, blocked_until - time.monotonic()) + 1.0)
    expired = time.time() - tmux_vim._UNANSWERED_BACKOFF_SECONDS - 1
    os.utime(marker, (expired, expired))

    follower.goto_file(str(target))
    assert not marker.exists()
