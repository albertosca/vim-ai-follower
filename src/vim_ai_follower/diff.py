"""Pure, I/O-free line diffing: compute a bottom-to-top edit script between
two file versions, replay it, and detect binary content."""

from __future__ import annotations

import difflib
from dataclasses import dataclass
from typing import Literal

EditKind = Literal["replace", "delete", "insert"]


@dataclass(frozen=True)
class EditOp:
    kind: EditKind
    start_line: int
    end_line: int
    new_lines: tuple[str, ...]


def text_lines(text: str) -> list[str]:
    """The lines of text in the form the follower types and keeps (a typed
    file, a partial): split on LF only, a final LF ending the last line
    rather than starting an empty one, and "" no line at all. Never
    str.splitlines(), which also breaks at a form feed, \\x0b, \\x1c-\\x1e,
    U+0085 and U+2028/9 and drops a CR before a LF: an editor keeps all of
    them inside its line (BACKLOG "Mixed line endings")."""
    if not text:
        return []
    return text.removesuffix("\n").split("\n")


def file_lines(content: str) -> list[str]:
    """The lines an editor's buffer holds for a file whose text is `content`
    (text_lines), except that a file whose every LF ends a CRLF loses those
    CRs: Vim and nvim load it with fileformat=dos and keep the endings in the
    option. A last line with no LF is not a terminated one and keeps what it
    has (`a\r\nb` loads as dos ['a', 'b'], `a\r\nb\r` as dos ['a', 'b\r'],
    a lone `a\r` as unix; measured on Vim 9.2 and nvim 0.12, final review of
    backlog-sweep-5, C1: a CRLF file with no final EOL was typed with its CRs
    into a dos buffer, and the save doubled them). A file with MIXED endings
    loads with fileformat=unix and each CR kept in its line, and so it is
    typed (backends.buffer_forms reads a buffer the same way)."""
    lines = text_lines(content)
    terminated = content.split("\n")[:-1]
    if terminated and all(line.endswith("\r") for line in terminated):
        return [line[:-1] if index < len(terminated) else line for index, line in enumerate(lines)]
    return lines


def compute_edit_script(before: str, after: str) -> list[EditOp]:
    """`before` and `after` in typed form (backends.typed_text)."""
    before_lines = text_lines(before)
    after_lines = text_lines(after)
    matcher = difflib.SequenceMatcher(None, before_lines, after_lines, autojunk=False)

    ops: list[EditOp] = []
    for tag, before_start, before_end, after_start, after_end in matcher.get_opcodes():
        if tag == "equal":
            continue
        if tag == "replace":
            kind: EditKind = "replace"
        elif tag == "delete":
            kind = "delete"
        else:
            kind = "insert"
        ops.append(
            EditOp(
                kind=kind,
                start_line=before_start + 1,
                end_line=before_end,
                new_lines=tuple(after_lines[after_start:after_end]),
            )
        )
    ops.reverse()
    return ops


def apply_ops(before: str, ops: list[EditOp]) -> str:
    """Replay `ops` onto `before` and return the resulting buffer content.

    The return terminates every line with "\\n" instead of joining with it,
    which is the canonical form of a persisted `partial` (hooks._terminated
    builds the show_fresh side the same way). Every caller here reconstructs a
    partial that is later split again (text_lines) — by rewrite_buffer, by the
    next apply_ops that takes it as a base, and by the interrupt notification —
    and a "\\n".join round trip is LOSSY for a trailing blank line:
    ['a','b',''] -> "a\\nb\\n" -> ['a','b'] drops it. Terminating is lossless.

    `before` is read through text_lines() either way, so a base may arrive in
    either form: "a\\nb" and "a\\nb\\n" are the same two lines and yield
    identical line numbering. An empty result stays "" (no lone newline) —
    both consumers derive `seeded` from the partial's truthiness."""
    lines = text_lines(before)
    for operation in ops:
        if operation.end_line >= operation.start_line:
            del lines[operation.start_line - 1 : operation.end_line]
        insert_at = operation.start_line - 1
        lines[insert_at:insert_at] = list(operation.new_lines)
    return "".join(line + "\n" for line in lines)


def is_binary(content: bytes) -> bool:
    return b"\x00" in content[:8192]
