"""TmuxVimFollower.probe_buffer: the one read-back this keystroke-driven
backend does — Vim dumps the buffer to a probe file and Python compares it
with the diff's base.

Vim is faked here by a send_text side effect that writes the probe file the
way `writefile()` would (every item followed by a newline). The real Vim
expression is exercised against a real tmux+vim in
tests/test_integration_edit_base_mismatch.py.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from unittest.mock import patch

import pytest
from helpers import resolve_typed_paths, typed_path

from vim_ai_follower import cache
from vim_ai_follower.backends import buffer_forms, tmux_vim
from vim_ai_follower.backends.tmux_vim import TmuxVimFollower

NONCE = "n0nce"


def _probe_line(path: str, probe: Path) -> str:
    """The exact Ex line probe_buffer sends, spelled out (not imported) so an
    unintended change to the constant is caught — same rule as
    test_tmux_vim._goto."""
    return (
        ":try | let g:vaf_p = " + typed_path(path) + " | if g:vaf_p !=# ''"
        " | let g:vaf_n = get(filter(range(1, bufnr('$')), 'bufexists(v:val)"
        " && fnamemodify(bufname(v:val), '':p'') ==# fnamemodify(g:vaf_p, '':p'')'), 0, -1)"
        f" | let g:vaf_r = writefile(getbufline(g:vaf_n, 1, '$') + ['{NONCE}'], '{probe}')"
        " | endif | catch | finally | unlet! g:vaf_h g:vaf_p g:vaf_n g:vaf_r | endtry"
    )


def _vim(writes: Callable[[Path], None] | None) -> Callable[..., None]:
    """A send_text side effect: when the probe line arrives, `writes` plays
    Vim and produces the probe file."""

    def _send_text(self: object, text: str) -> None:
        if "writefile" in text and writes is not None:
            writes(cache.CACHE_DIR / "probe-3.txt")

    return _send_text


def _dump(*items: str) -> Callable[[Path], None]:
    def _write(probe: Path) -> None:
        probe.write_bytes("".join(item + "\n" for item in items).encode())

    return _write


def _probe(writes: Callable[[Path], None] | None, content: str) -> tuple[str, list[str]]:
    sent: list[str] = []

    def _record(self: object, text: str) -> None:
        sent.append(resolve_typed_paths(text))
        _vim(writes)(self, text)

    with (
        patch("vim_ai_follower.backends.tmux_vim.TmuxPane.send_text", _record),
        patch("vim_ai_follower.backends.tmux_vim.TmuxPane.send_key"),
        patch("vim_ai_follower.backends.tmux_vim.secrets.token_hex", return_value=NONCE),
    ):
        held = TmuxVimFollower(pane_id="%3").probe_buffer("/w/f.py", content)
    return held, sent


def test_sends_the_probe_line_and_answers_holds_for_a_matching_buffer() -> None:
    held, sent = _probe(_dump("a", "b", NONCE), "a\nb\n")
    assert held == "holds"
    assert sent == [_probe_line("/w/f.py", cache.CACHE_DIR / "probe-3.txt")]


def test_a_buffer_holding_other_content_is_not_the_base() -> None:
    held, _ = _probe(_dump("a", "OLD", NONCE), "a\nb\n")
    assert held == "differs"


def test_an_unloaded_or_missing_buffer_is_absent() -> None:
    """getbufline() is empty for both, and a loaded buffer always has a line:
    the listed-but-unloaded case would load the finished file from disk if
    navigated to, so it holds no base at all — not even an empty file's."""
    held, _ = _probe(_dump(NONCE), "")
    assert held == "absent"


def test_an_empty_file_matches_the_single_empty_line_vim_probe() -> None:
    held, _ = _probe(_dump("", NONCE), "")
    assert held == "holds"


def test_a_crlf_file_matches_the_dos_buffer_vim_probe() -> None:
    """Vim loads a CRLF file with fileformat=dos: its lines carry no \\r."""
    held, _ = _probe(_dump("a", "b", NONCE), "a\r\nb\r\n")
    assert held == "holds"


def test_a_dump_from_an_older_probe_is_never_taken_as_the_answer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A probe that timed out can still land later: its nonce differs, so
    it is ignored and the wait runs out instead of reading the wrong answer."""
    monkeypatch.setattr(tmux_vim, "_PROBE_TIMEOUT_SECONDS", 0.05)
    held, _ = _probe(_dump("a", "b", "0ld"), "a\nb\n")
    assert held == "unknown"


def test_a_half_written_dump_is_not_an_answer(monkeypatch: pytest.MonkeyPatch) -> None:
    """Read mid-write, a dump of the buffer ["a", "b"] can look like ["a"]:
    only the closing nonce says the whole buffer has arrived."""
    monkeypatch.setattr(tmux_vim, "_PROBE_TIMEOUT_SECONDS", 0.05)
    held, _ = _probe(_dump("a", "b"), "a\n")
    assert held == "unknown"


def test_no_answer_within_the_timeout_is_unknown(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(tmux_vim, "_PROBE_TIMEOUT_SECONDS", 0.05)
    held, _ = _probe(None, "a\n")
    assert held == "unknown"


def test_a_stale_probe_file_is_removed_before_asking() -> None:
    """A leftover dump from an older, timed-out probe must not survive into
    this one's wait."""
    probe = cache.CACHE_DIR / "probe-3.txt"
    probe.parent.mkdir(parents=True, exist_ok=True)
    probe.write_text(f"stale\n{NONCE}\n")
    seen: list[bool] = []

    def _vim_writes(p: Path) -> None:
        seen.append(p.exists())
        _dump("a", NONCE)(p)

    held, _ = _probe(_vim_writes, "a\n")
    assert seen == [False]
    assert held == "holds"


def test_the_probe_file_is_removed_after_reading() -> None:
    _probe(_dump("a", NONCE), "a\n")
    assert not (cache.CACHE_DIR / "probe-3.txt").exists()


@pytest.mark.parametrize(
    ("content", "forms"),
    [
        ("", [[""]]),
        ("a", [["a"]]),
        ("a\n", [["a"]]),
        ("a\n\n", [["a", ""]]),
        ("a\r\nb\r\n", [["a\r", "b\r"], ["a", "b"]]),
        ("a\r\nb\n", [["a\r", "b"]]),
    ],
)
def test_buffer_forms(content: str, forms: list[list[str]]) -> None:
    assert buffer_forms(content) == forms
