"""Resolve the nvim RPC socket to drive: adopt a running nvim's socket, or
launch a dedicated `nvim --listen` in a tmux split. macOS keeps a socket-less
nvim's socket at $TMPDIR/nvim.$USER/<random>/nvim.<pid>.0 (XDG_RUNTIME_DIR is
unset), so adoption globs by the pane's nvim pid.

Also the bound on every nvim RPC: an nvim that stops answering (SIGSTOPped,
or wedged behind a prompt) used to hang `status`, `stop` and every hook until
Claude Code's 600 s hook timeout. Liveness is a raw `nvim_get_mode` probe with
a socket timeout (probe_nvim), and a connect or request made in the main
thread runs under a SIGALRM deadline (rpc_deadline) that raises RpcTimeout,
an OSError. A probe that gets no answer ("silent") is NOT a dead nvim: one
running `:!make`, `system()` or a long Lua loop is silent too, so only
"absent" (nobody listening) may make a follower read as gone, and only an
absent socket is ever unlinked. A REFUSED connect is not proof of absence
either: on macOS a connection its client closed keeps its slot in the listen
queue, so after 32 probes of an nvim that never accepts, connect is refused
exactly as on a dead nvim's file; a refusal reads "absent" only when no live
process was launched with `--listen <that socket>` and, for nvim's default
socket name (`nvim.<pid>.<n>`, an adopted nvim's), its pid is gone. The
requests that type an animation are exempt from the request deadline
(unbounded_requests): there, an nvim waiting on its user is to be waited
for.

pynvim is never imported at module level here: hooks.py imports this module
and must stay importable without the optional `nvim` extra."""

from __future__ import annotations

import contextlib
import os
import re
import shutil
import signal
import socket
import subprocess
import threading
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

from vim_ai_follower import cache, state
from vim_ai_follower.tmux import TmuxPane

# One RPC request. A legitimate slow request (a bufload that runs a plugin's
# FileType autocmd) finishes well inside it; a prompt the watchdog sweeps
# costs well under a second.
RPC_TIMEOUT_SECONDS = 10.0
# pynvim.attach, including the api-info request it makes on connecting.
CONNECT_TIMEOUT_SECONDS = 3.0
# The liveness probe: nvim answers nvim_get_mode even at a hit-enter prompt,
# confirm() or input(), but NOT while it runs `:!cmd`, `system()` or busy Lua
# (measured) — so a miss ("silent") means busy or stopped, never gone.
LIVENESS_TIMEOUT_SECONDS = 1.0
# msgpack-RPC `[0, 1, "nvim_get_mode", []]`, hand-encoded so the probe needs
# neither pynvim nor msgpack: fixarray(4), request type 0, msgid 1,
# fixstr(13) "nvim_get_mode", fixarray(0).
_GET_MODE_REQUEST = b"\x94\x00\x01\xadnvim_get_mode\x90"
# Its answer starts `[1, 1, nil, ` (fixarray(4), response type 1, msgid 1, no
# error), followed by the mode dictionary: a fixmap, map16 or map32 marker.
_GET_MODE_REPLY_PREFIX = b"\x94\x01\x01\xc0"
_MAP_MARKERS = frozenset(range(0x80, 0x90)) | {0xDE, 0xDF}
# The dictionary's `"blocking"` key (fixstr(8)) and its msgpack true: an nvim
# waiting on its user (hit-enter, confirm(), input()) answers get_mode with
# blocking true, and holds every other request until the user answers.
_BLOCKING_KEY = b"\xa8blocking"
_MSGPACK_TRUE = 0xC3
# The dictionary's `"mode"` key (fixstr(4)); nvim's mode names are all short
# enough for a fixstr (0xa0 + length) value: "r" is the hit-enter prompt,
# "rm" the more-prompt, "r?" a confirm, "c" an input() (measured 2026-10-17).
_MODE_KEY = b"\xa4mode"
_FIXSTR_FIRST, _FIXSTR_LAST = 0xA0, 0xBF
# The process scan that tells a full listen queue from a dead socket file:
# one `ps`, bounded; a scan that fails or runs out counts as a live listener.
PROCESS_SCAN_TIMEOUT_SECONDS = 2.0
# nvim's default server name, `…/nvim.<user>/<random>/nvim.<pid>.<n>` on
# macOS ($TMPDIR) and Linux ($XDG_RUNTIME_DIR or the same $TMPDIR form):
# the pid is the nvim's own (measured 2026-10-17, nvim 0.12.6). An adopted
# nvim's socket has this name and no `--listen` on its command line.
_DEFAULT_SOCKET_NAME = re.compile(r"nvim\.([1-9][0-9]*)\.[0-9]+")
# msgpack-RPC `[0, 1, "nvim_get_vvar", ["vim_did_enter"]]`, hand-encoded like
# the probe: fixarray(4), request 0, msgid 1, fixstr(13), fixarray(1) of a
# fixstr(13). Its answer once nvim has started is `[1, 1, nil, 1]`.
_DID_ENTER_REQUEST = b"\x94\x00\x01\xadnvim_get_vvar\x91\xadvim_did_enter"
_DID_ENTER_REPLY = b"\x94\x01\x01\xc0\x01"
# msgpack-RPC `[0, 1, "nvim_call_function", ["getcmdwintype", []]]`, hand-encoded
# like the probe: fixstr(18) method, fixarray(2) args of a fixstr(13) and an
# empty fixarray. Its answer is `[1, 1, nil, <fixstr>]`: "" outside the
# command-line window, its type (":", "/", "?") inside one.
_CMDWIN_REQUEST = b"\x94\x00\x01\xb2nvim_call_function\x92\xadgetcmdwintype\x90"
_CMDWIN_REPLY_PREFIX = b"\x94\x01\x01\xc0"
# Read at most this much of the reply looking for the key: get_mode's whole
# answer is about 30 bytes.
_REPLY_READ_LIMIT = 256

