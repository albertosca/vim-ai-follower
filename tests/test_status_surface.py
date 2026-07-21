"""Characterizes TmuxStatusSurface against the exact tmux commands the writer
cue / handoff cue / stop restore issued before this abstraction existed.
tests/test_cli_hooks.py and tests/test_cli_management.py exercise the real
call sites (hooks.py, commands.py) and remain the end-to-end oracle; these
tests pin the surface's own command sequence directly."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from helpers import make_mock_tmux_run

from vim_ai_follower.state import FollowerState
from vim_ai_follower.status_surface import (
    NvimStatusSurface,
    TmuxStatusSurface,
    _centered_box,
    _cterm_to_hex,
    status_surface_for,
)


def test_set_writer_tints_border_sets_status_top_and_title() -> None:
    surface = TmuxStatusSurface(pane_id="%2")
    with patch("vim_ai_follower.tmux.subprocess.run", side_effect=make_mock_tmux_run()) as run:
        surface.set_writer("code-reviewer", "colour78")
    cmds = [c.args[0] for c in run.call_args_list]
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
    cmds = [c.args[0] for c in run.call_args_list]
    assert ["tmux", "set-option", "-pu", "-t", "%2", "pane-border-style"] in cmds
    assert ["tmux", "set-option", "-pu", "-t", "%2", "pane-active-border-style"] in cmds


def test_clear_with_no_prior_set_state_resets_border_and_status() -> None:
    # Mirrors cmd_stop's restore: a freshly constructed surface's clear()
    # unconditionally unsets the border color and pane-border-status,
    # regardless of what (if anything) was showing.
    surface = TmuxStatusSurface(pane_id="%2")
    with patch("vim_ai_follower.tmux.subprocess.run", side_effect=make_mock_tmux_run()) as run:
        surface.clear()
    cmds = [c.args[0] for c in run.call_args_list]
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
    cmds = [c.args[0] for c in run.call_args_list]
    assert ["tmux", "display-message", "-p", "-t", "%2", "#{pane_title}"] in cmds
    assert ["tmux", "show-options", "-wv", "-t", "%2", "pane-border-status"] in cmds
    assert ["tmux", "select-pane", "-t", "%2", "-T", "Claude waiting"] in cmds
    assert ["tmux", "set-option", "-w", "-t", "%2", "pane-border-status", "top"] in cmds
    assert not any("pane-border-style" in c for c in cmds)

    with patch("vim_ai_follower.tmux.subprocess.run", side_effect=make_mock_tmux_run()) as run:
        surface.clear()
    cmds = [c.args[0] for c in run.call_args_list]
    # make_mock_tmux_run's display-message stub always answers "@1" (its
    # default window_id) for #{pane_title} too -- that is the "saved" title
    # restored here.
    assert ["tmux", "select-pane", "-t", "%2", "-T", "@1"] in cmds
    assert ["tmux", "set-option", "-wu", "-t", "%2", "pane-border-status"] in cmds
    assert not any("pane-border-style" in c for c in cmds)


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
    cmds = [c.args[0] for c in run.call_args_list]
    # restores the ORIGINAL title (mock answers "@1"), not "Claude waiting".
    assert ["tmux", "select-pane", "-t", "%2", "-T", "@1"] in cmds


def test_set_state_with_none_and_nothing_saved_falls_back_to_full_reset() -> None:
    # set_state(None) is the same "clear" request clear() serves elsewhere;
    # without a prior set_state to restore, it must fall back to the same
    # unconditional reset clear() performs.
    surface = TmuxStatusSurface(pane_id="%2")
    with patch("vim_ai_follower.tmux.subprocess.run", side_effect=make_mock_tmux_run()) as run:
        surface.set_state(None)
    cmds = [c.args[0] for c in run.call_args_list]
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


def test_set_writer_creates_a_floating_window_when_none_exists() -> None:
    # Two pre-existing windows that must both be skipped by the search: a
    # non-floating one, and a floating one showing an unrelated buffer.
    nvim = MagicMock()
    nvim.api.list_wins.return_value = [1, 2]
    nvim.api.win_get_config.side_effect = lambda win: (
        {"relative": ""} if win == 1 else {"relative": "editor"}
    )
    nvim.api.win_get_buf.side_effect = lambda win: 99
    nvim.api.buf_get_name.side_effect = lambda buf: "/tmp/other-file.py"
    nvim.api.create_buf.return_value = 7
    nvim.api.open_win.return_value = 3
    nvim.api.buf_get_lines.return_value = []
    nvim.api.win_get_cursor.return_value = (5, 0)

    with patch("pynvim.attach", return_value=nvim) as attach:
        NvimStatusSurface(socket_path="/tmp/x.sock").set_writer("code-reviewer", "colour78")

    attach.assert_called_once_with("socket", path="/tmp/x.sock")
    nvim.api.create_buf.assert_called_once_with(False, True)
    nvim.api.buf_set_name.assert_called_once()
    nvim.api.open_win.assert_called_once()
    nvim.api.buf_set_lines.assert_any_call(7, 0, -1, True, _centered_box(["Writing..."]))
    # cterm AND gui, so the cue shows under termguicolors too (colour78 -> #5fd787)
    nvim.command.assert_any_call(f"highlight VafWriterCue ctermfg=78 guifg={_cterm_to_hex(78)}")
    nvim.api.win_set_config.assert_any_call(
        3, {"title": [[" code-reviewer ", "VafWriterCue"]], "title_pos": "center"}
    )
    nvim.api.win_set_option.assert_any_call(3, "winhighlight", "FloatBorder:VafWriterCue")
    nvim.api.buf_add_highlight.assert_called_once()
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
    # Body is the centered name; the state line is not preserved (state lives
    # in the title now, and set_state owns the body).
    nvim.api.buf_set_lines.assert_any_call(7, 0, -1, True, _centered_box(["Writing..."]))
    nvim.api.win_set_config.assert_any_call(
        3, {"title": [[" new-writer ", "Title"]], "title_pos": "center"}
    )
    nvim.api.win_set_option.assert_any_call(3, "winhighlight", "")  # color=None clears the tint
    nvim.command.assert_not_called()  # color=None: no highlight command issued
    nvim.api.buf_add_highlight.assert_not_called()


def test_set_state_centers_the_text_and_opens_a_window_if_needed() -> None:
    nvim = MagicMock()
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
    nvim.api.win_get_buf.return_value = 7
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
