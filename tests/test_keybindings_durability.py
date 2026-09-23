"""A tmux keybinding outlives the process that bound it — so must the path inside it.

Both defects here were measured on 2026-09-22. An agent ran `claude-follow
start` from inside a tool-created git worktree; because the keys are tmux
SERVER-global, that repointed the prefix keys of every window on the real
server at `<worktree>/bin/claude-follow`. When the worktree was deleted every
key started failing with exit 127, and `start` — the one thing a user retries
when the keys go dead — returned early without re-registering anything.
"""

from __future__ import annotations

import functools
import os
import shlex
import subprocess
from collections.abc import Callable
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from helpers import make_mock_tmux_run

from vim_ai_follower import commands, keybindings, session, state

_mock_tmux_run = functools.partial(make_mock_tmux_run, pane_id="%9", other_panes=("%1", "%2"))


def _git(cwd: Path, *args: str) -> None:
    subprocess.run(
        [
            "git",
            "-c",
            "user.email=test@example.com",
            "-c",
            "user.name=test",
            "-c",
            "commit.gpgsign=false",
            "-C",
            str(cwd),
            *args,
        ],
        check=True,
        capture_output=True,
    )


@pytest.fixture
def repo_with_worktree(tmp_path: Path) -> tuple[Path, Path]:
    """A real repo with a real linked worktree, both carrying bin/claude-follow.

    An honest `git worktree add` rather than a stubbed `git rev-parse`: the
    thing under test is how git reports a worktree (including which answers
    come back relative), and a stub would pin the implementation instead of
    the behavior. Returns (main checkout, worktree), both resolved.
    """
    main = tmp_path / "main"
    (main / "bin").mkdir(parents=True)
    wrapper = main / "bin" / "claude-follow"
    wrapper.write_text("#!/bin/sh\nexec true\n")
    wrapper.chmod(0o755)
    _git(main, "init", "-q", "-b", "main", ".")
    _git(main, "add", "-A")
    _git(main, "commit", "-qm", "init")
    worktree = tmp_path / "wt"
    _git(main, "worktree", "add", "-q", str(worktree), "HEAD")
    return main.resolve(), worktree.resolve()


# --------------------------------------------------------------------------
# Defect A — the binding must never embed a path inside a throwaway worktree.
# --------------------------------------------------------------------------


