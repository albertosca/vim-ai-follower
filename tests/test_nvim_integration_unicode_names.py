"""Real-nvim twin of tests/test_integration_unicode_names.py (T24c item 7):
an ADOPTED nvim whose user opened `Café.py` under one Unicode normalization,
and a hook that passes the other.

Measured 2026-10-18 (nvim 0.12, macOS APFS): nvim, like Vim, keeps the
spelling it was given as the buffer's name, but _buffer_number compares
`vim.uv.fs_realpath` of both sides, and macOS realpath(3) answers the
on-disk spelling for either (Python's os.path.realpath does not). So an
EXISTING file is found under either spelling already, with no change on this
backend; these tests pin it. A file that does not exist yet has no realpath
and is not matched across spellings (on either backend: the filesystem
cannot say the two spellings are one file before there is a file).
"""

from __future__ import annotations

import os
import unicodedata
from pathlib import Path
from typing import Any, Literal

import pytest

pynvim = pytest.importorskip("pynvim")

from vim_ai_follower import hooks  # noqa: E402
from vim_ai_follower.backends.nvim import NvimFollower  # noqa: E402
from vim_ai_follower.diff import compute_edit_script  # noqa: E402
from vim_ai_follower.state import FollowerState  # noqa: E402

pytestmark = pytest.mark.integration

Form = Literal["NFC", "NFD"]

BEFORE = "a = 1\nb = 2\n"
AFTER = "a = 1\nc = 3\nb = 2\n"
NAME = "Café.py"
_WINDOW = "term-unicode"


def _adopted(headless_nvim: str, users_spelling: Path) -> tuple[Any, NvimFollower]:
    nvim = pynvim.attach("socket", path=headless_nvim)
    nvim.command("edit " + nvim.call("fnameescape", str(users_spelling)))
    FollowerState.set(_WINDOW, backend="nvim", target=headless_nvim, adopted=True, speed="instant")
    return nvim, NvimFollower(socket_path=headless_nvim, window_id=_WINDOW, pace_seconds=0.0)


def _spellings(tmp_path: Path, users_form: Form, hooks_form: Form) -> tuple[Path, Path]:
    root = Path(os.path.realpath(tmp_path))
    return (
        root / unicodedata.normalize(users_form, NAME),
        root / unicodedata.normalize(hooks_form, NAME),
    )


_FORMS = pytest.mark.parametrize(
    ("users_form", "hooks_form"), [("NFC", "NFD"), ("NFD", "NFC")], ids=["user-nfc", "user-nfd"]
)


@_FORMS
def test_an_adopted_nvims_buffer_under_the_other_normalization_is_the_hooks_file(
    headless_nvim: str, tmp_path: Path, users_form: Form, hooks_form: Form
) -> None:
    users_spelling, hooks_spelling = _spellings(tmp_path, users_form, hooks_form)
    users_spelling.write_text(BEFORE)
    if not hooks_spelling.exists() or not users_spelling.samefile(hooks_spelling):
        pytest.skip("a normalization-sensitive filesystem: the spellings are two files")
    nvim, follower = _adopted(headless_nvim, users_spelling)
    users_buffer = nvim.api.get_current_buf()
    file_path = hooks._file_path(
        {"tool_name": "Edit", "tool_input": {"file_path": str(hooks_spelling)}}
    )
    assert file_path == str(hooks_spelling)

    assert follower.probe_buffer(file_path, BEFORE) == "holds"
    users_spelling.write_text(AFTER)
    follower.apply_edit(file_path, compute_edit_script(BEFORE, AFTER), BEFORE)
    assert nvim.api.get_current_buf() == users_buffer
    assert nvim.api.buf_get_lines(users_buffer, 0, -1, True) == AFTER.splitlines()
    assert len(nvim.api.list_tabpages()) == 1
    assert [buffer.handle for buffer in nvim.buffers if buffer.name] == [users_buffer.handle]
    follower.ensure_showing(file_path)
    assert nvim.api.get_current_buf() == users_buffer
    assert len(nvim.api.list_tabpages()) == 1
    nvim.close()


@pytest.mark.parametrize("users_typing", ["", "mine"], ids=["empty", "typed"])
def test_a_buffer_named_in_nfc_for_a_new_file_is_found_by_the_nfd_hook_path(
    headless_nvim: str, tmp_path: Path, users_typing: str
) -> None:
    # The user `:e Café.py` (NFC) before the file exists; Claude's Write
    # names it NFD. Only the post hook asks nvim anything, and by then the
    # file exists, so both realpaths answer the on-disk spelling.
    probe = Path(os.path.realpath(tmp_path)) / "probe"
    probe.mkdir()
    (probe / unicodedata.normalize("NFC", NAME)).write_text("")
    if not (probe / unicodedata.normalize("NFD", NAME)).exists():
        pytest.skip("a normalization-sensitive filesystem: the spellings are two files")
    users_spelling, hooks_spelling = _spellings(tmp_path, "NFC", "NFD")
    nvim, follower = _adopted(headless_nvim, users_spelling)
    users_buffer = nvim.api.get_current_buf()
    if users_typing:
        nvim.api.buf_set_lines(users_buffer, 0, -1, True, [users_typing])
    # Before the file exists there is no realpath to match: "absent", and
    # no hook asks at that point.
    assert follower.probe_buffer(str(hooks_spelling), "") == "absent"

    hooks_spelling.write_text(AFTER)

    if users_typing:
        assert follower.probe_buffer(str(hooks_spelling), "") == "differs"
        assert nvim.api.buf_get_lines(users_buffer, 0, -1, True) == [users_typing]
        nvim.close()
        return
    assert follower.probe_buffer(str(hooks_spelling), "") == "holds"
    follower.apply_edit(str(hooks_spelling), compute_edit_script("", AFTER), "")
    assert nvim.api.get_current_buf() == users_buffer
    assert nvim.api.buf_get_lines(users_buffer, 0, -1, True) == AFTER.splitlines()
    assert len(nvim.api.list_tabpages()) == 1
    nvim.close()
