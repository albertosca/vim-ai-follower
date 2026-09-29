"""End-to-end tranche 3 of the machine-verifiable half of `qa/visual-battery.md`
(checks 1-10): the real `claude-follow` CLI, real hook payloads on stdin, a
real Vim/Neovim follower in an isolated world. Each test names the battery
check it replaces and the commit(s) it guards; what stays in the battery is
the part only a human can judge."""

from __future__ import annotations

import json
import shutil
import subprocess
import time
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import pytest
from e2e_harness import REPO_ROOT, E2EFollower, e2e_world, payload

from vim_ai_follower.commands import VIM_NEEDS_TMUX

pytestmark = pytest.mark.integration


@pytest.fixture
def world() -> Iterator[E2EFollower]:
    with e2e_world() as live:
        yield live


def _write_config(world: E2EFollower, settings: dict[str, str]) -> None:
    path = world.home / ".config" / "claude-vim-follower" / "config.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(settings))


def _assert_private_server(world: E2EFollower) -> None:
    """The tmux every child of this world reaches is the PRIVATE server. The
    checks without TMUX_PANE depend on it twice: session resolution then
    scans `tmux list-panes -a` for an ancestor pane, and on the developer's
    real server that scan could resolve pytest to his live window."""
    socket_path = world.tmux("display-message", "-p", "#{socket_path}").stdout.strip()
    assert Path(socket_path).resolve().is_relative_to(Path(world.tmux_tmpdir).resolve()), (
        f"tmux answered from {socket_path}, not from the private {world.tmux_tmpdir}"
    )


def _all_panes(world: E2EFollower) -> list[str]:
    return world.tmux("list-panes", "-a", "-F", "#{pane_id}").stdout.split()


def _follower_leftovers(world: E2EFollower) -> list[str]:
    cache = world.cache_dir
    if not cache.exists():
        return []
    return sorted(p.name for glob in ("*.pane", "nvim-*.sock") for p in cache.glob(glob))


FIXTURES = REPO_ROOT / "qa" / "fixtures"


def _write_through_hooks(
    world: E2EFollower,
    path: Path,
    content: str,
    identity: dict[str, str] | None = None,
    env: dict[str, str] | None = None,
) -> None:
    """One synchronous Write: pre snapshots the current file, the new content
    lands, post animates the diff — the order a PreToolUse/PostToolUse pair
    fires in."""
    world.cli("hook", "pre", stdin=payload("Write", path, identity=identity), env=env)
    path.write_text(content)
    world.cli("hook", "post", stdin=payload("Write", path, identity=identity), env=env)


def _pane_option(world: E2EFollower, pane: str, name: str) -> str:
    """A pane-LOCAL option's value, "" when unset on that pane."""
    return world.tmux("show-options", "-pv", "-t", pane, name, check=False).stdout.strip()


def _window_option(world: E2EFollower, pane: str, name: str) -> str:
    """A WINDOW-local option's value for the window holding `pane`, "" when unset."""
    return world.tmux("show-options", "-wv", "-t", pane, name, check=False).stdout.strip()


# --------------------------------------------------------------- Checks 1-2

REVIEWER = {"agent_id": "rev1", "agent_type": "code-reviewer"}


