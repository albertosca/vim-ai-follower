from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from vim_ai_follower.backends import get_follower

STATE_DIR = Path.home() / ".cache" / "claude-vim-follower"


def _state_path(session_id: str, base_dir: Path | None) -> Path:
    directory = base_dir if base_dir is not None else STATE_DIR
    return directory / f"{session_id}.pane"


def nvim_socket_path(session_id: str, base_dir: Path | None = None) -> Path:
    """Deterministic per-tmux-session RPC socket path. The Neovim side must
    call vim.fn.serverstart() at this exact path (see integração no README)."""
    directory = base_dir if base_dir is not None else STATE_DIR
    return directory / f"nvim-{session_id}.sock"


@dataclass(frozen=True)
class FollowerState:
    backend: str
    target: str
    current_file: str | None

    @classmethod
    def get(cls, session_id: str, base_dir: Path | None = None) -> FollowerState | None:
        path = _state_path(session_id, base_dir)
        if not path.exists():
            return None
        data = json.loads(path.read_text())
        backend = data["backend"]
        target = data["target"]
        if not get_follower(backend, target).is_alive():
            return None
        return cls(backend=backend, target=target, current_file=data.get("current_file"))

    @classmethod
    def set(
        cls,
        session_id: str,
        backend: str,
        target: str,
        current_file: str | None = None,
        base_dir: Path | None = None,
    ) -> None:
        path = _state_path(session_id, base_dir)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps({"backend": backend, "target": target, "current_file": current_file})
        )

    @classmethod
    def update_current_file(
        cls, session_id: str, file_path: str, base_dir: Path | None = None
    ) -> None:
        current = cls.get(session_id, base_dir)
        if current is None:
            return
        cls.set(
            session_id,
            current.backend,
            current.target,
            current_file=file_path,
            base_dir=base_dir,
        )

    @classmethod
    def clear(cls, session_id: str, base_dir: Path | None = None) -> None:
        _state_path(session_id, base_dir).unlink(missing_ok=True)
