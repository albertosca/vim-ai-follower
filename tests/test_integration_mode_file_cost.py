"""The mode file (tmux_vim._VIM_MODE) costs the user's Vim nothing they can
feel (final review I1): every ModeChanged used to write the file with
fsync and rename it, about 9 ms a change, so `:g/^/normal Ix` over 2000
lines took 32 s instead of 0.17 s. It now writes only when the mode CLASS
the hook tells apart changes, from a timer (which runs only once Vim waits
for a key, so never inside a `:normal`, a macro or a mapping), without
fsync; and a DEDICATED Vim gets no mode autocommands at all.

Real Vims: `vim -u NONE -i NONE` sourcing the real script, run headless for
the timings and in a private tmux server for the modes."""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
import time
from collections.abc import Iterator
from pathlib import Path

import pytest

from vim_ai_follower import cache
from vim_ai_follower.backends import tmux_vim

pytestmark = pytest.mark.integration


def _script(directory: Path) -> Path:
    """The real script, for a cache directory of its own."""
    _name, text = tmux_vim._vim_functions()
    path = directory / "vaf-test.vim"
    path.write_text(text)
    return path


def _headless(
    tmp_path: Path, script: Path, commands: list[str], *, before: list[str] | None = None
) -> list[str]:
    """Run a headless Vim on a 1000-line file with the script sourced, then
    `commands`; return what the last command wrote to `out.txt`."""
    edited = tmp_path / "file.txt"
    edited.write_text("a\n" * 1000)
    out = tmp_path / "out.txt"
    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    arguments = ["vim", "-u", "NONE", "-i", "NONE", "-N", "-n", "--not-a-term"]
    for command in before or []:
        arguments += ["--cmd", command]
    arguments += ["-c", f"source {script}"]
    for command in [*commands, "qa!"]:
        arguments += ["-c", command.replace("@OUT@", str(out))]
    environment = {
        key: value for key, value in os.environ.items() if key not in ("TMUX", "VIMINIT")
    }
    environment["HOME"] = str(home)
    subprocess.run(
        [*arguments, str(edited)],
        env=environment,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        timeout=120,
        check=False,
    )
    return out.read_text().splitlines()


def test_a_normal_command_over_a_thousand_lines_is_not_slowed_by_the_mode_file(
    tmp_path: Path,
) -> None:
    # Shipped: 9.9 s for 1000 lines (2000: 19.8 s), against 0.08 s with no
    # script; the bound leaves a loaded machine twenty times the room.
    cache.CACHE_DIRECTORY.mkdir(parents=True, exist_ok=True)
    timed = _headless(
        tmp_path,
        _script(tmp_path),
        [
            "let g:start = reltime()",
            "silent g/^/normal Ix",
            "call writefile([reltimefloat(reltime(g:start))], '@OUT@')",
        ],
    )
    assert float(timed[0]) < 2.0, f":g/^/normal Ix took {timed[0]} s"


def test_a_dedicated_vim_gets_no_mode_autocommands(tmp_path: Path) -> None:
    # Its mode is never read (hooks._busy_vim asks only adopted Vims).
    cache.CACHE_DIRECTORY.mkdir(parents=True, exist_ok=True)
    report = _headless(
        tmp_path,
        _script(tmp_path),
        [
            "startinsert",
            "stopinsert",
            "call writefile([exists('#vim_ai_follower_mode#ModeChanged')], '@OUT@')",
        ],
        before=[tmux_vim.DEDICATED_MARK],
    )
    assert report == ["0"]
    assert not list(cache.CACHE_DIRECTORY.glob("*mode-*"))


def test_an_adopted_vim_still_gets_them(tmp_path: Path) -> None:
    cache.CACHE_DIRECTORY.mkdir(parents=True, exist_ok=True)
    report = _headless(
        tmp_path,
        _script(tmp_path),
        ["call writefile([exists('#vim_ai_follower_mode#ModeChanged')], '@OUT@')"],
    )
    assert report == ["1"]


@pytest.fixture
def private_tmux() -> Iterator[list[str]]:
    """A private tmux server with no config: the argv prefix to reach it."""
    directory = tempfile.mkdtemp(prefix="vafm", dir="/tmp")
    prefix = ["tmux", "-S", f"{directory}/sock", "-f", "/dev/null"]
    try:
        yield prefix
    finally:
        subprocess.run([*prefix, "kill-server"], check=False, capture_output=True)
        shutil.rmtree(directory, ignore_errors=True)


