from __future__ import annotations

import contextlib
import functools
import subprocess
import sys
from collections.abc import Callable, Iterator
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from helpers import (
    MOCK_SERVER_PID,
    evict_spelled,
    make_mock_tmux_run,
    resolve_typed_paths,
    wipe_spelled,
)
from helpers import register_fake_follower as _register_fake_follower

from vim_ai_follower import (
    cache,
    commands,
    config,
    control,
    hooks,
    keybindings,
    session,
    snapshot,
    state,
)
from vim_ai_follower.backends import nvim_connect, tmux_vim
from vim_ai_follower.backends.nvim import _FIND_BUFFER_LUA
from vim_ai_follower.backends.nvim_connect import NvimNeverListened

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


@pytest.mark.parametrize("command", ["start", "stop"])
def test_start_and_stop_sweep_old_answer_locks_and_survive_a_failing_sweep(command: str) -> None:
    run = {"start": commands.cmd_start, "stop": commands.cmd_stop}[command]
    with (
        patch("vim_ai_follower.commands.resolve_session", return_value=None),
        patch("vim_ai_follower.commands.nvim_prompt.sweep_answer_locks") as sweep,
    ):
        assert run({"TMUX_PANE": "%1"}) == 1
    sweep.assert_called_once_with()
    with (
        patch("vim_ai_follower.commands.resolve_session", return_value=None),
        patch(
            "vim_ai_follower.commands.nvim_prompt.sweep_answer_locks",
            side_effect=PermissionError("denied"),
        ),
    ):
        assert run({"TMUX_PANE": "%1"}) == 1


@pytest.mark.parametrize("command", ["start", "stop", "status"])
def test_start_stop_and_status_sweep_stale_sockets_and_survive_a_failing_sweep(
    command: str,
) -> None:
    run = {"start": commands.cmd_start, "stop": commands.cmd_stop, "status": commands.cmd_status}[
        command
    ]
    expected = 0 if command == "status" else 1
    with (
        patch("vim_ai_follower.commands.resolve_session", return_value=None),
        patch("vim_ai_follower.commands.nvim_connect.sweep_stale_sockets") as sweep,
    ):
        assert run({"TMUX_PANE": "%1"}) == expected
    sweep.assert_called_once_with()
    with (
        patch("vim_ai_follower.commands.resolve_session", return_value=None),
        patch(
            "vim_ai_follower.commands.nvim_connect.sweep_stale_sockets",
            side_effect=PermissionError("denied"),
        ),
    ):
        assert run({"TMUX_PANE": "%1"}) == expected


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
        patch("vim_ai_follower.backends.nvim.probe_nvim", return_value="answers"),
        patch("vim_ai_follower.commands.nvim_connect.probe_nvim", return_value="answers"),
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
        patch("vim_ai_follower.backends.nvim.probe_nvim", return_value="answers"),
        patch("vim_ai_follower.commands.nvim_connect.probe_nvim", return_value="answers"),
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
    resolve.assert_called_once_with(
        "%1", "@1", adopt=False, wait_seconds=nvim_connect.START_SPLIT_WAIT_SECONDS
    )
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
    resolve.assert_called_once_with(
        "%1", "@1", adopt=True, wait_seconds=nvim_connect.START_SPLIT_WAIT_SECONDS
    )
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
    failure = subprocess.CalledProcessError(1, ["osascript"])
    with (
        patch("vim_ai_follower.commands.resolve_session", return_value=standalone_session),
        patch("vim_ai_follower.commands.launch_standalone_nvim", side_effect=failure),
    ):
        assert commands.cmd_start({}) == 1
    out, err = capsys.readouterr()
    assert (out, err) == (
        "",
        f"claude-follow: could not open a standalone nvim window ({failure})\n",
    )
    assert state.FollowerState.read("term-x") is None


def test_start_standalone_nvim_that_never_listens_reports_and_persists_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    # The launcher exited 0 but no nvim ever listened: `start` used to print
    # "attached", exit 0 and persist a follower that `status` then denied.
    config_path = tmp_path / "config.json"
    config_path.write_text('{"backend": "nvim", "nvim_window": "auto"}')
    monkeypatch.setattr(config, "CONFIG_PATH", config_path)
    standalone_session = session.Session(window_id="term-x", origin=None, in_tmux=False)
    never = NvimNeverListened("/c/nvim-term-x.sock", "nvim-qt", 10.0)
    with (
        patch("vim_ai_follower.commands.resolve_session", return_value=standalone_session),
        patch("vim_ai_follower.commands.launch_standalone_nvim", side_effect=never),
    ):
        assert commands.cmd_start({}) == 1
    out, err = capsys.readouterr()
    assert (out, err) == (
        "",
        "claude-follow: could not open a standalone nvim window (nvim-qt exited but no "
        "nvim listened on /c/nvim-term-x.sock within 10 s) — check that nvim-qt can "
        "start nvim, then run start again\n",
    )
    assert state.FollowerState.read("term-x") is None


