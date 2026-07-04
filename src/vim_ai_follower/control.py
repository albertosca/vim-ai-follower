from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

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


def check_signal(
    session_id: str, base_dir: Path | None = None
) -> Literal["interrupt", "pause"] | None:
    interrupt_path = _interrupt_path(session_id, base_dir)
    if interrupt_path.exists():
        interrupt_path.unlink()
        return "interrupt"
    pause_path = _pause_path(session_id, base_dir)
    if pause_path.exists():
        pause_path.unlink()
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


def save_pending_apply_edit(
    session_id: str, ops: list[EditOp], pace_seconds: float, base_dir: Path | None = None
) -> None:
    path = _pending_path(session_id, base_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "kind": "apply_edit",
                "remaining_ops": [
                    {
                        "kind": op.kind,
                        "start_line": op.start_line,
                        "end_line": op.end_line,
                        "new_lines": list(op.new_lines),
                    }
                    for op in ops
                ],
                "pace_seconds": pace_seconds,
            }
        )
    )


def save_pending_show_fresh(
    session_id: str, lines: tuple[str, ...], pace_seconds: float, base_dir: Path | None = None
) -> None:
    path = _pending_path(session_id, base_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {"kind": "show_fresh", "remaining_lines": list(lines), "pace_seconds": pace_seconds}
        )
    )


def load_pending_animation(
    session_id: str, base_dir: Path | None = None
) -> PendingApplyEdit | PendingShowFresh | None:
    path = _pending_path(session_id, base_dir)
    if not path.exists():
        return None
    data = json.loads(path.read_text())
    path.unlink()
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
    return PendingShowFresh(lines=tuple(data["remaining_lines"]), pace_seconds=data["pace_seconds"])


def discard_pending_animation(session_id: str, base_dir: Path | None = None) -> None:
    _pending_path(session_id, base_dir).unlink(missing_ok=True)
