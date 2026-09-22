"""Real tmux+vim proof that a lost animation-slot race never orphans a tab.

Sibling of test_integration_tab_eviction.py, which proved the same property
against a `close_tab` that missed. This one attacks the OTHER half: the
bookkeeping itself. When `try_acquire_animating` loses, the post-edit hook has
to say "this buffer is now out of sync" and its only lever used to be dropping
the file from `open_files` — the very tuple eviction picks victims from. A file
that already HAD a tab therefore lost its only route back to being an eviction
candidate: the tab stayed in Vim forever while `open_files` sat pinned at
`max_tabs`, one tab further behind per collision.

Measured live on 2026-09-22 on Alberto's own follower: nine tabs in Vim against
`max_tabs=5` and exactly five `open_files`, in a window whose state recorded
three concurrent writers.

The probe is imported from test_integration_tab_eviction rather than
reimplemented: the tab count has to come out of Vim itself (`capture-pane`
renders a scrolled, abbreviated tabline and cannot answer "how many tabs"), and
that module's `writefile()` round trip already deletes the dump first and waits
for a `TABS=` prefix, so a stale read can never pass for an answer.
"""

from __future__ import annotations

import io
import json
from collections.abc import Callable
from pathlib import Path

import pytest
from test_integration_tab_eviction import (
    _start_follower,
    _vim_state,
    _window_id,
    _write_through_the_hook,
)

from vim_ai_follower import cli, config, control
from vim_ai_follower.state import FollowerState

pytestmark = pytest.mark.integration


def _write_through_a_busy_slot(
    path: Path, body: str, window_id: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """One production Write hook that LOSES the animation-slot race.

    The marker carries this process's own live pid, which is exactly what a
    parallel hook mid-animation leaves behind, so `try_acquire_animating`
    refuses and the hook takes its skip branch — no keystrokes, no tab work.
    Cleared in a finally so the collision is scoped to this one edit."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body)
    payload = json.dumps({"tool_name": "Write", "tool_input": {"file_path": str(path)}})
    monkeypatch.setattr("sys.stdin", io.StringIO(payload))
    control.mark_animating(window_id)
    try:
        assert cli.main(["hook", "post"]) == 0
    finally:
        control.clear_animating(window_id)


def test_a_busy_slot_collision_never_strands_a_tab_outside_eviction(
    tmux_session: str,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    wait_until: Callable[..., bool],
) -> None:
    """Three files animated, then a collision on the FIRST of them (the case
    that matters: it already owns a tab), then enough further files to push
    every survivor past `max_tabs`.

    Vim must settle at `max_tabs`. Before the fix it settles at `max_tabs + 1`,
    because the collided file left `open_files` while its tab stayed open and
    no later eviction could ever name it."""
    follower_pane = _start_follower(tmux_session, monkeypatch, wait_until)
    window_id = _window_id(tmux_session)
    max_tabs = config.load().max_tabs
    dump = tmp_path / "dump.txt"

    root = tmp_path / "src"
    paths = [root / f"mod{i}.py" for i in range(max_tabs + 4)]
    collided = paths[0]

    for i, path in enumerate(paths[:3]):
        _write_through_the_hook(path, f"value = {i}\n", monkeypatch)
    before = FollowerState.read(window_id)
    assert before is not None
    assert len(before.open_files) == 3, (
        "setup never got three tracked tabs open — nothing is being measured"
    )

    _write_through_a_busy_slot(collided, "value = 99\n", window_id, monkeypatch)

    for i, path in enumerate(paths[3:], start=3):
        _write_through_the_hook(path, f"value = {i}\n", monkeypatch)

    tab_count, buffers = _vim_state(follower_pane, dump, wait_until)
    assert tab_count == max_tabs, (
        f"max_tabs is {max_tabs} but Vim holds {tab_count} tabs after {len(paths)} files "
        f"with one busy-slot collision — a skipped edit stranded a tab outside eviction. "
        f"Buffers: {buffers}"
    )
    assert str(collided) not in buffers, (
        f"{collided} lost the slot race and its tab was never evictable again"
    )

    state = FollowerState.read(window_id)
    assert state is not None
    assert sorted(state.open_files) == sorted(buffers)
    assert sorted(state.open_files) == sorted(str(p) for p in paths[-max_tabs:])
