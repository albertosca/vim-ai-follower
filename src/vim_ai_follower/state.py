from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from vim_ai_follower.tmux import TmuxPane

STATE_DIR = Path.home() / ".cache" / "claude-vim-follower"


def _state_path(session_id: str, base_dir: Path | None) -> Path:
    directory = base_dir if base_dir is not None else STATE_DIR
    return directory / f"{session_id}.pane"


@dataclass(frozen=True)
class FollowerState:
    pane: TmuxPane
    current_file: str | None

    @classmethod
    def get(cls, session_id: str, base_dir: Path | None = None) -> FollowerState | None:
        path = _state_path(session_id, base_dir)
        if not path.exists():
            return None
        data = json.loads(path.read_text())
        pane = TmuxPane(pane_id=data["pane_id"])
        if not pane.exists():
            return None
        return cls(pane=pane, current_file=data.get("current_file"))

    @classmethod
    def set(
        cls,
        session_id: str,
        pane: TmuxPane,
        current_file: str | None = None,
        base_dir: Path | None = None,
    ) -> None:
        path = _state_path(session_id, base_dir)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"pane_id": pane.pane_id, "current_file": current_file}))

    @classmethod
    def update_current_file(
        cls, session_id: str, file_path: str, base_dir: Path | None = None
    ) -> None:
        current = cls.get(session_id, base_dir)
        if current is None:
            return
        cls.set(session_id, current.pane, current_file=file_path, base_dir=base_dir)

    @classmethod
    def clear(cls, session_id: str, base_dir: Path | None = None) -> None:
        _state_path(session_id, base_dir).unlink(missing_ok=True)
