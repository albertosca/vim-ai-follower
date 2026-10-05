"""Real tmux+vim proof that a navigation which fails never lets the lines
after it act on whatever buffer happens to be current.

The trigger found in review (2026-09-29): the tmux backend names its cache
files `~/…` once the pane's Vim was found to share the hook's HOME, and that
answer is keyed on the pane's process. In an adopted pane that process is the
user's SHELL, so a Vim restarted there with another HOME (`HOME=… vim`)
inherited the stale "yes": every handle read failed (E484, a prompt printing
the other HOME's cache path), goto_file navigated nowhere, and the lines after
it ran on the user's own buffer — a Read's lock (MOD=1, nomodifiable,
readonly), and with dedicated semantics an Edit typed into it and the relock's
`:e!` destroyed the unsaved text.

Here the user's Vim runs in a plain shell pane, first on the hook's HOME (so
the "yes" is recorded), then restarted on another HOME with unsaved text in
`user.txt`. Each follower entry point must leave that buffer exactly as it was
(modified, content intact, modifiable, not readonly, on disk untouched), leave
no prompt and no E484, and the NEXT call must ask again and land on the target.
"""

from __future__ import annotations

import contextlib
import os
import subprocess
from collections.abc import Callable
from pathlib import Path

import pytest

from vim_ai_follower import cache
from vim_ai_follower.backends.tmux_vim import TmuxVimFollower
from vim_ai_follower.diff import compute_edit_script
from vim_ai_follower.state import FollowerState

pytestmark = pytest.mark.integration

BEFORE = "a = 1\nb = 2\n"
AFTER = "a = 1\nc = 3\nb = 2\n"
USER_TEXT = "user line\nUSER UNSAVED WORK"


def _tmux(*args: str) -> str:
    return subprocess.run(
        ["tmux", *args], capture_output=True, text=True, check=True
    ).stdout.strip()


def _keys(pane: str, *keys: str) -> None:
    subprocess.run(["tmux", "send-keys", "-t", pane, *keys], check=True)


def _state(pane: str, out: Path, wait_until: Callable[..., bool]) -> list[str]:
    """The current buffer's name, flags, lines, then `:messages` — straight
    out of Vim, deleted first and waited on a sentinel."""
    out.unlink(missing_ok=True)
    _keys(pane, "Escape", "Escape")
    _keys(
        pane,
        "-l",
        "--",
        ":call writefile(['BUF=' . bufname('%'), 'MOD=' . &modified,"
        " 'MA=' . &modifiable, 'RO=' . &readonly, 'SWAP=' . &swapfile, 'TABS=' . tabpagenr('$')]"
        " + getline(1, '$') + ['MESSAGES']"
        f" + split(execute('messages'), \"\\n\") + ['END'], '{out}')",
    )
    _keys(pane, "Enter")

    def answered() -> bool:
        return out.exists() and out.read_text().endswith("END\n")

    assert wait_until(answered, timeout=10.0), _tmux("capture-pane", "-p", "-t", pane)
    return out.read_text().splitlines()[:-1]