# Bounded wait, in a hook, for a tmux split's nvim to ANSWER on its socket
# and be started (nvim_ready: real RPC replies). The socket file is no readiness proof: an
# nvim spinning in its init binds it and never answers, and one that exits
# leaves nothing — both were recorded as the follower and `start` said
# "attached". Past the wait the split is an error (SplitNvimNeverAnswered),
# its pane left open so its own error stays readable.
_SOCKET_WAIT_SECONDS = 3.0
# `claude-follow start` is the user waiting at a prompt for the answer: it
# gives a heavy config the standalone launch's patient ceiling.
START_SPLIT_WAIT_SECONDS = 10.0
# A standalone window is a GUI app's or a terminal app's cold launch, slower
# than a tmux split, and here the wait is also the verdict: past it the launch
# is reported as failed (NvimNeverListened) instead of attached. The number is
# a generous ceiling, not a measurement.
_STANDALONE_SOCKET_WAIT_SECONDS = 10.0
# The hook auto-open path cannot afford that: a hook blocks the tool call it
# runs in, so every Edit/Write/Read would stall the full wait on a launcher
# that starts nothing. It waits briefly, and after a failure the window backs
# off auto-opening for LAUNCH_BACKOFF_SECONDS (`claude-follow start` is the
# explicit, patient retry and clears the backoff).
AUTO_OPEN_SOCKET_WAIT_SECONDS = 3.0
LAUNCH_BACKOFF_SECONDS = 300.0
# The socket sweep spares a socket file younger than this: a launch whose
# nvim is still booting (past the caller's wait, see answering_socket) has
# bound its socket but may not answer yet, and no .pane names it until then.
SOCKET_SWEEP_GRACE_SECONDS = 60.0
_SOCKET_POLL_INTERVAL_SECONDS = 0.02


class StandaloneLaunchFailed(Exception):
    """A standalone nvim could not be brought up: nothing to attach to."""


class SocketPathBlocked(StandaloneLaunchFailed):
    """Something nobody answers on sits at the socket path and cannot be
    removed (a directory, a file we may not delete): nvim could never bind."""

    def __init__(self, sock: str, cause: OSError) -> None:
        super().__init__(f"cannot clear {sock}: {cause}")
        self.sock = sock


class SplitNvimNotStarted(StandaloneLaunchFailed):
    """The nvim on this window's launch socket answers but has not finished
    starting: a split left open at its startup prompt (an init error, a swap
    ATTENTION, an `input()`). Neither attached nor split beside."""

    def __init__(self, sock: str) -> None:
        super().__init__(
            f"nvim on {sock} has not finished starting (a prompt in its pane?); "
            "press Enter there or close the pane"
        )
        self.sock = sock


