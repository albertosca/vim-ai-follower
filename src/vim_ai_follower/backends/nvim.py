"""First-class Neovim follower backend. Unlike the retired nvim_rpc stub, this
drives a dedicated (or adopted) nvim entirely over msgpack-RPC: a Python control
loop types each edit one CHARACTER at a time over the API — checking
control.check_signal before every character, exactly like animate.run_lines/
run_ops do per keystroke — highlighting the active line with an extmark, moving
the cursor, and forcing a redraw so the typing shows smoothly. No `tmux
send-keys`, so the keystroke-corruption bug class the tmux backend fights simply
does not exist here."""

from __future__ import annotations

import contextlib
import logging
import time
from collections.abc import Callable, Iterator, Sequence
from dataclasses import dataclass
from pathlib import Path

import pynvim

from vim_ai_follower import config, control
from vim_ai_follower.animate import DEFAULT_PACE_SECONDS, AnimationResult, _wait_while_paused
from vim_ai_follower.backends import (
    BufferProbe,
    NavigationFailed,
    classify_buffer,
    log_adopted_close,
    log_state_gone,
    nvim_connect,
    typed_form,
)
from vim_ai_follower.backends.nvim_connect import bounded_attach, probe_nvim
from vim_ai_follower.backends.nvim_prompt import dismiss_prompt, exec_logged, prompt_guard
from vim_ai_follower.control import PendingApplyEdit, PendingShowFresh
from vim_ai_follower.diff import EditOp, apply_ops
from vim_ai_follower.state import FollowerState, owned_state

logger = logging.getLogger("vim_ai_follower")

_NAMESPACE = "vaf"
_TYPING_HL = "VafTypingLine"


def _terminated(lines: Sequence[str]) -> str:
    """The buffer content of `lines` in the terminated-newline form every
    persisted partial uses (hooks._terminated is the same function on the hook
    side; duplicated rather than imported because hooks imports this module).
    Terminating beats joining: a "\\n".join round-trip through splitlines
    drops a trailing blank line, this one is lossless."""
    return "".join(line + "\n" for line in lines)


def _bind_save(save_pending: Callable[[int], None] | None, index: int) -> Callable[[], None]:
    """A zero-arg callable persisting the crash-fallback remainder from `index`
    (or a no-op when no save_pending was given), as _wait_while_paused wants."""

    def _save() -> None:
        if save_pending is not None:
            save_pending(index)

    return _save


# One RPC: canonicalize both sides INSIDE nvim, which is what named the
# buffers. fs_realpath returns nil for a path that does not exist (yet), so
# fall back to resolving the directory and re-attaching the basename.
_FIND_BUFFER_LUA = """
local function canon(p)
  local real = vim.uv.fs_realpath(p)
  if real then return real end
  local dir = vim.uv.fs_realpath(vim.fs.dirname(p))
  if dir then return dir .. '/' .. vim.fs.basename(p) end
  return vim.fn.fnamemodify(p, ':p')
end
local want = canon(...)
for _, buf in ipairs(vim.api.nvim_list_bufs()) do
  local name = vim.api.nvim_buf_get_name(buf)
  if name ~= '' and canon(name) == want then return buf end
end
return -1
"""


def _buffer_number(nvim: pynvim.Nvim, file_path: str) -> int:
    """The number of the buffer holding `file_path`, or -1 — found by walking
    the buffer list, never by `bufnr()`.

    `bufnr()` (like `:bwipeout {name}`) takes a buffer-name PATTERN. Measured
    2026-09-22 against real nvim: with `app/[slug]/page.tsx` and its
    pattern-sibling `app/s/page.tsx` both open, `bufnr(target)` returned the
    SIBLING, so goto_file landed on the wrong file and close_tab evicted it.
    Both sides are realpath'd because nvim stores names with a symlinked
    directory prefix resolved (macOS /tmp -> /private/tmp) — the one thing
    `bufnr()` got right, and a plain name compare would lose."""
    number: int = nvim.exec_lua(_FIND_BUFFER_LUA, file_path)
    return number


def _animate_lines(
    nvim: pynvim.Nvim,
    buf: int,
    lines: tuple[str, ...],
    start_row: int,
    pace_provider: Callable[[], float],
    window_id: str,
    ns: int,
    base_dir: Path | None = None,
    save_pending: Callable[[int], None] | None = None,
) -> AnimationResult:
    """Type `lines` into `buf` from `start_row`, one CHARACTER at a time over
    the API, checking for a pause/interrupt signal before every character (not
    just between lines) so both respond immediately — the per-keystroke
    granularity the tmux backend gets from run_lines. A redraw per character
    flushes the terminal UI so the typing shows smoothly instead of in
    coalesced bursts.

    On interrupt mid-line, the buffer is left exactly as far as it was typed —
    no snap to the line's full text. Alberto's report (2026-09-16): with the
    old snap, interrupting mid-line "finished writing the line" before handing
    the buffer over, which is precisely what he does not want. The line is NOT
    counted as shown (the run returns "interrupted" with the line's own index,
    not index + 1), so it is the first entry of the saved remainder and gets
    retyped from its start on resume — the whole-line partial/resume machinery
    stays coherent (it always retypes a stored remainder line from scratch,
    never mid-character) without any consumer needing to know a line was ever
    half-typed. This mirrors the mid-char PAUSE crash-fallback below
    (`save_pending(index)`), which already treats the in-progress line as not
    yet shown for the same reason. A pause blocks in place via
    animate._wait_while_paused until the user resumes (retyping from the same
    character) or interrupts. The pace is re-read per line so a live Ctrl+a
    +/- takes effect at the next line; a zero pace types the whole line at
    once (the pace-0 catch-up must not animate).
    `save_pending(index)` persists the crash-fallback remainder while paused."""
    # Static default (2026-09-15, Alberto's request): his real nvim config
    # loads gruvbox via ~/.vimrc, but that config's own plugin loading can
    # still be mid-flight when typing starts, so force it explicitly and
    # synchronously here instead of racing load order. Order matters twice
    # over: `set background=dark` must run BEFORE `colorscheme` (gruvbox
    # reads &background at source time; setting it after forces a second,
    # redundant re-source), and BOTH must run before `highlight default
    # {_TYPING_HL}` below — `:colorscheme` always runs `hi clear` first,
    # which wipes every Vaf* highlight group already defined (VafTypingLine
    # here, and VafWriterCue from hooks._apply_writer_cue), silently and
    # invisibly disabling the typing cue and transiently blanking the
    # writer-cue border. The g:colors_name guard makes this a true no-op
    # after the first animation on this nvim process, so only the very
    # first call pays the hi clear cost (both self-heal afterward via
    # hooks._refresh_writer_cue's completion re-render). `silent!` swallows
    # "E185: Cannot find color scheme" in hermetic test/CI nvims that don't
    # have the gruvbox plugin at all; `background` is a built-in option and
    # needs no guard. `silent!` must sit directly before `colorscheme`, not
    # before `if` — a modifier on `if` does NOT propagate to guard the
    # command(s) it guards (verified against real nvim: `silent! if 1 |
    # colorscheme bogus | endif` still raises E185, while `if 1 | silent!
    # colorscheme bogus | endif` swallows it).
    nvim.command("set background=dark")
    nvim.command(
        "if get(g:, 'colors_name', '') !=# 'gruvbox' | silent! colorscheme gruvbox | endif"
    )
    nvim.command(f"highlight default {_TYPING_HL} ctermbg=237 guibg=#3a3a3a")
    index = 0
    while index < len(lines):
        signal = control.check_signal(window_id, base_dir)
        if signal == "interrupt":
            return AnimationResult("interrupted", index)
        if signal == "pause":
            if not _wait_while_paused(window_id, _bind_save(save_pending, index), base_dir):
                return AnimationResult("interrupted", index)
            continue  # resumed: retype this line from its clean boundary
        row = start_row + index
        nvim.api.buf_set_lines(buf, row, row, True, [""])
        line = lines[index]
        pace = pace_provider()
        if pace <= 0:  # pace-0 catch-up: type the whole line at once, no anim
            if line:
                nvim.api.buf_set_text(buf, row, 0, row, 0, [line])
            index += 1
            continue
        mark = nvim.api.buf_set_extmark(buf, ns, row, 0, {"line_hl_group": _TYPING_HL})
        char = 0
        # nvim's API columns are BYTE offsets; `char` counts CODE POINTS. They
        # coincide only while the line stays ASCII, so carry the byte offset
        # of everything typed so far alongside the character index instead of
        # reusing `char` as a column. Before this, the first multi-byte
        # character desynchronised the two by (width - 1) bytes and every
        # later insert landed inside an already-typed character: Alberto's
        # `alpha — beta` reached the buffer as b'alpha \xe2 beta\x80\x94'
        # (QA visual battery, 2026-09-21). `char` keeps its own meaning
        # untouched — the pace loop, the signal checks and every persisted
        # remainder stay character-granular.
        col = 0
        interrupted = False
        while char < len(line):
            signal = control.check_signal(window_id, base_dir)
            if signal == "interrupt":
                interrupted = True
                break
            if signal == "pause":
                if not _wait_while_paused(window_id, _bind_save(save_pending, index), base_dir):
                    interrupted = True
                    break
                continue  # resumed: retype from the same character
            nvim.api.buf_set_text(buf, row, col, row, col, [line[char]])
            col += len(line[char].encode("utf-8"))
            char += 1
            with contextlib.suppress(Exception):
                # Byte-based too, so the cursor trails the typed prefix by its
                # byte length, not its character count.
                nvim.api.win_set_cursor(0, [row + 1, col])
            nvim.command("redraw")
            time.sleep(pace)
        nvim.api.buf_del_extmark(buf, ns, mark)
        if interrupted:
            # Leave the line exactly as far as it was typed (no snap) and
            # don't count it as shown — see the docstring above.
            return AnimationResult("interrupted", index)
        index += 1
    return AnimationResult("completed", len(lines))


