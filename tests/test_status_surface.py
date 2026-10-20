"""Characterizes TmuxStatusSurface against the exact tmux commands the writer
cue / handoff cue / stop restore issued before this abstraction existed.
tests/test_cli_hooks.py and tests/test_cli_management.py exercise the real
call sites (hooks.py, commands.py) and remain the end-to-end oracle; these
tests pin the surface's own command sequence directly."""

from __future__ import annotations

import shutil
import tempfile
import threading
import time
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, call, patch

import pynvim
import pytest
from helpers import fake_nvim, make_mock_tmux_run

from vim_ai_follower.hooks import HANDOFF_CUE_READONLY
from vim_ai_follower.state import FollowerState
from vim_ai_follower.status_surface import (
    NvimStatusSurface,
    TmuxStatusSurface,
    _centered_box,
    _cterm_to_hex,
    _window_width,
    display_width,
    status_surface_for,
)


def test_set_writer_tints_border_sets_status_top_and_title() -> None:
    surface = TmuxStatusSurface(pane_id="%2")
    with patch("vim_ai_follower.tmux.subprocess.run", side_effect=make_mock_tmux_run()) as run:
        surface.set_writer("code-reviewer", "colour78")
    cmds = [recorded_call.args[0] for recorded_call in run.call_args_list]
    assert ["tmux", "set-option", "-p", "-t", "%2", "pane-border-style", "fg=colour78"] in cmds
    assert [
        "tmux",
        "set-option",
        "-p",
        "-t",
        "%2",
        "pane-active-border-style",
        "fg=colour78",
    ] in cmds
    assert ["tmux", "set-option", "-w", "-t", "%2", "pane-border-status", "top"] in cmds
    assert ["tmux", "select-pane", "-t", "%2", "-T", "code-reviewer"] in cmds


def test_set_writer_with_no_color_unsets_border_style() -> None:
    surface = TmuxStatusSurface(pane_id="%2")
    with patch("vim_ai_follower.tmux.subprocess.run", side_effect=make_mock_tmux_run()) as run:
        surface.set_writer("code-reviewer", None)
    cmds = [recorded_call.args[0] for recorded_call in run.call_args_list]
    assert ["tmux", "set-option", "-pu", "-t", "%2", "pane-border-style"] in cmds
    assert ["tmux", "set-option", "-pu", "-t", "%2", "pane-active-border-style"] in cmds


def test_clear_with_no_prior_set_state_resets_border_and_status() -> None:
    # Mirrors cmd_stop's restore: a freshly constructed surface's clear()
    # unconditionally unsets the border color and pane-border-status,
    # regardless of what (if anything) was showing.
    surface = TmuxStatusSurface(pane_id="%2")
    with patch("vim_ai_follower.tmux.subprocess.run", side_effect=make_mock_tmux_run()) as run:
        surface.clear()
    cmds = [recorded_call.args[0] for recorded_call in run.call_args_list]
    assert ["tmux", "set-option", "-pu", "-t", "%2", "pane-border-style"] in cmds
    assert ["tmux", "set-option", "-pu", "-t", "%2", "pane-active-border-style"] in cmds
    assert ["tmux", "set-option", "-wu", "-t", "%2", "pane-border-status"] in cmds


