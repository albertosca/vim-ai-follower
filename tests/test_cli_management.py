from __future__ import annotations

import functools
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from helpers import make_mock_tmux_run
from helpers import register_fake_follower as _register_fake_follower

from vim_ai_follower import commands, config, control, keybindings, session, snapshot, state
from vim_ai_follower.backends.nvim import _FIND_BUFFER_LUA

_mock_tmux_run = functools.partial(make_mock_tmux_run, pane_id="%9", other_panes=("%1", "%2"))


def test_start_without_tmux_env_fails() -> None:
    assert commands.cmd_start({}) == 1


def test_start_creates_and_registers_a_pane() -> None:
    with patch("vim_ai_follower.tmux.subprocess.run", side_effect=_mock_tmux_run()):
        assert commands.cmd_start({"TMUX_PANE": "%1"}) == 0
        result = state.FollowerState.get("@1")
    assert result is not None
    assert result.target == "%9"


def test_start_is_idempotent_when_already_running() -> None:
    with patch("vim_ai_follower.tmux.subprocess.run", side_effect=_mock_tmux_run()):
        commands.cmd_start({"TMUX_PANE": "%1"})
        exit_code = commands.cmd_start({"TMUX_PANE": "%1"})
        result = state.FollowerState.get("@1")
    assert exit_code == 0
    assert result is not None
    assert result.target == "%9"


def test_stop_kills_pane_and_clears_state() -> None:
    with patch("vim_ai_follower.tmux.subprocess.run", side_effect=_mock_tmux_run()):
        commands.cmd_start({"TMUX_PANE": "%1"})
        exit_code = commands.cmd_stop({"TMUX_PANE": "%1"})
        result = state.FollowerState.get("@1")
    assert exit_code == 0
    assert result is None


def test_stop_dead_tmux_pane_fails() -> None:
    # {} alone no longer means "fail" — no TMUX_PANE now resolves to the
    # standalone session. A dead-pane resolve_session() -> None is the case
    # that must still fail, mirroring the old _require_window(env) is None.
    with patch("vim_ai_follower.commands.resolve_session", return_value=None):
        assert commands.cmd_stop({"TMUX_PANE": "%1"}) == 1


def test_stop_without_active_follower_is_a_noop() -> None:
    with patch("vim_ai_follower.tmux.subprocess.run", side_effect=_mock_tmux_run()):
        assert commands.cmd_stop({"TMUX_PANE": "%1"}) == 0


def test_stop_standalone_stops_the_backend_but_skips_tmux_side_effects() -> None:
    # Standalone (no TMUX_PANE): resolve_session() returns a Session with
    # in_tmux=False. cmd_stop must still drive the backend's stop() (an nvim
    # RPC qall!, not a tmux operation) and clear the follower's own status
    # surface, but must NOT touch tmux at all — no border restore shell-out,
    # no keybindings.unregister() (standalone never registered tmux keys).
    standalone_session = session.Session(window_id="term-x", origin=None, in_tmux=False)
    state.FollowerState.set("term-x", "nvim", "/tmp/standalone.sock", origin="")
    with (
        patch("vim_ai_follower.commands.resolve_session", return_value=standalone_session),
        patch("pynvim.attach", return_value=MagicMock()) as attach,
        patch("vim_ai_follower.tmux.subprocess.run", side_effect=_mock_tmux_run()) as run,
        patch("vim_ai_follower.commands.keybindings.unregister") as mock_unregister,
    ):
        assert commands.cmd_stop({}) == 0
    attach.assert_called()  # the nvim backend's stop() (qall!) actually ran
    mock_unregister.assert_not_called()
    border_calls = [
        c.args[0]
        for c in run.call_args_list
        if c.args[0][:2] == ["tmux", "set-option"] and "pane-border-style" in c.args[0]
    ]
    assert border_calls == []
    assert state.FollowerState.get("term-x") is None


def test_speed_standalone_updates_speed_but_skips_the_tmux_status_line() -> None:
    # Standalone: cmd_speed must still persist the new speed, but must NOT flash
    # the tmux status line — current.target is an nvim socket path, not a pane,
    # so the show_status shell-out is gated on session.in_tmux.
    standalone_session = session.Session(window_id="term-x", origin=None, in_tmux=False)
    state.FollowerState.set("term-x", "nvim", "/tmp/standalone.sock", origin="", speed="rapido")
    with (
        patch("vim_ai_follower.commands.resolve_session", return_value=standalone_session),
        patch("pynvim.attach", return_value=MagicMock()),
        patch("vim_ai_follower.commands.tmux.show_status") as show_status,
    ):
        assert commands.cmd_speed({}, "up") == 0
    show_status.assert_not_called()
    updated = state.FollowerState.read("term-x")
    assert updated is not None and updated.speed == "muito_rapido"


def test_status_reports_no_follower_and_names_the_identity(
    capsys: pytest.CaptureFixture[str],
) -> None:
    # "no follower" alone is ambiguous between "nothing is running" and "this
    # run is asking about the wrong identity", which is exactly the confusion
    # that cost a session half an hour on 2026-09-22.
    with patch("vim_ai_follower.tmux.subprocess.run", side_effect=_mock_tmux_run()):
        commands.cmd_status({"TMUX_PANE": "%1"})
    out = capsys.readouterr().out
    assert "no follower for @1 (tmux)" in out


def test_status_reports_active_follower(capsys: pytest.CaptureFixture[str]) -> None:
    # A live follower in ANOTHER window has to exist for the one-line
    # assertion below to mean anything: with no other follower, a roster leak
    # onto the happy path prints nothing and the canary comes back blind.
    _register_fake_follower("@18", "%23")
    run = make_mock_tmux_run(pane_id="%9", other_panes=("%1", "%2"), vim_panes=("%23",))
    with patch("vim_ai_follower.tmux.subprocess.run", side_effect=run):
        commands.cmd_start({"TMUX_PANE": "%1"})
        capsys.readouterr()  # drop cmd_start's own line
        commands.cmd_status({"TMUX_PANE": "%1"})
    out = capsys.readouterr().out
    assert "%9" in out
    assert "active for @1 (tmux)" in out
    # The happy path stays one line: Alberto routinely has followers in
    # several windows and reads this constantly — it must not hand him a
    # roster every time.
    assert len(out.strip().splitlines()) == 1


