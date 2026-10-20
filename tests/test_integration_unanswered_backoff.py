"""A follower Vim that does not answer stalls one hook, not every hook (final
M3), on a real tmux+vim at the follower pane's real 49 columns.

A Vim that is blocked (an adopted Vim running `:!make`; here a `:sleep`)
never confirms a navigation: each call waited the whole landing
timeout (~10 s) and every hook after it waited again. After one unanswered
landing the pane is in a backoff (`unanswered-<pane>` in the cache
directory): for _UNANSWERED_BACKOFF_SECONDS a navigation raises
NavigationFailed at once and a probe is "unknown" at once, with no key sent
into the pane. A tab close sends nothing either: it is recorded and runs
at the next contact after the backoff. A landing removes the mark.

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
from test_integration_relock_autocmd import follower, state, vim_pane

from vim_ai_follower.backends import NavigationFailed, tmux_vim
from vim_ai_follower.backends.tmux_vim import TmuxVimFollower
from vim_ai_follower.state import FollowerState
from vim_ai_follower.tmux import TmuxPane

pytestmark = pytest.mark.integration

# How long the follower's Vim is blocked: past the first call's landing wait.
_BLOCKED_SECONDS = 14


def _tmux(*arguments: str) -> str:
    return subprocess.run(["tmux", *arguments], capture_output=True, text=True, check=True).stdout


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
        assert time.monotonic() - started < 0.5
    assert counter.count == 0
    # A close sends nothing either: it is recorded for the next contact
    # (the tests below).
    assert marker.exists()

    # Vim is free again (and has run the keys the first call queued); the
    # backoff runs out.
    time.sleep(max(0.0, blocked_until - time.monotonic()) + 1.0)
    expired = time.time() - tmux_vim._UNANSWERED_BACKOFF_SECONDS - 1
    os.utime(marker, (expired, expired))

    follower.goto_file(str(target))
    assert not marker.exists()


def _expire(marker: Path) -> None:
    expired = time.time() - tmux_vim._UNANSWERED_BACKOFF_SECONDS - 1
    os.utime(marker, (expired, expired))


@pytest.mark.parametrize("adopted", [False, True], ids=["dedicated", "adopted"])
def test_a_close_during_the_backoff_sends_nothing_and_releases_later(
    adopted: bool, tmux_session: str, tmp_path: Path, wait_until: Callable[..., bool]
) -> None:
    """Sweep 5 (Alberto's answer A4 = A): a close during the backoff was sent
    at once, and a Vim blocked in `:!cmd` handed its keys to the command's
    stdin. Now it sends nothing: the eviction (and, adopted, the release of
    the follower's claims) is recorded for this Vim and runs at the next
    contact once the backoff is over (review of T6+T7, I1 and m4 still
    hold: the buffer is given back and the tab closed, only later)."""
    target = tmp_path / "t.py"
    target.write_text("a = 1\n")
    other = tmp_path / "other.txt"
    other.write_text("other\n")
    swallowed = tmp_path / "stdin.txt"
    pane = vim_pane(tmux_session, tmp_path, wait_until, f"edit {other}")
    reader = follower(pane, adopted=adopted)
    reader.ensure_showing(str(target))  # Claude's Read: a tab, locked
    assert state(pane, tmp_path, wait_until)[0] == "0"
    marker = tmux_vim._unanswered_path(pane)
    marker.touch()  # one landing went unanswered moments ago
    _keys(pane, "-l", "--", f":!cat > {swallowed}")
    _keys(pane, "Enter")
    assert wait_until(swallowed.exists, timeout=5.0)

    started = time.monotonic()
    reader.close_tab(str(target))
    assert time.monotonic() - started < 0.5
    time.sleep(0.5)
    assert swallowed.read_bytes() == b"", swallowed.read_bytes()

    _keys(pane, "C-d")  # cat ends; Vim waits for Enter
    _keys(pane, "Enter")
    expression = f"tabpagenr('$') . bufexists('{target}')"
    assert state(pane, tmp_path, wait_until, expression)[2] == "21"  # still there
    _expire(marker)
    reader.goto_file(str(other))  # the next contact
    assert state(pane, tmp_path, wait_until, expression)[2] == "10"
    assert not tmux_vim._pending_evictions_path(_vim_pid(pane)).exists()


def _vim_pid(pane: str) -> int:
    vim = tmux_vim._foreground_vim(pane)
    assert vim is not None
    return vim.pid


def test_a_stop_during_the_backoff_unlocks_the_users_own_buffer_at_the_next_contact(
    tmux_session: str, tmp_path: Path, wait_until: Callable[..., bool]
) -> None:
    """I1 as the reviewer reproduced it: the user's OWN buffer, locked by a
    Read, released by the close `claude-follow stop` runs per open file —
    now at the next contact after the backoff, not while it holds."""
    target = tmp_path / "t.py"
    target.write_text("a = 1\n")
    pane = vim_pane(tmux_session, tmp_path, wait_until, f"edit {target}")
    adopted = follower(pane, adopted=True)
    adopted.ensure_showing(str(target))
    marker = tmux_vim._unanswered_path(pane)
    marker.touch()

    adopted.close_tab(str(target))
    modifiable, *_ = state(pane, tmp_path, wait_until)
    assert modifiable == "0"  # nothing sent yet

    _expire(marker)
    adopted.goto_file(str(target))  # the next contact: no lock of its own
    modifiable, _paste, answer, *_ = state(
        pane, tmp_path, wait_until, "&readonly . get(b:, 'vaf_ro_ours', 'x')"
    )
    assert (modifiable, answer) == ("1", "00")
