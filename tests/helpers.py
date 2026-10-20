"""Shared test doubles for the CLI-level test modules."""

from __future__ import annotations

import os
import re
import socket
import threading
from collections.abc import Callable, Iterator
from contextlib import contextmanager, suppress
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

from vim_ai_follower import state

# The pid make_mock_tmux_run's server answers to `display-message -p
# '#{pid}'`, and that register_fake_follower records: the state then proves
# it was written under this server (state.proven_here), as a real one does.
MOCK_SERVER_PID = 4242

# The VIMINIT of every Vim (and nvim: 'viminfofile' is 'shadafile' there) a
# test starts under the developer's HOME: no user vimrc or plugins, and no
# viminfo/ShaDa written into that HOME (T24b re-review I-A: every
# tmux_session Vim wrote ~/.viminfo). Extend it with `| …`, never replace it.
HERMETIC_VIMINIT = "set nocompatible noloadplugins viminfofile=NONE"


def load_scaled(seconds: float) -> float:
    """A timeout stretched by the machine's load per CPU, at most four times: the
    full suite runs at load 30 on 8 CPUs (N-D, 2026-10-20), where a 30 s hook
    wait failed for time alone. A real hang still fails, at most four times later."""
    load_per_cpu = os.getloadavg()[0] / (os.cpu_count() or 1)
    return seconds * min(4.0, max(1.0, load_per_cpu))


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

    def _run(command: list[str], **keyword_arguments: object) -> MagicMock:
        result = MagicMock()
        result.returncode = 0
        if command[:3] == ["tmux", "list-panes", "-a"]:
            lines = [f"{pane} zsh" for pane in other_panes]
            lines.extend(f"{pane} vim" for pane in vim_panes)
            if pane_exists:
                lines.append(f"{pane_id} vim")
            result.stdout = "".join(line + "\n" for line in lines)
        elif command == ["tmux", "display-message", "-p", "#{pid}"]:
            result.stdout = f"{MOCK_SERVER_PID}\n"
        elif command[:2] == ["tmux", "display-message"]:
            result.stdout = f"{window_id}\n"
        elif command[:2] == ["tmux", "split-window"]:
            result.stdout = f"{pane_id}\n"
        elif command[:2] == ["tmux", "list-keys"]:
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
    server_pid: int | None = MOCK_SERVER_PID,
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
            server_pid=server_pid,
        )


def shows_the_buffer(nvim: MagicMock, number: object = None) -> None:
    """Wire a mock nvim so goto_file finds the looked-up buffer already shown
    in a window (its switch branch): one tab, one window, showing the buffer
    `number`, or by default whatever exec_lua answers at the time of the
    call (the buffer lookup of a test that sets exec_lua.return_value). A
    bare MagicMock lists no tab at all, so goto_file would take its new-tab
    branch, which proves the tab it adds (_show_in_new_tab) and fails."""
    nvim.api.list_tabpages.return_value = [MagicMock(name="tab")]
    nvim.api.tabpage_list_wins.return_value = [MagicMock(name="window")]
    nvim.api.win_get_buf.side_effect = lambda window: MagicMock(
        number=nvim.exec_lua.return_value if number is None else number
    )


def tabnew_adds_a_tab(nvim: MagicMock) -> MagicMock:
    """Wire a mock nvim so `:tabnew` adds one tabpage and whatever buffer is
    put in a window becomes the current one: what _show_in_new_tab needs to
    prove it landed. Returns the new tab's window, the one the buffer must
    be put in (never window 0, see _show_in_new_tab)."""
    old_tab, new_tab = MagicMock(handle=1), MagicMock(handle=2)
    window = MagicMock(name="the new tab's window")
    opened: list[MagicMock] = []
    shown: list[object] = []

    def command(text: str) -> None:
        if text == "tabnew":
            opened.append(new_tab)

    def win_set_buf(target: object, buffer: object) -> None:
        shown.append(buffer)

    nvim.command.side_effect = command
    nvim.api.list_tabpages.side_effect = lambda: [old_tab, *opened]
    nvim.api.tabpage_get_win.side_effect = lambda tab: window
    nvim.api.win_set_buf.side_effect = win_set_buf
    nvim.api.get_current_buf.side_effect = lambda: MagicMock(handle=shown[-1] if shown else None)
    return window


