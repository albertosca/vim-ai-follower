"""On-disk signalling between the CLI keybindings and a running animation:
pause/interrupt signal files, the PID-tagged animating-state marker, and the
resumable pending-animation snapshot."""

from __future__ import annotations

import fcntl
import glob
import json
import logging
import os
import secrets
import time
from collections.abc import Iterator
from contextlib import contextmanager, suppress
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Literal

from vim_ai_follower import cache
from vim_ai_follower.diff import EditOp

logger = logging.getLogger("vim_ai_follower")


def _control_dir(base_directory: Path | None) -> Path:
    # cache.CACHE_DIRECTORY is read at call time (not bound at import) so one
    # monkeypatch of the cache module isolates every consumer in tests.
    return base_directory if base_directory is not None else cache.CACHE_DIRECTORY


def _pause_path(window_id: str, base_directory: Path | None) -> Path:
    return _control_dir(base_directory) / f"{window_id}.pause"


def _interrupt_path(window_id: str, base_directory: Path | None) -> Path:
    return _control_dir(base_directory) / f"{window_id}.interrupt"


def _pending_path(window_id: str, base_directory: Path | None) -> Path:
    return _control_dir(base_directory) / f"{window_id}.pending_animation.json"


def _consume(path: Path) -> bool:
    """Atomically claim a signal file, and True only when it was addressed to
    THIS process (for a pause file: an odd number of this process's presses,
    _consume_presses). The claim is a rename, not a read-then-unlink, so the file
    read is exactly the one claimed — a fresh signal written over a stale one
    in between can never be deleted unread. Losing the race to another
    consumer or to clear_signals means the signal is not ours to act on — and
    must never crash the animation. A signal addressed to anyone else is stale
    (see _request) and is consumed without being obeyed."""
    return _consume_presses(path) % 2 == 1


def _consume_presses(path: Path) -> int:
    """Claim a signal file and count the lines in it addressed to THIS
    process: one line per press (_append_press). Read under the file's lock,
    so a press that passed its check before the rename has been written."""
    claimed = path.with_name(f"{path.name}.{os.getpid()}.claimed")
    try:
        path.rename(claimed)
    except FileNotFoundError:
        return 0
    with claimed.open("r") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        addressees = [line.strip() for line in handle.read().splitlines()] or [""]
    claimed.unlink()
    if "" in addressees:
        # Every request since 8ce0d92 names its owner. An empty one comes from
        # an older claude-follow still bound to the keys (another installation
        # of a different version): dropped like any stale signal, but said.
        logger.warning(
            "dropped an empty %s signal: an older claude-follow (before PID-addressed "
            "signals) is likely bound to the keys — update or remove that installation",
            path.suffix.lstrip("."),
        )
    return addressees.count(str(os.getpid()))


def check_signal(
    window_id: str, base_directory: Path | None = None
) -> Literal["interrupt", "pause"] | None:
    if _consume(_interrupt_path(window_id, base_directory)):
        return "interrupt"
    if _consume(_pause_path(window_id, base_directory)):
        return "pause"
    return None


def clear_signals(window_id: str, base_directory: Path | None = None) -> None:
    _pause_path(window_id, base_directory).unlink(missing_ok=True)
    _interrupt_path(window_id, base_directory).unlink(missing_ok=True)
    sweep_signal_leftovers(window_id, base_directory)


def sweep_signal_leftovers(window_id: str, base_directory: Path | None = None) -> None:
    """Remove the `<signal>.<pid>.claimed` and `<signal>.<pid>.tmp` files a
    process left when it was killed between its claim (_consume's rename) and
    the unlink, or between a request's temp write and its replace. Nothing
    ever reads them; a live process's are never touched."""
    for signal_path in (
        _pause_path(window_id, base_directory),
        _interrupt_path(window_id, base_directory),
    ):
        prefix = f"{signal_path.name}."
        for suffix in (".claimed", ".tmp"):
            # Escaped: a window id is an identity string, never a pattern.
            pattern = f"{glob.escape(signal_path.name)}.*{suffix}"
            for leftover in signal_path.parent.glob(pattern):
                pid = leftover.name[len(prefix) : -len(suffix)]
                if pid.isdigit() and not _alive(int(pid)):
                    leftover.unlink(missing_ok=True)


