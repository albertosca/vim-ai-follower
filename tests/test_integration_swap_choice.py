"""Real tmux+vim proof that a swap file on the target never stalls the
follower pane, and that answering it does not change anything for the
user's own editing.

Geometry matters and is not incidental, for the same reason it does in
tests/test_integration_goto_file_e37.py: the real follower pane is a
`tmux split-window -h` inside the suite's 100-column window, which leaves
it at 49 columns. At that width Vim's `E325: ATTENTION ... Swap file
"..." already exists!` block is long enough to hit the `-- More --` pager
BEFORE it reaches the `[O]pen Read-Only, (E)dit anyway, (R)ecover,
(Q)uit, (A)bort` question, so the pane is blocked twice over and every
keystroke the follower sends afterwards answers a prompt instead of
navigating (measured). Every test here goes through `cli.main(["start"])`
so it gets the production geometry, never a hand-rolled session.

Two states reach that dialog in normal use and both are covered below: a
LIVE owner (another Vim holding the file open — the everyday adopt-mode
case, the user's own editor on the file Claude is writing) and a STALE
swap left behind by a crash, which additionally offers `(D)elete it`.

Policy (Alberto, 2026-09-21): the follower answers `(E)dit anyway`, adopt
mode included, because it never writes the file — its buffers are
display-only and relocked read-only. The two tests that keep that promise
honest are the ones that are easy to leave out: that the other Vim can
still `:w` its unsaved work afterwards, and that the stale swap file is
still on disk (we choose Edit, never `(D)elete it`, so no recovery data
is destroyed).

The last test is the scoping one, and it is the reason the mechanism is a
`SwapExists` autocommand torn down in `finally` rather than
`shortmess+=A` or `set noswapfile`. Those two also clear the dialog and
both were measured to LEAK: global, never restored, and from then on the
user's own `:e` of a swapped file opens silently. Here the user's `:e`
must still get the normal dialog after the follower has navigated.

Each scenario asserts liveness with a plain NON-colon keystroke, not just
the absence of the dialog from the capture. A `:`-prefixed command is not
a valid probe: `:` is a real key in More mode, so an Ex command can
dismiss the very prompt it was sent to detect and report success.
"""

from __future__ import annotations

import os
import signal
import subprocess
from collections.abc import Callable
from pathlib import Path

import pytest

from vim_ai_follower import cli
from vim_ai_follower.backends.tmux_vim import TmuxVimFollower

pytestmark = pytest.mark.integration

_SWAP_GROUP = "vim_ai_follower_swap"
_DIALOG_MARKERS = (
    "ATTENTION",
    "E325",
    "swap file",
    "Swap file",
    "-- More --",
    "Press ENTER",
    "Open Read-Only",
)


def _pane_ids(session_name: str) -> list[str]:
    result = subprocess.run(
        ["tmux", "list-panes", "-a", "-t", session_name, "-F", "#{pane_id}"],
        capture_output=True,
        text=True,
        check=True,
    )
    return result.stdout.splitlines()


def _capture(pane_id: str) -> str:
    result = subprocess.run(
        ["tmux", "capture-pane", "-t", pane_id, "-p"],
        capture_output=True,
        text=True,
        check=True,
    )
    return result.stdout


def _send_text(pane_id: str, text: str) -> None:
    subprocess.run(["tmux", "send-keys", "-t", pane_id, "-l", "--", text], check=True)


def _send_key(pane_id: str, key: str) -> None:
    subprocess.run(["tmux", "send-keys", "-t", pane_id, key], check=True)


def _pane_width(pane_id: str) -> int:
    result = subprocess.run(
        ["tmux", "display-message", "-p", "-t", pane_id, "#{pane_width}"],
        capture_output=True,
        text=True,
        check=True,
    )
    return int(result.stdout.strip())


def _pane_pid(pane_id: str) -> int:
    result = subprocess.run(
        ["tmux", "display-message", "-p", "-t", pane_id, "#{pane_pid}"],
        capture_output=True,
        text=True,
        check=True,
    )
    return int(result.stdout.strip())