# Turns the current buffer's swap back on in an ADOPTED nvim, after the
# follower named or loaded it with swap off (_name_without_swap, goto_file,
# _open_from_disk). An adopted nvim is the user's own editor: without a swap,
# a crash loses what they type into that buffer and a second editor opening
# the file gets no ATTENTION (Alberto's ruling, 2026-09-28). A dedicated
# follower's buffers are display-only and stay swap-off.
#
# Turning swap on runs the same swap check the opt-out avoided. Measured
# 2026-09-28 on a UI nvim in a tmux pane and on Vim at 49 columns, an owner
# editor holding the swap: a plain `setlocal swapfile` raised E325 (and
# `buf_set_option(swapfile, True)` blocked a UI nvim at the ATTENTION pager);
# a scoped SwapExists answer does not help, because SwapExists never fires
# for an option toggle; `shortmess+=A`, saved and restored in `finally`,
# was silent in both editors and still created the swap (`.swo` beside a
# held one), and a second editor opening the file then got its SwapExists.
_SWAP_BACK_ON = (
    "let g:vaf_shortmess = &shortmess | set shortmess+=A"
    " | try | setlocal swapfile"
    " | finally | let &shortmess = g:vaf_shortmess | unlet g:vaf_shortmess | endtry"
)


# "Clean", for ensure_showing's re-read of an open buffer: it holds nothing the
# user typed that is not on disk. nvim's 'modified' alone cannot say it, because
# this backend never writes its buffers: every buffer the follower typed is
# 'modified' although it matches the file Claude wrote. So a completed
# animation stamps the buffer's changedtick (_drive, `b:vaf_synced_tick`), and
# a buffer is clean when it is unmodified OR its tick still equals that stamp.
# Any user edit moves the tick, even one undone again, so the answer errs
# toward "not clean", the side that never discards typing. An interrupt does
# not stamp: the handed-over buffer is the user's.
_SYNCED_TICK = "vaf_synced_tick"
_IS_CLEAN_LUA = f"""
local buf = ...
if not vim.bo[buf].modified then return true end
return vim.b[buf].{_SYNCED_TICK} == vim.api.nvim_buf_get_changedtick(buf)
"""
# The completion stamp, in ONE request: nvim serves one request at a time, so
# no keystroke of the user's can land between the compare and the stamp. The
# tick is stamped only when the buffer holds exactly the lines the follower
# meant to leave there (_drive's `expected`); a buffer that also holds the
# user's typing (typed while the animation finished, C5 of the 2026-09-28
# review) stays unstamped, so a later Read never re-reads it away.
_STAMP_IF_EXPECTED_LUA = f"""
local buf, expected = ...
local lines = vim.api.nvim_buf_get_lines(buf, 0, -1, true)
if #lines ~= #expected then return false end
for index = 1, #lines do
  if lines[index] ~= expected[index] then return false end
end
vim.b[buf].{_SYNCED_TICK} = vim.api.nvim_buf_get_changedtick(buf)
return true
"""


def _fileformat_of(data: bytes) -> str | None:
    """The 'fileformat' a file's bytes call for: "dos" when every line break
    is CR LF, "unix" when none is, and None for a file with no line break or
    with MIXED endings, whose format stays whatever the editor chose when it
    loaded the buffer."""
    breaks = data.count(b"\n")
    crlf = data.count(b"\r\n")
    if breaks == 0 or 0 < crlf < breaks:
        return None
    return "dos" if crlf == breaks else "unix"


def _format_from_disk(nvim: pynvim.Nvim, buf: int, file_path: str) -> None:
    """Set `buf`'s 'bomb' and 'fileformat' from the file at file_path.

    Both editors keep a leading BOM in 'bomb' and a CRLF file's line breaks
    in 'fileformat', not in the buffer, and the follower types neither
    (backends.without_bom; splitlines() drops the CRs). So an Edit that
    adds or drops a BOM, or changes the line endings, changed nothing on
    screen and left the option at its
    pre-edit value, and the user's next `:w` (or the hand-off's) undid that
    part of Claude's change (final review of backlog-sweep-3, N1; Task 12 of
    backlog-sweep-4 for 'fileformat'). Read from disk, which holds Claude's
    finished file whenever a completed animation runs from the post hook.
    A file that cannot be read leaves both alone; mixed endings leave
    'fileformat' alone (_fileformat_of). 'fileformat' is set only when it
    changes, and before the caller's relock: nvim refuses it (E21) on a
    nomodifiable buffer.

    Setting either option marks the buffer modified. A buffer that was NOT
    modified before (the animation typed nothing: an Edit that changed only
    the line endings or the BOM of a clean adopted buffer) and whose lines
    are the file's is put back to unmodified: it now writes exactly the
    file on disk, and leaving it `[+]` made `:q` refuse with E37 for no
    change (T12+T13 review M3). A buffer the animation typed into keeps its
    'modified'."""
    try:
        data = Path(file_path).read_bytes()
    except OSError:
        return
    was_modified = nvim.api.buf_get_option(buf, "modified")
    nvim.api.buf_set_option(buf, "bomb", data[:3] == b"\xef\xbb\xbf")
    fileformat = _fileformat_of(data)
    if fileformat is not None and nvim.api.buf_get_option(buf, "fileformat") != fileformat:
        nvim.api.buf_set_option(buf, "fileformat", fileformat)
    if not was_modified:
        lines = nvim.api.buf_get_lines(buf, 0, -1, False)
        if classify_buffer(lines, data.decode("utf-8", errors="replace")) == "holds":
            nvim.api.buf_set_option(buf, "modified", False)


def _buffer_lines(content: str) -> list[str]:
    """The lines a buffer holding `content` shows, as the follower types it
    (splitlines, like show_fresh and apply_ops); an empty content is the one
    empty line nvim never goes below."""
    return content.splitlines() or [""]


# The marks an ADOPTED nvim's eviction and `claude-follow stop` go by (see the
# tmux backend's _VIM_EVICT for the C1 reproduction and the rules): a buffer
# the follower created (`b:vaf_created`: goto_file's create_buf,
# _open_from_disk's bufadd, show_fresh's) and a window it opened for a buffer
# the user already had (`w:vaf_opened`). Editor variables, not cache state:
# they die with the editor, so an nvim restarted on the same socket holds no
# buffer the follower would call its own.
_CREATED = "vaf_created"
_OPENED = "vaf_opened"
# One RPC: what to do with an adopted nvim's buffer `buf` whose tab is being
# closed. "Modified" is the negation of _IS_CLEAN_LUA (this backend never
# writes, so a completed animation's buffer is 'modified' but clean): an
# unclean buffer is "kept", a clean one the follower created is wiped, and
# one the user had loses only the windows the follower opened for it.
_EVICT_LUA = f"""
local buf = ...
local clean = not vim.bo[buf].modified
  or vim.b[buf].{_SYNCED_TICK} == vim.api.nvim_buf_get_changedtick(buf)
if not clean then return 'kept' end
if vim.b[buf].{_CREATED} then
  vim.cmd('silent! bwipeout! ' .. buf)
  return 'wiped'
end
local verdict = 'forgotten'
for _, window in ipairs(vim.fn.win_findbuf(buf)) do
  if vim.w[window].{_OPENED} then
    verdict = 'closed'
    pcall(vim.api.nvim_win_close, window, false)
  end
end
return verdict
"""


# show_fresh's read-then-clear of the buffer it just named; see the tmux
# backend's _VIM_RENAME for why (E13 on a plain `:w`) and for each piece.
# One request: nvim redraws only between requests, so the file's content is
# never on screen. This is a disk read outside ensure_showing, but not a
# navigation: it lands on the freshly created, empty buffer show_fresh is
# about to type into, so there is nothing to discard and no E37 to meet.
#
# With the buffer's 'undolevels' at -1 for that one step, so neither the read
# nor the clear is an undo step: a `%d` on the undo stack kept a huge file in
# memory, and the user's `u` on a handed-over or adopted buffer brought the
# whole finished file back (C5 residual). The buffer-local value is saved
# raw (-123456 means "use the global") and put back even if the read fails.
_READ_THEN_CLEAR_LUA = """
local buf = ...
local saved = vim.bo[buf].undolevels
vim.bo[buf].undolevels = -1
local ok, failure = pcall(vim.cmd, 'noautocmd silent! edit! | silent! %d _')
vim.bo[buf].undolevels = saved
if not ok then error(failure) end
"""