def _request(path: Path, window_id: str, base_directory: Path | None) -> bool:
    """Address a signal to the animation live at this instant: the file holds
    its owner's PID, and only that process obeys it (_consume).

    Addressing is what tells a fresh signal from a stale one. The drivers used
    to clear every signal when they started, which wiped a P pressed in the
    gap between the hook claiming the slot (the "running" that P reads) and
    the driver's first keystroke — navigation, unlock and tab eviction happen
    in that gap, so "Paused" flashed and the edit typed through (BACKLOG D1,
    5 of 5 runs). A signal the previous animation never consumed carries that
    process's PID, so the next hook — another process — drops it on sight.
    With no live owner there is nothing to signal, so nothing is written.

    Signals sent to a hook reach every animation that one hook process runs
    (a catch-up, the edit, a des-interrupt replay): that is one "Writing..."
    the user saw, so a P aimed at it is still meant for it.

    True when the signal was written to a live owner. The caller read the
    marker's state before calling, and the owner can die in between: then
    nothing is written and False tells the caller not to claim it worked."""
    owner = _live_owner(window_id, base_directory)
    if owner is None:
        return False
    path.parent.mkdir(parents=True, exist_ok=True)
    staged = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    staged.write_text(str(owner))
    staged.replace(path)  # atomic: a consumer never claims an empty half-file
    return True


def request_pause(window_id: str, base_directory: Path | None = None) -> int:
    """Append one press for the live owner, and the number of its presses
    now waiting unread (0: no live owner, nothing written). P is a toggle:
    presses the driver had not read yet (a catch-up rebuild reads none) pair
    off, and only an odd count pauses (T20 m4: two used to collapse into one
    and the user ended paused)."""
    owner = _live_owner(window_id, base_directory)
    if owner is None:
        return 0
    return _append_press(_pause_path(window_id, base_directory), owner)


def _append_press(path: Path, owner: int) -> int:
    """One `O_APPEND` line under the file's lock. The file is the one at
    `path` when the line is written: a consumer that renamed it away first
    (it claims by rename, then reads under the lock) leaves this open handle
    on its claimed copy, and the press goes to a new file instead. The
    count is read back through the same descriptor: the claim's rename takes
    no lock, so the path may already be gone after the write (final review
    of backlog-sweep-5, M2), and the file written into is the one whose
    presses the consumer reads."""
    path.parent.mkdir(parents=True, exist_ok=True)
    while True:
        descriptor = os.open(path, os.O_RDWR | os.O_APPEND | os.O_CREAT, 0o600)
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX)
            try:
                current = path.stat()
            except FileNotFoundError:
                continue
            if current.st_ino != os.fstat(descriptor).st_ino:
                continue
            os.write(descriptor, f"{owner}\n".encode())
            size = os.fstat(descriptor).st_size
            content = os.pread(descriptor, size, 0).decode(errors="replace")
            return content.split().count(str(owner))
        finally:
            os.close(descriptor)


def request_interrupt(window_id: str, base_directory: Path | None = None) -> bool:
    return _request(_interrupt_path(window_id, base_directory), window_id, base_directory)


_CORRECTION_LEFTOVER_SECONDS = 60.0
_CORRECTION_CLOSE_POLL_SECONDS = 0.02


def _correction_files(window_id: str, base_directory: Path | None) -> list[Path]:
    # Escaped: a window id is an identity string, never a pattern.
    return list(_control_dir(base_directory).glob(f"{glob.escape(window_id)}.correction-*"))


def new_correction_token(window_id: str, base_directory: Path | None = None) -> Path:
    """A new token for a correction popup scheduled for later (tmux.show_popup
    `withdraw_token`): the popup opens only while the token exists. Files a
    killed popup shell left, older than a minute, go first: a popup lasts
    1.5 s."""
    directory = _control_dir(base_directory)
    directory.mkdir(parents=True, exist_ok=True)
    cut_off = time.time() - _CORRECTION_LEFTOVER_SECONDS
    for leftover in _correction_files(window_id, base_directory):
        with suppress(FileNotFoundError):
            if leftover.stat().st_mtime < cut_off:
                leftover.unlink()
    token = directory / f"{window_id}.correction-{os.getpid()}-{secrets.token_hex(3)}"
    token.touch()
    return token


def withdraw_corrections(
    window_id: str, base_directory: Path | None = None, wait_seconds: float = 1.0
) -> None:
    """Before a P or S press shows its own popup: a correction a drain
    scheduled for this window must not cover it (tmux drops a popup opened
    while another is up). One not open yet is cancelled (its token removed);
    one open is told to close (its `.showing` removed) and waited for, at
    most `wait_seconds`, until its shell says it closed."""
    waiting: list[Path] = []
    for path in _correction_files(window_id, base_directory):
        if path.name.endswith(".closed"):
            continue
        with suppress(FileNotFoundError):
            path.unlink()
            if path.name.endswith(".showing"):
                waiting.append(path.with_name(path.name.removesuffix(".showing") + ".closed"))
    deadline = time.monotonic() + wait_seconds
    for closed in waiting:
        while not closed.exists() and time.monotonic() < deadline:
            time.sleep(_CORRECTION_CLOSE_POLL_SECONDS)
        closed.unlink(missing_ok=True)


