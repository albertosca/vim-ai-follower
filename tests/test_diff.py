from __future__ import annotations

from vim_ai_follower.diff import EditOp, compute_edit_script, is_binary


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
    assert [op.start_line for op in ops] == [5, 2]


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
