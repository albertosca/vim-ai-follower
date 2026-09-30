"""Real tmux+vim proof that the tmux backend treats a file Vim holds under
ANOTHER NAME as that file: a buffer opened through a file symlink (a
dotfiles checkout: ~/.vimrc -> ~/.dotfiles/vimrc) while the hook passes the
realpath, or under a case variant on a case-insensitive filesystem.

Vim navigates by file identity (`:tab drop` switches to the existing buffer
whatever its name), so the follower must confirm the landing by identity too.
Review, 2026-09-29: a landing check that compared NAMES failed every Read of a
symlinked file after a 10 s wait (nothing shown, nothing locked), and a case
variant failed the Edit and then left a stray empty tab on each retry.
"""

from __future__ import annotations

import os
import subprocess
import time
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
# Far below the 10 s landing timeout: a call that waits it out fails here.
QUICK_SECONDS = 5.0


def _tmux(*args: str) -> str:
    return subprocess.run(
        ["tmux", *args], capture_output=True, text=True, check=True
    ).stdout.strip()


def _keys(pane: str, *keys: str) -> None:
    subprocess.run(["tmux", "send-keys", "-t", pane, *keys], check=True)


def _state(pane: str, out: Path, wait_until: Callable[..., bool]) -> dict[str, str | list[str]]:
    """The current buffer's name, flags and lines, the tab count and the
    listed buffers' resolved names — straight out of Vim."""
    out.unlink(missing_ok=True)
    _keys(pane, "Escape", "Escape")
    _keys(
        pane,
        "-l",
        "--",
        ":call writefile(['BUF=' . bufname('%'), 'MA=' . &modifiable, 'RO=' . &readonly,"
        " 'TABS=' . tabpagenr('$'), 'FILES=' . join(map(filter(range(1, bufnr('$')),"
        " 'buflisted(v:val) && bufname(v:val) !=# \"\"'), 'resolve(fnamemodify(bufname(v:val),"
        " \":p\"))'), ',')] + getline(1, '$') + ['END'], '" + str(out) + "')",
    )
    _keys(pane, "Enter")

    def answered() -> bool:
        return out.exists() and out.read_text().endswith("END\n")

    assert wait_until(answered, timeout=10.0), _tmux("capture-pane", "-p", "-t", pane)
    lines = out.read_text().splitlines()[:-1]
    fields: dict[str, str | list[str]] = {}
    for line in lines[:5]:
        key, value = line.split("=", 1)
        fields[key] = value
    fields["LINES"] = lines[5:]
    return fields


def _user_vim(tmux_session: str, monkeypatch: pytest.MonkeyPatch, root: Path, name: str) -> str:
    """A user's shell at the follower's width, with Vim opened on `name`."""
    monkeypatch.delenv("TMUX_PANE", raising=False)
    assert "TMUX" not in os.environ
    socket = _tmux("display-message", "-p", "#{socket_path}")
    assert socket.startswith(os.path.realpath(os.environ["TMUX_TMPDIR"]) + "/"), socket
    home = root / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setattr(cache, "CACHE_DIR", home / ".cache" / "claude-vim-follower")
    pane = _tmux(
        "split-window", "-h", "-t", tmux_session, "-c", str(root), "-P", "-F", "#{pane_id}",
        "env", "-i", f"HOME={home}", f"PATH={os.environ['PATH']}", "TERM=xterm-256color",
        "bash", "--norc", "--noprofile",
    )  # fmt: skip
    assert _tmux("display-message", "-p", "-t", pane, "#{pane_width}") == "49"
    _keys(pane, "-l", "--", f"vim -N -u NONE -i NONE {name}")
    _keys(pane, "Enter")
    time.sleep(0.2)
    for _ in range(100):
        if _tmux("display-message", "-p", "-t", pane, "#{pane_current_command}") == "vim":
            break
        time.sleep(0.1)
    time.sleep(0.5)
    return pane


def _quick(call: Callable[[], object]) -> object:
    start = time.monotonic()
    result = call()
    elapsed = time.monotonic() - start
    assert elapsed < QUICK_SECONDS, f"took {elapsed:.2f}s"
    return result


