"""On-disk signalling between the CLI keybindings and a running animation:
pause/interrupt signal files, the PID-tagged animating-state marker, and the
resumable pending-animation snapshot."""

from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Literal

from vim_ai_follower import cache
from vim_ai_follower.diff import EditOp


def _control_dir(base_dir: Path | None) -> Path:
    # cache.CACHE_DIR is read at call time (not bound at import) so one
    # monkeypatch of the cache module isolates every consumer in tests.
    return base_dir if base_dir is not None else cache.CACHE_DIR


def _pause_path(window_id: str, base_dir: Path | None) -> Path:
    return _control_dir(base_dir) / f"{window_id}.pause"


def _interrupt_path(window_id: str, base_dir: Path | None) -> Path:
    return _control_dir(base_dir) / f"{window_id}.interrupt"


def _pending_path(window_id: str, base_dir: Path | None) -> Path:
    return _control_dir(base_dir) / f"{window_id}.pending_animation.json"


def _consume(path: Path) -> bool:
    """Atomically claim a signal file: True only if THIS process removed
    it. Losing the race to clear_signals (or another consumer) means the
    signal is not ours to act on — and must never crash the animation."""
    try:
        path.unlink()
    except FileNotFoundError:
        return False
    return True


def check_signal(
    window_id: str, base_dir: Path | None = None
) -> Literal["interrupt", "pause"] | None:
    if _consume(_interrupt_path(window_id, base_dir)):
        return "interrupt"
    if _consume(_pause_path(window_id, base_dir)):
        return "pause"
    return None


def clear_signals(window_id: str, base_dir: Path | None = None) -> None:
    _pause_path(window_id, base_dir).unlink(missing_ok=True)
    _interrupt_path(window_id, base_dir).unlink(missing_ok=True)


def request_pause(window_id: str, base_dir: Path | None = None) -> None:
    path = _pause_path(window_id, base_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.touch()


def request_interrupt(window_id: str, base_dir: Path | None = None) -> None:
    path = _interrupt_path(window_id, base_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.touch()


def has_pending_animation(window_id: str, base_dir: Path | None = None) -> bool:
    """Non-consuming peek at whether a resumable animation is on disk.
    Production consumes it via load_pending_animation instead; this predicate
    is the suite's observation point for the crash-fallback file."""
    return _pending_path(window_id, base_dir).exists()


def _animating_path(window_id: str, base_dir: Path | None) -> Path:
    return _control_dir(base_dir) / f"{window_id}.animating"


def mark_animating(window_id: str, base_dir: Path | None = None, state: str = "running") -> None:
    """Record that an animation is in flight ("running"), waiting while
    paused ("paused"), or waiting for the user's save after an interrupt
    ("handoff") — tagged with this process's PID so a crashed hook can
    never leave a convincing stale marker."""
    path = _animating_path(window_id, base_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"{os.getpid()} {state}")


def try_acquire_animating(
    window_id: str, base_dir: Path | None = None, state: str = "running"
) -> bool:
    """Atomically claim this window's animation slot for the current process.
    Returns True when this process now owns it, False when a LIVE process
    already holds it.

    This replaces a check-then-act on animating_state at the hook guard: six
    parallel Write tool calls fire six hooks near-simultaneously, and a plain
    "read the marker, then later write it" lets them all pass the guard and
    animate into the same pane at once — their unlock/wipe/opener keystrokes
    interleave into garble (scripts/repro-concurrent-hooks.sh). The O_EXCL
    create is the atomic winner-takes-it so exactly one hook animates and the
    rest skip. A marker left by a crashed hook (dead PID) is reclaimed."""
    path = _animating_path(window_id, base_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = f"{os.getpid()} {state}"
    try:
        fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
    except FileExistsError:
        if animating_state(window_id, base_dir) is not None:
            return False  # a live process is already animating this window
        # Stale marker from a crashed hook: reclaim it atomically. If another
        # hook reclaims first, its O_EXCL wins and ours raises again — skip.
        try:
            path.unlink()
            fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
        except (FileExistsError, FileNotFoundError):
            return False
    with os.fdopen(fd, "w") as handle:
        handle.write(payload)
    return True


def clear_animating(window_id: str, base_dir: Path | None = None) -> None:
    _animating_path(window_id, base_dir).unlink(missing_ok=True)


def animating_state(window_id: str, base_dir: Path | None = None) -> str | None:
    """The live animation's state, or None when no live process owns one."""
    try:
        content = _animating_path(window_id, base_dir).read_text().split()
        pid = int(content[0])
    except (FileNotFoundError, ValueError, IndexError):
        return None
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return None  # stale marker from a crashed animation
    except PermissionError:  # pragma: no cover - not ours, but it exists
        pass
    return content[1] if len(content) > 1 else "running"


def is_animating(window_id: str, base_dir: Path | None = None) -> bool:
    """Boolean predicate form of animating_state, for callers that only need
    "is a live process animating?" without the running/paused/handoff detail.
    Production reads animating_state directly; this is the suite's observation
    point (parallel to has_pending_animation)."""
    return animating_state(window_id, base_dir) is not None


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


def _write_pending(window_id: str, payload: dict[str, Any], base_dir: Path | None) -> None:
    path = _pending_path(window_id, base_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(payload))
    tmp.replace(path)  # atomic: a concurrent reader sees old-or-new, never half


def save_pending_apply_edit(
    window_id: str,
    ops: list[EditOp],
    pace_seconds: float,
    base_dir: Path | None = None,
    file_path: str = "",
    partial: str | None = None,
) -> None:
    _write_pending(
        window_id,
        {
            "kind": "apply_edit",
            "remaining_ops": [asdict(op) for op in ops],
            "pace_seconds": pace_seconds,
            "file_path": file_path,
            "partial": partial,
        },
        base_dir,
    )


def save_pending_show_fresh(
    window_id: str,
    lines: tuple[str, ...],
    pace_seconds: float,
    continuation: bool = False,
    base_dir: Path | None = None,
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
        base_dir,
    )


def load_pending_animation(
    window_id: str, base_dir: Path | None = None
) -> PendingApplyEdit | PendingShowFresh | None:
    path = _pending_path(window_id, base_dir)
    try:
        data = json.loads(path.read_text())
    except FileNotFoundError:
        return None
    path.unlink(missing_ok=True)
    if data["kind"] == "apply_edit":
        ops = [
            EditOp(
                kind=op["kind"],
                start_line=op["start_line"],
                end_line=op["end_line"],
                new_lines=tuple(op["new_lines"]),
            )
            for op in data["remaining_ops"]
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


def discard_pending_animation(window_id: str, base_dir: Path | None = None) -> None:
    _pending_path(window_id, base_dir).unlink(missing_ok=True)