def test_second_writer_tints_and_titles_the_follower_border_and_stop_restores_it(
    world: E2EFollower,
) -> None:
    """Battery checks 1 and 2 — the per-writer colour cue and the border
    restore on `stop` (commands.cmd_stop clears the surface BEFORE killing the
    follower pane: pane-border-status is a WINDOW option, and unsetting it
    through the killed pane's id cannot resolve its target).

    Writer 1 (the session alone) leaves the follower border neutral; writer 2,
    a subagent, tints the FOLLOWER pane's border — both border styles, to its
    palette colour by position: writers are [e2e, rev1], so rev1 is
    PALETTE[1], colour78 — turns the window's border-status bar on and titles
    the pane with its agent_type. Read with show-options after each hook has
    exited, since mid-animation the "Writing..." cue owns title and bar.
    Whether colour78 is distinguishable in the user's own theme stays in the
    battery: show-options proves the value, not the pixels."""
    world.start("tmux", "lento")
    follower = world.follower_target()
    path = world.workdir / "cue.py"

    # Writer 1 is watched WHILE it animates, not only after: the completed
    # animation's refresh clears the surface whenever fewer than two writers
    # exist, so a cue wrongly applied to a lone writer would be gone by the
    # time its hook exits — while the user watched it tinted the whole time.
    writer1 = (FIXTURES / "cue-writer1.py").read_text()
    world.cli("hook", "pre", stdin=payload("Write", path))
    path.write_text(writer1)
    proc = world.cli_background("hook", "post", stdin=payload("Write", path))
    styles_while_running: list[str] = []
    while proc.poll() is None:
        if world.animating_state() == "running":
            styles_while_running.append(_pane_option(world, follower, "pane-border-style"))
        time.sleep(0.05)
    world.wait_for_hook_exit(proc)
    assert len(styles_while_running) >= 10, (
        f"only {len(styles_while_running)} samples during the animation: nothing was watched"
    )
    assert set(styles_while_running) == {""}, (
        f"a lone writer tinted the border mid-animation: {set(styles_while_running)}"
    )
    assert world.vim_buffer_bytes(follower) == writer1.encode(), "writer 1 did not animate"
    assert _pane_option(world, follower, "pane-border-style") == ""
    assert _pane_option(world, follower, "pane-active-border-style") == ""
    assert _window_option(world, follower, "pane-border-status") == ""

    _write_through_hooks(world, path, (FIXTURES / "cue-writer2.py").read_text(), REVIEWER)
    assert _pane_option(world, follower, "pane-border-style") == "fg=colour78"
    assert _pane_option(world, follower, "pane-active-border-style") == "fg=colour78"
    assert _window_option(world, follower, "pane-border-status") == "top"
    title = world.tmux("display-message", "-p", "-t", follower, "#{pane_title}").stdout.strip()
    assert title == "code-reviewer"

    # Check 2. The precondition above (status "top") is what makes the
    # restore below measure something.
    world.cli("stop")
    assert _all_panes(world) == [world.origin_pane], "the follower pane survived stop"
    assert _window_option(world, world.origin_pane, "pane-border-status") == ""
    assert _pane_option(world, world.origin_pane, "pane-border-style") == ""
    assert _pane_option(world, world.origin_pane, "pane-active-border-style") == ""


# --------------------------------------------------------------- Check 4


def _vim_listed_buffers(world: E2EFollower, pane: str) -> list[Path]:
    """Every LISTED buffer's name in the Vim in `pane`, resolved. Written by
    Vim itself behind a sentinel first line, so a stale or half-written dump
    cannot pass for an answer; `:w!`-style keys, like vim_buffer_bytes."""
    out = world.workdir / f"bufs-{pane.lstrip('%')}-{time.monotonic_ns()}.txt"
    command = (
        f":call writefile(['BUFS'] + map(getbufinfo({{'buflisted': 1}}), "
        f"'fnamemodify(v:val.name, \":p\")'), '{out}')"
    )
    world.tmux("send-keys", "-t", pane, "Escape", "Escape")
    world.tmux("send-keys", "-t", pane, "-l", "--", command)
    world.tmux("send-keys", "-t", pane, "Enter")
    world.wait_until(
        lambda: out.exists() and out.read_text().startswith("BUFS\n"),
        f"the buffer list dump at {out}",
        timeout=10.0,
    )
    names = out.read_text().splitlines()[1:]
    out.unlink()
    return [Path(name).resolve() for name in names if name]


