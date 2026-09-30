"""The tmux backend's landing check: a line that must act on its target
(goto_file's navigation, show_fresh's rename) answers only once the current
buffer IS the target (tmux_vim._ANSWER_IF_LANDED), and nothing more is sent
without that answer. The real-Vim half, with a user's unsaved buffer and a
stale HOME answer, is tests/test_integration_stale_home.py."""

from __future__ import annotations

import re
from pathlib import Path
from unittest.mock import patch

import pytest

from vim_ai_follower import cache
from vim_ai_follower.backends import tmux_vim
from vim_ai_follower.backends.tmux_vim import NavigationFailed, TmuxVimFollower
from vim_ai_follower.diff import compute_edit_script
from vim_ai_follower.tmux import TmuxPane

pytestmark = pytest.mark.real_landing

_LANDED = re.compile(r"writefile\(\['([0-9a-f]{8})'\], '([^']*)'\)")


def _vim(lands: bool, sent: list[str]) -> object:
    """A send_text double playing Vim: a navigating line's confirmation is
    written only when `lands`."""

    def send_text(self: object, text: str) -> None:
        sent.append(text)
        match = _LANDED.search(text)
        if lands and match:
            Path(match.group(2)).write_text(match.group(1) + "\n")

    return send_text


def _run(lands: bool, call: object) -> tuple[list[str], BaseException | None]:
    sent: list[str] = []
    with (
        patch.object(TmuxPane, "send_text", _vim(lands, sent)),
        patch.object(TmuxPane, "send_key"),
        patch.object(TmuxPane, "pane_pid", return_value=None),
        patch.object(tmux_vim, "_PROBE_TIMEOUT_SECONDS", 0.05),
        patch.object(tmux_vim, "_LANDING_TIMEOUT_SECONDS", 0.05),
        patch("vim_ai_follower.control.check_signal", return_value=None),
    ):
        try:
            call(TmuxVimFollower(pane_id="%2"))  # type: ignore[operator]
        except NavigationFailed as error:
            return sent, error
    return sent, None


ACTING = {
    "goto_file": lambda follower: follower.goto_file("/tmp/t.py"),
    "ensure_showing": lambda follower: follower.ensure_showing("/tmp/t.py"),
    "apply_edit": lambda follower: follower.apply_edit(
        "/tmp/t.py", compute_edit_script("a\n", "a\nb\n"), "a\n"
    ),
    "reload_and_relock": lambda follower: follower.reload_and_relock("/tmp/t.py"),
    "reload_from_disk": lambda follower: follower.reload_from_disk("/tmp/t.py"),
    "rewrite_buffer": lambda follower: follower.rewrite_buffer("/tmp/t.py", "a\n"),
    "show_fresh": lambda follower: follower.show_fresh("/tmp/t.py", "a\n"),
}


@pytest.mark.parametrize("name", ACTING)
def test_nothing_follows_a_navigation_vim_did_not_confirm(name: str) -> None:
    sent, error = _run(False, ACTING[name])
    assert isinstance(error, NavigationFailed)
    # The confirming line is the last thing typed: no lock, unlock, `%d`,
    # filetype, keystrokes or relock after it.
    assert _LANDED.search(sent[-1]), sent
    assert not any(_LANDED.search(text) for text in sent[:-1])
    assert not (cache.CACHE_DIR / "landed-2.txt").exists()


@pytest.mark.parametrize("name", ACTING)
def test_a_confirmed_navigation_goes_on(name: str) -> None:
    sent, error = _run(True, ACTING[name])
    assert error is None
    if name != "goto_file":
        assert not _LANDED.search(sent[-1]), sent
    assert not (cache.CACHE_DIR / "landed-2.txt").exists()


def test_a_stale_confirmation_from_an_earlier_line_is_not_taken() -> None:
    """A late answer to an earlier navigation carries that line's nonce."""
    answer = cache.CACHE_DIR / "landed-2.txt"
    answer.parent.mkdir(parents=True)

    def stale(self: object, text: str) -> None:
        answer.write_text("00000000\n")

    with (
        patch.object(TmuxPane, "send_text", stale),
        patch.object(TmuxPane, "send_key"),
        patch.object(TmuxPane, "pane_pid", return_value=None),
        patch.object(tmux_vim, "_PROBE_TIMEOUT_SECONDS", 0.05),
        patch.object(tmux_vim, "_LANDING_TIMEOUT_SECONDS", 0.05),
        pytest.raises(NavigationFailed),
    ):
        TmuxVimFollower(pane_id="%2").goto_file("/tmp/t.py")


# --- self-heal: an unanswered question drops a "yes" to the HOME question ---


def _record(marker_exists: bool) -> tuple[Path, Path]:
    cache.CACHE_DIR.mkdir(parents=True, exist_ok=True)
    record = cache.CACHE_DIR / "home-2"
    record.write_text("4242 h2-abcdef\n")
    marker = cache.CACHE_DIR / "h2-abcdef"
    if marker_exists:
        marker.write_text("")
    return record, marker


def test_a_failed_navigation_forgets_a_yes() -> None:
    record, _marker = _record(True)
    _sent, error = _run(False, ACTING["goto_file"])
    assert isinstance(error, NavigationFailed)
    assert not record.exists()


def test_a_failed_navigation_keeps_a_no() -> None:
    record, _marker = _record(False)
    _sent, error = _run(False, ACTING["goto_file"])
    assert isinstance(error, NavigationFailed)
    assert record.exists()


def test_a_confirmed_navigation_keeps_a_yes() -> None:
    record, _marker = _record(True)
    _sent, error = _run(True, ACTING["goto_file"])
    assert error is None
    assert record.exists()


def test_an_unanswered_probe_forgets_a_yes() -> None:
    record, _marker = _record(True)
    _sent, error = _run(False, lambda follower: follower.probe_buffer("/tmp/t.py", "a\n"))
    assert error is None
    assert not record.exists()


def test_an_unanswered_readonly_question_forgets_a_yes() -> None:
    record, _marker = _record(True)
    with (
        patch.object(tmux_vim, "_PROBE_TIMEOUT_SECONDS", 0.05),
        patch.object(tmux_vim, "_LANDING_TIMEOUT_SECONDS", 0.05),
        patch.object(TmuxVimFollower, "_is_adopted", return_value=True),
    ):
        assert TmuxVimFollower(pane_id="%2").user_readonly("/tmp/t.py") is False
    assert not record.exists()


def test_distrust_without_a_record_or_with_a_garbled_one_does_nothing() -> None:
    tmux_vim._distrust_home("%2")
    record = cache.CACHE_DIR / "home-2"
    record.parent.mkdir(parents=True)
    record.write_text("garbled")
    tmux_vim._distrust_home("%2")
    assert record.read_text() == "garbled"
