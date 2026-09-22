"""End-to-end harness: drive the REAL `claude-follow` CLI as a subprocess.

Everything else in the suite drives backend classes and hook functions
in-process. This harness exists for the one path none of that reaches: the
`bin/claude-follow` wrapper a user (and the tmux prefix keys) actually
invoke, fed hook payloads as JSON on stdin, writing state/signal/pending
files into a real cache directory.

The three pieces, in dependency order:

1. **Isolation.** `cache.CACHE_DIR` and `config.CONFIG_PATH` are resolved
   from `Path.home()` INSIDE the subprocess, so conftest's `monkeypatch`
   cannot reach them. The only lever is the child's `HOME`, so every child
   — the CLI calls, the tmux server, the vim/nvim the follower launches in
   one of its panes — gets a throwaway one. `assert_isolated()` proves the
   lever works by asking a child what it resolved, rather than assuming it;
   `assert_real_home_untouched()` re-checks afterwards that the developer's
   own cache, config and tmux server are as they were. This runs on
   Alberto's machine: a test that rewrites his live config or kills his
   tmux server is worse than no test at all.

2. **Driving.** `cli()` / `cli_background()` run the bundled wrapper with
   that env. A backgrounded hook is redirected to a log FILE, never piped:
   a pipe makes the parent shell (and, with `subprocess.PIPE` plus a later
   `communicate`, us) wait for the child, so the interrupt under test never
   lands — the same trap `scripts/qa-lib.sh` documents for `| tee`.

3. **Observing.** Verdicts read the REAL buffer, never `capture-pane`:
   `buf_get_lines` over the RPC socket for nvim, a `:w!` to a scratch path
   for Vim (exactly `qa_dump_nvim_buffer` / `qa_dump_vim_buffer`). Both
   return BYTES — pynvim decodes with `surrogateescape`, so corruption
   otherwise reads as an ordinary string mismatch. Waiting is on the
   `.animating` marker's running/handoff transition, not on a sleep: a
   buffer mid-animation and an interrupted one look identical, and a sleep
   turns that into a green test that measures nothing.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
import time
import uuid
from collections.abc import Callable, Iterator
from contextlib import contextmanager, suppress
from dataclasses import dataclass, field
from pathlib import Path
from typing import IO, Any

import pynvim

REPO_ROOT = Path(__file__).resolve().parents[1]
CLI = REPO_ROOT / "bin" / "claude-follow"
VENV_BIN = REPO_ROOT / ".venv" / "bin"

# Captured at import, while HOME is still the developer's own: the exact two
# locations a subprocess that escaped the isolated HOME would corrupt.
_REAL_HOME = Path(os.environ.get("HOME") or Path.home())
REAL_CACHE_DIR = _REAL_HOME / ".cache" / "claude-vim-follower"
REAL_CONFIG_PATH = _REAL_HOME / ".config" / "claude-vim-follower" / "config.json"

# Names a leaked child would create in the real cache dir. hook.log and
# snapshots/ are deliberately NOT on this list: Alberto's own live follower
# appends to them while the suite runs, so asserting on them would fail for
# reasons that have nothing to do with this test. Leakage still cannot hide
# there — a child whose CACHE_DIR was the real one would drop a .pane file
# beside the log, and the isolated-home token scan below reads every file.
_LEAK_GLOBS = ("*.pane", "*.animating", "*.pending_animation.json", "nvim-*.sock")

PACE_LENTO = 0.15  # config.SPEED_PACE_SECONDS["lento"], seconds per character

# A Unix domain socket path may not exceed ~104 bytes, and this world holds
# TWO of them: tmux's own, and the nvim RPC socket the follower creates at
# <HOME>/.cache/claude-vim-follower/nvim-<window_id>.sock — a path production
# code derives from HOME, so the harness cannot shorten it after the fact.
# macOS's per-user $TMPDIR (/var/folders/<2>/<27>/T/, 56 chars once resolved)
# spends more than half the budget before the world's own directories start,
# which is why this deliberately does NOT use tempfile's default location.
# /private/tmp is 13, leaving room to spare. The QA scripts stage under /tmp
# for the same reason.
_MAX_SOCKET_PATH = 104
_TEMP_ROOT = Path("/tmp")


def payload(tool_name: str, file_path: str | Path, **extra: Any) -> str:
    """One hook payload as the JSON line the CLI reads off stdin."""
    body: dict[str, Any] = {
        "tool_name": tool_name,
        "tool_input": {"file_path": str(file_path), **extra},
        "session_id": "e2e",
    }
    return json.dumps(body)


class WaitTimeout(AssertionError):
    """A state transition never happened. Carries what was being waited for
    and what was actually seen, because "timed out" alone cannot be told
    apart from "the thing under test never started"."""


@dataclass
class _RealHomeFingerprint:
    config_exists: bool
    config_bytes: bytes | None
    cache_entries: frozenset[str]
    tmux_sessions: frozenset[str]


def _fingerprint_real_home() -> _RealHomeFingerprint:
    config_exists = REAL_CONFIG_PATH.exists()
    return _RealHomeFingerprint(
        config_exists=config_exists,
        config_bytes=REAL_CONFIG_PATH.read_bytes() if config_exists else None,
        cache_entries=frozenset(
            str(path.relative_to(REAL_CACHE_DIR))
            for glob in _LEAK_GLOBS
            for path in REAL_CACHE_DIR.glob(glob)
        )
        if REAL_CACHE_DIR.exists()
        else frozenset(),
        tmux_sessions=_default_socket_sessions(),
    )


def _default_socket_sessions() -> frozenset[str]:
    """Session names on the developer's OWN tmux server. Read with the
    ambient environment on purpose — this is the server the harness must
    never reach, so it is the one thing queried without TMUX_TMPDIR."""
    result = subprocess.run(
        ["tmux", "list-sessions", "-F", "#{session_name}"],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        return frozenset()  # no server running at all
    return frozenset(result.stdout.split())


def _files_mentioning(directory: Path, needle: str) -> list[str]:
    """Every file under `directory` whose NAME or CONTENT contains `needle`.
    The needle is the isolated HOME's unique token, so a hit is proof that a
    child wrote its state into the real cache instead of the throwaway one —
    a .pane file's `target`, a socket path, a logged file path."""
    hits: list[str] = []
    if not directory.exists():
        return hits
    for path in sorted(directory.rglob("*")):
        if not path.is_file():
            continue
        if needle in path.name:
            hits.append(str(path))
            continue
        with suppress(OSError):
            if needle.encode() in path.read_bytes():
                hits.append(str(path))
    return hits


