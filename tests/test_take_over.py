"""The take-over question (take_over.py): only a deliberate `y` is a yes."""

from __future__ import annotations

import os
import subprocess
import sys
import termios
import time
import tty
from collections.abc import Callable, Iterator
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from vim_ai_follower import cache, take_over


@pytest.fixture
def terminal() -> Iterator[tuple[int, int]]:
    """A pty pair: (the side the prompt reads, the side the user types on),
    raw as the prompt runs it: a canonical tty holds a key until Enter."""
    user_side, prompt_side = os.openpty()
    tty.setraw(prompt_side)
    try:
        yield prompt_side, user_side
    finally:
        os.close(user_side)
        os.close(prompt_side)


def _clock(*readings: float) -> MagicMock:
    return MagicMock(side_effect=list(readings))


def test_a_lone_y_after_the_arming_time_is_yes(terminal: tuple[int, int]) -> None:
    prompt_side, user_side = terminal
    os.write(user_side, b"y")
    assert take_over.read_key_answer(prompt_side, 10, _clock(0.0, 0.0, 1.0)) == take_over.YES


def test_a_y_read_before_the_arming_time_is_no(terminal: tuple[int, int]) -> None:
    # A key typed while the popup opened (measured: 23 ms in) answers no.
    prompt_side, user_side = terminal
    os.write(user_side, b"y")
    assert take_over.read_key_answer(prompt_side, 10, _clock(0.0, 0.0, 0.1)) == "no"


@pytest.mark.parametrize("typed", [b"\r", b" ", b"Y", b"n", b"yy", b"\x1b"])
def test_any_other_key_or_a_burst_is_no(terminal: tuple[int, int], typed: bytes) -> None:
    prompt_side, user_side = terminal
    os.write(user_side, typed)
    assert take_over.read_key_answer(prompt_side, 10, _clock(0.0, 0.0, 5.0)) == "no"


def test_no_key_within_the_time_is_a_timeout(terminal: tuple[int, int]) -> None:
    prompt_side, _user_side = terminal
    assert take_over.read_key_answer(prompt_side, 0.05) == "timeout"


def test_an_early_wake_with_nothing_to_read_keeps_waiting(terminal: tuple[int, int]) -> None:
    prompt_side, user_side = terminal
    os.write(user_side, b"y")
    with patch(
        "vim_ai_follower.take_over.select.select", side_effect=[([], [], []), ([1], [], [])]
    ):
        answer = take_over.read_key_answer(prompt_side, 10, _clock(0.0, 0.0, 0.5, 1.0))
    assert answer == take_over.YES


def test_a_y_typed_before_the_prompt_went_raw_answers_no(terminal: tuple[int, int]) -> None:
    # Typed while the popup opened, before the prompt's setraw: a TCSAFLUSH
    # switch would have discarded it; it must be read, and answer "no".
    prompt_side, user_side = terminal
    cooked = termios.tcgetattr(prompt_side)
    cooked[3] |= termios.ICANON
    cooked[3] &= ~termios.ECHO
    termios.tcsetattr(prompt_side, termios.TCSANOW, cooked)
    os.write(user_side, b"y")
    assert take_over.run_prompt(prompt_side, lambda _text: None, 3, "Vim") == "no"


def test_the_prompt_shows_the_question_and_restores_the_terminal(
    terminal: tuple[int, int],
) -> None:
    prompt_side, user_side = terminal
    # Back to a cooked terminal, so the restore has something to tell apart.
    cooked = termios.tcgetattr(prompt_side)
    cooked[3] |= termios.ICANON
    cooked[3] &= ~termios.ECHO
    termios.tcsetattr(prompt_side, termios.TCSANOW, cooked)
    before = termios.tcgetattr(prompt_side)
    shown: list[str] = []
    os.write(user_side, b"n")
    assert take_over.run_prompt(prompt_side, shown.append, 3, "Vim") == "no"
    assert "your Vim" in shown[0] and "y = yes" in shown[0] and "(3 s)" in shown[0]
    assert termios.tcgetattr(prompt_side) == before


def test_main_writes_the_answer_with_its_token(tmp_path: Path) -> None:
    answer_path = tmp_path / "answer.txt"
    seen: list[str] = []

    def prompt_reading_the_file(*_arguments: object) -> str:
        seen.append(answer_path.read_text())
        return "timeout"

    with (
        patch(
            "vim_ai_follower.take_over.run_prompt", side_effect=prompt_reading_the_file
        ) as prompt,
        patch("vim_ai_follower.take_over.sys.stdin"),
    ):
        assert take_over.main([str(answer_path), "abc", "4", "Neovim"]) == 0
    assert prompt.call_args.args[2:] == (4.0, "Neovim")
    assert seen == ["abc started\n"]
    assert answer_path.read_text() == "abc timeout\n"
    assert list(tmp_path.iterdir()) == [answer_path]


