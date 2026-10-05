"""How the tmux backend's Vim gets a target's path, and its functions,
without either being spelled on the typed command line, which Vim echoes
(see tests/test_integration_cmdline_paths.py).

- The path: a one-shot handle file (tmux_vim._path_handle) that a call names
  and the script's s:read reads back and deletes.
- The functions: tmux_vim._VIM_SCRIPT, written to the cache directory
  (_script_file) and sourced by the define line (_define_line) only when the
  Vim lacks its dispatcher, named HOME-relative when the Vim shares the
  hook's HOME (_home_relative / _vim_shares_home).
"""

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
# Two screen rows at the follower pane's 49 columns, the typed `:` included.
# Measured on Vim 9.2 in a 49-column tmux pane (2026-10-05): `:` + 96
# characters fills both rows; `:` + 97 puts the cursor on a third.
MAXIMUM_LINE_LENGTH = 97


@pytest.mark.parametrize("path", HOSTILE)
def test_the_handle_holds_the_exact_bytes_and_the_call_does_not_spell_them(path: str) -> None:
    handle = tmux_vim._path_handle("%3", path)
    assert handle.parent == cache.CACHE_DIR
    assert re.fullmatch(r"p3-[0-9a-f]{6}", handle.name)
    assert handle.read_bytes() == os.fsencode(path)
    line = tmux_vim._call_line("goto", handle.name, 0)
    name = path.rsplit("/", 1)[1]
    assert name
    assert name not in line
    assert f"'{handle.name}'" in line


def test_every_call_gets_its_own_handle() -> None:
    """Vim runs a line some time after the send returns, so a shared file
    could be rewritten first: close_tab's eviction then show_fresh's
    pre-wipe would both wipe the NEW file."""
    first = tmux_vim._path_handle("%3", "/tmp/evicted.py")
    second = tmux_vim._path_handle("%3", "/tmp/new.py")
    assert first != second
    assert first.read_bytes() == b"/tmp/evicted.py"
    assert second.read_bytes() == b"/tmp/new.py"


def test_old_handles_of_every_pane_are_swept_and_nothing_else() -> None:
    """Vim deletes a handle as it reads it; the ones no Vim ever read (a
    pane that died, a Vim without the functions) are swept by any pane's
    next call, never anything that is not a handle."""
    cache.CACHE_DIR.mkdir(parents=True)
    old = cache.CACHE_DIR / "p3-dead00"
    old.write_bytes(b"/tmp/old.py")
    recent = cache.CACHE_DIR / "p3-cafe00"
    recent.write_bytes(b"/tmp/recent.py")
    other_pane = cache.CACHE_DIR / "p33-dead00"
    other_pane.write_bytes(b"/tmp/other.py")
    unrelated = [
        cache.CACHE_DIR / name
        for name in (
            "probe-3.txt",
            "p3-notahandle",
            "home-3",
            "h3-dead00",
            "landed-3.txt",
            "vaf-dead00.vim",
        )
    ]
    long_ago = time.time() - tmux_vim._PATH_HANDLE_MAX_AGE_SECONDS - 60
    for path in unrelated:
        path.write_bytes(b"x\n")
    for path in (old, other_pane, *unrelated):
        os.utime(path, (long_ago, long_ago))

    new = tmux_vim._path_handle("%5", "/tmp/f.py")

    assert not old.exists()
    assert not other_pane.exists()
    assert recent.exists()
    assert all(path.exists() for path in unrelated)
    assert new.exists()


def test_a_handle_another_hook_swept_first_is_not_an_error() -> None:
    cache.CACHE_DIR.mkdir(parents=True)
    ghost = cache.CACHE_DIR / "p3-dead00"
    with patch.object(Path, "glob", return_value=iter([ghost])):
        handle = tmux_vim._path_handle("%3", "/tmp/f.py")
    assert handle.read_bytes() == b"/tmp/f.py"


