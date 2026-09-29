from __future__ import annotations

import fcntl
import os
import subprocess
import sys
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

from vim_ai_follower import control
from vim_ai_follower.diff import EditOp


@pytest.fixture
def animating(tmp_path: Path) -> None:
    """This test process owns @1's animation, as a hook does while it types:
    signals are addressed to the live owner."""
    control.mark_animating("@1", base_dir=tmp_path)


def test_check_signal_returns_none_when_nothing_set(tmp_path: Path) -> None:
    assert control.check_signal("@1", base_dir=tmp_path) is None


@pytest.mark.usefixtures("animating")
def test_check_signal_returns_and_consumes_pause(tmp_path: Path) -> None:
    control.request_pause("@1", base_dir=tmp_path)
    assert control.check_signal("@1", base_dir=tmp_path) == "pause"
    assert control.check_signal("@1", base_dir=tmp_path) is None


@pytest.mark.usefixtures("animating")
def test_check_signal_returns_and_consumes_interrupt(tmp_path: Path) -> None:
    control.request_interrupt("@1", base_dir=tmp_path)
    assert control.check_signal("@1", base_dir=tmp_path) == "interrupt"
    assert control.check_signal("@1", base_dir=tmp_path) is None


@pytest.mark.usefixtures("animating")
def test_check_signal_interrupt_takes_priority_over_pause(tmp_path: Path) -> None:
    control.request_pause("@1", base_dir=tmp_path)
    control.request_interrupt("@1", base_dir=tmp_path)
    assert control.check_signal("@1", base_dir=tmp_path) == "interrupt"
    # pause is left alone, still pending
    assert control.check_signal("@1", base_dir=tmp_path) == "pause"


@pytest.mark.usefixtures("animating")
def test_clear_signals_removes_both_without_erroring_if_absent(tmp_path: Path) -> None:
    control.clear_signals("@1", base_dir=tmp_path)  # nothing to clear, no error
    control.request_pause("@1", base_dir=tmp_path)
    control.request_interrupt("@1", base_dir=tmp_path)
    control.clear_signals("@1", base_dir=tmp_path)
    assert control.check_signal("@1", base_dir=tmp_path) is None


def test_has_pending_animation_false_when_none_saved(tmp_path: Path) -> None:
    assert control.has_pending_animation("@1", base_dir=tmp_path) is False


def test_save_and_load_pending_apply_edit(tmp_path: Path) -> None:
    ops = [EditOp(kind="replace", start_line=2, end_line=2, new_lines=("X",))]
    control.save_pending_apply_edit("@1", ops, 0.05, base_dir=tmp_path)
    assert control.has_pending_animation("@1", base_dir=tmp_path) is True

    loaded = control.load_pending_animation("@1", base_dir=tmp_path)
    assert isinstance(loaded, control.PendingApplyEdit)
    assert loaded.ops == ops
    assert loaded.pace_seconds == 0.05
    # consumed on load
    assert control.has_pending_animation("@1", base_dir=tmp_path) is False


def test_save_and_load_pending_show_fresh(tmp_path: Path) -> None:
    control.save_pending_show_fresh("@1", ("a", "b"), 0.1, base_dir=tmp_path)
    loaded = control.load_pending_animation("@1", base_dir=tmp_path)
    assert isinstance(loaded, control.PendingShowFresh)
    assert loaded.lines == ("a", "b")
    assert loaded.pace_seconds == 0.1


def test_pending_show_fresh_round_trips_continuation(tmp_path: Path) -> None:
    control.save_pending_show_fresh("@1", ("x",), 0.05, continuation=True, base_dir=tmp_path)
    pending = control.load_pending_animation("@1", tmp_path)
    assert pending == control.PendingShowFresh(("x",), 0.05, continuation=True)


