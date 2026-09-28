"""Keep a plugin's hit-enter prompt from freezing a DEDICATED nvim follower.

The follower's nvim loads the user's whole config on purpose (it is where
gruvbox comes from), so every FileType/BufRead the follower fires runs the
user's plugins too — LSP autostart included. When one of them prints a message
taller than the command line, nvim raises a hit-enter prompt (mode "r"), and the
RPC call that fired it does not return until someone presses Enter: the
animation freezes. Measured 2026-09-25 with a mason x86_64 `ruff` on a Mac
without Rosetta ("Unknown system error -86"); any plugin message does it, at
any pane width.

Two mechanisms, both needed (each measured against a real UI nvim):

- a SWEEP, one fast `nvim_get_mode` before an entry point's first real call,
  clears a prompt raised between hooks (a plugin reporting asynchronously after
  the previous hook returned);
- a WATCHDOG, a daemon thread on a SECOND connection polling `nvim_get_mode`
  every POLL_SECONDS, clears a prompt raised INSIDE a call. The call's own
  channel cannot: it is the one blocked. `get_mode` and `input` are "fast"
  requests nvim answers even while a prompt blocks everything else.

Rejected by measurement: `--clean` (loses the user's look), `nomore`,
`shortmess`, dismiss-before-each-call alone (the prompt is raised inside the
call), and capturing output with `nvim_exec2` alone (a message delivered after
the call returns hangs the next one).

Two prompts are answered. The hit-enter prompt (mode "r") gets <CR>. The
more-prompt (mode "rm"), which a message taller than the screen raises instead,
gets <Esc> (`:h more-prompt`): it offers no choice, and <CR> would only page
it one line. <Esc> returns to normal mode and the blocked call completes. Never
`q`, though it quits the prompt the same way: every answer can arrive SPARE —
two guards on one socket both saw the prompt, or the user pressed a key at the
same moment — and then lands in normal mode, where `q` starts macro recording,
leaves nvim blocking in mode "n" that nothing here answers, and hangs every
later call (measured 2026-09-28: 4/40 frozen with two real watchdogs racing,
0/40 with <Esc>). Every other mode is left alone, "r?" above all: a confirm
prompt IS a choice, and a key would make it for the user.

A spare answer is NOT harmless, whatever the key. An answer made AT a prompt is
taken as the prompt's answer (measured 20/20 unmapped), but a spare one lands in
normal mode and goes through the USER's mappings — the dedicated follower loads
the user's whole config. Measured 2026-09-28: with `<Esc><Esc>` mapped and
`notimeout`, a spare <Esc> leaves nvim blocking in mode "n" forever; with
`nnoremap <CR> o<Esc>`, a spare <CR> edits the buffer (or, locked, raises E21
and a new hit-enter prompt). So exactly ONE guard may answer a given prompt:
the read-answer-settle sequence runs under a per-socket, CROSS-PROCESS answer
lock (a non-blocking `flock` on a lock file in the cache directory), and a
guard that cannot take it does not answer — it looks again on its next poll.
Under the lock the prompt is read a second time, so a guard that got the lock
only after another guard's answer landed finds it gone and sends nothing. The
lock is held only for that sequence, never across the entry point's own RPC.

Residual, not solvable here: the USER pressing a key at the very instant a
guard answers. Their key then lands spare in normal mode, through their own
mappings, exactly as they typed it.

Never used on an ADOPTED nvim: that is the user's own editor, and its prompts
are the user's to read — an automatic Enter would eat them.

pynvim is never imported here: the caller hands in its own `connect`, which
keeps status_surface (imported by hooks) importable without the nvim extra."""

from __future__ import annotations

import contextlib
import errno
import fcntl
import hashlib
import logging
import os
import threading
import time
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

from vim_ai_follower import cache

logger = logging.getLogger("vim_ai_follower")

POLL_SECONDS = 0.05
# How long stop() waits for the watchdog to notice. A poll is a fast request
# answered in microseconds, so anything near this is a dead socket, and the
# thread is a daemon either way — it cannot keep the hook process alive.
_JOIN_SECONDS = 2.0
_MESSAGES_TAIL = 5
# After a key is sent, how long to wait for nvim to leave the prompt before
# anyone may send another: a slow nvim still showing the SAME prompt on the
# next poll would otherwise get a spare key, which lands in normal mode.
_SETTLE_SECONDS = 0.4
_SETTLE_POLL_SECONDS = 0.02
# Sent only AT the prompt, by the one guard holding the answer lock (above).
_ANSWERS = {"r": "<CR>", "rm": "<Esc>"}
_NAMES = {"r": "hit-enter prompt", "rm": "more-prompt"}
WATCHDOG_THREAD_NAME = "vaf-hit-enter-watchdog"

