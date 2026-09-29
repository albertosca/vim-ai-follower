from __future__ import annotations

import fcntl
import os
import subprocess
import sys
from collections.abc import Callable
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


class _Competitor:
    """A competing hook: a real, separate process that tries to take the slot
    and then stays alive, as a hook that won would."""

    def __init__(self, base_dir: Path) -> None:
        self.proc = subprocess.Popen(
            [sys.executable, "-c", _COMPETITOR, str(base_dir)],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            text=True,
        )
        assert self.proc.stdout is not None
        self.won = self.proc.stdout.readline().strip() == "True"

    def close(self) -> None:
        assert self.proc.stdin is not None
        self.proc.stdin.close()
        self.proc.wait()


class _Steps:
    """Stands in for control's `os` and `animating_state`: right after the
    acquirer's n-th step (an OS call or a marker read) returns or raises, the
    event registered for n runs. Walking n over every step holds the acquirer
    between each pair of its steps in turn, whatever those steps are."""

    def __init__(self, events: dict[int, Callable[[], None]]) -> None:
        self.events = events
        self.calls = 0
        self.fired: set[int] = set()
        self._animating_state = control.animating_state

    def _step(self) -> None:
        self.calls += 1
        event = self.events.get(self.calls)
        if event is not None:
            self.fired.add(self.calls)
            event()

    def _wrap(self, real: Callable[..., Any]) -> Callable[..., Any]:
        def call(*args: Any, **kwargs: Any) -> Any:
            try:
                return real(*args, **kwargs)
            finally:
                self._step()

        return call

    def __getattr__(self, name: str) -> Any:
        real = getattr(os, name)
        return self._wrap(real) if callable(real) else real

    def acquire(self, base_dir: Path) -> bool:
        with (
            patch("vim_ai_follower.control.os", self),
            patch("vim_ai_follower.control.animating_state", self._wrap(self._animating_state)),
        ):
            return control.try_acquire_animating("@1", base_dir)


def _compete_into(base_dir: Path, found: list[_Competitor]) -> Callable[[], None]:
    def compete() -> None:
        found.append(_Competitor(base_dir))

    return compete


def _assert_one_owner_at_most(base: Path, ours: bool, competitor: _Competitor, where: str) -> None:
    owners = [os.getpid()] * ours + [competitor.proc.pid] * competitor.won
    assert len(owners) <= 1, f"{where}: {len(owners)} owners of one slot"
    if owners:
        marker = control._read_marker("@1", base)
        assert marker is not None and marker[0] == owners[0], (
            f"{where}: the marker names {marker}, not the owner {owners[0]}"
        )


@pytest.mark.parametrize("marker_before", ["none", "a crashed hook's"])
def test_a_competing_hook_at_any_step_of_an_acquire_never_makes_two_owners(
    tmp_path: Path, marker_before: str
) -> None:
    # Six parallel Writes fire six hooks at once. Whichever step one of them
    # is between when another looks at the marker, exactly one may own the
    # slot: a loser that read the winner's still-EMPTY marker used to call it
    # stale, unlink it and reclaim — two hooks typing into one pane.
    competed = 0
    for step in range(1, 60):
        base = tmp_path / str(step)
        base.mkdir()
        if marker_before != "none":
            (base / "@1.animating").write_text("99999999 running")  # dead pid
        competitors: list[_Competitor] = []
        steps = _Steps({step: _compete_into(base, competitors)})
        ours = steps.acquire(base)
        if not competitors:
            break  # the acquire finished in fewer steps: every gap was probed
        competed += 1
        try:
            _assert_one_owner_at_most(base, ours, competitors[0], f"after step {step}")
            assert ours or competitors[0].won, f"after step {step}: nobody owns a free slot"
        finally:
            competitors[0].close()
    assert competed >= 2, "the competitor never ran: nothing was probed"


