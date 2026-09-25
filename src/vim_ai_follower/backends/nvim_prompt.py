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

Only mode "r" exactly is dismissed. "r?" is a confirm prompt and "rm" the
more-prompt; answering those with Enter could pick a choice for the user.

Never used on an ADOPTED nvim: that is the user's own editor, and its prompts
are the user's to read — an automatic Enter would eat them.

pynvim is never imported here: the caller hands in its own `connect`, which
keeps status_surface (imported by hooks) importable without the nvim extra."""

from __future__ import annotations

import contextlib
import logging
import threading
from collections.abc import Callable, Iterator
from typing import Any

logger = logging.getLogger("vim_ai_follower")

POLL_SECONDS = 0.05
# How long stop() waits for the watchdog to notice. A poll is a fast request
# answered in microseconds, so anything near this is a dead socket, and the
# thread is a daemon either way — it cannot keep the hook process alive.
_JOIN_SECONDS = 2.0
_MESSAGES_TAIL = 5
WATCHDOG_THREAD_NAME = "vaf-hit-enter-watchdog"

# Sockets with a guard already active in THIS thread: an entry point that calls
# another (apply_edit -> goto_file) must not start a second watchdog, which
# would answer the same prompt twice and send the spare Enter to normal mode.
_active = threading.local()


def dismiss_hit_enter(nvim: Any) -> dict[str, Any] | None:
    """Answer a hit-enter prompt with <CR> and return the mode that was seen,
    or None when there was nothing to dismiss. Fast requests only."""
    mode: dict[str, Any] = nvim.api.get_mode()
    if mode.get("blocking") and mode.get("mode") == "r":
        nvim.api.input("<CR>")
        return mode
    return None


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
    """Polls a second connection and dismisses hit-enter prompts until
    stop(). Every failure — the connection refused, a poll raising — is
    logged and ends the thread; nothing is ever raised to the hook. The
    connection is closed by the thread itself, on every exit path."""

    def __init__(self, connect: Callable[[], Any], label: str) -> None:
        self._connect = connect
        self._label = label
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
                seen = dismiss_hit_enter(nvim)
                if seen is not None:
                    self.dismissals += 1
                    logger.warning(
                        "nvim follower: dismissed a hit-enter prompt during %s (watchdog)",
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
        swept = dismiss_hit_enter(nvim) is not None
        if swept:
            logger.warning(
                "nvim follower: dismissed a hit-enter prompt left before %s (sweep)", label
            )
        watchdog = Watchdog(connect, label)
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
