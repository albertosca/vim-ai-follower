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
import shutil
import subprocess
import time
from collections.abc import Callable, Iterator
from pathlib import Path

import pytest

from vim_ai_follower import cache
from vim_ai_follower.backends import NavigationFailed
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


def _user_vim(
    tmux_session: str,
    monkeypatch: pytest.MonkeyPatch,
    root: Path,
    name: str,
    *,
    cwd: Path | None = None,
) -> str:
    """A user's shell at the follower's width (in `cwd`, `root` by default),
    with Vim opened on `name`; HOME is `root`/home."""
    monkeypatch.delenv("TMUX_PANE", raising=False)
    assert "TMUX" not in os.environ
    socket = _tmux("display-message", "-p", "#{socket_path}")
    assert socket.startswith(os.path.realpath(os.environ["TMUX_TMPDIR"]) + "/"), socket
    home = root / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setattr(cache, "CACHE_DIR", home / ".cache" / "claude-vim-follower")
    pane = _tmux(
        "split-window", "-h", "-t", tmux_session, "-c", str(cwd or root), "-P", "-F", "#{pane_id}",
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


# Two DIFFERENT files whose names differ only in case, on a case-sensitive
# volume. macOS Vim has 'fileignorecase' on by default, and review
# 2026-09-29 measured on such a volume: lookups honoring it, and Vim's own
# `:tab drop` under it, took one file for the other — a Read of Readme.md
# showed README.md, an Edit of Readme.md was typed into README.md's buffer
# (the relock's `:e!` hid it), a retype of README.md wiped Readme.md's
# buffer, and in an adopted Vim a Read of README.md LOCKED the user's
# modified Readme.md.
LOWER = "lower one\nlower two\n"
LOWER_AFTER = "lower one\nlower inserted\nlower two\n"
UPPER = "UPPER ONE\nUPPER TWO\n"
UPPER_AFTER = "UPPER ONE\nUPPER INSERTED\nUPPER TWO\n"
UPPER_FRESH = "UPPER FRESH\n"


@pytest.fixture(scope="module")
def case_sensitive_volume(tmp_path_factory: pytest.TempPathFactory) -> Iterator[Path]:
    """A real Case-sensitive APFS disk image, attached for the module and
    detached and deleted after it. Skipped where hdiutil is missing (Linux
    CI) or refuses to make one."""
    if shutil.which("hdiutil") is None:
        pytest.skip("needs hdiutil (macOS) for a case-sensitive volume")
    base = Path(os.path.realpath(tmp_path_factory.mktemp("case-volume")))
    image = base / "vafcs.dmg"
    mount = base / "mnt"
    mount.mkdir()
    create = subprocess.run(
        ["hdiutil", "create", "-size", "20m", "-fs", "Case-sensitive APFS",
         "-volname", "vafcs", str(image)],
        capture_output=True, text=True, check=False,
    )  # fmt: skip
    if create.returncode != 0:
        pytest.skip(f"hdiutil create failed: {create.stderr.strip()}")
    attach = subprocess.run(
        ["hdiutil", "attach", "-nobrowse", "-mountpoint", str(mount), str(image)],
        capture_output=True, text=True, check=False,
    )  # fmt: skip
    if attach.returncode != 0:
        image.unlink(missing_ok=True)
        pytest.skip(f"hdiutil attach failed: {attach.stderr.strip()}")
    try:
        yield mount
    finally:
        detach = subprocess.run(["hdiutil", "detach", str(mount)], check=False)
        if detach.returncode != 0:
            subprocess.run(["hdiutil", "detach", "-force", str(mount)], check=False)
        image.unlink(missing_ok=True)


def _case_pair(volume: Path, name: str) -> tuple[Path, Path]:
    work = volume / name
    work.mkdir()
    lower = work / "Readme.md"
    upper = work / "README.md"
    lower.write_text(LOWER)
    upper.write_text(UPPER)
    assert not lower.samefile(upper), "not a case-sensitive volume"
    return lower, upper


# One buffer, as _buffers reports it: its tail name, changedtick, the flags
# modified/modifiable/readonly as three digits, and its lines.
Buffer = tuple[str, int, str, list[str]]


def _buffers(pane: str, out: Path, wait_until: Callable[..., bool]) -> tuple[str, list[Buffer]]:
    """The current buffer's tail name and every named buffer, straight out
    of Vim."""
    out.unlink(missing_ok=True)
    _keys(pane, "Escape", "Escape")
    _keys(
        pane,
        "-l",
        "--",
        ":call writefile(['CUR=' . fnamemodify(bufname('%'), ':t')] + map(filter(range(1,"
        " bufnr('$')), 'bufexists(v:val) && bufname(v:val) !=# \"\"'), 'fnamemodify(bufname(v:val),"
        ' ":t") . "\\t" . getbufvar(v:val, "changedtick") . "\\t" . getbufvar(v:val,'
        ' "&modified") . getbufvar(v:val, "&modifiable") . getbufvar(v:val, "&readonly")'
        ' . "\\t" . join(getbufline(v:val, 1, "$"), "|")\') + [\'END\'], \'' + str(out) + "')",
    )
    _keys(pane, "Enter")

    def answered() -> bool:
        return out.exists() and out.read_text().endswith("END\n")

    assert wait_until(answered, timeout=10.0), _tmux("capture-pane", "-p", "-t", pane)
    lines = out.read_text().splitlines()[:-1]
    buffers: list[Buffer] = []
    for line in lines[1:]:
        name, tick, flags, text = line.split("\t")
        buffers.append((name, int(tick), flags, text.split("|") if text else []))
    return lines[0].removeprefix("CUR="), buffers


def _only(buffers: list[Buffer], name: str) -> Buffer:
    matching = [buffer for buffer in buffers if buffer[0] == name]
    assert len(matching) == 1, buffers
    return matching[0]


def test_case_only_different_files_stay_apart_in_a_dedicated_follower(
    case_sensitive_volume: Path,
    tmux_session: str,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    wait_until: Callable[..., bool],
) -> None:
    lower, upper = _case_pair(case_sensitive_volume, "dedicated")
    (lower.parent / "other.txt").write_text("other\n")
    root = Path(os.path.realpath(tmp_path))
    pane = _user_vim(tmux_session, monkeypatch, root, "other.txt", cwd=lower.parent)
    follower = TmuxVimFollower(pane_id=pane, pace_seconds=0.0)

    # A Read of each shows its own file.
    _quick(lambda: follower.ensure_showing(str(upper)))
    current, buffers = _buffers(pane, tmp_path / "b1.txt", wait_until)
    assert current == "README.md", buffers
    assert _only(buffers, "README.md")[3] == UPPER.splitlines()
    _quick(lambda: follower.ensure_showing(str(lower)))
    current, buffers = _buffers(pane, tmp_path / "b2.txt", wait_until)
    assert current == "Readme.md", buffers
    assert _only(buffers, "Readme.md")[3] == LOWER.splitlines()
    assert _only(buffers, "README.md")[3] == UPPER.splitlines()

    # An Edit of each moves its own buffer and never touches the other
    # (changedtick: typing into it and the relock's reload both move it).
    untouched = _only(buffers, "README.md")
    lower.write_text(LOWER_AFTER)
    _quick(lambda: follower.apply_edit(str(lower), compute_edit_script(LOWER, LOWER_AFTER), LOWER))
    current, buffers = _buffers(pane, tmp_path / "b3.txt", wait_until)
    assert current == "Readme.md", buffers
    assert _only(buffers, "Readme.md")[3] == LOWER_AFTER.splitlines()
    assert _only(buffers, "README.md") == untouched, buffers
    untouched = _only(buffers, "Readme.md")
    upper.write_text(UPPER_AFTER)
    _quick(lambda: follower.apply_edit(str(upper), compute_edit_script(UPPER, UPPER_AFTER), UPPER))
    current, buffers = _buffers(pane, tmp_path / "b4.txt", wait_until)
    assert current == "README.md", buffers
    assert _only(buffers, "README.md")[3] == UPPER_AFTER.splitlines()
    assert _only(buffers, "Readme.md") == untouched, buffers

    # A retype of README.md wipes its own buffer, never its sibling's.
    upper.write_text(UPPER_FRESH)
    _quick(lambda: follower.show_fresh(str(upper), UPPER_FRESH, in_new_tab=True))
    current, buffers = _buffers(pane, tmp_path / "b5.txt", wait_until)
    assert current == "README.md", buffers
    assert _only(buffers, "README.md")[3] == UPPER_FRESH.splitlines()
    assert _only(buffers, "Readme.md") == untouched, buffers


def test_case_only_different_files_stay_apart_in_an_adopted_vim(
    case_sensitive_volume: Path,
    tmux_session: str,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    wait_until: Callable[..., bool],
) -> None:
    lower, upper = _case_pair(case_sensitive_volume, "adopted")
    root = Path(os.path.realpath(tmp_path))
    pane = _user_vim(tmux_session, monkeypatch, root, "Readme.md", cwd=lower.parent)
    # The user's unsaved work in Readme.md.
    _keys(pane, "-l", "GoUSER UNSAVED")
    _keys(pane, "Escape")
    window_id = _tmux("display-message", "-p", "-t", pane, "#{window_id}")
    FollowerState.set(window_id, backend="tmux", target=pane, adopted=True, speed="instant")
    follower = TmuxVimFollower(pane_id=pane, pace_seconds=0.0, window_id=window_id)
    _current, buffers = _buffers(pane, tmp_path / "b0.txt", wait_until)
    users = _only(buffers, "Readme.md")
    assert users[2] == "110" and users[3][-1] == "USER UNSAVED", users

    # A Read of README.md shows and locks README.md, never the user's buffer.
    _quick(lambda: follower.ensure_showing(str(upper)))
    current, buffers = _buffers(pane, tmp_path / "b1.txt", wait_until)
    assert current == "README.md", buffers
    shown = _only(buffers, "README.md")
    assert (shown[2], shown[3]) == ("001", UPPER.splitlines()), shown
    assert _only(buffers, "Readme.md") == users, buffers
    assert _quick(lambda: follower.probe_buffer(str(upper), UPPER)) == "holds"

    # An Edit and a retype of README.md leave the user's buffer alone.
    upper.write_text(UPPER_AFTER)
    _quick(lambda: follower.apply_edit(str(upper), compute_edit_script(UPPER, UPPER_AFTER), UPPER))
    upper.write_text(UPPER_FRESH)
    _quick(lambda: follower.show_fresh(str(upper), UPPER_FRESH, in_new_tab=True))
    current, buffers = _buffers(pane, tmp_path / "b2.txt", wait_until)
    assert current == "README.md", buffers
    assert _only(buffers, "README.md")[3] == UPPER_FRESH.splitlines()
    assert _only(buffers, "Readme.md") == users, buffers
    assert lower.read_text() == LOWER


def test_a_symlink_loop_among_the_buffers_blocks_no_navigation(
    tmux_session: str,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    wait_until: Callable[..., bool],
) -> None:
    """resolve() of a looping symlink raises E655. Review 2026-09-29: raised
    inside the buffer lookup, it aborted the whole lookup, so while such a
    buffer existed every navigation answered "elsewhere" and every Read and
    Edit was skipped; and a probe of a loop target went unanswered, which
    dropped the pane's HOME answer."""
    root = Path(os.path.realpath(tmp_path))
    (root / "a.txt").write_text(BEFORE)
    (root / "b.txt").write_text("b\n")
    (root / "loop_a").symlink_to(root / "loop_b")
    (root / "loop_b").symlink_to(root / "loop_a")
    pane = _user_vim(tmux_session, monkeypatch, root, "a.txt")
    _keys(pane, "-l", ":badd loop_a")
    _keys(pane, "Enter")
    follower = TmuxVimFollower(pane_id=pane, pace_seconds=0.0)

    _quick(lambda: follower.ensure_showing(str(root / "b.txt")))  # a new tab
    assert _state(pane, tmp_path / "s1.txt", wait_until)["BUF"] == "b.txt"
    _quick(lambda: follower.ensure_showing(str(root / "a.txt")))  # an open buffer
    assert _state(pane, tmp_path / "s2.txt", wait_until)["BUF"] == "a.txt"
    (root / "a.txt").write_text(AFTER)
    _quick(
        lambda: follower.apply_edit(str(root / "a.txt"), compute_edit_script(BEFORE, AFTER), BEFORE)
    )
    edited = _state(pane, tmp_path / "s3.txt", wait_until)
    assert (edited["BUF"], edited["LINES"]) == ("a.txt", AFTER.splitlines()), edited

    record = cache.CACHE_DIR / f"home-{pane.lstrip('%')}"
    assert record.exists()
    # A loop target: answered (its buffer is listed but not loaded), and the
    # pane's HOME answer stands.
    assert _quick(lambda: follower.probe_buffer(str(root / "loop_a"), "")) == "absent"
    assert record.exists()


def test_a_navigation_vim_resolves_to_a_buffer_of_another_name_is_not_a_landing(
    tmux_session: str,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    wait_until: Callable[..., bool],
) -> None:
    """The landing verdict confirms IDENTITY by name (resolve()d, with the
    filesystem's case folding), not merely that a navigation ran: with
    'fileignorecase' off Vim matches a file it already holds by inode, so
    `:tab drop` of a HARD LINK lands on the buffer of the other name. That
    is no landing on the hook's file: it answers "elsewhere" at once, and
    nothing more is sent (no lock on that buffer)."""
    root = Path(os.path.realpath(tmp_path))
    held = root / "held.txt"
    held.write_text(BEFORE)
    link = root / "hard_link.txt"
    link.hardlink_to(held)
    pane = _user_vim(tmux_session, monkeypatch, root, "held.txt")
    follower = TmuxVimFollower(pane_id=pane, pace_seconds=0.0)

    with pytest.raises(NavigationFailed, match="landed on another buffer"):
        _quick(lambda: follower.ensure_showing(str(link)))
    shown = _state(pane, tmp_path / "s1.txt", wait_until)
    assert (shown["BUF"], shown["MA"], shown["RO"]) == ("held.txt", "1", "0"), shown
    # show_fresh's rename onto the link: the pre-wipe finds no buffer of that
    # name, and Vim refuses the rename (E95, it holds a loaded buffer for the
    # same inode), so nothing lands and nothing is typed.
    with pytest.raises(NavigationFailed, match="landed on another buffer"):
        _quick(lambda: follower.show_fresh(str(link), AFTER, in_new_tab=True))
    renamed = _state(pane, tmp_path / "s2.txt", wait_until)
    assert renamed["LINES"] != AFTER.splitlines(), renamed


def test_a_rename_that_does_not_land_leaves_the_users_buffer_alone(
    tmux_session: str,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    wait_until: Callable[..., bool],
) -> None:
    """show_fresh's rename line goes on, after the `:file`, to make the buffer
    writable, read the file and clear it (`edit!` then `%d`), and in an
    adopted Vim to claim its readonly. All of that runs only on the
    identity-confirmed landing of THIS call. Review, 2026-09-29: a user
    autocommand switching tabs on the rename (`BufFilePost … tabfirst`) left
    the user's buffer current; the landing check answered "elsewhere", but
    the tail of the line had already wiped the user's unsaved text."""
    root = Path(os.path.realpath(tmp_path))
    (root / "user.txt").write_text("user line\n")
    target = root / "jump.py"
    target.write_text("jump = 1\n")
    pane = _user_vim(tmux_session, monkeypatch, root, "user.txt")
    _keys(pane, "-l", ":autocmd BufFilePost jump.py tabfirst")
    _keys(pane, "Enter")
    _keys(pane, "-l", "GoUSER UNSAVED")
    _keys(pane, "Escape")
    window_id = _tmux("display-message", "-p", "-t", pane, "#{window_id}")
    FollowerState.set(window_id, backend="tmux", target=pane, adopted=True, speed="instant")
    follower = TmuxVimFollower(pane_id=pane, pace_seconds=0.0, window_id=window_id)

    with pytest.raises(NavigationFailed, match="landed on another buffer"):
        _quick(lambda: follower.show_fresh(str(target), "jump = 1\n", in_new_tab=True))
    user = _state(pane, tmp_path / "s1.txt", wait_until)
    assert user["BUF"] == "user.txt", user
    assert user["LINES"] == ["user line", "USER UNSAVED"], user
    assert (user["MA"], user["RO"]) == ("1", "0"), user
    assert "Press ENTER" not in _tmux("capture-pane", "-p", "-t", pane)
    modified = tmp_path / "mod.txt"
    _keys(
        pane,
        "-l",
        "--",
        f":call writefile([&modified, get(b:, 'vaf_ro_ours', 'none'), 'END'], '{modified}')",
    )
    _keys(pane, "Enter")
    assert wait_until(
        lambda: modified.exists() and modified.read_text().endswith("END\n"), timeout=10.0
    )
    assert modified.read_text().split() == ["1", "none", "END"]
