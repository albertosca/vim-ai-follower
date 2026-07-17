from __future__ import annotations

import hashlib
import shutil
from pathlib import Path

SNAPSHOT_DIR = Path.home() / ".cache" / "claude-vim-follower" / "snapshots"


def _snapshot_path(window_id: str, file_path: str, base_dir: Path | None) -> Path:
    directory = base_dir if base_dir is not None else SNAPSHOT_DIR
    digest = hashlib.sha256(file_path.encode("utf-8")).hexdigest()
    return directory / window_id / f"{digest}.before"


def save(window_id: str, file_path: str, content: str, base_dir: Path | None = None) -> None:
    path = _snapshot_path(window_id, file_path, base_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)


def load(window_id: str, file_path: str, base_dir: Path | None = None) -> str:
    path = _snapshot_path(window_id, file_path, base_dir)
    if not path.exists():
        return ""
    return path.read_text()


def clear(window_id: str, base_dir: Path | None = None) -> None:
    """Delete every stored snapshot for the window (stop/orphan cleanup)."""
    directory = base_dir if base_dir is not None else SNAPSHOT_DIR
    shutil.rmtree(directory / window_id, ignore_errors=True)