def test_status_standalone_reports_no_follower(capsys: pytest.CaptureFixture[str]) -> None:
    # {} with no TMUX_PANE resolves to the standalone session now, not a
    # failure — a plain "not running inside tmux" no longer applies here.
    standalone_session = session.Session(window_id="term-x", origin=None, in_tmux=False)
    with patch("vim_ai_follower.commands.resolve_session", return_value=standalone_session):
        assert commands.cmd_status({}) == 0
    assert "no follower for term-x (not in tmux)" in capsys.readouterr().out


def test_status_lists_live_followers_under_other_identities(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The lost-identity case at the CLI: the run resolves to a synthetic id
    with nothing registered, while the real window's follower is alive. Target
    pane and current file are what let Alberto recognise which window it is."""
    _register_fake_follower("@18", "%23", current_file="/tmp/real.py")
    lost = session.Session(window_id="term-w0t0p0:ABC", origin=None, in_tmux=False)
    with (
        patch("vim_ai_follower.commands.resolve_session", return_value=lost),
        patch(
            "vim_ai_follower.tmux.subprocess.run",
            side_effect=make_mock_tmux_run(pane_id="%23"),
        ),
    ):
        assert commands.cmd_status({}) == 0
    out = capsys.readouterr().out
    assert "no follower for term-w0t0p0:ABC (not in tmux)" in out
    assert "may have lost its window identity" in out
    assert "@18: tmux %23, showing /tmp/real.py" in out


def test_status_dead_tmux_pane(capsys: pytest.CaptureFixture[str]) -> None:
    # resolve_session returns None ONLY when TMUX_PANE is set and tmux cannot
    # resolve it, so the message has to name that — saying "not running inside
    # tmux" was exactly backwards and cost half an hour on 2026-09-22.
    with patch("vim_ai_follower.commands.resolve_session", return_value=None):
        assert commands.cmd_status({"TMUX_PANE": "%1"}) == 0
    out = capsys.readouterr().out
    assert "could not resolve TMUX_PANE=%1" in out
    assert "not running inside tmux" not in out


def test_stop_names_the_unresolvable_pane_on_stderr(capsys: pytest.CaptureFixture[str]) -> None:
    # Same wording on every command that gives up on a None session, not just
    # status — the defect was a class, present at all seven call sites.
    with patch("vim_ai_follower.commands.resolve_session", return_value=None):
        assert commands.cmd_stop({"TMUX_PANE": "%7"}) == 1
    assert "could not resolve TMUX_PANE=%7" in capsys.readouterr().err


def test_start_nvim_launches_dedicated_and_records_not_adopted() -> None:
    with (
        patch("vim_ai_follower.tmux.subprocess.run", side_effect=_mock_tmux_run()),
        patch(
            "vim_ai_follower.commands.resolve_nvim_target",
            return_value=("/tmp/nvim.sock", True),
        ) as resolve,
    ):
        assert commands.cmd_start({"TMUX_PANE": "%1"}, backend="nvim") == 0
    resolve.assert_called_once_with("%1", "@1", adopt=False)
    result = state.FollowerState.read("@1")
    assert result is not None
    assert result.backend == "nvim"
    assert result.target == "/tmp/nvim.sock"
    assert result.adopted is False  # launched, so a dedicated (relockable) nvim


def test_start_nvim_adopts_existing_and_records_adopted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config_path = tmp_path / "config.json"
    config_path.write_text('{"adopt_existing": true}')
    monkeypatch.setattr(config, "CONFIG_PATH", config_path)
    with (
        patch("vim_ai_follower.tmux.subprocess.run", side_effect=_mock_tmux_run()),
        patch(
            "vim_ai_follower.commands.resolve_nvim_target",
            return_value=("/found.sock", False),
        ) as resolve,
    ):
        assert commands.cmd_start({"TMUX_PANE": "%1"}, backend="nvim") == 0
    resolve.assert_called_once_with("%1", "@1", adopt=True)
    result = state.FollowerState.read("@1")
    assert result is not None
    assert result.adopted is True  # adopted the user's own nvim; never relocked


def test_start_uses_config_backend_nvim_without_flag(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config_path = tmp_path / "config.json"
    config_path.write_text('{"backend": "nvim"}')
    monkeypatch.setattr(config, "CONFIG_PATH", config_path)
    with (
        patch("vim_ai_follower.tmux.subprocess.run", side_effect=_mock_tmux_run()),
        patch(
            "vim_ai_follower.commands.resolve_nvim_target",
            return_value=("/s.sock", True),
        ) as resolve,
    ):
        assert commands.cmd_start({"TMUX_PANE": "%1"}) == 0  # no --backend flag
    resolve.assert_called_once()
    result = state.FollowerState.read("@1")
    assert result is not None
    assert result.backend == "nvim"


def test_start_resolves_on_failure_and_speed_from_config(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config_path = tmp_path / "config.json"
    config_path.write_text('{"on_failure": "reopen", "speed": "lento"}')
    monkeypatch.setattr(config, "CONFIG_PATH", config_path)
    with patch("vim_ai_follower.tmux.subprocess.run", side_effect=_mock_tmux_run()):
        assert commands.cmd_start({"TMUX_PANE": "%1"}) == 0
        result = state.FollowerState.get("@1")
    assert result is not None
    assert result.on_failure == "reopen"
    assert result.speed == "lento"


def test_start_explicit_args_override_config(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config_path = tmp_path / "config.json"
    config_path.write_text('{"on_failure": "reopen", "speed": "lento"}')
    monkeypatch.setattr(config, "CONFIG_PATH", config_path)
    with patch("vim_ai_follower.tmux.subprocess.run", side_effect=_mock_tmux_run()):
        assert commands.cmd_start({"TMUX_PANE": "%1"}, on_failure="silent", speed="instant") == 0
        result = state.FollowerState.get("@1")
    assert result is not None
    assert result.on_failure == "silent"
    assert result.speed == "instant"


def test_status_reports_on_failure_and_speed(capsys: pytest.CaptureFixture[str]) -> None:
    with patch("vim_ai_follower.tmux.subprocess.run", side_effect=_mock_tmux_run()):
        commands.cmd_start({"TMUX_PANE": "%1"}, on_failure="reopen", speed="lento")
        commands.cmd_status({"TMUX_PANE": "%1"})
    out = capsys.readouterr().out
    assert "on_failure=reopen" in out
    assert "speed=lento" in out


def test_start_adopts_existing_vim_pane_when_configured(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config_path = tmp_path / "config.json"
    config_path.write_text('{"adopt_existing": true}')
    monkeypatch.setattr(config, "CONFIG_PATH", config_path)

    def _run(cmd: list[str], **kwargs: object) -> MagicMock:
        if cmd[:3] == ["tmux", "list-panes", "-a"]:
            return MagicMock(returncode=0, stdout="%1 zsh\n%7 vim\n")
        if cmd[:4] == ["tmux", "list-panes", "-t", "%1"]:
            return MagicMock(returncode=0, stdout="%1 zsh\n%7 vim\n")
        if cmd[:2] == ["tmux", "display-message"]:
            return MagicMock(returncode=0, stdout="@1\n")
        if cmd[:3] == ["tmux", "list-keys", "-T"]:
            return MagicMock(returncode=1, stdout="")
        return MagicMock(returncode=0, stdout="")

    with patch("vim_ai_follower.tmux.subprocess.run", side_effect=_run) as run:
        assert commands.cmd_start({"TMUX_PANE": "%1"}) == 0
        result = state.FollowerState.get("@1")

    assert not any(c.args[0][:2] == ["tmux", "split-window"] for c in run.call_args_list)
    assert result is not None
    assert result.target == "%7"
    assert result.adopted is True
    assert result.shown_any is True


def test_start_falls_back_to_a_split_when_adoption_finds_nothing_to_adopt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config_path = tmp_path / "config.json"
    config_path.write_text('{"adopt_existing": true}')
    monkeypatch.setattr(config, "CONFIG_PATH", config_path)

    # No vim pane sharing the origin's window — adopt_target must return
    # None, and cmd_start falls through to its normal dedicated split.
    with patch(
        "vim_ai_follower.tmux.subprocess.run",
        side_effect=_mock_tmux_run(other_panes=("%1",)),
    ) as run:
        assert commands.cmd_start({"TMUX_PANE": "%1"}) == 0
        result = state.FollowerState.get("@1")

    splits = [c.args[0] for c in run.call_args_list if c.args[0][:2] == ["tmux", "split-window"]]
    assert len(splits) == 1
    assert result is not None
    assert result.adopted is False


def test_start_nvim_standalone_launches_window(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config_path = tmp_path / "config.json"
    config_path.write_text('{"backend": "nvim", "nvim_window": "auto"}')
    monkeypatch.setattr(config, "CONFIG_PATH", config_path)
    standalone_session = session.Session(window_id="term-x", origin=None, in_tmux=False)
    with (
        patch("vim_ai_follower.commands.resolve_session", return_value=standalone_session),
        patch("vim_ai_follower.commands.launch_standalone_nvim", return_value="/s.sock") as launch,
    ):
        assert commands.cmd_start({}) == 0
    launch.assert_called_once_with("term-x")
    result = state.FollowerState.read("term-x")
    assert result is not None
    assert result.backend == "nvim"
    assert result.target == "/s.sock"
    assert result.adopted is False


def test_start_standalone_nvim_launcher_failure_reports_and_exits(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    # The launcher shells out (osascript / nvim-qt); a non-zero exit — e.g.
    # macOS Automation permission not yet granted — must surface as the
    # actionable message and rc 1, never a raw traceback, and persist nothing.
    config_path = tmp_path / "config.json"
    config_path.write_text('{"backend": "nvim", "nvim_window": "auto"}')
    monkeypatch.setattr(config, "CONFIG_PATH", config_path)
    standalone_session = session.Session(window_id="term-x", origin=None, in_tmux=False)
    with (
        patch("vim_ai_follower.commands.resolve_session", return_value=standalone_session),
        patch(
            "vim_ai_follower.commands.launch_standalone_nvim",
            side_effect=subprocess.CalledProcessError(1, ["osascript"]),
        ),
    ):
        assert commands.cmd_start({}) == 1
    assert "could not open a standalone nvim window" in capsys.readouterr().err
    assert state.FollowerState.read("term-x") is None


def test_start_in_tmux_nvim_window_always_launcher_failure_reports_and_exits(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    # Same guard on the nvim_window=always override taken while inside tmux.
    config_path = tmp_path / "config.json"
    config_path.write_text('{"backend": "nvim", "nvim_window": "always"}')
    monkeypatch.setattr(config, "CONFIG_PATH", config_path)
    in_tmux = session.Session(window_id="@1", origin="%1", in_tmux=True)
    with (
        patch("vim_ai_follower.commands.resolve_session", return_value=in_tmux),
        patch("vim_ai_follower.commands.keybindings.register"),
        patch(
            "vim_ai_follower.commands.launch_standalone_nvim",
            side_effect=subprocess.CalledProcessError(1, ["nvim-qt"]),
        ),
    ):
        assert commands.cmd_start({}) == 1
    assert "could not open a standalone nvim window" in capsys.readouterr().err


def test_start_vim_backend_without_tmux_errors(capsys: pytest.CaptureFixture[str]) -> None:
    standalone_session = session.Session(window_id="term-x", origin=None, in_tmux=False)
    with patch("vim_ai_follower.commands.resolve_session", return_value=standalone_session):
        rc = commands.cmd_start({}, backend="tmux")
    assert rc == 1
    assert "requires tmux" in capsys.readouterr().out


def test_start_nvim_window_never_without_tmux_errors(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    config_path = tmp_path / "config.json"
    config_path.write_text('{"nvim_window": "never"}')
    monkeypatch.setattr(config, "CONFIG_PATH", config_path)
    standalone_session = session.Session(window_id="term-x", origin=None, in_tmux=False)
    with patch("vim_ai_follower.commands.resolve_session", return_value=standalone_session):
        rc = commands.cmd_start({}, backend="nvim")
    assert rc == 1
    assert "nvim_window" in capsys.readouterr().out


def test_start_nvim_window_always_uses_standalone_launcher_in_tmux(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config_path = tmp_path / "config.json"
    config_path.write_text('{"nvim_window": "always"}')
    monkeypatch.setattr(config, "CONFIG_PATH", config_path)
    with (
        patch("vim_ai_follower.tmux.subprocess.run", side_effect=_mock_tmux_run()),
        patch("vim_ai_follower.commands.launch_standalone_nvim", return_value="/s.sock") as launch,
        patch("vim_ai_follower.commands.resolve_nvim_target") as split_target,
    ):
        assert commands.cmd_start({"TMUX_PANE": "%1"}, backend="nvim") == 0
    launch.assert_called_once_with("@1")
    split_target.assert_not_called()
    result = state.FollowerState.read("@1")
    assert result is not None
    assert result.backend == "nvim"
    assert result.target == "/s.sock"


def test_start_dead_tmux_pane_fails() -> None:
    with patch("vim_ai_follower.commands.resolve_session", return_value=None):
        assert commands.cmd_start({"TMUX_PANE": "%1"}) == 1


def _bind_calls(run_mock: MagicMock) -> list[list[str]]:
    return [c.args[0] for c in run_mock.call_args_list if c.args[0][:2] == ["tmux", "bind-key"]]


def _unbind_calls(run_mock: MagicMock) -> list[list[str]]:
    return [c.args[0] for c in run_mock.call_args_list if c.args[0][:2] == ["tmux", "unbind-key"]]


def test_start_registers_keybindings_with_absolute_path_and_silenced_output(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("CLAUDE_PLUGIN_ROOT", raising=False)
    with patch("vim_ai_follower.tmux.subprocess.run", side_effect=_mock_tmux_run()) as run:
        # Resolve INSIDE the patch, the way cmd_start does. tmux and
        # keybindings share one subprocess module object, so this patch also
        # intercepts the `git rev-parse` behind the worktree redirect
        # (_durable_wrapper): resolving outside it compares a redirected path
        # against an unredirected one, and fails whenever the suite happens to
        # run from a worktree. What this test pins is that the binding embeds
        # an absolute, resolved path rather than a bare name; the redirect
        # itself is covered in test_keybindings_durability.py.
        exe = keybindings._claude_follow_executable()  # the resolved absolute path
        assert Path(exe).is_absolute() and exe.endswith("claude-follow")
        assert commands.cmd_start({"TMUX_PANE": "%1"}) == 0
    binds = _bind_calls(run)
    assert [cmd[4] for cmd in binds] == ["P", "S", "+", "_", "F"]
    for cmd, subcommand in zip(
        binds, ("pause", "interrupt", "speed-up", "speed-down", "toggle"), strict=True
    ):
        # run-shell without -b blocks ALL tmux input until the command
        # exits — a resume replay lasts tens of seconds, freezing the user
        assert cmd[6] == "-b"
        shell_command = cmd[-1]
        # a bare "claude-follow" resolves to nothing under the tmux server's
        # PATH (exit 127) — the binding must embed the resolved ABSOLUTE path
        assert exe in shell_command
        # any stdout inside run-shell throws the pane into a view-mode overlay
        assert f" {subcommand} >/dev/null 2>&1" in shell_command
        # tmux pre-expands #{pane_id} in the run-shell string at keypress
        # time; nesting $(tmux display-message -p "%N") double-expands and
        # display-message EATS the % (formats again), yielding an invalid
        # pane target — TMUX_PANE must take the pre-expanded id directly
        assert shell_command.startswith("TMUX_PANE=#{pane_id} ")
        assert "display-message" not in shell_command


def test_existing_binding_skips_malformed_and_untabled_lines() -> None:
    # Real `list-keys -T prefix` output can include lines this parser must
    # tolerate without matching: an unbalanced-quote line (shlex.split raises
    # ValueError) and a line with no "-T" token at all — both must be
    # skipped, falling through to the line that actually matches.
    output = (
        'broken "quote\n'
        "unrelated command\n"
        'bind-key -T prefix P run-shell "/x/claude-follow pause"\n'
    )
    with patch(
        "vim_ai_follower.keybindings.subprocess.run",
        return_value=MagicMock(returncode=0, stdout=output),
    ):
        assert (
            keybindings._existing_binding("P")
            == 'bind-key -T prefix P run-shell "/x/claude-follow pause"'
        )


def test_claude_follow_executable_prefers_the_plugin_wrapper_when_running_as_a_plugin(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # As a Claude Code plugin, CLAUDE_PLUGIN_ROOT points at the install; the
    # tmux server (no plugin PATH, no venv) needs the bundled bin wrapper's
    # absolute path.
    monkeypatch.setenv("CLAUDE_PLUGIN_ROOT", str(tmp_path))
    assert keybindings._claude_follow_executable() == str(tmp_path / "bin" / "claude-follow")


def test_claude_follow_executable_resolves_the_bundled_wrapper_without_the_plugin_env(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # No CLAUDE_PLUGIN_ROOT (started from the shell, not a plugin hook): the
    # bundled bin/claude-follow, resolved from the package's own location, must
    # still give the tmux server an ABSOLUTE, resolvable path — never a bare
    # name it can't find. (Regression: a bare "claude-follow" binding was dead
    # in the tmux server.)
    monkeypatch.delenv("CLAUDE_PLUGIN_ROOT", raising=False)
    result = keybindings._claude_follow_executable()
    assert result.endswith("/bin/claude-follow")
    assert Path(result).is_absolute()
    assert Path(result).exists()  # the bundled wrapper really is in the repo


def test_claude_follow_executable_uses_the_venv_script_when_no_bundled_wrapper(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Non-editable install (no bundled bin/ next to the package): the pip
    # console script next to the interpreter is the absolute path to embed.
    monkeypatch.delenv("CLAUDE_PLUGIN_ROOT", raising=False)
    monkeypatch.setattr(
        keybindings, "_bundled_wrapper", lambda: tmp_path / "no-bin" / "claude-follow"
    )
    venv_bin = tmp_path / "venv-bin"
    venv_bin.mkdir()
    (venv_bin / "claude-follow").touch()
    monkeypatch.setattr(sys, "executable", str(venv_bin / "python"))
    assert keybindings._claude_follow_executable() == str(venv_bin / "claude-follow")


def test_claude_follow_executable_falls_back_to_which_then_bare_name(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("CLAUDE_PLUGIN_ROOT", raising=False)  # not running as a plugin
    # No bundled wrapper (e.g. an odd install layout) and no venv script:
    # then, and only then, fall back to PATH, then a bare name.
    monkeypatch.setattr(
        keybindings, "_bundled_wrapper", lambda: tmp_path / "no-bin" / "claude-follow"
    )
    monkeypatch.setattr(sys, "executable", str(tmp_path / "nowhere" / "python"))
    with patch("vim_ai_follower.keybindings.shutil.which", return_value="/opt/bin/claude-follow"):
        assert keybindings._claude_follow_executable() == "/opt/bin/claude-follow"
    with patch("vim_ai_follower.keybindings.shutil.which", return_value=None):
        assert keybindings._claude_follow_executable() == "claude-follow"


def test_stop_unregisters_keybindings_and_clears_signals() -> None:
    with patch("vim_ai_follower.tmux.subprocess.run", side_effect=_mock_tmux_run()) as run:
        commands.cmd_start({"TMUX_PANE": "%1"})
        control.request_pause("@1")
        control.save_pending_apply_edit("@1", [], 0.0)
        assert commands.cmd_stop({"TMUX_PANE": "%1"}) == 0
    unbinds = _unbind_calls(run)
    assert ["tmux", "unbind-key", "-T", "prefix", "P"] in unbinds
    assert ["tmux", "unbind-key", "-T", "prefix", "S"] in unbinds
    assert control.check_signal("@1") is None
    assert control.has_pending_animation("@1") is False


def test_stop_restores_a_pre_existing_binding() -> None:
    previous = "bind-key -T prefix P paste-buffer"

    def _run_with_existing_binding(cmd: list[str], **kwargs: object) -> MagicMock:
        # Full-table listing (no per-key filter arg): the caller extracts
        # P's line itself and finds nothing for S.
        if cmd[:4] == ["tmux", "list-keys", "-T", "prefix"]:
            return MagicMock(returncode=0, stdout=previous + "\n")
        return _mock_tmux_run()(cmd, **kwargs)

    with patch(
        "vim_ai_follower.tmux.subprocess.run", side_effect=_run_with_existing_binding
    ) as run:
        assert commands.cmd_start({"TMUX_PANE": "%1"}) == 0
        assert commands.cmd_stop({"TMUX_PANE": "%1"}) == 0

    rebinds = [c.args[0] for c in run.call_args_list if c.args[0][:2] == ["tmux", "bind-key"]]
    assert ["tmux", "bind-key", "-T", "prefix", "P", "paste-buffer"] in rebinds
    unbinds = _unbind_calls(run)
    assert ["tmux", "unbind-key", "-T", "prefix", "S"] in unbinds  # S had no previous binding
    assert not keybindings._saved_bindings_path().exists()


def test_stop_unbinds_instead_of_restoring_a_stale_claude_follow_binding() -> None:
    stale = 'bind-key -T prefix P run-shell "/old/venv/claude-follow pause >/dev/null 2>&1"'

    def _run_with_stale(cmd: list[str], **kwargs: object) -> MagicMock:
        if cmd[:4] == ["tmux", "list-keys", "-T", "prefix"]:
            return MagicMock(returncode=0, stdout=stale + "\n")
        return _mock_tmux_run()(cmd, **kwargs)

    with patch("vim_ai_follower.tmux.subprocess.run", side_effect=_run_with_stale) as run:
        assert commands.cmd_start({"TMUX_PANE": "%1"}) == 0
        assert commands.cmd_stop({"TMUX_PANE": "%1"}) == 0

    # "restoring" our own leftover binding from a crashed run would resurrect
    # a possibly-broken path — unbind is the correct cleanup
    assert ["tmux", "unbind-key", "-T", "prefix", "P"] in _unbind_calls(run)


def test_restart_after_crash_does_not_overwrite_the_saved_original_binding() -> None:
    with patch("vim_ai_follower.tmux.subprocess.run", side_effect=_mock_tmux_run()):
        assert commands.cmd_start({"TMUX_PANE": "%1"}) == 0
    saved_path = keybindings._saved_bindings_path()
    first = saved_path.read_text()

    # simulate a crash: follower state lost, tmux bindings (ours) still live
    state.FollowerState.clear("@1")

    def _run_with_our_binding(cmd: list[str], **kwargs: object) -> MagicMock:
        if cmd[:3] == ["tmux", "list-keys", "-T"]:
            return MagicMock(
                returncode=0,
                stdout='bind-key -T prefix P run-shell "/x/claude-follow pause"\n',
            )
        return _mock_tmux_run()(cmd, **kwargs)

    with patch("vim_ai_follower.tmux.subprocess.run", side_effect=_run_with_our_binding):
        assert commands.cmd_start({"TMUX_PANE": "%1"}) == 0

    # the re-registration must NOT record our own still-bound key as the
    # user's "previous" binding — the original record wins
    assert saved_path.read_text() == first


def test_stop_with_corrupt_saved_bindings_falls_back_to_unbind() -> None:
    with patch("vim_ai_follower.tmux.subprocess.run", side_effect=_mock_tmux_run()):
        assert commands.cmd_start({"TMUX_PANE": "%1"}) == 0
    keybindings._saved_bindings_path().write_text("{broken")
    with patch("vim_ai_follower.tmux.subprocess.run", side_effect=_mock_tmux_run()) as run:
        assert commands.cmd_stop({"TMUX_PANE": "%1"}) == 0
    unbinds = _unbind_calls(run)
    assert ["tmux", "unbind-key", "-T", "prefix", "P"] in unbinds
    assert ["tmux", "unbind-key", "-T", "prefix", "S"] in unbinds


def test_stop_on_adopted_pane_closes_tabs_but_not_the_pane() -> None:
    a = "/tmp/a.py"
    b = "/tmp/b.py"
    with patch(
        "vim_ai_follower.tmux.subprocess.run",
        return_value=MagicMock(returncode=0, stdout="%7 vim\n"),
    ):
        state.FollowerState.set(
            "@1", "tmux", "%7", origin="%1", adopted=True, open_files=(a, b), shown_any=True
        )

    with patch(
        "vim_ai_follower.tmux.subprocess.run",
        side_effect=make_mock_tmux_run(pane_id="%7", other_panes=("%1",)),
    ) as run:
        assert commands.cmd_stop({"TMUX_PANE": "%1"}) == 0

    sends = [
        c.args[0] for c in run.call_args_list if c.args[0][:4] == ["tmux", "send-keys", "-t", "%7"]
    ]
    literal = [c[6] for c in sends if "-l" in c]
    # close_tab resolves a buffer NUMBER and wipes that, because `:bwipeout!`
    # takes a buffer-name PATTERN, not a path: `app/[slug]/page.tsx` is a
    # character class and the wipe silently misses (measured 2026-09-22).
    assert any(f"fnamemodify('{a}', ':p')" in text for text in literal)
    assert any(f"fnamemodify('{b}', ':p')" in text for text in literal)
    # bwipeout alone closes each tab; a :tabclose here would eat an
    # innocent neighbor (see close_tab).
    assert not any("tabclose" in text for text in literal)
    assert not any(c.args[0][:2] == ["tmux", "kill-pane"] for c in run.call_args_list)
    unbinds = _unbind_calls(run)
    assert ["tmux", "unbind-key", "-T", "prefix", "P"] in unbinds
    assert state.FollowerState.get("@1") is None


def test_stop_on_adopted_nvim_closes_tabs_over_rpc_not_tmux_send_keys(
    capsys: pytest.CaptureFixture[str],
) -> None:
    # An adopted nvim's `target` is an RPC socket path, not a tmux pane id —
    # cmd_stop must route it through get_follower(backend, ...) rather than
    # hardcoding TmuxVimFollower (which would aim tmux send-keys at the
    # socket path and blow up before FollowerState.clear runs).
    sock = "/tmp/nvim-@1.sock"
    a = "/tmp/a.py"
    b = "/tmp/b.py"
    state.FollowerState.set(
        "@1", "nvim", sock, origin="%1", adopted=True, open_files=(a, b), shown_any=True
    )
    nvim_mock = MagicMock()
    nvim_mock.exec_lua.return_value = 7
    with (
        patch("vim_ai_follower.tmux.subprocess.run", side_effect=_mock_tmux_run()) as run,
        patch("pynvim.attach", return_value=nvim_mock) as attach,
    ):
        exit_code = commands.cmd_stop({"TMUX_PANE": "%1"})
    assert exit_code == 0
    assert "claude-follow: stopped" in capsys.readouterr().out
    # the nvim RPC path was actually used to close the tabs...
    attach.assert_called()
    nvim_mock.exec_lua.assert_any_call(_FIND_BUFFER_LUA, a)
    nvim_mock.exec_lua.assert_any_call(_FIND_BUFFER_LUA, b)
    # ...and never through a tmux send-keys aimed at the socket path (what
    # the old hardcoded TmuxVimFollower(pane_id=existing.target, ...) did —
    # it never calls pynvim.attach, and instead fires send-keys -t <sock>).
    assert not any(c.args[0][:4] == ["tmux", "send-keys", "-t", sock] for c in run.call_args_list)
    # state cleanup still ran to completion
    assert state.FollowerState.get("@1") is None


def test_stop_restores_the_follower_border() -> None:
    _register_fake_follower(
        "@1", "%2", writers=("$1", "a9"), writer_labels=("session:$1", "explore")
    )
    mock_run = make_mock_tmux_run(window_id="@1", pane_id="%2")
    with patch("vim_ai_follower.tmux.subprocess.run", side_effect=mock_run) as run:
        assert commands.cmd_stop({"TMUX_PANE": "%1"}) == 0
    cmds = [c.args[0] for c in run.call_args_list]
    # border color unset (both styles) + border-status restored on the pane
    assert ["tmux", "set-option", "-pu", "-t", "%2", "pane-border-style"] in cmds
    assert ["tmux", "set-option", "-wu", "-t", "%2", "pane-border-status"] in cmds


def test_stop_keeps_keybindings_while_another_live_follower_exists() -> None:
    _register_fake_follower("@1", "%2")
    _register_fake_follower("@2", "%3")
    mock_run = make_mock_tmux_run(window_id="@1", pane_id="%2", vim_panes=("%3",))
    with (
        patch("vim_ai_follower.tmux.subprocess.run", side_effect=mock_run),
        patch("vim_ai_follower.commands.keybindings.unregister") as mock_unregister,
    ):
        exit_code = commands.cmd_stop({"TMUX_PANE": "%1"})
    assert exit_code == 0
    assert state.FollowerState.read("@1") is None
    assert state.FollowerState.read("@2") is not None
    mock_unregister.assert_not_called()


def test_final_stop_unregisters_keybindings() -> None:
    _register_fake_follower("@1", "%2")
    mock_run = make_mock_tmux_run(window_id="@1", pane_id="%2")
    with (
        patch("vim_ai_follower.tmux.subprocess.run", side_effect=mock_run),
        patch("vim_ai_follower.commands.keybindings.unregister") as mock_unregister,
    ):
        exit_code = commands.cmd_stop({"TMUX_PANE": "%1"})
    assert exit_code == 0
    mock_unregister.assert_called_once()


def test_stop_collects_dead_and_legacy_state() -> None:
    _register_fake_follower("@1", "%2")
    _register_fake_follower("@2", "%9")  # dead: %9 not in list-panes output
    _register_fake_follower("$0", "%2")  # legacy session key: collected even though %2 is alive
    control.request_pause("@2")
    control.save_pending_apply_edit("@2", [], 0.0, file_path="/tmp/a.py")
    snapshot.save("@2", "/tmp/a.py", "before")
    mock_run = make_mock_tmux_run(window_id="@1", pane_id="%2")
    with (
        patch("vim_ai_follower.tmux.subprocess.run", side_effect=mock_run),
        patch("vim_ai_follower.commands.keybindings.unregister") as mock_unregister,
    ):
        exit_code = commands.cmd_stop({"TMUX_PANE": "%1"})
    assert exit_code == 0
    assert state.FollowerState.read("@2") is None
    assert state.FollowerState.read("$0") is None
    assert control.check_signal("@2") is None
    assert not control.has_pending_animation("@2")
    assert snapshot.load("@2", "/tmp/a.py") == ""
    mock_unregister.assert_called_once()


def test_speed_up_steps_state_and_reports(capsys: pytest.CaptureFixture[str]) -> None:
    _register_fake_follower("@1", "%2")
    state.FollowerState.update("@1", speed="rapido")
    with (
        patch("vim_ai_follower.tmux.subprocess.run", side_effect=make_mock_tmux_run()) as run,
        patch("vim_ai_follower.tmux.subprocess.Popen", return_value=MagicMock()) as popen,
    ):
        assert commands.cmd_speed({"TMUX_PANE": "%1"}, "up") == 0
    result = state.FollowerState.read("@1")
    assert result is not None
    assert result.speed == "muito_rapido"
    assert "speed muito_rapido" in capsys.readouterr().out
    assert popen.call_args_list == []  # status line, never a keyboard-grabbing popup
    status_calls = [
        c.args[0] for c in run.call_args_list if c.args[0][:2] == ["tmux", "display-message"]
    ]
    assert any("Speed: muito_rapido" in " ".join(c) for c in status_calls)


def test_speed_clamps_at_the_fast_end_and_says_so(capsys: pytest.CaptureFixture[str]) -> None:
    _register_fake_follower("@1", "%2")
    state.FollowerState.update("@1", speed="instant")
    with (
        patch("vim_ai_follower.tmux.subprocess.run", side_effect=make_mock_tmux_run()) as run,
        patch("vim_ai_follower.tmux.subprocess.Popen", return_value=MagicMock()) as popen,
    ):
        assert commands.cmd_speed({"TMUX_PANE": "%1"}, "up") == 0
    result = state.FollowerState.read("@1")
    assert result is not None
    assert result.speed == "instant"  # saturates, never wraps to lento
    assert "speed instant (fastest)" in capsys.readouterr().out
    # Feedback rides the tmux status line, never a popup: popups grab the
    # keyboard, which forced a wait between consecutive +/_ presses (live
    # finding, 2026-07-15).
    assert popen.call_args_list == []
    status_calls = [
        c.args[0] for c in run.call_args_list if c.args[0][:2] == ["tmux", "display-message"]
    ]
    assert any("Speed: instant (fastest)" in " ".join(c) for c in status_calls)


def test_speed_clamps_at_the_slow_end_and_says_so(capsys: pytest.CaptureFixture[str]) -> None:
    _register_fake_follower("@1", "%2")
    state.FollowerState.update("@1", speed="lento")
    with (
        patch("vim_ai_follower.tmux.subprocess.run", side_effect=make_mock_tmux_run()),
        patch("vim_ai_follower.tmux.subprocess.Popen", return_value=MagicMock()),
    ):
        assert commands.cmd_speed({"TMUX_PANE": "%1"}, "down") == 0
    result = state.FollowerState.read("@1")
    assert result is not None
    assert result.speed == "lento"
    assert "speed lento (slowest)" in capsys.readouterr().out


def test_speed_dead_tmux_pane_fails() -> None:
    with patch("vim_ai_follower.commands.resolve_session", return_value=None):
        assert commands.cmd_speed({"TMUX_PANE": "%1"}, "up") == 1


def test_speed_without_follower_is_honest_noop(capsys: pytest.CaptureFixture[str]) -> None:
    with (
        patch("vim_ai_follower.tmux.subprocess.run", side_effect=_mock_tmux_run()),
        patch("vim_ai_follower.tmux.subprocess.Popen", return_value=MagicMock()) as popen,
    ):
        assert commands.cmd_speed({"TMUX_PANE": "%1"}, "down") == 0
    out = capsys.readouterr().out
    assert "no follower active" in out
    assert popen.call_args_list == []


def _mock_tmux_run_with_zoom(zoomed: str, **kwargs: object) -> Callable[..., MagicMock]:
    """Like make_mock_tmux_run, but display-message queries for
    '#{window_zoomed_flag}' answer with the given flag ("0" or "1") instead
    of the window id every other display-message query returns."""
    base = make_mock_tmux_run(**kwargs)  # type: ignore[arg-type]

    def _run(cmd: list[str], **run_kwargs: object) -> MagicMock:
        if cmd[:2] == ["tmux", "display-message"] and cmd[-1] == "#{window_zoomed_flag}":
            return MagicMock(returncode=0, stdout=f"{zoomed}\n")
        return base(cmd, **run_kwargs)

    return _run


def _resize_pane_calls(run_mock: MagicMock) -> list[list[str]]:
    return [
        c.args[0] for c in run_mock.call_args_list if c.args[0][:3] == ["tmux", "resize-pane", "-Z"]
    ]


def test_toggle_dead_tmux_pane_fails() -> None:
    with patch("vim_ai_follower.commands.resolve_session", return_value=None):
        assert commands.cmd_toggle({"TMUX_PANE": "%1"}) == 1


def test_toggle_without_a_registered_follower_is_a_noop(
    capsys: pytest.CaptureFixture[str],
) -> None:
    with patch("vim_ai_follower.tmux.subprocess.run", side_effect=_mock_tmux_run()):
        assert commands.cmd_toggle({"TMUX_PANE": "%1"}) == 0
    assert "no follower to toggle" in capsys.readouterr().out


def test_toggle_off_without_origin_skips_zoom() -> None:
    # register_fake_follower never sets origin (defaults to "") — muting a
    # follower that was never given an origin pane must not try to zoom one.
    _register_fake_follower("@1", "%2")
    with patch(
        "vim_ai_follower.tmux.subprocess.run",
        side_effect=_mock_tmux_run_with_zoom("0", pane_id="%2"),
    ) as run:
        assert commands.cmd_toggle({"TMUX_PANE": "%1"}) == 0
    result = state.FollowerState.read("@1")
    assert result is not None
    assert result.enabled is False
    assert _resize_pane_calls(run) == []


def test_toggle_on_without_origin_skips_unzoom_and_reopen() -> None:
    _register_fake_follower("@1", "%2", open_files=("/a.py",), shown_any=True)
    state.FollowerState.update("@1", enabled=False)
    with patch(
        "vim_ai_follower.tmux.subprocess.run",
        side_effect=_mock_tmux_run_with_zoom("1", pane_id="%2"),
    ) as run:
        assert commands.cmd_toggle({"TMUX_PANE": "%1"}) == 0
    result = state.FollowerState.read("@1")
    assert result is not None
    assert result.enabled is True
    assert _resize_pane_calls(run) == []


def test_toggle_off_mutes_and_zooms_origin() -> None:
    _register_fake_follower("@1", "%2")
    state.FollowerState.update("@1", origin="%1")
    with patch(
        "vim_ai_follower.tmux.subprocess.run",
        side_effect=_mock_tmux_run_with_zoom("0", pane_id="%2"),
    ) as run:
        assert commands.cmd_toggle({"TMUX_PANE": "%1"}) == 0
    result = state.FollowerState.read("@1")
    assert result is not None
    assert result.enabled is False
    assert _resize_pane_calls(run) == [["tmux", "resize-pane", "-Z", "-t", "%1"]]


def test_toggle_on_unzooms_clears_open_files_and_keeps_tabs() -> None:
    _register_fake_follower("@1", "%2", open_files=("/a.py",), shown_any=True)
    state.FollowerState.update("@1", origin="%1", enabled=False)
    with patch(
        "vim_ai_follower.tmux.subprocess.run",
        side_effect=_mock_tmux_run_with_zoom("1", pane_id="%2"),
    ) as run:
        assert commands.cmd_toggle({"TMUX_PANE": "%1"}) == 0
    result = state.FollowerState.read("@1")
    assert result is not None
    assert result.enabled is True
    assert result.open_files == ()
    assert result.shown_any is True
    assert _resize_pane_calls(run) == [["tmux", "resize-pane", "-Z", "-t", "%1"]]
    tabcloses = [
        c.args[0] for c in run.call_args_list if c.args[0][:4] == ["tmux", "send-keys", "-t", "%2"]
    ]
    assert tabcloses == []  # the pane stayed alive — no split, no tab teardown


def test_toggle_on_reopens_dead_pane_as_dedicated_split() -> None:
    state.FollowerState.set(
        "@1",
        "tmux",
        "%2",
        origin="%1",
        adopted=True,
        enabled=False,
        open_files=("/a.py",),
        shown_any=True,
    )

    def _run(cmd: list[str], **kwargs: object) -> MagicMock:
        if cmd[:3] == ["tmux", "list-panes", "-a"]:
            # the old pane %2 is dead — no vim running there anymore
            return MagicMock(returncode=0, stdout="")
        if cmd[:2] == ["tmux", "display-message"] and cmd[-1] == "#{window_zoomed_flag}":
            return MagicMock(returncode=0, stdout="0\n")
        if cmd[:2] == ["tmux", "display-message"]:
            return MagicMock(returncode=0, stdout="@1\n")
        if cmd[:2] == ["tmux", "split-window"]:
            return MagicMock(returncode=0, stdout="%9\n")
        return MagicMock(returncode=0, stdout="")

    with patch("vim_ai_follower.tmux.subprocess.run", side_effect=_run) as run:
        assert commands.cmd_toggle({"TMUX_PANE": "%1"}) == 0

    splits = [c.args[0] for c in run.call_args_list if c.args[0][:2] == ["tmux", "split-window"]]
    assert splits == [["tmux", "split-window", "-h", "-t", "%1", "-P", "-F", "#{pane_id}", "vim"]]
    result = state.FollowerState.read("@1")
    assert result is not None
    assert result.target == "%9"
    assert result.adopted is False
    assert result.shown_any is False
    assert result.open_files == ()
