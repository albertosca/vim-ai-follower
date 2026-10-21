from __future__ import annotations

import pytest

from vim_ai_follower.backends import typed_text
from vim_ai_follower.diff import (
    EditOp,
    apply_ops,
    compute_edit_script,
    file_lines,
    is_binary,
    text_lines,
)


def test_no_changes_returns_empty_list() -> None:
    assert compute_edit_script("a\nb\nc", "a\nb\nc") == []


def test_pure_insertion_at_end() -> None:
    ops = compute_edit_script("a\nb", "a\nb\nc\nd")
    assert ops == [EditOp(kind="insert", start_line=3, end_line=2, new_lines=("c", "d"))]


def test_pure_insertion_at_start() -> None:
    ops = compute_edit_script("b\nc", "a\nb\nc")
    assert ops == [EditOp(kind="insert", start_line=1, end_line=0, new_lines=("a",))]


def test_pure_deletion() -> None:
    ops = compute_edit_script("a\nb\nc\nd", "a\nd")
    assert ops == [EditOp(kind="delete", start_line=2, end_line=3, new_lines=())]


def test_replace_single_line() -> None:
    ops = compute_edit_script("a\nb\nc", "a\nX\nc")
    assert ops == [EditOp(kind="replace", start_line=2, end_line=2, new_lines=("X",))]


def test_multiple_ops_ordered_bottom_to_top() -> None:
    ops = compute_edit_script("a\nb\nc\nd\ne", "a\nX\nc\nd\nY")
    assert [operation.start_line for operation in ops] == [5, 2]


def test_empty_before_is_full_insertion() -> None:
    ops = compute_edit_script("", "a\nb")
    assert ops == [EditOp(kind="insert", start_line=1, end_line=0, new_lines=("a", "b"))]


def test_empty_after_is_full_deletion() -> None:
    ops = compute_edit_script("a\nb", "")
    assert ops == [EditOp(kind="delete", start_line=1, end_line=2, new_lines=())]


def test_is_binary_true_for_null_byte() -> None:
    assert is_binary(b"hello\x00world") is True


def test_is_binary_false_for_plain_text() -> None:
    assert is_binary(b"hello world\n") is False


def test_apply_ops_replace_single_line() -> None:
    ops = [EditOp(kind="replace", start_line=2, end_line=2, new_lines=("X",))]
    assert apply_ops("a\nb\nc", ops) == "a\nX\nc\n"


def test_apply_ops_pure_insertion_at_top() -> None:
    ops = [EditOp(kind="insert", start_line=1, end_line=0, new_lines=("a",))]
    assert apply_ops("b\nc", ops) == "a\nb\nc\n"


def test_apply_ops_pure_insertion_at_end() -> None:
    ops = [EditOp(kind="insert", start_line=3, end_line=2, new_lines=("c", "d"))]
    assert apply_ops("a\nb", ops) == "a\nb\nc\nd\n"


def test_apply_ops_pure_deletion() -> None:
    ops = [EditOp(kind="delete", start_line=2, end_line=3, new_lines=())]
    assert apply_ops("a\nb\nc\nd", ops) == "a\nd\n"


def test_apply_ops_empty_list_returns_before_unchanged() -> None:
    # "Unchanged" up to the canonical terminated form apply_ops always returns
    # (see tests/test_partial_form.py for why the return has to be lossless).
    assert apply_ops("a\nb", []) == "a\nb\n"
    assert apply_ops("a\nb\n", []) == "a\nb\n"


def test_apply_ops_multiple_ops_applied_in_given_order() -> None:
    # compute_edit_script returns ops bottom-to-top; apply_ops must apply
    # them in that same given order (not re-sort or reverse) for the line
    # numbers in later ops to stay valid.
    before = "a\nb\nc\nd\ne"
    after = "a\nX\nc\nd\nY"
    ops = compute_edit_script(before, after)
    assert [operation.start_line for operation in ops] == [5, 2]  # already bottom-to-top
    assert apply_ops(before, ops) == after + "\n"


def test_apply_ops_reconstructs_partial_prefix_of_a_multi_op_diff() -> None:
    # The actual use case: only the first K of N ops "happened" so far.
    before = "a\nb\nc\nd\ne"
    after = "a\nX\nc\nd\nY"
    ops = compute_edit_script(before, after)
    assert apply_ops(before, ops[:1]) == "a\nb\nc\nd\nY\n"
    assert apply_ops(before, ops[:0]) == before + "\n"


@pytest.mark.parametrize(
    ("text", "lines"),
    [
        ("", []),
        ("\n", [""]),
        ("a", ["a"]),
        ("a\n", ["a"]),
        ("a\n\n", ["a", ""]),
        (
            "a\x0cb\x0bc\x1cd\x1de\x1ef\x85g\u2028h\u2029i\n",
            ["a\x0cb\x0bc\x1cd\x1de\x1ef\x85g\u2028h\u2029i"],
        ),
        ("a\r\nb\n", ["a\r", "b"]),
        ("a\rb\n", ["a\rb"]),
    ],
)
def test_text_lines_splits_on_lf_only(text: str, lines: list[str]) -> None:
    assert text_lines(text) == lines


@pytest.mark.parametrize(
    ("content", "lines"),
    [
        ("", []),
        ("a\r\nb\r\n", ["a", "b"]),  # all CRLF: fileformat=dos keeps the CRs
        ("a\r\nb\n", ["a\r", "b"]),  # mixed: unix, each CR kept in its line
        ("a\r\nb", ["a\r", "b"]),
        ("a\nb\n", ["a", "b"]),
    ],
)
def test_file_lines_drops_the_crs_only_of_an_all_crlf_file(content: str, lines: list[str]) -> None:
    assert file_lines(content) == lines


def test_typed_text_is_the_buffer_s_lines_each_newline_terminated() -> None:
    assert typed_text("\ufeffa\r\nb\r\n") == "a\nb\n"
    assert typed_text("a\r\nb\n") == "a\r\nb\n"
    assert typed_text("a\x0cb") == "a\x0cb\n"
    assert typed_text("") == ""


def test_an_edit_script_keeps_a_form_feed_inside_its_line() -> None:
    assert compute_edit_script("a\x0cb\n", "a\x0cb\nc\n") == [
        EditOp(kind="insert", start_line=2, end_line=1, new_lines=("c",))
    ]
    assert apply_ops("a\x0cb\n", [EditOp("insert", 2, 1, ("c",))]) == "a\x0cb\nc\n"
