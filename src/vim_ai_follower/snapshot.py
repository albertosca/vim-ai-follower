from __future__ import annotations

import hashlib
from pathlib import Path

SNAPSHOT_DIR = Path.home() / ".cache" / "claude-vim-follower" / "snapshots"


def _snapshot_path(session_id: str, file_path: str, base_dir: Path | None) -> Path:
    directory = base_dir if base_dir is not None else SNAPSHOT_DIR
    digest = hashlib.sha256(file_path.encode("utf-8")).hexdigest()
    return directory / session_id / f"{digest}.before"


def save(session_id: str, file_path: str, content: str, base_dir: Path | None = None) -> None:
    path = _snapshot_path(session_id, file_path, base_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)


def load(session_id: str, file_path: str, base_dir: Path | None = None) -> str:
    path = _snapshot_path(session_id, file_path, base_dir)
    if not path.exists():
        return ""
    return path.read_text()
