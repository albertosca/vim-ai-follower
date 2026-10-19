"""Per-tmux-window follower state (registered pane, tracked tabs, speed and
flags) persisted as JSON, plus the tab recency/eviction helper."""

from __future__ import annotations

import contextlib
import dataclasses
import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from vim_ai_follower import cache
from vim_ai_follower.backends import get_follower

_DEFAULT_ORIGIN = ""
_DEFAULT_ON_FAILURE = "silent"
_DEFAULT_SPEED = "rapido"


def _pid_or_none(value: object) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _state_path(window_id: str, base_dir: Path | None) -> Path:
    directory = base_dir if base_dir is not None else cache.CACHE_DIR
    return directory / f"{window_id}.pane"


def stopped_path(window_id: str, base_dir: Path | None = None) -> Path:
    """The tombstone `claude-follow stop` leaves for a window: it holds the
    tmux server pid the stop ran under ('' standalone). While it holds, no
    state is written for the window and auto-open does not reopen it, until
    `start` or toggle clear it. Never named "*.pane": the orphan scan in
    commands._other_live_follower collects those."""
    directory = base_dir if base_dir is not None else cache.CACHE_DIR
    return directory / f"{window_id}.stopped"


_UNKNOWN_SERVER = "unknown"


def mark_stopped(
    window_id: str,
    server_pid: int | None,
    in_tmux: bool,
    base_dir: Path | None = None,
    server_started: int | None = None,
) -> None:
    """Write the tombstone: the tmux server pid the stop ran under, followed
    by that server's start time when it is known ("<pid> <start>"), '' for a
    standalone window, or "unknown" for a stop inside tmux that could not
    learn its server's pid (see is_stopped for what each one holds)."""
    path = stopped_path(window_id, base_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    if not in_tmux:
        path.write_text("")
    elif server_pid is None:
        path.write_text(_UNKNOWN_SERVER)
    elif server_started is None:
        path.write_text(str(server_pid))
    else:
        path.write_text(f"{server_pid} {server_started}")


def _recorded_server(recorded: str) -> tuple[str, int | None]:
    """A tombstone's server pid (as written) and start time (None when it
    has none, or one that does not parse)."""
    pid, _space, started = recorded.partition(" ")
    try:
        return pid, int(started)
    except ValueError:
        return pid, None


def is_stopped(
    window_id: str,
    server_pid: int | None,
    base_dir: Path | None = None,
    server_started: int | None = None,
) -> bool:
    """True while the window's tombstone holds. A tombstone written under a
    tmux server pid holds only under that same server: window ids are reused
    across tmux restarts, so @4 today is not the @4 a stop under the previous
    server silenced. A caller that cannot learn its server's pid (None) is
    held by it: the stop is the user's word, and an unproven "another
    server" must not undo it. One written with no pid (standalone, where
    the window id is the terminal's own) holds whatever the server. One
    written inside tmux with the pid unknown holds only while the pid stays
    unknown: it cannot tell this server from a later one, and holding under
    every later server would silence a window id that tmux has reused.

    A pid match is not enough when both start times are known (the stop's,
    recorded with its pid, and server_started): a server that started
    later is a new one that got the old pid, which a reboot makes common
    (final review M10). Either one unknown leaves the pid to decide."""
    try:
        recorded = stopped_path(window_id, base_dir).read_text().strip()
    except OSError:
        return False
    if recorded == "":
        return True
    if server_pid is None:
        return True
    pid, started = _recorded_server(recorded)
    if pid != str(server_pid):
        return False
    return started is None or server_started is None or started == server_started


def clear_stopped(window_id: str, base_dir: Path | None = None) -> None:
    stopped_path(window_id, base_dir).unlink(missing_ok=True)
    stopped_note_path(window_id, base_dir).unlink(missing_ok=True)


def stopping_path(window_id: str, base_dir: Path | None = None) -> Path:
    """A copy of the state a `stop` is tearing down, kept until its teardown
    is over (commands._state_to_tear_down). Never named "*.pane"."""
    directory = base_dir if base_dir is not None else cache.CACHE_DIR
    return directory / f"{window_id}.stopping"


def keep_stopping_record(window_id: str, base_dir: Path | None = None) -> None:
    """Copy the window's state to its stopping_path; best-effort."""
    with contextlib.suppress(OSError):
        stopping_path(window_id, base_dir).write_bytes(
            _state_path(window_id, base_dir).read_bytes()
        )


def stopped_note_path(window_id: str, base_dir: Path | None = None) -> Path:
    """Marks that auto-open already said, once, why it is not reopening this
    stopped window; it goes with the tombstone."""
    directory = base_dir if base_dir is not None else cache.CACHE_DIR
    return directory / f"{window_id}.stopped-noted"


def _stopped_here(window_id: str, base_dir: Path | None) -> bool:
    """is_stopped for the CURRENT tmux server, asking tmux only when a
    tombstone exists, so an ordinary write costs one stat."""
    if not stopped_path(window_id, base_dir).exists():
        return False
    from vim_ai_follower import binding  # late: binding imports this module

    return stopped_under(window_id, current_server_pid(), binding.tmux_server_started(), base_dir)


def stopped_under(
    window_id: str,
    server_pid: int | None,
    server_started: int | None,
    base_dir: Path | None = None,
) -> bool:
    """is_stopped, sweeping a tombstone that is PROVEN to be an earlier
    server's: this server has its pid but started later, or its pid is
    gone. Nothing else ever removed one (final review M10), and its
    `.stopped-noted` then kept auto-open from saying why it stayed off. A
    tombstone of another LIVE server is kept: the cache is shared, and that
    server's window may have the same id; one naming no server is kept too."""
    if is_stopped(window_id, server_pid, base_dir, server_started):
        return True
    try:
        recorded = stopped_path(window_id, base_dir).read_text().strip()
    except OSError:
        return False
    pid_text, _started = _recorded_server(recorded)
    if not pid_text.isdigit():
        return False  # '' or "unknown": no server to prove gone
    if pid_text == str(server_pid):
        # Not held under the same pid: both start times known, and different.
        clear_stopped(window_id, base_dir)
        return False
    try:
        os.kill(int(pid_text), 0)
    except ProcessLookupError:
        clear_stopped(window_id, base_dir)
    except OSError:
        pass  # EPERM: alive under another user; anything else proves nothing
    return False


# What reading a state file can raise: a missing or unreadable file, bad
# JSON, a missing key, or JSON of the wrong shape (`null`, a list: TypeError
# or AttributeError on the field reads). One list for every reader that must
# treat such a file as "proves nothing" instead of crashing.
UNREADABLE_STATE_ERRORS = (OSError, ValueError, KeyError, TypeError, AttributeError)


def follower_stopped(
    window_id: str, follower: tuple[str, str] | None, base_dir: Path | None = None
) -> bool:
    """Whether `claude-follow stop` ended the follower `follower` (its
    (backend, target), None when the caller does not know it) while a hook
    waited on it: in a pause or a hand-off. True when the window's tombstone
    holds under this tmux server, or when the state names another follower (a
    stop followed by a start, which lifts the tombstone).

    A MISSING state alone is not a stop (final review I4): another window's
    `stop` sweeps a `.pane` whose editor looks dead, which a dedicated Vim
    running `:!pytest` does, and the wait then released Claude saying the
    user stopped the follower. A real stop writes its tombstone before it
    clears the state, so a stop is never seen as a missing state alone. An
    unreadable state proves nothing (a write caught mid-way, or garbage):
    only the tombstone decides then."""
    try:
        now = FollowerState.read(window_id, base_dir)
    except UNREADABLE_STATE_ERRORS:
        now = None
    if now is not None and follower is not None and (now.backend, now.target) != follower:
        return True
    return _stopped_here(window_id, base_dir)


def current_server_pid() -> int | None:
    """The attached tmux server's pid (binding.tmux_server_pid), imported
    late: binding imports this module."""
    from vim_ai_follower import binding

    return binding.tmux_server_pid()


def _records_a_server(backend: str, origin: str) -> bool:
    """Whether a state lives in a tmux server: every tmux-backend state, and
    an nvim one started from a tmux pane (it has an origin). A standalone
    nvim's state has neither and no server to record."""
    return backend == "tmux" or bool(origin)


def from_another_server(state: FollowerState) -> bool:
    """True when the state names the tmux server it was written under and
    the current server is a different one: after a tmux restart window and
    pane ids start again from @0/%0, so the state's pane is somebody else's
    (Claude's own pane, the user's shell or Vim). False when either pid is
    unknown, which proves nothing either way; `stop`, the one caller that
    must not act without proof, asks proven_here instead."""
    if state.server_pid is None:
        return False
    current = current_server_pid()
    return current is not None and current != state.server_pid


def proven_here(state: FollowerState) -> bool:
    """True only when the state was written under the CURRENT tmux server:
    its recorded pid equals the one measured now. Within one server tmux
    never reuses a pane id, so that is what proves the state's pane is
    still the one the follower was started in. A state with no recorded
    pid (written before the pid was recorded) or a server that does not
    answer proves nothing."""
    return state.server_pid is not None and state.server_pid == current_server_pid()


def unproven_here(state: FollowerState) -> bool:
    """True when the state lives in a tmux server (a tmux backend, or an
    origin pane) and proven_here cannot vouch for it. What every path that
    SPLITS or ZOOMS from a stored state asks (the reopen, toggle; final
    review M12): from_another_server reads a pre-upgrade state with no pid
    as "same server", and after a tmux restart its origin is somebody else's
    pane. A standalone nvim records no server and needs no proof."""
    return _records_a_server(state.backend, state.origin) and not proven_here(state)


def nvim_socket_path(window_id: str, base_dir: Path | None = None) -> Path:
    """Deterministic per-window RPC socket path. The Neovim side must call
    vim.fn.serverstart() at this exact path (the README documents no such integration).
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
    # open_files answers "which tabs exist" — it is the eviction candidate
    # list. stale_files answers the separate question "which of those tabs
    # holds a buffer that no longer matches disk", so a skipped edit can say
    # "resync this one" without also saying "this tab is gone". Conflating
    # the two is what stranded tabs outside eviction forever (see mark_stale).
    # Invariant, enforced in set(): stale_files is a subset of open_files.
    open_files: tuple[str, ...] = ()
    stale_files: tuple[str, ...] = ()
    enabled: bool = True
    adopted: bool = False
    shown_any: bool = False
    writers: tuple[str, ...] = ()
    writer_labels: tuple[str, ...] = ()
    # The pid of the tmux server the follower was started under (None for a
    # standalone nvim, and for a state written before it was recorded). See
    # from_another_server / proven_here.
    server_pid: int | None = None

    @classmethod
    def read(cls, window_id: str, base_dir: Path | None = None) -> FollowerState | None:
        """Returns the persisted state without checking whether the
        follower is still alive — used by recovery logic that needs the
        origin pane even when the current one has died."""
        return cls.read_path(_state_path(window_id, base_dir))

    @classmethod
    def read_path(cls, path: Path) -> FollowerState | None:
        """read() of a state file at an explicit path (a stopping record)."""
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
            # A .pane file written before stale tracking existed has no
            # such key: default it, exactly like the other late fields.
            stale_files=tuple(data.get("stale_files", [])),
            enabled=data.get("enabled", True),
            # Missing means unknown, and unknown must not lock or press keys:
            # every reader of `adopted` takes the user's-editor side on True.
            adopted=data.get("adopted", True),
            shown_any=data.get("shown_any", False),
            writers=tuple(data.get("writers", [])),
            writer_labels=tuple(data.get("writer_labels", [])),
            server_pid=_pid_or_none(data.get("server_pid")),
        )

    @classmethod
    def get(cls, window_id: str, base_dir: Path | None = None) -> FollowerState | None:
        state = cls.read(window_id, base_dir)
        if state is None:
            return None
        if from_another_server(state):
            # A state left by a previous tmux server: its pane id now names
            # some other pane of this one, which may well be running vim.
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
        stale_files: tuple[str, ...] = (),
        enabled: bool = True,
        adopted: bool = False,
        shown_any: bool = False,
        writers: tuple[str, ...] = (),
        writer_labels: tuple[str, ...] = (),
        base_dir: Path | None = None,
        server_pid: int | Literal["measure"] | None = "measure",
    ) -> bool:
        """Write the state, and say whether it was written (False under a
        `stop` tombstone). server_pid "measure", the default every caller
        that starts or adopts a follower relies on, records the current
        tmux server's pid for a state that lives in tmux; update() passes
        the stored one through, so a later write never re-proves a state
        that was not written under this server."""
        if _stopped_here(window_id, base_dir):
            # `claude-follow stop` ran for this window after this writer read
            # the state: a hook finishing its animation, or an auto-open
            # racing the stop. Writing now would resurrect the follower the
            # user just stopped, so the write is dropped (`start`/toggle
            # clear the tombstone before they write).
            return False
        if server_pid == "measure":
            server_pid = current_server_pid() if _records_a_server(backend, origin) else None
        path = _state_path(window_id, base_dir)
        path.parent.mkdir(parents=True, exist_ok=True)
        # Enforce stale_files is a subset of open_files here rather than at
        # each caller, because not every caller knows the field exists:
        # cmd_toggle's un-mute resets open_files to () to force a resync and
        # would otherwise leave stale entries naming tabs nothing tracks any
        # more -- the same dangling shape this pairing exists to prevent.
        # Eviction relies on it too: touch_open_files drops the victim from
        # open_files and this drops its stale mark in the same write.
        stale_files = tuple(stale for stale in stale_files if stale in open_files)
        payload = json.dumps(
            {
                "backend": backend,
                "target": target,
                "current_file": current_file,
                "origin": origin,
                "on_failure": on_failure,
                "speed": speed,
                "open_files": open_files,
                "stale_files": stale_files,
                "enabled": enabled,
                "adopted": adopted,
                "shown_any": shown_any,
                "writers": writers,
                "writer_labels": writer_labels,
                "server_pid": server_pid,
            }
        )
        # Atomic write: a follower per window means several hooks can now
        # write and read .pane files concurrently. Writing to a
        # PID-private temp file and renaming it onto the target means a
        # reader always sees the whole old or whole new state, never a
        # half-written file, and two racing writers never share a temp to
        # corrupt. The temp name never ends in ".pane", so a crash leaves
        # nothing the *.pane orphan scan would mistake for state.
        temporary_path = path.with_name(f"{path.name}.{os.getpid()}.tmp")
        temporary_path.write_text(payload)
        temporary_path.replace(path)
        if _stopped_here(window_id, base_dir):
            # The check above and the replace are not one step: a whole
            # `stop` can run between them (mark_stopped, then a clear that
            # finds nothing yet), and this write then lands under its
            # tombstone (final review M9). A stop writes its tombstone before
            # it clears, so either its clear comes after this write, or this
            # second look sees the tombstone and takes the write back: only
            # while the file is still this write, since a `start` may have
            # lifted the tombstone and written its own state since.
            with contextlib.suppress(OSError):
                if path.read_text() == payload:
                    path.unlink()
            return False
        return True

    @classmethod
    def update(cls, window_id: str, base_dir: Path | None = None, **changes: Any) -> bool:
        """Persist a partial change on top of the stored state, alive or not
        (a toggle must be able to re-enable a follower whose pane died).
        False when nothing was written: no state, or `set` refused the write
        (a `stop` of this window), so a command can say so (final review M11)."""
        current = cls.read(window_id, base_dir)
        if current is None:
            return False
        updated = dataclasses.replace(current, **changes)
        return cls.set(
            window_id,
            updated.backend,
            updated.target,
            current_file=updated.current_file,
            origin=updated.origin,
            on_failure=updated.on_failure,
            speed=updated.speed,
            open_files=updated.open_files,
            stale_files=updated.stale_files,
            enabled=updated.enabled,
            adopted=updated.adopted,
            shown_any=updated.shown_any,
            writers=updated.writers,
            writer_labels=updated.writer_labels,
            base_dir=base_dir,
            server_pid=updated.server_pid,
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
    def mark_stale(cls, window_id: str, file_path: str, base_dir: Path | None = None) -> None:
        """Record that the follower's tab for file_path holds a buffer that
        disk has moved on from, so the next touch must retype it whole
        instead of animating a diff onto a base that was never applied.

        Deliberately NOT expressed by dropping the file from open_files,
        which is what the skipped-edit path used to do: that tuple doubles
        as the eviction candidate list, so the drop told eviction "this tab
        is gone" while Vim still held it, and the tab could never be named
        as a victim again. Measured live on 2026-09-22 — nine tabs in Vim
        against max_tabs=5 and exactly five open_files, climbing by one per
        collision in a window with three concurrent writers.

        A no-op for a file with no tab (nothing to be out of sync; the
        stale-subset-of-open invariant would drop the entry anyway) and for
        a window with no state at all — the .animating marker and the .pane
        file have decoupled lifecycles, so a stop can delete the state while
        a still-live animator's marker survives (cmd_stop never touches its
        own marker). Deduped: repeated collisions on one file are one mark.
        """
        current = cls.read(window_id, base_dir)
        if current is None or file_path not in current.open_files:
            return
        cls.update(
            window_id,
            base_dir=base_dir,
            stale_files=tuple(dict.fromkeys((*current.stale_files, file_path))),
        )

    @classmethod
    def clear_stale(cls, window_id: str, file_path: str, base_dir: Path | None = None) -> None:
        """Drop file_path's stale mark: an animation for it just finished, so
        its buffer now holds the whole file and matches what the next diff
        will be computed against.

        Only completion paths call this. An INTERRUPTED animation must leave
        the mark — the buffer really does hold a half-typed prefix — and so
        must navigation. On both backends a Read re-reads a CLEAN open
        buffer (tmux s:reload, nvim `_reload_if_clean`), which can
        resync a stale one — but the mark is kept anyway, so the next Edit
        still retypes that file in full: a wasted retype, never a diff
        applied to the wrong base. Same no-state no-op as mark_stale."""
        current = cls.read(window_id, base_dir)
        if current is None:
            return
        cls.update(
            window_id,
            base_dir=base_dir,
            stale_files=tuple(stale for stale in current.stale_files if stale != file_path),
        )

    @classmethod
    def clear(cls, window_id: str, base_dir: Path | None = None) -> None:
        _state_path(window_id, base_dir).unlink(missing_ok=True)


def owned_state(
    window_id: str, backend: str, target: str, base_dir: Path | None = None
) -> FollowerState | None:
    """The window's FollowerState if it is readable AND names this follower
    (its backend and target), else None. What a follower asks before it acts
    as a dedicated one: a state that is gone, unreadable, malformed, or that
    a racing `stop` + `start` wrote for ANOTHER follower (re-review of
    sweep-4 T4+T5, N1: the adopted editor's eviction wiped the user's
    unsaved buffer on such a state) proves nothing about this one."""
    try:
        state = FollowerState.read(window_id, base_dir)
    except UNREADABLE_STATE_ERRORS:
        return None
    if state is None or state.backend != backend or state.target != target:
        return None
    return state


def touch_open_files(
    open_files: tuple[str, ...], file_path: str, max_tabs: int
) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """Recency bump: file_path becomes most recent; anything past max_tabs
    falls off the old end. The touched file is by construction never in the
    evicted slice, which is what keeps eviction away from the animating or
    handed-over file.

    Stale marks are not threaded through here on purpose: an evicted file
    loses its mark when the caller persists the shrunken open_files, because
    FollowerState.set enforces stale-is-a-subset-of-open on every write. That
    chokepoint also covers the writers that never see this function (notably
    cmd_toggle's un-mute, which zeroes open_files directly)."""
    files = (*(open_file for open_file in open_files if open_file != file_path), file_path)
    return files[-max_tabs:], files[:-max_tabs]