class NvimNeverListened(StandaloneLaunchFailed):
    """The standalone launcher returned, but no nvim listened on the socket
    before the deadline: nothing to attach to."""

    def __init__(self, sock: str, launcher: str, waited: float) -> None:
        super().__init__(f"{launcher} exited but no nvim listened on {sock} within {waited:g} s")
        self.sock = sock
        self.launcher = launcher


class SplitNvimNeverAnswered(Exception):
    """The tmux split ran `nvim --listen <sock>`, but no nvim answered on the
    socket before the deadline: nothing to attach to. The pane is never
    closed for it — nvim's own error is on it (Alberto's answer B,
    2026-10-16) — so the message says where to read why, or that the pane is
    already gone because nvim exited."""

    def __init__(self, sock: str, waited: float, pane_id: str, *, pane_open: bool) -> None:
        fate = (
            "its pane is left open so you can read why"
            if pane_open
            else "its pane has closed (nvim exited)"
        )
        super().__init__(f"nvim did not answer on {sock} within {waited:g} s; {fate}")
        self.sock = sock
        self.pane_id = pane_id


def launch_socket_path(window_id: str, base_dir: Path | None = None) -> Path:
    return state.nvim_socket_path(window_id, base_dir)


def _socket_path_in_existing_dir(window_id: str) -> Path:
    """The launch socket path, with its directory created: nvim refuses
    `--listen` into a missing directory ("Failed to --listen: no such file
    or directory") and exits, so the follower never comes up. On a fresh
    install nothing may have created the cache dir yet — outside tmux no
    keybinding claim writes there before `start` launches nvim (found by the
    battery check 9 e2e, 2026-09-29)."""
    path = launch_socket_path(window_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def discover_adopt_socket(pane_id: str) -> str | None:
    pid = TmuxPane(pane_id=pane_id).pane_pid()
    if pid is None:
        return None
    tmpdir = os.environ.get("TMPDIR", "/tmp")
    user = os.environ.get("USER", "")
    matches = sorted(Path(tmpdir, f"nvim.{user}").glob(f"*/nvim.{pid}.0"))
    return str(matches[0]) if matches else None


def resolve_nvim_target(
    origin_pane: str, window_id: str, adopt: bool, wait_seconds: float = _SOCKET_WAIT_SECONDS
) -> tuple[str, bool]:
    """(socket, launched): the user's nvim in the origin pane when `adopt`
    finds one, else this window's dedicated split. Raises
    SplitNvimNeverAnswered when the split's nvim does not answer within
    `wait_seconds`, SocketPathBlocked when the socket path cannot be
    cleared for it, SplitNvimNotStarted when the nvim already there has not
    finished starting, CalledProcessError when tmux cannot split. The wait is
    `wait_seconds` in all: each check and each pause is capped to the time
    left, so the message's number is the time spent."""
    if adopt:
        found = discover_adopt_socket(origin_pane)
        if found is not None:
            return found, False
    sock = str(_socket_path_in_existing_dir(window_id))
    if nvim_ready(sock):
        # A split whose nvim came up after its caller stopped waiting (its
        # pane was left open): attach. A second split's nvim would die on
        # "address already in use" next to it.
        return sock, True
    if last_probe_verdict(sock) == "answers":
        raise SplitNvimNotStarted(sock)
    _remove_dead_socket(sock)
    split = subprocess.run(
        [
            "tmux", "split-window", "-h", "-P", "-F", "#{pane_id}", "-t", origin_pane,
            "nvim", "--listen", sock,
        ],
        capture_output=True,
        text=True,
        check=True,
    )  # fmt: skip
    pane_id = split.stdout.strip()
    deadline = time.monotonic() + wait_seconds
    while True:
        left = deadline - time.monotonic()
        if left <= 0:
            pane_open = TmuxPane(pane_id=pane_id).running_command() is not None
            raise SplitNvimNeverAnswered(sock, wait_seconds, pane_id, pane_open=pane_open)
        if nvim_ready(sock, min(LIVENESS_TIMEOUT_SECONDS, left)):
            return sock, True
        time.sleep(min(_SOCKET_POLL_INTERVAL_SECONDS, max(deadline - time.monotonic(), 0.0)))


def standalone_launch_command(
    sock: str, *, has_nvim_qt: bool, has_vimr: bool, is_iterm: bool
) -> list[str]:
    """Argv to open a VISIBLE nvim listening on `sock`, preferring a native
    GUI, then a split pane inside the user's own iTerm2 window/tab (so it
    lands beside their existing work instead of a disconnected process),
    and only falling back to a fresh Terminal.app window (always present
    on macOS) when neither a GUI app nor iTerm2 is available."""
    if has_nvim_qt:
        return ["nvim-qt", "--", "--listen", sock]
    if has_vimr:
        return ["open", "-a", "VimR", "--args", "--listen", sock]
    if is_iterm:
        # Verified against the installed iTerm2.sdef (2026-09-04): `split
        # vertically with default profile` is a command on the `session`
        # class, taking an optional `command` parameter. Targeting the
        # bundle id (not "iTerm2" by name) is immune to a future app
        # rename. This puts nvim in a NEW pane beside the user's current
        # work, in the SAME window/tab — the thing Alberto asked for
        # instead of a disconnected Terminal.app window (2026-09-03).
        script = (
            'tell application id "com.googlecode.iterm2"\n'
            "  tell current session of current window\n"
            f'    split vertically with default profile command "nvim --listen {sock}"\n'
            "  end tell\n"
            "end tell"
        )
        return ["osascript", "-e", script]
    # `do script` alone can create the window WITHOUT bringing it on screen: if
    # Terminal.app is already running (even with no windows, or backgrounded on
    # another Space), the new window comes back `visible=false` and Terminal
    # never becomes frontmost — the command reports success but nothing is
    # visible to the user (live finding, 2026-09-03). `activate` first is what
    # actually shows the window.
    script = (
        'tell app "Terminal" to activate\n'
        f'tell app "Terminal" to do script "exec nvim --listen {sock}"'
    )
    return ["osascript", "-e", script]


def _vimr_app_present() -> bool:
    return Path("/Applications/VimR.app").exists()


class RpcTimeout(OSError):
    """nvim did not answer within the deadline. An OSError on purpose: every
    `except OSError` that handles a dead socket handles a frozen nvim too."""


@contextlib.contextmanager
def rpc_deadline(seconds: float) -> Iterator[None]:
    """Raise RpcTimeout in the main thread when the block outlives `seconds`.

    A SIGALRM timer (setitimer), whose handler raises: measured 2026-10-16
    against a SIGSTOPped nvim, it interrupts pynvim's asyncio loop both
    inside `attach` and inside a request, and the process connects again
    once the nvim is resumed. The previous handler and timer come back in
    `finally`; a nested deadline keeps whichever expires first. Outside the
    main thread signals cannot be used, so it is a no-op there (the prompt
    watchdog's thread makes only fast requests, on its own connection)."""
    if threading.current_thread() is not threading.main_thread():
        yield
        return
    outer_remaining, _interval = signal.getitimer(signal.ITIMER_REAL)
    if outer_remaining and outer_remaining <= seconds:
        yield  # the enclosing deadline is the shorter one: it stays in charge
        return

    def expired(_signum: int, _frame: object) -> None:
        raise RpcTimeout(f"nvim did not answer within {seconds:g} s")

    started = time.monotonic()
    previous_handler = signal.signal(signal.SIGALRM, expired)
    signal.setitimer(signal.ITIMER_REAL, seconds)
    try:
        yield
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, previous_handler)
        if outer_remaining:
            left = outer_remaining - (time.monotonic() - started)
            # An outer deadline that passed meanwhile still fires, at once.
            signal.setitimer(signal.ITIMER_REAL, max(left, 0.001))


