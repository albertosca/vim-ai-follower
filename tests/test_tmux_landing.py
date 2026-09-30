"""The tmux backend's landing check: a line that must act on its target
(goto_file's navigation, show_fresh's rename) answers only once the current
buffer IS the target (tmux_vim._ANSWER_IF_LANDED), and nothing more is sent
without that answer. The real-Vim half, with a user's unsaved buffer and a
stale HOME answer, is tests/test_integration_stale_home.py."""

from __future__ import annotations

import re
import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest

from vim_ai_follower import cache
from vim_ai_follower.backends import NavigationFailed, tmux_vim
from vim_ai_follower.backends.tmux_vim import TmuxVimFollower
from vim_ai_follower.diff import compute_edit_script
from vim_ai_follower.tmux import TmuxPane

pytestmark = pytest.mark.real_landing

_LANDED = re.compile(r"writefile\(\[g:vaf_r, '([0-9a-f]{8})'\], '([^']*)'\)")
_HANDLE = re.compile(r"let g:vaf_h = '([^']*)'")


def _vim(verdict: str | None, sent: list[str], reads_handle: bool = True) -> object:
    """A send_text double playing Vim. A path read deletes its handle, as
    Vim's does (unless `reads_handle` is off: a Vim whose `~` misses it). The
    landing check writes `verdict` with the token, or nothing when None."""

    def send_text(self: object, text: str) -> None:
        sent.append(text)
        handle = _HANDLE.search(text)
        if reads_handle and handle:
            Path(handle.group(1)).unlink(missing_ok=True)
        match = _LANDED.search(text)
        if verdict is not None and match:
            Path(match.group(2)).write_text(f"{verdict}\n{match.group(1)}\n")

    return send_text


def _run(
    lands: bool | str | None, call: object, reads_handle: bool = True
) -> tuple[list[str], BaseException | None]:
    """lands: True answers "landed", False answers nothing, a string is the
    verdict itself."""
    verdict = "landed" if lands is True else None if lands is False else lands
    sent: list[str] = []
    with (
        patch.object(TmuxPane, "send_text", _vim(verdict, sent, reads_handle)),
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


@pytest.mark.parametrize("verdict", [False, "elsewhere", "unread", "garbage"])
@pytest.mark.parametrize("name", ACTING)
def test_nothing_follows_a_navigation_vim_did_not_confirm(name: str, verdict: bool | str) -> None:
    sent, error = _run(verdict, ACTING[name])
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
        answer.write_text("landed\n00000000\n")

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


def test_no_answer_and_an_unread_handle_forget_a_yes() -> None:
    """What a Vim whose `~` is not the hook's leaves: it reads no handle and
    its answer goes nowhere."""
    record, _marker = _record(True)
    _sent, error = _run(False, ACTING["goto_file"], reads_handle=False)
    assert isinstance(error, NavigationFailed)
    assert "asking about its HOME again" in str(error)
    assert not record.exists()
    assert not list(cache.CACHE_DIR.glob("p2-*"))  # the unread handle goes


@pytest.mark.parametrize(
    ("lands", "reason"),
    [
        (False, "no answer"),  # the handle was read: a busy or slow Vim
        ("elsewhere", "it landed on another buffer"),
        ("unread", "its path handle could not be read"),
    ],
)
def test_other_failures_keep_a_yes(lands: bool | str, reason: str) -> None:
    record, _marker = _record(True)
    _sent, error = _run(lands, ACTING["goto_file"])
    assert isinstance(error, NavigationFailed)
    assert f"({reason})" in str(error)
    assert record.exists()


def test_a_failed_navigation_keeps_a_no() -> None:
    record, _marker = _record(False)
    _sent, error = _run(False, ACTING["goto_file"], reads_handle=False)
    assert isinstance(error, NavigationFailed)
    assert record.exists()


def test_a_confirmed_navigation_keeps_a_yes() -> None:
    record, _marker = _record(True)
    _sent, error = _run(True, ACTING["goto_file"])
    assert error is None
    assert record.exists()


def test_an_unanswered_probe_forgets_a_yes() -> None:
    record, _marker = _record(True)
    _sent, error = _run(
        False, lambda follower: follower.probe_buffer("/tmp/t.py", "a\n"), reads_handle=False
    )
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
    assert tmux_vim._distrust_home("%2") is False
    record = cache.CACHE_DIR / "home-2"
    record.parent.mkdir(parents=True)
    record.write_text("garbled")
    assert tmux_vim._distrust_home("%2") is False
    assert record.read_text() == "garbled"


@pytest.mark.integration
def test_a_real_vims_landing_left_by_an_earlier_line_never_confirms_a_new_one(
    tmp_path: Path,
) -> None:
    """The check answers "landed" only for the landing its own acting line
    recorded: an earlier line's leftovers (a garbled one never reached its
    `unlet!`) name another token, and an acting line forgets them first."""
    answer = tmp_path / "landed.txt"
    out = tmp_path / "out.txt"
    missing = tmp_path / "gone"
    check = tmux_vim._ANSWER_IF_LANDED.format(
        nonce="'bbbbbbbb'", answer=tmux_vim._vim_string(str(answer))
    ).removeprefix(":")
    start = tmux_vim._ACTING_START.format(
        token="'cccccccc'",
        read=f"let g:vaf_h = {tmux_vim._vim_string(str(missing))} | let g:vaf_p = ''",
    ).removeprefix(":")
    subprocess.run(
        [
            "vim",
            "-Nu",
            "NONE",
            "-i",
            "NONE",
            "-es",
            "-c",
            "let g:vaf_p = '/x' | let g:vaf_k = 'aaaaaaaa' | let g:vaf_landed = bufnr('%')",
            "-c",
            check,
            "-c",
            f"call writefile(readfile('{answer}'), '{out}')",
            "-c",
            "let g:vaf_landed = bufnr('%') | let g:vaf_k = 'cccccccc'",
            "-c",
            start,
            "-c",
            f"call writefile(readfile('{out}') + [exists('g:vaf_landed') . ''], '{out}')",
            "-c",
            "qa!",
        ],
        check=True,
        timeout=30,
        stdin=subprocess.DEVNULL,
    )
    assert out.read_text().splitlines() == ["elsewhere", "bbbbbbbb", "0"]
