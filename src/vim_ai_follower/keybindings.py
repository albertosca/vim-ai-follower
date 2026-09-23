"""Register and unregister the tmux prefix keybindings that drive follower control commands."""

from __future__ import annotations

import functools
import json
import os
import shlex
import shutil
import subprocess
import sys
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

from vim_ai_follower import cache

_KEYBINDINGS: tuple[tuple[str, str], ...] = (
    ("P", "pause"),
    ("S", "interrupt"),
    ("+", "speed-up"),
    ("_", "speed-down"),
    ("F", "toggle"),
)


def _bundled_wrapper() -> Path:
    """The bundled bin/claude-follow wrapper, resolved relative to THIS
    package's on-disk location (src/vim_ai_follower/keybindings.py ->
    <root>/bin/claude-follow). Works for both the plugin and an editable clone
    without depending on any environment variable."""
    return Path(__file__).resolve().parents[2] / "bin" / "claude-follow"


def _git_dirs(directory: Path) -> tuple[Path, Path, Path] | None:
    """(common git dir, this tree's own git dir, working-tree top level) for
    `directory`, all absolute. None when `directory` is not inside a git
    working tree, or git is not installed.

    Every answer is re-anchored on `directory` before resolving, because git
    prints these RELATIVE to its own cwd whenever that is shorter: asked from
    a repo's own bin/, --git-common-dir comes back as "../.git" while
    --git-dir comes back absolute. Comparing the two raw strings would label
    every ordinary checkout a worktree."""
    try:
        result = subprocess.run(
            [
                "git",
                "-C",
                str(directory),
                "rev-parse",
                "--git-common-dir",
                "--git-dir",
                "--show-toplevel",
            ],
            capture_output=True,
            text=True,
            check=False,
        )
    except OSError:
        return None  # no git on this machine: degrade, never traceback
    lines = result.stdout.splitlines()
    if result.returncode != 0 or len(lines) != 3:
        return None
    paths = [Path(directory, line).resolve() for line in lines]
    return paths[0], paths[1], paths[2]


@functools.lru_cache(maxsize=8)
def _durable_wrapper(wrapper: Path) -> Path:
    """`wrapper`, or its twin in the MAIN checkout when `wrapper` lives inside
    a linked git worktree.

    Resolution happens once, per invocation; the tmux binding it feeds is
    SERVER-global and lasts as long as the tmux server. A path inside a
    throwaway worktree therefore outlives the worktree it names. Measured
    2026-09-22: a `start` run from a tool-created worktree repointed the
    prefix keys of every window on the real server, and deleting that worktree
    left all five keys exiting 127 — silently, since run-shell discards both
    streams. A linked worktree is the case where the common git dir differs
    from this tree's git dir, and the main checkout is the common dir's
    parent.

    Cached per path: every `hook post` heals the keys through here, and on a
    dev checkout (no CLAUDE_PLUGIN_ROOT) each call spawned `git rev-parse`.
    Whether a path sits in a linked worktree does not change mid-process."""
    dirs = _git_dirs(wrapper.parent)
    if dirs is None:
        return wrapper
    common_dir, git_dir, toplevel = dirs
    if common_dir == git_dir:
        return wrapper  # the main checkout (or a plain clone): already durable
    candidate = Path(common_dir.parent, os.path.relpath(wrapper, toplevel))
    if not candidate.exists():
        # Nothing there to bind — an absent path is deader than an ephemeral
        # one, which at least works until the worktree goes away.
        return wrapper
    return candidate


def _claude_follow_executable() -> str:
    """Absolute path to the claude-follow entry point. tmux run-shell commands
    execute with the tmux SERVER's environment, whose PATH never includes this
    project's virtualenv nor a plugin's PATH — a bare name exits 127 there, so
    the keybinding must embed the resolved path (never a bare name).

    Resolution order: the plugin install (CLAUDE_PLUGIN_ROOT) when set, else the
    bundled wrapper resolved from this package's own location (covers a plugin
    started outside a hook, and an editable clone) — redirected to the main
    checkout when that location is a linked worktree, since the binding must
    outlive it — else this venv's installed script, else PATH."""
    plugin_root = os.environ.get("CLAUDE_PLUGIN_ROOT")
    if plugin_root:
        return str(Path(plugin_root) / "bin" / "claude-follow")
    bundled = _bundled_wrapper()
    if bundled.exists():
        return str(_durable_wrapper(bundled))
    candidate = Path(sys.executable).parent / "claude-follow"
    if candidate.exists():
        return str(candidate)
    located = shutil.which("claude-follow")
    return located if located is not None else "claude-follow"


