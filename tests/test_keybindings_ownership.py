"""Keybinding ownership: which installation the server-global prefix keys
belong to, and the rule for re-pointing or taking them (spec:
2026-09-22-keybinding-ownership-design)."""

from __future__ import annotations

import functools
import json
import os
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from helpers import make_mock_tmux_run

from vim_ai_follower import cli, commands, hooks, keybindings, state

_mock_tmux_run = functools.partial(make_mock_tmux_run, pane_id="%9", other_panes=("%1",))


def _fake_install(root: Path, version: str) -> Path:
    """A plugin-cache-shaped install: <root>/vim-ai-follower/<version>/bin/claude-follow."""
    plugin_root = root / "vim-ai-follower" / version
    exe = plugin_root / "bin" / "claude-follow"
    exe.parent.mkdir(parents=True)
    exe.write_text("#!/bin/sh\n")
    exe.chmod(0o755)
    return plugin_root


def test_plugin_installation_id_strips_the_version_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    plugin_root = _fake_install(tmp_path, "0.2.0")
    monkeypatch.setenv("CLAUDE_PLUGIN_ROOT", str(plugin_root))
    owner = keybindings.current_owner()
    assert owner.executable == str(plugin_root / "bin" / "claude-follow")
    assert owner.installation == str(plugin_root.parent)


def test_non_plugin_installation_id_is_the_executable_itself(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("CLAUDE_PLUGIN_ROOT", raising=False)
    exe = tmp_path / "checkout" / "bin" / "claude-follow"
    monkeypatch.setattr(keybindings, "_claude_follow_executable", lambda: str(exe))
    assert keybindings.current_owner() == keybindings.Owner(str(exe), str(exe))


def test_register_records_the_owner_and_unregister_deletes_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    plugin_root = _fake_install(tmp_path, "0.2.0")
    monkeypatch.setenv("CLAUDE_PLUGIN_ROOT", str(plugin_root))
    with patch("vim_ai_follower.tmux.subprocess.run", side_effect=_mock_tmux_run()):
        keybindings.register()
        assert json.loads(keybindings._owner_path().read_text()) == {
            "executable": str(plugin_root / "bin" / "claude-follow"),
            "installation": str(plugin_root.parent),
        }
        keybindings.unregister()
    assert not keybindings._owner_path().exists()


@pytest.mark.parametrize("content", ["", "{not json", "[]", '{"executable": "/x"}'])
def test_a_malformed_owner_record_reads_as_absent(content: str) -> None:
    path = keybindings._owner_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)
    assert keybindings.read_owner() is None


def _record(owner: keybindings.Owner) -> None:
    path = keybindings._owner_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    record = {"executable": owner.executable, "installation": owner.installation}
    path.write_text(json.dumps(record))


def _bound_paths(run: MagicMock) -> list[str]:
    """The run-shell command strings of every bind-key call made."""
    return [c.args[0][-1] for c in run.call_args_list if c.args[0][:2] == ["tmux", "bind-key"]]