def test_start_retries_at_once_and_waits_the_full_deadline_despite_an_auto_open_backoff(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A failed auto-open keeps hooks from relaunching for a while; the user
    # typing `start` is the explicit retry, with the patient deadline.
    config_path = tmp_path / "config.json"
    config_path.write_text('{"backend": "nvim", "nvim_window": "auto"}')
    monkeypatch.setattr(config, "CONFIG_PATH", config_path)
    nvim_connect.record_launch_failure("term-x")
    assert nvim_connect.launch_backoff_active("term-x")
    standalone_session = session.Session(window_id="term-x", origin=None, in_tmux=False)
    with (
        patch("vim_ai_follower.commands.resolve_session", return_value=standalone_session),
        patch("vim_ai_follower.commands.launch_standalone_nvim", return_value="/s.sock") as launch,
    ):
        assert commands.cmd_start({}) == 0
    launch.assert_called_once_with("term-x")  # the default, patient deadline
    assert not nvim_connect.launch_backoff_active("term-x")


def test_start_reports_a_socket_path_it_cannot_clear(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    config_path = tmp_path / "config.json"
    config_path.write_text('{"backend": "nvim", "nvim_window": "auto"}')
    monkeypatch.setattr(config, "CONFIG_PATH", config_path)
    standalone_session = session.Session(window_id="term-x", origin=None, in_tmux=False)
    blocked = nvim_connect.SocketPathBlocked("/c/nvim-term-x.sock", PermissionError(1, "denied"))
    with (
        patch("vim_ai_follower.commands.resolve_session", return_value=standalone_session),
        patch("vim_ai_follower.commands.launch_standalone_nvim", side_effect=blocked),
    ):
        assert commands.cmd_start({}) == 1
    out, err = capsys.readouterr()
    assert (out, err) == (
        "",
        f"claude-follow: could not open a standalone nvim window ({blocked})\n",
    )
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
    # stdout may carry the keybinding note printed before the launch; the
    # error itself goes to stderr, and only there.
    out, err = capsys.readouterr()
    failure = subprocess.CalledProcessError(1, ["nvim-qt"])
    assert err == f"claude-follow: could not open a standalone nvim window ({failure})\n"
    assert "could not open" not in out


def test_start_vim_backend_without_tmux_errors(capsys: pytest.CaptureFixture[str]) -> None:
    standalone_session = session.Session(window_id="term-x", origin=None, in_tmux=False)
    with patch("vim_ai_follower.commands.resolve_session", return_value=standalone_session):
        rc = commands.cmd_start({}, backend="tmux")
    assert rc == 1
    # An error, so stderr — like its sibling "could not open a standalone
    # nvim window" — and nothing on stdout.
    out, err = capsys.readouterr()
    assert (out, err) == ("", commands.VIM_NEEDS_TMUX + "\n")


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
    # An error, so stderr — like VIM_NEEDS_TMUX (battery check 10) — and
    # nothing on stdout.
    out, err = capsys.readouterr()
    assert (out, err) == (
        "",
        "claude-follow: nvim_window is 'never' but there is no tmux session — "
        "set nvim_window to auto/always, or start inside tmux\n",
    )


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
        control.mark_animating("@1")  # a signal is only written for a live animation
        control.request_pause("@1")
        assert (cache.CACHE_DIR / "@1.pause").exists()  # else the check below is vacuous
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


def test_stop_on_adopted_pane_closes_tabs_but_not_the_pane(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The mocked Vim never answers s:evict's verdict: don't wait for it.
    monkeypatch.setattr(tmux_vim, "_PROBE_TIMEOUT_SECONDS", 0.01)
    a = "/tmp/a.py"
    b = "/tmp/b.py"
    with patch(
        "vim_ai_follower.tmux.subprocess.run",
        return_value=MagicMock(returncode=0, stdout="%7 vim\n"),
    ):
        state.FollowerState.set(
            "@1",
            "tmux",
            "%7",
            origin="%1",
            adopted=True,
            open_files=(a, b),
            shown_any=True,
            server_pid=MOCK_SERVER_PID,
        )

    with patch(
        "vim_ai_follower.tmux.subprocess.run",
        side_effect=make_mock_tmux_run(pane_id="%7", other_panes=("%1",)),
    ) as run:
        assert commands.cmd_stop({"TMUX_PANE": "%1"}) == 0

    sends = [
        c.args[0] for c in run.call_args_list if c.args[0][:4] == ["tmux", "send-keys", "-t", "%7"]
    ]
    literal = [resolve_typed_paths(c[6]) for c in sends if "-l" in c]
    # close_tab resolves a buffer NUMBER, because `:bwipeout!` takes a
    # buffer-name PATTERN, not a path: `app/[slug]/page.tsx` is a character
    # class and the wipe silently misses (measured 2026-09-22). In the user's
    # own Vim it goes through s:evict, which never wipes the user's work.
    assert evict_spelled(a) in literal
    assert evict_spelled(b) in literal
    assert wipe_spelled(a) not in literal
    assert wipe_spelled(b) not in literal
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
        patch("vim_ai_follower.backends.nvim.probe_nvim", return_value="answers"),
        patch("vim_ai_follower.commands.nvim_connect.probe_nvim", return_value="answers"),
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
    control.mark_animating("@2")  # a signal is only written for a live animation
    control.request_pause("@2")
    assert (cache.CACHE_DIR / "@2.pause").exists()  # else the check below is vacuous
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


@pytest.mark.parametrize("verdict", ["silent", "answers"])
def test_status_marks_an_nvim_follower_that_does_not_answer(
    capsys: pytest.CaptureFixture[str], verdict: str
) -> None:
    # Silent (busy in `:!make`, or stopped) is listed — alive — and marked,
    # both as this identity's follower and as another identity's.
    state.FollowerState.set("term-x", "nvim", "/tmp/mine.sock", current_file="/tmp/a.py")
    state.FollowerState.set("@7", "nvim", "/tmp/theirs.sock", current_file="/tmp/b.py")
    with patch("vim_ai_follower.backends.nvim_connect._probe", return_value=(verdict, False, None)):
        assert commands.cmd_status({"TERM_SESSION_ID": "x"}) == 0
        mine = capsys.readouterr().out
        assert commands.cmd_status({"TERM_SESSION_ID": "y"}) == 0
        others = capsys.readouterr().out
    marked = verdict == "silent"
    assert "backend nvim (/tmp/mine.sock)" in mine
    assert ("not answering (busy in a command, or stopped)" in mine) is marked
    assert "@7: nvim /tmp/theirs.sock, showing /tmp/b.py" in others
    assert ("/tmp/b.py — not answering" in others) is marked


# --- stop: state first, bounded teardown, and it sticks (sweep-4 T17) -------

_DEDICATED_SOCKET = "/tmp/nvim-@1.sock"


def _hook_log() -> str:
    from vim_ai_follower import hooks

    return hooks.LOG_PATH.read_text() if hooks.LOG_PATH.exists() else ""


def _nvim_follower(adopted: bool, open_files: tuple[str, ...] = ()) -> None:
    state.FollowerState.set(
        "@1",
        "nvim",
        _DEDICATED_SOCKET,
        origin="%1",
        adopted=adopted,
        open_files=open_files,
        shown_any=True,
        server_pid=MOCK_SERVER_PID,
    )


def _tmux_with_panes(listing: str, children: str = "") -> Callable[..., MagicMock]:
    """A tmux whose `list-panes -t` answers `listing` (id, pid and start
    command, tab-separated) and whose `pgrep -P` answers `children`."""
    base = make_mock_tmux_run()

    def _run(cmd: list[str], **kwargs: object) -> MagicMock:
        if cmd[:3] == ["tmux", "list-panes", "-t"]:
            return MagicMock(returncode=0, stdout=listing)
        if cmd[:2] == ["pgrep", "-P"]:
            return MagicMock(returncode=0 if children else 1, stdout=children)
        if cmd[:2] == ["ps", "-o"]:
            return MagicMock(returncode=0, stdout=f"nvim --embed --listen {_DEDICATED_SOCKET}\n")
        return base(cmd, **kwargs)

    return _run


def test_stop_clears_the_state_before_a_teardown_that_fails() -> None:
    _nvim_follower(adopted=False)
    control.mark_animating("@1")
    control.request_pause("@1")
    assert (cache.CACHE_DIR / "@1.pause").exists()
    control.save_pending_apply_edit("@1", [], 0.0)
    seen_at_teardown: list[bool] = []

    def frozen_stop(*_args: object, **_kwargs: object) -> None:
        seen_at_teardown.append((cache.CACHE_DIR / "@1.pane").exists())
        raise nvim_connect.RpcTimeout("nvim did not answer within 10 s")

    with (
        patch("vim_ai_follower.tmux.subprocess.run", side_effect=_mock_tmux_run()),
        patch("vim_ai_follower.binding.tmux_server_pid", return_value=4242),
        patch("pynvim.attach", return_value=MagicMock()),
        patch("vim_ai_follower.commands.nvim_connect.probe_nvim", return_value="answers"),
        patch("vim_ai_follower.backends.nvim.NvimFollower.stop", side_effect=frozen_stop),
    ):
        assert commands.cmd_stop({"TMUX_PANE": "%1"}) == 0
    assert seen_at_teardown == [False]
    assert not (cache.CACHE_DIR / "@1.pane").exists()
    assert control.check_signal("@1") is None
    assert control.has_pending_animation("@1") is False
    assert state.is_stopped("@1", 4242)
    assert "nvim did not answer within 10 s" in _hook_log()


def test_a_hook_finishing_after_stop_does_not_resurrect_the_follower() -> None:
    _register_fake_follower("@1", "%2", open_files=("/tmp/a.py",))
    held = state.FollowerState.read("@1")
    assert held is not None
    with (
        patch("vim_ai_follower.tmux.subprocess.run", side_effect=_mock_tmux_run()),
        patch("vim_ai_follower.binding.tmux_server_pid", return_value=4242),
    ):
        assert commands.cmd_stop({"TMUX_PANE": "%1"}) == 0
        # The hook's own reads still see the state it read before the stop.
        with patch.object(state.FollowerState, "read", return_value=held):
            state.FollowerState.update("@1", current_file="/tmp/a.py")
            state.FollowerState.mark_stale("@1", "/tmp/a.py")
    assert not (cache.CACHE_DIR / "@1.pane").exists()


def test_stop_clears_a_state_a_racing_hook_wrote_during_the_teardown() -> None:
    _nvim_follower(adopted=False)

    def racing_write(*_args: object, **_kwargs: object) -> None:
        # A set that passed its tombstone check before the stop wrote it.
        (cache.CACHE_DIR / "@1.pane").write_text((cache.CACHE_DIR / "kept.json").read_text())

    (cache.CACHE_DIR / "kept.json").write_text((cache.CACHE_DIR / "@1.pane").read_text())
    with (
        patch("vim_ai_follower.tmux.subprocess.run", side_effect=_mock_tmux_run()),
        patch("vim_ai_follower.binding.tmux_server_pid", return_value=4242),
        patch("vim_ai_follower.commands.nvim_connect.probe_nvim", return_value="answers"),
        patch("vim_ai_follower.status_surface.NvimStatusSurface.clear"),
        patch("vim_ai_follower.backends.nvim.NvimFollower.stop", side_effect=racing_write),
        patch("vim_ai_follower.commands.keybindings.unregister") as unregister,
    ):
        assert commands.cmd_stop({"TMUX_PANE": "%1"}) == 0
    assert not (cache.CACHE_DIR / "@1.pane").exists()
    unregister.assert_called_once_with()


def test_start_clears_the_tombstone() -> None:
    state.mark_stopped("@1", 4242, in_tmux=True)
    with (
        patch("vim_ai_follower.tmux.subprocess.run", side_effect=_mock_tmux_run()),
        patch("vim_ai_follower.binding.tmux_server_pid", return_value=4242),
    ):
        assert commands.cmd_start({"TMUX_PANE": "%1"}) == 0
    assert not state.stopped_path("@1").exists()
    assert state.FollowerState.read("@1") is not None


def test_toggle_after_stop_turns_auto_open_back_on(capsys: pytest.CaptureFixture[str]) -> None:
    state.mark_stopped("@1", 4242, in_tmux=True)
    with patch("vim_ai_follower.tmux.subprocess.run", side_effect=_mock_tmux_run()):
        assert commands.cmd_toggle({"TMUX_PANE": "%1"}) == 0
    assert not state.stopped_path("@1").exists()
    assert "auto-open is back on for this window" in capsys.readouterr().out


def test_toggle_on_clears_a_tombstone_before_writing() -> None:
    _register_fake_follower("@1", "%2")
    state.FollowerState.update("@1", enabled=False)
    state.mark_stopped("@1", 4242, in_tmux=True)
    with (
        patch(
            "vim_ai_follower.tmux.subprocess.run",
            side_effect=_mock_tmux_run_with_zoom("1", pane_id="%2"),
        ),
        patch("vim_ai_follower.binding.tmux_server_pid", return_value=4242),
    ):
        assert commands.cmd_toggle({"TMUX_PANE": "%1"}) == 0
    result = state.FollowerState.read("@1")
    assert result is not None
    assert result.enabled is True


@pytest.mark.parametrize(
    ("blocking", "mode", "named"),
    [
        (True, "n", "in the middle of a key sequence"),
        (False, "r?", "a confirm prompt"),
        (False, "c", "on the command line"),
        (False, "cv", "on the command line"),
        (False, "no", "an operator pending"),
        (False, "nov", "an operator pending"),
        (False, "noV", "an operator pending"),
        (False, "no\x16", "an operator pending"),
        (False, "i", "in insert mode"),
        (False, "niI", "in insert mode"),
        (False, "R", "in replace mode"),
        (False, "t", "in terminal mode"),
        (False, "niR", "in replace mode"),
        (False, "niV", "in replace mode"),
        (False, "v", "in visual mode"),
        (False, "V", "in visual mode"),
        (False, "\x16", "in visual mode"),
        (False, "s", "in select mode"),
        (False, "vs", "in select mode"),
        (False, "\x13", "in select mode"),
    ],
    ids=[
        "keys-pending",
        "confirm",
        "cmdline",
        "cmdline-ex",
        "operator",
        "operator-v",
        "operator-V",
        "operator-ctrl-v",
        "insert",
        "insert-ctrl-o",
        "replace",
        "terminal",
        "replace-ctrl-o",
        "virtual-replace-ctrl-o",
        "visual",
        "visual-line",
        "visual-block",
        "select",
        "visual-ctrl-o-from-select",
        "select-block",
    ],
)
def test_stop_of_an_adopted_nvim_waiting_on_its_user_sends_nothing_and_logs_one_line(
    blocking: bool, mode: str, named: str
) -> None:
    # Keys pending (blocking, not at a hit-enter or more-prompt), a confirm or
    # the command line (not blocking, but the user's answer lands on whatever
    # buffer is current then, task 22 review I1): nothing the follower may
    # answer or act under.
    _nvim_follower(adopted=True, open_files=("/tmp/a.py", "/tmp/b.py"))
    with (
        patch("vim_ai_follower.tmux.subprocess.run", side_effect=_mock_tmux_run()) as run,
        patch("pynvim.attach") as attach,
        patch("vim_ai_follower.commands.nvim_connect.probe_nvim", return_value="answers"),
        patch("vim_ai_follower.commands.nvim_connect.last_probe_blocking", return_value=blocking),
        patch("vim_ai_follower.commands.nvim_connect.last_probe_mode", return_value=mode),
        patch("vim_ai_follower.take_over.ask") as asked,
    ):
        assert commands.cmd_stop({"TMUX_PANE": "%1"}) == 0
    # A stop never asks to take over (Alberto, 2026-10-17): it sends nothing.
    asked.assert_not_called()
    attach.assert_not_called()
    assert not any(c.args[0][:2] == ["tmux", "kill-pane"] for c in run.call_args_list)
    lines = [line for line in _hook_log().splitlines() if _DEDICATED_SOCKET in line]
    assert len(lines) == 1, _hook_log()
    assert f"waiting on its user ({named})" in lines[0]
    assert state.FollowerState.read("@1") is None


def test_stop_of_an_adopted_nvim_in_its_command_line_window_sends_nothing() -> None:
    # `q:` reads "n", not blocking; closing the follower's tabs there raises
    # E11 (T22 re-review m2).
    _nvim_follower(adopted=True, open_files=("/tmp/a.py",))
    with (
        patch("vim_ai_follower.tmux.subprocess.run", side_effect=_mock_tmux_run()) as run,
        patch("pynvim.attach") as attach,
        patch("vim_ai_follower.commands.nvim_connect.probe_nvim", return_value="answers"),
        patch("vim_ai_follower.commands.nvim_connect.last_probe_verdict", return_value="answers"),
        patch("vim_ai_follower.commands.nvim_connect.last_probe_blocking", return_value=False),
        patch("vim_ai_follower.commands.nvim_connect.last_probe_mode", return_value="n"),
        patch(
            "vim_ai_follower.commands.nvim_connect.in_command_line_window", return_value=True
        ) as cmdwin,
    ):
        assert commands.cmd_stop({"TMUX_PANE": "%1"}) == 0
    cmdwin.assert_called_once_with(_DEDICATED_SOCKET)
    attach.assert_not_called()
    assert not any(c.args[0][:2] == ["tmux", "kill-pane"] for c in run.call_args_list)
    lines = [line for line in _hook_log().splitlines() if _DEDICATED_SOCKET in line]
    assert len(lines) == 1, _hook_log()
    assert "waiting on its user (in the command-line window)" in lines[0]
    assert state.FollowerState.read("@1") is None


def test_stop_of_an_adopted_nvim_at_a_hit_enter_prompt_sweeps_then_tears_down() -> None:
    # Decision (a): the stop acts in the user's editor (its float, the tabs
    # the follower opened), so a hit-enter prompt left up is dismissed first,
    # as the Vim follower's Escape pair would.
    _nvim_follower(adopted=True, open_files=("/tmp/a.py",))
    sweeper = MagicMock()
    prompt = {"mode": "r", "blocking": True}
    modes = iter([prompt, prompt])
    sweeper.api.get_mode.side_effect = lambda: next(modes, {"mode": "n", "blocking": False})
    sweeper.api.exec2.return_value = {"output": ""}
    teardown = MagicMock(return_value=False)
    with (
        patch("vim_ai_follower.tmux.subprocess.run", side_effect=_mock_tmux_run()),
        patch("vim_ai_follower.commands.nvim_connect.probe_nvim", return_value="answers"),
        patch("vim_ai_follower.commands.nvim_connect.last_probe_blocking", return_value=True),
        patch("vim_ai_follower.commands.nvim_connect.last_probe_mode", return_value="r"),
        patch("vim_ai_follower.commands.nvim_connect.bounded_attach", return_value=sweeper),
        patch("vim_ai_follower.commands._run_nvim_teardown", teardown),
    ):
        assert commands.cmd_stop({"TMUX_PANE": "%1"}) == 0
    sweeper.api.input.assert_called_once_with("<CR>")
    teardown.assert_called_once()
    assert "waiting on its user" not in _hook_log()
    assert "dismissed a hit-enter prompt left before stop (sweep)" in _hook_log()
    assert state.FollowerState.read("@1") is None


def test_stop_of_an_adopted_nvim_whose_prompt_cannot_be_reached_sends_nothing_more() -> None:
    _nvim_follower(adopted=True, open_files=("/tmp/a.py",))
    teardown = MagicMock(return_value=False)
    with (
        patch("vim_ai_follower.tmux.subprocess.run", side_effect=_mock_tmux_run()),
        patch("vim_ai_follower.commands.nvim_connect.probe_nvim", return_value="answers"),
        patch("vim_ai_follower.commands.nvim_connect.last_probe_blocking", return_value=True),
        patch("vim_ai_follower.commands.nvim_connect.last_probe_mode", return_value="rm"),
        patch(
            "vim_ai_follower.commands.nvim_connect.bounded_attach",
            side_effect=ConnectionRefusedError("refused"),
        ),
        patch("vim_ai_follower.commands._run_nvim_teardown", teardown),
    ):
        assert commands.cmd_stop({"TMUX_PANE": "%1"}) == 0
    teardown.assert_not_called()
    lines = [line for line in _hook_log().splitlines() if _DEDICATED_SOCKET in line]
    assert len(lines) == 1, _hook_log()
    assert "could not be answered" in lines[0] and "refused" in lines[0]


@pytest.mark.parametrize("verdict", ["silent", "garbled", "absent"])
def test_stop_of_an_unreachable_adopted_nvim_never_touches_its_pane(verdict: str) -> None:
    _nvim_follower(adopted=True, open_files=("/tmp/a.py",))
    listing = f"%1\t100\tzsh\n%5\t200\tnvim --listen {_DEDICATED_SOCKET}\n"
    with (
        patch("vim_ai_follower.tmux.subprocess.run", side_effect=_tmux_with_panes(listing)) as run,
        patch("pynvim.attach") as attach,
        patch("vim_ai_follower.commands.nvim_connect.probe_nvim", return_value=verdict),
        patch("vim_ai_follower.commands._send_signal") as send_signal,
    ):
        assert commands.cmd_stop({"TMUX_PANE": "%1"}) == 0
    attach.assert_not_called()
    assert not any(c.args[0][:2] == ["tmux", "kill-pane"] for c in run.call_args_list)
    send_signal.assert_not_called()
    lines = [line for line in _hook_log().splitlines() if _DEDICATED_SOCKET in line]
    assert len(lines) == 1, _hook_log()


def test_stop_of_a_silent_dedicated_nvim_closes_its_pane_and_reaps_what_survives() -> None:
    _nvim_follower(adopted=False)
    listing = f"%1\t100\tzsh\n%5\t200\tnvim --listen {_DEDICATED_SOCKET}\n"
    alive = {200: False, 201: True}
    with (
        patch(
            "vim_ai_follower.tmux.subprocess.run",
            side_effect=_tmux_with_panes(listing, children="201\n"),
        ) as run,
        patch("pynvim.attach") as attach,
        patch("vim_ai_follower.commands.nvim_connect.probe_nvim", return_value="silent"),
        patch("vim_ai_follower.commands._process_alive", side_effect=lambda pid: alive[pid]),
        patch("vim_ai_follower.commands._REAP_GRACE_SECONDS", 0.05),
        patch("vim_ai_follower.commands._send_signal") as send_signal,
    ):
        assert commands.cmd_stop({"TMUX_PANE": "%1"}) == 0
    attach.assert_not_called()
    kills = [c.args[0] for c in run.call_args_list if c.args[0][:2] == ["tmux", "kill-pane"]]
    assert kills == [["tmux", "kill-pane", "-t", "%5"]]
    import signal

    send_signal.assert_called_once_with(201, signal.SIGKILL)
    assert "did not answer" in _hook_log()


def test_stop_never_kills_a_survivor_that_is_not_the_followers_nvim() -> None:
    _nvim_follower(adopted=False)
    listing = f"%5\t200\tnvim --listen {_DEDICATED_SOCKET}\n"
    base = _tmux_with_panes(listing, children="201\n")

    def _run(cmd: list[str], **kwargs: object) -> MagicMock:
        if cmd[:2] == ["ps", "-o"]:
            return MagicMock(returncode=0, stdout="some-other-process\n")
        return base(cmd, **kwargs)

    with (
        patch("vim_ai_follower.tmux.subprocess.run", side_effect=_run),
        patch("vim_ai_follower.commands.nvim_connect.probe_nvim", return_value="silent"),
        patch("vim_ai_follower.commands._process_alive", return_value=True),
        patch("vim_ai_follower.commands._REAP_GRACE_SECONDS", 0.05),
        patch("vim_ai_follower.commands._send_signal") as send_signal,
    ):
        assert commands.cmd_stop({"TMUX_PANE": "%1"}) == 0
    send_signal.assert_not_called()


def test_stop_of_a_silent_dedicated_nvim_without_its_pane_kills_nothing() -> None:
    # The follower nvim is not in a pane of the origin's window (a
    # standalone window opened from tmux, or a pane already gone).
    _nvim_follower(adopted=False)
    listing = "%1\t100\tzsh\n%6\t300\tnvim --listen /tmp/someone-else.sock\n"
    with (
        patch("vim_ai_follower.tmux.subprocess.run", side_effect=_tmux_with_panes(listing)) as run,
        patch("vim_ai_follower.commands.nvim_connect.probe_nvim", return_value="silent"),
        patch("vim_ai_follower.commands._send_signal") as send_signal,
    ):
        assert commands.cmd_stop({"TMUX_PANE": "%1"}) == 0
    assert not any(c.args[0][:2] == ["tmux", "kill-pane"] for c in run.call_args_list)
    send_signal.assert_not_called()


def test_stop_of_an_answering_dedicated_nvim_quits_it_and_kills_no_pane_that_closed() -> None:
    _nvim_follower(adopted=False)
    nvim_mock = MagicMock()
    nvim_mock.api.get_mode.return_value = {"mode": "n", "blocking": False}
    listings = iter(
        [f"%1\t100\tzsh\n%5\t200\tnvim --listen {_DEDICATED_SOCKET}\n", "%1\t100\tzsh\n"]
    )
    base = _tmux_with_panes("")

    def _run(cmd: list[str], **kwargs: object) -> MagicMock:
        if cmd[:3] == ["tmux", "list-panes", "-t"]:
            return MagicMock(returncode=0, stdout=next(listings, "%1\t100\tzsh\n"))
        return base(cmd, **kwargs)

    with (
        patch("vim_ai_follower.tmux.subprocess.run", side_effect=_run) as run,
        patch("pynvim.attach", return_value=nvim_mock),
        patch("vim_ai_follower.commands.nvim_connect.probe_nvim", return_value="answers"),
        patch("vim_ai_follower.commands._send_signal") as send_signal,
    ):
        assert commands.cmd_stop({"TMUX_PANE": "%1"}) == 0
    nvim_mock.command.assert_any_call("qall!")
    assert not any(c.args[0][:2] == ["tmux", "kill-pane"] for c in run.call_args_list)
    send_signal.assert_not_called()


def test_stop_kills_the_pane_of_a_dedicated_nvim_that_did_not_quit() -> None:
    _nvim_follower(adopted=False)
    listing = f"%1\t100\tzsh\n%5\t200\tnvim --listen {_DEDICATED_SOCKET}\n"
    with (
        patch("vim_ai_follower.tmux.subprocess.run", side_effect=_tmux_with_panes(listing)) as run,
        patch("pynvim.attach", return_value=MagicMock()),
        patch("vim_ai_follower.commands.nvim_connect.probe_nvim", return_value="answers"),
        patch("vim_ai_follower.commands._QUIT_GRACE_SECONDS", 0.05),
        patch("vim_ai_follower.commands._process_alive", return_value=False),
    ):
        assert commands.cmd_stop({"TMUX_PANE": "%1"}) == 0
    kills = [c.args[0] for c in run.call_args_list if c.args[0][:2] == ["tmux", "kill-pane"]]
    assert kills == [["tmux", "kill-pane", "-t", "%5"]]


def test_stop_gives_up_the_rest_of_the_teardown_after_a_timeout() -> None:
    _nvim_follower(adopted=True, open_files=("/tmp/a.py", "/tmp/b.py"))
    closed: list[str] = []

    def frozen_close(file_path: str, **_kwargs: object) -> None:
        closed.append(file_path)
        raise nvim_connect.RpcTimeout("nvim did not answer within 10 s")

    with (
        patch("vim_ai_follower.tmux.subprocess.run", side_effect=_mock_tmux_run()),
        patch("vim_ai_follower.commands.nvim_connect.probe_nvim", return_value="answers"),
        patch("vim_ai_follower.status_surface.NvimStatusSurface.clear"),
        patch("vim_ai_follower.backends.nvim.NvimFollower.close_tab", side_effect=frozen_close),
    ):
        assert commands.cmd_stop({"TMUX_PANE": "%1"}) == 0
    assert closed == ["/tmp/a.py"]
    assert "gave up" in _hook_log()


def test_stop_skips_the_teardown_once_its_budget_is_spent() -> None:
    _nvim_follower(adopted=True, open_files=("/tmp/a.py",))
    with (
        patch("vim_ai_follower.tmux.subprocess.run", side_effect=_mock_tmux_run()),
        patch("vim_ai_follower.commands.nvim_connect.probe_nvim", return_value="answers"),
        patch("vim_ai_follower.commands.nvim_connect.CONNECT_TIMEOUT_SECONDS", 0.0),
        patch("vim_ai_follower.commands.nvim_connect.RPC_TIMEOUT_SECONDS", 0.0),
        patch("vim_ai_follower.status_surface.NvimStatusSurface.clear") as clear,
        patch("vim_ai_follower.backends.nvim.NvimFollower.close_tab") as close_tab,
    ):
        assert commands.cmd_stop({"TMUX_PANE": "%1"}) == 0
    clear.assert_not_called()
    close_tab.assert_not_called()
    assert "gave up" in _hook_log()


def test_stop_passes_the_adopted_flag_it_read_to_close_tab() -> None:
    _nvim_follower(adopted=True, open_files=("/tmp/a.py",))
    with (
        patch("vim_ai_follower.tmux.subprocess.run", side_effect=_mock_tmux_run()),
        patch("vim_ai_follower.commands.nvim_connect.probe_nvim", return_value="answers"),
        patch("vim_ai_follower.status_surface.NvimStatusSurface.clear"),
        patch("vim_ai_follower.backends.nvim.NvimFollower.close_tab") as close_tab,
    ):
        assert commands.cmd_stop({"TMUX_PANE": "%1"}) == 0
    close_tab.assert_called_once_with("/tmp/a.py", dedicated=False)


def test_stop_survives_an_unreadable_state() -> None:
    (cache.CACHE_DIR).mkdir(parents=True, exist_ok=True)
    (cache.CACHE_DIR / "@1.pane").write_text("{not json")
    with patch("vim_ai_follower.tmux.subprocess.run", side_effect=_mock_tmux_run()):
        assert commands.cmd_stop({"TMUX_PANE": "%1"}) == 0
    assert not (cache.CACHE_DIR / "@1.pane").exists()


def test_stop_logs_a_tmux_teardown_step_that_fails_and_carries_on() -> None:
    state.FollowerState.set(
        "@1",
        "tmux",
        "%9",
        origin="%1",
        adopted=True,
        open_files=("/tmp/a.py", "/tmp/b.py"),
        server_pid=MOCK_SERVER_PID,
    )
    closed: list[str] = []

    def failing_close(file_path: str, **_kwargs: object) -> None:
        closed.append(file_path)
        raise subprocess.CalledProcessError(1, ["tmux", "send-keys"])

    with (
        patch("vim_ai_follower.tmux.subprocess.run", side_effect=_mock_tmux_run()),
        patch(
            "vim_ai_follower.backends.tmux_vim.TmuxVimFollower.close_tab",
            side_effect=failing_close,
        ),
    ):
        assert commands.cmd_stop({"TMUX_PANE": "%1"}) == 0
    assert closed == ["/tmp/a.py", "/tmp/b.py"]
    assert "stop: closing /tmp/b.py failed" in _hook_log()
    assert state.FollowerState.read("@1") is None


def test_stop_logs_an_nvim_step_that_fails_without_a_timeout_and_carries_on() -> None:
    _nvim_follower(adopted=True, open_files=("/tmp/a.py", "/tmp/b.py"))
    closed: list[str] = []

    def failing_close(file_path: str, **_kwargs: object) -> None:
        closed.append(file_path)
        raise RuntimeError("nvim said no")

    with (
        patch("vim_ai_follower.tmux.subprocess.run", side_effect=_mock_tmux_run()),
        patch("vim_ai_follower.commands.nvim_connect.probe_nvim", return_value="answers"),
        patch("vim_ai_follower.status_surface.NvimStatusSurface.clear"),
        patch("vim_ai_follower.backends.nvim.NvimFollower.close_tab", side_effect=failing_close),
    ):
        assert commands.cmd_stop({"TMUX_PANE": "%1"}) == 0
    assert closed == ["/tmp/a.py", "/tmp/b.py"]
    assert "stop: closing /tmp/a.py failed: nvim said no" in _hook_log()


@pytest.mark.parametrize(
    "listing",
    [
        None,  # list-panes failed: the origin's window is gone
        "%5\t200\tnvim --listen 'unterminated\n",  # a start command shlex cannot split
        "%5\tnot-a-pid\tnvim --listen /tmp/nvim-@1.sock\n",
        "%5\t200\n",  # a line without a start command
        "%1\t100\tnvim --listen /tmp/nvim-@1.sock\n",  # the origin itself: never
    ],
)
def test_stop_finds_no_follower_pane_in_a_listing_it_cannot_trust(listing: str | None) -> None:
    _nvim_follower(adopted=False)
    base = _tmux_with_panes(listing or "")

    def _run(command: list[str], **kwargs: object) -> MagicMock:
        if listing is None and command[:3] == ["tmux", "list-panes", "-t"]:
            return MagicMock(returncode=1, stdout="")
        return base(command, **kwargs)

    with (
        patch("vim_ai_follower.tmux.subprocess.run", side_effect=_run) as run,
        patch("vim_ai_follower.commands.nvim_connect.probe_nvim", return_value="silent"),
    ):
        assert commands.cmd_stop({"TMUX_PANE": "%1"}) == 0
    assert not any(call.args[0][:2] == ["tmux", "kill-pane"] for call in run.call_args_list)


def test_process_alive_and_send_signal_against_real_processes() -> None:
    import signal

    sleeper = subprocess.Popen(["sleep", "30"])
    try:
        assert commands._process_alive(sleeper.pid) is True
        commands._send_signal(sleeper.pid, signal.SIGKILL)
        sleeper.wait(timeout=5)
        assert commands._process_alive(sleeper.pid) is False
        commands._send_signal(sleeper.pid, signal.SIGKILL)  # gone: no error
    finally:
        sleeper.kill()
        sleeper.wait()
    assert commands._process_alive(1) is True  # launchd: alive, not ours to signal


def test_tmux_stop_never_kills_an_adopted_pane() -> None:
    with patch("vim_ai_follower.tmux.subprocess.run") as run:
        tmux_vim.TmuxVimFollower(pane_id="%2").stop(dedicated=False)
    run.assert_not_called()


# --- final review of T17: nothing reaches a pane not proven the follower's --
#
# C1/C2: the `.pane` outlives a tmux restart and the new server numbers its
# panes from %0 again, so a stale state's target can be Claude's own pane
# (killed) or a shell (typed into). The proof is the server the state was
# written under (state.proven_here), then a pane that still runs vim.


def _touches_the_pane(run: MagicMock, pane_id: str) -> list[list[str]]:
    """Every call that would act on pane_id: a kill, typed keys, an option."""
    acting = ("kill-pane", "send-keys", "set-option", "resize-pane", "respawn-pane")
    return [
        c.args[0]
        for c in run.call_args_list
        if len(c.args[0]) > 1 and c.args[0][1] in acting and pane_id in c.args[0]
    ]


@pytest.mark.parametrize("adopted", [False, True])
@pytest.mark.parametrize("recorded", [1111, None], ids=["another-server", "no-server-recorded"])
def test_stop_sends_nothing_to_a_pane_not_proven_under_this_server(
    adopted: bool, recorded: int | None
) -> None:
    # The worst case: the stale target is the caller's own pane, and it is
    # even running vim (the reviewer's repro: %0, the origin).
    state.FollowerState.set(
        "@1",
        "tmux",
        "%1",
        origin="%1",
        adopted=adopted,
        open_files=("/tmp/x.py",),
        shown_any=True,
        server_pid=recorded,
    )
    with patch(
        "vim_ai_follower.tmux.subprocess.run",
        side_effect=make_mock_tmux_run(pane_id="%1"),
    ) as run:
        assert commands.cmd_stop({"TMUX_PANE": "%1"}) == 0
    assert _touches_the_pane(run, "%1") == []
    assert state.FollowerState.read("@1") is None
    lines = [line for line in _hook_log().splitlines() if "not proven" in line]
    assert len(lines) == 1, _hook_log()


@pytest.mark.parametrize("adopted", [False, True])
def test_stop_sends_no_keys_and_no_kill_to_a_pane_that_no_longer_runs_vim(adopted: bool) -> None:
    # Under this server, but the follower's Vim is gone: the pane is now a
    # shell (or `cat`), and typed Vim script would run there.
    state.FollowerState.set(
        "@1",
        "tmux",
        "%9",
        origin="%1",
        adopted=adopted,
        open_files=("/tmp/x.py",),
        shown_any=True,
        server_pid=MOCK_SERVER_PID,
    )
    with patch(
        "vim_ai_follower.tmux.subprocess.run",
        side_effect=make_mock_tmux_run(pane_id="%9", pane_exists=False, other_panes=("%9",)),
    ) as run:
        assert commands.cmd_stop({"TMUX_PANE": "%1"}) == 0
    calls = [c.args[0] for c in run.call_args_list]
    assert not any(c[:2] == ["tmux", "kill-pane"] for c in calls)
    assert not any(c[:2] == ["tmux", "send-keys"] for c in calls)
    assert "no longer runs vim" in _hook_log()


def test_stop_leaves_the_pane_of_a_dedicated_nvim_from_another_server() -> None:
    state.FollowerState.set(
        "@1", "nvim", _DEDICATED_SOCKET, origin="%1", adopted=False, server_pid=1111
    )
    listing = f"%1\t100\tzsh\n%5\t200\tnvim --listen {_DEDICATED_SOCKET}\n"
    with (
        patch("vim_ai_follower.tmux.subprocess.run", side_effect=_tmux_with_panes(listing)) as run,
        patch("vim_ai_follower.commands.nvim_connect.probe_nvim", return_value="silent"),
        patch("vim_ai_follower.commands._send_signal") as send_signal,
    ):
        assert commands.cmd_stop({"TMUX_PANE": "%1"}) == 0
    assert not any(c.args[0][:2] == ["tmux", "kill-pane"] for c in run.call_args_list)
    send_signal.assert_not_called()
    assert "is left alone: its state was not written under this tmux server" in _hook_log()


def test_stop_keeps_a_record_of_what_it_tears_down_until_the_teardown_is_over() -> None:
    # Minor 6: the state goes before the teardown, so a stop killed mid-way
    # needs something else to name the pane for the next stop.
    _register_fake_follower("@1", "%9")
    pane_state = (cache.CACHE_DIR / "@1.pane").read_text()
    seen: list[str] = []

    def killing(*_args: object, **_kwargs: object) -> None:
        seen.append(state.stopping_path("@1").read_text())

    with (
        patch("vim_ai_follower.tmux.subprocess.run", side_effect=_mock_tmux_run()),
        patch("vim_ai_follower.backends.tmux_vim.TmuxVimFollower.stop", side_effect=killing),
    ):
        assert commands.cmd_stop({"TMUX_PANE": "%1"}) == 0
    assert seen == [pane_state]
    assert not state.stopping_path("@1").exists()


def test_a_second_stop_finishes_the_teardown_of_a_stop_that_was_killed() -> None:
    _register_fake_follower("@1", "%9")
    state.stopping_path("@1").write_text((cache.CACHE_DIR / "@1.pane").read_text())
    state.FollowerState.clear("@1")
    state.mark_stopped("@1", MOCK_SERVER_PID, in_tmux=True)
    with patch("vim_ai_follower.tmux.subprocess.run", side_effect=_mock_tmux_run()) as run:
        assert commands.cmd_stop({"TMUX_PANE": "%1"}) == 0
    kills = [c.args[0] for c in run.call_args_list if c.args[0][:2] == ["tmux", "kill-pane"]]
    assert kills == [["tmux", "kill-pane", "-t", "%9"]]
    assert not state.stopping_path("@1").exists()


def test_a_stop_with_an_unreadable_record_tears_down_nothing() -> None:
    state.stopping_path("@1").parent.mkdir(parents=True, exist_ok=True)
    state.stopping_path("@1").write_text("{not json")
    with patch("vim_ai_follower.tmux.subprocess.run", side_effect=_mock_tmux_run()) as run:
        assert commands.cmd_stop({"TMUX_PANE": "%1"}) == 0
    assert not any(c.args[0][:2] == ["tmux", "kill-pane"] for c in run.call_args_list)
    assert not state.stopping_path("@1").exists()


def test_the_nvim_teardown_including_its_pane_close_holds_its_budget() -> None:
    # Minor 1: the RPC steps used the whole CONNECT + RPC budget, then the
    # pane close added its quit grace and its reap grace on top (~15 s for a
    # 13 s bound). A fake clock makes the worst case exact: every step runs
    # until a hair before the deadline it was given (the alarm that would
    # end it), the pane never goes, and its process never dies.
    _nvim_follower(adopted=False)
    clock = [0.0]
    budget = nvim_connect.CONNECT_TIMEOUT_SECONDS + nvim_connect.RPC_TIMEOUT_SECONDS
    deadlines: list[float] = []

    def monotonic() -> float:
        return clock[0]

    def sleep(seconds: float) -> None:
        clock[0] += seconds

    @contextlib.contextmanager
    def rpc_deadline(seconds: float) -> Iterator[None]:
        deadlines.append(seconds)
        yield

    def slow_step(*_args: object, **_kwargs: object) -> None:
        clock[0] += deadlines[-1] - 0.01

    with (
        patch("vim_ai_follower.tmux.subprocess.run", side_effect=_mock_tmux_run()),
        patch("vim_ai_follower.commands.time.monotonic", side_effect=monotonic),
        patch("vim_ai_follower.commands.time.sleep", side_effect=sleep),
        patch("vim_ai_follower.commands.nvim_connect.probe_nvim", return_value="answers"),
        patch("vim_ai_follower.commands.rpc_deadline", side_effect=rpc_deadline),
        patch("vim_ai_follower.status_surface.NvimStatusSurface.clear", side_effect=slow_step),
        patch("vim_ai_follower.backends.nvim.NvimFollower.stop", side_effect=slow_step),
        patch("vim_ai_follower.commands._pane_running", return_value=("%5", 200)),
        patch("vim_ai_follower.commands._child_pids", return_value=[]),
        patch("vim_ai_follower.commands.TmuxPane.kill"),
        patch("vim_ai_follower.commands._process_alive", return_value=True),
        patch("vim_ai_follower.commands._command_line", return_value=_DEDICATED_SOCKET),
        patch("vim_ai_follower.commands._send_signal") as send_signal,
    ):
        assert commands.cmd_stop({"TMUX_PANE": "%1"}) == 0
    send_signal.assert_called_once()
    assert clock[0] <= budget + 0.1, clock[0]


def test_stop_names_the_server_from_tmux_variable_when_tmux_does_not_answer() -> None:
    # Minor 4: a tombstone written with no pid inside tmux held under every
    # later server; $TMUX carries the pid ("socket,pid,session").
    with (
        patch("vim_ai_follower.tmux.subprocess.run", side_effect=_mock_tmux_run()),
        patch("vim_ai_follower.binding.tmux_server_pid", return_value=None),
    ):
        assert commands.cmd_stop({"TMUX_PANE": "%1", "TMUX": "/tmp/s,777,0"}) == 0
        assert state.stopped_path("@1").read_text() == "777"
        assert commands.cmd_stop({"TMUX_PANE": "%1", "TMUX": "garbled"}) == 0
        assert state.stopped_path("@1").read_text() == "unknown"


def test_a_start_that_fails_keeps_the_stop() -> None:
    # Minor 5: the tombstone used to go before the launch.
    state.mark_stopped("@1", MOCK_SERVER_PID, in_tmux=True)
    with (
        patch("vim_ai_follower.tmux.subprocess.run", side_effect=_mock_tmux_run()),
        patch(
            "vim_ai_follower.commands.TmuxVimFollower.start",
            side_effect=subprocess.CalledProcessError(1, ["tmux", "split-window"]),
        ),
        pytest.raises(subprocess.CalledProcessError),
    ):
        commands.cmd_start({"TMUX_PANE": "%1"})
    assert state.stopped_path("@1").exists()


def test_a_start_of_a_running_follower_lifts_a_stop() -> None:
    _register_fake_follower("@1", "%9")
    state.mark_stopped("@1", MOCK_SERVER_PID, in_tmux=True)
    with patch("vim_ai_follower.tmux.subprocess.run", side_effect=_mock_tmux_run()):
        assert commands.cmd_start({"TMUX_PANE": "%1"}) == 0
    assert not state.stopped_path("@1").exists()


def test_toggle_ignores_a_state_left_by_another_tmux_server(
    capsys: pytest.CaptureFixture[str],
) -> None:
    _register_fake_follower("@1", "%9", server_pid=1111)
    state.FollowerState.update("@1", origin="%1")
    with patch("vim_ai_follower.tmux.subprocess.run", side_effect=_mock_tmux_run()) as run:
        assert commands.cmd_toggle({"TMUX_PANE": "%1"}) == 0
    assert "no follower to toggle" in capsys.readouterr().out
    assert not any(c.args[0][:2] == ["tmux", "resize-pane"] for c in run.call_args_list)


# --- a tmux split nvim that never answers (T19, answer B) -----------------


def test_start_reports_a_split_nvim_that_never_answers_and_records_nothing(
    capsys: pytest.CaptureFixture[str],
) -> None:
    never = nvim_connect.SplitNvimNeverAnswered("/c/nvim-@1.sock", 10.0, "%7", pane_open=True)
    with (
        patch("vim_ai_follower.tmux.subprocess.run", side_effect=_mock_tmux_run()) as run,
        patch("vim_ai_follower.commands.resolve_nvim_target", side_effect=never),
    ):
        assert commands.cmd_start({"TMUX_PANE": "%1"}, backend="nvim") == 1
    captured = capsys.readouterr()
    assert "attached" not in captured.out
    assert captured.err == (
        "claude-follow: nvim did not answer on /c/nvim-@1.sock within 10 s; "
        "its pane is left open so you can read why\n"
    )
    assert state.FollowerState.read("@1") is None
    assert not [c for c in run.call_args_list if c.args[0][:2] == ["tmux", "kill-pane"]]
    # The next hook must not split a second pane next to this one at once.
    assert nvim_connect.launch_backoff_active("@1")
    assert "nvim did not answer on /c/nvim-@1.sock" in hooks.LOG_PATH.read_text()


def test_start_splits_at_once_despite_a_backoff_and_waits_ten_seconds() -> None:
    # `start` is the user's explicit retry: it bypasses the backoff a failed
    # auto-open left, as for a standalone window, with the patient deadline.
    nvim_connect.record_launch_failure("@1")
    with (
        patch("vim_ai_follower.tmux.subprocess.run", side_effect=_mock_tmux_run()),
        patch(
            "vim_ai_follower.commands.resolve_nvim_target", return_value=("/tmp/nvim.sock", True)
        ) as resolve,
    ):
        assert commands.cmd_start({"TMUX_PANE": "%1"}, backend="nvim") == 0
    resolve.assert_called_once_with(
        "%1", "@1", adopt=False, wait_seconds=nvim_connect.START_SPLIT_WAIT_SECONDS
    )
    assert not nvim_connect.launch_backoff_active("@1")


def test_start_reports_a_split_socket_it_cannot_clear(capsys: pytest.CaptureFixture[str]) -> None:
    blocked = nvim_connect.SocketPathBlocked("/c/nvim-@1.sock", OSError("a listener"))
    with (
        patch("vim_ai_follower.tmux.subprocess.run", side_effect=_mock_tmux_run()),
        patch("vim_ai_follower.commands.resolve_nvim_target", side_effect=blocked),
    ):
        assert commands.cmd_start({"TMUX_PANE": "%1"}, backend="nvim") == 1
    assert capsys.readouterr().err == "claude-follow: cannot clear /c/nvim-@1.sock: a listener\n"
    assert state.FollowerState.read("@1") is None


def test_start_reports_a_tmux_that_cannot_split_in_one_line(
    capsys: pytest.CaptureFixture[str],
) -> None:
    # Review m4: the split's CalledProcessError used to end `start` in a
    # traceback. No backoff: tmux failing is not an nvim that never came up.
    refused = subprocess.CalledProcessError(
        1, ["tmux", "split-window"], stderr="no space for new pane\n"
    )
    with (
        patch("vim_ai_follower.tmux.subprocess.run", side_effect=_mock_tmux_run()),
        patch("vim_ai_follower.commands.resolve_nvim_target", side_effect=refused),
    ):
        assert commands.cmd_start({"TMUX_PANE": "%1"}, backend="nvim") == 1
    captured = capsys.readouterr()
    assert (
        captured.err
        == "claude-follow: tmux could not split a pane for nvim: no space for new pane\n"
    )
    assert "attached" not in captured.out
    assert state.FollowerState.read("@1") is None
    assert not nvim_connect.launch_backoff_active("@1")


@pytest.mark.parametrize(
    ("typing", "failure", "logged"),
    [
        ("in insert mode", None, "(in insert mode)"),
        (None, OSError("ps vanished"), "(its mode could not be read: ps vanished)"),
    ],
    ids=["typing", "unreadable"],
)
def test_stop_of_an_adopted_vim_whose_user_is_typing_sends_nothing_and_asks_nothing(
    typing: str | None, failure: Exception | None, logged: str
) -> None:
    # `stop` takes no answer (Alberto, 2026-10-17): close_tab's Escape pair
    # would end the user's mode, so nothing reaches the Vim, and a mode it
    # cannot read counts the same.
    with patch(
        "vim_ai_follower.tmux.subprocess.run",
        return_value=MagicMock(returncode=0, stdout="%7 vim\n"),
    ):
        state.FollowerState.set(
            "@1",
            "tmux",
            "%7",
            origin="%1",
            adopted=True,
            open_files=("/tmp/a.py",),
            shown_any=True,
            server_pid=MOCK_SERVER_PID,
        )
    with (
        patch(
            "vim_ai_follower.tmux.subprocess.run",
            side_effect=make_mock_tmux_run(pane_id="%7", other_panes=("%1",)),
        ) as run,
        patch(
            "vim_ai_follower.backends.tmux_vim.typing_mode",
            return_value=typing,
            side_effect=failure,
        ) as read,
        patch("vim_ai_follower.take_over.ask") as asked,
    ):
        assert commands.cmd_stop({"TMUX_PANE": "%1"}) == 0
    read.assert_called_once_with("%7")
    asked.assert_not_called()
    sends = [c.args[0] for c in run.call_args_list if c.args[0][:2] == ["tmux", "send-keys"]]
    assert sends == []
    assert state.FollowerState.get("@1") is None
    lines = [line for line in _hook_log().splitlines() if "waiting on its user" in line]
    assert len(lines) == 1 and logged in lines[0], _hook_log()
