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
from vim_ai_follower.tmux import TmuxPane

# What _typed_path types for a handle named by its absolute path; `g:x` is
# the variable these tests read into.
_READ_TAIL = (
    r""" \| let g:x = filereadable\(g:vaf_h\) \? join\(readfile\(g:vaf_h,'b'\),"\\n"\) : ''"""
    r""" \| call delete\(g:vaf_h\)$"""
)
_HANDLE = re.compile(r"""^let g:vaf_h = '([^']*)'""" + _READ_TAIL)


def _read(pane_id: str, path: str) -> str:
    return tmux_vim._typed_path(pane_id, path, "g:x")


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
    expression = _read("%3", path)
    handle = _handle(expression)
    assert handle.parent == cache.CACHE_DIR
    assert re.fullmatch(r"p3-[0-9a-f]{6}", handle.name)
    assert handle.read_bytes() == os.fsencode(path)
    name = path.rsplit("/", 1)[1]
    assert name
    assert name not in expression


def test_every_call_gets_its_own_handle() -> None:
    """Vim runs a line some time after the send returns, so a shared file
    could be rewritten first: close_tab's eviction then show_fresh's
    pre-wipe would both wipe the NEW file."""
    first = _handle(_read("%3", "/tmp/evicted.py"))
    second = _handle(_read("%3", "/tmp/new.py"))
    assert first != second
    assert first.read_bytes() == b"/tmp/evicted.py"
    assert second.read_bytes() == b"/tmp/new.py"


def test_old_handles_of_every_pane_are_swept_and_nothing_else() -> None:
    """Vim deletes a handle as it reads it; the ones no Vim ever read (a
    pane that died, a Vim on another HOME) are swept by any pane's next
    call, never anything that is not a handle."""
    cache.CACHE_DIR.mkdir(parents=True)
    old = cache.CACHE_DIR / "p3-dead00"
    old.write_bytes(b"/tmp/old.py")
    recent = cache.CACHE_DIR / "p3-cafe00"
    recent.write_bytes(b"/tmp/recent.py")
    other_pane = cache.CACHE_DIR / "p33-dead00"
    other_pane.write_bytes(b"/tmp/other.py")
    unrelated = [
        cache.CACHE_DIR / name
        for name in ("probe-3.txt", "p3-notahandle", "home-3", "h3-dead00", "landed-3.txt")
    ]
    long_ago = time.time() - tmux_vim._PATH_HANDLE_MAX_AGE_SECONDS - 60
    for path in unrelated:
        path.write_bytes(b"x\n")
    for path in (old, other_pane, *unrelated):
        os.utime(path, (long_ago, long_ago))

    new = _handle(_read("%5", "/tmp/f.py"))

    assert not old.exists()
    assert not other_pane.exists()
    assert recent.exists()
    assert all(path.exists() for path in unrelated)
    assert new.exists()


def test_a_handle_another_hook_swept_first_is_not_an_error() -> None:
    cache.CACHE_DIR.mkdir(parents=True)
    ghost = cache.CACHE_DIR / "p3-dead00"
    with patch.object(Path, "glob", return_value=iter([ghost])):
        expression = _read("%3", "/tmp/f.py")
    assert _handle(expression).read_bytes() == b"/tmp/f.py"


def test_a_taken_handle_name_is_never_reused() -> None:
    cache.CACHE_DIR.mkdir(parents=True)
    taken = cache.CACHE_DIR / "p3-aaaaaa"
    taken.write_bytes(b"/tmp/someone-else.py")
    with patch(
        "vim_ai_follower.backends.tmux_vim.secrets.token_hex", side_effect=["aaaaaa", "bbbbbb"]
    ):
        handle = _handle(_read("%3", "/tmp/f.py"))
    assert handle.name == "p3-bbbbbb"
    assert taken.read_bytes() == b"/tmp/someone-else.py"