def test_set_state_saves_prior_title_and_status_then_clear_restores_them() -> None:
    # Mirrors _await_user_handoff: set_state shows the cue after saving
    # whatever title/border-status was there, and the matching clear()
    # restores exactly that saved pair -- never touching border color.
    surface = TmuxStatusSurface(pane_id="%2")
    with patch("vim_ai_follower.tmux.subprocess.run", side_effect=make_mock_tmux_run()) as run:
        surface.set_state("Claude waiting")
    cmds = [recorded_call.args[0] for recorded_call in run.call_args_list]
    assert ["tmux", "display-message", "-p", "-t", "%2", "#{pane_title}"] in cmds
    assert ["tmux", "show-options", "-wv", "-t", "%2", "pane-border-status"] in cmds
    assert ["tmux", "select-pane", "-t", "%2", "-T", "Claude waiting"] in cmds
    assert ["tmux", "set-option", "-w", "-t", "%2", "pane-border-status", "top"] in cmds
    assert not any("pane-border-style" in command for command in cmds)

    with patch("vim_ai_follower.tmux.subprocess.run", side_effect=make_mock_tmux_run()) as run:
        surface.clear()
    cmds = [recorded_call.args[0] for recorded_call in run.call_args_list]
    # make_mock_tmux_run's display-message stub always answers "@1" (its
    # default window_id) for #{pane_title} too -- that is the "saved" title
    # restored here.
    assert ["tmux", "select-pane", "-t", "%2", "-T", "@1"] in cmds
    assert ["tmux", "set-option", "-wu", "-t", "%2", "pane-border-status"] in cmds
    assert not any("pane-border-style" in command for command in cmds)


def test_clear_restores_a_saved_title_that_ends_in_a_semicolon() -> None:
    # T23 re-review m-a: the user's `vim x;` came back as `vim x`.
    surface = TmuxStatusSurface(pane_id="%2")
    with patch("vim_ai_follower.tmux.subprocess.run") as run:
        run.return_value = MagicMock(returncode=0, stdout="vim x;\n")
        surface.set_state("Claude waiting")
    with patch("vim_ai_follower.tmux.subprocess.run") as run:
        surface.clear()
    cmds = [recorded_call.args[0] for recorded_call in run.call_args_list]
    assert ["tmux", "select-pane", "-t", "%2", "-T", "vim x\\;"] in cmds


def test_set_state_twice_keeps_the_first_saved_title_for_clear_to_restore() -> None:
    # The des-interrupt calls set_state a second time ("Writing...") over the
    # handoff cue; the second call must NOT re-save (clobbering the original the
    # first save captured), so clear() still restores the original title.
    surface = TmuxStatusSurface(pane_id="%2")
    with patch("vim_ai_follower.tmux.subprocess.run", side_effect=make_mock_tmux_run()):
        surface.set_state("Claude waiting")
        surface.set_state("Writing...")
    with patch("vim_ai_follower.tmux.subprocess.run", side_effect=make_mock_tmux_run()) as run:
        surface.clear()
    cmds = [recorded_call.args[0] for recorded_call in run.call_args_list]
    # restores the ORIGINAL title (mock answers "@1"), not "Claude waiting".
    assert ["tmux", "select-pane", "-t", "%2", "-T", "@1"] in cmds


def test_set_state_with_none_and_nothing_saved_falls_back_to_full_reset() -> None:
    # set_state(None) is the same "clear" request clear() serves elsewhere;
    # without a prior set_state to restore, it must fall back to the same
    # unconditional reset clear() performs.
    surface = TmuxStatusSurface(pane_id="%2")
    with patch("vim_ai_follower.tmux.subprocess.run", side_effect=make_mock_tmux_run()) as run:
        surface.set_state(None)
    cmds = [recorded_call.args[0] for recorded_call in run.call_args_list]
    assert ["tmux", "set-option", "-pu", "-t", "%2", "pane-border-style"] in cmds
    assert ["tmux", "set-option", "-wu", "-t", "%2", "pane-border-status"] in cmds


def test_status_surface_for_returns_tmux_surface_bound_to_the_target_pane() -> None:
    state = FollowerState(
        backend="tmux",
        target="%2",
        current_file=None,
        origin="%1",
        on_failure="reopen",
        speed="normal",
    )
    surface = status_surface_for(state)
    assert isinstance(surface, TmuxStatusSurface)
    assert surface.pane_id == "%2"


