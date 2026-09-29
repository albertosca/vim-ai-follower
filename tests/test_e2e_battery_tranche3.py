"""End-to-end tranche 3 of the machine-verifiable half of `qa/visual-battery.md`
(checks 1-10): the real `claude-follow` CLI, real hook payloads on stdin, a
real Vim/Neovim follower in an isolated world. Each test names the battery
check it replaces and the commit(s) it guards; what stays in the battery is
the part only a human can judge."""

from __future__ import annotations

import json
import time
from collections.abc import Iterator
from pathlib import Path

import pytest
from e2e_harness import REPO_ROOT, E2EFollower, e2e_world, payload

from vim_ai_follower.commands import VIM_NEEDS_TMUX

pytestmark = pytest.mark.integration


@pytest.fixture
def world() -> Iterator[E2EFollower]:
    with e2e_world() as live:
        yield live


def _write_config(world: E2EFollower, settings: dict[str, str]) -> None:
    path = world.home / ".config" / "claude-vim-follower" / "config.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(settings))


def _assert_private_server(world: E2EFollower) -> None:
    """The tmux every child of this world reaches is the PRIVATE server. The
    checks without TMUX_PANE depend on it twice: session resolution then
    scans `tmux list-panes -a` for an ancestor pane, and on the developer's
    real server that scan could resolve pytest to his live window."""
    socket_path = world.tmux("display-message", "-p", "#{socket_path}").stdout.strip()
    assert Path(socket_path).resolve().is_relative_to(Path(world.tmux_tmpdir).resolve()), (
        f"tmux answered from {socket_path}, not from the private {world.tmux_tmpdir}"
    )


def _all_panes(world: E2EFollower) -> list[str]:
    return world.tmux("list-panes", "-a", "-F", "#{pane_id}").stdout.split()


def _follower_leftovers(world: E2EFollower) -> list[str]:
    cache = world.cache_dir
    if not cache.exists():
        return []
    return sorted(p.name for glob in ("*.pane", "nvim-*.sock") for p in cache.glob(glob))


FIXTURES = REPO_ROOT / "qa" / "fixtures"


def _write_through_hooks(
    world: E2EFollower,
    path: Path,
    content: str,
    identity: dict[str, str] | None = None,
    env: dict[str, str] | None = None,
) -> None:
    """One synchronous Write: pre snapshots the current file, the new content
    lands, post animates the diff — the order a PreToolUse/PostToolUse pair
    fires in."""
    world.cli("hook", "pre", stdin=payload("Write", path, identity=identity), env=env)
    path.write_text(content)
    world.cli("hook", "post", stdin=payload("Write", path, identity=identity), env=env)


def _pane_option(world: E2EFollower, pane: str, name: str) -> str:
    """A pane-LOCAL option's value, "" when unset on that pane."""
    return world.tmux("show-options", "-pv", "-t", pane, name, check=False).stdout.strip()


def _window_option(world: E2EFollower, pane: str, name: str) -> str:
    """A WINDOW-local option's value for the window holding `pane`, "" when unset."""
    return world.tmux("show-options", "-wv", "-t", pane, name, check=False).stdout.strip()


# --------------------------------------------------------------- Checks 1-2

REVIEWER = {"agent_id": "rev1", "agent_type": "code-reviewer"}


def test_second_writer_tints_and_titles_the_follower_border_and_stop_restores_it(
    world: E2EFollower,
) -> None:
    """Battery checks 1 and 2 — the per-writer colour cue and the border
    restore on `stop` (commands.cmd_stop clears the surface BEFORE killing the
    follower pane: pane-border-status is a WINDOW option, and unsetting it
    through the killed pane's id cannot resolve its target).

    Writer 1 (the session alone) leaves the follower border neutral; writer 2,
    a subagent, tints the FOLLOWER pane's border — both border styles, to its
    palette colour by position: writers are [e2e, rev1], so rev1 is
    PALETTE[1], colour78 — turns the window's border-status bar on and titles
    the pane with its agent_type. Read with show-options after each hook has
    exited, since mid-animation the "Writing..." cue owns title and bar.
    Whether colour78 is distinguishable in the user's own theme stays in the
    battery: show-options proves the value, not the pixels."""
    world.start("tmux", "lento")
    follower = world.follower_target()
    path = world.workdir / "cue.py"

    # Writer 1 is watched WHILE it animates, not only after: the completed
    # animation's refresh clears the surface whenever fewer than two writers
    # exist, so a cue wrongly applied to a lone writer would be gone by the
    # time its hook exits — while the user watched it tinted the whole time.
    writer1 = (FIXTURES / "cue-writer1.py").read_text()
    world.cli("hook", "pre", stdin=payload("Write", path))
    path.write_text(writer1)
    proc = world.cli_background("hook", "post", stdin=payload("Write", path))
    styles_while_running: list[str] = []
    while proc.poll() is None:
        if world.animating_state() == "running":
            styles_while_running.append(_pane_option(world, follower, "pane-border-style"))
        time.sleep(0.05)
    world.wait_for_hook_exit(proc)
    assert len(styles_while_running) >= 10, (
        f"only {len(styles_while_running)} samples during the animation: nothing was watched"
    )
    assert set(styles_while_running) == {""}, (
        f"a lone writer tinted the border mid-animation: {set(styles_while_running)}"
    )
    assert world.vim_buffer_bytes(follower) == writer1.encode(), "writer 1 did not animate"
    assert _pane_option(world, follower, "pane-border-style") == ""
    assert _pane_option(world, follower, "pane-active-border-style") == ""
    assert _window_option(world, follower, "pane-border-status") == ""

    _write_through_hooks(world, path, (FIXTURES / "cue-writer2.py").read_text(), REVIEWER)
    assert _pane_option(world, follower, "pane-border-style") == "fg=colour78"
    assert _pane_option(world, follower, "pane-active-border-style") == "fg=colour78"
    assert _window_option(world, follower, "pane-border-status") == "top"
    title = world.tmux("display-message", "-p", "-t", follower, "#{pane_title}").stdout.strip()
    assert title == "code-reviewer"

    # Check 2. The precondition above (status "top") is what makes the
    # restore below measure something.
    world.cli("stop")
    assert _all_panes(world) == [world.origin_pane], "the follower pane survived stop"
    assert _window_option(world, world.origin_pane, "pane-border-status") == ""
    assert _pane_option(world, world.origin_pane, "pane-border-style") == ""
    assert _pane_option(world, world.origin_pane, "pane-active-border-style") == ""