# One RPC: nvim's cwd-relative form of the path, kept only when it
# round-trips (see _display_name).
_DISPLAY_NAME_LUA = """
local full = ...
local short = vim.fn.fnamemodify(full, ':.')
if vim.fn.fnamemodify(short, ':p') == vim.fn.fnamemodify(full, ':p') then return short end
return full
"""


def _display_name(nvim: pynvim.Nvim, file_path: str) -> str:
    """file_path as nvim names a file opened by RELATIVE path: relative to the
    current window's working directory when it is under it, full otherwise.
    The name every buffer the follower names or adds is given, so the
    tabline, the statusline and the `:w` message read `sub/a.py` instead of
    the full path the hook passes (measured 2026-09-29: nvim, like Vim, keeps
    a name given in full as it is and only shortens it on a `:cd`).

    `:.` compares against getcwd(), which is the physical directory even when
    nvim was started from, or `:cd` to, a symlinked spelling of it (measured,
    macOS /tmp -> /private/tmp), and the hook passes a realpath.

    The short name is kept only when its `:p` is exactly the full path's
    `:p` — the same round-trip guard as the tmux backend's
    s:display (_VIM_LANDING), where it is load-bearing (its lookups compare `:p`
    with `==#`). Here _buffer_number canonicalizes with fs_realpath, so it
    would find either name; the guard is `:p`, not realpath, for parity:
    both backends then shorten exactly the same paths, and it needs no disk
    access. What it turns away (measured 2026-09-29, nvim 0.12.5): a
    wrong-CASE path on macOS (`:.` shortens case-insensitively, so the short
    form's `:p` comes back in the cwd's case) and `~x/a.py` (its `:p` stays
    the relative `~x/a.py`). Both keep the full name, which nvim stores and
    finds as it did before relative names."""
    name: str = nvim.exec_lua(_DISPLAY_NAME_LUA, file_path)
    return name


def _name_without_swap(nvim: pynvim.Nvim, buf: pynvim.api.Buffer, file_path: str) -> None:
    """Name a new, empty buffer after file_path, opted out of swap first.

    Naming a swap-enabled buffer runs nvim's swap check for the new name, so
    when a LIVE second editor holds file_path's swap, `buf_set_name` raises
    E325 out of the RPC call. In a follower with a UI it also leaves the
    ATTENTION text on screen at a hit-enter prompt that blocks every later
    RPC call (measured 2026-09-28: a swap-held file whose buffer had vanished
    was retyped through show_fresh, the hook crashed on the E325 and the next
    RPC call hung). Same override, and for the same reasons, as
    _open_from_disk's before bufload: the buffer is display-only and never
    written, and `swapfile` is buffer-local, so the check still runs for
    every file the follower does not name. In an adopted nvim the caller
    turns swap back on once the buffer is current (_restore_swap_if_adopted)."""
    nvim.api.buf_set_option(buf, "swapfile", False)
    nvim.api.buf_set_name(buf, _display_name(nvim, file_path))


def _show_in_window(nvim: pynvim.Nvim, window: pynvim.api.Window, buf: int, file_path: str) -> None:
    """Put the buffer `buf` in `window`, make that window current, and wipe
    the empty buffer it showed before (_wipe_if_scratch). NavigationFailed
    when the current buffer is not `buf` afterwards (an autocommand moved
    the cursor) or the API refused: under 'nohidden', replacing a window's
    modified buffer raises E37, which used to escape the hook as a raw
    NvimError (final review of backlog-sweep-3, N2/N3)."""
    try:
        replaced = nvim.api.win_get_buf(window)
        nvim.api.win_set_buf(window, buf)
        nvim.api.set_current_win(window)
    except pynvim.NvimError as error:
        raise NavigationFailed(
            f"the follower's nvim could not show {file_path}: {error}"
        ) from error
    _wipe_if_scratch(nvim, replaced, buf)
    if nvim.api.get_current_buf().handle != buf:
        raise NavigationFailed(f"the follower's nvim did not land on {file_path}")


def _is_scratch(nvim: pynvim.Nvim, buffer: pynvim.api.Buffer) -> bool:
    """Whether `buffer` is the empty buffer a `:tabnew` makes: unnamed,
    unmodified and holding one empty line.

    Under 'nohidden' that buffer is already UNLOADED once replaced (measured,
    nvim 0.12), and an unloaded buffer has no lines to read: unloaded and
    unnamed it holds no text at all, so it counts as empty too."""
    return (
        nvim.api.buf_get_name(buffer) == ""
        and not nvim.api.buf_get_option(buffer, "modified")
        and (
            not nvim.api.buf_is_loaded(buffer)
            or (
                nvim.api.buf_line_count(buffer) == 1
                and nvim.api.buf_get_lines(buffer, 0, 1, False) == [""]
            )
        )
    )


def _wipe_if_scratch(nvim: pynvim.Nvim, scratch: pynvim.api.Buffer, kept: int) -> None:
    """Wipe `scratch` when it is a `:tabnew`'s empty buffer (_is_scratch)
    shown in no window, and not `kept`. Anything else may be the user's and
    is left alone.

    A buffer that is already gone is left gone, never asked anything: a
    'bufhidden=wipe' buffer is wiped by nvim the moment it leaves its last
    window, and a start screen (`:enew` reuses the empty startup buffer 1,
    then `setlocal bufhidden=wipe buftype=nofile`) is exactly that, so the
    first show_fresh in a dedicated nvim whose config draws one asked
    `buf_get_name` of a wiped buffer and crashed the hook with "Invalid
    buffer id: 1" (Task 24 item 1 of backlog-sweep-4)."""
    if (
        scratch.handle != kept
        and nvim.api.buf_is_valid(scratch.handle)
        and _is_scratch(nvim, scratch)
        and not nvim.funcs.win_findbuf(scratch.handle)
    ):
        nvim.command(f"silent! bwipeout {scratch.handle}")


def _give_back_hijacked_windows(nvim: pynvim.Nvim, shown_before: dict[int, int]) -> None:
    """Undo what a user autocommand that jumps on TabNew, BufNew, WinNew or
    BufAdd (`autocmd TabNew * tabfirst`) makes nvim's own `:tabnew` do: the
    jump runs before the tabnew's empty buffer is entered, so that buffer
    lands in whichever window the cursor jumped to, a USER's window in any
    tab. `shown_before` is every window of every tab before the tabnew
    (window handle -> buffer handle): each of them that now shows an empty
    scratch (_is_scratch) instead gets its own buffer back, by handle, and
    the scratch is wiped once shown nowhere. A window the follower created is
    never in that record, so it is never touched; a buffer that is gone is
    left gone. Measured (2026-10-06, headless nvim 0.12): without this a
    Read hid the user's buffer, under 'nohidden' too, and an early version
    that looked only at the window the tabnew was made from missed every
    jump to another tab (review of sweep-4 T8, I2: 92 of 896 matrix runs).
    The scratch is unmodified, so giving the window back runs no abandon
    check on it."""
    for window_handle, buffer_handle in shown_before.items():
        if not nvim.api.win_is_valid(window_handle) or not nvim.api.buf_is_valid(buffer_handle):
            continue
        now = nvim.api.win_get_buf(window_handle)
        if now.handle != buffer_handle and _is_scratch(nvim, now):
            nvim.api.win_set_buf(window_handle, buffer_handle)
            _wipe_if_scratch(nvim, now, buffer_handle)


