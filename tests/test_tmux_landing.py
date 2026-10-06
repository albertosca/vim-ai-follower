"""The tmux backend's landing check: a line that must act on its target
(goto_file's navigation, show_fresh's rename) answers only once the current
buffer IS the target (tmux_vim's s:landed), and nothing more is sent
without that answer. The real-Vim half, with a user's unsaved buffer and a
stale HOME answer, is tests/test_integration_stale_home.py."""

from __future__ import annotations

import os
import re
import subprocess
import time
from pathlib import Path
from unittest.mock import patch

import pytest

from vim_ai_follower import cache
from vim_ai_follower.backends import NavigationFailed, tmux_vim
from vim_ai_follower.backends.tmux_vim import TmuxVimFollower
from vim_ai_follower.diff import compute_edit_script
from vim_ai_follower.state import FollowerState
from vim_ai_follower.tmux import TmuxPane

pytestmark = pytest.mark.real_landing

# The landing check's call (s:landed) and the calls that read a path handle,
# each naming its handle (`p<pane>-<6 hex>`, also the call's token).
_LANDED = re.compile(r"\('landed','(p([0-9]+)-[0-9a-f]{6})'\)")
_HANDLE = re.compile(r"\('(?:goto|rename|probe|wipe)','(p[0-9]+-[0-9a-f]{6})'")


def _vim(verdict: str | None, sent: list[str], reads_handle: bool = True) -> object:
    """A send_text double playing Vim. A path read deletes its handle, as
    Vim's does (unless `reads_handle` is off: a Vim that never got the
    functions, because the `~` it was told to source them from misses). The
    landing check writes `verdict` with the token, or nothing when None."""

    def send_text(self: object, text: str) -> None:
        sent.append(text)
        handle = _HANDLE.search(text)
        if reads_handle and handle:
            (cache.CACHE_DIR / handle.group(1)).unlink(missing_ok=True)
        match = _LANDED.search(text)
        if verdict is not None and match:
            answer = cache.CACHE_DIR / f"landed-{match.group(2)}.txt"
            answer.write_text(f"{verdict}\n{match.group(1)}\n")

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
        answer.write_text("landed\np2-000000\n")

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
    assert f"({reason}" in str(error)  # no answer goes on: "; not retrying …"
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
    (cache.CACHE_DIR / "readonly-2.asked").touch()  # a restore requested it
    tmux_vim._SENT_READONLY_TOKENS["%2"] = 123456  # and this process sent it
    with (
        patch.object(tmux_vim, "_PROBE_TIMEOUT_SECONDS", 0.05),
        patch.object(tmux_vim, "_LANDING_TIMEOUT_SECONDS", 0.05),
        patch.object(TmuxVimFollower, "_is_dedicated", return_value=False),
    ):
        assert TmuxVimFollower(pane_id="%2").user_readonly("/tmp/t.py") is False
    assert not record.exists()


def test_a_vim_whose_state_is_gone_is_still_asked_about_readonly() -> None:
    # Fail closed: unknown is not dedicated, so the user's readonly gets
    # the `:w!` cue instead of a plain one that ends in E45.
    FollowerState.set("@9", backend="tmux", target="%2", adopted=True)
    FollowerState.clear("@9")
    answer = cache.CACHE_DIR / "readonly-2.txt"
    answer.parent.mkdir(parents=True, exist_ok=True)
    answer.write_text("1\n123456\n")
    (cache.CACHE_DIR / "readonly-2.asked").touch()  # a restore requested it
    tmux_vim._SENT_READONLY_TOKENS["%2"] = 123456  # and this process sent it
    assert TmuxVimFollower(pane_id="%2", window_id="@9").user_readonly("/tmp/t.py") is True


def test_a_dedicated_vim_is_never_asked_about_readonly() -> None:
    FollowerState.set("@9", backend="tmux", target="%2", adopted=False)
    answer = cache.CACHE_DIR / "readonly-2.txt"
    answer.parent.mkdir(parents=True, exist_ok=True)
    answer.write_text("1\n")
    assert TmuxVimFollower(pane_id="%2", window_id="@9").user_readonly("/tmp/t.py") is False


