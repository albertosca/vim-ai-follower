from __future__ import annotations

import hashlib
import shutil
import time
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


# How long an Edit's in-flight mark holds a Read of its file back. A post hook
# normally follows the file write within a second, but the mark is set BEFORE
# any permission prompt, so the pre->post gap includes the user deciding. An
# Edit that is denied or fails never runs a post hook and leaves its mark
# behind; expiring it bounds that, and while it lives it only silences a Read
# whose file changed on disk since the snapshot (see hooks._edit_in_flight),
# which a denied or failed Edit did not do. Five minutes covers a slow
# permission answer without leaving a stale mark around for a session.
IN_FLIGHT_TTL_SECONDS = 300


def in_flight_path(window_id: str, file_path: str, base_dir: Path | None = None) -> Path:
    """The mark an Edit's pre hook leaves beside its snapshot until its post
    hook runs. Its mtime is the moment the edit started."""
    return _snapshot_path(window_id, file_path, base_dir).with_suffix(".inflight")


def mark_in_flight(
    window_id: str, file_path: str, writer: str = "", base_dir: Path | None = None
) -> None:
    """Record the edit as started now, by `writer` (the hook payload's writer
    identity, "" when it carries none). Rewriting restarts the clock."""
    path = in_flight_path(window_id, file_path, base_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(writer)


def in_flight_writer(window_id: str, file_path: str, base_dir: Path | None = None) -> str:
    """The identity that set the mark, or "" when unknown or unreadable."""
    try:
        return in_flight_path(window_id, file_path, base_dir).read_text().strip()
    except (OSError, UnicodeDecodeError):
        return ""


def clear_in_flight(window_id: str, file_path: str, base_dir: Path | None = None) -> None:
    in_flight_path(window_id, file_path, base_dir).unlink(missing_ok=True)


def in_flight(window_id: str, file_path: str, base_dir: Path | None = None) -> bool:
    """True while an Edit of file_path has run its pre hook but not its post
    hook, and started less than IN_FLIGHT_TTL_SECONDS ago."""
    try:
        started = in_flight_path(window_id, file_path, base_dir).stat().st_mtime
    except OSError:
        return False
    return time.time() - started < IN_FLIGHT_TTL_SECONDS


def clear(window_id: str, base_dir: Path | None = None) -> None:
    """Delete every stored snapshot for the window (stop/orphan cleanup)."""
    directory = base_dir if base_dir is not None else SNAPSHOT_DIR
    shutil.rmtree(directory / window_id, ignore_errors=True)
