from __future__ import annotations

from typing import Literal, Protocol

from vim_ai_follower.animate import DEFAULT_PACE_SECONDS, AnimationResult
from vim_ai_follower.control import PendingApplyEdit, PendingShowFresh
from vim_ai_follower.diff import EditOp

# The answer to "is this buffer the edit's base?", in the four shapes the hook
# acts on differently:
#   - "holds": loaded, and exactly `content` — a diff can be typed onto it;
#   - "absent": no buffer, or one that is listed but unloaded — nothing is in
#     it, so replacing it (show_fresh's wipe) loses nothing;
#   - "differs": loaded with other content — in an ADOPTED editor that may be
#     the user's unsaved typing, which a wipe would destroy;
#   - "unknown": the editor never answered.
BufferProbe = Literal["holds", "differs", "absent", "unknown"]


class NavigationFailed(RuntimeError):
    """The editor did not confirm landing on the file a call had to act on,
    so the call stopped before sending anything that acts on the current
    buffer (the tmux backend's landing check). The hooks log it in one line
    and skip the edit; the message says why."""


class Follower(Protocol):
    """A target that can show and animate file edits. Multi-file navigation
    is tab-based in both backends: the tmux backend's goto_file sends one
    Ex line via keystrokes that switches to an already-loaded buffer by
    number and falls back to `:tab drop` only for a file no buffer holds
    (a drop would re-read a clean buffer from disk); the nvim backend's goto_file finds or opens
    the tab via its RPC API (never an Ex command, to avoid discarding
    unsaved-but-never-written buffer content — E37 / silent disk reload).
    Either way, close_tab is the generic eviction primitive the hook calls
    on whatever falls past max_tabs."""

    def is_alive(self) -> bool: ...

    def ensure_showing(self, file_path: str) -> None: ...

    # What file_path's buffer holds relative to `content`, the base an edit
    # script is about to be typed onto (see BufferProbe). A backend that
    # cannot tell answers "unknown", never a guess.
    def probe_buffer(self, file_path: str, content: str) -> BufferProbe: ...

    def goto_file(self, file_path: str) -> None: ...

    def close_tab(self, file_path: str) -> None: ...

    # `before` is the content these ops were computed against. It exists for
    # the tmux backend, whose driver sends keystrokes and can never read the
    # buffer back, so it cannot compute the crash-fallback `partial` a pause
    # persists without being told the base. The nvim backend reads its own
    # buffer at run start and ignores the argument. Optional so a caller with
    # no snapshot can still animate — the pending is then saved with
    # partial=None and the consumer falls back to the live buffer.
    def apply_edit(
        self, file_path: str, ops: list[EditOp], before: str | None = None
    ) -> AnimationResult: ...

    def show_fresh(
        self, file_path: str, content: str, in_new_tab: bool = False
    ) -> AnimationResult: ...

    def goto_line(self, offset: int) -> None: ...

    def stop(self) -> None: ...

    def reload_and_relock(self, file_path: str) -> None: ...

    def reload_from_disk(self, file_path: str) -> None: ...

    def rewrite_buffer(self, file_path: str, content: str) -> AnimationResult: ...

    def resume(
        self,
        pending: PendingApplyEdit | PendingShowFresh,
        *,
        seeded: bool = False,
        reload: bool = True,
    ) -> AnimationResult: ...

    def hand_over(self) -> None: ...

    # Whether file_path's buffer, as handed over to the user at an interrupt,
    # is readonly by the USER's own setting (an adopted editor only; a
    # dedicated follower's readonly is never the user's). The hand-off cue
    # then asks for `:w!`, because a plain `:w` fails with E45. A backend
    # that cannot tell answers False (the plain cue).
    def user_readonly(self, file_path: str) -> bool: ...


def buffer_forms(content: str) -> list[list[str]]:
    """The line lists an editor buffer holding the file `content` can show.

    One trailing newline is the last line's terminator, not an extra empty
    line (an editor's buffer of "a\nb\n" is ["a", "b"]), and an empty file is
    a single empty line. A file whose every line ends in CR is also accepted
    without them, because Vim and nvim load it with fileformat=dos and strip
    the CRs from the buffer. Splits on "\n" only, never str.splitlines(),
    which would also break on form feeds and U+2028 that stay inside an
    editor's line."""
    text = content[:-1] if content.endswith("\n") else content
    lines = text.split("\n")
    forms = [lines]
    if all(line.endswith("\r") for line in lines):
        forms.append([line[:-1] for line in lines])
    return forms


def classify_buffer(lines: list[str], content: str) -> BufferProbe:
    """What a buffer showing `lines` holds relative to the file `content`. An
    empty list is "absent": a loaded buffer always has at least one line, so
    only a missing or unloaded buffer reads back as none (getbufline() in Vim,
    buf_get_lines in nvim)."""
    if not lines:
        return "absent"
    return "holds" if lines in buffer_forms(content) else "differs"


def get_follower(
    backend: str,
    target: str,
    pace_seconds: float = DEFAULT_PACE_SECONDS,
    window_id: str = "",
) -> Follower:
    if backend == "tmux":
        from vim_ai_follower.backends.tmux_vim import TmuxVimFollower

        return TmuxVimFollower(pane_id=target, pace_seconds=pace_seconds, window_id=window_id)
    if backend == "nvim":
        from vim_ai_follower.backends.nvim import NvimFollower

        return NvimFollower(socket_path=target, window_id=window_id, pace_seconds=pace_seconds)
    raise ValueError(f"unknown backend: {backend!r}")