def bounded_attach(socket_path: str) -> Any:
    """`pynvim.attach` under CONNECT_TIMEOUT_SECONDS, with every later request
    on the connection run under RPC_TIMEOUT_SECONDS (main thread only, like
    rpc_deadline). pynvim 0.6.0 routes every API call — `nvim.api.*`,
    `nvim.command`, a Buffer's or Window's methods — through the one
    `Session.request` of the connection, so wrapping it covers them all.
    Inside unbounded_requests(nvim) the request deadline is lifted."""
    import pynvim

    with rpc_deadline(CONNECT_TIMEOUT_SECONDS):
        nvim = pynvim.attach("socket", path=socket_path)
    session = nvim._session
    unbounded_request = session.request
    bounds = {"lifted": 0}

    def bounded_request(method: Any, *args: Any, **kwargs: Any) -> Any:
        if bounds["lifted"]:
            return unbounded_request(method, *args, **kwargs)
        with rpc_deadline(RPC_TIMEOUT_SECONDS):
            return unbounded_request(method, *args, **kwargs)

    setattr(session, "request", bounded_request)  # noqa: B010 - an instance override of a method
    setattr(session, _BOUNDS_ATTRIBUTE, bounds)
    return nvim


_BOUNDS_ATTRIBUTE = "_vaf_bounds"


