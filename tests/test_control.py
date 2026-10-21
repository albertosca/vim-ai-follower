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
    control.mark_animating("@1", base_directory=tmp_path)


def test_check_signal_returns_none_when_nothing_set(tmp_path: Path) -> None:
    assert control.check_signal("@1", base_directory=tmp_path) is None


@pytest.mark.usefixtures("animating")
def test_check_signal_returns_and_consumes_pause(tmp_path: Path) -> None:
    control.request_pause("@1", base_directory=tmp_path)
    assert control.check_signal("@1", base_directory=tmp_path) == "pause"
    assert control.check_signal("@1", base_directory=tmp_path) is None


@pytest.mark.usefixtures("animating")
def test_check_signal_returns_and_consumes_interrupt(tmp_path: Path) -> None:
    control.request_interrupt("@1", base_directory=tmp_path)
    assert control.check_signal("@1", base_directory=tmp_path) == "interrupt"
    assert control.check_signal("@1", base_directory=tmp_path) is None


@pytest.mark.usefixtures("animating")
def test_check_signal_interrupt_takes_priority_over_pause(tmp_path: Path) -> None:
    control.request_pause("@1", base_directory=tmp_path)
    control.request_interrupt("@1", base_directory=tmp_path)
    assert control.check_signal("@1", base_directory=tmp_path) == "interrupt"
    # pause is left alone, still pending
    assert control.check_signal("@1", base_directory=tmp_path) == "pause"


@pytest.mark.usefixtures("animating")
def test_clear_signals_removes_both_without_erroring_if_absent(tmp_path: Path) -> None:
    control.clear_signals("@1", base_directory=tmp_path)  # nothing to clear, no error
    control.request_pause("@1", base_directory=tmp_path)
    control.request_interrupt("@1", base_directory=tmp_path)
    control.clear_signals("@1", base_directory=tmp_path)
    assert control.check_signal("@1", base_directory=tmp_path) is None


def test_has_pending_animation_false_when_none_saved(tmp_path: Path) -> None:
    assert control.has_pending_animation("@1", base_directory=tmp_path) is False


def test_save_and_load_pending_apply_edit(tmp_path: Path) -> None:
    ops = [EditOp(kind="replace", start_line=2, end_line=2, new_lines=("X",))]
    control.save_pending_apply_edit("@1", ops, 0.05, base_directory=tmp_path)
    assert control.has_pending_animation("@1", base_directory=tmp_path) is True

    loaded = control.load_pending_animation("@1", base_directory=tmp_path)
    assert isinstance(loaded, control.PendingApplyEdit)
    assert loaded.ops == ops
    assert loaded.pace_seconds == 0.05
    # consumed on load
    assert control.has_pending_animation("@1", base_directory=tmp_path) is False


def test_save_and_load_pending_show_fresh(tmp_path: Path) -> None:
    control.save_pending_show_fresh("@1", ("a", "b"), 0.1, base_directory=tmp_path)
    loaded = control.load_pending_animation("@1", base_directory=tmp_path)
    assert isinstance(loaded, control.PendingShowFresh)
    assert loaded.lines == ("a", "b")
    assert loaded.pace_seconds == 0.1


def test_pending_show_fresh_round_trips_continuation(tmp_path: Path) -> None:
    control.save_pending_show_fresh("@1", ("x",), 0.05, continuation=True, base_directory=tmp_path)
    pending = control.load_pending_animation("@1", tmp_path)
    assert pending == control.PendingShowFresh(("x",), 0.05, continuation=True)


def test_pending_show_fresh_defaults_continuation_false(tmp_path: Path) -> None:
    control.save_pending_show_fresh("@1", ("x",), 0.05, base_directory=tmp_path)
    pending = control.load_pending_animation("@1", tmp_path)
    assert pending == control.PendingShowFresh(("x",), 0.05, continuation=False)


