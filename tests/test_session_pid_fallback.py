"""resolve_session's pid-ancestry fallback: when TMUX_PANE was stripped from
the environment but the process is still a descendant of a tmux pane, recover
that pane's window instead of inventing a synthetic term-*/tty-* identity that
no follower is registered under.

Every test here carries @pytest.mark.pid_walk, which switches off conftest's
autouse neutraliser (see the `no_pid_ancestry_walk` fixture) so the code under
test really does reach its subprocess boundary — faked below for the unit
tests, real for the single integration test at the bottom."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import textwrap
from collections.abc import Callable
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

from vim_ai_follower import session as session_mod
from vim_ai_follower.tmux import TmuxWindow

pytestmark = pytest.mark.pid_walk

SYNTHETIC_ENV = {"TERM_SESSION_ID": "w0t0p0"}
SYNTHETIC = session_mod.Session(window_id="term-w0t0p0", origin=None, in_tmux=False)


def make_proc_world(
    parents: dict[int, int],
    panes: dict[int, tuple[str, str]],
    calls: list[list[str]],
    *,
    tmux_returncode: int = 0,
    tmux_raises: bool = False,
    ps_raises: bool = False,
    extra_pane_lines: tuple[str, ...] = (),
) -> Callable[..., MagicMock]:
    """A subprocess.run double covering both boundaries the fallback touches:
    `tmux list-panes -a` (pane_pid -> pane_id window_id) and `ps -o ppid= -p N`
    (pid -> parent pid). A pid missing from `parents` answers the way ps really
    answers for a pid that has gone away: exit 1 and empty stdout. Every
    command issued is appended to `calls`, so a test can assert how much the
    walk cost and not only where it landed."""

    def _run(cmd: list[str], **kwargs: Any) -> MagicMock:
        calls.append(cmd)
        result = MagicMock()
        result.returncode = 0
        if cmd[:2] == ["tmux", "list-panes"]:
            if tmux_raises:
                raise OSError("tmux not found")
            result.returncode = tmux_returncode
            lines = [f"{pid} {pane} {window}" for pid, (pane, window) in panes.items()]
            lines.extend(extra_pane_lines)
            result.stdout = "".join(line + "\n" for line in lines)
        elif cmd[:2] == ["ps", "-o"]:
            if ps_raises:
                raise OSError("ps not found")
            pid = int(cmd[-1])
            parent = parents.get(pid)
            if parent is None:
                result.returncode = 1
                result.stdout = ""
            else:
                result.stdout = f"  {parent}\n"
        else:  # pragma: no cover - the fallback issues no other command
            raise AssertionError(f"unexpected command {cmd}")
        return result

    return _run


def install(
    monkeypatch: pytest.MonkeyPatch,
    ppid: int,
    parents: dict[int, int],
    panes: dict[int, tuple[str, str]],
    **kwargs: Any,
) -> list[list[str]]:
    """Returns the list the fake records every issued command into."""
    calls: list[list[str]] = []
    monkeypatch.setattr(os, "getppid", lambda: ppid)
    monkeypatch.setattr(
        "vim_ai_follower.session.subprocess.run",
        make_proc_world(parents, panes, calls, **kwargs),
    )
    return calls


def test_recovers_the_window_when_the_direct_parent_is_a_pane(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    install(monkeypatch, ppid=500, parents={500: 400}, panes={500: ("%7", "@3")})
    assert session_mod.resolve_session(SYNTHETIC_ENV) == session_mod.Session(
        window_id="@3", origin="%7", in_tmux=True, recovered_via_pid=True
    )


def test_recovers_the_window_from_an_ancestor_three_hops_up(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # python3 (the hook) -> sh -> claude -> the pane's shell: the healthy
    # shape measured on this machine, minus the inherited TMUX_PANE.
    install(
        monkeypatch,
        ppid=500,
        parents={500: 501, 501: 502, 502: 1},
        panes={502: ("%7", "@3"), 9999: ("%8", "@4")},
    )
    session = session_mod.resolve_session(SYNTHETIC_ENV)
    assert session is not None
    assert (session.window_id, session.origin, session.recovered_via_pid) == ("@3", "%7", True)


def test_the_nearest_matching_ancestor_wins(monkeypatch: pytest.MonkeyPatch) -> None:
    """A pane's shell can itself be a descendant of another pane (a nested
    tmux, or a test server spawned from inside a real one). Walking outward
    and returning on the FIRST hit is what keeps the answer the pane the
    process is actually sitting in, not an outer one."""
    install(
        monkeypatch,
        ppid=500,
        parents={500: 501, 501: 502},
        panes={501: ("%inner", "@inner"), 502: ("%outer", "@outer")},
    )
    session = session_mod.resolve_session(SYNTHETIC_ENV)
    assert session is not None
    assert (session.window_id, session.origin) == ("@inner", "%inner")


def test_no_matching_ancestor_keeps_the_synthetic_identity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    install(
        monkeypatch,
        ppid=500,
        parents={500: 501, 501: 1},
        panes={9998: ("%7", "@3")},
    )
    assert session_mod.resolve_session(SYNTHETIC_ENV) == SYNTHETIC


def test_reaching_launchd_ends_the_walk(monkeypatch: pytest.MonkeyPatch) -> None:
    """The pathological bg-pty-host chain (daemon -> claude daemon -> launchd)
    reaches no pane at all; pid 1 is the floor, and pane pids are never <= 1."""
    install(monkeypatch, ppid=500, parents={500: 1}, panes={1: ("%7", "@3")})
    assert session_mod.resolve_session(SYNTHETIC_ENV) == SYNTHETIC


def test_an_ancestor_that_died_mid_walk_ends_the_walk(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # 501 is absent from `parents`, so ps answers exit 1 / empty stdout, the
    # way it answers for a pid that exited between two hops.
    install(monkeypatch, ppid=500, parents={500: 501}, panes={9998: ("%7", "@3")})
    assert session_mod.resolve_session(SYNTHETIC_ENV) == SYNTHETIC


def test_the_depth_cap_is_eight() -> None:
    """Pinned as a literal on purpose. The test below builds its chain FROM
    the constant, which makes it a property test ("the walk stops at the cap")
    that stays green however the cap moves — verified by canary on 2026-09-22,
    where raising the cap to 64 did not make it fail. The number itself is a
    measured decision (a little over twice the deepest healthy chain observed
    on this machine), so changing it has to be deliberate enough to edit a
    test."""
    assert session_mod._MAX_ANCESTOR_HOPS == 8


def test_the_depth_cap_is_respected(monkeypatch: pytest.MonkeyPatch) -> None:
    """A match one hop beyond the cap is not found, and the walk stops paying
    for `ps` there too rather than running on and discarding the answer."""
    cap = session_mod._MAX_ANCESTOR_HOPS
    chain = list(range(500, 500 + cap + 2))
    parents = {pid: pid + 1 for pid in chain[:-1]}
    calls = install(monkeypatch, ppid=chain[0], parents=parents, panes={chain[cap]: ("%7", "@3")})
    assert session_mod.resolve_session(SYNTHETIC_ENV) == SYNTHETIC
    # One tmux call plus one `ps` per hop except the last: the cap-th pid is
    # examined, never walked past.
    ps_calls = [cmd for cmd in calls if cmd[0] == "ps"]
    assert len(ps_calls) == cap - 1
    assert ps_calls[-1] == ["ps", "-o", "ppid=", "-p", str(chain[cap - 2])]

    # ... and the last ancestor still inside the cap IS found, so the first
    # half fails for the cap and not because the walk is broken outright.
    install(monkeypatch, ppid=chain[0], parents=parents, panes={chain[cap - 1]: ("%7", "@3")})
    session = session_mod.resolve_session(SYNTHETIC_ENV)
    assert session is not None
    assert session.window_id == "@3"


def test_tmux_missing_keeps_the_synthetic_identity(monkeypatch: pytest.MonkeyPatch) -> None:
    install(monkeypatch, ppid=500, parents={500: 400}, panes={500: ("%7", "@3")}, tmux_raises=True)
    assert session_mod.resolve_session(SYNTHETIC_ENV) == SYNTHETIC


def test_tmux_failing_keeps_the_synthetic_identity(monkeypatch: pytest.MonkeyPatch) -> None:
    install(monkeypatch, ppid=500, parents={500: 400}, panes={500: ("%7", "@3")}, tmux_returncode=1)
    assert session_mod.resolve_session(SYNTHETIC_ENV) == SYNTHETIC


def test_no_tmux_panes_at_all_keeps_the_synthetic_identity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    install(monkeypatch, ppid=500, parents={500: 400}, panes={})
    assert session_mod.resolve_session(SYNTHETIC_ENV) == SYNTHETIC


def test_ps_missing_keeps_the_synthetic_identity(monkeypatch: pytest.MonkeyPatch) -> None:
    install(monkeypatch, ppid=500, parents={500: 501}, panes={501: ("%7", "@3")}, ps_raises=True)
    assert session_mod.resolve_session(SYNTHETIC_ENV) == SYNTHETIC


def test_unparsable_pane_lines_are_skipped(monkeypatch: pytest.MonkeyPatch) -> None:
    """tmux can print a short line (a pane whose format field is empty) and
    prints `#{pane_pid}` as the literal string on versions that do not know
    the field. Neither may take the whole lookup down."""
    install(
        monkeypatch,
        ppid=500,
        parents={500: 400},
        panes={500: ("%7", "@3")},
        extra_pane_lines=("", "nope %9 @9", "123 %9"),
    )
    session = session_mod.resolve_session(SYNTHETIC_ENV)
    assert session is not None
    assert session.window_id == "@3"


def test_tmux_pane_in_the_environment_never_walks_the_ancestry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The healthy path must not pay for the fallback: with TMUX_PANE present
    neither `ps` nor `tmux list-panes -a` is issued at all (the one tmux call
    made is TmuxWindow.from_env's display-message, on the other module)."""
    calls: list[list[str]] = []

    def _boom(cmd: list[str], **kwargs: Any) -> MagicMock:
        calls.append(cmd)
        raise AssertionError(f"the fallback ran: {cmd}")

    monkeypatch.setattr("vim_ai_follower.session.subprocess.run", _boom)
    monkeypatch.setattr(
        TmuxWindow,
        "from_env",
        classmethod(lambda cls, env: TmuxWindow(window_id="@3")),
    )
    assert session_mod.resolve_session({"TMUX_PANE": "%1"}) == session_mod.Session(
        window_id="@3", origin="%1", in_tmux=True
    )
    assert calls == []


