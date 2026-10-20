"""How long the tmux backend's typed call lines stay on a real Vim's screen.

Usage: uv run python scripts/measure_typed_echo.py <adopted|dedicated> [calls]

Runs a private tmux server (TMUX unset, a private TMUX_TMPDIR, `-f
/dev/null`, a throwaway HOME), a Vim in a 49-column pane, and the real
TmuxVimFollower against it (show_fresh, probe, apply_edit, ensure_showing,
close_tab per round) until at least `calls` (default 400) follower lines have
been echoed. `tmux pipe-pane` copies Vim's raw terminal output into a
recorder (this script again, `--record <file>`) that stamps every read with
time.monotonic(). A typed line is EXPOSED when its echo reaches the terminal
in one chunk and the CR that ends it only in a later one; the report gives
the count, the median/p95/max exposure and how many lasted over 1 ms.

`adopted` runs a plain Vim with an adopted state, so the script installs its
mode-file group (ModeChanged); `dedicated` launches the Vim the way `start`
does (`--cmd 'let g:vaf_dedicated = 1'`), which installs none. Run each twice
and compare against the run-to-run spread, on an idle machine (load < 10)."""

from __future__ import annotations

import os
import re
import shutil
import struct
import subprocess
import sys
import tempfile
import time
from pathlib import Path

_RECORD = struct.Struct("<dI")
_ECHO = re.compile(rb":(?:if !?exists\('\*VafFollower_|filetype detect|%d|setlocal|sil! )")


def record(output_path: str) -> None:
    """The pipe-pane sink: one record per read of the pane's output."""
    with Path(output_path).open("ab") as output:
        while True:
            data = os.read(0, 65536)
            if not data:
                return
            output.write(_RECORD.pack(time.monotonic(), len(data)) + data)
            output.flush()


def _chunks(path: Path) -> list[tuple[float, bytes]]:
    data = path.read_bytes()
    offset = 0
    found: list[tuple[float, bytes]] = []
    while offset + _RECORD.size <= len(data):
        stamp, length = _RECORD.unpack_from(data, offset)
        offset += _RECORD.size
        found.append((stamp, data[offset : offset + length]))
        offset += length
    return found


def exposures(path: Path) -> tuple[int, list[float]]:
    """(echoed follower lines, exposure in ms of each exposed one)."""
    stream = b""
    starts: list[tuple[int, float]] = []
    for stamp, data in _chunks(path):
        starts.append((len(stream), stamp))
        stream += data

    def stamp_at(position: int) -> float:
        current = starts[0][1]
        for start, stamp in starts:
            if start > position:
                break
            current = stamp
        return current

    echoes = 0
    exposed: list[float] = []
    for match in _ECHO.finditer(stream):
        line_end = match.end()
        if b"VafFollower_" in match.group():
            line_end = stream.find(b"endif", match.end())
            if line_end < 0:
                continue
        close = stream.find(b"\r", line_end)
        if close < 0:
            continue
        echoes += 1
        shown_at, closed_at = stamp_at(match.start()), stamp_at(close)
        if closed_at > shown_at:
            exposed.append((closed_at - shown_at) * 1000)
    return echoes, sorted(exposed)


def _tmux(*arguments: str) -> str:
    return subprocess.run(
        ["tmux", *arguments], capture_output=True, text=True, check=True
    ).stdout.strip()


def measure(variant: str, calls: int) -> None:
    for name in ("TMUX", "TMUX_PANE"):
        os.environ.pop(name, None)
    socket_directory = tempfile.mkdtemp(prefix="vaf-echo-", dir="/tmp")
    scratch = Path(tempfile.mkdtemp(prefix="vaf-echo-run-"))
    home = scratch / "home"
    project = scratch / "project"
    home.mkdir()
    project.mkdir()
    os.environ["TMUX_TMPDIR"] = socket_directory
    os.environ["HOME"] = str(home)

    from vim_ai_follower import cache
    from vim_ai_follower.backends.tmux_vim import TmuxVimFollower
    from vim_ai_follower.diff import compute_edit_script
    from vim_ai_follower.state import FollowerState

    cache.CACHE_DIRECTORY = home / ".cache" / "claude-vim-follower"
    cache.CACHE_DIRECTORY.mkdir(parents=True)
    chunks_path = scratch / "chunks.bin"
    vim_command = ["vim", "-i", "NONE"]
    if variant == "dedicated":
        vim_command += ["--cmd", "let g:vaf_dedicated = 1"]
    try:
        _tmux("-f", "/dev/null", "new-session", "-d", "-s", "measure", "-x", "100", "-y", "30")
        socket_path = _tmux("display-message", "-p", "#{socket_path}")
        if not socket_path.startswith(os.path.realpath(socket_directory)):
            raise SystemExit(f"not a private server: {socket_path}")
        pane = _tmux(
            "split-window", "-h", "-l", "49", "-t", "measure", "-c", str(project),
            "-e", f"HOME={home}", "-e", "VIMINIT=set nocompatible noloadplugins viminfofile=NONE",
            "-P", "-F", "#{pane_id}", *vim_command,
        )  # fmt: skip
        time.sleep(1.0)
        _tmux(
            "pipe-pane", "-O", "-t", pane,
            f"{sys.executable} -P {Path(__file__).resolve()} --record {chunks_path}",
        )  # fmt: skip
        width = _tmux("display-message", "-p", "-t", pane, "#{pane_width}")
        window_id = _tmux("display-message", "-p", "-t", pane, "#{window_id}")
        FollowerState.set(
            window_id, "tmux", pane, adopted=variant == "adopted", shown_any=True, speed="instant"
        )
        follower = TmuxVimFollower(pane_id=pane, pace_seconds=0.0, window_id=window_id)
        before_text, after_text = "x = 1\ny = 2\n", "x = 1\nz = 3\ny = 2\n"
        round_index = 0
        while (exposures(chunks_path)[0] if chunks_path.exists() else 0) < calls:
            target = project / f"file_{round_index}.py"
            target.write_text(before_text)
            follower.show_fresh(str(target), before_text, in_new_tab=True)
            target.write_text(after_text)
            follower.probe_buffer(str(target), before_text)
            follower.apply_edit(
                str(target), compute_edit_script(before_text, after_text), before_text
            )
            follower.ensure_showing(str(target))
            follower.close_tab(str(target))
            round_index += 1
        time.sleep(1.0)
        _tmux("pipe-pane", "-t", pane)
        time.sleep(0.3)
        echoes, exposed = exposures(chunks_path)
        print(f"variant={variant} width={width} rounds={round_index} echoes={echoes}")
        print(f"exposed={len(exposed)} over_1ms={sum(1 for value in exposed if value > 1.0)}")
        if exposed:
            p95 = exposed[min(len(exposed) - 1, int(len(exposed) * 0.95))]
            print(
                f"exposure ms: median {exposed[len(exposed) // 2]:.2f}"
                f" p95 {p95:.2f} max {exposed[-1]:.2f}"
            )
        print(f"load={os.getloadavg()[0]:.1f}")
    finally:
        subprocess.run(["tmux", "kill-server"], check=False, capture_output=True)
        shutil.rmtree(socket_directory, ignore_errors=True)
        shutil.rmtree(scratch, ignore_errors=True)


def main() -> None:
    if sys.argv[1:2] == ["--record"]:
        record(sys.argv[2])
        return
    if not sys.argv[1:2] or sys.argv[1] not in ("adopted", "dedicated"):
        raise SystemExit(__doc__)
    measure(sys.argv[1], int(sys.argv[2]) if len(sys.argv) > 2 else 400)


if __name__ == "__main__":
    main()