def test_an_unrequested_readonly_answer_is_never_waited_for_or_read(
    caplog: pytest.LogCaptureFixture,
) -> None:
    # Re-review of sweep-4 T4+T5, N3: with the state gone, the hand-off sends
    # no readonly restore, so nothing will answer and an answer on disk is an
    # EARLIER hand-off's. Only a restore this cycle requested is waited for.
    FollowerState.set("@9", backend="tmux", target="%2", adopted=True)
    FollowerState.clear("@9")
    stale = cache.CACHE_DIR / "readonly-2.txt"
    stale.parent.mkdir(parents=True, exist_ok=True)
    stale.write_text("1\n")
    with patch(
        "vim_ai_follower.backends.tmux_vim.time.sleep", side_effect=AssertionError("waited")
    ):
        assert TmuxVimFollower(pane_id="%2", window_id="@9").user_readonly("/tmp/t.py") is False
    assert "no readonly answer" not in caplog.text


# --- final M3: one unanswered landing backs the pane off ---


def test_no_answer_backs_the_pane_off_and_the_next_calls_send_nothing(
    caplog: pytest.LogCaptureFixture,
) -> None:
    _sent, error = _run(False, ACTING["goto_file"])
    assert isinstance(error, NavigationFailed)
    assert "(no answer; not retrying for 30 s)" in str(error)
    assert tmux_vim._unanswered_path("%2").exists()
    calls = {
        **ACTING,
        "probe_buffer": lambda follower: follower.probe_buffer("/tmp/t.py", "a\n"),
        "close_tab": lambda follower: follower.close_tab("/tmp/t.py"),
    }
    for name, call in calls.items():
        started = time.monotonic()
        sent, error = _run(True, call)
        assert time.monotonic() - started < 0.5, name
        if name == "close_tab":
            # Sent anyway, to give back what the follower holds (review of
            # T6+T7, I1): the call alone, no define line, no verdict awaited.
            assert error is None
            assert len(sent) == 1 and "('evict','p2-" in sent[0], sent
            continue
        assert sent == [], name
        if name == "probe_buffer":
            assert error is None
        else:
            assert isinstance(error, NavigationFailed), name
            assert "did not answer 0 s ago; not retrying for another 30 s" in str(error)
    assert "did not ask the follower's Vim about /tmp/t.py: the follower's Vim" in caplog.text
    assert "closing /tmp/t.py without waiting for its verdict: the follower's Vim" in caplog.text


def test_no_answer_explained_by_a_home_distrust_does_not_back_off() -> None:
    """The HOME distrust comes first: the next call asks again and heals at
    once (tests/test_integration_stale_home.py)."""
    record, _marker = _record(True)
    _sent, error = _run(False, ACTING["goto_file"], reads_handle=False)
    assert isinstance(error, NavigationFailed)
    assert str(error).endswith("(no answer; asking about its HOME again)")
    assert not record.exists()
    assert not tmux_vim._unanswered_path("%2").exists()


@pytest.mark.parametrize("lands", ["elsewhere", "unread"])
def test_an_answer_that_is_not_a_landing_does_not_back_off(lands: str) -> None:
    _sent, error = _run(lands, ACTING["goto_file"])
    assert isinstance(error, NavigationFailed)
    assert not tmux_vim._unanswered_path("%2").exists()


def test_a_landing_after_the_backoff_ends_it() -> None:
    marker = tmux_vim._unanswered_path("%2")
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.touch()
    expired = time.time() - tmux_vim._UNANSWERED_BACKOFF_SECONDS - 1
    os.utime(marker, (expired, expired))
    sent, error = _run(True, ACTING["goto_file"])
    assert error is None
    assert sent != []
    assert not marker.exists()


def _marker_at(offset_seconds: float, directory: bool = False) -> Path:
    marker = tmux_vim._unanswered_path("%2")
    marker.parent.mkdir(parents=True, exist_ok=True)
    if directory:
        # A mark that cannot be unlinked (a directory in its place).
        marker.mkdir()
        (marker / "inside").touch()
    else:
        marker.touch()
    stamp = time.time() + offset_seconds
    os.utime(marker, (stamp, stamp))
    return marker


def test_a_backoff_mark_from_the_future_is_no_backoff() -> None:
    """A clock set back (review of T6+T7, I2): a mark an hour ahead held
    the pane for an hour. It is removed, and the next navigation goes to
    Vim and lands."""
    marker = _marker_at(3600)
    sent, error = _run(True, ACTING["goto_file"])
    assert error is None
    assert sent != []
    assert not marker.exists()


def test_a_mark_within_the_clock_skew_still_backs_off() -> None:
    marker = _marker_at(1.0)
    sent, error = _run(True, ACTING["goto_file"])
    assert isinstance(error, NavigationFailed)
    assert sent == []
    assert marker.exists()