@pytest.fixture
def plugin(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """This process runs as plugin version 0.2.0; returns its plugin root."""
    root = _fake_install(tmp_path, "0.2.0")
    monkeypatch.setenv("CLAUDE_PLUGIN_ROOT", str(root))
    return root


def _claim(**kwargs: bool) -> tuple[keybindings.Claim, MagicMock]:
    with patch("vim_ai_follower.tmux.subprocess.run", side_effect=_mock_tmux_run()) as run:
        result = keybindings.claim(**kwargs)
    return result, run


def test_claim_with_no_owner_takes_the_keys(plugin: Path) -> None:
    result, run = _claim()
    assert result.outcome is keybindings.ClaimOutcome.TAKEN
    assert result.previous is None
    assert all(str(plugin / "bin" / "claude-follow") in cmd for cmd in _bound_paths(run))
    assert len(_bound_paths(run)) == 5


def test_claim_same_installation_same_path_binds_nothing(plugin: Path) -> None:
    _record(keybindings.current_owner())
    result, run = _claim()
    assert result.outcome is keybindings.ClaimOutcome.UNCHANGED
    assert _bound_paths(run) == []


def test_claim_repair_rebinds_even_when_unchanged(plugin: Path) -> None:
    """A tmux server restart drops the bindings but keeps the cache: `start`
    (repair=True) must still bind, or it stops being a repair."""
    _record(keybindings.current_owner())
    result, run = _claim(repair=True)
    assert result.outcome is keybindings.ClaimOutcome.UNCHANGED
    assert len(_bound_paths(run)) == 5


def test_claim_same_installation_new_version_refreshes(plugin: Path, tmp_path: Path) -> None:
    old = _fake_install(tmp_path, "0.1.0")  # still on disk: kept-alive old version
    _record(keybindings.Owner(str(old / "bin" / "claude-follow"), str(old.parent)))
    result, run = _claim()
    assert result.outcome is keybindings.ClaimOutcome.REFRESHED
    assert all("0.2.0" in cmd for cmd in _bound_paths(run))
    assert len(_bound_paths(run)) == 5


def test_claim_takes_from_a_dead_foreign_owner(plugin: Path, tmp_path: Path) -> None:
    gone = tmp_path / "deleted-checkout" / "bin" / "claude-follow"
    _record(keybindings.Owner(str(gone), str(gone)))
    result, run = _claim()
    assert result.outcome is keybindings.ClaimOutcome.TAKEN_FROM_DEAD
    assert len(_bound_paths(run)) == 5


def test_claim_keeps_a_live_foreign_owner(plugin: Path, tmp_path: Path) -> None:
    other = tmp_path / "dev" / "bin" / "claude-follow"
    other.parent.mkdir(parents=True)
    other.write_text("#!/bin/sh\n")
    other.chmod(0o755)
    owner = keybindings.Owner(str(other), str(other))
    _record(owner)
    with patch("vim_ai_follower.tmux.subprocess.run", side_effect=_holding(owner)) as run:
        result = keybindings.claim(repair=True)
    assert result.outcome is keybindings.ClaimOutcome.KEPT_FOREIGN
    assert _bound_paths(run) == []
    assert keybindings.read_owner() == keybindings.Owner(str(other), str(other))


def test_claim_force_takes_from_a_live_foreign_owner(plugin: Path, tmp_path: Path) -> None:
    other = tmp_path / "dev" / "bin" / "claude-follow"
    other.parent.mkdir(parents=True)
    other.write_text("#!/bin/sh\n")
    other.chmod(0o755)
    _record(keybindings.Owner(str(other), str(other)))
    result, run = _claim(force=True)
    assert result.outcome is keybindings.ClaimOutcome.TAKEN_BY_FORCE
    assert len(_bound_paths(run)) == 5
    assert keybindings.read_owner() == keybindings.current_owner()


def test_a_bare_name_owner_counts_as_dead(plugin: Path) -> None:
    _record(keybindings.Owner("claude-follow", "claude-follow"))
    result, _ = _claim()
    assert result.outcome is keybindings.ClaimOutcome.TAKEN_FROM_DEAD


def test_a_non_executable_owner_counts_as_dead(plugin: Path, tmp_path: Path) -> None:
    plain = tmp_path / "dev" / "bin" / "claude-follow"
    plain.parent.mkdir(parents=True)
    plain.write_text("")  # exists, mode 0644
    _record(keybindings.Owner(str(plain), str(plain)))
    assert _claim()[0].outcome is keybindings.ClaimOutcome.TAKEN_FROM_DEAD


def test_a_malformed_record_is_taken_over(plugin: Path) -> None:
    keybindings._owner_path().parent.mkdir(parents=True, exist_ok=True)
    keybindings._owner_path().write_text("{trunc")
    assert _claim()[0].outcome is keybindings.ClaimOutcome.TAKEN


def test_describe_names_both_sides() -> None:
    old = keybindings.Owner("/a/0.1.0/bin/claude-follow", "/a")
    new = keybindings.Owner("/a/0.2.0/bin/claude-follow", "/a")
    dev = keybindings.Owner("/dev/bin/claude-follow", "/dev/bin/claude-follow")
    make, kind = keybindings.Claim, keybindings.ClaimOutcome
    assert keybindings.describe(make(kind.TAKEN, None, new)) is None
    assert keybindings.describe(make(kind.UNCHANGED, new, new)) is None
    assert keybindings.describe(make(kind.REFRESHED, old, new)) == (
        "keybindings re-pointed from /a/0.1.0/bin/claude-follow to /a/0.2.0/bin/claude-follow"
    )
    assert keybindings.describe(make(kind.TAKEN_FROM_DEAD, dev, new)) == (
        "keybindings moved here from /dev/bin/claude-follow, which no longer exists"
    )
    assert keybindings.describe(make(kind.TAKEN_BY_FORCE, dev, new)) == (
        "keybindings taken from /dev/bin/claude-follow (/dev/bin/claude-follow)"
    )
    assert keybindings.describe(make(kind.KEPT_FOREIGN, dev, new)) == (
        "keybindings belong to /dev/bin/claude-follow (/dev/bin/claude-follow) — left there;"
        " rerun `claude-follow start --take-keys` to move them here"
    )


def _holding(owner: keybindings.Owner) -> object:
    """A tmux mock whose prefix table shows `owner`'s P binding: a live
    foreign owner that actually HOLDS the keys (the only one claim() defers
    to since the stale-record fix)."""
    base = _mock_tmux_run()
    line = f'bind-key -T prefix P run-shell -b "TMUX_PANE=#{{pane_id}} {owner.executable} pause"\n'

    def run(cmd: list[str], **kwargs: object) -> MagicMock:
        if cmd[:2] == ["tmux", "list-keys"]:
            return MagicMock(returncode=0, stdout=line)
        return base(cmd, **kwargs)

    return run


def _live_foreign_owner(tmp_path: Path) -> keybindings.Owner:
    other = tmp_path / "dev" / "bin" / "claude-follow"
    other.parent.mkdir(parents=True)
    other.write_text("#!/bin/sh\n")
    other.chmod(0o755)
    owner = keybindings.Owner(str(other), str(other))
    _record(owner)
    return owner


def test_start_leaves_a_live_foreign_owner_and_says_how_to_force(
    plugin: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    owner = _live_foreign_owner(tmp_path)
    with patch("vim_ai_follower.tmux.subprocess.run", side_effect=_holding(owner)) as run:
        assert commands.cmd_start({"TMUX_PANE": "%1"}) == 0
    assert _bound_paths(run) == []
    out = capsys.readouterr().out
    assert f"keybindings belong to {owner.installation}" in out
    assert "--take-keys" in out


def test_start_take_keys_moves_them(
    plugin: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _live_foreign_owner(tmp_path)
    with patch("vim_ai_follower.tmux.subprocess.run", side_effect=_mock_tmux_run()) as run:
        assert commands.cmd_start({"TMUX_PANE": "%1"}, take_keys=True) == 0
    assert len(_bound_paths(run)) == 5
    assert "keybindings taken from" in capsys.readouterr().out


def test_cli_passes_take_keys_through(monkeypatch: pytest.MonkeyPatch) -> None:
    with patch("vim_ai_follower.commands.cmd_start", return_value=0) as start:
        assert cli.main(["start", "--take-keys"]) == 0
    assert start.call_args.kwargs["take_keys"] is True


def test_already_running_start_does_not_claim_a_refresh_it_did_not_do(
    plugin: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    with patch("vim_ai_follower.tmux.subprocess.run", side_effect=_mock_tmux_run()):
        assert commands.cmd_start({"TMUX_PANE": "%1"}) == 0
    capsys.readouterr()
    owner = _live_foreign_owner(tmp_path)
    with patch("vim_ai_follower.tmux.subprocess.run", side_effect=_holding(owner)):
        assert commands.cmd_start({"TMUX_PANE": "%1"}) == 0
    out = capsys.readouterr().out
    assert "already running" in out
    assert "keybindings refreshed" not in out
    assert "keybindings belong to" in out


def test_auto_open_logs_taking_keys_from_a_dead_owner(plugin: Path, tmp_path: Path) -> None:
    from vim_ai_follower import config, hooks

    config.CONFIG_PATH.write_text(json.dumps({"open_policy": "always"}))
    gone = tmp_path / "deleted-checkout" / "bin" / "claude-follow"
    _record(keybindings.Owner(str(gone), str(gone)))
    target = tmp_path / "f.py"
    target.write_text("x\n")
    payload = {"tool_name": "Read", "tool_input": {"file_path": str(target)}}
    with patch("vim_ai_follower.tmux.subprocess.run", side_effect=_mock_tmux_run()) as run:
        assert hooks.cmd_hook_post({"TMUX_PANE": "%1"}, payload) == 0
    assert len(_bound_paths(run)) == 5
    assert f"keybindings moved here from {gone}" in hooks.LOG_PATH.read_text()


def _read_payload(path: Path) -> dict[str, object]:
    return {"tool_name": "Read", "tool_input": {"file_path": str(path)}}


def _running_follower() -> None:
    state.FollowerState.set("@1", "tmux", "%9", origin="%1")


def test_hook_post_repoints_keys_after_a_plugin_update(plugin: Path, tmp_path: Path) -> None:
    old = tmp_path / "vim-ai-follower" / "0.1.0" / "bin" / "claude-follow"  # removed by update
    _record(keybindings.Owner(str(old), str(plugin.parent)))
    _running_follower()
    target = tmp_path / "f.py"
    target.write_text("x\n")
    with patch("vim_ai_follower.tmux.subprocess.run", side_effect=_mock_tmux_run()) as run:
        assert hooks.cmd_hook_post({"TMUX_PANE": "%1"}, _read_payload(target)) == 0
    assert all("0.2.0" in cmd for cmd in _bound_paths(run))
    assert len(_bound_paths(run)) == 5
    assert "keybindings re-pointed from" in hooks.LOG_PATH.read_text()


def test_hook_post_heals_before_navigating(plugin: Path, tmp_path: Path) -> None:
    _record(keybindings.Owner(str(tmp_path / "gone"), str(plugin.parent)))
    _running_follower()
    target = tmp_path / "f.py"
    target.write_text("x\n")
    with patch("vim_ai_follower.tmux.subprocess.run", side_effect=_mock_tmux_run()) as run:
        hooks.cmd_hook_post({"TMUX_PANE": "%1"}, _read_payload(target))
    cmds = [c.args[0] for c in run.call_args_list]
    first_bind = next(i for i, c in enumerate(cmds) if c[:2] == ["tmux", "bind-key"])
    first_keys = next(i for i, c in enumerate(cmds) if c[:2] == ["tmux", "send-keys"])
    assert first_bind < first_keys


def test_hook_post_without_a_follower_never_touches_the_keys(plugin: Path, tmp_path: Path) -> None:
    target = tmp_path / "f.py"
    target.write_text("x\n")
    with patch("vim_ai_follower.tmux.subprocess.run", side_effect=_mock_tmux_run()) as run:
        hooks.cmd_hook_post({"TMUX_PANE": "%1"}, _read_payload(target))
    assert _bound_paths(run) == []
    assert not keybindings._owner_path().exists()


def test_foreign_owner_warning_is_throttled(plugin: Path, tmp_path: Path) -> None:
    owner = _live_foreign_owner(tmp_path)
    _running_follower()
    target = tmp_path / "f.py"
    target.write_text("x\n")
    with patch("vim_ai_follower.tmux.subprocess.run", side_effect=_holding(owner)):
        for _ in range(3):
            hooks.cmd_hook_post({"TMUX_PANE": "%1"}, _read_payload(target))
    assert hooks.LOG_PATH.read_text().count("keybindings belong to") == 1


def test_a_failing_bind_never_fails_the_hook(plugin: Path, tmp_path: Path) -> None:
    import subprocess

    _record(keybindings.Owner(str(tmp_path / "gone"), str(tmp_path / "gone")))
    _running_follower()
    base = _mock_tmux_run()

    def run(cmd: list[str], **kwargs: object) -> MagicMock:
        if cmd[:2] == ["tmux", "bind-key"]:
            raise subprocess.CalledProcessError(1, cmd)
        return base(cmd, **kwargs)

    target = tmp_path / "f.py"
    target.write_text("x\n")
    with patch("vim_ai_follower.tmux.subprocess.run", side_effect=run):
        assert hooks.cmd_hook_post({"TMUX_PANE": "%1"}, _read_payload(target)) == 0
    assert "keybinding heal failed" in hooks.LOG_PATH.read_text()


def test_foreign_owner_warning_repeats_once_the_interval_has_passed(
    plugin: Path, tmp_path: Path
) -> None:
    owner = _live_foreign_owner(tmp_path)
    _running_follower()
    target = tmp_path / "f.py"
    target.write_text("x\n")
    with patch("vim_ai_follower.tmux.subprocess.run", side_effect=_holding(owner)):
        hooks.cmd_hook_post({"TMUX_PANE": "%1"}, _read_payload(target))
        stale = time.time() - hooks.IDENTITY_WARN_INTERVAL_SECONDS - 1
        os.utime(hooks._foreign_keys_warning_marker(), (stale, stale))
        hooks.cmd_hook_post({"TMUX_PANE": "%1"}, _read_payload(target))
    assert hooks.LOG_PATH.read_text().count("keybindings belong to") == 2


def test_an_edit_hook_also_heals_the_keys(plugin: Path, tmp_path: Path) -> None:
    old = tmp_path / "vim-ai-follower" / "0.1.0" / "bin" / "claude-follow"
    _record(keybindings.Owner(str(old), str(plugin.parent)))
    _running_follower()
    target = tmp_path / "f.py"
    target.write_text("x\n")
    payload = {"tool_name": "Write", "tool_input": {"file_path": str(target)}}
    with patch("vim_ai_follower.tmux.subprocess.run", side_effect=_mock_tmux_run()) as run:
        assert hooks.cmd_hook_post({"TMUX_PANE": "%1"}, payload) == 0
    assert len(_bound_paths(run)) == 5
    assert all("0.2.0" in cmd for cmd in _bound_paths(run))


# ---------------------------------------------------------------------------
# Final-review fixes (whole-branch review, 2026-09-23).
# ---------------------------------------------------------------------------


def _plugin_cache_install(root: Path, version: str) -> Path:
    """The REAL plugin-cache shape: <home>/.claude/plugins/cache/<marketplace>/
    <plugin>/<version>/bin/claude-follow — what `/start` resolves by itself."""
    marketplace = root / ".claude" / "plugins" / "cache" / "vim-ai-follower"
    return _fake_install(marketplace, version).resolve()


def test_start_via_slash_command_and_hooks_agree_on_the_installation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`/start` runs `claude-follow start` in the Bash tool, which has NO
    CLAUDE_PLUGIN_ROOT (measured 2026-09-23); hooks do have it. Both must
    name the same installation, or the user's own hooks see a live foreign
    owner and never re-point the keys after an update."""
    root = _plugin_cache_install(tmp_path, "0.2.6")
    exe = root / "bin" / "claude-follow"
    monkeypatch.delenv("CLAUDE_PLUGIN_ROOT", raising=False)
    monkeypatch.setattr(keybindings, "_bundled_wrapper", lambda: exe)
    started = keybindings.current_owner()

    monkeypatch.setenv("CLAUDE_PLUGIN_ROOT", str(root))
    hooked = keybindings.current_owner()

    assert started == hooked
    assert started.installation == str(root.parent)


def test_a_shell_started_old_version_is_refreshed_by_a_new_versions_hook(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    old = _plugin_cache_install(tmp_path, "0.2.6")
    new = _plugin_cache_install(tmp_path, "0.2.7")  # old dir kept alive
    monkeypatch.delenv("CLAUDE_PLUGIN_ROOT", raising=False)
    monkeypatch.setattr(keybindings, "_bundled_wrapper", lambda: old / "bin" / "claude-follow")
    _record(keybindings.current_owner())

    monkeypatch.setenv("CLAUDE_PLUGIN_ROOT", str(new))
    result, run = _claim()
    assert result.outcome is keybindings.ClaimOutcome.REFRESHED
    assert len(_bound_paths(run)) == 5
    assert all("0.2.7" in cmd for cmd in _bound_paths(run))


def _mock_run_with_bindings(lines: list[str]) -> object:
    base = _mock_tmux_run()

    def run(cmd: list[str], **kwargs: object) -> MagicMock:
        if cmd[:2] == ["tmux", "list-keys"]:
            return MagicMock(returncode=0, stdout="".join(line + "\n" for line in lines))
        return base(cmd, **kwargs)

    return run


def test_a_live_foreign_owner_that_no_longer_holds_the_keys_is_taken_over(
    plugin: Path, tmp_path: Path
) -> None:
    """A tmux server restart drops every binding while the owner record (and
    the other installation's executable) survive. Deferring to it would leave
    the user with no keys at all, announced as "belong to X"."""
    owner = _live_foreign_owner(tmp_path)
    run_fn = _mock_run_with_bindings(["bind-key -T prefix P send-keys C-a"])  # not the owner's
    with patch("vim_ai_follower.tmux.subprocess.run", side_effect=run_fn) as run:
        result = keybindings.claim()
    assert result.outcome is keybindings.ClaimOutcome.TAKEN_FROM_STALE
    assert result.previous == owner
    assert len(_bound_paths(run)) == 5


def test_a_live_foreign_owner_that_holds_the_keys_is_kept(plugin: Path, tmp_path: Path) -> None:
    owner = _live_foreign_owner(tmp_path)
    bound = f'bind-key -T prefix P run-shell -b "TMUX_PANE=#{{pane_id}} {owner.executable} pause"'
    with patch(
        "vim_ai_follower.tmux.subprocess.run", side_effect=_mock_run_with_bindings([bound])
    ) as run:
        result = keybindings.claim()
    assert result.outcome is keybindings.ClaimOutcome.KEPT_FOREIGN
    assert _bound_paths(run) == []


def test_describe_a_stale_takeover() -> None:
    dev = keybindings.Owner("/dev/bin/claude-follow", "/dev/bin/claude-follow")
    new = keybindings.Owner("/a/0.2.0/bin/claude-follow", "/a")
    result = keybindings.Claim(keybindings.ClaimOutcome.TAKEN_FROM_STALE, dev, new)
    assert keybindings.describe(result) == (
        "keybindings moved here: the record named /dev/bin/claude-follow,"
        " but no key was bound to it (tmux restarted?)"
    )


def test_auto_open_repairs_keys_even_when_the_record_already_names_us(
    plugin: Path, tmp_path: Path
) -> None:
    """Auto-open after a tmux restart: the record says "mine, same path" but
    the server has no bindings. repair=True is what re-binds them."""
    from vim_ai_follower import config

    config.CONFIG_PATH.write_text(json.dumps({"open_policy": "always"}))
    _record(keybindings.current_owner())
    target = tmp_path / "f.py"
    target.write_text("x\n")
    with patch("vim_ai_follower.tmux.subprocess.run", side_effect=_mock_tmux_run()) as run:
        hooks.cmd_hook_post({"TMUX_PANE": "%1"}, _read_payload(target))
    assert len(_bound_paths(run)) == 5


def test_the_per_hook_heal_touches_tmux_keys_only_when_something_changed(
    plugin: Path, tmp_path: Path
) -> None:
    """The common case of every hook: the record names us. No bind-key and no
    list-keys — otherwise every edit re-binds five server-global keys."""
    _record(keybindings.current_owner())
    _running_follower()
    target = tmp_path / "f.py"
    target.write_text("x\n")
    with patch("vim_ai_follower.tmux.subprocess.run", side_effect=_mock_tmux_run()) as run:
        hooks.cmd_hook_post({"TMUX_PANE": "%1"}, _read_payload(target))
    keys_calls = [
        c.args[0][:2]
        for c in run.call_args_list
        if c.args[0][:2] in (["tmux", "bind-key"], ["tmux", "list-keys"])
    ]
    assert keys_calls == []


def test_repeated_owner_lookups_spawn_git_at_most_once(monkeypatch: pytest.MonkeyPatch) -> None:
    """Every hook post heals the keys, and on the dev checkout (no
    CLAUDE_PLUGIN_ROOT) resolving the executable asks git whether it sits in a
    linked worktree. The answer for one path cannot change inside a process,
    so it must be computed once, not on every edit."""
    import subprocess as real_subprocess

    monkeypatch.delenv("CLAUDE_PLUGIN_ROOT", raising=False)
    calls: list[list[str]] = []
    original = real_subprocess.run

    def counting(cmd: list[str], **kwargs: object) -> object:
        calls.append(cmd)
        return original(cmd, **kwargs)  # type: ignore[call-overload]

    monkeypatch.setattr("vim_ai_follower.keybindings.subprocess.run", counting)
    first = keybindings.current_owner()
    second = keybindings.current_owner()
    assert first == second
    assert sum(cmd[:1] == ["git"] for cmd in calls) <= 1


def test_the_owner_record_is_replaced_atomically(plugin: Path) -> None:
    """Another installation's hook can read the record at any moment. A
    plain write_text truncates first, so a reader landing mid-write saw no
    owner and took live keys; the record must appear whole or not at all."""
    seen: list[tuple[str, str]] = []
    real_replace = os.replace

    def spying_replace(src: str, dst: str) -> None:
        seen.append((Path(src).read_text(), str(dst)))
        real_replace(src, dst)

    with (
        patch("vim_ai_follower.tmux.subprocess.run", side_effect=_mock_tmux_run()),
        patch("vim_ai_follower.keybindings.os.replace", side_effect=spying_replace),
    ):
        keybindings.register()
    assert len(seen) == 1
    content, target = seen[0]
    assert target == str(keybindings._owner_path())
    assert json.loads(content) == {
        "executable": keybindings.current_owner().executable,
        "installation": keybindings.current_owner().installation,
    }
    assert sorted(p.name for p in keybindings._owner_path().parent.iterdir()) == [
        "keybindings-owner.json",
        "saved-keybindings.json",
    ]


def test_a_failed_owner_write_leaves_no_temp_file(plugin: Path) -> None:
    with (
        patch("vim_ai_follower.tmux.subprocess.run", side_effect=_mock_tmux_run()),
        patch("vim_ai_follower.keybindings.os.replace", side_effect=OSError("disk full")),
        pytest.raises(OSError, match="disk full"),
    ):
        keybindings.register()
    assert sorted(p.name for p in keybindings._owner_path().parent.iterdir()) == [
        "saved-keybindings.json"
    ]


def test_a_dead_followers_leftover_state_does_not_heal_the_keys(
    plugin: Path, tmp_path: Path
) -> None:
    """The heal used to run whenever follower STATE existed, so a window whose
    follower pane had died still re-bound the server-global keys on every
    edit. Only a live follower's hook may touch them."""
    _record(keybindings.Owner(str(tmp_path / "gone"), str(tmp_path / "gone")))
    _running_follower()
    target = tmp_path / "f.py"
    target.write_text("x\n")
    dead = _mock_tmux_run(pane_exists=False)
    with patch("vim_ai_follower.tmux.subprocess.run", side_effect=dead) as run:
        assert hooks.cmd_hook_post({"TMUX_PANE": "%1"}, _read_payload(target)) == 0
    assert _bound_paths(run) == []
