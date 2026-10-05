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


# The tmux backend types only short calls into Vim functions it defines once
# per Vim (tmux_vim._VIM_SCRIPT). Its lines are spelled out here rather than
# built from tmux_vim, like the command literals the tests pin, with three
# normalizations (resolve_typed_paths): the dispatcher's name (a hash of the
# script) becomes `<fn>`, the script file the define line sources becomes
# `<script>`, and a call's handle argument becomes typed_path(<the path the
# handle holds>) for the calls that read it, `<token>` for the landing check.
FUNCTION = "<fn>"
_FUNCTION_NAME = re.compile(r"VafFollower_[0-9a-f]{6}")
_SOURCED = re.compile(r"sil! so \S+|exe 'sil! so ' \. fnameescape\('(?:[^']|'')*'\)")
_CALL = re.compile(r"call <fn>\('(\w+)'((?:,[^,)]+)*)\)")
_HANDLE = re.compile(r"'(p[0-9]+-[0-9a-f]{6})'")
# The calls whose handle argument names a path (tmux_vim._path_handle).
_READING_OPS = {"goto", "rename", "wipe", "probe"}
# Every handle the backend created during the test, by name (conftest's
# autouse record_path_handles): a test may read its sends after its own
# CACHE_DIR patch is gone, and the line names the handle only.
PATH_HANDLES: dict[str, Path] = {}


def typed_path(path: object) -> str:
    """What resolve_typed_paths leaves in a sent line where the tmux backend
    passed `path` by handle. Never Vim syntax, so a line that spelled the path
    itself can never match an expectation built from this."""
    return f"<typed-path {path}>"


def call_spelled(op: str, *arguments: object) -> str:
    """The typed line that runs s:<op>(arguments) through the dispatcher,
    guarded so a Vim without it runs nothing (tmux_vim._call_line)."""
    rendered = ",".join(
        f"'{argument}'"
        if isinstance(argument, str) and not argument.startswith("<")
        else str(argument)
        for argument in (op, *arguments)
    )
    return f":if exists('*{FUNCTION}') | call {FUNCTION}({rendered}) | endif"


def define_line() -> str:
    """The line that sources the functions into a Vim that lacks them
    (tmux_vim._define_line), as resolve_typed_paths leaves it."""
    return f":if !exists('*{FUNCTION}') | <script> | endif"


def landed_line() -> str:
    """The landing check that follows every navigating call (s:landed)."""
    return call_spelled("landed", "<token>")


def case_folds(path: object) -> int:
    """What the tmux backend's lookups get as `folds` for `path`: 1 when the
    filesystem finds the same file under the path with every letter's case
    swapped, else 0. Decided here independently of tmux_vim._case_folds."""
    text = str(path)
    if text.swapcase() == text:
        return 0
    try:
        return int(Path(text).samefile(text.swapcase()))
    except OSError:
        return 0


def goto_spelled(path: object) -> str:
    """What resolve_typed_paths leaves of the call goto_file sends for
    `path` (s:goto)."""
    return call_spelled("goto", typed_path(path), case_folds(path))


def rename_spelled(path: object, *, adopted: bool = False, in_new_tab: bool = False) -> str:
    """What resolve_typed_paths leaves of show_fresh's rename call
    (s:rename)."""
    return call_spelled("rename", typed_path(path), case_folds(path), int(in_new_tab), int(adopted))


def wipe_spelled(path: object) -> str:
    """What resolve_typed_paths leaves of the call an eviction (and
    show_fresh's pre-wipe) sends to wipe `path`'s buffer by NUMBER (s:wipe)."""
    return call_spelled("wipe", typed_path(path), case_folds(path))


def probe_spelled(path: object) -> str:
    """What resolve_typed_paths leaves of probe_buffer's call (s:probe)."""
    return call_spelled("probe", typed_path(path), case_folds(path))


def vim_function(name: str) -> str:
    """The body of the script-local function s:<name> in the script the tmux
    backend defines, from its `function!` line to its `endfunction`."""
    from vim_ai_follower.backends.tmux_vim import _vim_functions

    text = _vim_functions()[1]
    start = text.index(f"function! s:{name}(")
    return text[start : text.index("endfunction", start) + len("endfunction")]


def resolve_typed_paths(text: str) -> str:
    """`text` normalized as described above. A handle a reading call names
    must exist and hold the exact bytes: reading it is how the test knows
    which file the line names."""
    text = _SOURCED.sub("<script>", _FUNCTION_NAME.sub(FUNCTION, text))

    def resolve(call: re.Match[str]) -> str:
        op, arguments = call.group(1), call.group(2)

        def argument(match: re.Match[str]) -> str:
            if op not in _READING_OPS:
                return "<token>"
            handle = PATH_HANDLES[match.group(1)]
            return typed_path(os.fsdecode(handle.read_bytes()))

        return f"call {FUNCTION}('{op}'{_HANDLE.sub(argument, arguments)})"

    return _CALL.sub(resolve, text)