@pytest.mark.parametrize("adopted", [False, True], ids=["dedicated", "adopted"])
def test_a_file_open_through_a_symlink_is_the_hooks_realpath(
    tmux_session: str,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    wait_until: Callable[..., bool],
    adopted: bool,
) -> None:
    root = Path(os.path.realpath(tmp_path))
    real = root / "dotfiles_vimrc"
    real.write_text(BEFORE)
    (root / "vimrc_link").symlink_to(real)
    pane = _user_vim(tmux_session, monkeypatch, root, "vimrc_link")
    window_id = ""
    if adopted:
        window_id = _tmux("display-message", "-p", "-t", pane, "#{window_id}")
        FollowerState.set(window_id, backend="tmux", target=pane, adopted=True, speed="instant")
    follower = TmuxVimFollower(pane_id=pane, pace_seconds=0.0, window_id=window_id)

    # A Read: shown and locked where it is, no new tab.
    _quick(lambda: follower.ensure_showing(str(real)))
    shown = _state(pane, tmp_path / "s1.txt", wait_until)
    assert shown["BUF"] == "vimrc_link", shown
    assert (shown["MA"], shown["RO"], shown["TABS"]) == ("0", "1", "1"), shown
    # The probe finds the buffer under its other name: not "absent" (which
    # would send an Edit to the wipe-and-retype path).
    assert _quick(lambda: follower.probe_buffer(str(real), BEFORE)) == "holds"

    # An Edit: typed into that same buffer.
    real.write_text(AFTER)
    _quick(lambda: follower.apply_edit(str(real), compute_edit_script(BEFORE, AFTER), BEFORE))
    edited = _state(pane, tmp_path / "s2.txt", wait_until)
    assert edited["BUF"] == "vimrc_link", edited
    assert edited["LINES"] == AFTER.splitlines(), edited
    assert edited["TABS"] == "1", edited
    assert edited["FILES"] == str(real), edited
    if adopted:
        return
    # A retype (a dedicated follower's own buffer): the pre-wipe finds it
    # under the link's name, so the rename makes no second buffer for the
    # file (Vim refuses one: E95) and leaves no stray tab.
    _quick(lambda: follower.show_fresh(str(real), AFTER, in_new_tab=True))
    retyped = _state(pane, tmp_path / "s3.txt", wait_until)
    assert retyped["LINES"] == AFTER.splitlines(), retyped
    assert retyped["FILES"] == str(real), retyped


def test_a_case_variant_is_the_same_file_and_leaves_no_stray_tab(
    tmux_session: str,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    wait_until: Callable[..., bool],
) -> None:
    root = Path(os.path.realpath(tmp_path))
    lower = root / "readme.md"
    lower.write_text(BEFORE)
    upper = root / "README.md"
    if not upper.exists():
        pytest.skip("case-sensitive filesystem: README.md is another file")
    (root / "other.txt").write_text("other\n")
    pane = _user_vim(tmux_session, monkeypatch, root, "other.txt")
    follower = TmuxVimFollower(pane_id=pane, pace_seconds=0.0)

    _quick(lambda: follower.ensure_showing(str(lower)))  # tab 2
    lower.write_text(AFTER)
    # The hook's spelling differs from the buffer's only in case.
    assert _quick(lambda: follower.probe_buffer(str(upper), BEFORE)) == "holds"
    _quick(lambda: follower.apply_edit(str(upper), compute_edit_script(BEFORE, AFTER), BEFORE))
    edited = _state(pane, tmp_path / "s1.txt", wait_until)
    assert edited["LINES"] == AFTER.splitlines(), edited
    assert edited["TABS"] == "2", edited
    # A retype of it: the pre-wipe finds the case variant (closing its tab),
    # so the new tab replaces it: no second buffer for the file, no stray tab.
    _quick(lambda: follower.show_fresh(str(upper), AFTER, in_new_tab=True))
    retyped = _state(pane, tmp_path / "s2.txt", wait_until)
    assert retyped["LINES"] == AFTER.splitlines(), retyped
    assert retyped["TABS"] == "2", retyped
    files = str(retyped["FILES"]).split(",")
    assert [name for name in files if name.lower() == str(lower).lower()] != [], files
    assert len([name for name in files if name.lower() == str(lower).lower()]) == 1, files