def has_pending_animation(window_id: str, base_directory: Path | None = None) -> bool:
    """Non-consuming peek at whether a resumable animation is on disk.
    Production consumes it via load_pending_animation instead; this predicate
    is the suite's observation point for the crash-fallback file."""
    return _pending_path(window_id, base_directory).exists()


def _animating_path(window_id: str, base_directory: Path | None) -> Path:
    return _control_dir(base_directory) / f"{window_id}.animating"


def _write_marker_atomically(path: Path, payload: str) -> Path:
    """The marker's content, written to a private sibling first so the marker
    itself is only ever published whole (by link or replace). A reader that
    sees the marker at all sees its owner: an EMPTY marker is indistinguishable
    from a crashed hook's, and a live one read as stale gets taken."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    temporary_path.write_text(payload)
    return temporary_path


def mark_animating(
    window_id: str, base_directory: Path | None = None, state: str = "running"
) -> bool:
    """Record this process's animation state: in flight ("running"), waiting
    while paused ("paused"), or waiting for the user's save after an
    interrupt ("handoff") — tagged with this process's PID so a crashed hook
    can never leave a convincing stale marker.

    Only a state change: the slot's LIFETIME belongs to whoever acquired it
    (the hook, or the CLI's keyboard resume), which releases it on its own way
    out — one hook runs several drivers (a catch-up, the edit, a hand-off
    replay), and a driver releasing on exit left the slot empty between them
    for a second hook to take. So a marker owned by another LIVE process is
    never overwritten (False); a free or dead slot is claimed as an acquire
    would. Replaced, never rewritten in place: hooks racing for the slot read
    it the whole time (see _write_marker_atomically)."""
    path = _animating_path(window_id, base_directory)
    temporary_path = _write_marker_atomically(path, f"{os.getpid()} {state}")
    try:
        seen = _marker_seen(path)
        if seen is not None and seen[1] == os.getpid():
            # Ours: nobody else replaces or removes a live owner's marker.
            temporary_path.replace(path)
            return True
        if seen is not None and seen[1] is not None and _alive(seen[1]):
            return False  # another live process owns this window's slot
        return _publish(temporary_path, path) or _reclaim(temporary_path, path)
    finally:
        temporary_path.unlink(missing_ok=True)


def try_acquire_animating(
    window_id: str, base_directory: Path | None = None, state: str = "running"
) -> bool:
    """Atomically claim this window's animation slot for the current process.
    Returns True when this process now owns it, False when a LIVE process
    already holds it.

    This replaces a check-then-act on animating_state at the hook guard: six
    parallel Write tool calls fire six hooks near-simultaneously, and a plain
    "read the marker, then later write it" lets them all pass the guard and
    animate into the same pane at once — their unlock/wipe/opener keystrokes
    interleave into garble (scripts/repro-concurrent-hooks.sh).

    The claim is a hard link of an already-written file onto the marker name:
    it fails if the name exists, and when it succeeds the marker appears with
    its owner in it. (An O_EXCL create followed by a separate write published
    an empty marker in between, which a loser read as a crashed hook's and
    reclaimed — two owners.) A crash before the link leaves only the private
    temp file, so no half-claim can wedge the slot; the next acquire sweeps
    such leftovers."""
    path = _animating_path(window_id, base_directory)
    path.parent.mkdir(parents=True, exist_ok=True)
    _sweep_dead_temp_files(path)
    temporary_path = _write_marker_atomically(path, f"{os.getpid()} {state}")
    try:
        if _publish(temporary_path, path):
            return True
        if animating_state(window_id, base_directory) is not None:
            return False  # a live process is already animating this window
        return _reclaim(temporary_path, path)
    finally:
        temporary_path.unlink(missing_ok=True)


def _sweep_dead_temp_files(path: Path) -> None:
    """Remove `<marker>.<pid>.tmp` files whose writer died between writing
    and cleaning them up. A live writer's is never touched."""
    for leftover in path.parent.glob(f"{path.name}.*.tmp"):
        pid = leftover.name[len(path.name) + 1 : -len(".tmp")]
        if pid.isdigit() and not _alive(int(pid)):
            leftover.unlink(missing_ok=True)


def _publish(temporary_path: Path, path: Path) -> bool:
    try:
        os.link(temporary_path, path)
    except FileExistsError:
        return False
    return True


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:  # pragma: no cover - not ours, but it exists
        pass
    return True


def _marker_seen(path: Path) -> tuple[tuple[int, int], int | None] | None:
    """The marker file's identity (device, inode) and owner PID as read from
    that very file — None for the PID when the content names no one — or
    None when there is no marker at all."""
    try:
        file_descriptor = os.open(path, os.O_RDONLY)
    except FileNotFoundError:
        return None
    try:
        stat = os.fstat(file_descriptor)
        words = os.read(file_descriptor, 256).decode(errors="replace").split()
    finally:
        os.close(file_descriptor)
    pid = int(words[0]) if words and words[0].isdigit() else None
    return (stat.st_dev, stat.st_ino), pid


@contextmanager
def _reclaim_lock(path: Path, *, wait: bool) -> Iterator[bool]:
    """The lock every removal of someone else's (dead) marker happens under.
    Yields False when not waiting and another process holds it. The kernel
    drops it with its holder, so a crash under it cannot wedge the slot."""
    # The .lock file is never deleted, by design: removing a flock file lets a
    # second process lock a new inode while the first still holds the old one.
    file_descriptor = os.open(path.with_name(f"{path.name}.lock"), os.O_CREAT | os.O_WRONLY, 0o644)
    try:
        try:
            fcntl.flock(file_descriptor, fcntl.LOCK_EX if wait else fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            yield False
            return
        yield True
    finally:
        os.close(file_descriptor)


def _remove_dead_marker(path: Path) -> bool | None:
    """Under the reclaim lock: remove the marker if its owner is dead. None
    when there is no marker, False when it belongs to a live process or was
    replaced since it was read, True when removed.

    "No marker" and "a dead marker" are different answers: unlinking by name
    after reading "stale" would take whatever fresh marker replaced it, so a
    dead one is removed ONLY while the file at the name is still the one read
    (device and inode). os.stat/os.unlink, not Path's: the same `os` every
    other step goes through, so a test can stand between any two of them."""
    seen = _marker_seen(path)
    if seen is None:
        return None
    identity, pid = seen
    if pid is not None and _alive(pid):
        return False
    try:
        current = os.stat(path)  # noqa: PTH116
    except FileNotFoundError:
        return None
    if (current.st_dev, current.st_ino) != identity:
        return False
    os.unlink(path)  # noqa: PTH108
    return True


def _reclaim(temporary_path: Path, path: Path) -> bool:
    """Take the slot from a crashed hook (dead PID), or lose. Reclaimers
    serialize on the lock; one already reclaiming means this hook lost the
    race, and it skips without waiting. A slot found free under the lock is
    only tried with the same link any other hook would use."""
    with _reclaim_lock(path, wait=False) as locked:
        if not locked:
            return False  # another hook is reclaiming this very marker
        if _remove_dead_marker(path) is False:
            return False  # a live owner's (reclaimed since the first read)
        return _publish(temporary_path, path)


def clear_animating(window_id: str, base_directory: Path | None = None) -> None:
    """Release this process's slot. Another live process's marker is never
    removed — it releases its own; a crashed hook's is removed (under the
    reclaim lock, identity-checked)."""
    path = _animating_path(window_id, base_directory)
    seen = _marker_seen(path)
    if seen is None:
        return
    pid = seen[1]
    if pid == os.getpid():
        path.unlink(missing_ok=True)  # ours: nobody else removes a live owner's
        return
    if pid is not None and _alive(pid):
        return
    with _reclaim_lock(path, wait=True):
        _remove_dead_marker(path)


def _read_marker(window_id: str, base_directory: Path | None) -> tuple[int, str] | None:
    """The live owner's PID and state, or None when no live process owns one."""
    try:
        content = _animating_path(window_id, base_directory).read_text().split()
        pid = int(content[0])
    except (FileNotFoundError, ValueError, IndexError):
        return None
    if not _alive(pid):
        return None  # stale marker from a crashed animation
    return pid, content[1] if len(content) > 1 else "running"


def _live_owner(window_id: str, base_directory: Path | None) -> int | None:
    marker = _read_marker(window_id, base_directory)
    return None if marker is None else marker[0]


def animating_state(window_id: str, base_directory: Path | None = None) -> str | None:
    """The live animation's state, or None when no live process owns one."""
    marker = _read_marker(window_id, base_directory)
    return None if marker is None else marker[1]


def is_animating(window_id: str, base_directory: Path | None = None) -> bool:
    """Boolean predicate form of animating_state, for callers that only need
    "is a live process animating?" without the running/paused/handoff detail.
    Production reads animating_state directly; this is the suite's observation
    point (parallel to has_pending_animation)."""
    return animating_state(window_id, base_directory) is not None


@dataclass(frozen=True)
class PendingApplyEdit:
    """Resumable remainder of a diff animation. file_path records which tab
    the resume must re-select first (empty only for legacy/unknown state).
    partial is the already-applied prefix — see PendingShowFresh."""

    ops: list[EditOp]
    pace_seconds: float
    file_path: str = ""
    partial: str | None = None


@dataclass(frozen=True)
class PendingShowFresh:
    """Resumable remainder of a fresh-file retype. continuation marks that
    earlier lines already landed (so resume opens lines instead of typing
    into the wiped buffer's first line); file_path is the tab to re-select.

    partial is the content the buffer held when this remainder was persisted
    — the counterpart the crash-fallback catch-up rebuilds from instead of
    trusting the live buffer's shape (which, since an interrupt stopped
    snapping the current line, routinely ends on a HALF-TYPED line the row
    math would then push down instead of replace). None means "not recorded":
    a pending file written before this field existed, or one saved by a call
    site that cannot compute it (the tmux animation drivers, which never see
    the content already on screen). A consumer reading None must fall back to
    the live buffer — degraded, but exactly the behavior that preceded it."""

    lines: tuple[str, ...]
    pace_seconds: float
    continuation: bool = False
    file_path: str = ""
    partial: str | None = None


def _write_pending(window_id: str, payload: dict[str, Any], base_directory: Path | None) -> None:
    path = _pending_path(window_id, base_directory)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path = path.with_name(path.name + ".tmp")
    temporary_path.write_text(json.dumps(payload))
    temporary_path.replace(path)  # atomic: a concurrent reader sees old-or-new, never half


def save_pending_apply_edit(
    window_id: str,
    ops: list[EditOp],
    pace_seconds: float,
    base_directory: Path | None = None,
    file_path: str = "",
    partial: str | None = None,
) -> None:
    _write_pending(
        window_id,
        {
            "kind": "apply_edit",
            "remaining_ops": [asdict(operation) for operation in ops],
            "pace_seconds": pace_seconds,
            "file_path": file_path,
            "partial": partial,
        },
        base_directory,
    )


def save_pending_show_fresh(
    window_id: str,
    lines: tuple[str, ...],
    pace_seconds: float,
    continuation: bool = False,
    base_directory: Path | None = None,
    file_path: str = "",
    partial: str | None = None,
) -> None:
    _write_pending(
        window_id,
        {
            "kind": "show_fresh",
            "remaining_lines": list(lines),
            "pace_seconds": pace_seconds,
            "continuation": continuation,
            "file_path": file_path,
            "partial": partial,
        },
        base_directory,
    )


def load_pending_animation(
    window_id: str, base_directory: Path | None = None
) -> PendingApplyEdit | PendingShowFresh | None:
    path = _pending_path(window_id, base_directory)
    try:
        # A ValueError here is a file that is not UTF-8 or not JSON.
        data = json.loads(path.read_text())
    except FileNotFoundError:
        return None
    except ValueError as error:
        _drop_unreadable_pending(path, error)
        return None
    path.unlink(missing_ok=True)
    try:
        return _parse_pending(data)
    except (ValueError, KeyError, TypeError, AttributeError) as error:
        _drop_unreadable_pending(path, error)
        return None


def _drop_unreadable_pending(path: Path, error: Exception) -> None:
    """The writes are atomic, so this is a file damaged or written by
    something else. Raising on it failed every P and every hook until the file
    was deleted by hand: it is removed and logged once instead."""
    path.unlink(missing_ok=True)
    logger.warning("dropped an unreadable pending animation (%s): %r", path.name, error)


def _parse_pending(data: Any) -> PendingApplyEdit | PendingShowFresh:
    if data["kind"] == "apply_edit":
        ops = [
            EditOp(
                kind=operation["kind"],
                start_line=operation["start_line"],
                end_line=operation["end_line"],
                new_lines=tuple(operation["new_lines"]),
            )
            for operation in data["remaining_ops"]
        ]
        return PendingApplyEdit(
            ops=ops,
            pace_seconds=data["pace_seconds"],
            file_path=data.get("file_path", ""),
            partial=data.get("partial"),
        )
    return PendingShowFresh(
        lines=tuple(data["remaining_lines"]),
        pace_seconds=data["pace_seconds"],
        continuation=data.get("continuation", False),
        file_path=data.get("file_path", ""),
        partial=data.get("partial"),
    )


def discard_pending_animation(window_id: str, base_directory: Path | None = None) -> None:
    _pending_path(window_id, base_directory).unlink(missing_ok=True)