def test_pending_show_fresh_defaults_continuation_false(tmp_path: Path) -> None:
    control.save_pending_show_fresh("@1", ("x",), 0.05, base_dir=tmp_path)
    pending = control.load_pending_animation("@1", tmp_path)
    assert pending == control.PendingShowFresh(("x",), 0.05, continuation=False)


def test_pending_round_trips_file_path(tmp_path: Path) -> None:
    control.save_pending_show_fresh("@1", ("a",), 0.03, base_dir=tmp_path, file_path="/tmp/f.py")
    pending = control.load_pending_animation("@1", base_dir=tmp_path)
    assert pending is not None and pending.file_path == "/tmp/f.py"


def test_pending_defaults_file_path_for_old_payloads(tmp_path: Path) -> None:
    control.save_pending_apply_edit("@1", [], 0.03, base_dir=tmp_path)
    pending = control.load_pending_animation("@1", base_dir=tmp_path)
    assert pending is not None and pending.file_path == ""


def test_load_pending_animation_returns_none_when_absent(tmp_path: Path) -> None:
    assert control.load_pending_animation("@1", base_dir=tmp_path) is None


def test_discard_pending_animation_removes_file_without_erroring_if_absent(
    tmp_path: Path,
) -> None:
    control.discard_pending_animation("@1", base_dir=tmp_path)  # no error
    control.save_pending_apply_edit("@1", [], 0.0, base_dir=tmp_path)
    control.discard_pending_animation("@1", base_dir=tmp_path)
    assert control.has_pending_animation("@1", base_dir=tmp_path) is False


@pytest.mark.usefixtures("animating")
def test_check_signal_survives_losing_the_claim_race(tmp_path: Path) -> None:
    control.request_interrupt("@1", tmp_path)
    real_rename = Path.rename

    def racing_rename(self: Path, target: Path) -> Path:
        self.unlink()  # another process got there first...
        return real_rename(self, target)  # ...then ours runs on a gone file

    with patch.object(Path, "rename", racing_rename):
        # losing the race means the signal was not ours to act on — and the
        # animation process must NOT crash mid-animation over it
        assert control.check_signal("@1", tmp_path) is None


def test_pending_write_is_atomic_no_partial_file_visible(tmp_path: Path) -> None:
    op = EditOp(kind="insert", start_line=1, end_line=0, new_lines=("x",))
    control.save_pending_apply_edit("@1", [op], 0.1, tmp_path)
    leftovers = sorted(p.name for p in tmp_path.iterdir())
    assert leftovers == ["@1.pending_animation.json"]  # no .tmp residue
    # and the write goes through an atomic rename, not a direct write:
    with patch.object(Path, "replace", autospec=True, side_effect=Path.replace) as replace:
        control.save_pending_show_fresh("@1", ("y",), 0.1, base_dir=tmp_path)
    assert replace.called


def test_load_pending_returns_none_when_file_vanishes_mid_read(tmp_path: Path) -> None:
    control.save_pending_show_fresh("@1", ("y",), 0.1, base_dir=tmp_path)
    with patch.object(Path, "read_text", side_effect=FileNotFoundError):
        assert control.load_pending_animation("@1", tmp_path) is None
    # the real file is still there for the next, non-racing loader:
    assert control.load_pending_animation("@1", tmp_path) is not None


def test_animating_marker_lifecycle(tmp_path: Path) -> None:
    assert control.is_animating("@1", tmp_path) is False
    control.mark_animating("@1", tmp_path)
    assert control.is_animating("@1", tmp_path) is True  # this test process is alive
    control.clear_animating("@1", tmp_path)
    assert control.is_animating("@1", tmp_path) is False


def test_animating_marker_ignores_dead_process(tmp_path: Path) -> None:
    control.mark_animating("@1", tmp_path)
    # overwrite with a PID that cannot be running (max pid + unlikely)
    (tmp_path / "@1.animating").write_text("99999999")
    assert control.is_animating("@1", tmp_path) is False


