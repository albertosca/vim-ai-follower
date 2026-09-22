"""The stale_files field: its on-disk default, and the subset invariant.

stale_files names tabs whose buffer no longer matches disk. It is kept apart
from open_files because open_files doubles as the eviction candidate list --
see FollowerState.mark_stale for the bug that conflating them caused. These
tests pin the two properties the rest of the codebase relies on without
restating: an old .pane file still reads, and nothing can persist a stale
entry for a tab that is not open.
"""

from __future__ import annotations

import json
from pathlib import Path

from vim_ai_follower.state import FollowerState


def test_a_pane_file_written_before_stale_tracking_still_reads(tmp_path: Path) -> None:
    """Upgrading in place must not crash on the state already on disk: a
    live follower's .pane file predates this field by definition."""
    (tmp_path / "@1.pane").write_text(
        json.dumps({"backend": "tmux", "target": "%2", "open_files": ["/tmp/a.py"]})
    )
    loaded = FollowerState.read("@1", base_dir=tmp_path)
    assert loaded is not None
    assert loaded.open_files == ("/tmp/a.py",)
    assert loaded.stale_files == ()


def test_set_refuses_to_persist_a_stale_entry_for_an_unopened_file(tmp_path: Path) -> None:
    """The invariant is enforced at the single write chokepoint, not at each
    caller, because a caller that has never heard of stale_files can still
    invalidate one: cmd_toggle's un-mute zeroes open_files to force a resync
    and would otherwise leave the marks behind, naming tabs nothing tracks.
    Reproduced here in that exact shape (update with open_files=())."""
    FollowerState.set("@1", "tmux", "%2", open_files=("/tmp/a.py", "/tmp/b.py"), base_dir=tmp_path)
    FollowerState.update("@1", base_dir=tmp_path, stale_files=("/tmp/a.py",))

    FollowerState.update("@1", base_dir=tmp_path, enabled=True, open_files=())

    loaded = FollowerState.read("@1", base_dir=tmp_path)
    assert loaded is not None
    assert loaded.open_files == ()
    assert loaded.stale_files == ()


def test_mark_and_clear_stale_are_no_ops_without_state(tmp_path: Path) -> None:
    """The .animating marker and the .pane file have decoupled lifecycles, so
    a hook can reach either call after a stop deleted the state under it."""
    FollowerState.mark_stale("@nosuch", "/tmp/a.py", base_dir=tmp_path)
    FollowerState.clear_stale("@nosuch", "/tmp/a.py", base_dir=tmp_path)
    assert FollowerState.read("@nosuch", base_dir=tmp_path) is None


def test_clear_stale_leaves_the_other_marks_alone(tmp_path: Path) -> None:
    FollowerState.set(
        "@1",
        "tmux",
        "%2",
        open_files=("/tmp/a.py", "/tmp/b.py"),
        stale_files=("/tmp/a.py", "/tmp/b.py"),
        base_dir=tmp_path,
    )
    FollowerState.clear_stale("@1", "/tmp/a.py", base_dir=tmp_path)
    loaded = FollowerState.read("@1", base_dir=tmp_path)
    assert loaded is not None
    assert loaded.stale_files == ("/tmp/b.py",)
