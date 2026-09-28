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
  advances it a line, so the follower answers it with `q`.

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
import os
import shutil
import signal
import subprocess
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
        probe.api.input("q" if mode["mode"] == "rm" else "<CR>")
        time.sleep(0.1)


@pytest.fixture
def watchdog_channels(monkeypatch: pytest.MonkeyPatch) -> list[int]:
    """The nvim channel id of every connection a watchdog opened, recorded by
    wrapping the connect it is handed — so the test can prove each one was
    closed again."""
    opened: list[int] = []
    real_init = nvim_prompt.Watchdog.__init__

    def spy(self: nvim_prompt.Watchdog, connect: Callable[[], Any], label: str) -> None:
        def recording() -> Any:
            conn = connect()
            opened.append(conn.channel_id)
            return conn

        real_init(self, recording, label)

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