def _start_follower(
    tmux_session: str, monkeypatch: pytest.MonkeyPatch, wait_until: Callable[..., bool]
) -> str:
    origin_pane = _pane_ids(tmux_session)[0]
    monkeypatch.setenv("TMUX_PANE", origin_pane)
    assert cli.main(["start"]) == 0
    assert wait_until(lambda: len(_pane_ids(tmux_session)) == 2)
    pane_id = next(p for p in _pane_ids(tmux_session) if p != origin_pane)
    # Guard the guard: at >= 51 columns the ATTENTION block stops paging
    # and this whole file could pass with the fix reverted.
    width = _pane_width(pane_id)
    assert width < 51, (
        f"follower pane is {width} columns: the ATTENTION block would no "
        "longer page, so these tests could not detect the stall they exist for"
    )
    return pane_id


def _swap_path(target: Path) -> Path:
    """Where Vim puts the swap for `target` with the suite's hermetic
    VIMINIT: 'directory' still starts with '.', so it lands beside the
    file as `.<name>.swp`."""
    return target.with_name(f".{target.name}.swp")


def _open_in_another_vim(tmux_session: str, target: Path, wait_until: Callable[..., bool]) -> str:
    """A SECOND, independent Vim holding `target` open, in its own tmux
    window — the live swap owner. Returns its pane id."""
    result = subprocess.run(
        [
            "tmux",
            "new-window",
            "-t",
            tmux_session,
            "-P",
            "-F",
            "#{pane_id}",
            f"vim {target}",
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    pane_id = result.stdout.strip()
    assert wait_until(lambda: _swap_path(target).exists(), timeout=10.0), (
        f"the other Vim never created {_swap_path(target)}"
    )
    return pane_id


def _read_or_empty(path: Path) -> str:
    """Vim's default 'writebackup' saves by renaming the original aside,
    writing a new file and then removing the backup — so there is a real
    instant in which the target does not exist. A plain read_text() in a
    poll raises FileNotFoundError there and kills the wait instead of
    retrying, which is a flaky instrument, not a failure of the product."""
    try:
        return path.read_text()
    except OSError:
        return ""


def _assert_no_dialog(pane_id: str) -> None:
    pane = _capture(pane_id)
    for marker in _DIALOG_MARKERS:
        assert marker not in pane, f"{marker!r} is on screen after goto_file:\n{pane}"


def _row_with(pane_id: str, marker: str) -> tuple[int, str]:
    lines = _capture(pane_id).splitlines()
    row = next(i for i, line in enumerate(lines) if marker in line)
    return row, lines[row]


def _assert_plain_key_lands_as_a_command(
    pane_id: str, row: int, line_before: str, wait_until: Callable[..., bool]
) -> None:
    """Send one NON-colon key ('x', Normal-mode delete-char) and require it
    to have executed against the buffer. Under the bug the pane sits in
    More mode, where 'x' is simply not a pager key: it changes nothing and
    only redraws the pager's own help line.

    Which character goes is deliberately not pinned — the cursor column
    depends on how the buffer was reached."""
    deletions = {line_before[:i] + line_before[i + 1 :] for i in range(len(line_before))}
    _send_text(pane_id, "x")
    assert wait_until(
        lambda: (lambda ls: row < len(ls) and ls[row] in deletions)(_capture(pane_id).splitlines()),
        timeout=5.0,
    ), (
        "the plain 'x' keystroke never landed as a real command — row "
        f"{row} was {line_before!r} and no single-character deletion of it "
        f"appeared, pane is:\n{_capture(pane_id)}"
    )


def _read_back(
    pane_id: str, tmp_path: Path, name: str, command: str, wait_until: Callable[..., bool]
) -> str:
    """Run `command` in the pane's Vim and return its output, via :redir to
    a FILE. Never via a scratch buffer: pasting the output would leave the
    buffer modified, and the next `:e` would then die on E37 without ever
    opening a file — a 'no dialog' result that measures nothing."""
    out = tmp_path / name
    _send_text(pane_id, f":redir! > {out}")
    _send_key(pane_id, "Enter")
    _send_text(pane_id, command)
    _send_key(pane_id, "Enter")
    _send_text(pane_id, ":redir END")
    _send_key(pane_id, "Enter")
    assert wait_until(lambda: out.exists() and out.stat().st_size > 0, timeout=5.0), (
        f"Vim never executed the read-back for {command!r} — it is still "
        f"blocked. Pane:\n{_capture(pane_id)}"
    )
    return out.read_text()


def _assert_no_hook_residue(
    pane_id: str, tmp_path: Path, name: str, wait_until: Callable[..., bool]
) -> None:
    """The hook must be gone the instant the navigation is over. This is
    also a liveness proof: a Vim stuck at `-- More --` never runs :redir,
    so the read-back would time out."""
    listing = _read_back(pane_id, tmp_path, name, ":silent! autocmd SwapExists", wait_until)
    assert _SWAP_GROUP not in listing, f"the swap hook outlived the navigation:\n{listing}"
    assert "swapchoice" not in listing, f"the swap hook outlived the navigation:\n{listing}"


def test_live_owner_swap_never_stalls_and_the_other_vim_still_writes(
    tmux_session: str,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    wait_until: Callable[..., bool],
) -> None:
    """The everyday adopt-mode case: the user's own Vim is holding the very
    file Claude is writing, with unsaved changes of their own."""
    follower_pane_id = _start_follower(tmux_session, monkeypatch, wait_until)
    target = tmp_path / "live_owner.py"
    target.write_text("disk alpha\ndisk beta\n")

    other = _open_in_another_vim(tmux_session, target, wait_until)
    _send_text(other, "oOTHER VIM UNSAVED")
    _send_key(other, "Escape")
    assert wait_until(lambda: "OTHER VIM UNSAVED" in _capture(other), timeout=5.0)

    TmuxVimFollower(pane_id=follower_pane_id).goto_file(str(target))

    assert wait_until(lambda: "disk alpha" in _capture(follower_pane_id), timeout=10.0), (
        f"the follower never landed on the file:\n{_capture(follower_pane_id)}"
    )
    _assert_no_dialog(follower_pane_id)
    # (b) the REAL disk content, not the other Vim's unsaved buffer (which
    # is what `(R)ecover` would have shown instead).
    pane = _capture(follower_pane_id)
    assert "disk beta" in pane
    assert "OTHER VIM UNSAVED" not in pane
    row, line_before = _row_with(follower_pane_id, "disk alpha")
    _assert_plain_key_lands_as_a_command(follower_pane_id, row, line_before, wait_until)
    _assert_no_hook_residue(follower_pane_id, tmp_path, "live.txt", wait_until)

    # (c) the other Vim's session is untouched: it still owns its swap and
    # can still save its unsaved work.
    _send_text(other, ":w")
    _send_key(other, "Enter")
    assert wait_until(lambda: "OTHER VIM UNSAVED" in _read_or_empty(target), timeout=15.0), (
        "the other Vim could not write its unsaved work after the follower "
        f"edited the same file anyway. Its pane:\n{_capture(other)}"
    )


def test_stale_swap_never_stalls_and_is_left_on_disk(
    tmux_session: str,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    wait_until: Callable[..., bool],
) -> None:
    """A crash-leftover swap with no live owner. Vim's dialog gains a
    `(D)elete it` here — the follower must still answer Edit, so the swap
    survives for the user to recover from later."""
    follower_pane_id = _start_follower(tmux_session, monkeypatch, wait_until)
    target = tmp_path / "stale.py"
    target.write_text("stale disk alpha\nstale disk beta\n")

    doomed = _open_in_another_vim(tmux_session, target, wait_until)
    _send_text(doomed, "oWORK LOST IN THE CRASH")
    _send_key(doomed, "Escape")
    assert wait_until(lambda: "WORK LOST IN THE CRASH" in _capture(doomed), timeout=5.0)
    # SIGKILL, never SIGTERM: on SIGTERM Vim deletes its own swap on the way
    # out, and the scenario would silently become "no swap at all" — a test
    # that passes against the unfixed code.
    os.kill(_pane_pid(doomed), signal.SIGKILL)
    swap = _swap_path(target)
    assert wait_until(lambda: swap.exists(), timeout=5.0), "the swap was cleaned up"

    TmuxVimFollower(pane_id=follower_pane_id).goto_file(str(target))

    assert wait_until(lambda: "stale disk alpha" in _capture(follower_pane_id), timeout=10.0), (
        f"the follower never landed on the file:\n{_capture(follower_pane_id)}"
    )
    _assert_no_dialog(follower_pane_id)
    assert "stale disk beta" in _capture(follower_pane_id)
    row, line_before = _row_with(follower_pane_id, "stale disk alpha")
    _assert_plain_key_lands_as_a_command(follower_pane_id, row, line_before, wait_until)
    _assert_no_hook_residue(follower_pane_id, tmp_path, "stale.txt", wait_until)
    # Edit anyway, never (D)elete it: the crashed session's recovery data
    # is still there.
    assert swap.exists(), "the follower destroyed the crashed session's swap file"


def test_a_target_with_no_swap_is_reached_exactly_as_before(
    tmux_session: str,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    wait_until: Callable[..., bool],
) -> None:
    """Control. The hook fires only on SwapExists, so the ordinary case has
    to be untouched: the file opens, and the follower's Vim is left with
    'swapfile' and 'shortmess' exactly as it found them — the two settings
    the rejected mechanisms would have changed globally and forever."""
    follower_pane_id = _start_follower(tmux_session, monkeypatch, wait_until)
    before = _read_back(
        follower_pane_id, tmp_path, "before.txt", ":silent set swapfile? shortmess?", wait_until
    )
    target = tmp_path / "no_swap.py"
    target.write_text("plain alpha\nplain beta\n")
    assert not _swap_path(target).exists()

    TmuxVimFollower(pane_id=follower_pane_id).goto_file(str(target))

    assert wait_until(lambda: "plain alpha" in _capture(follower_pane_id), timeout=10.0)
    _assert_no_dialog(follower_pane_id)
    assert "plain beta" in _capture(follower_pane_id)
    row, line_before = _row_with(follower_pane_id, "plain alpha")
    _assert_plain_key_lands_as_a_command(follower_pane_id, row, line_before, wait_until)
    _assert_no_hook_residue(follower_pane_id, tmp_path, "nosw.txt", wait_until)

    after = _read_back(
        follower_pane_id, tmp_path, "after.txt", ":silent set swapfile? shortmess?", wait_until
    )
    assert after == before, f"global options changed:\nbefore: {before!r}\nafter:  {after!r}"


def test_the_edit_anyway_policy_does_not_leak_into_the_users_own_edit(
    tmux_session: str,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    wait_until: Callable[..., bool],
) -> None:
    """Adopt-mode scoping, and the reason the mechanism is a torn-down
    autocommand rather than `shortmess+=A` or `set noswapfile`.

    After the follower has navigated (answering the dialog for ITS target),
    the user does a plain `:e` in that same Vim, of an UNRELATED file that
    also has a swap. They must still get the normal dialog: the follower's
    policy applies to the follower's own navigation, not to the user's
    editing. Both rejected mechanisms fail exactly here — the file just
    opens, silently."""
    follower_pane_id = _start_follower(tmux_session, monkeypatch, wait_until)
    target = tmp_path / "followers_target.py"
    target.write_text("followers content\n")
    unrelated = tmp_path / "users_own_file.py"
    unrelated.write_text("the users own file\n")

    _open_in_another_vim(tmux_session, target, wait_until)
    _open_in_another_vim(tmux_session, unrelated, wait_until)

    TmuxVimFollower(pane_id=follower_pane_id).goto_file(str(target))
    assert wait_until(lambda: "followers content" in _capture(follower_pane_id), timeout=10.0)
    _assert_no_dialog(follower_pane_id)
    _assert_no_hook_residue(follower_pane_id, tmp_path, "leak.txt", wait_until)

    # The follower's buffer is clean, so this `:e` really does try to open
    # the file — it cannot die on E37 first and hand back a meaningless
    # "no dialog".
    modified = _read_back(
        follower_pane_id, tmp_path, "mod.txt", ':silent echo "modified=" . &modified', wait_until
    )
    assert "modified=0" in modified, (
        f"the buffer was dirty, the :e below proves nothing: {modified!r}"
    )

    _send_text(follower_pane_id, f":e {unrelated}")
    _send_key(follower_pane_id, "Enter")

    assert wait_until(
        lambda: any(m in _capture(follower_pane_id) for m in ("ATTENTION", "E325", "swap file")),
        timeout=10.0,
    ), (
        "the user's own :e of an unrelated swapped file did NOT get the "
        "normal swap dialog — the follower's Edit-anyway policy leaked into "
        f"their editing. Pane:\n{_capture(follower_pane_id)}"
    )