def test_status_surface_for_returns_nvim_surface_bound_to_the_socket_path() -> None:
    state = FollowerState(
        backend="nvim",
        target="/tmp/nvim-@1.sock",
        current_file=None,
        origin="%1",
        on_failure="reopen",
        speed="normal",
    )
    surface = status_surface_for(state)
    assert isinstance(surface, NvimStatusSurface)
    assert surface.socket_path == "/tmp/nvim-@1.sock"


# --- NvimStatusSurface (mocked pynvim.attach) -----------------------------


def test_cterm_to_hex_covers_the_color_cube_and_the_grayscale_ramp() -> None:
    # Cube (16-231): colour78 is the green PALETTE entry.
    assert _cterm_to_hex(78) == "#5fd787"
    # Grayscale ramp (232-255): level = 8 + 10*(n-232).
    assert _cterm_to_hex(232) == "#080808"
    assert _cterm_to_hex(255) == "#eeeeee"


def test_a_cue_into_a_frozen_nvim_gives_up_at_the_bounded_connect() -> None:
    # A SIGSTOPped nvim accepts the connection and never answers. The cue is
    # a hook's first call into the follower: unbounded, it held the hook (and
    # Claude's tool call) until Claude Code's own timeout.
    directory = Path(tempfile.mkdtemp(prefix="vafq", dir="/tmp"))  # short: AF_UNIX
    sock = directory / "frozen.sock"
    try:
        with (
            fake_nvim(sock, reply=None) as frozen,
            patch("vim_ai_follower.backends.nvim_connect.CONNECT_TIMEOUT_SECONDS", 0.2),
        ):
            # With the bound gone the cue would wait forever: hanging up after
            # 5 s ends it, and the timing assertion FAILS instead of hanging.
            hang_guard = threading.Timer(5.0, frozen.close)
            hang_guard.start()
            started = time.monotonic()
            try:
                NvimStatusSurface(socket_path=str(sock)).set_writer("code-reviewer", "colour78")
            finally:
                hang_guard.cancel()
            assert time.monotonic() - started < 2.0
    finally:
        shutil.rmtree(directory, ignore_errors=True)


def test_set_writer_creates_a_floating_window_when_none_exists() -> None:
    # Two pre-existing windows that must both be skipped by the search: a
    # non-floating one, and a floating one showing an unrelated buffer.
    nvim = MagicMock()
    nvim.api.list_wins.return_value = [1, 2]
    nvim.api.win_get_config.side_effect = lambda win: (
        {"relative": ""} if win == 1 else {"relative": "editor"}
    )
    nvim.api.win_get_buf.side_effect = lambda win: 99
    nvim.api.buf_get_name.side_effect = lambda buffer: "/tmp/other-file.py"
    nvim.api.create_buf.return_value = 7
    nvim.api.open_win.return_value = 3
    nvim.api.buf_get_lines.return_value = []
    nvim.api.win_get_cursor.return_value = (5, 0)

    with patch("pynvim.attach", return_value=nvim) as attach:
        NvimStatusSurface(socket_path="/tmp/x.sock").set_writer("code-reviewer", "colour78")

    # Every connection targets the follower's socket (the cue's own, plus the
    # hit-enter watchdog's on a dedicated follower — see nvim_prompt).
    assert attach.call_args_list
    assert all(
        recorded_call == call("socket", path="/tmp/x.sock")
        for recorded_call in attach.call_args_list
    )
    nvim.api.create_buf.assert_called_once_with(False, True)
    nvim.api.buf_set_name.assert_called_once()
    nvim.api.open_win.assert_called_once()
    # The body is BLANK: the box is pure identity (label lives in the title).
    # An activity line here would outlive the animation and lie ("Writing..."
    # forever after completion — live finding, 2026-08-25 battery Check 5);
    # transient text is set_state's job, asserted separately.
    nvim.api.buf_set_lines.assert_any_call(7, 0, -1, True, _centered_box([]))
    # cterm AND gui, so the cue shows under termguicolors too (colour78 -> #5fd787)
    nvim.command.assert_any_call(f"highlight VafWriterCue ctermfg=78 guifg={_cterm_to_hex(78)}")
    nvim.api.win_set_config.assert_any_call(
        3, {"title": [[" code-reviewer ", "VafWriterCue"]], "title_pos": "center"}
    )
    nvim.api.win_set_option.assert_any_call(3, "winhighlight", "FloatBorder:VafWriterCue")
    # Blank body -> nothing to highlight in it (the colored title and border
    # carry the identity; the cursor extmark below carries it inline).
    nvim.api.buf_add_highlight.assert_not_called()
    nvim.api.buf_set_extmark.assert_called_once()
    extmark_call = nvim.api.buf_set_extmark.call_args
    assert extmark_call.args[2] == 4  # row - 1, from win_get_cursor's (5, 0)


