"""Shared test doubles for the CLI-level test modules."""

from __future__ import annotations

import os
import re
from collections.abc import Callable
from pathlib import Path
from unittest.mock import MagicMock, patch

from vim_ai_follower import state


def make_mock_tmux_run(
    window_id: str = "@1",
    pane_id: str = "%2",
    pane_exists: bool = True,
    other_panes: tuple[str, ...] = (),
    vim_panes: tuple[str, ...] = (),
) -> Callable[..., MagicMock]:
    """Factory for a subprocess.run side_effect impersonating a tmux server
    with one vim follower pane (pane_id) plus optional shell panes
    (other_panes, listed as zsh) and optional other live vim panes
    (vim_panes, listed as vim — e.g. another window's live follower)."""

    def _run(cmd: list[str], **kwargs: object) -> MagicMock:
        result = MagicMock()
        result.returncode = 0
        if cmd[:3] == ["tmux", "list-panes", "-a"]:
            lines = [f"{pane} zsh" for pane in other_panes]
            lines.extend(f"{pane} vim" for pane in vim_panes)
            if pane_exists:
                lines.append(f"{pane_id} vim")
            result.stdout = "".join(line + "\n" for line in lines)
        elif cmd[:2] == ["tmux", "display-message"]:
            result.stdout = f"{window_id}\n"
        elif cmd[:2] == ["tmux", "split-window"]:
            result.stdout = f"{pane_id}\n"
        elif cmd[:2] == ["tmux", "list-keys"]:
            result.returncode = 1
            result.stdout = ""
        else:
            result.stdout = ""
        return result

    return _run


def register_fake_follower(
    window_id: str,
    pane_id: str,
    current_file: str | None = None,
    open_files: tuple[str, ...] = (),
    shown_any: bool = False,
    writers: tuple[str, ...] = (),
    writer_labels: tuple[str, ...] = (),
) -> None:
    with patch(
        "vim_ai_follower.tmux.subprocess.run",
        return_value=MagicMock(returncode=0, stdout=f"{pane_id} vim\n"),
    ):
        state.FollowerState.set(
            window_id,
            "tmux",
            pane_id,
            current_file=current_file,
            open_files=open_files,
            shown_any=shown_any,
            writers=writers,
            writer_labels=writer_labels,
        )


def route_exec_lua(nvim: MagicMock, *, buffer: int, display_name: str) -> None:
    """Make a mock nvim's exec_lua answer per Lua chunk: the buffer lookup
    (_FIND_BUFFER_LUA) gets `buffer`, the naming (_DISPLAY_NAME_LUA) gets
    `display_name`. One return_value for both would hand the buffer number
    to buf_set_name/bufadd as the name, and a test asserting the name would
    then pin whatever the mock happened to return."""
    from vim_ai_follower.backends.nvim import _DISPLAY_NAME_LUA, _FIND_BUFFER_LUA

    answers: dict[str, object] = {_FIND_BUFFER_LUA: buffer, _DISPLAY_NAME_LUA: display_name}

    def answer(code: str, *args: object) -> object:
        return answers[code]

    nvim.exec_lua.side_effect = answer


# The statements the tmux backend types in place of a path: a guarded read of
# the one-shot handle file that holds it, then its deletion
# (tmux_vim._typed_path), the handle named either by its absolute path or
# HOME-relative through expand(). Spelled out rather than imported, like the
# command literals the tests pin.
_TYPED_PATH = re.compile(
    r"""let g:vaf_h = (?:'((?:[^']|'')*)'|expand\('~/([^']*)',1\))"""
    r""" \| let (\S+) = filereadable\(g:vaf_h\) \? join\(readfile\(g:vaf_h,'b'\),"\\n"\) : ''"""
    r""" \| call delete\(g:vaf_h\)"""
)


def typed_path(path: object) -> str:
    """What resolve_typed_paths leaves in a sent line where the tmux backend
    typed `path` by handle, as the value of the read's variable (the rest of
    the statement is `let <var> = `). Never Vim syntax, so a line that spelled
    the path itself can never match an expectation built from this."""
    return f"<typed-path {path}>"


