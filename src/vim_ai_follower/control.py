from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Literal

from vim_ai_follower.diff import EditOp

CONTROL_DIR = Path.home() / ".cache" / "claude-vim-follower"


def _dir(base_dir: Path | None) -> Path:
    return base_dir if base_dir is not None else CONTROL_DIR


def _pause_path(session_id: str, base_dir: Path | None) -> Path:
    return _dir(base_dir) / f"{session_id}.pause"


def _interrupt_path(session_id: str, base_dir: Path | None) -> Path:
    return _dir(base_dir) / f"{session_id}.interrupt"


def _pending_path(session_id: str, base_dir: Path | None) -> Path:
    return _dir(base_dir) / f"{session_id}.pending_animation.json"


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
    session_id: str, base_dir: Path | None = None
) -> Literal["interrupt", "pause"] | None:
    if _consume(_interrupt_path(session_id, base_dir)):
        return "interrupt"
    if _consume(_pause_path(session_id, base_dir)):
        return "pause"
    return None


def clear_signals(session_id: str, base_dir: Path | None = None) -> None:
    _pause_path(session_id, base_dir).unlink(missing_ok=True)
    _interrupt_path(session_id, base_dir).unlink(missing_ok=True)


def request_pause(session_id: str, base_dir: Path | None = None) -> None:
    path = _pause_path(session_id, base_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.touch()


def request_interrupt(session_id: str, base_dir: Path | None = None) -> None:
    path = _interrupt_path(session_id, base_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.touch()


def has_pending_animation(session_id: str, base_dir: Path | None = None) -> bool:
    return _pending_path(session_id, base_dir).exists()


@dataclass(frozen=True)
class PendingApplyEdit:
    ops: list[EditOp]
    pace_seconds: float


@dataclass(frozen=True)
class PendingShowFresh:
    lines: tuple[str, ...]
    pace_seconds: float
    continuation: bool = False


def _write_pending(session_id: str, payload: dict[str, Any], base_dir: Path | None) -> None:
    path = _pending_path(session_id, base_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(payload))
    tmp.replace(path)  # atomic: a concurrent reader sees old-or-new, never half


def save_pending_apply_edit(
    session_id: str, ops: list[EditOp], pace_seconds: float, base_dir: Path | None = None
) -> None:
    _write_pending(
        session_id,
        {
            "kind": "apply_edit",
            "remaining_ops": [asdict(op) for op in ops],
            "pace_seconds": pace_seconds,
        },
        base_dir,
    )


def save_pending_show_fresh(
    session_id: str,
    lines: tuple[str, ...],
    pace_seconds: float,
    continuation: bool = False,
    base_dir: Path | None = None,
) -> None:
    _write_pending(
        session_id,
        {
            "kind": "show_fresh",
            "remaining_lines": list(lines),
            "pace_seconds": pace_seconds,
            "continuation": continuation,
        },
        base_dir,
    )


def load_pending_animation(
    session_id: str, base_dir: Path | None = None
) -> PendingApplyEdit | PendingShowFresh | None:
    path = _pending_path(session_id, base_dir)
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
        return PendingApplyEdit(ops=ops, pace_seconds=data["pace_seconds"])
    return PendingShowFresh(
        lines=tuple(data["remaining_lines"]),
        pace_seconds=data["pace_seconds"],
        continuation=data.get("continuation", False),
    )


def discard_pending_animation(session_id: str, base_dir: Path | None = None) -> None:
    _pending_path(session_id, base_dir).unlink(missing_ok=True)
