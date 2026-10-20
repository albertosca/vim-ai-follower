"""A post-pause navigation that fails inside a hook leaves the remainder for
the next hook's catch-up (final review: only unit-tested in animate._resume).

The pause wait discards the pending remainder; the resume's goto then has to
land on the file again. Here the user, during the pause, opens a tab and adds
a `TabEnter` autocmd that bounces every tab switch to the last tab, so the
goto lands "elsewhere" (NavigationFailed). The hook logs it and exits; the
remainder it saved again is what the next Edit's catch-up types.

Real CLI, real tmux, a real dedicated Vim, `HOME` isolated by the e2e
harness."""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from e2e_harness import E2EFollower, e2e_world, payload

from vim_ai_follower.state import FollowerState

pytestmark = pytest.mark.integration


def _text(*lines: str) -> str:
    return "".join(line + "\n" for line in lines)


FRESH = _text(*(f"value_{index:02d} = {index}  # MARK_{index:02d}" for index in range(24)))
NEXT_EDIT = FRESH + "tail = 'CATCHUP OK'\n"


@pytest.fixture
def world() -> Iterator[E2EFollower]:
    with e2e_world() as live:
        yield live


def _keys(world: E2EFollower, pane: str, command: str) -> None:
    world.tmux("send-keys", "-t", pane, "-l", f"{command}\r")


def test_a_failed_post_pause_navigation_leaves_the_remainder_for_the_next_hook(
    world: E2EFollower,
) -> None:
    path = world.workdir / "paused.py"
    world.start("tmux", "lento")
    pane = world.follower_target()
    world.cli("hook", "pre", stdin=payload("Write", path))
    path.write_text(FRESH)
    process = world.cli_background("hook", "post", stdin=payload("Write", path))
    world.wait_for_animating("running")
    world.wait_until(
        lambda: "MARK_02" in world.capture_pane(pane), "the retype under way", timeout=90.0
    )
    world.cli("pause")
    world.wait_for_animating("paused")

    world.tmux("send-keys", "-t", pane, "Escape")
    _keys(world, pane, ":tabnew")
    _keys(world, pane, ":autocmd TabEnter * tablast")
    world.cli("pause")
    world.wait_for_hook_exit(process)

    log = (world.cache_dir / "hook.log").read_text()
    assert "Traceback" not in log, log
    assert "skipped the animation of" in log, log
    remainder = world.pending()
    assert remainder["kind"] == "show_fresh", remainder
    assert remainder["remaining_lines"], remainder
    assert tuple(remainder["remaining_lines"]) == tuple(
        FRESH.splitlines()[-len(remainder["remaining_lines"]) :]
    )

    _keys(world, pane, ":autocmd! TabEnter")
    assert FollowerState.update(world.window_id, base_directory=world.cache_dir, speed="instant")
    world.cli("hook", "pre", stdin=payload("Edit", path))
    path.write_text(NEXT_EDIT)
    post = world.cli("hook", "post", stdin=payload("Edit", path))
    assert post.returncode == 0, post.stderr

    log = (world.cache_dir / "hook.log").read_text()
    assert "Traceback" not in log, log
    assert not world.pending_path.exists(), "the catch-up left its remainder"
    assert world.vim_buffer_bytes(pane) == NEXT_EDIT.encode()