def test_set_writer_reuses_an_existing_floating_window_and_clears_the_color_when_none() -> None:
    nvim = MagicMock()
    nvim.api.list_wins.return_value = [3]
    nvim.api.win_get_config.return_value = {"relative": "editor"}
    nvim.api.win_get_buf.return_value = 7
    nvim.api.buf_get_name.return_value = "vaf-status"
    nvim.api.buf_get_lines.return_value = ["old-label", "Claude waiting"]
    nvim.api.win_get_cursor.return_value = (1, 0)

    with patch("pynvim.attach", return_value=nvim):
        NvimStatusSurface(socket_path="/tmp/x.sock").set_writer("new-writer", None)

    nvim.api.create_buf.assert_not_called()
    nvim.api.open_win.assert_not_called()
    # Body resets to blank; transient state is not preserved (identity lives
    # in the title, and set_state owns any transient body text).
    nvim.api.buf_set_lines.assert_any_call(7, 0, -1, True, _centered_box([]))
    nvim.api.win_set_config.assert_any_call(
        3, {"title": [[" new-writer ", "Title"]], "title_pos": "center"}
    )
    nvim.api.win_set_option.assert_any_call(3, "winhighlight", "")  # color=None clears the tint
    nvim.command.assert_not_called()  # color=None: no highlight command issued
    nvim.api.buf_add_highlight.assert_not_called()


def test_set_state_centers_the_text_and_opens_a_window_if_needed() -> None:
    nvim = MagicMock()
    nvim.api.get_option_value.return_value = 80  # columns
    nvim.api.list_wins.return_value = []
    nvim.api.create_buf.return_value = 7
    nvim.api.open_win.return_value = 3
    nvim.api.buf_get_lines.return_value = []

    with patch("pynvim.attach", return_value=nvim):
        NvimStatusSurface(socket_path="/tmp/x.sock").set_state("Claude waiting")

    nvim.api.buf_set_lines.assert_any_call(7, 0, -1, True, _centered_box(["Claude waiting"]))
    nvim.api.open_win.assert_called_once()


def test_set_state_reuses_an_existing_window_without_recreating_it() -> None:
    nvim = MagicMock()
    nvim.api.get_option_value.return_value = 80  # columns
    nvim.api.list_wins.return_value = [3]
    nvim.api.win_get_config.return_value = {"relative": "editor"}
    nvim.api.win_get_buf.return_value = 7
    nvim.api.buf_get_name.return_value = "vaf-status"
    nvim.api.buf_get_lines.return_value = ["code-reviewer"]

    with patch("pynvim.attach", return_value=nvim):
        NvimStatusSurface(socket_path="/tmp/x.sock").set_state("Claude waiting")

    nvim.api.open_win.assert_not_called()
    nvim.api.buf_set_lines.assert_any_call(7, 0, -1, True, _centered_box(["Claude waiting"]))


def test_set_state_with_none_delegates_to_clear() -> None:
    nvim = MagicMock()
    nvim.api.list_wins.return_value = [3]
    nvim.api.win_get_config.return_value = {"relative": "editor"}
    nvim.api.win_get_buf.return_value = 7
    nvim.api.buf_get_name.return_value = "vaf-status"
    nvim.api.get_current_buf.return_value = 1

    with patch("pynvim.attach", return_value=nvim):
        NvimStatusSurface(socket_path="/tmp/x.sock").set_state(None)

    nvim.api.win_close.assert_called_once_with(3, True)