def test_animating_marker_ignores_garbage_content(tmp_path: Path) -> None:
    control.mark_animating("@1", tmp_path)
    (tmp_path / "@1.animating").write_text("not-a-pid")
    assert control.is_animating("@1", tmp_path) is False


def test_try_acquire_animating_claims_the_slot_when_free(tmp_path: Path) -> None:
    assert control.try_acquire_animating("@1", tmp_path) is True
    assert control.animating_state("@1", tmp_path) == "running"


def test_try_acquire_animating_rejects_a_second_live_holder(tmp_path: Path) -> None:
    assert control.try_acquire_animating("@1", tmp_path) is True
    # A second attempt while a live process (this one) still holds the slot
    # loses — this is the atomic guard six parallel hooks race through.
    assert control.try_acquire_animating("@1", tmp_path) is False


def test_try_acquire_animating_reclaims_a_dead_holders_marker(tmp_path: Path) -> None:
    (tmp_path / "@1.animating").write_text("99999999 running")  # crashed hook, dead pid
    assert control.try_acquire_animating("@1", tmp_path) is True
    assert control.animating_state("@1", tmp_path) == "running"  # now ours, alive


def test_try_acquire_animating_loses_a_reclaim_race(tmp_path: Path) -> None:
    (tmp_path / "@1.animating").write_text("99999999 running")  # stale, dead pid
    # The link always reports the name taken: the stale marker is detected and
    # unlinked, but the claim loses to a competitor that linked the free name
    # first, so acquire gives up instead of clobbering the winner.
    with patch("vim_ai_follower.control.os.link", side_effect=FileExistsError):
        assert control.try_acquire_animating("@1", tmp_path) is False
    assert not list(tmp_path.glob("*.tmp"))  # the unpublished claim is cleaned up


def test_try_acquire_animating_skips_while_another_hook_reclaims(tmp_path: Path) -> None:
    (tmp_path / "@1.animating").write_text("99999999 running")  # stale, dead pid
    # Another hook holds the reclaim lock: it is mid-reclaim, and waiting for
    # it would only find it the owner. Skip, leaving the marker to it.
    with (tmp_path / "@1.animating.lock").open("w") as held:
        fcntl.flock(held, fcntl.LOCK_EX)
        assert control.try_acquire_animating("@1", tmp_path) is False
    assert (tmp_path / "@1.animating").read_text() == "99999999 running"


_COMPETITOR = """
import sys
from pathlib import Path
from vim_ai_follower import control
print(control.try_acquire_animating("@1", Path(sys.argv[1])), flush=True)
sys.stdin.read()  # stay alive, as a hook animating the slot it won does
"""


class _CompetitorAfterStep:
    """Stands in for control's `os`: right after the acquirer's `step`-th OS
    call returns (or raises), a competing hook — a real, separate, still-alive
    process — tries to take the same slot. Walking `step` over every call
    holds the acquirer between each pair of its steps in turn, whatever
    those steps are."""

    def __init__(self, step: int, base_dir: Path) -> None:
        self.step = step
        self.base_dir = base_dir
        self.calls = 0
        self.competitor: subprocess.Popen[str] | None = None
        self.competitor_won: bool | None = None

    def _compete(self) -> None:
        self.competitor = subprocess.Popen(
            [sys.executable, "-c", _COMPETITOR, str(self.base_dir)],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            text=True,
        )
        assert self.competitor.stdout is not None
        self.competitor_won = self.competitor.stdout.readline().strip() == "True"

    def __getattr__(self, name: str) -> Any:
        real = getattr(os, name)
        if not callable(real):
            return real

        def call(*args: Any, **kwargs: Any) -> Any:
            try:
                return real(*args, **kwargs)
            finally:
                self.calls += 1
                if self.calls == self.step:
                    self._compete()

        return call