def test_two_windows_never_bleed_content_into_each_others_follower(
    world: E2EFollower,
) -> None:
    """Battery check 4 — window-scoped identity (the 2026-07-16 incident:
    one window's edits animated into another window's follower). Two windows
    of one tmux server, a follower in each, a distinct file written through
    each window's own hooks (TMUX_PANE is what scopes them). Each follower's
    Vim must list exactly its own file and hold exactly its bytes — a content
    claim, read from the buffers, not from the screen."""
    world.start("tmux", "instant")
    follower0 = world.follower_target()
    pane1 = world.tmux(
        "new-window", "-d", "-t", world.session, "-P", "-F", "#{pane_id}", "sh"
    ).stdout.strip()
    window1 = world.tmux("display-message", "-p", "-t", pane1, "#{window_id}").stdout.strip()
    assert window1 != world.window_id
    env1 = world.env_with(TMUX_PANE=pane1)
    world.cli("start", "--backend", "tmux", "--speed", "instant", env=env1)
    state1 = json.loads((world.cache_dir / f"{window1}.pane").read_text())
    follower1 = state1["target"]
    assert follower1 not in (follower0, pane1, world.origin_pane)

    alpha = world.workdir / "window_alpha.py"
    beta = world.workdir / "window_beta.py"
    alpha_text = (FIXTURES / "window-alpha.py").read_text()
    beta_text = (FIXTURES / "window-beta.py").read_text()
    _write_through_hooks(world, alpha, alpha_text)
    _write_through_hooks(world, beta, beta_text, env=env1)

    assert _vim_listed_buffers(world, follower0) == [alpha.resolve()]
    assert _vim_listed_buffers(world, follower1) == [beta.resolve()]
    assert world.vim_buffer_bytes(follower0) == alpha_text.encode()
    assert world.vim_buffer_bytes(follower1) == beta_text.encode()


# --------------------------------------------------------------- Check 5

# One atomic snapshot of the follower nvim: nvim runs a request to completion
# before the next, so the lines, cursor, typing-line extmarks and floats come
# from the SAME instant — separate RPC reads could straddle a keystroke of the
# animation and pair a cursor with the wrong line.
_NVIM_SAMPLE_LUA = """
local buf = vim.api.nvim_get_current_buf()
local ns = vim.api.nvim_create_namespace('vaf')
local rows = {}
for _, m in ipairs(vim.api.nvim_buf_get_extmarks(buf, ns, 0, -1, {details = true})) do
  if m[4].line_hl_group == 'VafTypingLine' then table.insert(rows, m[2]) end
end
local floats = {}
for _, win in ipairs(vim.api.nvim_list_wins()) do
  local cfg = vim.api.nvim_win_get_config(win)
  if cfg.relative ~= '' then
    local title = cfg.title
    if type(title) == 'table' then
      local parts = {}
      for _, chunk in ipairs(title) do
        table.insert(parts, chunk[1] .. '@' .. (chunk[2] or ''))
      end
      title = table.concat(parts, '|')
    end
    table.insert(floats, {title = title or '', winhighlight = vim.wo[win].winhighlight})
  end
end
return {
  name = vim.api.nvim_buf_get_name(buf),
  lines = vim.api.nvim_buf_get_lines(buf, 0, -1, true),
  cursor_row = vim.api.nvim_win_get_cursor(0)[1] - 1,
  typing_rows = rows,
  floats = floats,
}
"""


def _sample_while_running(
    world: E2EFollower,
    sock: str,
    proc: subprocess.Popen[bytes],
    path: Path,
    running: Callable[[], bool] | None = None,
) -> list[dict[str, Any]]:
    """Every snapshot of `path`'s buffer taken while the hook animates it.
    `running` defaults to this world's window's marker reading "running"."""
    nvim = world._nvim(sock)
    wanted = path.resolve()
    if running is None:
        running = lambda: world.animating_state() == "running"  # noqa: E731
    samples: list[dict[str, Any]] = []
    while proc.poll() is None:
        if running():
            sample = nvim.exec_lua(_NVIM_SAMPLE_LUA, [])
            if sample["name"] and Path(sample["name"]).resolve() == wanted:
                samples.append(sample)
        time.sleep(0.03)
    world.wait_for_hook_exit(proc)
    return samples


