"""Unit coverage for the byte-offset contract nvim's API imposes on the
per-character typing loop.

nvim_buf_set_text and nvim_win_set_cursor take BYTE columns, while the loop
walks the line by code point. On a pure-ASCII line the two coincide, which is
why this never showed up before; on the first multi-byte character they
diverge by (len(utf-8 bytes) - 1) and every later insert lands at the wrong
place, splitting the character's bytes across the row. Measured against real
nvim (2026-09-21): `alpha — beta` landed as b'alpha \\xe2 beta\\x80\\x94'.

These tests pin the column SEQUENCE with a mocked Nvim — a real-nvim
counterpart lives in test_nvim_integration_utf8.py. The character index stays
the loop's own currency (pace/signal bookkeeping, partial remainders); only
what crosses the API is converted to bytes.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from unittest.mock import MagicMock, call, patch

import pytest

from vim_ai_follower.animate import AnimationResult
from vim_ai_follower.backends.nvim import _animate_lines

# "aé—b": 1 + 2 + 3 + 1 bytes. Deliberately one character from each UTF-8
# width class below 4 bytes, in an order where a code-point index and a byte
# index disagree from the second character on.
_MIXED = "aé—b"


@pytest.fixture(autouse=True)
def _no_sleep() -> Iterator[None]:
    with patch("vim_ai_follower.backends.nvim.time.sleep"):
        yield


def _insert_columns(nvim: MagicMock) -> list[int]:
    """The start column of every buf_set_text call, in order."""
    return [c.args[2] for c in nvim.api.buf_set_text.call_args_list]


def _cursor_columns(nvim: MagicMock) -> list[int]:
    """The column of every win_set_cursor call, in order."""
    return [c.args[1][1] for c in nvim.api.win_set_cursor.call_args_list]


def test_typing_inserts_each_character_at_its_byte_column() -> None:
    nvim = MagicMock()
    with patch("vim_ai_follower.control.check_signal", return_value=None):
        result = _animate_lines(nvim, 7, (_MIXED,), 0, lambda: 0.05, "@1", ns=3)
    assert result == AnimationResult("completed", 1)
    # 0, 1, 3, 6 — byte offsets. The buggy version produced the code-point
    # sequence 0, 1, 2, 3, which splits é and — across the row.
    assert nvim.api.buf_set_text.call_args_list == [
        call(7, 0, 0, 0, 0, ["a"]),
        call(7, 0, 1, 0, 1, ["é"]),
        call(7, 0, 3, 0, 3, ["—"]),
        call(7, 0, 6, 0, 6, ["b"]),
    ]


def test_typing_inserts_an_astral_character_at_its_four_byte_width() -> None:
    # A 4-byte code point (emoji, outside the BMP) is one Python character
    # but four bytes; the next insert must clear all four.
    nvim = MagicMock()
    with patch("vim_ai_follower.control.check_signal", return_value=None):
        _animate_lines(nvim, 7, ("\U0001f600x",), 0, lambda: 0.05, "@1", ns=3)
    assert _insert_columns(nvim) == [0, 4]


def test_typing_a_combining_sequence_advances_per_code_point() -> None:
    # A combining mark is its own code point typed after its base, so the
    # columns simply accumulate its own byte width — no grapheme clustering
    # needed, and nothing here may raise. Written as an escape on purpose:
    # "e" + U+0301 renders identically to a precomposed "é", so a literal
    # here could be normalised away and silently turn this into a copy of
    # the test above. The length assert is the guard that it did not.
    nvim = MagicMock()
    decomposed = "e\u0301x"
    assert len(decomposed) == 3  # 3 code points, 4 bytes
    with patch("vim_ai_follower.control.check_signal", return_value=None):
        _animate_lines(nvim, 7, (decomposed,), 0, lambda: 0.05, "@1", ns=3)
    assert _insert_columns(nvim) == [0, 1, 3]


def test_cursor_follows_the_typed_prefix_in_byte_columns() -> None:
    # nvim_win_set_cursor's column is byte-based too: after each character the
    # cursor sits at the byte length of everything typed so far.
    nvim = MagicMock()
    with patch("vim_ai_follower.control.check_signal", return_value=None):
        _animate_lines(nvim, 7, (_MIXED,), 4, lambda: 0.05, "@1", ns=3)
    assert _cursor_columns(nvim) == [1, 3, 6, 7]
    # row is 1-indexed for the cursor and unchanged by the byte conversion
    assert {c.args[1][0] for c in nvim.api.win_set_cursor.call_args_list} == {5}


def test_each_line_restarts_the_byte_column_at_zero() -> None:
    # The byte offset is per-line state, never carried across the line
    # boundary — a multi-byte line must not offset the next line's first
    # insert.
    nvim = MagicMock()
    with patch("vim_ai_follower.control.check_signal", return_value=None):
        _animate_lines(nvim, 7, ("é", "é"), 0, lambda: 0.05, "@1", ns=3)
    assert nvim.api.buf_set_text.call_args_list == [
        call(7, 0, 0, 0, 0, ["é"]),
        call(7, 1, 0, 1, 0, ["é"]),
    ]


def test_pace_zero_whole_line_insert_stays_at_column_zero() -> None:
    # The pace-0 catch-up inserts the whole line at column 0 — already
    # byte-safe (0 is 0 in both currencies), and it must stay a single call.
    nvim = MagicMock()
    with patch("vim_ai_follower.control.check_signal", return_value=None):
        _animate_lines(nvim, 7, (_MIXED,), 2, lambda: 0.0, "@1", ns=1)
    nvim.api.buf_set_text.assert_called_once_with(7, 2, 0, 2, 0, [_MIXED])


def test_interrupt_after_a_multi_byte_character_leaves_a_whole_prefix() -> None:
    # Interrupt before the 4th character: the row holds exactly "aé—", every
    # insert at its byte column. Stopping one character LATER than the first
    # multi-byte one is what makes this discriminate — a stop after just
    # "aé" lands at the same columns in both currencies and would pass
    # against the bug. Nothing may be inserted after the stop, so the buffer
    # the user takes over is valid UTF-8, never a split character.
    nvim = MagicMock()
    # calls: line boundary, char0, char1, char2, char3 -> interrupt on the 5th
    signals = [None, None, None, None, "interrupt"]
    with patch("vim_ai_follower.control.check_signal", side_effect=signals):
        result = _animate_lines(nvim, 7, (_MIXED,), 0, lambda: 0.05, "@1", ns=3)
    assert result == AnimationResult("interrupted", 0)
    assert nvim.api.buf_set_text.call_args_list == [
        call(7, 0, 0, 0, 0, ["a"]),
        call(7, 0, 1, 0, 1, ["é"]),
        call(7, 0, 3, 0, 3, ["—"]),
    ]


def test_pause_resumes_at_the_same_byte_column(tmp_path: Path) -> None:
    # A pause blocks in place and resumes retyping FROM THE SAME character,
    # so the byte offset must survive the wait untouched — neither reset to
    # the character index nor advanced by the blocked iteration.
    nvim = MagicMock()
    base_dir = tmp_path / "cache"
    # calls: line boundary, char0, char1, then pause (twice: the trigger and
    # the toggle that _wait_while_paused reads as "resume"), then the rest.
    signals = [None, None, None, "pause", "pause", None, None]
    with patch("vim_ai_follower.control.check_signal", side_effect=signals):
        result = _animate_lines(nvim, 7, (_MIXED,), 0, lambda: 0.05, "@1", ns=3, base_dir=base_dir)
    assert result == AnimationResult("completed", 1)
    assert _insert_columns(nvim) == [0, 1, 3, 6]