# The landing check that follows a navigating line (tmux_vim._ANSWER_IF_LANDED):
# the call's token (random hex, stamped by the acting line as `g:vaf_k` and
# compared by the check), and the verdict written with it to the pane's
# `landed-<pane>.txt`, named either way like a handle.
_TOKEN = re.compile(r"'[0-9a-f]{8}'")
_LANDED = re.compile(
    r"""writefile\(\[g:vaf_r, <token>\], (?:'[^']*/landed-[0-9]+\.txt'"""
    r"""|expand\('~/[^']*/landed-[0-9]+\.txt',1\))\)"""
)
LANDED = "writefile([g:vaf_r, <token>], <landed>)"


def acting_start(path: object) -> str:
    """What resolve_typed_paths leaves of the start of a line that must land
    on `path` (tmux_vim._ACTING_START)."""
    return (
        f":unlet! g:vaf_p g:vaf_landed | let g:vaf_k = <token> | let g:vaf_p = {typed_path(path)}"
    )


def landed_line() -> str:
    """What resolve_typed_paths leaves of the landing check that follows every
    navigating line (tmux_vim._ANSWER_IF_LANDED)."""
    return (
        ":let g:vaf_r = get(g:, 'vaf_p', '') ==# '' ? 'unread'"
        " : get(g:, 'vaf_k', '') ==# <token> && get(g:, 'vaf_landed', -1) == bufnr('%')"
        " ? 'landed' : 'elsewhere'"
        f" | try | let g:vaf_r = {LANDED} | catch | endtry"
        " | unlet! g:vaf_p g:vaf_k g:vaf_landed g:vaf_r"
    )


def case_folds(path: object) -> int:
    """What the tmux backend's lookups get as {folds} for `path`: 1 when the
    filesystem finds the same file under the path with every letter's case
    swapped, else 0. Decided here independently of tmux_vim._case_folds."""
    text = str(path)
    if text.swapcase() == text:
        return 0
    try:
        return int(Path(text).samefile(text.swapcase()))
    except OSError:
        return 0


def _resolved_spelled(variable: str, name: str) -> str:
    return (
        f"try | let {variable} = resolve(fnamemodify({name}, ':p'))"
        f" | catch | let {variable} = fnamemodify({name}, ':p') | endtry"
    )


def find_buffer_spelled(variable: str, folds: int) -> str:
    """The tmux backend's buffer lookup (tmux_vim._find_buffer), spelled out
    rather than imported so an unintended change to it is caught: resolve()d
    full names, each resolve() guarded (a symlink loop raises E655), compared
    with the filesystem's case folding as a literal."""
    return (
        _resolved_spelled("g:vaf_q", variable) + " | let g:vaf_n = -1"
        " | for g:vaf_i in range(1, bufnr('$'))"
        f" | {_resolved_spelled('g:vaf_c', 'bufname(g:vaf_i)')}"
        f" | if index([g:vaf_q], g:vaf_c, 0, {folds}) == 0 | let g:vaf_n = g:vaf_i | break | endif"
        " | endfor"
    )


def land_if_target_spelled(folds: int) -> str:
    """The identity half of the landing verdict (tmux_vim._LAND_IF_TARGET)."""
    return (
        _resolved_spelled("g:vaf_c", "bufname('%')")
        + f" | if index([g:vaf_q], g:vaf_c, 0, {folds}) == 0"
        " | let g:vaf_landed = bufnr('%') | endif"
    )


_SHORT = "fnamemodify(g:vaf_p, ':.')"
_DISPLAY = f"(fnamemodify({_SHORT}, ':p') ==# fnamemodify(g:vaf_p, ':p') ? {_SHORT} : g:vaf_p)"


