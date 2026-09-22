"""Resolve whether this run is inside tmux (existing window identity) or
standalone (no tmux) — and, standalone, a stable per-terminal id to key all
follower state on, in place of tmux's #{window_id}.

Also answers the diagnostic follow-up question "if nothing is registered under
the identity I resolved to, who else is out there?" (other_live_followers).
That lives here rather than in state.py because it is about identity, not
about one window's state, and because both the hook path and the CLI need the
same answer — see hooks._warn_lost_window_identity and commands.cmd_status."""

from __future__ import annotations

import os
import subprocess
from dataclasses import dataclass
from pathlib import Path

from vim_ai_follower import cache
from vim_ai_follower.state import FollowerState
from vim_ai_follower.tmux import TmuxWindow


@dataclass(frozen=True)
class Session:
    window_id: str
    origin: str | None
    in_tmux: bool
    recovered_via_pid: bool = False
    """True when TMUX_PANE was missing and the window was recovered by walking
    this process's ancestry (_recover_window_from_ancestry).

    A separate flag rather than a sentinel in `origin`, because `origin` holds
    a real pane id either way and so cannot tell the two apart; and defaulted,
    so every existing `Session(window_id=..., origin=..., in_tmux=...)` call
    and equality assertion keeps working unchanged."""


@dataclass(frozen=True)
class OtherFollower:
    """A live follower registered under some identity other than this run's."""

    window_id: str
    backend: str
    target: str
    current_file: str | None


def other_live_followers(window_id: str) -> list[OtherFollower]:
    """Live followers keyed on an identity other than window_id.

    Purely diagnostic, and deliberately read-only: commands._other_live_follower
    scans the same *.pane files but COLLECTS the dead ones as it goes, which is
    right for a stop and wrong here — a hook must never garbage-collect another
    window's state as a side effect of failing to find its own.

    Liveness is checked through FollowerState.get, so every candidate costs a
    backend probe (a tmux shell-out, an nvim RPC connect). Callers on the hook
    path must throttle this, never run it per edit."""
    others = []
    for path in sorted(cache.CACHE_DIR.glob("*.pane")):
        key = path.stem
        if key == window_id:
            continue
        state = FollowerState.get(key)
        if state is None:
            continue
        others.append(
            OtherFollower(
                window_id=key,
                backend=state.backend,
                target=state.target,
                current_file=state.current_file,
            )
        )
    return others


def _standalone_id(env: dict[str, str]) -> str:
    term = env.get("TERM_SESSION_ID")
    if term:
        return f"term-{term}"
    iterm = env.get("ITERM_SESSION_ID")
    if iterm:
        return f"iterm-{iterm}"
    try:
        return "tty-" + Path(os.ttyname(0)).name
    except OSError:
        return "standalone-default"


# The hook's own chain to its pane's shell, measured on this machine
# (2026-09-22) by walking `ps -o ppid=` from a python process started the way
# the hook is: python3 -> the shell the harness runs the hook command in ->
# claude -> the pane's shell. Three hops; four when an extra launcher sits in
# between (under `uv run`, the shape the test suite and manual probes see).
# Eight is a little over twice the deepest measured healthy chain: enough
# headroom for one more launcher without turning the walk into a scan.
_MAX_ANCESTOR_HOPS = 8


def _tmux_panes_by_pid() -> dict[int, tuple[str, str]]:
    """pane_pid -> (pane_id, window_id) for every pane on every session of the
    tmux server this environment points at, or {} when there is no answer.

    Shells out from here rather than through tmux.TmuxPane so that faking this
    boundary in a test cannot disturb the many tests that fake
    tmux.subprocess.run, and so that adding the fallback needed no edit to a
    module other modules depend on.

    Costs one `tmux list-panes -a`: median 8.6 ms against a throwaway 20-pane
    server, measured 2026-09-22 on this machine. Never raises — a missing tmux
    binary (OSError) and a server that is not running (returncode 1, or a
    server that answers nothing) are both just "no panes"."""
    try:
        result = subprocess.run(
            ["tmux", "list-panes", "-a", "-F", "#{pane_pid} #{pane_id} #{window_id}"],
            capture_output=True,
            text=True,
            check=False,
        )
    except OSError:
        return {}
    if result.returncode != 0:
        return {}
    panes: dict[int, tuple[str, str]] = {}
    for line in result.stdout.splitlines():
        parts = line.split()
        if len(parts) != 3 or not parts[0].isdigit():
            continue
        panes[int(parts[0])] = (parts[1], parts[2])
    return panes