def _saved_bindings_path() -> Path:
    return cache.CACHE_DIR / "saved-keybindings.json"


@dataclass(frozen=True)
class Owner:
    """Who the server-global prefix keys currently belong to: the executable
    path embedded in the bindings, and the installation it came from."""

    executable: str
    installation: str


def _owner_path() -> Path:
    return cache.CACHE_DIR / "keybindings-owner.json"


def _installation_id(executable: str) -> str:
    """The installation `executable` belongs to. Running as a plugin, every
    version of it is ONE installation: the id is CLAUDE_PLUGIN_ROOT's parent,
    because the root itself is version-stamped (installPath ends in
    `/<version>`) and `plugin update` replaces it. That layout is observed in
    installed_plugins.json on 2026-09-22, not a documented contract; if it
    changes, each path becomes its own installation and a dead old path is
    still taken, only a kept-alive old one is warned about instead of
    refreshed. Anything else (dev checkout wrapper, venv script) is
    identified by its own path.

    The env is not enough on its own: `/start` runs `claude-follow start` in
    the Bash tool, which has NO CLAUDE_PLUGIN_ROOT (measured 2026-09-23) and
    reaches the bundled wrapper by its versioned path, while hooks do have
    it. Both must land on the same id, so the plugin-cache shape
    `.../plugins/cache/<marketplace>/<plugin>/<version>/bin/claude-follow` is
    recognized from the path itself. The version segment is not assumed to
    look like a version: some plugin caches use commit hashes there."""
    plugin_root = os.environ.get("CLAUDE_PLUGIN_ROOT")
    if plugin_root and Path(executable).is_relative_to(plugin_root):
        return str(Path(plugin_root).parent)
    parts = Path(executable).parts
    if len(parts) >= 8 and parts[-7:-5] == ("plugins", "cache") and parts[-2] == "bin":
        return str(Path(*parts[:-3]))
    return executable


def current_owner() -> Owner:
    executable = _claude_follow_executable()
    return Owner(executable, _installation_id(executable))


def read_owner() -> Owner | None:
    """The recorded owner, or None when there is none or it is unreadable —
    a truncated or foreign-schema record must degrade to "no owner", never
    raise out of a hook."""
    try:
        data = json.loads(_owner_path().read_text())
        return Owner(str(data["executable"]), str(data["installation"]))
    except (OSError, ValueError, KeyError, TypeError):
        return None