def fresh_buffer(nvim: MagicMock, handle: int = 7) -> MagicMock:
    """Wire a mock nvim for NvimFollower._fresh_buffer, which show_fresh
    names, reads and types into: the buffer create_buf returns is
    `nvim.current.buffer` (the object the tests assert on) with `handle`, it
    reads as current once shown, and `:tabnew` adds one tabpage."""
    buffer: MagicMock = nvim.current.buffer
    buffer.handle = handle
    nvim.api.create_buf.return_value = buffer
    nvim.api.get_current_buf.return_value = buffer
    old_tab, new_tab = MagicMock(handle=1), MagicMock(handle=2)
    tabs: list[list[MagicMock]] = [[old_tab]]

    def list_tabpages() -> list[MagicMock]:
        current = tabs[-1]
        tabs.append([old_tab, new_tab])
        return current

    nvim.api.list_tabpages.side_effect = list_tabpages
    return buffer


def route_exec_lua(nvim: MagicMock, *, buffer: int, display_name: str) -> None:
    """Make a mock nvim's exec_lua answer per Lua chunk: the buffer lookup
    (_FIND_BUFFER_LUA) gets `buffer`, the naming (_DISPLAY_NAME_LUA) gets
    `display_name`. One return_value for both would hand the buffer number
    to buf_set_name/bufadd as the name, and a test asserting the name would
    then pin whatever the mock happened to return. A completion's stamp
    (_STAMP_IF_EXPECTED_LUA) answers True, as for a buffer that holds what
    the run typed; show_fresh's read-then-clear (_READ_THEN_CLEAR_LUA)
    returns nothing; the typing loop's window lookup (_FOLLOWER_WINDOW_LUA)
    answers a window that shows the buffer."""
    from vim_ai_follower.backends.nvim import (
        _DISPLAY_NAME_LUA,
        _FIND_BUFFER_LUA,
        _FOLLOWER_WINDOW_LUA,
        _READ_THEN_CLEAR_LUA,
        _STAMP_IF_EXPECTED_LUA,
    )

    answers: dict[str, object] = {
        _FIND_BUFFER_LUA: buffer,
        _DISPLAY_NAME_LUA: display_name,
        _STAMP_IF_EXPECTED_LUA: True,
        _READ_THEN_CLEAR_LUA: None,
        _FOLLOWER_WINDOW_LUA: 1000,
    }

    def answer(code: str, *arguments: object) -> object:
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
_SOURCED = re.compile(r"sil! so [^|\s]+|exe 'sil! so ' \. fnameescape\('(?:[^']|'')*'\)")
_CALL = re.compile(r"call <fn>\('(\w+)'((?:,[^,)]+)*)\)")
_HANDLE = re.compile(r"'(p[0-9]+-[0-9a-f]{6})'")
# The calls whose handle argument names a path (tmux_vim._path_handle).
_READING_OPS = {"goto", "rename", "wipe", "probe", "evict", "give_back"}
# Every handle the backend created during the test, by name (conftest's
# autouse record_path_handles): a test may read its sends after its own
# CACHE_DIRECTORY patch is gone, and the line names the handle only.
PATH_HANDLES: dict[str, Path] = {}


def typed_path(path: object) -> str:
    """What resolve_typed_paths leaves in a sent line where the tmux backend
    passed `path` by handle. Never Vim syntax, so a line that spelled the path
    itself can never match an expectation built from this."""
    return f"<typed-path {path}>"


