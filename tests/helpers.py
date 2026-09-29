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


# The expression the tmux backend types in place of a path: a read of the
# one-shot handle file that holds it (tmux_vim._typed_path), named either by
# its absolute path or HOME-relative through expand(). Spelled out rather than
# imported, like the command literals the tests pin.
_TYPED_PATH = re.compile(
    r"""join\(readfile\((?:'((?:[^']|'')*)'|expand\('~/([^']*)',1\)),'b'\),"\\n"\)"""
)


def typed_path(path: object) -> str:
    """What resolve_typed_paths leaves in a sent line where the tmux backend
    typed `path` by handle. Never Vim syntax, so a line that spelled the path
    itself can never match an expectation built from this."""
    return f"<typed-path {path}>"


def resolve_typed_paths(text: str) -> str:
    """`text` with every handle read replaced by typed_path(<what the handle
    holds>). The handle must exist and hold the exact bytes: reading it is
    how the test knows which file the line names."""

    def resolve(match: re.Match[str]) -> str:
        absolute, home_relative = match.groups()
        if absolute is not None:
            handle = Path(absolute.replace("''", "'"))
        else:
            handle = Path.home() / home_relative
        return typed_path(os.fsdecode(handle.read_bytes()))

    return _TYPED_PATH.sub(resolve, text)
