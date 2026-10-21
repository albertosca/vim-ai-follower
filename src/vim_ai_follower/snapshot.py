from __future__ import annotations

import hashlib
import shutil
import time
from pathlib import Path

SNAPSHOT_DIRECTORY = Path.home() / ".cache" / "claude-vim-follower" / "snapshots"


def _snapshot_path(window_id: str, file_path: str, base_directory: Path | None) -> Path:
    directory = base_directory if base_directory is not None else SNAPSHOT_DIRECTORY
    digest = hashlib.sha256(file_path.encode("utf-8")).hexdigest()
    return directory / window_id / f"{digest}.before"


def save(window_id: str, file_path: str, content: str, base_directory: Path | None = None) -> None:
    path = _snapshot_path(window_id, file_path, base_directory)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content.encode("utf-8"))


def load(window_id: str, file_path: str, base_directory: Path | None = None) -> str:
    path = _snapshot_path(window_id, file_path, base_directory)
    if not path.exists():
        return ""
    # Bytes, not read_text(): its universal newlines would turn a CRLF
    # file's snapshot into LF, and it would never equal the file again.
    # Decoded like the pre hook reads the file: a snapshot the previous
    # write_text() left under a non-UTF-8 locale must not raise (T12+T13
    # review M2).
    return path.read_bytes().decode("utf-8", errors="replace")


def _stale_base_path(window_id: str, file_path: str, base_directory: Path | None) -> Path:
    return _snapshot_path(window_id, file_path, base_directory).with_suffix(".stalebase")


def save_stale_base(
    window_id: str, file_path: str, content: str, base_directory: Path | None = None
) -> None:
    """Record what the follower's buffer for file_path held when an edit of
    it was skipped (that edit's snapshot): the base the next edit may diff
    from, once a probe proves the buffer still holds it."""
    path = _stale_base_path(window_id, file_path, base_directory)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content.encode("utf-8"))


def load_stale_base(
    window_id: str, file_path: str, base_directory: Path | None = None
) -> str | None:
    """The recorded stale base, or None when there is none."""
    try:
        raw = _stale_base_path(window_id, file_path, base_directory).read_bytes()
    except OSError:
        return None
    return raw.decode("utf-8", errors="replace")


def clear_stale_base(window_id: str, file_path: str, base_directory: Path | None = None) -> None:
    _stale_base_path(window_id, file_path, base_directory).unlink(missing_ok=True)


def clear_stale_bases(window_id: str, base_directory: Path | None = None) -> None:
    """Delete every stale base of the window: the stale marks are gone with
    its open_files (a toggle's un-mute), so none would be read again."""
    directory = base_directory if base_directory is not None else SNAPSHOT_DIRECTORY
    for path in (directory / window_id).glob("*.stalebase"):
        path.unlink(missing_ok=True)


# How long an Edit's in-flight mark holds a Read of its file back. A post hook
# normally follows the file write within a second, but the mark is set BEFORE
# any permission prompt, so the pre->post gap includes the user deciding. An
# Edit that is denied or fails never runs a post hook and leaves its mark
# behind; expiring it bounds that, and while it lives it only silences a Read
# whose file changed on disk since the snapshot (see hooks._edit_in_flight),
# which a denied or failed Edit did not do. Five minutes covers a slow
# permission answer without leaving a stale mark around for a session.
IN_FLIGHT_TTL_SECONDS = 300


def in_flight_path(window_id: str, file_path: str, base_directory: Path | None = None) -> Path:
    """The mark an Edit's pre hook leaves beside its snapshot until its post
    hook runs. Its mtime is the moment the edit started."""
    return _snapshot_path(window_id, file_path, base_directory).with_suffix(".inflight")


def mark_in_flight(
    window_id: str, file_path: str, writer: str = "", base_directory: Path | None = None
) -> None:
    """Record the edit as started now, by `writer` (the hook payload's writer
    identity, "" when it carries none). Rewriting restarts the clock."""
    path = in_flight_path(window_id, file_path, base_directory)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(writer)


def in_flight_writer(window_id: str, file_path: str, base_directory: Path | None = None) -> str:
    """The identity that set the mark, or "" when unknown or unreadable."""
    try:
        return in_flight_path(window_id, file_path, base_directory).read_text().strip()
    except (OSError, UnicodeDecodeError):
        return ""


def clear_in_flight(window_id: str, file_path: str, base_directory: Path | None = None) -> None:
    in_flight_path(window_id, file_path, base_directory).unlink(missing_ok=True)


def in_flight(window_id: str, file_path: str, base_directory: Path | None = None) -> bool:
    """True while an Edit of file_path has run its pre hook but not its post
    hook, and started less than IN_FLIGHT_TTL_SECONDS ago."""
    try:
        started = in_flight_path(window_id, file_path, base_directory).stat().st_mtime
    except OSError:
        return False
    return time.time() - started < IN_FLIGHT_TTL_SECONDS


def clear(window_id: str, base_directory: Path | None = None) -> None:
    """Delete every stored snapshot for the window (stop/orphan cleanup)."""
    directory = base_directory if base_directory is not None else SNAPSHOT_DIRECTORY
    shutil.rmtree(directory / window_id, ignore_errors=True)