def call_spelled(operation: str, *arguments: object) -> str:
    """The typed line that runs s:<op>(arguments) through the dispatcher,
    guarded so a Vim without it runs nothing (tmux_vim._call_line)."""
    rendered = ",".join(
        f"'{argument}'"
        if isinstance(argument, str) and not argument.startswith("<")
        else str(argument)
        for argument in (operation, *arguments)
    )
    return f":if exists('*{FUNCTION}')|call {FUNCTION}({rendered})|endif"


def define_line() -> str:
    """The line that sources the functions into a Vim that lacks them
    (tmux_vim._define_line), as resolve_typed_paths leaves it."""
    return f":if !exists('*{FUNCTION}')|<script>|endif"


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


def evict_spelled(path: object) -> str:
    """What resolve_typed_paths leaves of the call an ADOPTED Vim's eviction
    (and `stop`) sends for `path`'s buffer (s:evict)."""
    return call_spelled("evict", typed_path(path), case_folds(path))


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
        operation, arguments = call.group(1), call.group(2)

        def argument(match: re.Match[str]) -> str:
            if operation not in _READING_OPS:
                return "<token>"
            handle = PATH_HANDLES[match.group(1)]
            return typed_path(os.fsdecode(handle.read_bytes()))

        return f"call {FUNCTION}('{operation}'{_HANDLE.sub(argument, arguments)})"

    return _CALL.sub(resolve, text)


def nvim_mode_reply(message_id: int) -> bytes:
    """msgpack `[1, message_id, nil, {"mode": "n", "blocking": false}]`."""
    return bytes([0x94, 0x01, message_id, 0xC0, 0x82]) + b"\xa4mode\xa1n\xa8blocking\xc2"


class FakeNvim:
    """A unix-socket listener speaking just enough msgpack-RPC: each request
    `[0, msgid, method, args]` gets `reply(msgid)` back (a well-formed
    `nvim_get_mode` answer by default). `reply=None` accepts and never
    answers, which is what a SIGSTOPped nvim looks like from outside;
    `reply="hang up"` closes the connection without a word, which the
    client may see as a refused send or as an empty read, whichever the
    scheduler picks; `reply="hang up after the request"` reads the request
    first, so the client's read is always the empty one."""

    def __init__(self, path: Path, reply: Any = "mode") -> None:
        self.path = path
        self.reply = reply
        self.server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.server.bind(str(path))
        self.server.listen()
        # A short accept timeout lets close() stop the thread and then close
        # every client it accepted: closing the listener alone does not wake
        # a blocked accept, and a client accepted after close() went through
        # its list was left to the garbage collector (final fix X1).
        self.server.settimeout(0.05)
        self.closing = threading.Event()
        self.clients: list[socket.socket] = []
        self.thread = threading.Thread(target=self._serve, daemon=True)
        self.thread.start()

    def _serve(self) -> None:
        while not self.closing.is_set():
            try:
                client, _address = self.server.accept()
            except TimeoutError:
                continue
            except OSError:
                return
            self.clients.append(client)
            if self.reply is None:
                continue
            if self.reply == "hang up":
                client.close()
                continue
            if self.reply == "hang up after the request":
                with suppress(OSError):
                    client.recv(4096)
                client.close()
                continue
            with suppress(OSError):
                request = client.recv(4096)
                # [0, msgid, ...]: a fixarray, the request type, a fixint msgid
                message_id = request[2]
                client.sendall(nvim_mode_reply(message_id) if self.reply == "mode" else self.reply)

    def close(self) -> None:
        self.closing.set()
        # Bounded: a client that connected and never sent holds the thread
        # in its recv.
        self.thread.join(timeout=2)
        for client in self.clients:
            client.close()
        self.server.close()


@contextmanager
def fake_nvim(path: Path, reply: Any = "mode") -> Iterator[FakeNvim]:
    fake = FakeNvim(path, reply)
    try:
        yield fake
    finally:
        fake.close()