def goto_spelled(path: object) -> str:
    """What resolve_typed_paths leaves of the exact Ex line the tmux backend's
    goto_file sends for `path`, spelled out (never imported from
    tmux_vim._GOTO_FILE: importing would make every assertion agree with
    whatever the constant says). The `:try`/`:catch` swallows E37 and nothing
    else; the `SwapExists` hook answers the ATTENTION dialog `(E)dit anyway`;
    'fileignorecase' is off for the navigation and restored in `finally`; the
    landing is recorded only when the current buffer is the target."""
    folds = case_folds(path)
    return (
        acting_start(path) + " | if g:vaf_p !=# ''"
        f" | {find_buffer_spelled('g:vaf_p', folds)}"
        ' | exe "augroup vim_ai_follower_swap"'
        " | exe \"autocmd SwapExists * ++once let v:swapchoice = 'e'\""
        ' | exe "augroup END"'
        " | let g:vaf_f = &fic | let &fic = 0"
        " | try"
        f" | if g:vaf_n < 0 | exe 'silent tab drop ' . fnameescape({_DISPLAY})"
        " | elseif g:vaf_n != bufnr('%')"
        " | exe win_gotoid(get(win_findbuf(g:vaf_n), 0)) ? '' : 'silent tab sbuffer ' . g:vaf_n"
        " | endif"
        r" | catch /^Vim\%((\a\+)\)\=:E37:/"
        ' | finally | let &fic = g:vaf_f | exe "autocmd! vim_ai_follower_swap"'
        ' | exe "augroup! vim_ai_follower_swap" | endtry'
        f" | {land_if_target_spelled(folds)}"
        " | endif | unlet! g:vaf_h g:vaf_n g:vaf_q g:vaf_c g:vaf_i g:vaf_f"
    )


def rename_spelled(path: object, *, adopted: bool = False, in_new_tab: bool = False) -> str:
    """What resolve_typed_paths leaves of show_fresh's rename-in-place line:
    the path typed by handle into `g:vaf_p` through fnameescape(), named
    relative to Vim's cwd when that round-trips (tmux_vim._vim_display_name),
    `silent`, with 'fileignorecase' off; an adopted Vim's also claims the
    readonly option; the landing is recorded only when the renamed buffer is
    the target. Spelled out for the same reason as goto_spelled."""
    claim = " | let b:vaf_user_ro = 0 | let b:vaf_ro_ours = 2" if adopted else ""
    return (
        acting_start(path)
        + " | if g:vaf_p !=# ''"
        + (" | tabnew" if in_new_tab else "")
        + " | setlocal noswapfile | let g:vaf_f = &fic | let &fic = 0"
        f" | try | silent exe 'file ' . fnameescape({_DISPLAY})"
        " | finally | let &fic = g:vaf_f | endtry"
        + f" | {_resolved_spelled('g:vaf_q', 'g:vaf_p')}"
        + f" | {land_if_target_spelled(case_folds(path))}"
        + " | if exists('g:vaf_landed')"
        + claim
        + " | setlocal buftype= modifiable noreadonly"
        + " | noautocmd silent! edit! | silent! %d _ | endif"
        + " | endif | unlet! g:vaf_h g:vaf_n g:vaf_q g:vaf_c g:vaf_i g:vaf_f"
    )


def wipe_spelled(path: object) -> str:
    """What resolve_typed_paths leaves of the exact Ex line an eviction (and
    show_fresh's pre-wipe) sends to wipe `path`'s buffer by NUMBER, spelled
    out for the same reason as goto_spelled."""
    return (
        f":let g:vaf_wipe_name = {typed_path(path)}"
        " | if g:vaf_wipe_name !=# ''"
        f" | {find_buffer_spelled('g:vaf_wipe_name', case_folds(path))}"
        " | if g:vaf_n > 0 | exe 'silent! bwipeout! ' . g:vaf_n | endif"
        " | endif | unlet! g:vaf_h g:vaf_n g:vaf_q g:vaf_c g:vaf_i g:vaf_f g:vaf_wipe_name"
    )


def resolve_typed_paths(text: str) -> str:
    """`text` with every handle read replaced by typed_path(<what the handle
    holds>). The handle must exist and hold the exact bytes: reading it is
    how the test knows which file the line names."""

    def resolve(match: re.Match[str]) -> str:
        absolute, home_relative, variable = match.groups()
        if absolute is not None:
            handle = Path(absolute.replace("''", "'"))
        else:
            handle = Path.home() / home_relative
        return f"let {variable} = {typed_path(os.fsdecode(handle.read_bytes()))}"

    return _LANDED.sub(LANDED, _TOKEN.sub("<token>", _TYPED_PATH.sub(resolve, text)))