@pytest.mark.parametrize("marker_before", ["none", "a crashed hook's"])
def test_a_competing_hook_at_any_step_of_an_acquire_never_makes_two_owners(
    tmp_path: Path, marker_before: str
) -> None:
    # Six parallel Writes fire six hooks at once. Whichever step one of them
    # is between when another looks at the marker, exactly one may own the
    # slot: a loser that read the winner's still-EMPTY marker used to call it
    # stale, unlink it and reclaim — two hooks typing into one pane.
    competed = 0
    for step in range(1, 50):
        base = tmp_path / str(step)
        base.mkdir()
        if marker_before != "none":
            (base / "@1.animating").write_text("99999999 running")  # dead pid
        fake_os = _CompetitorAfterStep(step, base)
        with patch("vim_ai_follower.control.os", fake_os):
            ours = control.try_acquire_animating("@1", base)
        if fake_os.competitor is None:
            break  # the acquire finished in fewer steps: every gap was probed
        competed += 1
        try:
            owners = [os.getpid()] * ours + [fake_os.competitor.pid] * bool(fake_os.competitor_won)
            assert len(owners) == 1, f"after step {step}: {len(owners)} owners of one slot"
            marker = control._read_marker("@1", base)
            assert marker is not None and marker[0] == owners[0], (
                f"after step {step}: the marker names {marker}, not the owner {owners[0]}"
            )
        finally:
            assert fake_os.competitor.stdin is not None
            fake_os.competitor.stdin.close()
            fake_os.competitor.wait()
    assert competed >= 2, "the competitor never ran: nothing was probed"


def test_a_state_change_replaces_the_marker_never_truncates_it(tmp_path: Path) -> None:
    # A hook going running -> paused -> handoff rewrites its marker while
    # other hooks read it to decide whether the slot is free. Rewritten in
    # place, a reader between the truncate and the write sees an EMPTY
    # marker, calls it stale, and takes the slot from a live owner. Replaced,
    # a reader holding the old file still reads the old, complete owner.
    assert control.try_acquire_animating("@1", tmp_path) is True
    with (tmp_path / "@1.animating").open() as before:
        control.mark_animating("@1", tmp_path, state="paused")
        assert before.read() == f"{os.getpid()} running"
    assert control.animating_state("@1", tmp_path) == "paused"
    assert sorted(p.name for p in tmp_path.iterdir()) == ["@1.animating"]


def test_a_signal_with_no_live_animation_to_address_is_not_written(tmp_path: Path) -> None:
    # P read "running", then the animation ended before the signal was written:
    # there is no one to pause, and the NEXT animation must not inherit it.
    control.request_pause("@1", base_dir=tmp_path)
    control.request_interrupt("@1", base_dir=tmp_path)
    control.mark_animating("@1", base_dir=tmp_path)  # the next animation starts
    assert control.check_signal("@1", base_dir=tmp_path) is None


def test_a_signal_addressed_to_a_dead_animation_is_dropped_not_obeyed(tmp_path: Path) -> None:
    earlier = subprocess.Popen(["sleep", "30"])
    try:
        (tmp_path / "@1.animating").write_text(f"{earlier.pid} running")
        control.request_pause("@1", base_dir=tmp_path)
    finally:
        earlier.kill()
        earlier.wait()
    assert (tmp_path / "@1.pause").read_text() == str(earlier.pid)  # addressed to it
    control.mark_animating("@1", base_dir=tmp_path)  # a new animation, this process
    assert control.check_signal("@1", base_dir=tmp_path) is None
    assert not (tmp_path / "@1.pause").exists()  # consumed as stale, not left behind


def test_a_signal_is_addressed_to_the_owner_the_key_press_saw(tmp_path: Path) -> None:
    control.mark_animating("@1", base_dir=tmp_path)
    control.request_interrupt("@1", base_dir=tmp_path)
    assert (tmp_path / "@1.interrupt").read_text() == str(os.getpid())
    assert not list(tmp_path.glob("*.tmp"))  # written atomically, no half file left