# --- HOME-relative cache paths (tmux_vim._cache_file / _vim_shares_home) ---

_HOME_HANDLE = re.compile(r"""^let g:vaf_h = expand\('~/([^']*)',1\)""" + _READ_TAIL)


@pytest.fixture
def home_cache(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """HOME a symlinked spelling of a temp dir (like the demo's), with the
    cache directory under it as in real use."""
    real = tmp_path / "real-home"
    real.mkdir()
    home = tmp_path / "home"
    home.symlink_to(real)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setattr(cache, "CACHE_DIR", home / ".cache" / "claude-vim-follower")
    return home


def _vim(creates_marker: bool, sent: list[str]) -> object:
    """A send_text double: Vim answering the HOME question (creating the
    marker through `~`, which in the same-HOME case is the hook's cache)."""

    def send_text(self: object, text: str) -> None:
        sent.append(text)
        match = re.search(r"expand\('~/([^']*)', 1\)\)", text)
        if creates_marker and match and "writefile([]" in text:
            (Path.home() / match.group(1)).write_text("")

    return send_text


def _typed(pane_pid: int | None, creates_marker: bool, sent: list[str], path: str) -> str:
    with (
        patch.object(TmuxPane, "pane_pid", return_value=pane_pid),
        patch.object(TmuxPane, "send_text", _vim(creates_marker, sent)),
        patch.object(TmuxPane, "send_key"),
        patch.object(tmux_vim, "_PROBE_TIMEOUT_SECONDS", 0.05),
    ):
        return _read("%3", path)


def test_a_vim_on_the_hooks_home_gets_the_handle_home_relative(home_cache: Path) -> None:
    sent: list[str] = []
    expression = _typed(4242, True, sent, "/tmp/f.py")
    match = _HOME_HANDLE.match(expression)
    assert match, expression
    assert match.group(1).startswith(".cache/claude-vim-follower/p3-")
    assert (home_cache / match.group(1)).read_bytes() == b"/tmp/f.py"
    # The question was asked once, HOME-relative too.
    assert len(sent) == 1
    assert sent[0].startswith(":try | let g:vaf_r = writefile([], expand('~/.cache/")
    assert str(home_cache) not in sent[0]
    assert os.path.realpath(home_cache) not in sent[0]
    # and is remembered for this Vim: no second question.
    assert _HOME_HANDLE.match(_typed(4242, True, sent, "/tmp/g.py"))
    assert len(sent) == 1


def test_a_vim_on_another_home_gets_the_absolute_handle(home_cache: Path) -> None:
    sent: list[str] = []
    expression = _typed(4242, False, sent, "/tmp/f.py")
    handle = _handle(expression)
    assert handle.parent == cache.CACHE_DIR
    assert handle.read_bytes() == b"/tmp/f.py"
    assert len(sent) == 1
    # Not asked again for the same Vim: the answer is the marker's absence.
    _handle(_typed(4242, False, sent, "/tmp/g.py"))
    assert len(sent) == 1


def test_an_answer_that_lands_after_the_wait_is_picked_up_later(home_cache: Path) -> None:
    sent: list[str] = []
    _handle(_typed(4242, False, sent, "/tmp/f.py"))
    marker = re.search(r"expand\('~/([^']*)'", sent[0])
    assert marker
    (Path.home() / marker.group(1)).write_text("")  # Vim got there late
    assert _HOME_HANDLE.match(_typed(4242, False, sent, "/tmp/g.py"))
    assert len(sent) == 1


def test_a_new_vim_in_the_pane_is_asked_again(home_cache: Path) -> None:
    sent: list[str] = []
    assert _HOME_HANDLE.match(_typed(4242, True, sent, "/tmp/f.py"))
    first_marker = re.search(r"expand\('~/([^']*)'", sent[0])
    assert first_marker
    # Another process in the pane, on another HOME.
    _handle(_typed(5151, False, sent, "/tmp/g.py"))
    assert len(sent) == 2
    assert not (Path.home() / first_marker.group(1)).exists()


def test_asking_again_clears_every_marker_of_the_pane_and_no_other(home_cache: Path) -> None:
    """Two hooks asking at once each leave a marker, and only the last one is
    recorded; the next question sweeps them all."""
    cache.CACHE_DIR.mkdir(parents=True)
    unrecorded = cache.CACHE_DIR / "h3-0a0a0a"
    unrecorded.write_text("")
    other_pane = cache.CACHE_DIR / "h33-0a0a0a"
    other_pane.write_text("")
    (cache.CACHE_DIR / "home-3").write_text("4242 h3-0b0b0b\n")
    (cache.CACHE_DIR / "h3-0b0b0b").write_text("")
    sent: list[str] = []
    assert _HOME_HANDLE.match(_typed(5151, True, sent, "/tmp/f.py"))
    assert len(sent) == 1
    asked = re.search(r"(h3-[0-9a-f]{6})", sent[0])
    assert asked
    assert {path.name for path in cache.CACHE_DIR.glob("h3*-*")} == {"h33-0a0a0a", asked.group(1)}


def test_a_cache_dir_outside_home_gets_the_absolute_handle(
    home_cache: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(cache, "CACHE_DIR", tmp_path / "elsewhere" / "cache")
    sent: list[str] = []
    handle = _handle(_typed(4242, True, sent, "/tmp/f.py"))
    assert handle.parent == tmp_path / "elsewhere" / "cache"
    assert sent == []


def test_a_pane_tmux_cannot_place_gets_the_absolute_handle(home_cache: Path) -> None:
    sent: list[str] = []
    _handle(_typed(None, True, sent, "/tmp/f.py"))
    assert sent == []


def test_a_cache_dir_expand_would_misread_gets_the_absolute_handle(
    home_cache: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """expand() also expands wildcards and `$NAME`, so only a plain cache path
    is typed through it."""
    monkeypatch.setattr(cache, "CACHE_DIR", home_cache / "a $b*" / "cache")
    sent: list[str] = []
    _handle(_typed(4242, True, sent, "/tmp/f.py"))
    assert sent == []


def test_a_cache_file_with_an_unsafe_name_is_typed_in_full(home_cache: Path) -> None:
    with patch.object(TmuxPane, "pane_pid") as pane_pid:
        expression = tmux_vim._cache_file("%3", cache.CACHE_DIR / "odd name*")
    assert expression == "'" + str(cache.CACHE_DIR / "odd name*") + "'"
    pane_pid.assert_not_called()


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
    expression = _read("%3", path)
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
            expression,
            "-c",
            f"call writefile(split(g:x, \"\\n\", 1), '{out}', 'b')",
            "-c",
            "qa!",
        ],
        check=True,
        timeout=30,
    )
    assert out.read_bytes() == os.fsencode(path)


@pytest.mark.integration
def test_a_real_vim_with_a_wildignore_over_the_cache_still_answers_and_reads(
    home_cache: Path, tmp_path: Path
) -> None:
    """expand() applies 'wildignore' unless told not to (nosuf): a user's
    `wildignore=*/.cache/*` would make both the HOME question and the
    HOME-relative handle expand to ''. The real Vim runs with the hook's HOME
    and such a wildignore, answers the question, and reads the handle back."""
    cache.CACHE_DIR.mkdir(parents=True)
    question = tmux_vim._HOME_CHECK.format(
        marker=tmux_vim._vim_string("~/.cache/claude-vim-follower/h3-0c0c0c")
    )
    with patch.object(tmux_vim, "_vim_shares_home", return_value=True):
        expression = _read("%3", "/tmp/it's here.py")
    assert "expand('~/" in expression, expression
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
            "set wildignore=*/.cache/*,h3-*,p3-*",
            "-c",
            question.removeprefix(":"),
            "-c",
            expression,
            "-c",
            f"call writefile(split(g:x, \"\\n\", 1), '{out}', 'b')",
            "-c",
            "qa!",
        ],
        check=True,
        timeout=30,
        env={**os.environ, "HOME": str(home_cache)},
    )
    assert (cache.CACHE_DIR / "h3-0c0c0c").exists()
    assert out.read_bytes() == b"/tmp/it's here.py"