def test_a_taken_handle_name_is_never_reused() -> None:
    cache.CACHE_DIR.mkdir(parents=True)
    taken = cache.CACHE_DIR / "p3-aaaaaa"
    taken.write_bytes(b"/tmp/someone-else.py")
    with patch(
        "vim_ai_follower.backends.tmux_vim.secrets.token_hex", side_effect=["aaaaaa", "bbbbbb"]
    ):
        handle = tmux_vim._path_handle("%3", "/tmp/f.py")
    assert handle.name == "p3-bbbbbb"
    assert taken.read_bytes() == b"/tmp/someone-else.py"


# --- the script and the lines that call it ---


def test_the_dispatcher_is_named_after_the_script_and_its_cache_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A Vim holding an older version's functions (or another cache
    directory's) sources this one instead of calling it with the wrong
    arguments, so the name changes with the text."""
    name, text = tmux_vim._vim_functions()
    assert re.fullmatch(r"VafFollower_[0-9a-f]{6}", name)
    assert f"function! {name}(op, ...) abort" in text
    assert f"let s:dir = '{cache.CACHE_DIR}/'" in text
    monkeypatch.setattr(cache, "CACHE_DIR", tmp_path / "another")
    other, _ = tmux_vim._vim_functions()
    assert other != name
    monkeypatch.setattr(tmux_vim, "_VIM_SCRIPT", tmux_vim._VIM_SCRIPT + '" changed\n')
    assert tmux_vim._vim_functions()[0] not in (name, other)


def test_every_function_in_the_script_stops_at_its_first_error() -> None:
    """An error stops the rest of the function exactly as it stopped the
    rest of a typed line (measured: `abort`, with `finally` still run)."""
    text = tmux_vim._vim_functions()[1]
    definitions = [line for line in text.splitlines() if line.startswith("function")]
    assert len(definitions) >= 15
    assert all(line.startswith("function! ") and line.endswith(") abort") for line in definitions)
    # Only the dispatcher is global; the rest are script-local.
    assert [line for line in definitions if not line.startswith("function! s:")] == [
        f"function! {tmux_vim._vim_functions()[0]}(op, ...) abort"
    ]


def test_the_script_file_is_written_once_and_rewritten_when_it_differs() -> None:
    name, text = tmux_vim._vim_functions()
    path = tmux_vim._script_file(name, text)
    assert path == cache.CACHE_DIR / f"vaf-{name.removeprefix('VafFollower_')}.vim"
    assert path.read_text() == text
    written = path.stat().st_mtime_ns
    os.utime(path, ns=(written - 10**9, written - 10**9))
    assert tmux_vim._script_file(name, text) == path
    assert path.stat().st_mtime_ns == written - 10**9  # left alone
    path.write_text("garbage")
    tmux_vim._script_file(name, text)
    assert path.read_text() == text
    # The rewrite goes through a temporary file and a rename.
    assert [entry.name for entry in cache.CACHE_DIR.iterdir()] == [path.name]


def test_a_call_line_does_nothing_in_a_vim_without_the_functions() -> None:
    name, _ = tmux_vim._vim_functions()
    assert tmux_vim._call_line("goto", "p3-abcdef", 1) == (
        f":if exists('*{name}')|call {name}('goto','p3-abcdef',1)|endif"
    )


@pytest.mark.parametrize("pane", ["%3", "%999", "%1000", "%9999", "%99999", "%999999"])
def test_every_typed_call_fits_two_screen_rows(pane: str) -> None:
    """Up to a six-digit pane id (a long-lived tmux server's), the longest
    call (show_fresh's rename, whose handle carries the pane id) and the
    define line, HOME-relative, stay within two rows at 49 columns."""
    number = int(pane.lstrip("%"))
    handle = f"p{number}-abcdef"
    lines = [
        tmux_vim._call_line("rename", handle, 1, 1, 1),
        tmux_vim._call_line("goto", handle, 1),
        tmux_vim._call_line("probe", handle, 1),
        tmux_vim._call_line("landed", handle),
        tmux_vim._call_line("restore_readonly", number),
        tmux_vim._call_line("reload", 1, 1),
        tmux_vim._call_line("relock", 1, 1),
        tmux_vim._HOME_CHECK.format(
            marker=tmux_vim._vim_string(f"~/.cache/claude-vim-follower/h{number}-abcdef")
        ),
    ]
    with patch.object(
        tmux_vim, "_home_relative", return_value="~/.cache/claude-vim-follower/vaf-abcdef.vim"
    ):
        lines.append(tmux_vim._define_line(pane))
    too_long = [line for line in lines if len(line) > MAXIMUM_LINE_LENGTH]
    assert too_long == []


# --- HOME-relative names (tmux_vim._home_relative / _vim_shares_home) ---


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


def _define(pane_pid: int | None, creates_marker: bool, sent: list[str]) -> str:
    with (
        patch.object(TmuxPane, "pane_pid", return_value=pane_pid),
        patch.object(TmuxPane, "send_text", _vim(creates_marker, sent)),
        patch.object(TmuxPane, "send_key"),
        patch.object(tmux_vim, "_PROBE_TIMEOUT_SECONDS", 0.05),
    ):
        return tmux_vim._define_line("%3")


def _sourced(line: str) -> str:
    match = re.fullmatch(r":if !exists\('\*VafFollower_[0-9a-f]{6}'\)\|(.*)\|endif", line)
    assert match, line
    return match.group(1)


def test_a_vim_on_the_hooks_home_sources_the_script_home_relative(home_cache: Path) -> None:
    sent: list[str] = []
    line = _define(4242, True, sent)
    sourced = _sourced(line)
    assert re.fullmatch(r"sil! so ~/\.cache/claude-vim-follower/vaf-[0-9a-f]{6}\.vim", sourced)
    assert (home_cache / sourced.removeprefix("sil! so ~/")).read_text() == (
        tmux_vim._vim_functions()[1]
    )
    # The question was asked once, HOME-relative too, and silently.
    assert len(sent) == 1
    assert sent[0].startswith(":silent! call writefile([], expand('~/.cache/")
    assert str(home_cache) not in sent[0]
    assert os.path.realpath(home_cache) not in sent[0]
    # and is remembered for this Vim: no second question.
    assert _define(4242, True, sent) == line
    assert len(sent) == 1


def test_a_vim_on_another_home_sources_the_script_by_its_absolute_path(home_cache: Path) -> None:
    sent: list[str] = []
    sourced = _sourced(_define(4242, False, sent))
    # The cache path in full; `/tmp/…` is safe to type bare.
    assert sourced.startswith(f"sil! so {cache.CACHE_DIR}/vaf-")
    assert len(sent) == 1
    # Not asked again for the same Vim: the answer is the marker's absence.
    _define(4242, False, sent)
    assert len(sent) == 1


def test_an_absolute_path_with_unsafe_characters_goes_through_fnameescape(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`:source` would expand `$b`, `*` or a space-split in a bare name."""
    monkeypatch.setattr(cache, "CACHE_DIR", tmp_path / "a $b*" / "it's")
    with patch.object(TmuxPane, "pane_pid", return_value=None):
        sourced = _sourced(tmux_vim._define_line("%3"))
    name = tmux_vim._vim_functions()[0].removeprefix("VafFollower_")
    escaped = str(cache.CACHE_DIR / f"vaf-{name}.vim").replace("'", "''")
    assert sourced == f"exe 'sil! so ' . fnameescape('{escaped}')"


def test_an_answer_that_lands_after_the_wait_is_picked_up_later(home_cache: Path) -> None:
    sent: list[str] = []
    assert "sil! so /" in _define(4242, False, sent)
    marker = re.search(r"expand\('~/([^']*)'", sent[0])
    assert marker
    (Path.home() / marker.group(1)).write_text("")  # Vim got there late
    assert "sil! so ~/" in _define(4242, False, sent)
    assert len(sent) == 1


def test_a_new_vim_in_the_pane_is_asked_again(home_cache: Path) -> None:
    sent: list[str] = []
    assert "sil! so ~/" in _define(4242, True, sent)
    first_marker = re.search(r"expand\('~/([^']*)'", sent[0])
    assert first_marker
    # Another process in the pane, on another HOME.
    assert "sil! so /" in _define(5151, False, sent)
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
    assert "sil! so ~/" in _define(5151, True, sent)
    assert len(sent) == 1
    asked = re.search(r"(h3-[0-9a-f]{6})", sent[0])
    assert asked
    assert {path.name for path in cache.CACHE_DIR.glob("h3*-*")} == {"h33-0a0a0a", asked.group(1)}


def test_a_cache_dir_outside_home_is_named_in_full_without_asking(
    home_cache: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(cache, "CACHE_DIR", tmp_path / "elsewhere" / "cache")
    sent: list[str] = []
    assert _sourced(_define(4242, True, sent)).startswith(f"sil! so {tmp_path}/elsewhere/cache/")
    assert sent == []


def test_a_pane_tmux_cannot_place_is_named_in_full(home_cache: Path) -> None:
    sent: list[str] = []
    assert "sil! so /" in _define(None, True, sent)
    assert sent == []


def test_a_cache_dir_expand_would_misread_is_named_in_full_without_asking(
    home_cache: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """expand() and `:source` also expand wildcards and `$NAME`, so only a
    plain cache path is typed through `~`."""
    monkeypatch.setattr(cache, "CACHE_DIR", home_cache / "a $b*" / "cache")
    sent: list[str] = []
    assert "fnameescape(" in _define(4242, True, sent)
    assert sent == []


def test_a_cache_file_with_an_unsafe_name_is_never_home_relative(home_cache: Path) -> None:
    with patch.object(TmuxPane, "pane_pid") as pane_pid:
        assert tmux_vim._home_relative("%3", cache.CACHE_DIR / "odd name*") is None
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
    assert any(re.search(r"'p2-[0-9a-f]{6}'", text) for text in typed)
    assert not any("/tmp/proj" in text for text in typed)


# --- the script in a real Vim ---


def _real_vim(*commands: str, home: Path | None = None, cwd: Path | None = None) -> None:
    """A real Vim, no vimrc, running `commands` with the script sourced."""
    name, text = tmux_vim._vim_functions()
    script = tmux_vim._script_file(name, text)
    environment = {**os.environ, "HOME": str(home)} if home else None
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
            *[argument for command in commands for argument in ("-c", command)],
            "-c",
            "qa!",
        ],
        check=True,
        timeout=30,
        stdin=subprocess.DEVNULL,
        env=environment,
        cwd=cwd,
    )


@pytest.mark.integration
@pytest.mark.parametrize("path", HOSTILE)
def test_a_real_vim_reads_the_handle_back_to_the_exact_path(path: str, tmp_path: Path) -> None:
    """s:read gives back the path's exact bytes: written out split on
    newlines, which writefile() joins with newlines again, so the dump is
    byte-for-byte the string."""
    handle = tmux_vim._path_handle("%3", path)
    name = tmux_vim._vim_functions()[0]
    out = tmp_path / "out.bin"
    _real_vim(
        f"let g:x = {name}('read', '{handle.name}')",
        f"call writefile(split(g:x, \"\\n\", 1), '{out}', 'b')",
    )
    assert out.read_bytes() == os.fsencode(path)


@pytest.mark.integration
def test_a_real_vim_with_a_wildignore_over_the_cache_still_answers_and_sources(
    home_cache: Path, tmp_path: Path
) -> None:
    """expand() applies 'wildignore' unless told not to (nosuf): a user's
    `wildignore=*/.cache/*` would make the HOME question expand to ''. The
    define line's `:source ~/…` is not subject to it (measured 2026-10-05).
    The real Vim runs with the hook's HOME and such a wildignore, answers
    the question, sources the script through the define line and reads a
    handle back."""
    cache.CACHE_DIR.mkdir(parents=True)
    question = tmux_vim._HOME_CHECK.format(
        marker=tmux_vim._vim_string("~/.cache/claude-vim-follower/h3-0c0c0c")
    )
    with patch.object(tmux_vim, "_vim_shares_home", return_value=True):
        define = tmux_vim._define_line("%3")
    assert "|sil! so ~/" in define, define
    handle = tmux_vim._path_handle("%3", "/tmp/it's here.py")
    name = tmux_vim._vim_functions()[0]
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
            "set wildignore=*/.cache/*,h3-*,p3-*,vaf-*",
            "-c",
            question.removeprefix(":"),
            "-c",
            define.removeprefix(":"),
            "-c",
            f"let g:x = {name}('read', '{handle.name}')",
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
    """The handle goes in the call that reads it. A handle that is not there
    (swept, or never written for this Vim) reads as '' with no error."""
    read = tmux_vim._path_handle("%3", "/tmp/f.py")
    missing = tmux_vim._path_handle("%3", "/tmp/g.py")
    missing.unlink()
    name = tmux_vim._vim_functions()[0]
    out = tmp_path / "out.txt"
    _real_vim(
        f"let g:first = {name}('read', '{read.name}')",
        f"let g:x = {name}('read', '{missing.name}') | let g:after = 'ran'",
        f"call writefile([g:first, '[' . g:x . ']', g:after]"
        f" + split(execute('messages'), \"\\n\"), '{out}')",
    )
    lines = out.read_text().splitlines()
    assert lines[:3] == ["/tmp/f.py", "[]", "ran"]
    assert not any("E484" in line for line in lines), lines
    assert not read.exists()


@pytest.mark.integration
def test_a_real_vim_wipes_nothing_when_the_wipes_handle_is_missing(tmp_path: Path) -> None:
    """A missing handle reads as '', and `fnamemodify('', ':p')` is the
    working directory with a trailing slash: exactly the `:p` of a buffer
    opened on `.`, which the eviction must not take for its target."""
    missing = tmux_vim._path_handle("%3", "/tmp/evicted.py")
    missing.unlink()
    name = tmux_vim._vim_functions()[0]
    out = tmp_path / "out.txt"
    _real_vim(
        "edit .",
        "let g:dot = bufnr('%')",
        tmux_vim._call_line("wipe", missing.name, 0).removeprefix(":"),
        f"call writefile([bufexists(g:dot) . '', bufname(g:dot), exists('*{name}') . ''], '{out}')",
        cwd=tmp_path,
    )
    assert out.read_text().splitlines() == ["1", ".", "1"]


@pytest.mark.integration
def test_a_real_vims_probe_gives_no_answer_when_its_handle_is_missing(tmp_path: Path) -> None:
    """No answer is "unknown" (retype or leave alone); an answer for the path
    '' would be the empty list of a missing buffer, "absent"."""
    missing = tmux_vim._path_handle("%3", "/tmp/f.py")
    missing.unlink()
    probe = cache.CACHE_DIR / "probe-3.txt"
    out = tmp_path / "out.txt"
    _real_vim(
        tmux_vim._call_line("probe", missing.name, 0).removeprefix(":"),
        f"call writefile(['ran'] + split(execute('messages'), \"\\n\"), '{out}')",
    )
    assert not probe.exists()
    assert out.read_text().splitlines()[0] == "ran"
    assert "E484" not in out.read_text()


@pytest.mark.integration
def test_a_real_vim_drops_the_dispatchers_of_other_versions_and_nothing_else(
    tmp_path: Path,
) -> None:
    """A long-lived Vim that sourced an older version keeps only the
    current dispatcher. A user function stays, even one named exactly like
    a dispatcher (`VafFollower_` + 6 hex): only a function set from the
    follower's own script for that hash (`vaf-<hash>.vim`) is ours."""
    name = tmux_vim._vim_functions()[0]
    older = tmp_path / "old-cache" / "vaf-0a0b0c.vim"
    older.parent.mkdir()
    older.write_text("function! VafFollower_0a0b0c(op, ...) abort\nendfunction\n")
    user = tmp_path / "user.vim"
    user.write_text(
        "function! VafFollower_mine() abort\nendfunction\n"
        # The exact shape of a dispatcher's name, but the user's own.
        "function! VafFollower_123abc() abort\nendfunction\n"
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
            f"source {older}",
            "-c",
            f"source {user}",
            "-c",
            f"source {tmux_vim._script_file(name, tmux_vim._vim_functions()[1])}",
            "-c",
            "call writefile(map(['VafFollower_0a0b0c', 'VafFollower_mine',"
            f" 'VafFollower_123abc', '{name}'],"
            f" {{_, function_name -> exists('*' . function_name) . ''}}), '{out}')",
            "-c",
            "qa!",
        ],
        check=True,
        timeout=30,
        stdin=subprocess.DEVNULL,
    )
    assert out.read_text().splitlines() == ["0", "1", "1", "1"]
