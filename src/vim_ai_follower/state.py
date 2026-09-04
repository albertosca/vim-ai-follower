"""Per-tmux-window follower state (registered pane, tracked tabs, speed and
flags) persisted as JSON, plus the tab recency/eviction helper."""

from __future__ import annotations

import dataclasses
import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from vim_ai_follower import cache
from vim_ai_follower.backends import get_follower

_DEFAULT_ORIGIN = ""
_DEFAULT_ON_FAILURE = "silent"
_DEFAULT_SPEED = "rapido"


def _state_path(window_id: str, base_dir: Path | None) -> Path:
    directory = base_dir if base_dir is not None else cache.CACHE_DIR
    return directory / f"{window_id}.pane"


def nvim_socket_path(window_id: str, base_dir: Path | None = None) -> Path:
    """Deterministic per-window RPC socket path. The Neovim side must call
    vim.fn.serverstart() at this exact path (see integração no README).
    Colons in window_id (present in iTerm's TERM_SESSION_ID, shaped like
    "term-w0t0p0:UUID") are replaced with underscores: nvim's --listen
    parses ANY address containing a colon as a TCP host:port via
    getaddrinfo, so a literal colon here makes nvim reject the path with
    "unknown node or service" instead of treating it as a unix socket
    (live finding 2026-09-03, confirmed in isolation 2026-09-04)."""
    directory = base_dir if base_dir is not None else cache.CACHE_DIR
    safe_window_id = window_id.replace(":", "_")
    return directory / f"nvim-{safe_window_id}.sock"


@dataclass(frozen=True)
class FollowerState:
    # current_file is display-only: it is reported by `status` and reset to
    # None after an interrupt to force a resync, but no navigation decision
    # reads it. Which tab to show is driven by open_files membership plus the
    # follower's own name-based goto_file preamble, never by this field.
    backend: str
    target: str
    current_file: str | None
    origin: str
    on_failure: str
    speed: str
    open_files: tuple[str, ...] = ()
    enabled: bool = True
    adopted: bool = False
    shown_any: bool = False
    writers: tuple[str, ...] = ()
    writer_labels: tuple[str, ...] = ()

    @classmethod
    def read(cls, window_id: str, base_dir: Path | None = None) -> FollowerState | None:
        """Returns the persisted state without checking whether the
        follower is still alive — used by recovery logic that needs the
        origin pane even when the current one has died."""
        path = _state_path(window_id, base_dir)
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
            open_files=tuple(data.get("open_files", [])),
            enabled=data.get("enabled", True),
            adopted=data.get("adopted", False),
            shown_any=data.get("shown_any", False),
            writers=tuple(data.get("writers", [])),
            writer_labels=tuple(data.get("writer_labels", [])),
        )

    @classmethod
    def get(cls, window_id: str, base_dir: Path | None = None) -> FollowerState | None:
        state = cls.read(window_id, base_dir)
        if state is None:
            return None
        if not get_follower(state.backend, state.target).is_alive():
            return None
        return state

    @classmethod
    def set(
        cls,
        window_id: str,
        backend: str,
        target: str,
        current_file: str | None = None,
        origin: str = _DEFAULT_ORIGIN,
        on_failure: str = _DEFAULT_ON_FAILURE,
        speed: str = _DEFAULT_SPEED,
        open_files: tuple[str, ...] = (),
        enabled: bool = True,
        adopted: bool = False,
        shown_any: bool = False,
        writers: tuple[str, ...] = (),
        writer_labels: tuple[str, ...] = (),
        base_dir: Path | None = None,
    ) -> None:
        path = _state_path(window_id, base_dir)
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = json.dumps(
            {
                "backend": backend,
                "target": target,
                "current_file": current_file,
                "origin": origin,
                "on_failure": on_failure,
                "speed": speed,
                "open_files": open_files,
                "enabled": enabled,
                "adopted": adopted,
                "shown_any": shown_any,
                "writers": writers,
                "writer_labels": writer_labels,
            }
        )
        # Atomic write: a follower per window means several hooks can now
        # write and read .pane files concurrently. Writing to a
        # PID-private temp file and renaming it onto the target means a
        # reader always sees the whole old or whole new state, never a
        # half-written file, and two racing writers never share a temp to
        # corrupt. The temp name never ends in ".pane", so a crash leaves
        # nothing the *.pane orphan scan would mistake for state.
        tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
        tmp.write_text(payload)
        tmp.replace(path)

    @classmethod
    def update(cls, window_id: str, base_dir: Path | None = None, **changes: Any) -> None:
        """Persist a partial change on top of the stored state, alive or not
        (a toggle must be able to re-enable a follower whose pane died)."""
        current = cls.read(window_id, base_dir)
        if current is None:
            return
        updated = dataclasses.replace(current, **changes)
        cls.set(
            window_id,
            updated.backend,
            updated.target,
            current_file=updated.current_file,
            origin=updated.origin,
            on_failure=updated.on_failure,
            speed=updated.speed,
            open_files=updated.open_files,
            enabled=updated.enabled,
            adopted=updated.adopted,
            shown_any=updated.shown_any,
            writers=updated.writers,
            writer_labels=updated.writer_labels,
            base_dir=base_dir,
        )

    @classmethod
    def update_current_file(
        cls, window_id: str, file_path: str | None, base_dir: Path | None = None
    ) -> None:
        """Record (display-only, see the current_file field) the file now
        shown, or None to clear the pointer after a handoff. Freshness is
        keyed on open_files, not this field, so clearing it does not force
        a full retype on the next edit — that edit is still a diff
        (apply_edit) against the pre-edit snapshot.
        Gated on get() — a dead follower's file pointer is not worth keeping."""
        current = cls.get(window_id, base_dir)
        if current is None:
            return
        cls.update(window_id, base_dir=base_dir, current_file=file_path)

    @classmethod
    def clear(cls, window_id: str, base_dir: Path | None = None) -> None:
        _state_path(window_id, base_dir).unlink(missing_ok=True)


def touch_open_files(
    open_files: tuple[str, ...], file_path: str, max_tabs: int
) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """Recency bump: file_path becomes most recent; anything past max_tabs
    falls off the old end. The touched file is by construction never in the
    evicted slice, which is what keeps eviction away from the animating or
    handed-over file."""
    files = (*(f for f in open_files if f != file_path), file_path)
    return files[-max_tabs:], files[:-max_tabs]
