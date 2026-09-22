"""Real tmux+vim proof that max_tabs actually caps the follower's tabs.

This is the test whose absence let the eviction bug ship. Every existing
eviction test asserted the BOOKKEEPING — that `touch_open_files` returns the
right five paths, that `close_tab` gets called with the sixth — and the
bookkeeping was never wrong. What nobody measured was whether Vim ended up
with five tabs, and it did not: `close_tab` sent `:bwipeout! {path}`, whose
argument Vim reads as a buffer-name PATTERN (`:h {bufname}`), so any path
containing `[`, `]`, `{` or `}` matched no buffer at all and `:silent!` ate
the E94 that said so. Measured live on 2026-09-22: the follower's state file
held exactly five `open_files` while the real tabline was scrolled past nine
tabs.

The old `close_tab` made it worse than a no-op. It navigated with
`goto_file` first, and `:tab drop` OPENS a tab for a path Vim does not
already hold — so an eviction whose wipe then missed ADDED a tab instead of
removing one, which is why the count climbs rather than merely sticking.

Content assertions here read the real buffer list over `writefile()`, never
`capture-pane`: the tabline is a rendered, abbreviated, horizontally
SCROLLED view (that live sighting only showed tabs 7-9 because the rest had
scrolled off), so it cannot answer "how many tabs are there". The tab count
and the buffer names come from Vim itself.
"""

from __future__ import annotations

import io
import json
import subprocess
from collections.abc import Callable
from pathlib import Path

import pytest

from vim_ai_follower import cli, config
from vim_ai_follower.backends.tmux_vim import TmuxVimFollower
from vim_ai_follower.state import FollowerState

pytestmark = pytest.mark.integration


def _pane_ids(session_name: str) -> list[str]:
    result = subprocess.run(
        ["tmux", "list-panes", "-t", session_name, "-F", "#{pane_id}"],
        capture_output=True,
        text=True,
        check=True,
    )
    return result.stdout.splitlines()


def _send_text(pane_id: str, text: str) -> None:
    subprocess.run(["tmux", "send-keys", "-t", pane_id, "-l", "--", text], check=True)


def _send_key(pane_id: str, key: str) -> None:
    subprocess.run(["tmux", "send-keys", "-t", pane_id, key], check=True)


def _start_follower(
    tmux_session: str, monkeypatch: pytest.MonkeyPatch, wait_until: Callable[..., bool]
) -> str:
    origin_pane = _pane_ids(tmux_session)[0]
    monkeypatch.setenv("TMUX_PANE", origin_pane)
    assert cli.main(["start"]) == 0
    assert wait_until(lambda: len(_pane_ids(tmux_session)) == 2)
    return next(p for p in _pane_ids(tmux_session) if p != origin_pane)


def _window_id(tmux_session: str) -> str:
    result = subprocess.run(
        ["tmux", "display-message", "-p", "-t", _pane_ids(tmux_session)[0], "#{window_id}"],
        capture_output=True,
        text=True,
        check=True,
    )
    return result.stdout.strip()