def test_an_owner_releasing_mid_acquire_never_makes_two_owners(tmp_path: Path) -> None:
    # The acquirer's claim fails against a LIVE owner, which then finishes and
    # releases the slot while the acquirer is still deciding; a third hook
    # claims the free slot at some later step. The acquirer used to read the
    # now-absent marker as "stale", unlink the third hook's fresh one and
    # link its own — both returned True (review of b84a392, race_absent.py).
    owner = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(120)"])
    probed = 0
    try:
        for release_at in range(1, 60):
            released = False
            for compete_at in range(release_at + 1, 60):
                base = tmp_path / f"{release_at}-{compete_at}"
                base.mkdir()
                marker = base / "@1.animating"
                marker.write_text(f"{owner.pid} running")
                competitors: list[_Competitor] = []
                steps = _Steps(
                    {
                        release_at: marker.unlink,
                        compete_at: _compete_into(base, competitors),
                    }
                )
                ours = steps.acquire(base)
                released = release_at in steps.fired
                if not released or not competitors:
                    break
                probed += 1
                try:
                    _assert_one_owner_at_most(
                        base,
                        ours,
                        competitors[0],
                        f"release after step {release_at}, competitor after {compete_at}",
                    )
                finally:
                    competitors[0].close()
            if not released:
                break
    finally:
        owner.kill()
        owner.wait()
    assert probed >= 3, f"only {probed} interleavings probed"


def _swap_before_stat(marker: Path, swap: Callable[[], None]) -> Callable[..., Any]:
    """os.stat for control that first lets another process act on the marker:
    the moment between reading the dead marker and removing it."""
    real = os.stat

    def stat(path: Any, *args: Any, **kwargs: Any) -> Any:
        if Path(path) == marker:
            swap()
        return real(path, *args, **kwargs)

    return stat


def test_a_reclaim_never_removes_a_marker_other_than_the_dead_one_it_read(
    tmp_path: Path,
) -> None:
    marker = tmp_path / "@1.animating"
    marker.write_text("99999999 running")  # dead pid
    other = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    try:

        def replaced_by_a_live_owner() -> None:
            fresh = tmp_path / "fresh"
            fresh.write_text(f"{other.pid} running")
            fresh.replace(marker)  # a new file (inode) at the same name

        with patch(
            "vim_ai_follower.control.os.stat", _swap_before_stat(marker, replaced_by_a_live_owner)
        ):
            assert control.try_acquire_animating("@1", tmp_path) is False
        assert marker.read_text() == f"{other.pid} running"
    finally:
        other.kill()
        other.wait()


def test_a_reclaim_whose_dead_marker_vanished_claims_the_free_name(tmp_path: Path) -> None:
    marker = tmp_path / "@1.animating"
    marker.write_text("99999999 running")  # dead pid
    with patch("vim_ai_follower.control.os.stat", _swap_before_stat(marker, marker.unlink)):
        assert control.try_acquire_animating("@1", tmp_path) is True
    assert control.animating_state("@1", tmp_path) == "running"


def test_an_acquire_sweeps_temp_files_a_dead_writer_left(tmp_path: Path) -> None:
    dead = tmp_path / "@1.animating.99999999.tmp"
    live = tmp_path / f"@1.animating.{os.getppid()}.tmp"  # a live writer's
    dead.write_text("99999999 running")
    live.write_text(f"{os.getppid()} running")
    assert control.try_acquire_animating("@1", tmp_path) is True
    assert not dead.exists()
    assert live.exists()


def test_a_state_change_never_takes_a_live_owners_slot(tmp_path: Path) -> None:
    other = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    try:
        marker = tmp_path / "@1.animating"
        marker.write_text(f"{other.pid} running")
        assert control.mark_animating("@1", tmp_path, state="paused") is False
        assert marker.read_text() == f"{other.pid} running"
        control.clear_animating("@1", tmp_path)  # not ours to release either
        assert marker.read_text() == f"{other.pid} running"
    finally:
        other.kill()
        other.wait()


def test_a_state_change_claims_a_free_or_dead_slot(tmp_path: Path) -> None:
    assert control.mark_animating("@1", tmp_path) is True  # free
    control.clear_animating("@1", tmp_path)
    assert control.is_animating("@1", tmp_path) is False
    (tmp_path / "@1.animating").write_text("99999999 running")  # a crashed hook's
    assert control.mark_animating("@1", tmp_path, state="handoff") is True
    assert control.animating_state("@1", tmp_path) == "handoff"


def test_clearing_a_crashed_hooks_marker_removes_it(tmp_path: Path) -> None:
    (tmp_path / "@1.animating").write_text("99999999 running")
    control.clear_animating("@1", tmp_path)
    assert not (tmp_path / "@1.animating").exists()
    control.clear_animating("@1", tmp_path)  # nothing there: a no-op


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
