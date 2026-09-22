"""The session->window binding, proved against a REAL tmux server.

Every other test of this feature (`test_binding.py`, `test_hook_session_binding.py`)
fakes `subprocess` or the `binding` module itself, so none of them can tell
whether the pieces fit together in a live process: the real CLI, a real
follower, a real tmux server whose pid `binding` reads back, and a hook whose
environment really has lost `TMUX_PANE`. That is what these two tests are for.

**The false pass this file is shaped to avoid.** `resolve_session` has a
second recovery path — `_recover_window_from_ancestry`, which walks this
process's parents against `tmux list-panes -a`. `conftest.py`'s autouse
`no_pid_ancestry_walk` switches it off in-process, and that fixture CANNOT
reach a hook run as a subprocess, which is exactly what these tests run. So a
blind hook fired from inside a pane of the private tmux server would be
recovered by the PID WALK, the animation would land, and the binding would
never be consulted — a green test measuring the wrong mechanism.

The shape that avoids it: the blind hook is fired from the pytest process,
which is a descendant of one of the developer's REAL panes. Those panes belong
to his own tmux server, and every call here carries the harness's private
`TMUX_TMPDIR` with `$TMUX` unset, so `tmux list-panes -a` answers with the
private server's panes only. None of them is an ancestor of pytest, the walk
finds no match, `resolve_session` falls through to the synthetic identity, and
the binding is the only thing left that can supply a window.

That reasoning is asserted, not trusted: the verdict is the hook log's
recovery line, which names the window AND the session id and is only ever
written from the `not in_tmux` branch. A buffer assertion alone cannot tell
which mechanism supplied the window.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import pytest
from e2e_harness import E2EFollower, _checked, _session_env_flags, e2e_world

pytestmark = pytest.mark.integration

# Fixed, and the same in both halves of each test: the binding is keyed on it.
SESSION_ID = "binding-e2e-session"

# Substrings of the two log lines that are the verdicts here. Both come from
# hooks.py; a rewording there should break these tests, because the log line
# IS the observable contract for "which mechanism supplied the window".
BINDING_RECOVERED = "from a stored session->window binding"
LOST_IDENTITY = "this session most likely lost its tmux window identity"


def _text(*lines: str) -> str:
    return "".join(line + "\n" for line in lines)


FIRST = _text("def alpha():", "    return 'FIRST_MARK'")
SECOND = _text("def alpha():", "    return 'SECOND_MARK'")


@pytest.fixture
def world() -> Iterator[E2EFollower]:
    with e2e_world() as live:
        yield live


def _hook_payload(file_path: Path) -> str:
    """A Write payload carrying a CHOSEN top-level `session_id`.

    `e2e_harness.payload` hard-codes `"e2e"`, which is fine for every other
    e2e test and useless here — the session id is the key the whole feature
    turns on, so it has to be explicit and readable in the log line."""
    return json.dumps(
        {
            "tool_name": "Write",
            "tool_input": {"file_path": str(file_path)},
            "session_id": SESSION_ID,
        }
    )


@contextmanager
def _blind(world: E2EFollower) -> Iterator[None]:
    """Run the CLI the way the harness bug leaves it: no `TMUX_PANE`.

    Mutates the harness's env dict rather than passing an override, because
    `E2EFollower.cli` is the thing under test here (the real `bin/claude-follow`
    wrapper) and the harness is read-only for this task. Restored in a finally
    so a failure inside the block cannot leave the world half-blinded."""
    pane = world.env.pop("TMUX_PANE")
    try:
        yield
    finally:
        world.env["TMUX_PANE"] = pane


def _hook_log(world: E2EFollower) -> str:
    path = world.cache_dir / "hook.log"
    return path.read_text(errors="replace") if path.exists() else ""


def _binding_file(world: E2EFollower) -> Path:
    return world.cache_dir / "bindings" / f"{SESSION_ID}.json"


def _edit(world: E2EFollower, target: Path, content: str) -> None:
    """One PreToolUse/PostToolUse pair through the real CLI, in the
    FOREGROUND: `hook post` animates synchronously, so when it returns the
    buffer is final and can be read without racing an animation. Order
    matters — `hook pre` snapshots the file as it is BEFORE the write."""
    world.cli("hook", "pre", stdin=_hook_payload(target))
    target.write_text(content)
    world.cli("hook", "post", stdin=_hook_payload(target))


def _start_vim_follower(world: E2EFollower) -> str:
    """Register a tmux/Vim follower and wait until Vim is really the pane's
    running command.

    The tmux backend rather than nvim on purpose: the guard test restarts the
    tmux server, and nvim's RPC socket path is derived from HOME and the
    window id, so the second follower would have to reuse a socket path the
    first one left behind. A pane id has no such afterlife.

    `split_from` returns as soon as tmux has created the pane, which is before
    `vim` has exec'd in it — send-keys landing in the shell instead would look
    like a follower that ignored the animation."""
    world.start("tmux", "instant")
    pane = world.follower_target()
    world.wait_until(
        lambda: (
            world.tmux(
                "display-message", "-p", "-t", pane, "#{pane_current_command}", check=False
            ).stdout.strip()
            == "vim"
        ),
        f"vim to become the running command in {pane}",
        timeout=30.0,
    )
    return pane


def _private_server_pid(world: E2EFollower) -> int | None:
    result = world.tmux("display-message", "-p", "#{pid}", check=False)
    value = result.stdout.strip()
    return int(value) if result.returncode == 0 and value.isdigit() else None


def _restart_private_tmux_server(world: E2EFollower) -> None:
    """Kill the private tmux server and build a fresh one on the SAME private
    socket directory, then re-point the world at its new origin pane.

    Every tmux call goes through `E2EFollower.tmux`, whose env has `$TMUX`
    UNSET and `TMUX_TMPDIR` pointing at this world's private directory. That
    ordering is the whole safety story: `$TMUX` takes priority over
    `TMUX_TMPDIR`, so a `kill-server` issued with it set would reach the
    developer's own server instead (it did, on 2026-09-22, and took a
    20-window session with it).

    Asserts both halves of the premise it exists to create — a NEW server pid,
    and the SAME window id handed out again. Measured on this machine
    2026-09-22: a fresh server numbers windows from @0, so the binding's `@0`
    now names a different window on a different server. Without the
    server-pid guard in `binding.recall`, that is a wrong-window animation."""
    old_pid = _private_server_pid(world)
    assert old_pid is not None, "no private tmux server to restart"
    world.tmux("kill-server", check=False)
    world.wait_until(
        lambda: _private_server_pid(world) is None,
        "the private tmux server to go away",
        timeout=15.0,
    )
    _checked(
        [
            "tmux",
            "new-session",
            "-d",
            "-s",
            world.session,
            "-x",
            "120",
            "-y",
            "40",
            *_session_env_flags(world.env),
            "sh",
        ],
        world.env,
    )
    new_pid = _private_server_pid(world)
    assert new_pid is not None and new_pid != old_pid, (
        f"the restarted server reports pid {new_pid}, the old one was {old_pid} — "
        "this test is only meaningful across a genuinely new server"
    )
    origin = _checked(
        ["tmux", "list-panes", "-t", world.session, "-F", "#{pane_id}"], world.env
    ).stdout.split()[0]
    window = _checked(
        ["tmux", "display-message", "-p", "-t", origin, "#{window_id}"], world.env
    ).stdout.strip()
    assert window == world.window_id, (
        f"the restarted server handed out window {window}, not {world.window_id} — "
        "the wrong-window collision this test guards against did not occur, so a "
        "pass here would prove nothing"
    )
    world.origin_pane = origin
    world.env["TMUX_PANE"] = origin


def test_a_hook_that_lost_tmux_pane_animates_into_the_window_it_bound_earlier(
    world: E2EFollower,
) -> None:
    """Task 3 Step 1 — the round trip, end to end through the real CLI.

    First edit: `TMUX_PANE` present, so the hook proves its window and
    `binding.remember` records it. Second edit: same `session_id`, no
    `TMUX_PANE`, fired from the pytest process so the pid-ancestry walk finds
    nothing (see the module docstring). The content still lands in the same
    Vim, and the log says the binding is why.
    """
    pane = _start_vim_follower(world)
    target = world.workdir / "bound.py"

    _edit(world, target, FIRST)
    assert b"FIRST_MARK" in world.vim_buffer_bytes(pane), (
        "the healthy half never animated, so the blind half below would prove nothing"
    )
    stored = json.loads(_binding_file(world).read_text())
    assert stored["window_id"] == world.window_id
    assert stored["tmux_server_pid"] == _private_server_pid(world)

    with _blind(world):
        _edit(world, target, SECOND)

    assert b"SECOND_MARK" in world.vim_buffer_bytes(pane), (
        "the blind edit never reached the follower's buffer"
    )
    recoveries = [line for line in _hook_log(world).splitlines() if BINDING_RECOVERED in line]
    assert len(recoveries) >= 1, (
        "nothing in the hook log says a binding supplied the window — the animation "
        f"above was driven by some OTHER mechanism (the pid-ancestry walk is the one "
        f"to suspect). Log:\n{_hook_log(world)}"
    )
    # Only reachable from the `not session.in_tmux` branch, and it names both
    # ends of the mapping: proof that resolve_session gave up and recall did
    # the work, not just that "something animated".
    assert f"recovered window {world.window_id} for session {SESSION_ID}" in recoveries[0]


def test_a_binding_is_never_reused_across_a_tmux_server_restart(world: E2EFollower) -> None:
    """Task 3 Step 2 — the wrong-window guard. This is the test that must
    never be deleted.

    The binding says `@0`. After the server restart there IS a live follower
    at `@0` — a different window, on a different server, most likely someone
    else's project. Everything the recovery checks except the server pid says
    go: the session id matches, the window id matches, the follower is alive.
    Only `binding.recall`'s server-pid comparison stands between the recorded
    window and an animation in the wrong editor.

    Deliberately does NOT assert that the stale binding file is deleted, even
    though `recall` does delete it. That deletion happens inside the recovery
    attempt, so asserting it here would make this test fail whenever Task 2's
    recovery branch is absent — and a guard that goes red when the thing it
    guards is switched off cannot tell "the guard held" from "nothing ran".
    The deletion is `binding.recall`'s own property and is covered in
    `tests/test_binding.py`.
    """
    _start_vim_follower(world)
    target = world.workdir / "bound.py"
    _edit(world, target, FIRST)
    assert _binding_file(world).exists(), "the healthy half never wrote a binding"

    _restart_private_tmux_server(world)
    assert json.loads(_binding_file(world).read_text())["window_id"] == world.window_id, (
        "the stale binding does not name the reassigned window, so it could not "
        "collide with it even without the guard"
    )
    pane = _start_vim_follower(world)

    with _blind(world):
        _edit(world, target, SECOND)

    landed = world.vim_buffer_bytes(pane)
    assert landed.strip() == b"", (
        f"the fresh follower's buffer is not empty — something animated into the "
        f"reassigned window: {landed!r}"
    )
    log = _hook_log(world)
    assert BINDING_RECOVERED not in log, f"a recovery fired across the restart:\n{log}"
    assert LOST_IDENTITY in log, (
        f"the blind hook neither recovered nor warned — it went silently blind, which "
        f"is the behavior the lost-identity warning exists to make visible:\n{log}"
    )