@dataclass(frozen=True)
class NvimFollower:
    """Follower backend that animates edits in a real Neovim over the RPC API.

    Real-tab navigation (goto_file/ensure_showing, via pure API tabpage/
    window lookups — never an Ex command) gives multi-file parity with the
    tmux backend; this class also owns the connection, show_fresh,
    apply_edit, the per-line driver, full pause/resume + live-speed parity,
    and the launched-vs-adopted relock distinction."""

    socket_path: str
    window_id: str = ""
    pace_seconds: float = DEFAULT_PACE_SECONDS

    def _connect(self) -> pynvim.Nvim:
        """Bounded: the attach under CONNECT_TIMEOUT_SECONDS and every request
        under RPC_TIMEOUT_SECONDS (nvim_connect.bounded_attach), so a frozen
        nvim raises RpcTimeout, an OSError, instead of hanging the hook."""
        nvim: pynvim.Nvim = bounded_attach(self.socket_path)
        return nvim

    def _pace_provider(self) -> float:
        """Re-read the live speed from FollowerState each line so a running
        animation reacts to Ctrl+a +/- at its next line boundary (parity with
        the tmux backend's _live_pace). Falls back to the pace this follower
        was constructed with when there's no window state to read."""
        if not self.window_id:
            return self.pace_seconds
        state = FollowerState.read(self.window_id)
        if state is None:
            return self.pace_seconds
        return config.pace_seconds_for(state.speed)

    def _is_dedicated(self) -> bool:
        """Positively a launched, dedicated follower: its FollowerState exists
        and says not adopted (the flag start/auto-open persist). Every choice
        between the dedicated and the adopted behavior asks THIS, never "is it
        adopted?": with the state gone (a concurrent `claude-follow stop`
        between two entry points of one hook, or no window id) nothing proves
        the nvim isn't the user's own editor, so each fails closed onto the
        user's side: no lock (_locks_buffers), no key pressed (_guarded), no
        message captured (_exec), swap back on, the readonly asked about, and
        the eviction rules instead of a wipe (close_tab; review of sweep-4
        T4+T5, C1). The state must also be readable and name THIS socket
        (owned_state): a racing `stop` + `start` writes a dedicated state for
        ANOTHER follower (N1). An adopted nvim is never locked: locking the
        user out of their own buffer would be hostile."""
        state = owned_state(self.window_id, "nvim", self.socket_path)
        return state is not None and state.adopted is False

    def _locks_buffers(self) -> bool:
        """Whether this follower may lock a buffer (nomodifiable): only a
        follower its FollowerState PROVES dedicated. Fails closed like
        _is_dedicated, and for the same reason: with the state gone mid-hook
        (a concurrent `claude-follow stop`), nothing proves the nvim is not
        the user's own editor, and a lock there would shut them out of their
        own buffer. An unlocked dedicated buffer only costs a stray keystroke
        on a display-only buffer."""
        return self._is_dedicated()

    def is_alive(self) -> bool:
        """Something nvim-like still listens on the socket: the raw
        nvim_get_mode probe (nvim_connect.probe_nvim) answers, or accepts and
        stays SILENT — bounded, because `status`, `stop` and every hook ask it
        of each follower. Silence is not death: an nvim running `:!make`,
        `system()` or a long Lua loop answers nothing until it is done, just
        like a SIGSTOPped one, and calling it gone let a `stop` in another
        window collect its state and a hook launch a second follower over it.
        Callers that must not wait on it read nvim_connect.last_probe_verdict.
        Only "absent" (nobody listening) and "garbled" (not an nvim) are gone.
        nvim answers get_mode even at a hit-enter prompt, so a follower stuck
        on a plugin's message reads as alive and answering — the entry point
        that follows sweeps the prompt (see nvim_prompt)."""
        return probe_nvim(self.socket_path) in ("answers", "silent")

    @contextlib.contextmanager
    def _guarded(self, nvim: pynvim.Nvim, label: str) -> Iterator[None]:
        """Run an entry point's body under nvim_prompt's sweep + watchdog, so
        a plugin message raising a hit-enter prompt cannot freeze it. A no-op
        on an adopted nvim, and when an outer entry point already guards this
        socket."""
        with prompt_guard(
            nvim, self._connect, key=self.socket_path, adopted=not self._is_dedicated(), label=label
        ):
            yield

    def _restore_swap_if_adopted(self, nvim: pynvim.Nvim) -> None:
        """Swap back on for the CURRENT buffer, in an adopted nvim only, with
        the ATTENTION message suppressed for that one step (_SWAP_BACK_ON).

        A toggle that fails (E303: no writable 'directory'; a user OptionSet
        autocmd that errors) raises out of exec2. Uncaught, it aborted
        show_fresh right after the rename and left an empty buffer. It is
        logged instead, and the buffer is left swap-off: the state the
        follower already chose before the re-enable, and the safe side.

        Skipped only when the state PROVES a dedicated follower
        (_is_dedicated): with the state gone, turning swap back on is the safe
        side (a dedicated follower's buffer with swap on only costs a swap
        file; the user's buffer with it off loses crash recovery)."""
        if self._is_dedicated():
            return
        try:
            nvim.api.exec2(_SWAP_BACK_ON, {"output": True})
        except pynvim.NvimError as exc:
            logger.warning("adopted nvim: could not turn swap back on, left off: %s", exc)
            nvim.api.buf_set_option(nvim.api.get_current_buf(), "swapfile", False)

    def _exec(self, nvim: pynvim.Nvim, command: str) -> None:
        """An event-firing Ex command (filetype detect, edit!, bufload): its
        synchronous plugin output is captured and logged, see exec_logged."""
        exec_logged(nvim, command, adopted=not self._is_dedicated())

    def _drive(
        self,
        nvim: pynvim.Nvim,
        buf: int,
        run: Callable[[], AnimationResult],
        expected: Sequence[str] | None,
        format_from: str | None = None,
    ) -> AnimationResult:
        """Shared envelope for every animation: unlock the buffer, mark the
        window "running" (a state change only: the hook that acquired the
        slot releases it, not this driver), run, then relock
        (nomodifiable) on a completed outcome — but only for a launched,
        dedicated follower. An adopted nvim is the user's own editor and is
        never relocked. An interrupt hands the buffer to the user, so it is
        deliberately left modifiable regardless.

        A completed run stamps the buffer clean (_IS_CLEAN_LUA) only when it
        holds exactly `expected`, the lines the run was meant to leave
        (_STAMP_IF_EXPECTED_LUA); None (a caller that cannot say) never
        stamps, so the buffer reads "not clean", the side that never
        discards typing.

        A completed run also sets the buffer's 'bomb' and 'fileformat' from
        the file at `format_from` (see _format_from_disk); None leaves them
        as they are.

        Signals are NOT cleared here: one pressed since the hook claimed the
        slot is addressed to this process and must survive to the first check;
        a stale one is addressed elsewhere and is dropped there (see
        control._request).

        Every request in here runs without the RPC deadline
        (nvim_connect.unbounded_requests): mid-animation, an nvim that stops
        answering is waiting on its user — a hit-enter prompt in an adopted
        nvim holds the typing until Enter, then it finishes. Cutting it
        crashed the hook, left a half-typed line and the "Writing..." float
        up, and the abandoned request still landed at Enter."""
        with nvim_connect.unbounded_requests(nvim):
            nvim.api.buf_set_option(buf, "modifiable", True)
            control.mark_animating(self.window_id)
            result = run()
            if result.outcome != "interrupted":
                if format_from is not None:
                    _format_from_disk(nvim, buf, format_from)
                # Clean only if it holds exactly what Claude wrote: see _IS_CLEAN_LUA.
                if expected is not None:
                    nvim.exec_lua(_STAMP_IF_EXPECTED_LUA, buf, list(expected))
                if self._locks_buffers():
                    nvim.api.buf_set_option(buf, "modifiable", False)
        return result

    def show_fresh(self, file_path: str, content: str, in_new_tab: bool = False) -> AnimationResult:
        """Rebuild the buffer named `file_path` from `content` and type it in.

        Deliberately never `:e`/`:edit` here: that would load the file's real
        (already-written) content and flash the finished result before the
        retype. The buffer is wiped and renamed in place, seeded with a single
        blank line, then each content line is animated in. in_new_tab opens a
        fresh tab first (:tabnew, no disk read) instead of reusing the
        current window — real multi-file parity with the tmux backend.

        Only a follower its state PROVES dedicated (_is_dedicated) may reuse
        the current window: in an adopted nvim, or one whose state is gone,
        that window is the user's, so every file opens in a tab of its own
        whatever the caller asked. The hook passes the state's `shown_any`,
        which an adopted nvim's state never set: its first Write took over
        the user's window and hid their buffer, and under 'nohidden' an
        unnamed nofile scratch there lost its text (review of sweep-4 T8,
        I1). Decided here, from ownership, not from a state field that can
        be missing."""
        nvim = self._connect()
        in_new_tab = in_new_tab or not self._is_dedicated()
        with self._guarded(nvim, "show_fresh"):
            ns = nvim.api.create_namespace(_NAMESPACE)
            # Wipe by NUMBER and name via the API: the Ex forms take the path as a
            # pattern / command-line argument, so `[`, `{`, `*` missed the old
            # buffer (then E95 on the rename) and `#`/`%` raised E499.
            stale = _buffer_number(nvim, file_path)
            if stale != -1:
                nvim.command(f"silent! bwipeout! {stale}")
            buf = self._fresh_buffer(nvim, file_path, in_new_tab)
            # Read the file in and clear it, in ONE command (nvim redraws only
            # between requests, so the content is never on screen): naming
            # left the buffer "not edited", and a plain `:w` after an
            # interrupt failed with E13. Same fix and reasons as the tmux
            # backend's s:rename (_VIM_RENAME); swap is still off here.
            nvim.exec_lua(_READ_THEN_CLEAR_LUA, buf)
            self._restore_swap_if_adopted(nvim)
            self._exec(nvim, "filetype detect")
            nvim.command("setlocal buftype=")
            lines = tuple(content.splitlines())

            def run() -> AnimationResult:
                nvim.api.buf_set_lines(buf, 0, -1, True, [""])

                def save_pending(index: int) -> None:
                    control.save_pending_show_fresh(
                        self.window_id,
                        lines[index:],
                        self._pace_provider(),
                        continuation=index > 0,
                        file_path=file_path,
                        # The buffer was wiped to a bare seed blank before this
                        # run, so the fully-typed lines ARE the whole partial. The
                        # line being typed when a pause lands is deliberately
                        # absent: it is the remainder's first entry and gets
                        # retyped from scratch, so counting it here too is exactly
                        # what duplicated it on the later catch-up.
                        partial=_terminated(lines[:index]),
                    )

                result = _animate_lines(
                    nvim,
                    buf,
                    lines,
                    0,
                    self._pace_provider,
                    self.window_id,
                    ns,
                    save_pending=save_pending,
                )
                if result.outcome == "completed" and lines:
                    # Drop the seed blank line the retype pushed to the bottom, so
                    # the buffer holds exactly `content` with no trailing blank.
                    nvim.api.buf_set_lines(buf, len(lines), len(lines) + 1, True, [])
                return result

            return self._drive(nvim, buf, run, list(lines) or [""], format_from=file_path)

    def _fresh_buffer(self, nvim: pynvim.Nvim, file_path: str, in_new_tab: bool) -> int:
        """A new buffer named file_path, shown in a new tab (in_new_tab,
        _show_in_new_tab) or the current window, made current; its handle.

        Created BY HANDLE and named before it is shown, never "the current
        buffer after `:tabnew`": a user autocommand that jumps on the tabnew
        (`BufEnter * if bufname('%') ==# '' … | tabfirst`) made the follower
        rename the USER's buffer onto Claude's file and the read-and-clear
        empty it, unsaved text and all (final review of backlog-sweep-3, I1,
        reproduced at acce2b2). If it is not current afterwards
        (NavigationFailed), the new buffer is wiped again: nothing has acted
        on any other buffer."""
        created = nvim.api.create_buf(True, False)
        _name_without_swap(nvim, created, file_path)
        nvim.api.buf_set_var(created, _CREATED, 1)
        handle: int = created.handle
        try:
            if in_new_tab:
                self._show_in_new_tab(nvim, handle, file_path)
            else:
                _show_in_window(nvim, nvim.api.get_current_win(), handle, file_path)
        except NavigationFailed:
            nvim.command(f"silent! bwipeout! {handle}")
            raise
        return handle

    def _show_in_new_tab(self, nvim: pynvim.Nvim, buf: int, file_path: str) -> pynvim.api.Window:
        """Show the existing buffer `buf` in one new tab and make it current;
        that tab's window, which the caller may mark as the follower's
        (`w:vaf_opened`).

        The tab is found BY HANDLE (the one tabpage `:tabnew` added), never
        as "the current window after `:tabnew`": a user autocommand that
        jumps tabs on the tabnew (`BufEnter * if bufname('%') ==# '' |
        tabfirst | endif`) put Claude's file into the USER's window, leaked
        one empty tab per Read, marked the user's window `w:vaf_opened` (which
        an adopted eviction then closed), and under 'nohidden' raised a raw
        E37 out of every Read (final review of backlog-sweep-3, N2/N3). The
        empty buffer the tabnew made is wiped once replaced, as `:enew`
        reuses an empty unnamed one (final M7: one leaked per Read). Raises
        NavigationFailed when no tab was added or `buf` is not current
        afterwards; it never wipes `buf`, which may be the user's (a caller
        that created it wipes it)."""
        before = {tab.handle for tab in nvim.api.list_tabpages()}
        shown_before = {
            window.handle: nvim.api.win_get_buf(window).handle for window in nvim.api.list_wins()
        }
        nvim.command("tabnew")
        added = [tab for tab in nvim.api.list_tabpages() if tab.handle not in before]
        if not added:
            raise NavigationFailed(f"the follower's nvim did not open a tab for {file_path}")
        window: pynvim.api.Window = nvim.api.tabpage_get_win(added[0])
        try:
            _give_back_hijacked_windows(nvim, shown_before)
        except pynvim.NvimError as error:
            raise NavigationFailed(
                f"the follower's nvim could not open a tab for {file_path}: {error}"
            ) from error
        _show_in_window(nvim, window, buf, file_path)
        return window

    def _run_ops(
        self,
        nvim: pynvim.Nvim,
        buf: int,
        ns: int,
        ops: list[EditOp],
        pace_provider: Callable[[], float],
        file_path: str,
    ) -> AnimationResult:
        """The shared op-loop behind both apply_edit and resume: each op
        deletes its old range (instant, via nvim_buf_set_lines) and animates
        its new lines in with `pace_provider`, checking for a signal at every
        op and line boundary. On a pause it persists the op-granular remainder
        (the current op onward) as the crash fallback and blocks until resume
        or interrupt — the same contract as animate.run_ops."""
        # The buffer is still clean here (nothing typed yet), so this is the
        # one honest reading of "what was on screen before this run" — the
        # base every persisted partial below is computed from. Read once:
        # mid-run the buffer may end on a half-typed line, which is precisely
        # the shape a crash-fallback consumer must not have to interpret.
        initial = _terminated(nvim.api.buf_get_lines(buf, 0, -1, True))
        index = 0
        while index < len(ops):
            op = ops[index]

            # One callback serving both boundaries: _wait_while_paused calls it
            # with no args (op-boundary pause), _animate_lines with the line
            # index (mid-op pause) — either way the saved remainder is
            # op-granular (the current op onward). The persisted pace is the
            # live pace, so a crash-fallback replay picks up the user's speed;
            # a live nvim resume just continues typing.
            def save_pending(_line: int = 0, _op: int = index) -> None:
                control.save_pending_apply_edit(
                    self.window_id,
                    ops[_op:],
                    self._pace_provider(),
                    file_path=file_path,
                    # Op-granular on this side too: the partly-typed op
                    # contributes nothing, exactly as the remainder replays it
                    # whole. Each op's line numbers are relative to the state
                    # its predecessors produced, so replaying the prefix onto
                    # `initial` reproduces the buffer without reading it.
                    partial=apply_ops(initial, ops[:_op]),
                )

            signal = control.check_signal(self.window_id)
            if signal == "interrupt":
                return AnimationResult("interrupted", index)
            if signal == "pause":
                if not _wait_while_paused(self.window_id, save_pending, None):
                    return AnimationResult("interrupted", index)
                continue  # resumed: retry this op from its clean boundary
            # start_line-1/end_line are the 0-indexed, end-exclusive range
            # nvim_buf_set_lines wants (same convention as diff.apply_ops).
            # An op whose range spans the buffer's own top-to-bottom (starts
            # at line 1 and reaches at least the current last line) empties
            # it, and nvim can never hold zero lines — it silently keeps one
            # implicit blank line, the same role show_fresh's own seed blank
            # plays (see its docstring). _animate_lines always inserts each
            # new line BEFORE its target row, so that implicit blank gets
            # pushed past every typed line instead of being consumed by one.
            # Snapshot the pre-delete count now: after the delete call it can
            # no longer tell a genuinely-emptied buffer from one that already
            # held a single real line.
            wipes_buffer = op.start_line == 1 and op.end_line >= nvim.api.buf_line_count(buf)
            # EditOp carries no old text, so keep the range we are about to
            # delete: an interrupt mid-op has to put it back (see below). Only
            # an op that animates new lines can stop mid-op — a delete-only op
            # runs to completion between two signal checks — so only that one
            # pays for the snapshot.
            old_lines = (
                nvim.api.buf_get_lines(buf, op.start_line - 1, op.end_line, True)
                if op.new_lines
                else []
            )
            nvim.api.buf_set_lines(buf, op.start_line - 1, op.end_line, True, [])
            if op.new_lines:
                # How many rows sit where the op's range used to be, right
                # after the delete: zero when the delete emptied the buffer
                # (the one line nvim insists on keeping is the implicit
                # blank, not content).
                rows_after_delete = nvim.api.buf_line_count(buf) - (1 if wipes_buffer else 0)
                result = _animate_lines(
                    nvim,
                    buf,
                    op.new_lines,
                    op.start_line - 1,
                    pace_provider,
                    self.window_id,
                    ns,
                    save_pending=save_pending,
                )
                if result.outcome == "interrupted":
                    # Roll the op back so the buffer the user takes over is
                    # exactly apply_ops(before, ops[:index]) — the state the
                    # interrupt notification quotes to Claude as "what was
                    # shown". Without this the old range is already gone while
                    # `index` says the op never happened, and the notification
                    # over-reports the buffer by every line this op deleted.
                    # The tmux backend gets the same guarantee from its `u`
                    # undos; here `:undo` is unusable — one undo entry per
                    # typed character, and in an adopted nvim the undo tree
                    # belongs to the user — so restore the region by hand.
                    # Its length is measured, not derived: _animate_lines
                    # inserts a blank row per line before typing into it, so a
                    # half-typed line occupies a row too, and a wiping op's
                    # implicit blank has to be swallowed by the same write or
                    # the restored buffer would carry a spurious trailing one.
                    occupied = nvim.api.buf_line_count(buf) - rows_after_delete
                    start = op.start_line - 1
                    nvim.api.buf_set_lines(buf, start, start + occupied, True, old_lines)
                    return AnimationResult("interrupted", index)
                if wipes_buffer:
                    # Drop the implicit blank now pushed past every typed
                    # line, mirroring show_fresh's own seed-blank cleanup.
                    # A delete-only op (no new_lines, handled above) never
                    # reaches here: the implicit blank IS the correct final
                    # state for a buffer emptied with nothing to replace it.
                    drop = op.start_line - 1 + len(op.new_lines)
                    nvim.api.buf_set_lines(buf, drop, drop + 1, True, [])
            index += 1
        return AnimationResult("completed", len(ops))

    def apply_edit(
        self, file_path: str, ops: list[EditOp], before: str | None = None
    ) -> AnimationResult:
        """Apply an edit script to file_path's buffer: each op deletes its
        old range (instant, via nvim_buf_set_lines) and animates its new lines
        in, checking for a signal at every op and line boundary.

        `before` is the content the ops were computed against (the hook's
        snapshot, as the buffer holds it). The tmux backend needs it for its
        crash-fallback `partial`; _run_ops reads this buffer directly at run
        start instead. Here it says what a completed run leaves:
        apply_ops(before, ops), the only content _drive stamps clean. Without
        it (None) the buffer is left unstamped.

        The ops were computed against a SNAPSHOT of that buffer, so they are
        meaningless without it. When the buffer is gone (an adopted nvim
        restarted on the same socket, or the user :bwipeout'd it) goto_file
        would hand us a brand-new EMPTY buffer and the op-loop would either
        fabricate wrong content (small ops "succeed") or raise nvim's "Index
        out of bounds" on any op touching a later line — uncaught, that
        escapes the hook process. Show the real on-disk content instead: this
        runs from PostToolUse, after Claude's write, so disk already holds
        exactly the post-edit content. Reported as completed(len(ops)) so the
        caller's bookkeeping (writer-cue refresh, open-file tracking) runs its
        normal non-interrupted path — nothing downstream assumes the
        animation actually typed. This branch never reaches _drive, so
        _open_from_disk locks the buffer itself: a completed animation
        leaves the buffer locked on both backends, and this is the one path
        that returns "completed" without going through the machinery that
        normally guarantees it."""
        nvim = self._connect()
        with self._guarded(nvim, "apply_edit"):
            if _buffer_number(nvim, file_path) == -1:
                self._open_from_disk(nvim, file_path)
                return AnimationResult("completed", len(ops))
            self.goto_file(file_path)
            ns = nvim.api.create_namespace(_NAMESPACE)
            buf = nvim.api.get_current_buf().handle
            return self._drive(
                nvim,
                buf,
                lambda: self._run_ops(nvim, buf, ns, ops, self._pace_provider, file_path),
                None if before is None else _buffer_lines(apply_ops(before, ops)),
                format_from=file_path,
            )

    def reload_and_relock(self, file_path: str) -> None:
        """Des-interrupt with NO stored remainder: discard the user's unsaved
        typing by reloading the file Claude wrote (`edit!`), then relock. The
        buffer's name matches that file, so — unlike show_fresh — we WANT the
        reload to load the finished disk content here (the user discarded their
        typing, so the file on disk is the truth). A launched, dedicated
        follower is relocked nomodifiable; an adopted nvim is the user's own
        editor and is never locked out of its own buffer (same rule as
        _drive)."""
        nvim = self._connect()
        with self._guarded(nvim, "reload_and_relock"):
            self.goto_file(file_path)
            self._exec(nvim, "edit!")
            if self._locks_buffers():
                buf = nvim.api.get_current_buf().handle
                nvim.api.buf_set_option(buf, "modifiable", False)

    def reload_from_disk(self, file_path: str) -> None:
        """Ground a buffer holding only follower text on the file on disk (a
        catch-up that just completed, hooks._animate_edit). Exactly the
        des-interrupt's reload: `edit!`, then the lock a dedicated follower
        gets; an adopted nvim is never locked."""
        self.reload_and_relock(file_path)

    def rewrite_buffer(self, file_path: str, content: str) -> AnimationResult:
        """Instantly (no animation) rebuild the buffer to `content` — the
        des-interrupt replay needs the buffer back at the interrupt-point state,
        discarding the user's unsaved typing, before resume() replays the
        remainder at live pace. Leaves the buffer UNLOCKED and seedless (exactly
        `content`): resume() runs immediately after and owns the relock."""
        nvim = self._connect()
        with self._guarded(nvim, "rewrite_buffer"):
            self.goto_file(file_path)
            buf = nvim.api.get_current_buf().handle
            nvim.api.buf_set_option(buf, "modifiable", True)
            lines = content.splitlines()
            nvim.api.buf_set_lines(buf, 0, -1, True, lines or [""])
            return AnimationResult("completed", len(lines))

    def _resume_fresh(
        self,
        nvim: pynvim.Nvim,
        buf: int,
        ns: int,
        lines: tuple[str, ...],
        pace_provider: Callable[[], float],
        file_path: str,
        seeded: bool,
    ) -> AnimationResult:
        """Append the remaining whole lines of an interrupted fresh retype to
        the current buffer, landing at EXACTLY the final content.

        The seed subtlety: show_fresh only drops its trailing seed blank on a
        COMPLETED outcome, so the two resume entry points hand us different
        buffer shapes. `seeded` carries that provenance EXPLICITLY — the buffer
        shape can't: a legit trailing blank in the content is indistinguishable
        from the seed by sniffing existing[-1] == "" (that ambiguity was the
        bug). The des-interrupt replay (seeded=False) runs right after
        rewrite_buffer, which rebuilt the buffer to EXACTLY the partial with no
        seed — so append at the very end. The pace-0 consume (seeded=True) runs
        on the LIVE interrupted buffer, which still carries the trailing seed
        blank: type the remainder in front of it and drop it on completion,
        exactly as show_fresh does."""
        existing = nvim.api.buf_get_lines(buf, 0, -1, True)
        start_row = len(existing) - 1 if seeded else len(existing)
        # Everything above start_row is the partial this replay is continuing
        # from — the seed blank, when there is one, sits below it and is not
        # part of the content. _animate_lines only ever inserts at or after
        # start_row, so these rows stay valid for the whole run.
        prefix = existing[:start_row]

        def save_pending(index: int) -> None:
            control.save_pending_show_fresh(
                self.window_id,
                lines[index:],
                self._pace_provider(),
                continuation=start_row + index > 0,
                file_path=file_path,
                partial=_terminated([*prefix, *lines[:index]]),
            )

        result = _animate_lines(
            nvim,
            buf,
            lines,
            start_row,
            pace_provider,
            self.window_id,
            ns,
            save_pending=save_pending,
        )
        if result.outcome == "completed" and seeded:
            # Drop the seed blank the retype pushed to the bottom.
            drop = start_row + len(lines)
            nvim.api.buf_set_lines(buf, drop, drop + 1, True, [])
        return result

    def resume(
        self,
        pending: PendingApplyEdit | PendingShowFresh,
        *,
        seeded: bool = False,
        reload: bool = True,
    ) -> AnimationResult:
        """Replay a saved animation remainder (des-interrupt live replay, or a
        pace-0 crash-fallback consume). Mirrors the tmux backend: re-select the
        tab, then replay the op-loop (PendingApplyEdit) or append the remaining
        whole lines (PendingShowFresh), wrapped in _drive so a completed replay
        relocks (unless adopted) and an interrupt hands the buffer over.

        `seeded` records whether the current buffer still carries show_fresh's
        trailing seed blank (True for the live pace-0 consume; False for the
        des-interrupt replay onto a seedless rewrite_buffer). Only PendingShow
        Fresh consults it; PendingApplyEdit ignores it.

        The pace-0 catch-up must stay silent forever: pending.pace_seconds == 0
        selects a fixed-0 provider that never re-reads live speed mid-catch-up
        (parity with tmux.resume).

        When the remainder's buffer is GONE, the replay is abandoned whole and
        NOTHING is touched — no navigation, no buffer creation, no relock.
        Deliberately not a disk load, unlike ensure_showing/apply_edit: loading
        here would leave the buffer holding the POST-edit disk content, and the
        apply_edit that hooks._animate_edit runs right after this (the pace-0
        catch-up precedes the new edit) would then find bufnr != -1 and animate
        before->after ops on top of after-content — garbage, or the same "Index
        out of bounds", one call later. Leaving the buffer absent hands the
        decision to apply_edit's own guard (or to show_fresh on the is_fresh
        path), which is the one with the content to show. Reported as completed
        for the whole remainder so no caller treats it as an interrupt. The
        des-interrupt replay can never reach this branch: hooks._await_user_
        handoff always calls rewrite_buffer first, which goes through goto_file
        and therefore recreates the buffer, and only resumes when that returned
        completed.

        `reload` is part of the Follower protocol: False marks the pre-edit
        catch-up, where disk already holds the NEXT edit (the tmux backend's
        relock then skips its re-read, see TmuxVimFollower.resume). This
        relock never re-reads the buffer; reload only decides whether a
        completed replay takes 'bomb' and 'fileformat' from disk
        (_format_from_disk), which the next edit's file must not set early."""
        nvim = self._connect()
        with self._guarded(nvim, "resume"):
            if pending.file_path and _buffer_number(nvim, pending.file_path) == -1:
                remaining = pending.ops if isinstance(pending, PendingApplyEdit) else pending.lines
                return AnimationResult("completed", len(remaining))
            ns = nvim.api.create_namespace(_NAMESPACE)
            if pending.file_path:
                self.goto_file(pending.file_path)
            buf = nvim.api.get_current_buf().handle
            provider = (lambda: 0.0) if pending.pace_seconds == 0.0 else self._pace_provider
            format_from = pending.file_path if reload and pending.file_path else None
            # What a completed replay leaves, from the remainder's own partial;
            # a legacy remainder without one (None) is never stamped clean.
            if isinstance(pending, PendingApplyEdit):
                return self._drive(
                    nvim,
                    buf,
                    lambda: self._run_ops(nvim, buf, ns, pending.ops, provider, pending.file_path),
                    None
                    if pending.partial is None
                    else _buffer_lines(apply_ops(pending.partial, pending.ops)),
                    format_from=format_from,
                )
            return self._drive(
                nvim,
                buf,
                lambda: self._resume_fresh(
                    nvim, buf, ns, pending.lines, provider, pending.file_path, seeded
                ),
                None
                if pending.partial is None
                else _buffer_lines(pending.partial + _terminated(pending.lines)),
                format_from=format_from,
            )

    def hand_over(self) -> None:
        """Unlock the current buffer for direct user editing. The interrupt
        path already leaves the buffer modifiable via _drive; this is the
        explicit re-assert used when a des-interrupt replay is itself
        interrupted. Acts on whatever buffer is current — safe here because
        it always runs immediately after resume() left the right tab
        current; unlike apply_edit, it has no independent file_path to
        navigate to."""
        nvim = self._connect()
        with self._guarded(nvim, "hand_over"):
            buf = nvim.api.get_current_buf().handle
            nvim.api.buf_set_option(buf, "modifiable", True)

    def goto_file(self, file_path: str) -> None:
        """Switch to the window showing file_path (by name, immune to the
        user closing/reordering tabs), opening one if missing. Checks every
        window of every tab, not just each tab's focused window — a buffer
        can be visible in an unfocused split (normal in an adopted nvim, the
        user's own editor), and checking only the focused window would miss
        it and open a duplicate tab for the same file (measured live,
        2026-09-15). set_current_win switches tabpage AND window in one
        call. Pure API calls only — never an Ex :edit/:drop/:buffer, which
        would trigger Vim's "abandon unsaved changes" guard (E37) or
        silently discard typed-but-unsaved content and reload from disk,
        exactly what this backend must never do (its buffers are never
        written; see show_fresh). This is the entry point that never SHOWS
        disk content — a missing buffer is created EMPTY here, because
        show_fresh's callers must not see the finished file flashed before
        it is typed. Its one disk read is the read-then-clear of that
        missing buffer (_READ_THEN_CLEAR_LUA): it lands on the buffer just
        created, empty and in a tab of its own, inside one request, so
        nothing is on screen and nothing can be discarded; it only makes the
        buffer "edited", so a later plain `:w` saves it. ensure_showing is
        the sibling that shows disk content. Every new tab is found by
        handle (_show_in_new_tab). Looked up via _buffer_number, never
        bufnr() — see there."""
        nvim = self._connect()
        with self._guarded(nvim, "goto_file"):
            bufnr = _buffer_number(nvim, file_path)
            if bufnr != -1:
                for tabpage in nvim.api.list_tabpages():
                    for win in nvim.api.tabpage_list_wins(tabpage):
                        if nvim.api.win_get_buf(win).number == bufnr:
                            nvim.api.set_current_win(win)
                            return
                # Buffer exists but isn't shown in any window — reachable (e.g.
                # after a show_fresh(in_new_tab=False) hijacks the one existing
                # tab, or after eviction touches a stale reference) — show it in
                # a new tab via API rather than risk any Ex command's
                # unsaved-changes guard.
                #
                # An UNLOADED buffer (`nvim a.py b.py` leaves b.py listed but
                # unloaded) is loaded from disk by win_set_buf, and the load
                # runs the swap check: with another editor holding the file's
                # swap it raised E325 out of this call (and so out of a Read's
                # ensure_showing), measured 2026-09-28. Opt it out first, as
                # _open_from_disk does before bufload. A loaded buffer passed
                # its check when it was loaded and keeps its swap.
                loading = not nvim.api.buf_is_loaded(bufnr)
                if loading:
                    nvim.api.buf_set_option(bufnr, "swapfile", False)
                window = self._show_in_new_tab(nvim, bufnr, file_path)
                # The window is the follower's, the buffer is not (s:evict).
                # Marked by the handle _show_in_new_tab proved is the new
                # tab's, never window 0, which a jumping autocommand makes
                # the user's own.
                nvim.api.win_set_var(window, _OPENED, 1)
                if loading:
                    self._restore_swap_if_adopted(nvim)
                return
            # No such buffer at all — create one in a new tab, then read the
            # file in and clear it in one request (_READ_THEN_CLEAR_LUA, as
            # show_fresh does): naming left it "not edited", so a plain `:w`
            # after a hand-off hit E13 (C2-C5 (c)).
            buf = self._fresh_buffer(nvim, file_path, in_new_tab=True)
            nvim.exec_lua(_READ_THEN_CLEAR_LUA, buf)
            self._restore_swap_if_adopted(nvim)

    def _open_from_disk(self, nvim: pynvim.Nvim, file_path: str) -> None:
        """Open file_path's REAL on-disk content in a new tab.

        Pure API (bufadd + bufload + win_set_buf) rather than `:edit`, for two
        measured reasons (2026-09-16, real headless nvim). First, `:edit`
        needs the path escaped: a perfectly ordinary name containing `#` or
        `%` raises E499 ("Empty file name for '%' or '#'"), while bufadd takes
        the path as data. Second, bufload only loads a buffer that is NOT
        already loaded, so it can never discard typed-but-unsaved content —
        no "abandon changes" guard (E37) is even in play, whereas `:edit`'s
        safety relies on the argument that tabnew's scratch window makes E37
        unreachable. bufadd applies nvim's own name canonicalization (macOS
        /tmp -> /private/tmp), which _buffer_number's realpath compare sees
        through, so a later lookup finds this very buffer. bufadd creates the buffer
        unlisted; list it, for parity with goto_file's create_buf(True, ...).
        A file that does not exist on disk is fine: the buffer opens empty,
        exactly as `:edit` on a new file would.

        Locked (nomodifiable) before returning when this is a launched,
        dedicated follower — nothing animates this buffer afterwards, so it
        must land in the same locked state ensure_showing's other branch
        does. An adopted nvim is the user's own editor and is never locked
        by this backend, navigation included: the controller's earlier
        ruling to mirror tmux's unconditional lock here (2026-09-16) has
        been reversed, restoring the same launched/adopted split `_drive`'s
        completion relock uses (see its docstring — locking the user out of
        their own buffer is hostile regardless of which entry point does
        it). Shared by both of this backend's disk-reading callers:
        ensure_showing (Read navigation) and apply_edit's vanished-buffer
        branch, where a "completed" outcome must leave the buffer locked
        exactly like a completed animation run through _drive would, unless
        adopted.

        Opens the file even when a LIVE second editor holds its swap. Left
        alone, `bufload` answers that with `E325: ATTENTION` raised straight
        out of the RPC call (the channel stays responsive — there is no stall
        like the tmux backend's), which escaped this method uncaught and took
        the hook process down instead of showing the file, on both callers.
        Safe to override because this backend's buffers are display-only and
        are never written to disk, so it cannot clobber the other editor's
        work. A STALE swap was never a problem: nvim recognises a swap whose
        owning Nvim is gone and ignores it (W325).

        Measured 2026-09-21, real headless nvim 0.12.5, two instances sharing
        one `directory`, the second holding the file open and unsaved:

        | candidate                            | loads | leaks | own swap |
        |--------------------------------------|-------|-------|----------|
        | (none — status quo)                  | E325  |   -   |     -    |
        | buf_set_option swapfile=False first  |  yes  |  no   |   none   |
        | SwapExists autocmd, v:swapchoice='e' | E325  |   -   |     -    |
        | catch NvimError /E325/ and continue  |  yes  |  no   |  .swo    |

        Re-measured with a real VIM (not a second nvim) as the swap owner,
        the likelier case for a Vim user running an adopted follower: same
        E325 unfixed, same clean load fixed. The override disables the CHECK,
        so it does not care which editor took the swap out.

        The autocmd — the tmux backend's mechanism, translated — simply does
        not apply here: `SwapExists` never fires under `bufload` (measured
        with a marker variable: still 0 after the raise), only under Ex
        `:edit`, which this method avoids for the E499/E37 reasons above.
        Catching E325 does work, because nvim finishes loading the correct
        content before raising, but it leans on undocumented post-error state
        and leaves the follower's buffer holding a SECOND swap file (`.swo`)
        for a file it will never write — litter that makes the next editor to
        open that file see a stale-swap prompt. Restoring swapfile=True after
        the load creates the same `.swo`, so the option stays off.

        Scoping: `buf_set_option` is buffer-local, on the buffer bufadd just
        created. Measured after the call, the global `swapfile` is still on,
        a freshly added unrelated buffer still reports swapfile on, and
        `bufload` on an unrelated swap-held file still raises E325 — so an
        adopted nvim's own behaviour, for every file the follower never
        touched, is unchanged. Must run BEFORE bufload: the swap check runs
        during the load, so setting it afterwards is a no-op that still
        raises."""
        bufnr = nvim.funcs.bufadd(_display_name(nvim, file_path))
        nvim.api.buf_set_option(bufnr, "swapfile", False)
        nvim.api.buf_set_var(bufnr, _CREATED, 1)
        # bufload fires BufRead/FileType, so the user's plugins run inside it:
        # through _exec, a message they print lands in hook.log instead of a
        # hit-enter prompt (nvim_prompt).
        self._exec(nvim, f"call bufload({bufnr})")
        nvim.api.buf_set_option(bufnr, "buflisted", True)
        try:
            self._show_in_new_tab(nvim, bufnr, file_path)
        except NavigationFailed:
            nvim.command(f"silent! bwipeout! {bufnr}")
            raise
        self._restore_swap_if_adopted(nvim)
        self._exec(nvim, "filetype detect")
        if self._locks_buffers():
            nvim.api.buf_set_option(bufnr, "modifiable", False)

    def probe_buffer(self, file_path: str, content: str) -> BufferProbe:
        """What file_path's buffer holds relative to `content`, the base an
        edit script is about to be typed onto (see BufferProbe). Read
        straight over RPC. An unloaded buffer (its tab closed under
        'nohidden') is "absent" with no check of its own: buf_get_lines of an
        unloaded buffer is an empty list (measured, nvim 0.12) — and
        goto_file's win_set_buf would load the finished file from disk into
        it. Pure reads — no navigation, no lock change, adopted or not.
        buf_get_lines decodes with surrogateescape, so a buffer holding bytes
        that are not UTF-8 never equals the hook's decoded `content`: it
        reads as "differs".

        A buffer in the follower's own typed form (backends.typed_form) also
        "holds" when the follower produced it: a dedicated follower's buffer,
        or one still clean (_IS_CLEAN_LUA: unmodified, or stamped at a
        completed animation and untouched since). That is what a retype of a
        file with MIXED line endings leaves (every CR dropped), which nvim's
        own load never shows. An adopted buffer the user changed into that
        form (`:%s/\\r$//`) is not the base and stays "differs", so an
        adopted editor leaves it alone (T12+T13 review I1)."""
        nvim = self._connect()
        with self._guarded(nvim, "probe_buffer"):
            bufnr = _buffer_number(nvim, file_path)
            if bufnr == -1:
                return "absent"
            lines: list[str] = nvim.api.buf_get_lines(bufnr, 0, -1, False)
            held = classify_buffer(lines, content)
            if (
                held == "differs"
                and lines == typed_form(content)
                and (self._is_dedicated() or nvim.exec_lua(_IS_CLEAN_LUA, bufnr))
            ):
                return "holds"
            return held

    def ensure_showing(self, file_path: str) -> None:
        """Show file_path with its REAL on-disk content — the Read-navigation
        and binary-file entry point, where the finished content is exactly
        what should appear because nothing is animated afterwards.

        This is THE entry point of this backend that shows disk content;
        goto_file never shows it (its one read, the read-then-clear of a
        buffer it creates, is cleared in the same request), because
        show_fresh's callers rely on the finished file not being flashed
        before it is typed. An existing
        buffer is switched to and re-read only when it is CLEAN (see
        _reload_if_clean), in a dedicated and an adopted nvim alike, so a
        file rewritten outside Claude's Edits (a formatter, `sed -i`, a
        checkout) is shown as it is on disk: parity with the tmux backend's
        s:reload (`_VIM_RELOAD`). A buffer holding the user's unsaved typing is
        never re-read.

        Both branches lock the buffer (nomodifiable) before returning, via
        the same `buf_set_option` call `_drive`'s completion relock uses —
        a stray keystroke here must not corrupt a buffer that isn't being
        actively animated, exactly the tmux backend's rationale for locking
        after `:tab drop` — but, like `_drive`, ONLY for a launched,
        dedicated follower. An adopted nvim is the user's own editor and is
        never locked by this backend, navigation included: a controller
        ruling on 2026-09-16 made this lock unconditional to mirror tmux
        exactly, which contradicted this backend's own design (and the
        README's promise that an adopted editor is never locked read-only);
        that ruling has since been reversed. See `_drive`'s docstring for
        why locking the user out of their own buffer is hostile regardless
        of which entry point would do it. `hand_over` is still how a
        launched follower's buffer comes back to the user."""
        nvim = self._connect()
        with self._guarded(nvim, "ensure_showing"):
            if _buffer_number(nvim, file_path) != -1:
                self.goto_file(file_path)
                buf = nvim.api.get_current_buf().handle
                self._reload_if_clean(nvim, buf)
                if self._locks_buffers():
                    nvim.api.buf_set_option(buf, "modifiable", False)
                return
            self._open_from_disk(nvim, file_path)

    def _reload_if_clean(self, nvim: pynvim.Nvim, buf: int) -> None:
        """Re-read the CURRENT buffer `buf` from disk when it is clean
        (_IS_CLEAN_LUA); leave it alone otherwise. Never writes.

        Prompt-free: `silent` keeps the "file" line and any autocommand's
        message from raising a hit-enter prompt in an adopted nvim, whose
        prompts nothing answers; a dedicated one also captures and logs the
        output (_exec).

        Swap-safe as it stands, measured 2026-09-28: unlike Vim (see the tmux
        backend's _VIM_RELOAD), nvim's `:edit!` of a buffer that already
        has a swap keeps that swap and runs no new swap search, so with
        another nvim holding the file's `.swp` a UI nvim re-read with no
        ATTENTION and kept its `.swo` (tests/test_e2e_adopted_swap.py), and
        headless nvim raised nothing. A dedicated follower's buffers have
        swap off. Turning swap off around the re-read, the way show_fresh and
        _open_from_disk opt out before a FIRST load, was tried: no test could
        tell it apart, and it deletes and recreates the user's swap file."""
        if nvim.exec_lua(_IS_CLEAN_LUA, buf):
            self._exec(nvim, "silent edit!")

    def close_tab(self, file_path: str, dedicated: bool | None = None) -> None:
        # Wipe by the number _buffer_number resolves, never by name: bwipeout
        # takes a PATTERN, so a raw name can miss the buffer or hit a
        # pattern-sibling (see _buffer_number). goto_file
        # first for structural parity with reload_and_relock/rewrite_buffer —
        # measured that bwipeout! alone (without navigating there first)
        # already closes the right tab regardless of which one is current,
        # so this preamble isn't load-bearing, just consistent style.
        #
        # An ADOPTED nvim is the user's own editor: _EVICT_LUA decides, and
        # nothing navigates first (goto_file would create a buffer for a
        # file no buffer holds, only to wipe it again). So does an nvim whose
        # state is GONE (a `claude-follow stop` racing a Read: the hook read
        # the state before its eviction, this re-reads it): only a state that
        # proves a dedicated follower gets the wipe (review of sweep-4 T4+T5,
        # C1: the fail-open check wiped the user's unsaved buffer). A caller
        # that already knows (`claude-follow stop`, which clears the state
        # before its teardown) says so in `dedicated`, and is trusted.
        nvim = self._connect()
        with self._guarded(nvim, "close_tab"):
            if not (self._is_dedicated() if dedicated is None else dedicated):
                if (
                    dedicated is None
                    and owned_state(self.window_id, "nvim", self.socket_path) is None
                ):
                    log_state_gone(self.window_id, file_path)
                bufnr = _buffer_number(nvim, file_path)
                if bufnr != -1:
                    log_adopted_close(file_path, nvim.exec_lua(_EVICT_LUA, bufnr))
                return
            self.goto_file(file_path)
            bufnr = _buffer_number(nvim, file_path)
            if bufnr != -1:
                nvim.command(f"silent! bwipeout! {bufnr}")

    def user_readonly(self, file_path: str) -> bool:
        """See Follower.user_readonly. This backend never sets 'readonly'
        itself, so in an adopted nvim a readonly buffer is the user's. A
        follower its state proves dedicated never asks (its readonly is never
        the user's); an unknown state asks, since the plain cue's `:w` fails
        with E45 on the user's readonly. A lookup that fails is False, the
        plain cue."""
        if self._is_dedicated():
            return False
        try:
            nvim = self._connect()
            bufnr = _buffer_number(nvim, file_path)
            return bufnr != -1 and bool(nvim.api.buf_get_option(bufnr, "readonly"))
        except (OSError, pynvim.NvimError):
            logger.warning("adopted nvim: could not read %s's readonly", file_path)
            return False

    def goto_line(self, offset: int) -> None:
        """Move the cursor to line `offset`, CLAMPED to the buffer's last
        line. An out-of-range Read offset is normal — Claude can read a file
        at an offset that a later edit made shorter — and nvim's cursor
        setter answers it with "Invalid cursor line: out of range", which
        escapes the hook process uncaught. The caller only ever passes
        offset > 0, so a lower clamp would be dead code."""
        nvim = self._connect()
        with self._guarded(nvim, "goto_line"):
            nvim.current.window.cursor = (min(offset, nvim.api.buf_line_count(0)), 0)

    def stop(self, dedicated: bool | None = None) -> None:
        # Quit the dedicated nvim this follower launched — its tmux split
        # closes with it. cmd_stop only routes a NON-adopted follower here (an
        # adopted nvim goes to close_tab) and, having cleared the window's
        # FollowerState first, says what it read in `dedicated`, which is
        # trusted. Without it the guard fails CLOSED: qall! only when the
        # state proves a dedicated follower (_is_dedicated) — an adopted
        # nvim, or one whose state is gone, may be the user's own editor,
        # and quitting it would be hostile.
        # A prompt left on screen would hold qall! in the queue behind it, so
        # sweep it first (fast requests only; no watchdog — its connection
        # would only drop with the process). A connect or sweep that fails
        # raises, for the caller to log; only the qall! itself is quiet,
        # because it tears down the RPC channel and the call may raise as
        # the socket drops.
        if not (self._is_dedicated() if dedicated is None else dedicated):
            return
        nvim = self._connect()
        dismiss_prompt(nvim, self.socket_path)
        with contextlib.suppress(Exception):
            nvim.command("qall!")
