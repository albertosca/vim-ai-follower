"""A whole-file retype says why, and a stale file whose buffer still holds
the content it had when an edit was skipped gets a diff, not a retype.

BACKLOG "Whole-file retype on every change" (2026-10-20): every skipped
edit marks its file stale, and a DEDICATED follower used to send every
stale file straight to show_fresh, which wipes the buffer and types the
whole file again. The buffer of a skipped edit's file still holds that
edit's `before` (nothing was sent to it), so the skip records it as the
file's "stale base" and the next edit probes for it: "holds" is a diff from
it. The probe is exact equality, so a base that is wrong for any reason
only falls back to the retype.
"""

from __future__ import annotations

import logging
from pathlib import Path
from unittest.mock import MagicMock, call

import pytest
from test_hooks_base_mismatch import AFTER, BEFORE, _follower, _run_hook, _setup

from vim_ai_follower import diff, hooks, snapshot, state

# What the buffer held when an earlier edit (BEFORE_SKIPPED → BEFORE) was skipped.
BEFORE_SKIPPED = "a\nb\nc\nd\n"


def _probe_answers(answers: dict[str, str]) -> MagicMock:
    """A follower whose probe answers by the base it is asked about."""
    follower = _follower("differs")
    follower.probe_buffer.side_effect = lambda _file, base: answers.get(base, "differs")
    return follower


@pytest.mark.parametrize(
    ("setup", "stale_base", "answers", "reason"),
    [
        ({"is_open": False}, None, {}, "not open (never opened, or evicted)"),
        ({"stale": True}, None, {}, "stale, no base recorded"),
        ({"stale": True}, BEFORE_SKIPPED, {}, "stale, base differs"),
        ({"stale": True}, BEFORE_SKIPPED, {BEFORE_SKIPPED: "absent"}, "stale, base absent"),
        ({}, None, {BEFORE: "differs"}, "probe differs"),
        ({}, None, {BEFORE: "absent"}, "probe absent"),
        ({}, None, {BEFORE: "unknown"}, "probe unknown"),
    ],
)
def test_every_whole_file_retype_logs_its_reason(
    setup: dict[str, bool],
    stale_base: str | None,
    answers: dict[str, str],
    reason: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    target, resolved = _setup(tmp_path, **setup)
    if stale_base is not None:
        snapshot.save_stale_base("@1", resolved, stale_base)
    follower = _probe_answers(answers)
    with caplog.at_level(logging.INFO, logger="vim_ai_follower"):
        _run_hook(target, follower, monkeypatch)

    assert follower.show_fresh.call_args == call(resolved, AFTER, in_new_tab=True)
    retype_lines = [
        record.getMessage() for record in caplog.records if "retype" in record.getMessage()
    ]
    assert retype_lines == [f"retype {resolved}: {reason}"]


@pytest.mark.parametrize("adopted", [False, True])
def test_a_stale_file_holding_its_recorded_base_gets_a_diff_from_it(
    adopted: bool, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target, resolved = _setup(tmp_path, stale=True, adopted=adopted)
    snapshot.save_stale_base("@1", resolved, BEFORE_SKIPPED)
    follower = _probe_answers({BEFORE_SKIPPED: "holds"})
    _run_hook(target, follower, monkeypatch)

    assert follower.show_fresh.call_args_list == []
    assert follower.apply_edit.call_args == call(
        resolved, diff.compute_edit_script(BEFORE_SKIPPED, AFTER), before=BEFORE_SKIPPED
    )
    follower_state = state.FollowerState.read("@1")
    assert follower_state is not None
    assert follower_state.stale_files == ()
    assert snapshot.load_stale_base("@1", resolved) is None


def test_an_adopted_stale_buffer_resynced_to_disk_still_gets_the_snapshot_diff(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A user's `:e!` makes the buffer the snapshot, not the recorded base:
    the base's "differs" must not leave a buffer alone that holds the
    snapshot."""
    target, resolved = _setup(tmp_path, stale=True, adopted=True)
    snapshot.save_stale_base("@1", resolved, BEFORE_SKIPPED)
    follower = _probe_answers({BEFORE: "holds"})
    _run_hook(target, follower, monkeypatch)

    assert follower.show_fresh.call_args_list == []
    assert follower.apply_edit.call_args.kwargs == {"before": BEFORE}


def test_a_dedicated_stale_buffer_not_holding_its_base_is_still_retyped(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target, resolved = _setup(tmp_path, stale=True)
    snapshot.save_stale_base("@1", resolved, BEFORE_SKIPPED)
    follower = _probe_answers({})
    _run_hook(target, follower, monkeypatch)

    assert follower.apply_edit.call_args_list == []
    assert follower.show_fresh.call_args == call(resolved, AFTER, in_new_tab=True)
    follower_state = state.FollowerState.read("@1")
    assert follower_state is not None
    assert follower_state.stale_files == ()


def test_a_skip_records_the_skipped_edits_base_and_keeps_the_first(tmp_path: Path) -> None:
    """The first skip records what the buffer holds (that edit's snapshot);
    a second skip of the same stale file must not replace it, since the
    buffer still holds the first edit's base."""
    _target, resolved = _setup(tmp_path)
    hooks._mark_stale("@1", resolved)
    assert snapshot.load_stale_base("@1", resolved) == BEFORE

    snapshot.save("@1", resolved, AFTER)
    hooks._mark_stale("@1", resolved)
    assert snapshot.load_stale_base("@1", resolved) == BEFORE
    follower_state = state.FollowerState.read("@1")
    assert follower_state is not None
    assert follower_state.stale_files == (resolved,)


def test_a_skip_of_a_file_with_no_tab_records_no_base(tmp_path: Path) -> None:
    _target, resolved = _setup(tmp_path, is_open=False)
    hooks._mark_stale("@1", resolved)
    assert snapshot.load_stale_base("@1", resolved) is None