@contextlib.contextmanager
def unbounded_requests(nvim: Any) -> Iterator[None]:
    """Lift bounded_attach's request deadline for the body, on this
    connection: the requests that TYPE an animation. There an nvim that stops
    answering is, far more often than not, an nvim waiting on its user (keys
    pending, or a prompt raised where no watchdog answers it), and
    cutting a request at RPC_TIMEOUT_SECONDS crashed the hook with a
    half-typed line and "Writing..." left up — while the abandoned request
    still landed at Enter. The outer bound is the hook's own timeout. The
    connect, the liveness probe, navigation, status, stop and the status float
    keep their deadlines. A no-op on a connection bounded_attach did not make."""
    bounds = getattr(nvim._session, _BOUNDS_ATTRIBUTE, None)
    if not isinstance(bounds, dict):
        yield
        return
    bounds["lifted"] += 1
    try:
        yield
    finally:
        bounds["lifted"] -= 1


# The last verdict probe_nvim reached for each socket, in this process: lets
# a hook that asked FollowerState.get (which probes) tell a busy follower
# from an idle one without probing a second time.
_last_verdicts: dict[str, str] = {}
# Whether that answer said the nvim is BLOCKING, waiting on its user.
_last_blocking: dict[str, bool] = {}
# The mode that answer named, None when it named none (or nothing answered).
_last_modes: dict[str, str | None] = {}


def last_probe_verdict(sock: str) -> str | None:
    """probe_nvim's latest verdict on `sock` in this process, None if never probed."""
    return _last_verdicts.get(sock)


def last_probe_blocking(sock: str) -> bool:
    """True when probe_nvim's latest answer on `sock` in this process said
    `blocking: true` — an nvim at a hit-enter prompt, confirm() or input(),
    which answers get_mode but holds every other request until its user
    answers. It still "answers": alive, and reachable once the prompt goes."""
    return _last_blocking.get(sock, False)


def last_probe_mode(sock: str) -> str | None:
    """The `mode` probe_nvim's latest answer on `sock` named in this process:
    with last_probe_blocking, it tells a hit-enter ("r") or more-prompt
    ("rm") — which the follower answers when it acts — from anything else
    that blocks. None when the latest probe got no answer, or none named."""
    return _last_modes.get(sock)


def probe_nvim(sock: str, timeout: float = LIVENESS_TIMEOUT_SECONDS) -> str:
    """Ask whatever listens on `sock` for nvim_get_mode, raw, within `timeout`.

    "answers": a well-formed reply to this request. "absent": no socket file,
    or nobody listening (a SIGKILLed nvim leaves its file behind) and no live
    process launched to listen there. "silent": the connection was accepted
    and nothing came back in time — a SIGSTOPped nvim, and just as well a
    live one busy in `:!cmd`, `system()` or a Lua loop — or it was refused
    while a live process listens there (a queue full of probes it never
    accepted). "garbled": something answered that is not an nvim's reply, or hung
    up. Thread-safe, no pynvim. The verdict is remembered (last_probe_verdict),
    and so are whether an answer said `blocking: true` (last_probe_blocking)
    and the mode it named (last_probe_mode)."""
    verdict, blocking, mode = _probe(sock, timeout)
    _last_verdicts[sock] = verdict
    _last_blocking[sock] = blocking
    _last_modes[sock] = mode
    return verdict


def _well_formed(received: bytes) -> bool:
    return (
        received.startswith(_GET_MODE_REPLY_PREFIX)
        and received[len(_GET_MODE_REPLY_PREFIX)] in _MAP_MARKERS
    )


def _blocking(received: bytes) -> bool | None:
    """The reply's `blocking` value, None while it has not arrived."""
    value_offset = received.find(_BLOCKING_KEY) + len(_BLOCKING_KEY)
    if value_offset == len(_BLOCKING_KEY) - 1 or len(received) <= value_offset:
        return None
    return received[value_offset] == _MSGPACK_TRUE


