"""A plugin message must never freeze a dedicated nvim follower.

The follower's nvim loads the user's whole config on purpose (gruvbox), so a
FileType autocmd — an LSP autostart, a linter, any plugin — runs inside the
follower's own RPC calls. When such a plugin prints a message taller than the
command line, nvim raises a hit-enter prompt, and the RPC call that triggered
it does not return until someone presses Enter (measured 2026-09-25 with a
mason x86_64 `ruff` on a Mac without Rosetta: "Unknown system error -86").

Only a real UI reproduces it — headless nvim never raises the prompt — so each
test runs nvim in a pane of a PRIVATE tmux server (the `tmux_session` fixture,
with TMUX and TMUX_PANE unset), 49 columns wide, the follower pane's real
width, 30 rows. Four triggers, parametrized:

- `lsp`: an LSP whose `cmd` is an EMPTY executable file. The spawn fails
  (ENOEXEC rather than -86, same message path, same prompt). A `cmd` that does
  not exist does NOT reproduce it: nvim skips a non-executable cmd silently.
- `echo`: a FileType autocmd that echoes ~400 characters, synchronously inside
  the call that fired FileType.
- `deferred`: the same echo via `vim.defer_fn`, delivered AFTER the triggering
  call returned. This is the case that capturing output (`nvim_exec2`) alone
  cannot fix: nothing is left to capture it, and the next unrelated call hangs.
- `tall`: a deferred ~3000-character echo, taller than the pane. nvim pages it
  with the MORE-prompt (mode "rm") instead of the hit-enter prompt; Enter only
  advances it a line, so the follower answers it with <Esc>.

Only `deferred` and `tall` prove the sweep and the watchdog: `lsp` and `echo`
print synchronously inside `filetype detect`/`bufload`, which the follower runs
through `nvim_exec2` with output captured, so no prompt is ever raised there
(measured: with the sweep and watchdog disabled, those two still pass the
freeze checks). They stay as regressions for the capture and its logging.

Every test runs the follower call on a worker thread with join(10), then — like
the next hook would — a second step (`is_alive`, the status cue's `set_state`,
`probe_buffer`) after the deferred message had time to land. A frozen call is
reported as a failure, and the finally-block dismisses the prompt over a
separate probe channel so the worker and the teardown can never hang."""

from __future__ import annotations

import contextlib
import logging
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import pytest

pynvim = pytest.importorskip("pynvim")

from vim_ai_follower import state  # noqa: E402
from vim_ai_follower.backends import nvim_prompt  # noqa: E402
from vim_ai_follower.backends.nvim import NvimFollower  # noqa: E402
from vim_ai_follower.status_surface import NvimStatusSurface  # noqa: E402

_JOIN_SECONDS = 10.0
_LONG = "string.rep('long plugin message ', 20)"
_ECHO = "vim.api.nvim_echo({ { 'vaf-probe: ' .. " + _LONG + " } }, true, {})"
_TALL = (
    "vim.api.nvim_echo({ { 'vaf-probe: ' .. string.rep('long plugin message ', 150) } }, true, {})"
)

_TRIGGERS = {
    "lsp": (
        "vim.lsp.config('broken', { cmd = { '{exe}' }, filetypes = { 'python' } })\n"
        "vim.lsp.enable('broken')\n"
    ),
    "echo": (
        "vim.api.nvim_create_autocmd('FileType', { pattern = 'python', callback = function()\n"
        f"  {_ECHO}\n"
        "end })\n"
    ),
    "deferred": (
        "vim.api.nvim_create_autocmd('FileType', { pattern = 'python', callback = function()\n"
        f"  vim.defer_fn(function() {_ECHO} end, 300)\n"
        "end })\n"
    ),
    "tall": (
        "vim.api.nvim_create_autocmd('FileType', { pattern = 'python', callback = function()\n"
        f"  vim.defer_fn(function() {_TALL} end, 300)\n"
        "end })\n"
    ),
}

_CONTENT = "x = 1\ny = 'é'\n"
_TRIGGERS["none"] = ""