def _existing_binding(key: str) -> str | None:
    # List the whole prefix table and filter ourselves: tmux 3.7b returns
    # empty output for `list-keys -T prefix <key>` even when the binding
    # exists, so per-key filtering can't be trusted across versions.
    result = subprocess.run(
        ["tmux", "list-keys", "-T", "prefix"],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        return None
    for line in result.stdout.splitlines():
        try:
            tokens = shlex.split(line)
        except ValueError:
            continue
        if "-T" in tokens:
            table_index = tokens.index("-T")
            if tokens[table_index + 1 : table_index + 3] == ["prefix", key]:
                return line.strip()
    return None


def register() -> None:
    saved_path = _saved_bindings_path()
    if not saved_path.exists():
        # Only the FIRST registration records "previous": re-registering
        # after a crash would otherwise save our own still-bound key as the
        # user's original binding.
        saved_path.parent.mkdir(parents=True, exist_ok=True)
        saved_path.write_text(json.dumps({key: _existing_binding(key) for key, _ in _KEYBINDINGS}))
    owner = current_owner()
    executable = shlex.quote(owner.executable)
    for key, subcommand in _KEYBINDINGS:
        subprocess.run(
            [
                "tmux",
                "bind-key",
                "-T",
                "prefix",
                key,
                "run-shell",
                # -b: run in background — a plain run-shell blocks ALL tmux
                # input until the command exits, and a resume replay lasts
                # tens of seconds. Backgrounding also makes the toggle
                # honest: a second press DURING the replay delivers a pause
                # signal the replay actually consumes.
                "-b",
                # tmux pre-expands #{pane_id} against the pane that triggered
                # the binding, so assign it directly. Nesting a
                # $(tmux display-message -p ...) around the pre-expanded
                # "%N" would format-expand it AGAIN, eating the "%" and
                # producing an invalid pane target.
                # >/dev/null: any stdout from run-shell throws the active
                # pane into tmux's view-mode overlay until dismissed.
                f"TMUX_PANE=#{{pane_id}} {executable} {subcommand} >/dev/null 2>&1",
            ],
            check=True,
        )
    _owner_path().write_text(
        json.dumps({"executable": owner.executable, "installation": owner.installation})
    )


class ClaimOutcome(StrEnum):
    TAKEN = "taken"
    UNCHANGED = "unchanged"
    REFRESHED = "refreshed"
    TAKEN_FROM_DEAD = "taken_from_dead"
    TAKEN_BY_FORCE = "taken_by_force"
    TAKEN_FROM_STALE = "taken_from_stale"
    KEPT_FOREIGN = "kept_foreign"


@dataclass(frozen=True)
class Claim:
    outcome: ClaimOutcome
    previous: Owner | None
    current: Owner


def _is_alive(owner: Owner) -> bool:
    # A bare name ("claude-follow", the last-resort fallback) is relative and
    # reads as dead: it never ran from tmux's PATH anyway.
    return Path(owner.executable).is_absolute() and os.access(owner.executable, os.X_OK)


def _holds_keys(owner: Owner) -> bool:
    """True when the server's prefix keys actually run `owner`'s executable.
    The record outlives the bindings — a tmux server restart drops every key
    while the cache (and the other installation) survive — and deferring to
    an owner that holds nothing would leave the user with no keys at all.
    Only asked on the rare live-foreign path, so the per-hook common case
    still never lists keys."""
    binding = _existing_binding(_KEYBINDINGS[0][0])
    return binding is not None and owner.executable in binding


def claim(force: bool = False, repair: bool = False) -> Claim:
    """Point the prefix keys at this installation, or deliberately don't.

    Same installation re-points (a plugin update moved it); a dead foreign
    owner is taken; a live foreign one keeps the keys unless `force`. The
    per-hook heal calls this with repair=False, so the common case binds
    nothing; `start` and auto-open pass repair=True because a restarted tmux
    server drops the bindings while the cache still names us as owner, and
    `start` must stay a real repair for that."""
    me = current_owner()
    previous = read_owner()
    if previous is None:
        outcome = ClaimOutcome.TAKEN
    elif previous.installation == me.installation:
        same = previous.executable == me.executable
        outcome = ClaimOutcome.UNCHANGED if same else ClaimOutcome.REFRESHED
    elif not _is_alive(previous):
        outcome = ClaimOutcome.TAKEN_FROM_DEAD
    elif force:
        outcome = ClaimOutcome.TAKEN_BY_FORCE
    elif not _holds_keys(previous):
        outcome = ClaimOutcome.TAKEN_FROM_STALE
    else:
        outcome = ClaimOutcome.KEPT_FOREIGN
    if outcome is not ClaimOutcome.KEPT_FOREIGN and (
        outcome is not ClaimOutcome.UNCHANGED or repair
    ):
        register()
    return Claim(outcome, previous, me)


def describe(result: Claim) -> str | None:
    """One line worth saying about `result`, or None when nothing changed
    hands (a first take is today's silent start path)."""
    prev = result.previous
    if prev is None or result.outcome in (ClaimOutcome.TAKEN, ClaimOutcome.UNCHANGED):
        return None
    if result.outcome is ClaimOutcome.REFRESHED:
        return f"keybindings re-pointed from {prev.executable} to {result.current.executable}"
    if result.outcome is ClaimOutcome.TAKEN_FROM_DEAD:
        return f"keybindings moved here from {prev.executable}, which no longer exists"
    if result.outcome is ClaimOutcome.TAKEN_FROM_STALE:
        return (
            f"keybindings moved here: the record named {prev.executable},"
            " but no key was bound to it (tmux restarted?)"
        )
    if result.outcome is ClaimOutcome.TAKEN_BY_FORCE:
        return f"keybindings taken from {prev.installation} ({prev.executable})"
    return (
        f"keybindings belong to {prev.installation} ({prev.executable}) — left there;"
        " rerun `claude-follow start --take-keys` to move them here"
    )


def unregister() -> None:
    # check=False everywhere: stop after a crash that skipped registration
    # must still clean up without erroring. Keybindings are SERVER-global
    # while follower state is per-window, so only the LAST stop calls this
    # (cmd_stop scans for other live followers first).
    saved_path = _saved_bindings_path()
    try:
        saved: dict[str, str | None] = json.loads(saved_path.read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        saved = {}
    for key, _ in _KEYBINDINGS:
        previous = saved.get(key)
        if previous and "claude-follow" not in previous:
            subprocess.run(["tmux", *shlex.split(previous)], check=False)
        else:
            subprocess.run(["tmux", "unbind-key", "-T", "prefix", key], check=False)
    saved_path.unlink(missing_ok=True)
    _owner_path().unlink(missing_ok=True)