def _mode(received: bytes) -> tuple[bool, str | None]:
    """(settled, the reply's `mode`): unsettled while the key or its value
    has not fully arrived; settled with None when the value is not a fixstr."""
    key_offset = received.find(_MODE_KEY)
    if key_offset == -1:
        return False, None
    marker_offset = key_offset + len(_MODE_KEY)
    if len(received) <= marker_offset:
        return False, None
    marker = received[marker_offset]
    if not _FIXSTR_FIRST <= marker <= _FIXSTR_LAST:
        return True, None
    value = received[marker_offset + 1 : marker_offset + 1 + marker - _FIXSTR_FIRST]
    if len(value) < marker - _FIXSTR_FIRST:
        return False, None
    return True, value.decode("utf-8", "replace")


def _probe(sock: str, timeout: float) -> tuple[str, bool, str | None]:
    """(verdict, blocking, mode): see probe_nvim. After a well-formed start
    it keeps reading for the `blocking` and `mode` values; a reply that ends,
    stalls or runs long without them is an answer from an nvim not known to
    be blocking, in no known mode."""
    probe = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    probe.settimeout(timeout)
    try:
        try:
            probe.connect(sock)
        except TimeoutError:
            return "silent", False, None
        except FileNotFoundError:
            return "absent", False, None
        except OSError:  # refused: a dead nvim's file, or a full listen queue
            return ("silent" if refused_by_a_live_nvim(sock) else "absent"), False, None
        deadline = time.monotonic() + timeout
        received = b""
        try:
            probe.sendall(_GET_MODE_REQUEST)
            while len(received) < len(_GET_MODE_REPLY_PREFIX) + 1:
                left = deadline - time.monotonic()
                if left <= 0:
                    return "silent", False, None
                probe.settimeout(left)
                chunk = probe.recv(64)
                if not chunk:
                    return "garbled", False, None
                received += chunk
        except TimeoutError:
            return "silent", False, None
        except OSError:
            return "garbled", False, None
        if not _well_formed(received):
            return "garbled", False, None
        try:
            while (_blocking(received) is None or not _mode(received)[0]) and len(
                received
            ) < _REPLY_READ_LIMIT:
                left = deadline - time.monotonic()
                if left <= 0:
                    break
                probe.settimeout(left)
                chunk = probe.recv(64)
                if not chunk:
                    break
                received += chunk
        except OSError:
            pass
    finally:
        probe.close()
    return "answers", bool(_blocking(received)), _mode(received)[1]


def nvim_answers(sock: str, timeout: float = LIVENESS_TIMEOUT_SECONDS) -> bool:
    """True only when an nvim answers on `sock` within `timeout`: a protocol
    check, not "something accepted a connection"."""
    return probe_nvim(sock, timeout) == "answers"


def nvim_ready(sock: str, timeout: float = LIVENESS_TIMEOUT_SECONDS) -> bool:
    """True only when an nvim answers on `sock` AND has finished starting
    (`v:vim_did_enter`): the readiness of a split nvim launched for the
    follower. An answer alone is not enough — measured 2026-10-17, a TUI
    nvim answers get_mode for a few ms after binding its socket, before its
    init runs (the embedded server waits for its UI), and one whose init then
    spins never answers again; an init that errors leaves nvim at a startup
    hit-enter prompt, which answers get_mode and holds every other request,
    so its error stays on screen instead of being swept as a follower prompt."""
    deadline = time.monotonic() + timeout
    if not nvim_answers(sock, timeout):
        return False
    left = deadline - time.monotonic()
    return left > 0 and _did_enter(sock, left)


def _did_enter(sock: str, timeout: float) -> bool:
    """Ask for `v:vim_did_enter`, raw like _probe: True only on the exact
    `[1, 1, nil, 1]` answer within `timeout`."""
    probe = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    probe.settimeout(timeout)
    deadline = time.monotonic() + timeout
    received = b""
    try:
        probe.connect(sock)
        probe.sendall(_DID_ENTER_REQUEST)
        while len(received) < len(_DID_ENTER_REPLY):
            # 0 s past the deadline is non-blocking: a recv with nothing
            # waiting raises BlockingIOError, an OSError like the timeout.
            probe.settimeout(max(deadline - time.monotonic(), 0.0))
            chunk = probe.recv(64)
            if not chunk:
                return False
            received += chunk
    except OSError:  # TimeoutError included: held at a prompt, busy, or gone
        return False
    finally:
        probe.close()
    return received.startswith(_DID_ENTER_REPLY)