def test_show_writes_and_flushes(capsys: pytest.CaptureFixture[str]) -> None:
    take_over._show("hello")
    assert capsys.readouterr().out == "hello"


_Popup = Callable[[list[str]], "subprocess.CompletedProcess[str] | None"]


def _tmux(
    clients: str = "1792249862 /dev/ttys001\n",
    popup: _Popup | None = None,
    *,
    clients_code: int = 0,
) -> MagicMock:
    """A subprocess.run stand-in: list-clients of the target's session
    answers `clients` (`<activity> <name>` lines); the popup call runs
    `popup(argv)` (writing an answer, raising, failing)."""

    def run(argv: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        if argv[1] == "list-clients":
            assert argv == [
                "tmux",
                "list-clients",
                "-t",
                "%3",
                "-F",
                "#{client_activity} #{client_name}",
            ], argv
            return subprocess.CompletedProcess(argv, clients_code, clients, "")
        assert argv[1] == "display-popup"
        if popup is not None:
            result = popup(argv)
            if result is not None:
                return result
        return subprocess.CompletedProcess(argv, 0, "", "")

    return MagicMock(side_effect=run)


def _answering(answer: str, *, token: str | None = None) -> _Popup:
    def popup(argv: list[str]) -> None:
        words = argv[-1].split()
        answer_path, real_token = words[-4], words[-3]
        Path(answer_path).write_text(f"{token or real_token} {answer}\n")

    return popup


def test_ask_is_yes_only_for_its_own_token() -> None:
    with patch("vim_ai_follower.take_over.subprocess.run", _tmux(popup=_answering("yes"))) as run:
        assert take_over.ask("%3", "Vim", 10) == take_over.YES
    popup = run.call_args_list[1].args[0]
    assert popup[:7] == ["tmux", "display-popup", "-c", "/dev/ttys001", "-t", "%3", "-E"]
    assert "vim_ai_follower.take_over" in popup[-1] and popup[-1].endswith(" 10 Vim")
    assert f"PYTHONPATH={Path(take_over.__file__).resolve().parent.parent}" in popup[-1]
    assert run.call_args_list[1].kwargs["timeout"] == 15.0
    assert list(cache.CACHE_DIR.glob("take-over-*")) == []


def test_ask_ignores_an_answer_with_another_token() -> None:
    popup = _answering("yes", token="0123456789abcdef")
    with patch("vim_ai_follower.take_over.subprocess.run", _tmux(popup=popup)):
        assert take_over.ask("%3", "Vim", 10) == "no answer"


@pytest.mark.parametrize("answer", ["no", "timeout"])
def test_ask_reports_a_no(answer: str) -> None:
    with patch("vim_ai_follower.take_over.subprocess.run", _tmux(popup=_answering(answer))):
        assert take_over.ask("%3", "Vim", 10) == f"answered {answer}"


_NEVER_SHOWN = (
    "not asked: the question was never shown (another popup was up, or it closed before it started)"
)


def test_ask_reports_a_popup_tmux_dropped() -> None:
    with patch("vim_ai_follower.take_over.subprocess.run", _tmux()):
        assert take_over.ask("%3", "Vim", 10) == _NEVER_SHOWN


_NO_CLIENT = "not asked: no tmux client is attached to the session of %3 to show the question on"


def test_ask_without_a_pane_or_a_client_asks_nothing() -> None:
    with patch("vim_ai_follower.take_over.subprocess.run", _tmux(clients="")) as run:
        assert take_over.ask("", "Neovim", 10) == "not asked: the editor is not in tmux"
        run.assert_not_called()
        assert take_over.ask("%3", "Neovim", 10) == _NO_CLIENT
    assert len(run.call_args_list) == 1


def test_ask_shows_the_question_on_the_most_recently_active_client_of_the_targets_session() -> None:
    # Measured on tmux 3.8: `list-clients -t <pane>` lists only the clients
    # of that pane's session, and #{client_activity} moves when a client is
    # typed into; a popup with no -c went to another client (review m7).
    clients = "1792249861 /dev/ttys041\n1792249865 /dev/ttys042\nnot-a-number /dev/x\n\n"
    clients += "1792249863 /dev/ttys043\n"
    with patch(
        "vim_ai_follower.take_over.subprocess.run", _tmux(clients, _answering("yes"))
    ) as run:
        assert take_over.ask("%3", "Vim", 10) == take_over.YES
    assert run.call_args_list[1].args[0][2:4] == ["-c", "/dev/ttys042"]


@pytest.mark.parametrize(("clients", "code"), [("", 0), ("1 /dev/ttys001\n", 1), ("x y\n", 0)])
def test_ask_with_no_client_on_the_targets_session_asks_nothing(clients: str, code: int) -> None:
    # A client attached only to an unrelated session lists nothing here.
    with patch(
        "vim_ai_follower.take_over.subprocess.run", _tmux(clients, clients_code=code)
    ) as run:
        assert take_over.ask("%3", "Vim", 10) == _NO_CLIENT
    assert len(run.call_args_list) == 1


def test_ask_reports_a_question_that_closed_without_an_answer() -> None:
    # The prompt marks the answer file "started" first: a popup closed after
    # that (a client killed, `display-popup -C`) is not a dropped popup.
    with patch("vim_ai_follower.take_over.subprocess.run", _tmux(popup=_answering("started"))):
        assert take_over.ask("%3", "Vim", 10) == "no answer: the question closed without one"


def test_ask_reports_a_popup_tmux_refused() -> None:
    def refused(argv: list[str]) -> subprocess.CompletedProcess[str] | None:
        return subprocess.CompletedProcess(argv, 1, "", "can't find pane: %3\n")

    with patch("vim_ai_follower.take_over.subprocess.run", _tmux(popup=refused)):
        assert take_over.ask("%3", "Vim", 10) == (
            "not asked: tmux could not show the question (can't find pane: %3)"
        )


def test_ask_sweeps_the_answer_files_earlier_questions_left_behind() -> None:
    # Final review M2: a prompt that outlived its hook's wait (the popup
    # timed out, the hook was killed) writes its answer file after `ask`'s
    # `finally` removed it, and nothing else ever did. Each ask removes the
    # ones older than _SWEEP_AFTER_SECONDS, far past any question.
    cache.CACHE_DIR.mkdir(parents=True, exist_ok=True)
    old = time.time() - take_over._SWEEP_AFTER_SECONDS - 60
    stale = [
        cache.CACHE_DIR / "take-over-00aa.txt",
        cache.CACHE_DIR / ".take-over-00bb.txt.00bb",
    ]
    fresh = [
        cache.CACHE_DIR / "take-over-11aa.txt",
        cache.CACHE_DIR / ".take-over-11bb.txt.11bb",
    ]
    unrelated = cache.CACHE_DIR / "mode-12.txt"
    for path in [*stale, *fresh, unrelated]:
        path.write_text("x\n")
    for path in [*stale, unrelated]:
        os.utime(path, (old, old))
    with patch("vim_ai_follower.take_over.subprocess.run", _tmux(clients="")):
        assert take_over.ask("%3", "Vim", 10) == _NO_CLIENT
    assert [path.exists() for path in stale] == [False, False]
    assert [path.exists() for path in fresh] == [True, True]
    assert unrelated.exists()


def test_a_sweep_that_cannot_delete_still_asks() -> None:
    cache.CACHE_DIR.mkdir(parents=True, exist_ok=True)
    stuck = cache.CACHE_DIR / "take-over-00aa.txt"
    stuck.mkdir()
    old = time.time() - take_over._SWEEP_AFTER_SECONDS - 60
    os.utime(stuck, (old, old))
    with patch("vim_ai_follower.take_over.subprocess.run", _tmux(popup=_answering("no"))):
        assert take_over.ask("%3", "Vim", 10) == "answered no"


def test_a_sweep_with_no_cache_dir_still_asks() -> None:
    assert not cache.CACHE_DIR.exists()
    with patch("vim_ai_follower.take_over.subprocess.run", _tmux(popup=_answering("no"))):
        assert take_over.ask("%3", "Vim", 10) == "answered no"


def test_ask_gives_up_on_a_popup_that_does_not_close() -> None:
    def hangs(argv: list[str]) -> None:
        raise subprocess.TimeoutExpired(argv, 15)

    with patch("vim_ai_follower.take_over.subprocess.run", _tmux(popup=hangs)):
        assert take_over.ask("%3", "Vim", 10) == "no answer: the question did not close in time"


def test_the_prompt_runs_as_a_module() -> None:
    # The popup runs `python -m vim_ai_follower.take_over`: the module must
    # import and refuse a wrong argument count without a terminal.
    result = subprocess.run(
        [sys.executable, "-c", "import vim_ai_follower.take_over as module; print(module.YES)"],
        capture_output=True,
        text=True,
        check=False,
        env={**os.environ, "PYTHONPATH": str(Path(take_over.__file__).resolve().parent.parent)},
    )
    assert result.stdout.strip() == "yes", result.stderr


@pytest.mark.parametrize(
    ("mode", "reason"),
    [
        ("i", "in insert mode"),
        ("niI", "in insert mode"),
        ("Rv", "in replace mode"),
        ("niV", "in replace mode"),
        ("t", "in terminal mode"),
        ("ntT", "in terminal mode"),
        ("Vs", "in select mode"),
        ("\x13", "in select mode"),
        ("\x16", "in visual mode"),
        ("n", None),
        ("nt", None),
        ("c", None),
        ("no", None),
        (None, None),
    ],
)
def test_typing_reason_names_the_typing_modes_of_vim_and_nvim(
    mode: str | None, reason: str | None
) -> None:
    assert take_over.typing_reason(mode) == reason
    assert reason is None or reason in take_over.TYPING_REASONS