def test_pending_round_trips_file_path(tmp_path: Path) -> None:
    control.save_pending_show_fresh(
        "@1", ("a",), 0.03, base_directory=tmp_path, file_path="/tmp/f.py"
    )
    pending = control.load_pending_animation("@1", base_directory=tmp_path)
    assert pending is not None and pending.file_path == "/tmp/f.py"


def test_pending_defaults_file_path_for_old_payloads(tmp_path: Path) -> None:
    control.save_pending_apply_edit("@1", [], 0.03, base_directory=tmp_path)
    pending = control.load_pending_animation("@1", base_directory=tmp_path)
    assert pending is not None and pending.file_path == ""


def test_load_pending_animation_returns_none_when_absent(tmp_path: Path) -> None:
    assert control.load_pending_animation("@1", base_directory=tmp_path) is None


def test_discard_pending_animation_removes_file_without_erroring_if_absent(
    tmp_path: Path,
) -> None:
    control.discard_pending_animation("@1", base_directory=tmp_path)  # no error
    control.save_pending_apply_edit("@1", [], 0.0, base_directory=tmp_path)
    control.discard_pending_animation("@1", base_directory=tmp_path)
    assert control.has_pending_animation("@1", base_directory=tmp_path) is False


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
    operation = EditOp(kind="insert", start_line=1, end_line=0, new_lines=("x",))
    control.save_pending_apply_edit("@1", [operation], 0.1, tmp_path)
    leftovers = sorted(entry.name for entry in tmp_path.iterdir())
    assert leftovers == ["@1.pending_animation.json"]  # no .tmp residue
    # and the write goes through an atomic rename, not a direct write:
    with patch.object(Path, "replace", autospec=True, side_effect=Path.replace) as replace:
        control.save_pending_show_fresh("@1", ("y",), 0.1, base_directory=tmp_path)
    assert replace.called