def in_command_line_window(sock: str, timeout: float = LIVENESS_TIMEOUT_SECONDS) -> bool | None:
    """Whether the nvim on `sock` has its command-line window open (`q:`,
    `q/`), raw like _probe, within `timeout`. get_mode cannot tell: inside
    it the mode reads "n" (or "i"), not blocking, while every window change
    raises E11 (measured 2026-10-17, nvim 0.12.6). None when no well-formed
    answer came in time — not an ordinary request's answer at a blocking
    prompt, so ask only an nvim the probe found not blocking."""
    connection = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    connection.settimeout(timeout)
    deadline = time.monotonic() + timeout
    received = b""
    try:
        connection.connect(sock)
        connection.sendall(_CMDWIN_REQUEST)
        while len(received) < len(_CMDWIN_REPLY_PREFIX) + 1:
            connection.settimeout(max(deadline - time.monotonic(), 0.0))
            chunk = connection.recv(64)
            if not chunk:
                return None
            received += chunk
    except OSError:  # TimeoutError included: held at a prompt, busy, or gone
        return None
    finally:
        connection.close()
    if not received.startswith(_CMDWIN_REPLY_PREFIX):
        return None
    marker = received[len(_CMDWIN_REPLY_PREFIX)]
    if not _FIXSTR_FIRST <= marker <= _FIXSTR_LAST:
        return None
    return marker != _FIXSTR_FIRST


def ready_split_socket(window_id: str) -> str | None:
    """This window's launch socket when a STARTED nvim answers on it — a
    split whose nvim came up after its caller stopped waiting."""
    sock = str(launch_socket_path(window_id))
    return sock if nvim_ready(sock) else None


def refused_by_a_live_nvim(sock: str) -> bool:
    """For a socket whose connect was refused: True when an nvim may still
    own it — its default name's pid lives, or a live process listens there
    (is_listened_on_by_a_live_process, failing closed). Either proves life."""
    return _default_socket_owner_lives(sock) or is_listened_on_by_a_live_process(sock)


def _default_socket_owner_lives(sock: str) -> bool:
    """`kill -0` on the pid in nvim's default socket name: True when it
    succeeds or is refused (EPERM: the process exists), False on ESRCH or
    for any other name."""
    named = _DEFAULT_SOCKET_NAME.fullmatch(Path(sock).name)
    if named is None:
        return False
    try:
        os.kill(int(named.group(1)), 0)
    except PermissionError:
        return True
    except (ProcessLookupError, OverflowError):
        return False
    return True


def is_listened_on_by_a_live_process(sock: str) -> bool:
    """True when a live process's command line has `--listen <sock>`, the
    path a whole argument (an nvim launched to listen there, or the `--embed`
    server its TUI starts with the same arguments), or when the scan cannot
    say: one `ps` within PROCESS_SCAN_TIMEOUT_SECONDS, failing CLOSED, since
    the caller is about to treat the socket as dead. `ps` prints argv joined
    by spaces, unquoted, so the match is on the joined text: a path with
    spaces still matches whole."""
    try:
        listing = subprocess.run(
            ["ps", "-A", "-ww", "-o", "command="],
            capture_output=True,
            text=True,
            check=False,
            timeout=PROCESS_SCAN_TIMEOUT_SECONDS,
        )
    except (OSError, subprocess.SubprocessError):
        return True
    if listing.returncode != 0:
        return True
    markers = {f" --listen {sock}", f" --listen {os.path.realpath(sock)}"}
    for command_line in listing.stdout.splitlines():
        for marker in markers:
            start = command_line.find(marker)
            while start != -1:
                end = start + len(marker)
                if end == len(command_line) or command_line[end].isspace():
                    return True
                start = command_line.find(marker, start + 1)
    return False


def _accepts(sock: str) -> bool:
    """True when something ACCEPTS a connection on the socket, nvim or not:
    the test for a path that must not be unlinked from under a listener."""
    probe = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    probe.settimeout(_SOCKET_POLL_INTERVAL_SECONDS * 10)
    try:
        probe.connect(sock)
    except OSError:
        return False
    finally:
        probe.close()
    return True