@pytest.mark.parametrize(
    "entry",
    [
        "ensure_showing-adopted",
        "apply_edit-dedicated",
        "show_fresh-dedicated",
        "show_fresh-adopted",
    ],
)
def test_a_failed_navigation_never_touches_the_users_buffer(
    tmux_session: str,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    wait_until: Callable[..., bool],
    entry: str,
) -> None:
    operation, semantics = entry.split("-")
    monkeypatch.delenv("TMUX_PANE", raising=False)
    assert "TMUX" not in os.environ
    socket = _tmux("display-message", "-p", "#{socket_path}")
    assert socket.startswith(os.path.realpath(os.environ["TMUX_TMPDIR"]) + "/"), socket
    root = Path(os.path.realpath(tmp_path))
    hook_home, other_home, proj = root / "hook-home", root / "other-home", root / "proj"
    for directory in (hook_home, other_home, proj):
        directory.mkdir()
    monkeypatch.setenv("HOME", str(hook_home))
    monkeypatch.setattr(cache, "CACHE_DIR", hook_home / ".cache" / "claude-vim-follower")
    target = proj / "target.py"
    target.write_text(BEFORE)
    user = proj / "user.txt"
    user.write_text("user line\n")

    # The user's shell, on the hook's HOME, at the follower pane's width.
    pane = _tmux(
        "split-window", "-h", "-t", tmux_session, "-c", str(proj), "-P", "-F", "#{pane_id}",
        "env", "-i", f"HOME={hook_home}", f"PATH={os.environ['PATH']}", "TERM=xterm-256color",
        "bash", "--norc", "--noprofile",
    )  # fmt: skip
    assert _tmux("display-message", "-p", "-t", pane, "#{pane_width}") == "49"

    def vim_in_pane(prefix: str) -> None:
        _keys(pane, "-l", "--", f"{prefix}vim -N -u NONE -i NONE user.txt")
        _keys(pane, "Enter")
        assert wait_until(
            lambda: _tmux("display-message", "-p", "-t", pane, "#{pane_current_command}") == "vim",
            timeout=10.0,
        )

    vim_in_pane("")
    window_id = ""
    if semantics == "adopted":
        window_id = _tmux("display-message", "-p", "-t", pane, "#{window_id}")
        FollowerState.set(window_id, backend="tmux", target=pane, adopted=True, speed="instant")
    follower = TmuxVimFollower(pane_id=pane, pace_seconds=0.0, window_id=window_id)
    # Vim on the hook's HOME: the "yes" is recorded against the shell's pid.
    follower.goto_file(str(target))
    record = (cache.CACHE_DIR / f"home-{pane.lstrip('%')}").read_text().split()
    assert (cache.CACHE_DIR / record[1]).exists()
    assert _state(pane, tmp_path / "s0.txt", wait_until)[0] == "BUF=target.py"

    # Same shell, Vim restarted on ANOTHER HOME, unsaved text in user.txt.
    _keys(pane, "-l", "--", ":qa!")
    _keys(pane, "Enter")
    assert wait_until(
        lambda: _tmux("display-message", "-p", "-t", pane, "#{pane_current_command}") == "bash",
        timeout=10.0,
    )
    vim_in_pane(f"HOME={other_home} ")
    _keys(pane, "-l", "--", "GoUSER UNSAVED WORK")
    _keys(pane, "Escape")
    before_follower = _state(pane, tmp_path / "s1.txt", wait_until)
    assert before_follower[:8] == [
        "BUF=user.txt", "MOD=1", "MA=1", "RO=0", "SWAP=1", "TABS=1", *USER_TEXT.split("\n")
    ]  # fmt: skip

    target.write_text(AFTER)
    with contextlib.suppress(Exception):  # a refusal to act is allowed, acting is not
        if operation == "ensure_showing":
            follower.ensure_showing(str(target))
        elif operation == "apply_edit":
            follower.apply_edit(str(target), compute_edit_script(BEFORE, AFTER), BEFORE)
        else:
            fresh = proj / "fresh.py"
            fresh.write_text("print('fresh')\n")
            follower.show_fresh(str(fresh), fresh.read_text(), in_new_tab=semantics == "adopted")
    after_failure = _state(pane, tmp_path / "s2.txt", wait_until)
    screen = _tmux("capture-pane", "-p", "-t", pane)

    # Same buffer, flags, swap, tab count and text: nothing ran on it, and no
    # stray tab was opened.
    assert after_failure[:9] == before_follower[:9], after_failure
    assert user.read_text() == "user line\n"
    assert "E484" not in "\n".join(after_failure), after_failure
    # The functions never reached this Vim (its `~` misses the script), and
    # every call line is guarded: nothing called an unknown function.
    assert "E117" not in "\n".join(after_failure), after_failure
    assert "Press ENTER" not in screen, screen
    assert str(other_home) not in screen, screen

    # The next call asks again (the stale "yes" is gone) and lands.
    follower.goto_file(str(target))
    landed = _state(pane, tmp_path / "s3.txt", wait_until)
    assert landed[0] == "BUF=target.py", landed
    # user.txt's buffer is still the user's, unsaved text and all.
    dump = tmp_path / "u.txt"
    _keys(
        pane,
        "-l",
        "--",
        ":call writefile(getbufline('user.txt', 1, '$')"
        f" + [getbufvar('user.txt', '&modified') . '', 'END'], '{dump}')",
    )
    _keys(pane, "Enter")
    assert wait_until(lambda: dump.exists() and dump.read_text().endswith("END\n"), timeout=10.0)
    assert dump.read_text().splitlines() == [*USER_TEXT.split("\n"), "1", "END"]