def _strict_prefix_rows(sample: dict[str, Any], final: list[str]) -> list[int]:
    """Rows holding a non-empty STRICT prefix of their final text: a line
    caught part-way through being typed."""
    return [
        row
        for row, text in enumerate(sample["lines"])
        if row < len(final) and text and text != final[row] and final[row].startswith(text)
    ]


# No line is a prefix of another, so a strict prefix can only be a line
# caught mid-typing, never a whole different line.
WRITER1_LINES = [
    "def area(width, height):",
    "    product = width * height",
    "    return product",
]
WRITER2_LINES = ["REVIEWED = True"]


def test_nvim_types_char_by_char_with_the_cursor_and_shows_the_writer_float(
    world: E2EFollower,
) -> None:
    """Battery check 5 — the nvim backend types character by character with
    the typed line highlighted and the cursor on it, and an attributed second
    writer gets the floating cue: its agent_type in the title, the border
    tinted VafWriterCue, whose gui colour is colour78's #5fd787.

    Sampled over RPC at `lento`. The char-granularity claim is what the
    `instant` speed (whole-line inserts) must fail, which is how the
    instrument is shown to tell per-char from per-line. Whether the typing
    LOOKS smooth and the tint is visible in a real colourscheme stays in the
    battery."""
    world.start("nvim", "lento")
    sock = world.follower_target()
    first = world.workdir / "typed.py"
    world.cli("hook", "pre", stdin=payload("Write", first))
    first.write_text("".join(line + "\n" for line in WRITER1_LINES))
    proc = world.cli_background("hook", "post", stdin=payload("Write", first))
    samples = _sample_while_running(world, sock, proc, first)

    caught = [s for s in samples if _strict_prefix_rows(s, WRITER1_LINES)]
    assert caught, (
        f"no sample caught a line part-way typed ({len(samples)} samples): "
        "the lines landed whole, not character by character"
    )
    # The cursor follows the line being typed. Only rows with 2+ characters
    # typed: the first character's buf_set_text lands just before its
    # win_set_cursor, and an atomic sample can fall between the two.
    typing = [
        s
        for s in samples
        if len(s["typing_rows"]) == 1 and len(s["lines"][s["typing_rows"][0]]) >= 2
    ]
    assert len(typing) >= 5, f"only {len(typing)} samples of a line being typed"
    for s in typing:
        assert s["cursor_row"] == s["typing_rows"][0], f"cursor off the typed line: {s}"
        assert _strict_prefix_rows(s, WRITER1_LINES) in ([], s["typing_rows"]), s
    # Writer 1 is alone: its float (the "Writing..." cue) has the default
    # title and no writer tint.
    for s in samples:
        for float_ in s["floats"]:
            assert "code-reviewer" not in float_["title"], float_
            assert "VafWriterCue" not in float_["winhighlight"], float_
    assert world.nvim_buffer_lines(sock, first) == WRITER1_LINES

    second = world.workdir / "reviewed.py"
    world.cli("hook", "pre", stdin=payload("Write", second, identity=REVIEWER))
    second.write_text("".join(line + "\n" for line in WRITER2_LINES))
    proc = world.cli_background("hook", "post", stdin=payload("Write", second, identity=REVIEWER))
    samples = _sample_while_running(world, sock, proc, second)
    during = [f for s in samples for f in s["floats"] if "code-reviewer" in f["title"]]
    assert during, "writer 2's float never showed its label while it animated"

    nvim = world._nvim(sock)
    [after] = nvim.exec_lua(_NVIM_SAMPLE_LUA, [])["floats"]
    assert "code-reviewer" in after["title"] and "@VafWriterCue" in after["title"], after
    assert "FloatBorder:VafWriterCue" in after["winhighlight"], after
    assert nvim.api.get_hl(0, {"name": "VafWriterCue"}).get("fg") == 0x5FD787


# --------------------------------------------------------------- Check 6


def _text(*lines: str) -> str:
    """Terminated, never joined: a join round-trip loses trailing blanks."""
    return "".join(line + "\n" for line in lines)


