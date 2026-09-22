"""Real tmux+vim proof that goto_file lands on the file it was given, whatever
characters its path carries.

`:tab drop {file}` takes its argument through Vim's command line: `#` and `%`
expand to the alternate/current file, `$NAME` to an environment variable, a
space splits the path into two arguments, and `*`/`[...]` glob — silently
opening a sibling whenever one matches (measured 2026-09-22: `a#1.py` opened a
buffer named after the alternate file, `a b.py` became two arguments). The
path now reaches Vim as a string literal passed through `fnameescape()`, so
the escaping is Vim's own, not a reimplementation of it.

Each case asserts where Vim actually LANDED (the current buffer's full path,
read back via writefile, never capture-pane) and that navigating back from
another tab reuses the tab instead of opening a duplicate.
"""

from __future__ import annotations

import os
import subprocess
from collections.abc import Callable
from pathlib import Path

import pytest
from test_integration_tab_eviction import _send_key, _send_text, _start_follower

from vim_ai_follower.backends.tmux_vim import TmuxVimFollower

pytestmark = pytest.mark.integration

# (target, a sibling a glob in the target would match, or None)
_CASES = [
    ("a#1.py", None),
    ("p%.py", None),
    ("a b.py", None),
    ("$HOME.py", None),
    ("f*.py", "foo.py"),
    ("app/[slug]/page.tsx", "app/s/page.tsx"),
]


def _landing(
    pane_id: str, out_path: Path, wait_until: Callable[..., bool]
) -> tuple[int, str, list[str]]:
    """(tab count, current buffer's full path, listed buffers' full paths),
    straight out of Vim. Deleted first and waited on a sentinel prefix so a
    stale or half-written answer cannot pass; retried because a loaded
    machine swallows the ESC pair (same protocol as the eviction tests)."""

    def answered() -> bool:
        try:
            return out_path.read_text().startswith("TABS=")
        except OSError:
            return False

    for _ in range(3):
        out_path.unlink(missing_ok=True)
        _send_key(pane_id, "Escape")
        _send_key(pane_id, "Escape")
        _send_text(
            pane_id,
            ":call writefile(['TABS=' . tabpagenr('$'), expand('%:p')]"
            " + map(filter(range(1, bufnr('$')), 'buflisted(v:val)'),"
            f" 'fnamemodify(bufname(v:val), \":p\")'), '{out_path}')",
        )
        _send_key(pane_id, "Enter")
        if wait_until(answered, timeout=10.0):
            break
    else:
        pane = subprocess.run(
            ["tmux", "capture-pane", "-t", pane_id, "-p"],
            capture_output=True,
            text=True,
            check=False,
        ).stdout
        raise AssertionError(f"Vim never wrote the landing dump. Pane:\n{pane}")
    lines = out_path.read_text().splitlines()
    return int(lines[0].removeprefix("TABS=")), lines[1], lines[2:]


def _make(root: Path, rel: str) -> Path:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"{rel}\n")
    return path


@pytest.mark.parametrize(("target_rel", "sibling_rel"), _CASES)
def test_goto_file_lands_on_the_exact_path_and_reuses_its_tab(
    tmux_session: str,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    wait_until: Callable[..., bool],
    target_rel: str,
    sibling_rel: str | None,
) -> None:
    follower_pane = _start_follower(tmux_session, monkeypatch, wait_until)
    follower = TmuxVimFollower(pane_id=follower_pane)
    dump = tmp_path / "dump.txt"
    other = _make(tmp_path, sibling_rel or "other.py")
    target = _make(tmp_path, target_rel)
    want = os.path.realpath(target)

    follower.ensure_showing(str(other))
    follower.ensure_showing(str(target))
    tabs, current, buffers = _landing(follower_pane, dump, wait_until)
    assert os.path.realpath(current) == want, f"landed on {current!r}, buffers {buffers}"
    assert [b for b in buffers if os.path.realpath(b) == want] == [current]

    follower.ensure_showing(str(other))
    follower.ensure_showing(str(target))
    tabs_again, current_again, _ = _landing(follower_pane, dump, wait_until)
    assert os.path.realpath(current_again) == want
    assert tabs_again == tabs, "navigating back opened a duplicate tab"