# --------------------------------------------------------------- Check 4


def _vim_listed_buffers(world: E2EFollower, pane: str) -> list[Path]:
    """Every LISTED buffer's name in the Vim in `pane`, resolved. Written by
    Vim itself behind a sentinel first line, so a stale or half-written dump
    cannot pass for an answer; `:w!`-style keys, like vim_buffer_bytes."""
    out = world.workdir / f"bufs-{pane.lstrip('%')}-{time.monotonic_ns()}.txt"
    command = (
        f":call writefile(['BUFS'] + map(getbufinfo({{'buflisted': 1}}), "
        f"'fnamemodify(v:val.name, \":p\")'), '{out}')"
    )
    world.tmux("send-keys", "-t", pane, "Escape", "Escape")
    world.tmux("send-keys", "-t", pane, "-l", "--", command)
    world.tmux("send-keys", "-t", pane, "Enter")
    world.wait_until(
        lambda: out.exists() and out.read_text().startswith("BUFS\n"),
        f"the buffer list dump at {out}",
        timeout=10.0,
    )
    names = out.read_text().splitlines()[1:]
    out.unlink()
    return [Path(name).resolve() for name in names if name]


def test_two_windows_never_bleed_content_into_each_others_follower(
    world: E2EFollower,
) -> None:
    """Battery check 4 — window-scoped identity (the 2026-07-16 incident:
    one window's edits animated into another window's follower). Two windows
    of one tmux server, a follower in each, a distinct file written through
    each window's own hooks (TMUX_PANE is what scopes them). Each follower's
    Vim must list exactly its own file and hold exactly its bytes — a content
    claim, read from the buffers, not from the screen."""
    world.start("tmux", "instant")
    follower0 = world.follower_target()
    pane1 = world.tmux(
        "new-window", "-d", "-t", world.session, "-P", "-F", "#{pane_id}", "sh"
    ).stdout.strip()
    window1 = world.tmux("display-message", "-p", "-t", pane1, "#{window_id}").stdout.strip()
    assert window1 != world.window_id
    env1 = world.env_with(TMUX_PANE=pane1)
    world.cli("start", "--backend", "tmux", "--speed", "instant", env=env1)
    state1 = json.loads((world.cache_dir / f"{window1}.pane").read_text())
    follower1 = state1["target"]
    assert follower1 not in (follower0, pane1, world.origin_pane)

    alpha = world.workdir / "window_alpha.py"
    beta = world.workdir / "window_beta.py"
    alpha_text = (FIXTURES / "window-alpha.py").read_text()
    beta_text = (FIXTURES / "window-beta.py").read_text()
    _write_through_hooks(world, alpha, alpha_text)
    _write_through_hooks(world, beta, beta_text, env=env1)

    assert _vim_listed_buffers(world, follower0) == [alpha.resolve()]
    assert _vim_listed_buffers(world, follower1) == [beta.resolve()]
    assert world.vim_buffer_bytes(follower0) == alpha_text.encode()
    assert world.vim_buffer_bytes(follower1) == beta_text.encode()


# --------------------------------------------------------------- Check 10


def test_tmux_backend_outside_tmux_fails_loudly_and_opens_nothing(world: E2EFollower) -> None:
    """Battery check 10 — guards fe87f5b. `start` with `backend: tmux` from a
    terminal outside tmux: the exact actionable message on STDERR (an error,
    like its sibling "could not open a standalone nvim window"), nothing on
    stdout (so no traceback anywhere), exit 1, and nothing opened — no pane,
    no follower state, no nvim socket.

    Driven through the CONFIG FILE, as the battery does, not a --backend flag.
    Outside tmux means no TMUX_PANE; TMUX_TMPDIR stays private, because the
    pid-ancestry scan that runs without TMUX_PANE asks tmux for every pane."""
    _write_config(world, {"backend": "tmux"})
    _assert_private_server(world)
    outside = world.env_with(drop=("TMUX_PANE",), TERM_SESSION_ID="e2e")
    panes_before = _all_panes(world)

    result = world.cli("start", expect_rc=1, env=outside)

    assert result.stderr == VIM_NEEDS_TMUX + "\n"
    assert result.stdout == ""
    assert _all_panes(world) == panes_before, "a pane opened"
    assert _follower_leftovers(world) == [], "start left follower state behind"