def test_a_backoff_mark_that_cannot_be_removed_never_breaks_a_navigation(
    caplog: pytest.LogCaptureFixture,
) -> None:
    _marker_at(3600, directory=True)
    sent, error = _run(True, ACTING["goto_file"])
    assert error is None
    assert sent != []
    removals = [record for record in caplog.records if "could not remove" in record.message]
    assert len(removals) == 1  # the backoff check; the landing's unlink is silent


@pytest.mark.parametrize(("verdict", "lands"), [("landed", True), ("elsewhere", False)])
def test_an_error_the_navigation_caught_is_logged_and_the_verdict_decides(
    verdict: str, lands: bool, caplog: pytest.LogCaptureFixture
) -> None:
    """s:goto catches any error (a user BufReadPost autocommand's) and
    s:landed writes it between the verdict and the token."""
    sent: list[str] = []
    landed = _vim(None, sent)

    def send_text(self: object, text: str) -> None:
        landed(self, text)  # type: ignore[operator]
        match = _LANDED.search(text)
        if match:
            answer = cache.CACHE_DIR / f"landed-{match.group(2)}.txt"
            answer.write_text(f"{verdict}\nVim(echoerr):x\n{match.group(1)}\n")

    with (
        patch.object(TmuxPane, "send_text", send_text),
        patch.object(TmuxPane, "send_key"),
        patch.object(TmuxPane, "pane_pid", return_value=None),
        patch.object(tmux_vim, "_LANDING_TIMEOUT_SECONDS", 0.5),
    ):
        follower = TmuxVimFollower(pane_id="%2")
        if lands:
            follower.goto_file("/tmp/t.py")
        else:
            with pytest.raises(NavigationFailed, match="it landed on another buffer"):
                follower.goto_file("/tmp/t.py")
    assert "navigating to /tmp/t.py, the follower's Vim raised: Vim(echoerr):x" in caplog.text


def test_distrust_without_a_record_or_with_a_garbled_one_does_nothing() -> None:
    assert tmux_vim._distrust_home("%2") is False
    record = cache.CACHE_DIR / "home-2"
    record.parent.mkdir(parents=True)
    record.write_text("garbled")
    assert tmux_vim._distrust_home("%2") is False
    assert record.read_text() == "garbled"


@pytest.mark.integration
def test_a_real_vims_landing_left_by_an_earlier_call_never_confirms_a_new_one(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The check answers "landed" only for the landing its own acting call
    recorded: an earlier call's leftovers (a garbled one never reached its
    `unlet!`) name another token, and an acting call forgets them first."""
    monkeypatch.setattr(cache, "CACHE_DIR", tmp_path / "cache")
    name, text = tmux_vim._vim_functions()
    script = tmux_vim._script_file(name, text)
    answer = cache.CACHE_DIR / "landed-2.txt"
    out = tmp_path / "out.txt"
    subprocess.run(
        [
            "vim",
            "-Nu",
            "NONE",
            "-i",
            "NONE",
            "-es",
            "-c",
            f"source {script}",
            "-c",
            "let g:vaf_p = '/x' | let g:vaf_k = 'p2-aaaaaa' | let g:vaf_landed = bufnr('%')",
            "-c",
            f"call {name}('landed', 'p2-bbbbbb')",
            "-c",
            f"call writefile(readfile('{answer}'), '{out}')",
            "-c",
            "let g:vaf_landed = bufnr('%') | let g:vaf_k = 'p2-cccccc'",
            # The handle is missing: the path reads as '', and the start
            # forgets the landing all the same.
            "-c",
            f"call {name}('acting_start', 'p2-cccccc')",
            "-c",
            f"call writefile(readfile('{out}') + [exists('g:vaf_landed') . ''], '{out}')",
            "-c",
            "qa!",
        ],
        check=True,
        timeout=30,
        stdin=subprocess.DEVNULL,
    )
    assert out.read_text().splitlines() == ["elsewhere", "p2-bbbbbb", "0"]


def test_the_readonly_token_is_a_six_digit_number_vim_reads_as_decimal() -> None:
    """The token is typed as a bare Vim number (a quoted one does not fit two
    screen rows with a six-digit pane id), and a Vim number with a leading 0
    is octal: the answer would carry back another number."""
    tokens = {tmux_vim._readonly_token() for _ in range(500)}
    assert all(100000 <= token <= 999999 for token in tokens)
    assert len(tokens) > 400