def sweep_stale_sockets() -> list[str]:
    """Remove the cache dir's `nvim-*.sock` files that no follower state names
    and nobody listens on (a SIGKILLed nvim's leftovers, one per window ever
    followed), and return their paths. A socket a `*.pane` names — by its
    window's socket path or as its target, readable or not — stays; so does
    one younger than SOCKET_SWEEP_GRACE_SECONDS (a launch still booting), and
    any socket the probe does not find "absent": an nvim busy in `:!make` is
    silent, and its socket must not be unlinked from under it."""
    directory = cache.CACHE_DIR
    named: set[str] = set()
    for pane in directory.glob("*.pane"):
        named.add(os.path.realpath(state.nvim_socket_path(pane.stem, directory)))
        try:
            recorded = state.FollowerState.read(pane.stem, directory)
        except (OSError, ValueError):  # unreadable: its window's path is kept above
            continue
        if recorded is not None:
            named.add(os.path.realpath(recorded.target))
    removed = []
    for candidate in sorted(directory.glob("nvim-*.sock")):
        if os.path.realpath(candidate) in named or not candidate.is_socket():
            continue
        if time.time() - candidate.lstat().st_mtime < SOCKET_SWEEP_GRACE_SECONDS:
            continue
        if probe_nvim(str(candidate)) != "absent":
            continue
        with contextlib.suppress(OSError):
            candidate.unlink()
            removed.append(str(candidate))
    return removed


def _remove_dead_socket(sock: str) -> None:
    """Clear a socket file nobody answers on, so the new nvim can bind the
    path and a leftover can never pass for it. One that answers is a live
    nvim's and is never removed."""
    path = Path(sock)
    if not path.exists():
        return
    if _accepts(sock) or refused_by_a_live_nvim(sock):
        # Someone listens there but did not answer as an nvim (the caller
        # asked first) — a busy nvim (`:!make`) is silent too, and one that
        # never accepts refuses once its queue is full: neither adopted nor
        # unlinked from under it, and no second nvim is launched over it.
        raise SocketPathBlocked(
            sock,
            OSError(
                "a listener that did not answer as an nvim within "
                f"{LIVENESS_TIMEOUT_SECONDS:g} s (busy, stopped, or not an nvim)"
            ),
        )
    try:
        path.unlink(missing_ok=True)
    except OSError as exc:  # a directory there, or a file we may not delete
        raise SocketPathBlocked(sock, exc) from exc


def launch_backoff_path(window_id: str) -> Path:
    return cache.CACHE_DIR / f"{window_id}.launch-failed"


def record_launch_failure(window_id: str) -> None:
    path = launch_backoff_path(window_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.touch()  # the mtime is the failure's time


def launch_backoff_active(window_id: str) -> bool:
    try:
        failed_at = launch_backoff_path(window_id).stat().st_mtime
    except FileNotFoundError:
        return False
    return time.time() - failed_at < LAUNCH_BACKOFF_SECONDS


def clear_launch_backoff(window_id: str) -> None:
    launch_backoff_path(window_id).unlink(missing_ok=True)


def answering_socket(window_id: str) -> str | None:
    """This window's standalone socket when an nvim already answers on it —
    typically one a slow launcher bound after the caller stopped waiting.
    An nvim, not whatever accepts a connection there."""
    sock = str(launch_socket_path(window_id))
    return sock if nvim_answers(sock) else None


def launch_standalone_nvim(
    window_id: str, wait_seconds: float = _STANDALONE_SOCKET_WAIT_SECONDS
) -> str:
    sock = str(_socket_path_in_existing_dir(window_id))
    if nvim_answers(sock, LIVENESS_TIMEOUT_SECONDS):
        # Already up (a launch that outlived its caller's wait): attach. A
        # second launch's nvim would die on "address already in use" and
        # stay on screen as an orphan error window.
        return sock
    _remove_dead_socket(sock)
    cmd = standalone_launch_command(
        sock,
        has_nvim_qt=shutil.which("nvim-qt") is not None,
        has_vimr=shutil.which("VimR") is not None or _vimr_app_present(),
        is_iterm=os.environ.get("TERM_PROGRAM") == "iTerm.app",
    )
    subprocess.run(cmd, check=True)
    deadline = time.monotonic() + wait_seconds
    while not nvim_answers(sock) and time.monotonic() < deadline:
        time.sleep(_SOCKET_POLL_INTERVAL_SECONDS)
    if not nvim_answers(sock):
        # The launcher exited 0 (nvim-qt that found no nvim, a terminal whose
        # nvim died on a config error): there is nothing to attach to, and a
        # follower persisted here would point at a socket no one listens on.
        raise NvimNeverListened(sock, cmd[0], wait_seconds)
    return sock
