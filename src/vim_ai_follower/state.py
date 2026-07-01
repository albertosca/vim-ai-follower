from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from vim_ai_follower.backends import get_follower

STATE_DIR = Path.home() / ".cache" / "claude-vim-follower"

_DEFAULT_ORIGIN = ""
_DEFAULT_ON_FAILURE = "silent"
_DEFAULT_SPEED = "rapido"


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
    origin: str
    on_failure: str
    speed: str

    @classmethod
    def read(cls, session_id: str, base_dir: Path | None = None) -> FollowerState | None:
        """Returns the persisted state without checking whether the
        follower is still alive — used by recovery logic that needs the
        origin pane even when the current one has died."""
        path = _state_path(session_id, base_dir)
        if not path.exists():
            return None
        data = json.loads(path.read_text())
        return cls(
            backend=data["backend"],
            target=data["target"],
            current_file=data.get("current_file"),
            origin=data.get("origin", _DEFAULT_ORIGIN),
            on_failure=data.get("on_failure", _DEFAULT_ON_FAILURE),
            speed=data.get("speed", _DEFAULT_SPEED),
        )

    @classmethod
    def get(cls, session_id: str, base_dir: Path | None = None) -> FollowerState | None:
        state = cls.read(session_id, base_dir)
        if state is None:
            return None
        if not get_follower(state.backend, state.target).is_alive():
            return None
        return state

    @classmethod
    def set(
        cls,
        session_id: str,
        backend: str,
        target: str,
        current_file: str | None = None,
        origin: str = _DEFAULT_ORIGIN,
        on_failure: str = _DEFAULT_ON_FAILURE,
        speed: str = _DEFAULT_SPEED,
        base_dir: Path | None = None,
    ) -> None:
        path = _state_path(session_id, base_dir)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(
                {
                    "backend": backend,
                    "target": target,
                    "current_file": current_file,
                    "origin": origin,
                    "on_failure": on_failure,
                    "speed": speed,
                }
            )
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
            origin=current.origin,
            on_failure=current.on_failure,
            speed=current.speed,
            base_dir=base_dir,
        )

    @classmethod
    def clear(cls, session_id: str, base_dir: Path | None = None) -> None:
        _state_path(session_id, base_dir).unlink(missing_ok=True)