def test_clear_closes_the_window_wipes_its_buffer_and_clears_cursor_virtual_text() -> None:
    nvim = MagicMock()
    nvim.api.list_wins.return_value = [3]
    nvim.api.win_get_config.return_value = {"relative": "editor"}
    # A Buffer OBJECT, as real pynvim returns — not a bare int. An int made
    # f"{buf}" read "7" and hid that the real Buffer formats as
    # "<Buffer(handle=7)>", which :bwipeout silently rejects.
    nvim.api.win_get_buf.return_value = MagicMock(number=7)
    nvim.api.buf_get_name.return_value = "vaf-status"
    nvim.api.get_current_buf.return_value = 1

    with patch("pynvim.attach", return_value=nvim):
        NvimStatusSurface(socket_path="/tmp/x.sock").clear()

    nvim.api.win_close.assert_called_once_with(3, True)
    nvim.command.assert_any_call("silent! bwipeout! 7")
    nvim.api.buf_clear_namespace.assert_called_once_with(
        1, nvim.api.create_namespace.return_value, 0, -1
    )


def test_clear_with_no_window_still_clears_cursor_virtual_text_without_raising() -> None:
    nvim = MagicMock()
    nvim.api.list_wins.return_value = []
    nvim.api.get_current_buf.return_value = 1

    with patch("pynvim.attach", return_value=nvim):
        NvimStatusSurface(socket_path="/tmp/x.sock").clear()

    nvim.api.win_close.assert_not_called()
    nvim.api.buf_clear_namespace.assert_called_once()


def test_all_methods_swallow_a_dead_socket_instead_of_raising() -> None:
    with patch("pynvim.attach", side_effect=OSError("no such socket")):
        surface = NvimStatusSurface(socket_path="/tmp/dead.sock")
        surface.set_writer("code-reviewer", "colour78")
        surface.set_state("Claude waiting")
        surface.set_state(None)
        surface.clear()


def test_tmux_retire_state_takes_down_a_matching_leftover_cue() -> None:
    from vim_ai_follower.status_surface import TmuxStatusSurface

    with (
        patch("vim_ai_follower.status_surface.TmuxPane.title", return_value="old cue"),
        patch.object(TmuxStatusSurface, "clear") as clear,
    ):
        TmuxStatusSurface(pane_id="%9").retire_state("other cue", "old cue")
    assert clear.call_args_list == [call()]


def test_tmux_retire_state_leaves_any_other_title_alone() -> None:
    from vim_ai_follower.status_surface import TmuxStatusSurface

    with (
        patch("vim_ai_follower.status_surface.TmuxPane.title", return_value="writer label"),
        patch.object(TmuxStatusSurface, "clear") as clear,
    ):
        TmuxStatusSurface(pane_id="%9").retire_state("other cue", "old cue")
    assert clear.call_args_list == []


def _status_float(nvim: Any) -> tuple[Any, list[str], str] | None:
    """The status float's window, its non-blank body lines and its title."""
    for window in nvim.api.list_wins():
        config = nvim.api.win_get_config(window)
        if config["relative"] == "":
            continue
        buffer = nvim.api.win_get_buf(window)
        if nvim.api.buf_get_name(buffer).endswith("vaf-status"):
            body = [line.strip() for line in nvim.api.buf_get_lines(buffer, 0, -1, True)]
            title = "".join(chunk[0] for chunk in config.get("title", []))
            return window, [line for line in body if line], title
    return None


