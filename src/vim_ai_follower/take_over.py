"""Asking the user of an ADOPTED editor whether the follower may take it over.

When the user's own Vim or nvim is known to be in a typing mode (insert,
replace, terminal, visual or select), acting there would put their next keys
into Claude's buffer. The hook asks instead (Alberto, 2026-10-17): a tmux
popup over the follower's window, and only a deliberate `y` takes over.
Everything else is "no": any other key (Enter and space included), closing
the popup, the timeout, no client attached to show it on, or a popup tmux
dropped because another one was up (it does that silently, exit 0).

Measured on tmux 3.8 (2026-10-17, a private server with a pty-attached
client): `display-popup -E` waits for the popup to close and returns its
command's exit status; with no client it fails at once ("no current
client"); a second popup while one is up returns 0 at once and never runs
its command; and it shows over the client's current window even when the
target pane is in another one. `display-menu` was rejected: it returns at
once, Enter picks the highlighted item, and it has no timeout.

A key typed while the popup opens is read by the prompt at once (an in-flight
`y` arrived 23 ms after the prompt started reading). So a key read before
ARM_SECONDS answers "no", whatever it is: a user typing when the popup
appears answers it with their next key, and that answer is "no". So does a
read of more than one byte (a burst or a paste).

The prompt itself runs inside the popup as `python -m
vim_ai_follower.take_over <answer file> <token> <seconds> <editor>`, and
writes `<token> started` to the answer file, then `<token> <answer>`; only a
file holding THIS ask's token and "yes" is a yes. The popup is shown on the
most recently active client of the target pane's own session (`-c`), and
with no client there nothing is asked."""

from __future__ import annotations

import os
import secrets
import select
import shlex
import subprocess
import sys
import termios
import time
import tty
from collections.abc import Callable
from pathlib import Path

from vim_ai_follower import cache

# A key read sooner than this after the prompt starts answers "no": it was
# typed before the user could have read the question.
ARM_SECONDS = 0.6
# The popup's own command may take this much longer than the question's
# timeout to close (its interpreter start, the answer write) before the hook
# stops waiting for it.
_CLOSE_MARGIN_SECONDS = 5.0
_POPUP_WIDTH = 46
_POPUP_HEIGHT = 6

YES = "yes"
# What the prompt writes to the answer file before it asks: a popup that
# closes after this without an answer was shown, not dropped.
_STARTED = "started"

# What typing_reason answers: the modes in which the user is typing.
TYPING_REASONS = frozenset(
    {"in insert mode", "in replace mode", "in terminal mode", "in visual mode", "in select mode"}
)


def typing_reason(mode: str | None) -> str | None:
    """The typing mode a Vim or nvim `mode(1)` string names (one of
    TYPING_REASONS), or None. Both editors spell them alike: insert and
    replace (`i…`, `R…`, and the `niI`/`niR`/`niV` of a CTRL-O from one),
    terminal (`t…`, nvim's `ntT`), select (`s`/`S`/CTRL-S and the
    `vs`/`Vs`/CTRL-V `s` of a CTRL-O from select) and visual
    (`v`/`V`/CTRL-V). Vim's Terminal-Normal `nt` is Normal mode."""
    if mode is None:
        return None
    if mode.startswith(("i", "niI")):
        return "in insert mode"
    if mode.startswith(("R", "niR", "niV")):
        return "in replace mode"
    if mode.startswith(("t", "ntT")):
        return "in terminal mode"
    if mode in ("vs", "Vs", "\x16s") or mode.startswith(("s", "S", "\x13")):
        return "in select mode"
    if mode.startswith(("v", "V", "\x16")):
        return "in visual mode"
    return None


def _answer_path(token: str) -> Path:
    return cache.CACHE_DIR / f"take-over-{token}.txt"


# Answer files (and their write temporaries) older than this are an earlier
# question's leftovers: a prompt that outlived its hook's wait writes its
# answer after `ask`'s `finally` removed the file (final review M2). Far
# past any question (config's 120 s plus the close margin).
_SWEEP_AFTER_SECONDS = 600.0


def _sweep_answers() -> None:
    """Delete the answer files and temporaries earlier questions left behind
    (_SWEEP_AFTER_SECONDS old). Best effort: a file that cannot be read or
    deleted stays."""
    cutoff = time.time() - _SWEEP_AFTER_SECONDS
    for pattern in ("take-over-*.txt", ".take-over-*.txt.*"):
        for path in cache.CACHE_DIR.glob(pattern):
            try:
                if path.stat().st_mtime < cutoff:
                    path.unlink()
            except OSError:
                continue


def _package_root() -> str:
    """The directory that holds the vim_ai_follower package, for the popup's
    interpreter to import it from (the plugin runs from its bundled src/)."""
    return str(Path(__file__).resolve().parent.parent)