# PEP 8 double blanks between defs: the rows a replay has to get right.
CONTROLS = _text(
    "import os",
    "",
    "",
    "def alpha(root):",
    "    first = 'ALPHA_MARK'",
    "    return os.path.join(root, first)",
    "",
    "",
    "def bravo(root):",
    "    second = 'BRAVO_MARK'",
    "    return second",
)


def _nvim_animation(world: E2EFollower, path: Path) -> tuple[str, subprocess.Popen[bytes]]:
    """A launched nvim follower at `normal`, animating CONTROLS into `path`
    in a background hook, far enough in that ALPHA_MARK is on screen."""
    world.start("nvim", "normal")
    sock = world.follower_target()
    world.cli("hook", "pre", stdin=payload("Write", path))
    path.write_text(CONTROLS)
    proc = world.cli_background("hook", "post", stdin=payload("Write", path))
    world.wait_for_animating("running")
    world.wait_until(
        lambda: "ALPHA_MARK" in "".join(world.nvim_buffer_lines(sock, path)),
        "ALPHA_MARK to be typed",
        timeout=60.0,
    )
    return sock, proc


def _interrupt_and_take_over(world: E2EFollower, sock: str, path: Path) -> Any:
    """`claude-follow interrupt` (prefix S), then prove the user really owns
    the buffer: it is the current one, and modifiable."""
    world.cli("interrupt")
    world.wait_for_animating("handoff")
    nvim = world._nvim(sock)
    current = Path(nvim.api.buf_get_name(nvim.api.get_current_buf())).resolve()
    assert current == path.resolve(), f"the current buffer is {current}, not the file"
    assert nvim.eval("&modifiable") == 1, "the interrupted buffer is still locked"
    return nvim


def test_nvim_pause_then_resume_through_the_cli_ends_on_the_exact_bytes(
    world: E2EFollower,
) -> None:
    """Battery check 6, pause/resume — `claude-follow pause` twice (prefix P
    twice). The pause really halts (two reads 0.6 s apart agree, and the
    buffer is short of the content: without that, a pause that never landed
    would pass), and the resume runs on to exactly the file Claude wrote.
    On nvim a pause can land mid-line — it is checked per character."""
    path = world.workdir / "paused.py"
    sock, proc = _nvim_animation(world, path)

    world.cli("pause")
    world.wait_for_animating("paused")
    held = world.nvim_buffer_bytes(sock, path)
    time.sleep(0.6)
    assert world.nvim_buffer_bytes(sock, path) == held, "typing continued while paused"
    assert held is not None and held != CONTROLS.encode(), "the pause landed after the end"

    world.cli("pause")
    world.wait_for_hook_exit(proc)
    assert world.nvim_buffer_bytes(sock, path) == CONTROLS.encode()


def test_nvim_interrupt_then_save_releases_the_hook_with_its_notification(
    world: E2EFollower,
) -> None:
    """Battery check 6, interrupt — prefix S hands the buffer over, and the
    user's own `:w` releases Claude's turn: the hook exits 0, its output is
    the notification telling Claude the user saved their version, the file
    on disk is the user's, and no crash-fallback remainder is left."""
    path = world.workdir / "handed.py"
    sock, proc = _nvim_animation(world, path)
    nvim = _interrupt_and_take_over(world, sock, path)

    nvim.api.buf_set_lines(0, -1, -1, True, ["USER_LINE = 1"])
    nvim.command("w")
    world.wait_for_hook_exit(proc)

    assert "USER_LINE = 1" in path.read_text()
    output = world.background_log(proc)
    assert "SAVED their own version" in output, f"no release notification: {output!r}"
    assert not world.pending_path.exists(), "the released hand-off left a remainder behind"