def test_an_inherited_session_is_not_marked_as_recovered(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        TmuxWindow,
        "from_env",
        classmethod(lambda cls, env: TmuxWindow(window_id="@3")),
    )
    session = session_mod.resolve_session({"TMUX_PANE": "%1"})
    assert session is not None
    assert session.recovered_via_pid is False


_PROBE = textwrap.dedent(
    """
    import json, os, sys
    sys.path.insert(0, sys.argv[1])
    from vim_ai_follower.session import resolve_session
    s = resolve_session({"TERM_SESSION_ID": "w0t0p0"})
    print(json.dumps({
        "window_id": s.window_id,
        "origin": s.origin,
        "in_tmux": s.in_tmux,
        "recovered_via_pid": s.recovered_via_pid,
        "saw_tmux_pane": "TMUX_PANE" in os.environ,
        "saw_tmux": "TMUX" in os.environ,
        "tmux_tmpdir": os.environ.get("TMUX_TMPDIR"),
    }))
    """
)


@pytest.mark.integration
def test_real_pane_is_recovered_without_tmux_pane_in_the_environment(
    tmux_session: str, tmp_path: Path, wait_until: Callable[..., bool]
) -> None:
    """The only test that proves the feature against reality: a real process
    inside a real tmux pane, with TMUX_PANE (and TMUX) scrubbed from its
    environment, still resolves to that pane's window.

    TMUX is scrubbed too, matching the harness bug's measured shape, which
    means the probe's bare `tmux` has to be pointed at the throwaway server
    by TMUX_TMPDIR alone — the conftest fixture's isolation mechanism. Two
    shell hops (bash -c '...; true' does not exec-optimise) put the pane's
    shell at depth 2, so this exercises a multi-hop walk and not just a
    direct-parent hit."""
    listed = subprocess.run(
        ["tmux", "list-panes", "-t", tmux_session, "-F", "#{pane_id} #{window_id}"],
        capture_output=True,
        text=True,
        check=True,
    )
    pane_id, window_id = listed.stdout.split()[:2]

    src = str(Path(session_mod.__file__).parent.parent)
    probe = tmp_path / "probe.py"
    probe.write_text(_PROBE)
    out = tmp_path / "out.json"
    inner = f"{sys.executable} {probe} {src} > {out} 2>&1; true"
    command = f"env -u TMUX_PANE -u TMUX TMUX_TMPDIR={os.environ['TMUX_TMPDIR']} bash -c {inner!r}"
    subprocess.run(["tmux", "send-keys", "-t", pane_id, command, "Enter"], check=True)

    assert wait_until(lambda: out.exists() and out.read_text().strip() != "", timeout=20.0), (
        "the probe never wrote its result"
    )
    payload = out.read_text()
    resolved = json.loads(payload)
    assert resolved["saw_tmux_pane"] is False, payload
    assert resolved["saw_tmux"] is False, payload
    assert resolved == {
        "window_id": window_id,
        "origin": pane_id,
        "in_tmux": True,
        "recovered_via_pid": True,
        "saw_tmux_pane": False,
        "saw_tmux": False,
        "tmux_tmpdir": os.environ["TMUX_TMPDIR"],
    }