def test_executable_resolves_out_of_a_worktree_into_the_main_checkout(
    repo_with_worktree: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    """THE regression test. Resolution is per-invocation; the binding it feeds
    is server-global and permanent. Running `start` from a worktree must still
    bind the main checkout's wrapper, which outlives the worktree."""
    main, worktree = repo_with_worktree
    monkeypatch.delenv("CLAUDE_PLUGIN_ROOT", raising=False)
    monkeypatch.setattr(keybindings, "_bundled_wrapper", lambda: worktree / "bin" / "claude-follow")

    result = Path(keybindings._claude_follow_executable())

    assert result == main / "bin" / "claude-follow"
    assert worktree not in result.parents  # deleting the worktree cannot kill the keys


def test_executable_leaves_a_main_checkout_path_alone(
    repo_with_worktree: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Guard against over-redirecting. From `<root>/bin`, git answers
    --git-common-dir with a RELATIVE "../.git" while --git-dir comes back
    absolute; comparing those two raw strings calls every ordinary checkout a
    worktree and sends the binding somewhere else entirely."""
    main, _ = repo_with_worktree
    monkeypatch.delenv("CLAUDE_PLUGIN_ROOT", raising=False)
    monkeypatch.setattr(keybindings, "_bundled_wrapper", lambda: main / "bin" / "claude-follow")

    # Assert the CLASSIFICATION, not only the path that comes out. Measured:
    # dropping the re-anchor misclassifies this main checkout as a worktree
    # and the returned path is still right, because the bogus candidate fails
    # the exists() check and falls back — so the returned path alone cannot
    # tell the two apart, and a test asserting only that is vacuous here.
    dirs = keybindings._git_dirs(main / "bin")
    assert dirs is not None
    common_dir, git_dir, _toplevel = dirs
    assert common_dir == git_dir

    assert Path(keybindings._claude_follow_executable()) == main / "bin" / "claude-follow"


def test_executable_keeps_a_worktree_path_when_the_main_checkout_lacks_the_wrapper(
    repo_with_worktree: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Redirect only to something that is actually there. A wrapper the main
    checkout does not have (deleted, or never tracked) would bind a path that
    is dead on arrival — worse than the ephemeral one."""
    main, worktree = repo_with_worktree
    (main / "bin" / "claude-follow").unlink()
    monkeypatch.delenv("CLAUDE_PLUGIN_ROOT", raising=False)
    monkeypatch.setattr(keybindings, "_bundled_wrapper", lambda: worktree / "bin" / "claude-follow")

    assert Path(keybindings._claude_follow_executable()) == worktree / "bin" / "claude-follow"


def test_executable_is_unchanged_outside_any_git_repository(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A released plugin install is not a git checkout at all; `git rev-parse`
    exits non-zero there and resolution must fall straight through."""
    plain = tmp_path / "plain" / "bin"
    plain.mkdir(parents=True)
    (plain / "claude-follow").touch()
    monkeypatch.delenv("CLAUDE_PLUGIN_ROOT", raising=False)
    monkeypatch.setattr(keybindings, "_bundled_wrapper", lambda: plain / "claude-follow")

    assert Path(keybindings._claude_follow_executable()) == plain / "claude-follow"


def test_executable_is_unchanged_when_git_is_not_installed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No git on the machine must degrade to the old behavior, not to a
    traceback out of `claude-follow start`."""
    wrapper_dir = tmp_path / "somewhere" / "bin"
    wrapper_dir.mkdir(parents=True)
    (wrapper_dir / "claude-follow").touch()
    monkeypatch.delenv("CLAUDE_PLUGIN_ROOT", raising=False)
    monkeypatch.setattr(keybindings, "_bundled_wrapper", lambda: wrapper_dir / "claude-follow")

    with patch("vim_ai_follower.keybindings.subprocess.run", side_effect=FileNotFoundError("git")):
        assert Path(keybindings._claude_follow_executable()) == wrapper_dir / "claude-follow"


def test_plugin_root_still_wins_over_the_worktree_redirect(
    repo_with_worktree: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    """CLAUDE_PLUGIN_ROOT stays first in the resolution order: a plugin install
    is already durable, and it is not this package's on-disk location."""
    _, worktree = repo_with_worktree
    monkeypatch.setenv("CLAUDE_PLUGIN_ROOT", "/opt/plugin")
    monkeypatch.setattr(keybindings, "_bundled_wrapper", lambda: worktree / "bin" / "claude-follow")

    assert keybindings._claude_follow_executable() == "/opt/plugin/bin/claude-follow"


# --------------------------------------------------------------------------
# Defect B — `start` must be a real repair for dead keys.
# --------------------------------------------------------------------------


def test_start_refreshes_the_keybindings_when_a_follower_is_already_running(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The keys are server-global and the follower state is per-window, so the
    keys can be dead while this window's state is perfectly healthy. `start` is
    what a user retries then; returning early before register() made that retry
    change nothing at all."""
    with patch("vim_ai_follower.tmux.subprocess.run", side_effect=_mock_tmux_run()):
        assert commands.cmd_start({"TMUX_PANE": "%1"}) == 0
        capsys.readouterr()
        with patch("vim_ai_follower.commands.keybindings.claim", wraps=keybindings.claim) as claim_:
            assert commands.cmd_start({"TMUX_PANE": "%1"}) == 0

    claim_.assert_called_once_with(force=False, repair=True)
    out = capsys.readouterr().out
    assert "already running" in out
    assert "keybindings refreshed" in out  # silent repair is indistinguishable from a no-op


def test_second_start_keeps_the_users_original_bindings_intact() -> None:
    """The guard that matters more than the fix: register() records the
    PREVIOUS bindings only on the first call. Re-registering from the
    already-running path must not overwrite the user's real originals with our
    own still-bound keys — that would turn a dead-key annoyance into losing
    their config on the next `stop`."""
    original = "bind-key -T prefix P send-keys C-a"
    ours = 'bind-key -T prefix P run-shell -b "TMUX_PANE=#{pane_id} /x/claude-follow pause"'
    listed = [original]
    base = _mock_tmux_run()

    def fake_run(cmd: list[str], **kwargs: object) -> MagicMock:
        if cmd[:2] == ["tmux", "list-keys"]:
            return MagicMock(returncode=0, stdout="".join(line + "\n" for line in listed))
        return base(cmd, **kwargs)

    # One patch, not two: keybindings and tmux share the very same subprocess
    # module object, so patching either name patches both.
    with patch("vim_ai_follower.tmux.subprocess.run", side_effect=fake_run):
        assert commands.cmd_start({"TMUX_PANE": "%1"}) == 0
        after_first = keybindings._saved_bindings_path().read_text()
        assert original in after_first
        listed[:] = [ours]  # the server now holds OUR bindings, not the user's
        assert commands.cmd_start({"TMUX_PANE": "%1"}) == 0

    assert keybindings._saved_bindings_path().read_text() == after_first


def test_start_standalone_already_running_never_touches_keybindings(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Outside tmux there is no server to bind on — and unregistering later
    would aim at whatever tmux server happens to be running on the machine."""
    standalone = session.Session(window_id="term-x", origin=None, in_tmux=False)
    state.FollowerState.set("term-x", "nvim", "/tmp/standalone.sock", origin="")

    with (
        patch("vim_ai_follower.commands.resolve_session", return_value=standalone),
        # FollowerState.get() liveness-probes the socket; without a live attach
        # the state reads back as dead and cmd_start never reaches the
        # already-running branch this test is about.
        patch("pynvim.attach", return_value=MagicMock()),
        patch("vim_ai_follower.commands.keybindings.register") as register,
    ):
        assert commands.cmd_start({}) == 0

    register.assert_not_called()
    assert "keybindings refreshed" not in capsys.readouterr().out


# --------------------------------------------------------------------------
# The assertion that would have caught the whole class, against a real server.
# --------------------------------------------------------------------------


@pytest.mark.integration
def test_bound_keys_point_at_an_executable_that_really_exists(
    tmux_session: str, wait_until: Callable[..., bool]
) -> None:
    """Bind on a real (throwaway, private-socket) server, read the keys back
    out of tmux, and put the embedded path past the file system. Nothing
    cheaper notices a dead binding: at keypress time run-shell fails with exit
    127 into /dev/null, so the key is simply inert and silent."""
    del wait_until  # keeps the fixture ordering explicit for the tmux server
    keybindings.register()
    try:
        listed = subprocess.run(
            ["tmux", "list-keys", "-T", "prefix"],
            capture_output=True,
            text=True,
            check=True,
        ).stdout
    finally:
        keybindings.unregister()

    bound = [line for line in listed.splitlines() if "claude-follow" in line]
    assert len(bound) == len(keybindings._KEYBINDINGS)
    for line in bound:
        shell_command = shlex.split(line)[-1]
        # "TMUX_PANE=#{pane_id} <executable> <subcommand> >/dev/null 2>&1"
        executable = Path(shlex.split(shell_command)[1])
        assert executable.is_file(), f"{executable} is gone — this key exits 127 on press"
        assert os.access(executable, os.X_OK), f"{executable} is not executable"