# Sockets with a guard already active in THIS thread: an entry point that calls
# another (apply_edit -> goto_file) must not start a second watchdog, which
# would answer the same prompt twice and send the spare Enter to normal mode.
_active = threading.local()

# Sockets whose answer lock already failed with a real error (not "busy"):
# logged once per process, so a read-only cache cannot flood hook.log.
_lock_errors_logged: set[str] = set()


def _answer_lock_path(socket_path: str) -> Path:
    """The answer lock for one nvim socket. In the cache directory, keyed by a
    hash of the socket path, rather than beside the socket: an nvim socket can
    live anywhere (standalone launches, a user-chosen path), and only the
    cache directory is known to be ours and writable. Read at call time, so a
    relocated cache (tests) is honoured."""
    digest = hashlib.sha256(socket_path.encode()).hexdigest()[:16]
    return cache.CACHE_DIR / "answer-locks" / f"{digest}.lock"


@contextlib.contextmanager
def _answer_lock(socket_path: str) -> Iterator[bool]:
    """Try to take this socket's answer lock without waiting; yield whether it
    is held. flock locks belong to the open file description, so two guards
    in ONE process (two os.open calls) exclude each other exactly like two
    processes do. Released and closed on every path. Any OSError other than
    "busy" is logged once per socket and yields False: not answering is
    always safe, the next poll tries again."""
    fd = -1
    held = False
    try:
        path = _answer_lock_path(socket_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        held = True
    except OSError as exc:
        if exc.errno not in (errno.EWOULDBLOCK, errno.EAGAIN) and socket_path not in (
            _lock_errors_logged
        ):
            _lock_errors_logged.add(socket_path)
            logger.warning(
                "nvim follower: answer lock unusable for %s, prompts left unanswered: %s",
                socket_path,
                exc,
            )
    try:
        yield held
    finally:
        if fd != -1:
            with contextlib.suppress(OSError):
                if held:
                    fcntl.flock(fd, fcntl.LOCK_UN)
            os.close(fd)


def _answer_for(mode: dict[str, Any]) -> str | None:
    return _ANSWERS.get(mode.get("mode", "")) if mode.get("blocking") else None


def dismiss_prompt(nvim: Any, socket_path: str) -> dict[str, Any] | None:
    """Answer a blocking hit-enter (<CR>) or more-prompt (<Esc>) and return the
    mode that was answered, or None when nothing was sent. Only the guard
    holding `socket_path`'s answer lock answers, and only if the prompt is
    still up when it re-reads under the lock (module docstring). After
    answering, still under the lock, it waits up to _SETTLE_SECONDS for nvim
    to leave that mode, so no one — itself on its next poll included — sees
    the same prompt again. Fast requests only."""
    if _answer_for(nvim.api.get_mode()) is None:
        return None  # the common case: no prompt, no lock file touched
    with _answer_lock(socket_path) as held:
        if not held:
            return None
        mode: dict[str, Any] = nvim.api.get_mode()
        key = _answer_for(mode)
        if key is None:
            return None
        nvim.api.input(key)
        deadline = time.monotonic() + _SETTLE_SECONDS
        while time.monotonic() < deadline and nvim.api.get_mode() == mode:
            time.sleep(_SETTLE_POLL_SECONDS)
        return mode


def close_connection(nvim: Any) -> None:
    """Close a pynvim connection so nvim drops the channel NOW.

    pynvim's close() closes the socket transport and then, in the same breath,
    its event loop — so asyncio never runs the callback that actually closes
    the socket, and the channel stays open in nvim until the garbage collector
    happens to reach it (measured 2026-09-25, pynvim 0.6.0: the channel still
    listed by nvim_list_chans after close(), gone only after gc.collect(), with
    a ResourceWarning). Closing the transport first and letting the loop run
    one iteration (call_soon(stop) + run_forever) delivers that callback before
    close() shuts the loop. The transport is reached through pynvim internals,
    so any failure falls back to the plain close()."""
    with contextlib.suppress(Exception):
        nvim._session.loop._transport.close()
        loop = nvim.loop
        loop.call_soon(loop.stop)
        loop.run_forever()
    with contextlib.suppress(Exception):
        nvim.close()


class Watchdog:
    """Polls a second connection and dismisses hit-enter and more-prompts until
    stop(). It stays alive for the whole guarded call, a pause included: a
    user's own `:ls` in a PAUSED dedicated follower is dismissed too. Every
    failure — the connection refused, a poll raising — is
    logged and ends the thread; nothing is ever raised to the hook. The
    connection is closed by the thread itself, on every exit path."""

    def __init__(self, connect: Callable[[], Any], label: str, key: str) -> None:
        self._connect = connect
        self._label = label
        self._key = key
        self._stop = threading.Event()
        self.dismissals = 0
        self._thread = threading.Thread(target=self._run, name=WATCHDOG_THREAD_NAME, daemon=True)

    def start(self) -> None:
        self._thread.start()

    def _run(self) -> None:
        try:
            nvim = self._connect()
        except Exception as exc:
            logger.warning(
                "nvim follower: prompt watchdog could not connect (%s): %s", self._label, exc
            )
            return
        try:
            while not self._stop.is_set():
                seen = dismiss_prompt(nvim, self._key)
                if seen is not None:
                    self.dismissals += 1
                    logger.warning(
                        "nvim follower: dismissed a %s during %s (watchdog)",
                        _NAMES[seen["mode"]],
                        self._label,
                    )
                self._stop.wait(POLL_SECONDS)
        except Exception as exc:
            if not self._stop.is_set():
                logger.warning("nvim follower: prompt watchdog stopped (%s): %s", self._label, exc)
        finally:
            close_connection(nvim)

    def stop(self) -> None:
        self._stop.set()
        self._thread.join(_JOIN_SECONDS)
        if self._thread.is_alive():
            logger.warning("nvim follower: prompt watchdog did not stop (%s)", self._label)


def _log_messages_tail(nvim: Any, label: str) -> None:
    """Log the last few lines of `:messages` — what the dismissed prompt was
    showing. Best-effort: a failure here is logged, never raised."""
    try:
        result = nvim.api.exec2("messages", {"output": True})
        tail = str(result.get("output", "")).strip().splitlines()[-_MESSAGES_TAIL:]
        logger.warning("nvim follower: :messages after %s: %s", label, " | ".join(tail))
    except Exception as exc:
        logger.warning("nvim follower: could not read :messages after %s: %s", label, exc)


@contextlib.contextmanager
def prompt_guard(
    nvim: Any, connect: Callable[[], Any], *, key: str, adopted: bool, label: str
) -> Iterator[None]:
    """Sweep, then run the body under a watchdog. A no-op for an adopted nvim
    and for a guard nested inside another on the same socket (`key`).

    The sweep runs on the caller's own connection and its errors propagate
    like any first RPC would (a dead socket must fail the same way it always
    did). After the body, when anything was dismissed, the tail of
    `:messages` is logged — read while the watchdog is still running, so a
    prompt landing at that very moment cannot hang the read — and only then
    is the watchdog stopped, in a finally, whether or not the body raised."""
    guarded: set[str] = getattr(_active, "keys", set())
    if adopted or key in guarded:
        yield
        return
    _active.keys = guarded | {key}
    try:
        seen = dismiss_prompt(nvim, key)
        swept = seen is not None
        if seen is not None:
            logger.warning(
                "nvim follower: dismissed a %s left before %s (sweep)",
                _NAMES[seen["mode"]],
                label,
            )
        watchdog = Watchdog(connect, label, key)
        watchdog.start()
        try:
            yield
        finally:
            if swept or watchdog.dismissals:
                _log_messages_tail(nvim, label)
            watchdog.stop()
    finally:
        _active.keys = guarded


def exec_logged(nvim: Any, command: str, *, adopted: bool) -> None:
    """Run an event-firing Ex command. On a dedicated follower through
    `nvim_exec2` with output captured, so a synchronous plugin message is
    returned instead of raising a prompt, and written to hook.log instead of
    lost. An adopted nvim runs it plainly: its messages are the user's to see
    in their own editor."""
    if adopted:
        nvim.command(command)
        return
    result = nvim.api.exec2(command, {"output": True})
    output = str(result.get("output", "")).strip()
    if output:
        logger.warning("nvim follower: `%s` printed: %s", command, output)