@pytest.fixture
def ui_nvim(tmux_session: str, monkeypatch: pytest.MonkeyPatch) -> Iterator[Callable[[str], str]]:
    """Factory: start a UI nvim with trigger `name` in a 49-column pane of the
    fixture's private tmux server and return its socket. The socket lives
    under a short mkdtemp (the ~104-char Unix socket limit). Teardown kills
    the pane's process by pid as well as through the fixture's kill-server."""
    monkeypatch.delenv("TMUX_PANE", raising=False)
    work = Path(tempfile.mkdtemp(prefix="cf-he-"))
    pids: list[int] = []

    def _start(name: str) -> str:
        exe = work / "emptyexec"
        exe.write_text("")
        exe.chmod(0o755)
        init = work / "init.lua"
        init.write_text(_TRIGGERS[name].replace("{exe}", str(exe)))
        sock = str(work / "n.sock")
        session = f"{tmux_session}-nv"
        subprocess.run(
            [
                "tmux", "new-session", "-d", "-s", session, "-x", "49", "-y", "30",
                f"nvim -u {init} --noplugin -n -i NONE --listen {sock}",
            ],
            check=True,
        )  # fmt: skip
        pid = subprocess.run(
            ["tmux", "display", "-p", "-t", session, "#{pane_pid}"],
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()
        pids.append(int(pid))
        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline and not Path(sock).exists():
            time.sleep(0.05)
        assert Path(sock).exists(), "nvim never opened its socket"
        return sock

    try:
        yield _start
    finally:
        for pid in pids:
            with contextlib.suppress(ProcessLookupError):
                os.kill(pid, signal.SIGKILL)
        shutil.rmtree(work, ignore_errors=True)


def _in_thread(fn: Callable[[], Any]) -> tuple[bool, dict[str, Any]]:
    box: dict[str, Any] = {}

    def run() -> None:
        try:
            box["value"] = fn()
        except Exception as exc:  # pragma: no cover - surfaced by the assert below
            box["error"] = exc

    worker = threading.Thread(target=run, daemon=True)
    worker.start()
    worker.join(_JOIN_SECONDS)
    return not worker.is_alive(), box


def _unfreeze(probe: Any) -> None:
    """Dismiss any hit-enter or more-prompt so a frozen worker (and teardown)
    can finish. get_mode and input are FAST requests: nvim answers them even
    while a prompt blocks every other call."""
    for _ in range(20):
        mode = probe.api.get_mode()
        if not mode["blocking"]:
            return
        # <Esc> for anything but hit-enter: it quits a more-prompt, and it
        # also ends a stray `q` macro recording (blocking normal mode).
        probe.api.input("<CR>" if mode["mode"] == "r" else "<Esc>")
        time.sleep(0.1)


@pytest.fixture
def watchdog_channels(monkeypatch: pytest.MonkeyPatch) -> list[int]:
    """The nvim channel id of every connection a watchdog opened, recorded by
    wrapping the connect it is handed — so the test can prove each one was
    closed again."""
    opened: list[int] = []
    real_init = nvim_prompt.Watchdog.__init__

    def spy(self: nvim_prompt.Watchdog, connect: Callable[[], Any], label: str, key: str) -> None:
        def recording() -> Any:
            conn = connect()
            opened.append(conn.channel_id)
            return conn

        real_init(self, recording, label, key)

    monkeypatch.setattr(nvim_prompt.Watchdog, "__init__", spy)
    return opened


def _buffer_bytes(probe: Any, path: str) -> list[bytes]:
    bufnr = probe.funcs.bufnr(path)
    lines = probe.api.buf_get_lines(bufnr, 0, -1, True)
    return [line.encode("utf-8", "surrogateescape") for line in lines]


@pytest.mark.integration
@pytest.mark.parametrize("trigger", ["lsp", "echo", "deferred", "tall"])
@pytest.mark.parametrize("entry", ["show_fresh", "ensure_showing"])
def test_a_plugin_prompt_never_freezes_the_dedicated_follower(
    ui_nvim: Callable[[str], str],
    tmp_path: Path,
    trigger: str,
    entry: str,
    watchdog_channels: list[int],
    wait_until: Callable[..., bool],
    caplog: pytest.LogCaptureFixture,
) -> None:
    sock = ui_nvim(trigger)
    probe = pynvim.attach("socket", path=sock)
    target = tmp_path / "a.py"
    target.write_text(_CONTENT)
    # A dedicated follower has state: without it the guard fails CLOSED.
    state.FollowerState.set("@1", "nvim", sock)
    follower = NvimFollower(socket_path=sock, window_id="@1", pace_seconds=0.05)
    try:
        if entry == "show_fresh":
            finished, box = _in_thread(lambda: follower.show_fresh(str(target), _CONTENT))
        else:
            finished, box = _in_thread(lambda: follower.ensure_showing(str(target)))
        assert finished, f"{entry} froze on a hit-enter prompt: {probe.api.get_mode()}"
        assert "error" not in box, box.get("error")

        # The next hook, in a hook's own order — liveness, the status cue,
        # then the follower: a deferred message has landed by now.
        time.sleep(0.8)

        def next_hook() -> tuple[bool, str]:
            alive = follower.is_alive()
            NvimStatusSurface(socket_path=sock).set_state("Writing...")
            return alive, follower.probe_buffer(str(target), _CONTENT)

        finished, box = _in_thread(next_hook)
        assert finished, f"the next hook froze on a hit-enter prompt: {probe.api.get_mode()}"
        assert box.get("value") == (True, "holds")

        # Mode first: a non-fast read would itself hang behind a prompt.
        assert probe.api.get_mode()["blocking"] is False
        assert _buffer_bytes(probe, str(target)) == [b"x = 1", "y = 'é'".encode()]

        # What the prompt said reached hook.log (captured output or the
        # :messages tail logged with a dismissal).
        logged = " ".join(r.getMessage() for r in caplog.records if r.name == "vim_ai_follower")
        assert ("Spawning language server" if trigger == "lsp" else "vaf-probe") in logged, logged

        # Nothing left behind: every watchdog thread ended and every
        # connection it opened is gone from nvim's channel list.
        assert watchdog_channels
        assert not [t for t in threading.enumerate() if t.name == nvim_prompt.WATCHDOG_THREAD_NAME]
        assert wait_until(
            lambda: not {c["id"] for c in probe.api.list_chans()} & set(watchdog_channels)
        ), (watchdog_channels, probe.api.list_chans())
    finally:
        _unfreeze(probe)


def _usable(nvim: Any) -> bool:
    """nvim is not blocked and an ordinary (non-fast) call completes."""
    if nvim.api.get_mode()["blocking"]:
        return False
    finished, _box = _in_thread(lambda: nvim.command("let g:vaf_usable = 1"))
    return finished


@pytest.mark.integration
@pytest.mark.parametrize("prompt_mode", sorted(nvim_prompt._ANSWERS))
def test_a_spare_answer_leaves_nvim_usable(ui_nvim: Callable[[str], str], prompt_mode: str) -> None:
    # The answer lands AFTER the prompt is gone — a second guard (another hook
    # process) answered the same prompt, or the user pressed the key at the
    # same moment — so nvim reads it as a normal-mode key. It must not leave
    # nvim blocked: `q` did (it starts macro recording, get_mode ->
    # {'mode': 'n', 'blocking': True}) and every later RPC hung, answered by
    # nothing, since neither the sweep nor the watchdog touches mode "n".
    probe = pynvim.attach("socket", path=ui_nvim("none"))
    try:
        assert probe.api.get_mode() == {"mode": "n", "blocking": False}
        probe.api.input(nvim_prompt._ANSWERS[prompt_mode])
        time.sleep(0.2)
        assert _usable(probe), probe.api.get_mode()
    finally:
        _unfreeze(probe)


@pytest.mark.integration
def test_two_connections_both_answering_one_more_prompt_leave_nvim_usable(
    ui_nvim: Callable[[str], str],
) -> None:
    # Two guards on one socket (two hook processes) both read the same
    # more-prompt before either answer lands, and both answer it. Driven
    # deterministically: both reads happen before either key is sent. The
    # racy form — two real Watchdogs against many prompts — froze 4/40 with
    # `q` in the reviewer's run (scratchpad/rr/exp.py E3), but its outcome
    # depends on thread scheduling, so it is not a test; this is its
    # worst-case interleaving, pinned.
    sock = ui_nvim("none")
    probe = pynvim.attach("socket", path=sock)
    a = pynvim.attach("socket", path=sock)
    b = pynvim.attach("socket", path=sock)
    try:
        probe.exec_lua("vim.defer_fn(function() " + _TALL + " end, 50)")
        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline and probe.api.get_mode()["mode"] != "rm":
            time.sleep(0.02)
        more = {"mode": "rm", "blocking": True}
        assert a.api.get_mode() == more
        assert b.api.get_mode() == more
        a.api.input(nvim_prompt._ANSWERS["rm"])
        b.api.input(nvim_prompt._ANSWERS["rm"])
        time.sleep(0.3)
        assert _usable(probe), probe.api.get_mode()
    finally:
        _unfreeze(probe)


# The user's config goes through every key that lands in NORMAL mode: a
# spare answer (sent after the prompt it answered is already gone) is
# remapped. With these two maps a spare <Esc> leaves nvim waiting forever
# for the rest of `<Esc><Esc>` (notimeout -> {'mode': 'n', 'blocking': True})
# and a spare <CR> edits the buffer. An answer made AT the prompt is not
# remapped, so the only defence is that exactly one guard answers.
_REMAPPING_CONFIG = (
    "vim.o.timeout = false\n"
    "vim.keymap.set('n', '<Esc><Esc>', ':nohlsearch<CR>')\n"
    "vim.keymap.set('n', '<CR>', 'o<Esc>')\n"
)
_TRIGGERS["remapping"] = _REMAPPING_CONFIG

# One guard, in its own process. It waits for a prompt, then calls the real
# dismiss_prompt through a spy that counts the keys it sends and holds the
# guards at two file barriers, pinning the race's worst case:
#   1. after its FIRST prompt read, until every guard has seen the prompt;
#   2. just before SENDING, until every other guard has either reached its own
#      send or given up (returned without sending).
# Without the answer lock both guards reach barrier 2 and both send. With it,
# the guard that holds the lock waits at barrier 2 still holding it, so the
# other one finds the lock busy and gives up — deterministically.
_GUARD_SCRIPT = """
import sys, time
from pathlib import Path
import pynvim
from vim_ai_follower import cache
from vim_ai_follower.backends import nvim_prompt

sock, cache_dir, barriers, me, peers = sys.argv[1:6]
cache.CACHE_DIR = Path(cache_dir)
seen, send = Path(barriers, "seen"), Path(barriers, "send")
real = pynvim.attach("socket", path=sock)

def wait_for(directory):
    deadline = time.monotonic() + 10
    while len(list(directory.iterdir())) < int(peers):
        assert time.monotonic() < deadline, f"barrier timeout at {directory.name}"
        time.sleep(0.005)

class Spy:
    def __init__(self):
        self.api = self
        self.sent = 0
        self.held = False
    def get_mode(self):
        mode = real.api.get_mode()
        if mode["blocking"] and not self.held:
            self.held = True
            (seen / me).touch()
            wait_for(seen)
        return mode
    def input(self, key):
        (send / me).touch()
        wait_for(send)
        self.sent += 1
        return real.api.input(key)

deadline = time.monotonic() + 10
while not real.api.get_mode()["blocking"]:
    assert time.monotonic() < deadline, "no prompt"
    time.sleep(0.01)
spy = Spy()
nvim_prompt.dismiss_prompt(spy, sock)
(send / me).touch()  # gave up (or already sent): release a peer waiting to send
print(spy.sent)
"""


@pytest.mark.integration
@pytest.mark.parametrize("message", ["hit-enter", "more"])
def test_two_guard_processes_seeing_one_prompt_send_exactly_one_answer(
    ui_nvim: Callable[[str], str], tmp_path: Path, message: str
) -> None:
    sock = ui_nvim("remapping")
    probe = pynvim.attach("socket", path=sock)
    script = tmp_path / "guard.py"
    script.write_text(_GUARD_SCRIPT)
    barriers = tmp_path / "barriers"
    (barriers / "seen").mkdir(parents=True)
    (barriers / "send").mkdir()
    cache_dir = tmp_path / "cache"
    env = {k: v for k, v in os.environ.items() if k not in ("TMUX", "TMUX_PANE")}
    try:
        probe.api.buf_set_lines(0, 0, -1, True, ["keep"])
        guards = [
            subprocess.Popen(
                [sys.executable, str(script), sock, str(cache_dir), str(barriers), name, "2"],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                env=env,
            )
            for name in ("a", "b")
        ]
        echo = _ECHO if message == "hit-enter" else _TALL
        probe.exec_lua("vim.defer_fn(function() " + echo + " end, 300)")
        outputs = [g.communicate(timeout=20) for g in guards]
        assert all(g.returncode == 0 for g in guards), outputs
        time.sleep(0.3)
        # The damage first (mode before any non-fast read), then its cause.
        assert probe.api.get_mode() == {"mode": "n", "blocking": False}
        assert _usable(probe)
        assert probe.api.buf_get_lines(0, 0, -1, True) == ["keep"]
        assert sorted(int(out) for out, _err in outputs) == [0, 1], outputs
    finally:
        _unfreeze(probe)


@pytest.mark.integration
@pytest.mark.parametrize("unusable", ["locks-dir-read-only", "lock-file-read-only"])
def test_an_unusable_answer_lock_still_answers_the_prompt(
    ui_nvim: Callable[[str], str],
    tmp_path: Path,
    unusable: str,
    caplog: pytest.LogCaptureFixture,
) -> None:
    # The answer lock cannot be taken — `answer-locks/` is not writable (mode
    # 0500), or the lock file is 0400 (say, left by a `sudo` run). Treating
    # that like "another guard is answering" meant nobody ever answered: the
    # entry point hung on the prompt, the very freeze the guard exists for.
    # The cache dir is the per-test one (conftest's isolated_dirs).
    sock = ui_nvim("none")
    probe = pynvim.attach("socket", path=sock)
    lock = nvim_prompt._answer_lock_path(sock)
    lock.parent.mkdir(parents=True)
    if unusable == "lock-file-read-only":
        lock.write_text("")
        lock.chmod(0o400)
    else:
        lock.parent.chmod(0o500)
    target = tmp_path / "a.py"
    target.write_text(_CONTENT)
    state.FollowerState.set("@1", "nvim", sock)
    follower = NvimFollower(socket_path=sock, window_id="@1", pace_seconds=0.05)
    try:
        probe.exec_lua("vim.defer_fn(function() " + _ECHO + " end, 50)")
        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline and not probe.api.get_mode()["blocking"]:
            time.sleep(0.02)
        assert probe.api.get_mode() == {"mode": "r", "blocking": True}

        with caplog.at_level(logging.WARNING, logger="vim_ai_follower"):
            finished, box = _in_thread(lambda: follower.show_fresh(str(target), _CONTENT))
        assert finished, f"show_fresh froze on a hit-enter prompt: {probe.api.get_mode()}"
        assert "error" not in box, box.get("error")
        assert probe.api.get_mode()["blocking"] is False
        assert _buffer_bytes(probe, str(target)) == [b"x = 1", "y = 'é'".encode()]
        assert "answer lock unusable" in caplog.text
    finally:
        _unfreeze(probe)
        lock.parent.chmod(0o700)
        with contextlib.suppress(FileNotFoundError):
            lock.chmod(0o600)