def test_nvim_des_interrupt_discards_the_users_unsaved_typing(world: E2EFollower) -> None:
    """Battery check 6, des-interrupt — after an interrupt the user types but
    does NOT save; a second prefix S throws that typing away and replays the
    rest of the animation onto exactly the content, double blanks and all."""
    path = world.workdir / "discarded.py"
    sock, proc = _nvim_animation(world, path)
    nvim = _interrupt_and_take_over(world, sock, path)

    nvim.api.buf_set_lines(0, -1, -1, True, ["JUNK_TYPED = 1"])
    assert "JUNK_TYPED = 1" in world.nvim_buffer_lines(sock, path)
    world.cli("interrupt")  # the des-interrupt
    world.wait_for_hook_exit(proc)

    assert world.nvim_buffer_bytes(sock, path) == CONTROLS.encode()
    assert path.read_text() == CONTROLS, "the unsaved typing reached the disk"


# ------------------------------------------------------ Checks 3, 7, 8

# A key-free observer, loaded from the isolated HOME's .vimrc into the
# follower's own Vim: a 15 ms timer appends every DISTINCT (buffer name,
# lines) state to a JSON-lines log. It sends no key into the pane, so the
# animation under test is untouched. The relock's closing `:e!` reloads the
# right file from disk, so an end-state dump is blind to what went wrong
# mid-animation; this sees the middle. A missed sample can hide a bad state,
# never invent one (tests/test_integration_edit_no_reload.py has the proof).
_OBSERVER_VIMRC = r"""
let g:vaf_obs_log = '{log}'
let g:vaf_obs_last = ''
function! VafObsTick(timer) abort
  let l:state = json_encode({{'name': expand('%:t'), 'lines': getline(1, '$')}})
  if l:state !=# g:vaf_obs_last
    let g:vaf_obs_last = l:state
    call writefile([l:state], g:vaf_obs_log, 'a')
  endif
endfunction
call timer_start(15, 'VafObsTick', {{'repeat': -1}})
"""


def _observed_states(log: Path) -> list[dict[str, Any]]:
    if not log.exists():
        return []
    return [json.loads(raw) for raw in log.read_text().splitlines() if raw]


def _vim_eval_dump(world: E2EFollower, pane: str, expression: str) -> str:
    """One Vim expression's value, written by Vim itself behind a sentinel."""
    out = world.workdir / f"eval-{time.monotonic_ns()}.txt"
    world.tmux("send-keys", "-t", pane, "Escape", "Escape")
    world.tmux(
        "send-keys", "-t", pane, "-l", "--", f":call writefile(['EVAL', {expression}], '{out}')"
    )
    world.tmux("send-keys", "-t", pane, "Enter")
    world.wait_until(
        lambda: out.exists() and out.read_text().startswith("EVAL\n"),
        f"Vim's answer for {expression}",
        timeout=10.0,
    )
    [value] = out.read_text().splitlines()[1:]
    out.unlink()
    return value


def _stray_lines(states: list[dict[str, Any]], contents: dict[str, list[str]]) -> list[str]:
    """Every observed line that is neither empty nor a prefix of a line of
    the file its buffer is named after — garble, a leaked `o`/`O` opener, a
    literal `:Nd`, another file's token."""
    stray: list[str] = []
    for state in states:
        wanted = contents.get(state["name"])
        if wanted is None:
            continue
        for line in state["lines"]:
            if line and not any(target.startswith(line) for target in wanted):
                stray.append(f"{state['name']}: {line!r}")
    return stray


PARALLEL_TAGS = ("alpha", "bravo", "charlie", "delta", "echo", "foxtrot")