def test_the_mode_file_names_every_class_the_hook_tells_apart(
    tmp_path: Path, private_tmux: list[str]
) -> None:
    cache.CACHE_DIRECTORY.mkdir(parents=True, exist_ok=True)
    script = _script(tmp_path)
    home = tmp_path / "home"
    home.mkdir()
    edited = tmp_path / "file.txt"
    edited.write_text("a\nb\n")
    environment = {
        key: value
        for key, value in os.environ.items()
        if key not in ("TMUX", "TMUX_PANE", "VIMINIT")
    }
    subprocess.run(
        [
            *private_tmux,
            "new-session",
            "-d",
            "-x",
            "80",
            "-y",
            "20",
            f"env HOME={home} vim -u NONE -i NONE -N -n -c 'set ttimeout ttimeoutlen=10'"
            f" -c 'source {script}' {edited}",
        ],
        env=environment,
        check=True,
    )

    def mode_file() -> Path | None:
        return next(iter(cache.CACHE_DIRECTORY.glob("mode-*.txt")), None)

    def mode() -> str:
        found = mode_file()
        return found.read_text().split()[1] if found else ""

    def written() -> int:
        found = mode_file()
        assert found is not None
        return found.stat().st_mtime_ns

    def expect(keys: list[str], wanted: str) -> None:
        for key in keys:
            literal = ["-l"] if len(key) > 1 and key not in ("Escape", "Enter") else []
            subprocess.run([*private_tmux, "send-keys", *literal, key], env=environment, check=True)
        deadline = time.monotonic() + 5.0
        while mode() != wanted:
            assert time.monotonic() < deadline, f"after {keys}: {mode()!r}, wanted {wanted!r}"
            time.sleep(0.05)

    expect([], "n")
    expect(["i"], "i")
    expect(["Escape"], "n")
    expect(["v"], "v")
    expect(["Escape"], "n")
    expect(["R"], "R")
    expect(["Escape"], "n")
    expect(["g", "h"], "s")
    expect(["Escape"], "n")
    expect(["g", "Q"], "cv")
    expect(["visual", "Enter"], "n")
    expect([":term", "Enter"], "t")
    expect(["exit", "Enter"], "n")
    # A command line is not a class of its own: no write for the n->c->n
    # every typed call line costs.
    before = written()
    expect([":%normal Ix", "Enter"], "n")
    time.sleep(0.3)
    assert written() == before


def test_a_stopped_mode_timer_restarts(tmp_path: Path, private_tmux: list[str]) -> None:
    # A plugin's timer_stopall() kills the 0 ms timer a ModeChanged started
    # before it ran (a mapping runs no timer): its flag stayed set and
    # s:note_mode returned early for good, so the file kept its last mode.
    cache.CACHE_DIRECTORY.mkdir(parents=True, exist_ok=True)
    script = _script(tmp_path)
    home = tmp_path / "home"
    home.mkdir()
    edited = tmp_path / "file.txt"
    edited.write_text("a\nb\n")
    environment = {
        key: value
        for key, value in os.environ.items()
        if key not in ("TMUX", "TMUX_PANE", "VIMINIT")
    }
    subprocess.run(
        [
            *private_tmux,
            "new-session",
            "-d",
            "-x",
            "80",
            "-y",
            "20",
            f"env HOME={home} vim -u NONE -i NONE -N -n -c 'set ttimeout ttimeoutlen=10'"
            f" -c 'source {script}'"
            " -c 'nnoremap <F2> i<C-o>:call timer_stopall()<CR><Esc>'"
            f" {edited}",
        ],
        env=environment,
        check=True,
    )

    def mode() -> str:
        found = next(iter(cache.CACHE_DIRECTORY.glob("mode-*.txt")), None)
        return found.read_text().split()[1] if found else ""

    def expect(key: str, wanted: str, timeout: float) -> None:
        subprocess.run([*private_tmux, "send-keys", key], env=environment, check=True)
        deadline = time.monotonic() + timeout
        while mode() != wanted:
            assert time.monotonic() < deadline, f"after {key}: {mode()!r}, wanted {wanted!r}"
            time.sleep(0.05)

    expect("Escape", "n", 5.0)
    expect("F2", "n", 5.0)
    time.sleep(0.3)
    expect("i", "i", 1.0)