def _vim_state(
    pane_id: str, out_path: Path, wait_until: Callable[..., bool]
) -> tuple[int, list[str]]:
    """(tab count, listed buffer names) straight out of Vim.

    Round-tripped through a file rather than `capture-pane` for the reason
    in this module's docstring. Deleting the file first and waiting for it
    to reappear is what distinguishes "Vim answered" from "Vim never ran the
    command and we re-read the previous answer" — a stale read here would
    report the old, passing tab count forever."""

    def answered() -> bool:
        # Existence is not the signal: writefile() creates the file and then
        # fills it, so a bare exists() check reads an empty file under load
        # (measured on this suite at load 7.5). The TABS= prefix is the
        # earliest point at which the answer is complete enough to parse.
        try:
            return out_path.read_text().startswith("TABS=")
        except OSError:
            return False

    # Two prompt-proof Escapes, then the query. Retried as a whole: a pane
    # that was still disambiguating a raw ESC pair swallows the colon and
    # the query never runs, and resending is harmless (the query is a pure
    # read). Without the retry a busy machine turns into a flaky assertion
    # about tab counts.
    for _ in range(3):
        out_path.unlink(missing_ok=True)
        _send_key(pane_id, "Escape")
        _send_key(pane_id, "Escape")
        _send_text(
            pane_id,
            ":call writefile(['TABS=' . tabpagenr('$')] + map(filter(range(1, bufnr('$')), "
            f"'buflisted(v:val)'), 'bufname(v:val)'), '{out_path}')",
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
        raise AssertionError(
            "Vim never wrote the buffer-list dump — the probe itself failed, "
            f"so no conclusion about tab count can be drawn. Pane:\n{pane}"
        )
    lines = out_path.read_text().splitlines()
    return int(lines[0].removeprefix("TABS=")), lines[1:]


def _write_through_the_hook(path: Path, body: str, monkeypatch: pytest.MonkeyPatch) -> None:
    """One production PostToolUse Write hook, start to finish. Synchronous:
    the animation runs inline, so when this returns the pane is settled."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body)
    payload = json.dumps({"tool_name": "Write", "tool_input": {"file_path": str(path)}})
    monkeypatch.setattr("sys.stdin", io.StringIO(payload))
    assert cli.main(["hook", "post"]) == 0


def test_eviction_caps_vim_at_max_tabs_for_pattern_metacharacter_paths(
    tmux_session: str,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    wait_until: Callable[..., bool],
) -> None:
    """Nine files through the real hook, under a Next.js-style `[slug]`
    directory — an ordinary path, and the exact shape `:bwipeout! {path}`
    cannot match, because `[slug]` is a character class to it.

    Asserts both halves. The tab count is the user-visible symptom; the
    named buffer being gone is what proves the tab closed because the buffer
    was wiped, rather than a count that happens to line up."""
    follower_pane = _start_follower(tmux_session, monkeypatch, wait_until)
    window_id = _window_id(tmux_session)
    max_tabs = config.load().max_tabs
    dump = tmp_path / "dump.txt"

    root = tmp_path / "app"
    paths = [root / "[slug]" / f"page{i}.tsx" for i in range(max_tabs + 4)]
    for i, path in enumerate(paths):
        _write_through_the_hook(path, f"export const n = {i};\n", monkeypatch)

    tab_count, buffers = _vim_state(follower_pane, dump, wait_until)
    assert tab_count == max_tabs, (
        f"max_tabs is {max_tabs} but Vim holds {tab_count} tabs after "
        f"{len(paths)} files — eviction is not closing anything. Buffers: {buffers}"
    )

    # The follower's own bookkeeping was never the broken half; assert it
    # agrees anyway, so a future failure says which side drifted.
    state = FollowerState.read(window_id)
    assert state is not None
    assert len(state.open_files) == max_tabs
    assert sorted(state.open_files) == sorted(buffers)

    # The four oldest are gone from Vim, not merely dropped from the state.
    for evicted in paths[:4]:
        assert str(evicted) not in buffers, f"{evicted} was 'evicted' but its buffer is still alive"


def test_eviction_caps_vim_at_max_tabs_for_plain_paths(
    tmux_session: str,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    wait_until: Callable[..., bool],
) -> None:
    """The same measurement on ordinary paths.

    Honest note: this one passed against the buggy `close_tab` too — a path
    with no pattern metacharacters happens to match itself, so the name-based
    wipe worked. It is a parity guard, not a canary; the test above is the
    one that goes red when the fix is reverted."""
    follower_pane = _start_follower(tmux_session, monkeypatch, wait_until)
    max_tabs = config.load().max_tabs
    dump = tmp_path / "dump.txt"

    paths = [tmp_path / "src" / f"mod{i}.py" for i in range(max_tabs + 3)]
    for i, path in enumerate(paths):
        _write_through_the_hook(path, f"value = {i}\n", monkeypatch)

    tab_count, buffers = _vim_state(follower_pane, dump, wait_until)
    assert tab_count == max_tabs
    assert sorted(buffers) == sorted(str(p) for p in paths[-max_tabs:])


def test_close_tab_wipes_a_bracketed_path_instead_of_silently_missing_it(
    tmux_session: str,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    wait_until: Callable[..., bool],
) -> None:
    """close_tab on its own, isolated from the hook stack, against the name
    form that was proved to defeat the old one.

    The third assertion is the one that pins the missing goto_file preamble:
    the old close_tab navigated first, so a wipe that missed left behind the
    tab `:tab drop` had just opened. Here the neighbours must be untouched
    and the total must go DOWN by exactly one — never up."""
    follower_pane = _start_follower(tmux_session, monkeypatch, wait_until)
    dump = tmp_path / "dump.txt"
    follower = TmuxVimFollower(pane_id=follower_pane)

    victim = tmp_path / "routes" / "[id]" / "handler.ts"
    keepers = [tmp_path / "routes" / f"keep{i}.ts" for i in range(2)]
    for path in [victim, *keepers]:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("export {};\n")
        follower.ensure_showing(str(path))

    before_count, before_buffers = _vim_state(follower_pane, dump, wait_until)
    assert str(victim) in before_buffers, (
        "setup never opened the victim — nothing is being measured"
    )

    follower.close_tab(str(victim))

    assert wait_until(
        lambda: str(victim) not in _vim_state(follower_pane, dump, wait_until)[1], timeout=10.0
    ), f"close_tab left {victim}'s buffer alive"
    after_count, after_buffers = _vim_state(follower_pane, dump, wait_until)
    assert after_count == before_count - 1
    assert sorted(after_buffers) == sorted(str(p) for p in keepers)


def test_close_tab_on_an_unopened_path_opens_nothing(
    tmux_session: str,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    wait_until: Callable[..., bool],
) -> None:
    """Evicting a path Vim does not hold must be a no-op.

    Honest note, same as the plain-paths test above: this one passed against
    the buggy close_tab too, and measurably so — the old pairing opened the
    file with `:tab drop` and then wiped it by a name that did match, which
    nets out to zero. It is a parity guard for the removed preamble, not a
    canary. What it does pin going forward is that eviction never TOUCHES a
    path Vim is not already holding: the old code's round trip was harmless
    only while the wipe always landed, and it is exactly that round trip
    that turned a missed wipe into a net-plus-one tab."""
    follower_pane = _start_follower(tmux_session, monkeypatch, wait_until)
    dump = tmp_path / "dump.txt"
    follower = TmuxVimFollower(pane_id=follower_pane)

    for i in range(2):
        path = tmp_path / f"open{i}.py"
        path.write_text("x = 1\n")
        follower.ensure_showing(str(path))
    before_count, before_buffers = _vim_state(follower_pane, dump, wait_until)

    stranger = tmp_path / "never-opened.py"
    stranger.write_text("y = 2\n")
    follower.close_tab(str(stranger))

    after_count, after_buffers = _vim_state(follower_pane, dump, wait_until)
    assert after_count == before_count
    assert after_buffers == before_buffers
    assert str(stranger) not in after_buffers