@dataclass
class E2EFollower:
    """One isolated world: a throwaway HOME, a private tmux server with one
    origin pane, and the real CLI wired to both."""

    token: str
    home: Path
    workdir: Path
    tmux_tmpdir: str
    session: str
    origin_pane: str
    window_id: str
    env: dict[str, str]
    _before: _RealHomeFingerprint
    _background: list[subprocess.Popen[bytes]] = field(default_factory=list)
    _logs: list[IO[bytes]] = field(default_factory=list)
    _nvim_clients: dict[str, pynvim.Nvim] = field(default_factory=dict)

    # ---------------------------------------------------------------- paths

    @property
    def cache_dir(self) -> Path:
        return self.home / ".cache" / "claude-vim-follower"

    @property
    def animating_path(self) -> Path:
        return self.cache_dir / f"{self.window_id}.animating"

    @property
    def pending_path(self) -> Path:
        return self.cache_dir / f"{self.window_id}.pending_animation.json"

    # ------------------------------------------------------------ isolation

    def assert_isolated(self) -> None:
        """Ask a child what it resolves CACHE_DIR/CONFIG_PATH to, instead of
        assuming the HOME lever worked. This is the positive half of the
        isolation claim; assert_real_home_untouched is the negative half, and
        neither alone is enough — "wrote nowhere" would pass the negative one
        while measuring nothing."""
        probe = (
            "from vim_ai_follower import cache, config;"
            "print(cache.CACHE_DIR);print(config.CONFIG_PATH)"
        )
        result = subprocess.run(
            ["python3", "-c", probe],
            env={**self.env, "PYTHONPATH": str(REPO_ROOT / "src")},
            capture_output=True,
            text=True,
            check=True,
        )
        resolved_cache, resolved_config = result.stdout.split()
        assert resolved_cache == str(self.cache_dir), (
            f"a subprocess resolved CACHE_DIR to {resolved_cache}, not the isolated "
            f"{self.cache_dir} — HOME isolation is not in effect"
        )
        assert resolved_config == str(self.home / ".config" / "claude-vim-follower/config.json"), (
            f"a subprocess resolved CONFIG_PATH to {resolved_config}, not under {self.home}"
        )

    def assert_real_home_untouched(self) -> None:
        after = _fingerprint_real_home()
        leaked = _files_mentioning(REAL_CACHE_DIR, self.token)
        assert not leaked, f"the isolated HOME's token leaked into the real cache: {leaked}"
        new_entries = after.cache_entries - self._before.cache_entries
        assert not new_entries, (
            f"new follower state appeared in {REAL_CACHE_DIR}: {sorted(new_entries)}"
        )
        assert after.config_exists == self._before.config_exists, (
            f"{REAL_CONFIG_PATH} existence changed during the test"
        )
        assert after.config_bytes == self._before.config_bytes, (
            f"{REAL_CONFIG_PATH} was rewritten during the test"
        )
        assert self.session not in after.tmux_sessions, (
            f"the isolated session {self.session} landed on the developer's own tmux server"
        )
        # "The server is still up", not "the exact same sessions are still
        # there". The hazard this guards is a `tmux kill-server` escaping onto
        # the default socket; which individual sessions the developer has open
        # is his business, and he closed and reopened his while this suite was
        # running (2026-09-22) — asserting the set would have failed for a
        # reason that has nothing to do with isolation.
        assert not (self._before.tmux_sessions and not after.tmux_sessions), (
            "the developer's own tmux server had sessions before this test and has "
            f"none now — it was killed. Had: {sorted(self._before.tmux_sessions)}"
        )

    # ------------------------------------------------------------------ CLI

    def cli(self, *args: str, stdin: str | None = None) -> subprocess.CompletedProcess[str]:
        """Run the bundled wrapper in the foreground. Every hook exit is 0 by
        design (hooks must never fail the tool call), so a non-zero one is a
        finding and is raised here rather than silently ignored."""
        result = subprocess.run(
            [str(CLI), *args],
            input=stdin,
            env=self.env,
            capture_output=True,
            text=True,
            check=False,
        )
        assert result.returncode == 0, (
            f"claude-follow {' '.join(args)} exited {result.returncode}\n"
            f"stdout: {result.stdout}\nstderr: {result.stderr}"
        )
        return result

    def cli_background(self, *args: str, stdin: str) -> subprocess.Popen[bytes]:
        """Start a hook that will block (animating, or holding a hand-off) and
        return its Popen. Output goes to a FILE, never a pipe: an unread pipe
        that fills blocks the child, and the whole point of these tests is
        that the child keeps running while we signal it.

        bin/claude-follow `exec`s python in place, so the returned pid IS the
        hook process — which is what makes the kill -9 in the crash-fallback
        test land on the right thing."""
        # SIM115 is suppressed deliberately: a context manager is exactly what
        # this must NOT be. The file has to outlive this call — it is the
        # running child's stdout for as long as the test keeps it alive — and
        # close() in the harness teardown is the matching half.
        log = tempfile.TemporaryFile()  # noqa: SIM115
        self._logs.append(log)
        proc = subprocess.Popen(
            [str(CLI), *args],
            stdin=subprocess.PIPE,
            stdout=log,
            stderr=subprocess.STDOUT,
            env=self.env,
        )
        assert proc.stdin is not None
        proc.stdin.write(stdin.encode())
        proc.stdin.close()
        self._background.append(proc)
        return proc

    def start(self, backend: str, speed: str = "lento") -> None:
        self.cli("start", "--backend", backend, "--speed", speed)

    def follower_target(self) -> str:
        """The drive target out of the persisted state file: a tmux pane id
        for the tmux backend, an nvim RPC socket path for nvim."""
        state = json.loads((self.cache_dir / f"{self.window_id}.pane").read_text())
        target = state["target"]
        assert isinstance(target, str)
        return target

    # -------------------------------------------------------------- waiting

    def wait_until(
        self,
        predicate: Callable[[], bool],
        what: str,
        timeout: float = 20.0,
        interval: float = 0.05,
    ) -> None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if predicate():
                return
            time.sleep(interval)
        raise WaitTimeout(
            f"timed out after {timeout}s waiting for {what}; "
            f"animating={self.animating_state()!r} pending={self.pending_path.exists()}"
        )

    def animating_state(self) -> str | None:
        """The marker's state — "running", "paused" or "handoff" — or None
        when no LIVE process owns it. Read independently of control.animating_state so the
        instrument is not the code under test — but with the same PID-liveness
        rule, which is the whole reason a crashed hook cannot leave a
        convincing stale marker."""
        try:
            fields = self.animating_path.read_text().split()
            pid = int(fields[0])
        except (OSError, ValueError, IndexError):
            return None
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return None
        return fields[1] if len(fields) > 1 else "running"

    def wait_for_animating(self, state: str, timeout: float = 20.0) -> None:
        self.wait_until(
            lambda: self.animating_state() == state,
            f"the .animating marker to read {state!r}",
            timeout,
        )

    def wait_for_hook_exit(self, proc: subprocess.Popen[bytes], timeout: float = 120.0) -> None:
        """A backgrounded hook must finish, and finish 0 — a hook that fails
        the tool call is a bug by design, so a non-zero exit is a finding."""
        self.wait_until(
            lambda: proc.poll() is not None,
            f"the background hook (pid {proc.pid}) to exit",
            timeout,
        )
        assert proc.returncode == 0, f"the hook exited {proc.returncode}, not 0"

    def pending(self) -> dict[str, Any]:
        """The persisted crash-fallback remainder, non-consuming (production
        consumes it; this is only an observation point)."""
        data = json.loads(self.pending_path.read_text())
        assert isinstance(data, dict)
        return data

    # ------------------------------------------------------------ observing

    def _nvim(self, sock: str) -> pynvim.Nvim:
        client = self._nvim_clients.get(sock)
        if client is None:
            client = pynvim.attach("socket", path=sock)
            self._nvim_clients[sock] = client
        return client

    def nvim_buffer_bytes(self, sock: str, file_path: str | Path) -> bytes | None:
        """The REAL buffer for `file_path`, as BYTES, or None when no buffer
        holds that name. Paths are compared RESOLVED: on macOS /tmp is a
        symlink to /private/tmp, so a buffer opened as /tmp/x.py reports
        /private/tmp/x.py and a string compare silently finds nothing."""
        wanted = Path(file_path).resolve()
        for buf in self._nvim(sock).buffers:
            if buf.name and Path(buf.name).resolve() == wanted:
                return b"".join(line.encode("utf-8", "surrogateescape") + b"\n" for line in buf[:])
        return None

    def nvim_buffer_lines(self, sock: str, file_path: str | Path) -> list[str]:
        raw = self.nvim_buffer_bytes(sock, file_path)
        return [] if raw is None else raw.decode("utf-8", "replace").split("\n")[:-1]

    def vim_buffer_bytes(self, pane: str) -> bytes:
        """The tmux backend's twin: dump the follower Vim's real buffer via
        `:w!` to a scratch path. Escape Escape first so the Ex command lands
        even if the pane was left in insert mode; the bang overrides the
        follower's readonly relock, and the write goes to the scratch path
        only, never to the file under test.

        Not `:redir | silent %p` (measured 2026-09-21 against a real
        follower): redir renders every blank line as a single space and drops
        the final newline, so a byte diff fails on a buffer that is correct."""
        out = self.workdir / f"vimdump-{uuid.uuid4().hex[:8]}.txt"
        self.tmux("send-keys", "-t", pane, "Escape", "Escape")
        self.tmux("send-keys", "-t", pane, "-l", "--", f":silent! w! {out}")
        self.tmux("send-keys", "-t", pane, "Enter")
        self.wait_until(out.exists, f"vim's buffer dump to appear at {out}", timeout=10.0)
        # The write is not atomic: poll for a stable size before reading, or a
        # long buffer reads back truncated and the diff blames the follower.
        self.wait_until(
            _stable_size(out), f"vim's buffer dump at {out} to stop growing", timeout=10.0
        )
        data = out.read_bytes()
        out.unlink()
        return data

    def buffer_bytes(self, backend: str, target: str, file_path: str | Path) -> bytes | None:
        return (
            self.vim_buffer_bytes(target)
            if backend == "tmux"
            else self.nvim_buffer_bytes(target, file_path)
        )

    def vim_messages(self, pane: str) -> str:
        """Vim's own `:messages` history, dumped to a scratch file.

        The durable record of an error, and the only one that survives: an
        error at 49 columns escalates to a hit-enter prompt, but the follower's
        own next keystrokes (`ensure_showing` ends with `:setlocal readonly
        nomodifiable` + Enter) dismiss it within milliseconds, so by the time a
        test looks, `capture-pane` shows a clean screen while `:messages` still
        names the error (measured 2026-09-22 against the E37 catch reverted).

        A colon command is the wrong PROBE for a prompt — `:` is a real key at
        a hit-enter prompt — but it is the right READER: it works whether or
        not one is up, which is exactly what a reader must do."""
        out = self.workdir / f"vimmsgs-{uuid.uuid4().hex[:8]}.txt"
        self.tmux("send-keys", "-t", pane, "Escape", "Escape")
        self.tmux(
            "send-keys", "-t", pane, "-l", "--", f":redir! > {out} | silent messages | redir END"
        )
        self.tmux("send-keys", "-t", pane, "Enter")
        self.wait_until(out.exists, f"vim's message dump to appear at {out}", timeout=10.0)
        self.wait_until(_stable_size(out), f"vim's message dump at {out} to settle", timeout=10.0)
        text = out.read_text(errors="replace")
        out.unlink()
        return text

    def capture_pane(self, pane: str) -> str:
        """The RENDERED screen. Correct only for verdicts that are ABOUT the
        screen (an E37 / hit-enter prompt); never for content."""
        return self.tmux("capture-pane", "-p", "-t", pane).stdout

    def visible_progress(self, backend: str, target: str, file_path: str | Path) -> str:
        """Cheap "has real content landed yet?" probe for a running
        animation. On nvim it reads the real buffer; on tmux it reads the
        rendered pane, because send-keys into an animating Vim would corrupt
        the very animation being measured. Progress only — never a verdict."""
        if backend == "tmux":
            return self.capture_pane(target)
        raw = self.nvim_buffer_bytes(target, file_path)
        return "" if raw is None else raw.decode("utf-8", "replace")

    def cursor_row(self, pane: str) -> tuple[int, int]:
        """(cursor row, pane height). Vim parks the cursor on the LAST row
        while a hit-enter prompt or the `-- More --` pager is blocking, and
        leaves it in the text area otherwise — so this reads "is the pane
        blocked?" without sending a keystroke that would dismiss the answer."""
        raw = self.tmux(
            "display-message", "-p", "-t", pane, "#{cursor_y} #{pane_height}"
        ).stdout.split()
        return int(raw[0]), int(raw[1])

    def pane_width(self, pane: str) -> int:
        return int(self.tmux("display-message", "-p", "-t", pane, "#{pane_width}").stdout.strip())

    def resize_pane(self, pane: str, width: int) -> int:
        self.tmux("resize-pane", "-t", pane, "-x", str(width), check=False)
        return self.pane_width(pane)

    # ----------------------------------------------------------------- tmux

    def tmux(self, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
        """Every tmux call the harness makes carries the private TMUX_TMPDIR,
        so none of them can reach the developer's own server."""
        return subprocess.run(
            ["tmux", *args], env=self.env, capture_output=True, text=True, check=check
        )

    # -------------------------------------------------------------- cleanup

    def close(self) -> None:
        """Tear the world down. Reached through a `finally`, so it covers a
        failing assertion and a Ctrl-C — but NOT a SIGKILL of the pytest
        process itself, which leaves the private tmux server, its nvim and
        /tmp/vaf-e2e-<token>/ behind (measured 2026-09-22, when the session
        running the suite was killed). They are harmless and self-labelled;
        `pkill -f vaf-e2e` plus `rm -rf /tmp/vaf-e2e-*` collects them."""
        for client in self._nvim_clients.values():
            with suppress(Exception):
                client.close()
        self._nvim_clients.clear()
        for proc in self._background:
            if proc.poll() is None:
                proc.kill()
            with suppress(Exception):
                proc.wait(timeout=5)
        self._background.clear()
        for log in self._logs:
            with suppress(OSError):
                log.close()
        self._logs.clear()
        # kill-server takes the whole private world with it: the origin pane,
        # the follower's vim/nvim split, and the server itself. Nothing here
        # can reach the developer's server — the socket lives in tmux_tmpdir.
        self.tmux("kill-server", check=False)
        shutil.rmtree(self.tmux_tmpdir, ignore_errors=True)
        shutil.rmtree(self.home, ignore_errors=True)


def _stable_size(path: Path) -> Callable[[], bool]:
    seen: dict[str, int] = {}

    def _check() -> bool:
        try:
            size = path.stat().st_size
        except OSError:
            return False
        was = seen.get("size")
        seen["size"] = size
        return was == size

    return _check


def _assert_socket_paths_fit(home: Path, tmux_tmpdir: str) -> None:
    """Fail with the real reason up front. Overshooting the socket limit
    surfaces as tmux's "error connecting to ... (File name too long)" or, on
    the nvim side, as an attach that times out — neither of which names the
    path length as the cause."""
    candidates = {
        "tmux socket": Path(tmux_tmpdir).resolve() / f"tmux-{os.getuid()}" / "default",
        # window ids are @0, @1, ... on a fresh server; @99 is generous.
        "nvim RPC socket": home.resolve() / ".cache" / "claude-vim-follower" / "nvim-@99.sock",
    }
    for what, path in candidates.items():
        assert len(str(path)) <= _MAX_SOCKET_PATH, (
            f"the {what} path would be {len(str(path))} bytes, over the "
            f"{_MAX_SOCKET_PATH}-byte Unix domain socket limit: {path}"
        )


def _checked(cmd: list[str], env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    """subprocess.run(check=True) with the STDERR in the exception message.
    A bare CalledProcessError prints the (very long) argv and swallows the one
    line that says what tmux actually objected to."""
    result = subprocess.run(cmd, env=env, capture_output=True, text=True, check=False)
    if result.returncode != 0:
        raise RuntimeError(
            f"{cmd[0]} {' '.join(cmd[1:3])} exited {result.returncode}: "
            f"{result.stderr.strip() or result.stdout.strip()!r}"
        )
    return result


@contextmanager
def e2e_world() -> Iterator[E2EFollower]:
    """Build the isolated world, hand it to the test, tear it down and only
    then check that the developer's own machine is as it was."""
    before = _fingerprint_real_home()
    token = f"vaf-e2e-{uuid.uuid4().hex[:8]}"
    root = _TEMP_ROOT / token
    home = root / "h"
    workdir = root / "w"
    tmux_tmpdir = str(root / "s")
    for directory in (home / ".cache", home / ".config", workdir, Path(tmux_tmpdir)):
        directory.mkdir(parents=True)
    _assert_socket_paths_fit(home, tmux_tmpdir)

    env = _child_env(home, workdir, tmux_tmpdir)
    session = f"{token}-s"
    _checked(
        [
            "tmux",
            "new-session",
            "-d",
            "-s",
            session,
            "-x",
            "120",
            "-y",
            "40",
            # Belt and braces over the env passed to this call: a pane the
            # follower opens later (the vim/nvim split) inherits the SESSION
            # environment, and these are the variables that keep it isolated
            # and hermetic. No ~/.vimrc and no ~/.config/nvim exist under the
            # throwaway HOME, so neither editor loads user config or plugins.
            *_session_env_flags(env),
            "sh",
        ],
        env,
    )
    origin_pane = _checked(
        ["tmux", "list-panes", "-t", session, "-F", "#{pane_id}"], env
    ).stdout.split()[0]
    window_id = _checked(
        ["tmux", "display-message", "-p", "-t", origin_pane, "#{window_id}"], env
    ).stdout.strip()
    env["TMUX_PANE"] = origin_pane

    world = E2EFollower(
        token=token,
        home=home,
        workdir=workdir,
        tmux_tmpdir=tmux_tmpdir,
        session=session,
        origin_pane=origin_pane,
        window_id=window_id,
        env=env,
        _before=before,
    )
    world.assert_isolated()
    try:
        yield world
    finally:
        world.close()
        shutil.rmtree(root, ignore_errors=True)
    world.assert_real_home_untouched()


def _child_env(home: Path, workdir: Path, tmux_tmpdir: str) -> dict[str, str]:
    """Built from scratch, not copied from os.environ: $TMUX must be ABSENT
    (it takes priority over TMUX_TMPDIR and would route every bare `tmux`
    call — including the ones inside the CLI's own subprocess calls —
    straight at the developer's live server), and so must anything else
    inherited from the terminal running the suite.

    The venv's bin goes first on PATH because bin/claude-follow execs a bare
    `python3`: the real wrapper stays the entry point under test, but it
    resolves to the interpreter that has pynvim."""
    return {
        "HOME": str(home),
        "PATH": f"{VENV_BIN}:{os.environ.get('PATH', '/usr/bin:/bin')}",
        "TMPDIR": str(workdir),
        "TMUX_TMPDIR": tmux_tmpdir,
        "XDG_CONFIG_HOME": str(home / ".config"),
        "XDG_DATA_HOME": str(home / ".local" / "share"),
        "XDG_STATE_HOME": str(home / ".local" / "state"),
        "XDG_CACHE_HOME": str(home / ".cache"),
        "SHELL": "/bin/sh",
        "TERM": "xterm-256color",
        "LANG": "en_US.UTF-8",
        "USER": os.environ.get("USER", "e2e"),
    }


def _session_env_flags(env: dict[str, str]) -> list[str]:
    keys = (
        "HOME",
        "TMPDIR",
        "TMUX_TMPDIR",
        "XDG_CONFIG_HOME",
        "XDG_DATA_HOME",
        "XDG_STATE_HOME",
        "XDG_CACHE_HOME",
        "PATH",
        "SHELL",
        "TERM",
        "LANG",
    )
    return [flag for key in keys for flag in ("-e", f"{key}={env[key]}")]
