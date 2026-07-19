"""Characterizes TmuxStatusSurface against the exact tmux commands the writer
cue / handoff cue / stop restore issued before this abstraction existed.
tests/test_cli_hooks.py and tests/test_cli_management.py exercise the real
call sites (hooks.py, commands.py) and remain the end-to-end oracle; these
tests pin the surface's own command sequence directly."""

from __future__ import annotations

from unittest.mock import patch

from helpers import make_mock_tmux_run

from vim_ai_follower.state import FollowerState
from vim_ai_follower.status_surface import TmuxStatusSurface, status_surface_for


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