@pytest.mark.integration
def test_a_real_vim_deletes_the_handle_it_read_and_reads_a_missing_one_as_empty(
    tmp_path: Path,
) -> None:
    """The handle goes on the line that reads it. A handle that is not there
    (swept, or named on a HOME this Vim does not have) reads as '' with no
    error at all: measured, readfile()'s E484 inside a one-line try aborts
    the rest of the line, catch included, and prompts at 49 columns."""
    cache.CACHE_DIR.mkdir(parents=True)
    read = _read("%3", "/tmp/f.py")
    (handle,) = cache.CACHE_DIR.iterdir()
    missing = _read("%3", "/tmp/g.py")
    (cache.CACHE_DIR / missing.split("'")[1]).unlink()
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
            read + " | let g:first = g:x",
            "-c",
            missing + " | let g:after = 'ran'",
            "-c",
            f"call writefile([g:first, '[' . g:x . ']', g:after]"
            f" + split(execute('messages'), \"\\n\"), '{out}')",
            "-c",
            "qa!",
        ],
        check=True,
        timeout=30,
        stdin=subprocess.DEVNULL,
    )
    lines = out.read_text().splitlines()
    assert lines[:3] == ["/tmp/f.py", "[]", "ran"]
    assert not any("E484" in line for line in lines), lines
    assert not handle.exists()