def _parent_pid(pid: int) -> int | None:
    """pid's parent, or None when it cannot be read.

    `ps` answers exit 1 with empty stdout for a pid that has gone away, which
    is the ordinary outcome when an ancestor exits between two hops — ancestry
    is a snapshot, so that is "stop walking", not an error.

    Median 3.8 ms per call, measured 2026-09-22 on this machine. The obvious
    alternative, reading the whole table once with `ps -eo pid=,ppid=` and
    walking it in memory, was measured at 25.7 ms for 925 processes — flat,
    but worse than the per-hop call for every chain of seven hops or fewer,
    and the healthy chain here is three or four. At the cap the two are a
    wash (7 x 3.8 = 26.6 ms), so the per-hop call wins on the cases that
    happen."""
    try:
        result = subprocess.run(
            ["ps", "-o", "ppid=", "-p", str(pid)],
            capture_output=True,
            text=True,
            check=False,
        )
    except OSError:
        return None
    value = result.stdout.strip()
    return int(value) if value.isdigit() else None


def _recover_window_from_ancestry() -> tuple[str, str] | None:
    """(pane_id, window_id) of the nearest tmux pane this process is running
    inside, or None.

    This is a causal match, not a guess: an ancestor pid that IS a pane's pid
    means the process really did start inside that pane. Deliberately nothing
    weaker — never `display-message -p '#{window_id}'` (that answers for the
    window the attached client is *looking at*, which has already picked the
    wrong window here), and never "the only live follower".

    Nearest ancestor wins, which matters because a pane's shell can itself be
    a descendant of another pane (a tmux started from inside a tmux); the
    innermost is the one the process is actually sitting in.

    pid reuse is not a real risk here. It bites when a *remembered* pid is
    compared against the present; both sides of this comparison are live and
    read ~10 ms apart, and a live ancestor's pid is by definition not
    available for reuse. The only stale side is the pane list, and a pid the
    OS recycled after that snapshot cannot be one of this process's ancestors,
    since they all predate it.

    Cost, timed end to end on this machine 2026-09-22 against a live 39-pane
    server: 19.1 ms median (18.2 min, 33.2 max) for a real four-hop match —
    one `tmux list-panes -a` plus one `ps` per hop except the last. The worst
    case, no match and a chain at or past the cap, is ~29 ms (a 19.9 ms median
    walk of seven `ps` calls, plus the tmux call). That worst case is only ever
    paid when TMUX_PANE is already missing — the case whose current behaviour
    is that the hook silently does nothing at all. tmux is asked first, so a
    machine with no tmux server pays one failed exec and no `ps` calls.

    What this does NOT recover: the Claude Code harness re-parenting execution
    to a `claude --bg-pty-host` daemon. That chain is daemon -> claude daemon
    -> launchd and reaches no pane at all (measured 2026-09-22), and the
    daemon predates the tmux server. This closes the narrower hole where
    TMUX_PANE was stripped but the process is still a pane's descendant."""
    panes = _tmux_panes_by_pid()
    if not panes:
        return None
    # A while rather than `for _ in range(cap)`: the cap-th ancestor is
    # examined but never walked past, so a for loop's exhaustion arc would be
    # unreachable (it costs a `ps` call whose answer is always discarded).
    pid: int | None = os.getppid()
    examined = 0
    while pid is not None and pid > 1:
        found = panes.get(pid)
        if found is not None:
            return found
        examined += 1
        if examined == _MAX_ANCESTOR_HOPS:
            break
        pid = _parent_pid(pid)
    return None


def resolve_session(env: dict[str, str]) -> Session | None:
    if env.get("TMUX_PANE"):
        window = TmuxWindow.from_env(env)
        if window is None:
            return None
        return Session(window_id=window.window_id, origin=env["TMUX_PANE"], in_tmux=True)
    recovered = _recover_window_from_ancestry()
    if recovered is not None:
        pane_id, window_id = recovered
        return Session(window_id=window_id, origin=pane_id, in_tmux=True, recovered_via_pid=True)
    return Session(window_id=_standalone_id(env), origin=None, in_tmux=False)
