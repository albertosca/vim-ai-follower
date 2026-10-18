"""Real tmux+vim proof that the tmux backend finds a buffer the user opened
under the OTHER Unicode normalization of the hook's path (T24c item 7).

macOS (APFS, HFS+, exFAT, FAT32 alike — measured 2026-10-18 on hdiutil images
of each) treats `Café.py` spelled with a precomposed é (NFC) and with `e` plus
a combining accent (NFD) as one file, but Vim keeps the bytes it was given:
`resolve()` and `:p` of either spelling stay that spelling. A user who typed
`:e Café.py` (NFC) while Claude's tools passed the NFD spelling, or the other
way round, had the probe say "absent" and every Edit fail safe with
NavigationFailed: never shown, marked stale. Python now hands Vim the other
spelling too (a second handle file), only when the filesystem says both
spellings are the same file, and the lookups (s:find, s:same_file) match
either.
"""

from __future__ import annotations

import logging
import os
import unicodedata
from collections.abc import Callable
from pathlib import Path
from typing import Literal

import pytest
from test_integration_same_file import _quick, _state, _tmux, _user_vim

from vim_ai_follower import hooks
from vim_ai_follower.backends.tmux_vim import TmuxVimFollower
from vim_ai_follower.diff import compute_edit_script
from vim_ai_follower.state import FollowerState

pytestmark = pytest.mark.integration

Form = Literal["NFC", "NFD"]

BEFORE = "a = 1\nb = 2\n"
AFTER = "a = 1\nc = 3\nb = 2\n"
NAME = "Café.py"


def _open_script(root: Path, path: Path) -> str:
    """Vim arguments that open `path` byte-exact: the name goes through a
    file, never through the shell (a C-locale readline may mangle bytes
    above 127)."""
    (root / "name.bin").write_bytes(os.fsencode(path))
    (root / "open.vim").write_text(
        "exe 'edit ' . fnameescape(join(readfile('name.bin', 'b'), \"\\n\"))\n"
    )
    return "-S open.vim"


@pytest.mark.parametrize(
    ("users_form", "hooks_form"), [("NFC", "NFD"), ("NFD", "NFC")], ids=["user-nfc", "user-nfd"]
)
def test_an_adopted_vims_buffer_under_the_other_normalization_is_the_hooks_file(
    tmux_session: str,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    wait_until: Callable[..., bool],
    caplog: pytest.LogCaptureFixture,
    users_form: Form,
    hooks_form: Form,
) -> None:
    root = Path(os.path.realpath(tmp_path))
    users_spelling = root / unicodedata.normalize(users_form, NAME)
    hooks_spelling = root / unicodedata.normalize(hooks_form, NAME)
    users_spelling.write_text(BEFORE)
    if not hooks_spelling.exists() or not users_spelling.samefile(hooks_spelling):
        pytest.skip("a normalization-sensitive filesystem: the spellings are two files")
    pane = _user_vim(tmux_session, monkeypatch, root, _open_script(root, users_spelling))
    window_id = _tmux("display-message", "-p", "-t", pane, "#{window_id}")
    FollowerState.set(window_id, backend="tmux", target=pane, adopted=True, speed="instant")
    follower = TmuxVimFollower(pane_id=pane, pace_seconds=0.0, window_id=window_id)
    file_path = hooks._file_path(
        {"tool_name": "Edit", "tool_input": {"file_path": str(hooks_spelling)}}
    )
    assert file_path == str(hooks_spelling)
    opened = _state(pane, tmp_path / "s0.txt", wait_until)
    assert os.fsencode(str(opened["BUF"])) == os.fsencode(str(users_spelling)), opened

    # The probe finds the user's buffer: not "absent".
    assert _quick(lambda: follower.probe_buffer(file_path, BEFORE)) == "holds"
    # An Edit lands on it and types there: no second buffer, no new tab.
    users_spelling.write_text(AFTER)
    _quick(lambda: follower.apply_edit(file_path, compute_edit_script(BEFORE, AFTER), BEFORE))
    edited = _state(pane, tmp_path / "s1.txt", wait_until)
    assert os.fsencode(str(edited["BUF"])) == os.fsencode(str(users_spelling)), edited
    assert edited["LINES"] == AFTER.splitlines(), edited
    assert edited["TABS"] == "1", edited
    assert len(str(edited["FILES"]).split(",")) == 1, edited
    # A Read shows the same buffer where it is.
    _quick(lambda: follower.ensure_showing(file_path))
    shown = _state(pane, tmp_path / "s2.txt", wait_until)
    assert os.fsencode(str(shown["BUF"])) == os.fsencode(str(users_spelling)), shown
    assert shown["TABS"] == "1", shown
    # The eviction finds it too: the user's own buffer is forgotten (its
    # verdict logged), not wiped and not missed ("absent" logs nothing).
    caplog.set_level(logging.INFO, logger="vim_ai_follower")
    _quick(lambda: follower.close_tab(file_path))
    assert "the user had it open" in caplog.text
    closed = _state(pane, tmp_path / "s3.txt", wait_until)
    assert closed["LINES"] == AFTER.splitlines(), closed
    assert _quick(lambda: follower.probe_buffer(file_path, AFTER)) == "holds"


def test_two_spellings_that_are_two_files_stay_apart(
    tmux_session: str,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    wait_until: Callable[..., bool],
) -> None:
    # Only a normalization-SENSITIVE filesystem (Linux ext4, for one) holds
    # both spellings as two files; no hdiutil image on macOS does (measured
    # 2026-10-18: APFS, HFS+, exFAT and FAT32 all take them for one file).
    root = Path(os.path.realpath(tmp_path))
    composed = root / unicodedata.normalize("NFC", NAME)
    decomposed = root / unicodedata.normalize("NFD", NAME)
    composed.write_text("composed\n")
    try:
        with decomposed.open("x") as stream:
            stream.write("decomposed\n")
    except FileExistsError:
        pytest.skip("a normalization-insensitive filesystem: the spellings are one file")
    pane = _user_vim(tmux_session, monkeypatch, root, _open_script(root, composed))
    window_id = _tmux("display-message", "-p", "-t", pane, "#{window_id}")
    FollowerState.set(window_id, backend="tmux", target=pane, adopted=True, speed="instant")
    follower = TmuxVimFollower(pane_id=pane, pace_seconds=0.0, window_id=window_id)

    assert _quick(lambda: follower.probe_buffer(str(decomposed), "decomposed\n")) == "absent"
    _quick(lambda: follower.ensure_showing(str(decomposed)))
    shown = _state(pane, tmp_path / "s1.txt", wait_until)
    assert shown["LINES"] == ["decomposed"], shown
    assert shown["TABS"] == "2", shown