@pytest.mark.integration
def test_a_real_vim_wipes_nothing_when_the_wipes_handle_is_missing(tmp_path: Path) -> None:
    """A missing handle reads as '', and `fnamemodify('', ':p')` is the
    working directory with a trailing slash: exactly the `:p` of a buffer
    opened on `.`, which the eviction must not take for its target."""
    missing = tmux_vim._typed_path("%3", "/tmp/evicted.py", "g:vaf_wipe_name")
    _handle(missing.replace("g:vaf_wipe_name", "g:x")).unlink()
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
            "edit .",
            "-c",
            "let g:dot = bufnr('%')",
            "-c",
            tmux_vim._WIPE_BUFFER.format(read=missing, folds=0).removeprefix(":"),
            "-c",
            f"call writefile([bufexists(g:dot) . '', bufname(g:dot)], '{out}')",
            "-c",
            "qa!",
        ],
        check=True,
        timeout=30,
        stdin=subprocess.DEVNULL,
        cwd=tmp_path,
    )
    assert out.read_text().splitlines() == ["1", "."]


@pytest.mark.integration
def test_a_real_vims_probe_gives_no_answer_when_its_handle_is_missing(tmp_path: Path) -> None:
    """No answer is "unknown" (retype or leave alone); an answer for the path
    '' would be the empty list of a missing buffer, "absent"."""
    missing = tmux_vim._typed_path("%3", "/tmp/f.py", "g:vaf_p")
    Path(missing.split("'")[1]).unlink()
    probe = tmp_path / "probe.txt"
    line = tmux_vim._PROBE_BUFFER.format(
        read=missing, nonce="'n0nce'", probe=tmux_vim._vim_string(str(probe)), folds=0
    )
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
            line.removeprefix(":"),
            "-c",
            f"call writefile(['ran'] + split(execute('messages'), \"\\n\"), '{out}')",
            "-c",
            "qa!",
        ],
        check=True,
        timeout=30,
        stdin=subprocess.DEVNULL,
    )
    assert not probe.exists()
    assert out.read_text().splitlines()[0] == "ran"
    assert "E484" not in out.read_text()
