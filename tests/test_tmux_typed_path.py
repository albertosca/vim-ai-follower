"""tmux_vim._typed_path: the target's path reaches the tmux backend's Vim
through a one-shot handle file instead of being spelled on the typed command
line, which Vim echoes (see tests/test_integration_cmdline_paths.py)."""

from __future__ import annotations

import os
import re
import subprocess
import time
from pathlib import Path
from unittest.mock import patch

import pytest

from vim_ai_follower import cache
from vim_ai_follower.backends import tmux_vim
from vim_ai_follower.backends.tmux_vim import TmuxVimFollower

_HANDLE = re.compile(r"""^join\(readfile\('([^']*)', 'b'\), "\\n"\)$""")

# Every byte a Vim string literal or a line-oriented read could mangle: a
# quote, a backslash, a newline, a CR, a trailing newline, a byte that is not
# UTF-8 (surrogateescape, as os.fsdecode hands it over), pattern characters.
HOSTILE = (
    '/tmp/it\'s \\ "q".py',
    "/tmp/two\nlines.py",
    "/tmp/cr\r.py",
    "/tmp/ends-in-newline\n",
    os.fsdecode(b"/tmp/latin1-\xe9.py"),
    "/tmp/app/[slug]/{a,b} %#$HOME.py",
    "/tmp/ünïcødé/ñ.py",
)


def _handle(expression: str) -> Path:
    match = _HANDLE.match(expression)
    assert match, expression
    return Path(match.group(1))


@pytest.mark.parametrize("path", HOSTILE)
def test_the_handle_holds_the_exact_bytes_and_the_line_does_not_spell_them(path: str) -> None:
    expression = tmux_vim._typed_path("%3", path)
    handle = _handle(expression)
    assert handle.parent == cache.CACHE_DIR
    assert handle.name.startswith("path-3-")
    assert handle.read_bytes() == os.fsencode(path)
    name = path.rsplit("/", 1)[1]
    assert name
    assert name not in expression


def test_every_call_gets_its_own_handle() -> None:
    """Vim runs a line some time after the send returns, so a shared file
    could be rewritten first: close_tab's eviction then show_fresh's
    pre-wipe would both wipe the NEW file."""
    first = _handle(tmux_vim._typed_path("%3", "/tmp/evicted.py"))
    second = _handle(tmux_vim._typed_path("%3", "/tmp/new.py"))
    assert first != second
    assert first.read_bytes() == b"/tmp/evicted.py"
    assert second.read_bytes() == b"/tmp/new.py"


def test_old_handles_of_the_same_pane_are_swept_and_nothing_else() -> None:
    cache.CACHE_DIR.mkdir(parents=True)
    old = cache.CACHE_DIR / "path-3-deadbeef"
    old.write_bytes(b"/tmp/old.py")
    recent = cache.CACHE_DIR / "path-3-cafebabe"
    recent.write_bytes(b"/tmp/recent.py")
    other_pane = cache.CACHE_DIR / "path-33-deadbeef"
    other_pane.write_bytes(b"/tmp/other.py")
    unrelated = cache.CACHE_DIR / "probe-3.txt"
    unrelated.write_bytes(b"x\n")
    long_ago = time.time() - tmux_vim._PATH_HANDLE_MAX_AGE_SECONDS - 60
    for path in (old, other_pane, unrelated):
        os.utime(path, (long_ago, long_ago))

    new = _handle(tmux_vim._typed_path("%3", "/tmp/f.py"))

    assert not old.exists()
    assert recent.exists()
    assert other_pane.exists()
    assert unrelated.exists()
    assert new.exists()


def test_a_handle_another_hook_swept_first_is_not_an_error() -> None:
    cache.CACHE_DIR.mkdir(parents=True)
    ghost = cache.CACHE_DIR / "path-3-deadbeef"
    with patch.object(Path, "glob", return_value=iter([ghost])):
        expression = tmux_vim._typed_path("%3", "/tmp/f.py")
    assert _handle(expression).read_bytes() == b"/tmp/f.py"


@pytest.mark.parametrize(
    "call",
    [
        lambda follower: follower.goto_file("/tmp/proj/secret.py"),
        lambda follower: follower.close_tab("/tmp/proj/secret.py"),
        lambda follower: follower.ensure_showing("/tmp/proj/secret.py"),
        lambda follower: follower.show_fresh("/tmp/proj/secret.py", "x\n"),
        lambda follower: follower.probe_buffer("/tmp/proj/secret.py", "x\n"),
    ],
    ids=["goto_file", "close_tab", "ensure_showing", "show_fresh", "probe_buffer"],
)
def test_no_sent_line_spells_the_target_path(call: object) -> None:
    follower = TmuxVimFollower(pane_id="%2")
    with (
        patch("vim_ai_follower.tmux.subprocess.run") as run,
        patch("vim_ai_follower.control.check_signal", return_value=None),
        patch.object(tmux_vim, "_PROBE_TIMEOUT_SECONDS", 0.0),
    ):
        call(follower)  # type: ignore[operator]
    typed = [sent.args[0][6] for sent in run.call_args_list if "-l" in sent.args[0]]
    assert any("readfile(" in text for text in typed)
    assert not any("/tmp/proj" in text for text in typed)


@pytest.mark.integration
@pytest.mark.parametrize("path", HOSTILE)
def test_a_real_vim_reads_the_handle_back_to_the_exact_path(path: str, tmp_path: Path) -> None:
    """The expression evaluated by a real Vim gives back the path's exact
    bytes: written out split on newlines, which writefile() joins with
    newlines again, so the dump is byte-for-byte the string."""
    expression = tmux_vim._typed_path("%3", path)
    out = tmp_path / "out.bin"
    subprocess.run(
        [
            "vim",
            "-Nu",
            "NONE",
            "-i",
            "NONE",
            "-es",
            "-c",
            f"call writefile(split({expression}, \"\\n\", 1), '{out}', 'b')",
            "-c",
            "qa!",
        ],
        check=True,
        timeout=30,
    )
    assert out.read_bytes() == os.fsencode(path)