def test_six_parallel_hooks_animate_exactly_one_file_cleanly(world: E2EFollower) -> None:
    """Battery check 7 — guards e51071e (the O_EXCL animation-slot claim).
    Six `hook post` processes fired back to back, as six Write tool calls in
    one turn fire them. Exactly one animates; the other five skip without
    touching the pane. Before the claim was atomic all six passed a
    check-then-act guard and interleaved their keystrokes: lines mixing two
    files' tokens, literal `o`/`O` openers typed as text.

    Non-vacuity: every loser must exit while the winner's marker still reads
    "running" — otherwise they merely ran after it, and there was no race."""
    log = world.workdir / "observer.log"
    world.set_vimrc(_OBSERVER_VIMRC.format(log=log))
    world.start("tmux", "lento")
    follower = world.follower_target()
    world.wait_until(log.exists, "the observer timer to start", timeout=15.0)

    contents = {
        f"{tag}.py": [f"{tag}_{i} = '{tag.upper()}_{i}'" for i in range(8)] for tag in PARALLEL_TAGS
    }
    paths = {name: world.workdir / name for name in contents}
    for name, path in paths.items():
        world.cli("hook", "pre", stdin=payload("Write", path))
        path.write_text(_text(*contents[name]))
    procs = [
        world.cli_background("hook", "post", stdin=payload("Write", path))
        for path in paths.values()
    ]

    marker_at_exit: dict[int, str | None] = {}
    deadline = time.monotonic() + 120.0
    while len(marker_at_exit) < len(procs) and time.monotonic() < deadline:
        for proc in procs:
            if proc.pid not in marker_at_exit and proc.poll() is not None:
                marker_at_exit[proc.pid] = world.animating_state()
        time.sleep(0.01)
    for proc in procs:
        world.wait_for_hook_exit(proc)
    racing = [pid for pid, state in marker_at_exit.items() if state == "running"]
    assert len(racing) == 5, (
        f"only {len(racing)} hooks exited while another was animating: {marker_at_exit}"
    )

    states = _observed_states(log)
    animated = {s["name"] for s in states if s["name"] in contents and any(s["lines"])}
    assert len(animated) == 1, f"more than one file reached the follower: {animated}"
    [winner] = animated
    assert _stray_lines(states, contents) == []
    assert len([s for s in states if s["name"] == winner]) >= 3, "the animation was not observed"
    assert _vim_eval_dump(world, follower, "tabpagenr('$')") == "1"
    assert world.vim_buffer_bytes(follower) == _text(*contents[winner]).encode()
    state = json.loads((world.cache_dir / f"{world.window_id}.pane").read_text())
    assert [Path(p).name for p in state["open_files"]] == [winner]


# --------------------------------------------------------------- Check 9

_NVIM_QT_SHIM = """#!/bin/sh
# Stands in for the nvim-qt GUI: argv is `-- --listen <socket>`. Starts a
# headless nvim on that socket (what the GUI would host) and returns at once,
# as the GUI launcher does.
echo "$@" > "{marker}"
nvim --headless --listen "$3" </dev/null >/dev/null 2>&1 &
exit 0
"""

# osascript opens Terminal.app/iTerm2 windows and `open -a` a GUI app. Neither
# may ever reach the real thing from a test: these fail LOUDLY and leave a
# marker, so a launcher that falls past the nvim-qt tier is a red test, never
# a window on the developer's screen.
_FAIL_LOUD_SHIM = """#!/bin/sh
echo "$0 $*" > "{marker}"
echo "e2e: refusing to run $0 — a test must never open a real window" >&2
exit 1
"""


def _install_shims(world: E2EFollower) -> tuple[Path, dict[str, Path]]:
    shims = world.workdir / "shims"
    shims.mkdir()
    markers = {name: world.workdir / f"{name}.called" for name in ("nvim-qt", "osascript", "open")}
    for name, marker in markers.items():
        template = _NVIM_QT_SHIM if name == "nvim-qt" else _FAIL_LOUD_SHIM
        shim = shims / name
        shim.write_text(template.format(marker=marker))
        shim.chmod(0o755)
    return shims, markers


