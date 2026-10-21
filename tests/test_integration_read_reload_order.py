"""A Read's hook returns only once Vim has re-read the file it shows
(sweep-4 residual of T24c). ensure_showing used to wait for the landing
answer and THEN send the clean-only re-read (s:reload), so the hook returned
while Vim still showed the stale buffer: a file rewritten by `sed -i` before
the Read was shown as it was until Vim got to the next line.

A user BufReadPost autocommand that sleeps 300 ms and then dumps the buffer
makes the order visible: when ensure_showing returns, the dump must already
be there and hold the new content. Real tmux+vim at 49 columns, hermetic;
isolation as in test_integration_relock_autocmd."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import pytest
from test_integration_relock_autocmd import follower, state, vim_pane

pytestmark = pytest.mark.integration


@pytest.mark.parametrize("adopted", [False, True], ids=["dedicated", "adopted"])
def test_the_read_hook_returns_after_the_reload(
    adopted: bool, tmux_session: str, tmp_path: Path, wait_until: Callable[..., bool]
) -> None:
    dump = tmp_path / "dump.txt"
    pane = vim_pane(
        tmux_session,
        tmp_path,
        wait_until,
        f"autocmd BufReadPost * sleep 300m | call writefile(getline(1, '$'), '{dump}')",
    )
    target = tmp_path / "t.py"
    target.write_text("old = 1\n")
    reader = follower(pane, adopted=adopted)
    reader.ensure_showing(str(target))
    # The first Read reads twice (the drop, then the re-read): a typed
    # round-trip waits for both before the dump is cleared.
    assert state(pane, tmp_path, wait_until)[3:] == ["old = 1"]
    assert dump.read_text() == "old = 1\n"

    # What `sed -i` does: a new file renamed over the old one.
    rewritten = tmp_path / "t.py.sed"
    rewritten.write_text("new = 2\n")
    rewritten.replace(target)
    dump.unlink()

    reader.ensure_showing(str(target))

    assert dump.exists(), "the hook returned before Vim re-read the file"
    assert dump.read_text() == "new = 2\n"