@pytest.mark.integration
def test_nvim_retire_state_closes_a_matching_cue_with_nothing_else_to_show(
    headless_nvim: str,
) -> None:
    # A "Paused" left by a press nothing obeyed (final M6): the float saves and
    # restores nothing, so before this it stayed until the next animation.
    surface = NvimStatusSurface(socket_path=headless_nvim, adopted=False)
    surface.set_state("Paused")
    nvim = pynvim.attach("socket", path=headless_nvim)
    assert _status_float(nvim) is not None
    surface.retire_state("Busy", "Paused")
    assert _status_float(nvim) is None


@pytest.mark.integration
def test_nvim_retire_state_keeps_a_writers_title_with_a_blank_body(headless_nvim: str) -> None:
    surface = NvimStatusSurface(socket_path=headless_nvim, adopted=False)
    surface.set_writer("code-reviewer", "colour78")
    surface.set_state("Paused")
    surface.retire_state("Paused")
    nvim = pynvim.attach("socket", path=headless_nvim)
    shown = _status_float(nvim)
    assert shown is not None
    assert shown[1:] == ([], " code-reviewer ")


@pytest.mark.integration
def test_nvim_retire_state_leaves_any_other_cue_alone(headless_nvim: str) -> None:
    surface = NvimStatusSurface(socket_path=headless_nvim, adopted=False)
    surface.retire_state("Paused")  # no float at all: nothing to do, no error
    nvim = pynvim.attach("socket", path=headless_nvim)
    assert _status_float(nvim) is None
    surface.set_state("Writing...")
    surface.retire_state("Paused")
    shown = _status_float(nvim)
    assert shown is not None
    assert shown[1] == ["Writing..."]


# --- hit-enter prompt (nvim_prompt) ------------------------------------------


def _prompt_blocked_nvim() -> MagicMock:
    """A mock nvim whose first get_mode reports a plugin's hit-enter prompt."""
    nvim = MagicMock()
    modes = iter([{"mode": "r", "blocking": True}] * 2)  # seen, then re-read under the lock
    nvim.api.get_mode.side_effect = lambda: next(modes, {"mode": "n", "blocking": False})
    nvim.api.exec2.return_value = {"output": ""}
    nvim.api.list_wins.return_value = []
    return nvim


def test_a_dedicated_followers_cue_sweeps_a_leftover_prompt_before_drawing() -> None:
    # "Writing..." is a hook's first ordinary call into the follower's nvim:
    # a prompt left there would freeze the hook before the follower's own
    # entry point could sweep it.
    nvim = _prompt_blocked_nvim()
    with patch("pynvim.attach", return_value=nvim):
        NvimStatusSurface(socket_path="/tmp/x.sock", adopted=False).set_state("Writing...")
    nvim.api.input.assert_called_once_with("<CR>")
    assert nvim.mock_calls.index(call.api.input("<CR>")) < nvim.mock_calls.index(
        call.api.list_wins()
    )


def test_an_adopted_editors_cue_never_answers_its_prompts() -> None:
    nvim = _prompt_blocked_nvim()
    with patch("pynvim.attach", return_value=nvim) as attach:
        NvimStatusSurface(socket_path="/tmp/x.sock", adopted=True).set_state("Writing...")
    nvim.api.get_mode.assert_not_called()
    nvim.api.input.assert_not_called()
    assert attach.call_count == 1  # no watchdog connection


def test_status_surface_for_carries_the_adopted_flag_to_the_nvim_surface() -> None:
    state = FollowerState(
        backend="nvim",
        target="/tmp/nvim-@1.sock",
        current_file=None,
        origin="%1",
        on_failure="reopen",
        speed="normal",
        adopted=True,
    )
    surface = status_surface_for(state)
    assert isinstance(surface, NvimStatusSurface)
    assert surface.adopted is True


def test_a_surface_built_without_the_adopted_flag_never_answers_prompts() -> None:
    # Unknown must not press keys: only a caller that KNOWS the follower is
    # dedicated (status_surface_for, from FollowerState) turns the sweep on.
    nvim = _prompt_blocked_nvim()
    with patch("pynvim.attach", return_value=nvim) as attach:
        NvimStatusSurface(socket_path="/tmp/x.sock").set_state("Writing...")
    nvim.api.get_mode.assert_not_called()
    nvim.api.input.assert_not_called()
    assert attach.call_count == 1