def _client_for(target_pane: str) -> str | None:
    """The client to show the question on: of the clients attached to
    `target_pane`'s session, the most recently active one (the one the user
    typed into last), or None when that session has none. Measured on tmux
    3.8: `list-clients -t <pane>` lists only the clients of the pane's
    session, and #{client_activity} moves when a client is typed into.
    Without `-c`, a popup went to whichever client tmux picked: with two
    clients a `y` in the watched one timed out, and with the only client on
    an unrelated session the question was shown there (review m7)."""
    result = subprocess.run(
        ["tmux", "list-clients", "-t", target_pane, "-F", "#{client_activity} #{client_name}"],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        return None
    latest: tuple[int, str] | None = None
    for line in result.stdout.splitlines():
        activity, _, name = line.partition(" ")
        if not activity.isdigit() or not name:
            continue
        if latest is None or int(activity) > latest[0]:
            latest = (int(activity), name)
    return latest[1] if latest is not None else None


def ask(target_pane: str, editor: str, seconds: float) -> str:
    """Ask, over `target_pane`'s window, whether to take over `editor`.
    YES only for a deliberate `y`; otherwise why not, for the hook's log."""
    if not target_pane:
        return "not asked: the editor is not in tmux"
    _sweep_answers()
    client = _client_for(target_pane)
    if client is None:
        return (
            f"not asked: no tmux client is attached to the session of {target_pane}"
            " to show the question on"
        )
    token = secrets.token_hex(8)
    answer_path = _answer_path(token)
    cache.CACHE_DIR.mkdir(parents=True, exist_ok=True)
    prompt = shlex.join(
        [
            "env",
            f"PYTHONPATH={_package_root()}",
            sys.executable,
            "-m",
            "vim_ai_follower.take_over",
            str(answer_path),
            token,
            f"{seconds:g}",
            editor,
        ]
    )
    try:
        try:
            shown = subprocess.run(
                [
                    "tmux",
                    "display-popup",
                    "-c",
                    client,
                    "-t",
                    target_pane,
                    "-E",
                    "-w",
                    str(_POPUP_WIDTH),
                    "-h",
                    str(_POPUP_HEIGHT),
                    prompt,
                ],
                capture_output=True,
                text=True,
                check=False,
                timeout=seconds + _CLOSE_MARGIN_SECONDS,
            )
        except subprocess.TimeoutExpired:
            return "no answer: the question did not close in time"
        if shown.returncode != 0:
            return f"not asked: tmux could not show the question ({shown.stderr.strip()})"
        return _read_answer(answer_path, token)
    finally:
        answer_path.unlink(missing_ok=True)


def _read_answer(answer_path: Path, token: str) -> str:
    try:
        written = answer_path.read_text().split()
    except OSError:
        # tmux drops a popup opened while another is up, with exit 0; a popup
        # closed before its prompt started leaves no file either.
        return (
            "not asked: the question was never shown"
            " (another popup was up, or it closed before it started)"
        )
    if len(written) != 2 or written[0] != token:
        return "no answer"
    if written[1] == _STARTED:
        # The prompt ran and the popup closed without its answer: a client
        # killed, a `display-popup -C` (review m8).
        return "no answer: the question closed without one"
    if written[1] == YES:
        return YES
    return f"answered {written[1]}"


def read_key_answer(
    input_fd: int,
    seconds: float,
    clock: Callable[[], float] = time.monotonic,
) -> str:
    """Wait up to `seconds` for one key on `input_fd` (already raw): "yes"
    for a lone `y` read at least ARM_SECONDS in, "no" for any other read,
    "timeout" for none."""
    started = clock()
    while True:
        remaining = seconds - (clock() - started)
        if remaining <= 0:
            return "timeout"
        ready, _, _ = select.select([input_fd], [], [], remaining)
        if not ready:
            continue
        data = os.read(input_fd, 64)
        if data == b"y" and clock() - started >= ARM_SECONDS:
            return YES
        return "no"


def run_prompt(input_fd: int, output: Callable[[str], None], seconds: float, editor: str) -> str:
    """The popup's side: show the question, read the answer with the terminal
    raw (restored after), and return it."""
    saved = termios.tcgetattr(input_fd)
    # TCSANOW, never setraw's default TCSAFLUSH: that discards a key typed
    # before the switch (which must answer "no" here, not vanish), and it
    # waits for pending output to drain first.
    tty.setraw(input_fd, termios.TCSANOW)
    try:
        output(
            f"Claude is editing a file, and your {editor}\r\n"
            "is in the middle of something.\r\n"
            "Take it over to show the edit?\r\n"
            f"  y = yes    any other key = no  ({seconds:g} s)\r\n"
        )
        return read_key_answer(input_fd, seconds)
    finally:
        termios.tcsetattr(input_fd, termios.TCSANOW, saved)


def _write_answer(answer_path: Path, token: str, answer: str) -> None:
    temporary = answer_path.with_name(f".{answer_path.name}.{token}")
    temporary.write_text(f"{token} {answer}\n")
    temporary.replace(answer_path)


def _show(text: str) -> None:
    sys.stdout.write(text)
    sys.stdout.flush()


def main(arguments: list[str]) -> int:
    answer_path, token, seconds, editor = arguments
    _write_answer(Path(answer_path), token, _STARTED)
    answer = run_prompt(sys.stdin.fileno(), _show, float(seconds), editor)
    _write_answer(Path(answer_path), token, answer)
    return 0


if __name__ == "__main__":  # pragma: no cover - runs inside the popup
    raise SystemExit(main(sys.argv[1:]))