def test_load_pending_returns_none_when_file_vanishes_mid_read(tmp_path: Path) -> None:
    control.save_pending_show_fresh("@1", ("y",), 0.1, base_directory=tmp_path)
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

    def __init__(self, base_directory: Path) -> None:
        self.proc = subprocess.Popen(
            [sys.executable, "-c", _COMPETITOR, str(base_directory)],
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
        assert self.proc.stdout is not None
        self.proc.stdout.close()


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
        def call(*arguments: Any, **keyword_arguments: Any) -> Any:
            try:
                return real(*arguments, **keyword_arguments)
            finally:
                self._step()

        return call

    def __getattr__(self, name: str) -> Any:
        real = getattr(os, name)
        return self._wrap(real) if callable(real) else real

    def acquire(self, base_directory: Path) -> bool:
        with (
            patch("vim_ai_follower.control.os", self),
            patch("vim_ai_follower.control.animating_state", self._wrap(self._animating_state)),
        ):
            return control.try_acquire_animating("@1", base_directory)


def _compete_into(base_directory: Path, found: list[_Competitor]) -> Callable[[], None]:
    def compete() -> None:
        found.append(_Competitor(base_directory))

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

    def stat(path: Any, *arguments: Any, **keyword_arguments: Any) -> Any:
        if Path(path) == marker:
            swap()
        return real(path, *arguments, **keyword_arguments)

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
    assert sorted(entry.name for entry in tmp_path.iterdir()) == ["@1.animating"]


def test_a_signal_with_no_live_animation_to_address_is_not_written(tmp_path: Path) -> None:
    # P read "running", then the animation ended before the signal was written:
    # there is no one to pause, and the NEXT animation must not inherit it.
    control.request_pause("@1", base_directory=tmp_path)
    control.request_interrupt("@1", base_directory=tmp_path)
    control.mark_animating("@1", base_directory=tmp_path)  # the next animation starts
    assert control.check_signal("@1", base_directory=tmp_path) is None


def test_a_signal_addressed_to_a_dead_animation_is_dropped_not_obeyed(tmp_path: Path) -> None:
    earlier = subprocess.Popen(["sleep", "30"])
    try:
        (tmp_path / "@1.animating").write_text(f"{earlier.pid} running")
        control.request_pause("@1", base_directory=tmp_path)
    finally:
        earlier.kill()
        earlier.wait()
    assert (tmp_path / "@1.pause").read_text() == f"{earlier.pid}\n"  # addressed to it
    control.mark_animating("@1", base_directory=tmp_path)  # a new animation, this process
    assert control.check_signal("@1", base_directory=tmp_path) is None
    assert not (tmp_path / "@1.pause").exists()  # consumed as stale, not left behind


def test_a_signal_is_addressed_to_the_owner_the_key_press_saw(tmp_path: Path) -> None:
    control.mark_animating("@1", base_directory=tmp_path)
    control.request_interrupt("@1", base_directory=tmp_path)
    assert (tmp_path / "@1.interrupt").read_text() == str(os.getpid())
    assert not list(tmp_path.glob("*.tmp"))  # written atomically, no half file left


def test_a_request_says_whether_it_reached_a_live_owner(tmp_path: Path) -> None:
    # The CLI reads "running" first and writes the signal after; an owner that
    # died in between gets nothing written, and the caller must know it.
    assert control.request_pause("@1", base_directory=tmp_path) == 0
    assert control.request_interrupt("@1", base_directory=tmp_path) is False
    control.mark_animating("@1", base_directory=tmp_path)
    assert control.request_pause("@1", base_directory=tmp_path) == 1
    assert control.request_interrupt("@1", base_directory=tmp_path) is True


def _dead_pid() -> int:
    finished = subprocess.Popen(["true"])
    finished.wait()
    return finished.pid


def test_clearing_signals_sweeps_what_killed_processes_left_behind(tmp_path: Path) -> None:
    # D1 residual: a process killed between its claim (rename to .claimed) and
    # the unlink, or between a request's temp write and its replace, leaves a
    # file nothing ever reads. clear_signals collects those of dead pids only.
    dead, live = _dead_pid(), os.getpid()
    names = [
        f"@1.{kind}.{pid}.{suffix}"
        for kind in ("pause", "interrupt")
        for pid in (dead, live)
        for suffix in ("claimed", "tmp")
    ]
    for name in names:
        (tmp_path / name).write_text("1")
    other_window = tmp_path / f"@10.pause.{dead}.claimed"
    other_window.write_text("1")
    unrelated_leftover = tmp_path / "@1.pause.x.tmp"
    unrelated_leftover.write_text("1")

    control.clear_signals("@1", base_directory=tmp_path)

    left = sorted(path.name for path in tmp_path.iterdir())
    assert left == sorted(
        [name for name in names if f".{live}." in name]
        + [other_window.name, unrelated_leftover.name]
    )


def test_sweeping_signal_leftovers_with_none_is_quiet(tmp_path: Path) -> None:
    control.sweep_signal_leftovers("@1", base_directory=tmp_path / "missing")
    control.sweep_signal_leftovers("@1", base_directory=tmp_path)
    assert list(tmp_path.iterdir()) == []


def test_the_leftover_sweep_reads_a_window_id_literally(tmp_path: Path) -> None:
    dead = _dead_pid()
    bracketed = tmp_path / f"term-[ab].pause.{dead}.claimed"
    lookalike = tmp_path / f"term-a.pause.{dead}.claimed"
    bracketed.write_text("1")
    lookalike.write_text("1")
    control.sweep_signal_leftovers("term-[ab]", base_directory=tmp_path)
    assert [path.name for path in tmp_path.iterdir()] == [lookalike.name]


@pytest.mark.usefixtures("animating")
def test_an_empty_signal_is_dropped_with_one_warning_about_version_skew(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    # A claude-follow from before PID-addressed signals (8ce0d92), still bound
    # to the keys by another installation, writes an empty file. It is never
    # obeyed (it names no owner), but the drop must not be silent.
    (tmp_path / "@1.pause").write_text("")
    with caplog.at_level("WARNING", logger="vim_ai_follower"):
        assert control.check_signal("@1", base_directory=tmp_path) is None
    warnings = [record for record in caplog.records if record.levelname == "WARNING"]
    assert len(warnings) == 1
    assert "empty pause signal" in warnings[0].getMessage()
    assert "older claude-follow" in warnings[0].getMessage()
    assert not (tmp_path / "@1.pause").exists()


@pytest.mark.usefixtures("animating")
def test_a_signal_for_another_owner_is_dropped_without_a_warning(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    (tmp_path / "@1.interrupt").write_text("1")
    with caplog.at_level("WARNING", logger="vim_ai_follower"):
        assert control.check_signal("@1", base_directory=tmp_path) is None
    assert caplog.records == []


@pytest.mark.parametrize(
    "content",
    [
        b"{not json",
        b"[]",
        b'{"kind": "apply_edit"}',
        b'{"kind": "show_fresh", "remaining_lines": 5, "pace_seconds": 0.1}',
        b'{"kind": "apply_edit", "remaining_ops": [{"kind": "insert"}], "pace_seconds": 0}',
        b"\xff\xfe\x00",
    ],
    ids=["garbage", "list", "missing-keys", "wrong-type", "op-missing-keys", "not-utf8"],
)
def test_a_corrupt_pending_file_is_dropped_once_with_a_log(
    content: bytes, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    # PF round-2 minor: it raised on every P and every hook until deleted.
    pending = tmp_path / "@1.pending_animation.json"
    pending.write_bytes(content)
    with caplog.at_level("WARNING", logger="vim_ai_follower"):
        assert control.load_pending_animation("@1", tmp_path) is None
        assert control.load_pending_animation("@1", tmp_path) is None
    assert not pending.exists()
    warnings = [record for record in caplog.records if record.levelname == "WARNING"]
    assert len(warnings) == 1
    assert "unreadable pending animation" in warnings[0].getMessage()


# --- withdrawable correction popups (sweep-5 T22, final m2) -----------------


def test_each_correction_token_is_new_and_exists(tmp_path: Path) -> None:
    first = control.new_correction_token("@1", tmp_path)
    second = control.new_correction_token("@1", tmp_path)
    assert first != second
    assert first.exists() and second.exists()
    assert first.name.startswith("@1.correction-")


def test_withdrawing_cancels_every_scheduled_correction_of_that_window(tmp_path: Path) -> None:
    mine = [control.new_correction_token("@1", tmp_path) for _ in range(2)]
    other = control.new_correction_token("@2", tmp_path)
    control.withdraw_corrections("@1", tmp_path)
    assert [token.exists() for token in mine] == [False, False]
    assert other.exists()


def test_withdrawing_a_showing_correction_waits_until_it_closed(tmp_path: Path) -> None:
    import threading
    import time

    token = control.new_correction_token("@1", tmp_path)
    showing = token.with_name(token.name + ".showing")
    closed = token.with_name(token.name + ".closed")
    token.rename(showing)  # the scheduled shell claimed it and opened the popup

    def popup() -> None:
        while showing.exists():
            time.sleep(0.01)
        time.sleep(0.1)
        closed.touch()

    thread = threading.Thread(target=popup)
    thread.start()
    started = time.monotonic()
    control.withdraw_corrections("@1", tmp_path)
    elapsed = time.monotonic() - started
    thread.join()
    assert elapsed >= 0.1
    assert not showing.exists() and not closed.exists()


def test_withdrawing_a_correction_that_already_closed_does_not_wait(tmp_path: Path) -> None:
    """Its popup closed on its own (`.closed` written) before the shell
    removed `.showing`: the scan must leave `.closed` for the wait, which
    then finds it at once instead of sitting out wait_seconds."""
    import time

    token = control.new_correction_token("@1", tmp_path)
    showing = token.with_name(token.name + ".showing")
    closed = token.with_name(token.name + ".closed")
    token.rename(showing)
    closed.touch()
    started = time.monotonic()
    control.withdraw_corrections("@1", tmp_path, wait_seconds=1.0)
    assert time.monotonic() - started < 0.5
    assert not showing.exists() and not closed.exists()


def test_withdrawing_gives_up_on_a_correction_that_never_closes(tmp_path: Path) -> None:
    import time

    token = control.new_correction_token("@1", tmp_path)
    token.rename(token.with_name(token.name + ".showing"))
    started = time.monotonic()
    control.withdraw_corrections("@1", tmp_path, wait_seconds=0.2)
    assert 0.2 <= time.monotonic() - started < 1.0


def test_a_new_token_sweeps_correction_leftovers_older_than_a_minute(tmp_path: Path) -> None:
    old = [tmp_path / f"@1.correction-9-aaaaaa{suffix}" for suffix in ("", ".showing", ".closed")]
    for path in old:
        path.touch()
        os.utime(path, (1, 1))
    fresh = tmp_path / "@1.correction-9-bbbbbb.closed"
    fresh.touch()
    control.new_correction_token("@1", tmp_path)
    assert [path.exists() for path in old] == [False, False, False]
    assert fresh.exists()


@pytest.mark.usefixtures("animating")
@pytest.mark.parametrize(("presses", "expected"), [(1, "pause"), (2, None), (3, "pause")])
def test_presses_not_yet_consumed_toggle(
    tmp_path: Path, presses: int, expected: str | None
) -> None:
    """T20 m4: P is a toggle, so two presses the driver had not read yet
    (during the uninterruptible catch-up rebuild) mean pause then resume. A
    second press used to overwrite the first, and the user ended paused."""
    counts = [control.request_pause("@1", base_directory=tmp_path) for _ in range(presses)]
    assert counts == list(range(1, presses + 1))
    assert control.check_signal("@1", base_directory=tmp_path) == expected
    assert not (tmp_path / "@1.pause").exists()


@pytest.mark.usefixtures("animating")
def test_only_the_owners_presses_are_counted(tmp_path: Path) -> None:
    # A stale press addressed to an earlier hook does not cancel this one's.
    (tmp_path / "@1.pause").write_text("1\n")
    assert control.request_pause("@1", base_directory=tmp_path) == 1
    assert control.check_signal("@1", base_directory=tmp_path) == "pause"


@pytest.mark.usefixtures("animating")
@pytest.mark.parametrize("left_at_the_path", ["nothing", "a newer file"])
def test_a_press_landing_while_the_file_is_claimed_is_not_lost(
    tmp_path: Path, left_at_the_path: str
) -> None:
    """A press that opened the file just before a consumer renamed it away
    (its claim) must not write into the claimed copy, which the consumer may
    already have read: it sees the path no longer names its file and writes
    to the one there now."""
    signal = tmp_path / "@1.pause"
    claimed = tmp_path / "@1.pause.claimed-by-test"
    control.request_pause("@1", base_directory=tmp_path)
    real_flock = fcntl.flock
    renamed: list[bool] = []

    def claim_before_the_lock(target: Any, operation: int) -> None:
        if isinstance(target, int) and not renamed:
            renamed.append(True)
            signal.rename(claimed)
            if left_at_the_path == "a newer file":
                signal.write_text("1\n")  # a stale press for another owner
        real_flock(target, operation)

    with patch.object(fcntl, "flock", claim_before_the_lock):
        assert control.request_pause("@1", base_directory=tmp_path) == 1
    assert claimed.read_text() == f"{os.getpid()}\n"  # the claimed copy untouched
    assert control.check_signal("@1", base_directory=tmp_path) == "pause"


def test_a_press_with_no_live_owner_counts_nothing(tmp_path: Path) -> None:
    assert control.request_pause("@1", base_directory=tmp_path) == 0