def _float_opts(nvim: MagicMock) -> dict[str, object]:
    options: dict[str, object] = nvim.api.open_win.call_args.args[2]
    return options


def _no_window_nvim(columns: int) -> MagicMock:
    nvim = MagicMock()
    nvim.api.list_wins.return_value = []
    nvim.api.create_buf.return_value = 7
    nvim.api.open_win.return_value = 3
    nvim.api.get_option_value.side_effect = lambda name, options: columns
    return nvim


def test_the_float_widens_to_fit_the_hand_off_cue_on_one_row() -> None:
    nvim = _no_window_nvim(49)
    with patch("pynvim.attach", return_value=nvim):
        NvimStatusSurface(socket_path="/tmp/x.sock").set_state(HANDOFF_CUE_READONLY)
    options = _float_opts(nvim)
    assert isinstance(options["width"], int) and isinstance(options["col"], int)
    assert options["width"] >= len(HANDOFF_CUE_READONLY)
    assert options["col"] >= 0
    assert options["col"] + options["width"] + 2 <= 49  # the border fits on screen
    body = nvim.api.buf_set_lines.call_args.args[4]
    assert HANDOFF_CUE_READONLY in [line.strip() for line in body]


def test_the_float_never_outgrows_a_narrow_editor() -> None:
    nvim = _no_window_nvim(30)
    with patch("pynvim.attach", return_value=nvim):
        NvimStatusSurface(socket_path="/tmp/x.sock").set_state(HANDOFF_CUE_READONLY)
    options = _float_opts(nvim)
    assert isinstance(options["width"], int)
    assert options["width"] <= 28
    assert options["col"] == 0


def test_a_short_cue_keeps_the_minimum_width() -> None:
    nvim = _no_window_nvim(80)
    with patch("pynvim.attach", return_value=nvim):
        NvimStatusSurface(socket_path="/tmp/x.sock").set_state("Paused")
    assert _float_opts(nvim)["width"] == 32
    assert _float_opts(nvim)["col"] == 80 - 32 - 2


def test_an_existing_float_is_resized_for_a_longer_cue() -> None:
    nvim = MagicMock()
    nvim.api.list_wins.return_value = [3]
    nvim.api.win_get_config.return_value = {"relative": "editor"}
    nvim.api.win_get_buf.return_value = 7
    nvim.api.buf_get_name.return_value = "vaf-status"
    nvim.api.get_option_value.side_effect = lambda name, options: 49
    with patch("pynvim.attach", return_value=nvim):
        NvimStatusSurface(socket_path="/tmp/x.sock").set_state(HANDOFF_CUE_READONLY)
    nvim.api.open_win.assert_not_called()
    width = len(HANDOFF_CUE_READONLY) + 2
    nvim.api.win_set_config.assert_any_call(
        3, {"relative": "editor", "row": 1, "col": 49 - width - 2, "width": width}
    )


def test_a_wide_character_cue_fits_its_float() -> None:
    cue = "\u7de8\u96c6\u4e2d \u2014 \u5f85\u6a5f"
    assert display_width(cue) == 13
    wide_cue = " ".join([cue] * 3)
    assert len(wide_cue) < 32 < display_width(wide_cue)
    assert _window_width(80, [wide_cue]) == display_width(wide_cue) + 2


def test_the_box_centers_by_display_cells() -> None:
    cue = "\u7de8\u96c6\u4e2d"
    row = next(line for line in _centered_box([cue], width=10) if line.strip())
    assert display_width(row) == 10
    assert row == "  " + cue + "  "


def test_a_combining_mark_takes_no_cell() -> None:
    assert display_width("e\u0301") == 1