def test_standalone_nvim_starts_animates_stays_open_and_stops_without_tmux(
    world: E2EFollower,
) -> None:
    """Battery check 9 — guards fe87f5b (no-tmux support). From a terminal
    outside tmux with `backend: nvim`, `nvim_window: auto`: `start` returns
    promptly having launched the GUI tier (an nvim-qt shim hosting a headless
    nvim) and keyed the follower by the terminal (term-e2e), a hook animates
    the file into it character by character, the nvim is still alive after
    the edit, and `stop` quits it.

    No TMUX_PANE, so session resolution scans `tmux list-panes -a` for an
    ancestor pane; TMUX_TMPDIR stays private so that scan sees only this
    world's server. What stays in the battery is the visible surface: an
    iTerm2 split beside the shell, and its Automation permission prompt."""
    _write_config(world, {"backend": "nvim", "nvim_window": "auto"})
    shims, markers = _install_shims(world)
    outside = world.env_with(
        drop=("TMUX_PANE",), TERM_SESSION_ID="e2e", PATH=f"{shims}:{world.env['PATH']}"
    )
    for name in markers:
        assert shutil.which(name, path=outside["PATH"]) == str(shims / name), (
            f"{name} does not resolve to its shim: a real one could open a window"
        )
    _assert_private_server(world)
    sock = str(world.cache_dir / "nvim-term-e2e.sock")
    try:
        began = time.monotonic()
        result = world.cli("start", "--speed", "lento", env=outside)
        assert time.monotonic() - began < 10.0, "start blocked the origin terminal"
        assert result.stdout == f"claude-follow: attached to standalone Neovim at {sock}\n"
        assert markers["nvim-qt"].exists(), "the nvim-qt tier was not the one launched"
        assert not markers["osascript"].exists() and not markers["open"].exists()
        state = json.loads((world.cache_dir / "term-e2e.pane").read_text())
        assert state["target"] == sock
        assert list(world.cache_dir.glob("@*.pane")) == [], (
            "a tmux-window identity was resolved: the ancestry scan reached a pane"
        )
        assert Path(sock).is_socket(), "the launched nvim is not listening on the socket"

        path = world.workdir / "standalone.py"
        world.cli("hook", "pre", stdin=payload("Write", path), env=outside)
        path.write_text(_text(*WRITER1_LINES))
        proc = world.cli_background("hook", "post", stdin=payload("Write", path), env=outside)
        samples = _sample_while_running(world, sock, proc, path, running=lambda: True)
        assert any(_strict_prefix_rows(s, WRITER1_LINES) for s in samples), (
            f"no sample caught a line part-way typed ({len(samples)} samples)"
        )
        assert world.nvim_buffer_bytes(sock, path) == _text(*WRITER1_LINES).encode()
        assert world._nvim(sock).eval("1") == 1, "the standalone nvim closed after the edit"

        world.cli("stop", env=outside)
        world.wait_until(
            lambda: subprocess.run(["pgrep", "-f", sock], capture_output=True).returncode == 1,
            "the standalone nvim to quit on stop",
            timeout=10.0,
        )
        assert not (world.cache_dir / "term-e2e.pane").exists()
    finally:
        # The shim's nvim is not in any tmux pane, so the world's kill-server
        # teardown cannot reach it.
        subprocess.run(["pkill", "-f", sock], check=False)


# --------------------------------------------------------------- Check 10


def test_tmux_backend_outside_tmux_fails_loudly_and_opens_nothing(world: E2EFollower) -> None:
    """Battery check 10 — guards fe87f5b. `start` with `backend: tmux` from a
    terminal outside tmux: the exact actionable message on STDERR (an error,
    like its sibling "could not open a standalone nvim window"), nothing on
    stdout (so no traceback anywhere), exit 1, and nothing opened — no pane,
    no follower state, no nvim socket.

    Driven through the CONFIG FILE, as the battery does, not a --backend flag.
    Outside tmux means no TMUX_PANE; TMUX_TMPDIR stays private, because the
    pid-ancestry scan that runs without TMUX_PANE asks tmux for every pane."""
    _write_config(world, {"backend": "tmux"})
    _assert_private_server(world)
    outside = world.env_with(drop=("TMUX_PANE",), TERM_SESSION_ID="e2e")
    panes_before = _all_panes(world)

    result = world.cli("start", expect_rc=1, env=outside)

    assert result.stderr == VIM_NEEDS_TMUX + "\n"
    assert result.stdout == ""
    assert _all_panes(world) == panes_before, "a pane opened"
    assert _follower_leftovers(world) == [], "start left follower state behind"
