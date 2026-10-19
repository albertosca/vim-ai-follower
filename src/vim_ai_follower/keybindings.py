"""Register and unregister the tmux prefix keybindings that drive follower control commands."""

from __future__ import annotations

import contextlib
import json
import logging
import os
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

from vim_ai_follower import cache

logger = logging.getLogger("vim_ai_follower")

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


def _may_be_in_linked_worktree(directory: Path) -> bool:
    """False when the nearest `.git` above `directory` is a DIRECTORY (a main
    checkout or plain clone) or there is none; True for a `.git` FILE, which
    is what a linked worktree (or a submodule) has — only then is git asked.
    Every hook post is a new process that heals the keys, so this stat is what
    keeps the dev checkout from spawning `git rev-parse` on every edit."""
    for parent in (directory, *directory.parents):
        marker = parent / ".git"
        if marker.is_dir():
            return False
        if marker.exists():
            return True
    return False


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
    parent."""
    if not _may_be_in_linked_worktree(wrapper.parent):
        return wrapper  # main checkout, plain clone, or no repo: already durable
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


def _write_owner(owner: Owner) -> None:
    """Replace the record atomically. Another installation's hook can read it
    at any moment, and a plain write_text truncates first: a reader landing
    mid-write saw no owner and took keys from a live installation. The temp
    file lives beside the target (os.replace is only atomic within one
    filesystem) and is removed if the rename fails."""
    path = _owner_path()
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        tmp.write_text(
            json.dumps({"executable": owner.executable, "installation": owner.installation})
        )
        tmp.replace(path)
    except OSError:
        tmp.unlink(missing_ok=True)
        raise


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


def _save_once(saved_path: Path, previous: dict[str, str | None]) -> None:
    """Write the user's bindings unless a record already exists, in one
    atomic step. Two registrations at once both found no record, and the
    later one, which listed OUR keys already bound by the first, replaced the
    user's bindings with ours (final review M6). The record is written to a
    temporary file and LINKED into place: a link onto an existing name fails,
    so the first record always wins and nobody reads a half-written one."""
    saved_path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=f".{saved_path.name}.", dir=saved_path.parent)
    try:
        with os.fdopen(descriptor, "w") as record:
            record.write(json.dumps(previous))
        with contextlib.suppress(FileExistsError):  # the first record wins
            os.link(temporary, saved_path)
    finally:
        Path(temporary).unlink(missing_ok=True)


def _read_saved(saved_path: Path) -> dict[str, str]:
    """The saved record's string entries; nothing for a missing, unreadable
    or damaged one. A list, `null`, a number where a line belongs or bytes
    that are not UTF-8 raised out of `unregister`, and the last `stop`
    exited non-zero with the keys still bound (final review M5)."""
    try:
        saved = json.loads(saved_path.read_text())
    except (OSError, ValueError):  # UnicodeDecodeError and JSONDecodeError are ValueErrors
        return {}
    if not isinstance(saved, dict):
        return {}
    return {
        key: previous
        for key, previous in saved.items()
        if isinstance(key, str) and isinstance(previous, str)
    }


def register() -> None:
    saved_path = _saved_bindings_path()
    if not saved_path.exists():
        # Only the FIRST registration records "previous": re-registering
        # after a crash would otherwise save our own still-bound key as the
        # user's original binding.
        _save_once(saved_path, {key: _existing_binding(key) for key, _ in _KEYBINDINGS})
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
    _write_owner(owner)


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


# A `\;` token list-keys prints is a chain separator, or a literal `;`
# argument (`send-keys '\;'` prints `send-keys \;`): it can only be the
# separator when a command follows it, since a command list holds no empty
# command and no command is named `;` or `}`.
_LITERAL_SEMICOLON_KEY = re.compile(r"(?<=\s)\\;(?=\s+(?:\\;|\})(?=\s|$)|\s*$)")


def _same_binding(listed: str, saved: str) -> bool:
    """Whether two list-keys lines name the same binding. Column padding
    differs with the other keys bound, and a brace block written over
    several lines lists as `a ;; b` but re-lists as `a ; b` once sourced
    (measured on tmux 3.8, 2026-10-17)."""

    def tokens(line: str) -> list[str]:
        return [";" if token == ";;" else token for token in line.split()]

    return tokens(listed) == tokens(saved)


# The characters tmux's config lexer skips between words.
_TMUX_BLANKS = " \t"


def _runs_only_its_binding(line: str) -> bool:
    """Whether sourcing `line` can run nothing but its own bind-key (T24b
    review m1: a tampered saved-keybindings.json ran a second command at
    `stop`). list-keys prints one binding per line, every brace that opens
    or closes a block as a token of its own (a literal one quoted or `\\}`)
    and a separator outside a block as `\\;`. So the line is refused when it
    holds a CR or LF, an unquoted unescaped brace inside a word (tmux opens
    a block at `{x` but not at `a{`, and closes one at `a}`), unbalanced
    blocks or quotes, or an unquoted unescaped `;` outside every block,
    which tmux runs as the next command (measured on tmux 3.8, 2026-10-18).
    Inside a block a `;` only separates the binding's own commands. A `#`
    after a space comments out the rest of the line (list-keys prints a bare
    one as `\\#`): what it hides runs nothing, and a `}` it hides that would
    balance an attached `x{` is already refused by the brace rule. A brace
    stands alone only between a space or a tab, tmux's only blanks:
    `isspace()` also accepted NBSP, `\\v`, `\\f`, `\\x1c`, U+0085, U+2028
    and U+3000, around which tmux reads `a<NBSP>{` as one word (T24b
    re-review m-A)."""
    if "\n" in line or "\r" in line:
        return False
    depth = 0
    quote = ""
    index = 0
    while index < len(line):
        character = line[index]
        if quote == "'":
            quote = "" if character == "'" else quote
        elif quote == '"' and character == "\\":
            index += 1
        elif quote == '"':
            quote = "" if character == '"' else quote
        elif character == "\\":
            index += 1
        elif character in "'\"":
            quote = character
        elif character in "{}":
            before = line[index - 1] if index else " "
            after = line[index + 1] if index + 1 < len(line) else " "
            if not (before in _TMUX_BLANKS and after in _TMUX_BLANKS):
                return False
            depth += 1 if character == "{" else -1
            if depth < 0:
                return False
        elif character == ";" and depth == 0:
            return False
        index += 1
    return quote == "" and depth == 0


def _binds_its_own_key(key: str, previous: str) -> bool:
    """Whether `previous` binds prefix `key`, the way list-keys prints it:
    `bind-key [-r] -T prefix <key> …`. The one-binding check alone let a
    tampered record saved under P rebind another key (`bind-key -T root C-c
    …`) at `stop` (final review M8). Our keys need no quoting, so a split on
    tmux's blanks is exact for them."""
    tokens = re.split(r"[ \t]+", previous.strip())
    if tokens[:1] != ["bind-key"]:
        return False
    options = tokens[1:]
    if options[:1] == ["-r"]:
        options = options[1:]
    return options[:3] == ["-T", "prefix", key] and len(options) > 3


def _restore_binding(key: str, previous: str) -> bool:
    """Re-bind the user's own `previous` (a line list-keys printed) and say
    whether tmux now lists exactly it. The line goes to `source-file`
    verbatim, since list-keys prints what the config parser reads back:
    brace blocks, `"\\$HOME"` and chains included, which an argv rebuilt by
    shlex lost (T24a review m4). Only a literal `;` key needs a quote."""
    if not _binds_its_own_key(key, previous) or not _runs_only_its_binding(previous):
        return False
    descriptor, config_path = tempfile.mkstemp(prefix="vaf-binding-", suffix=".conf")
    try:
        with os.fdopen(descriptor, "w") as config_file:
            config_file.write(_LITERAL_SEMICOLON_KEY.sub("'\\\\;'", previous) + "\n")
        result = subprocess.run(
            ["tmux", "source-file", config_path], capture_output=True, text=True, check=False
        )
    finally:
        Path(config_path).unlink(missing_ok=True)
    if result.returncode != 0:
        # Ask nothing more: on a dead server `list-keys` STARTS one, which
        # loads the user's real tmux.conf (final review M7).
        return False
    listed = _existing_binding(key)
    return listed is not None and _same_binding(listed, previous)


def unregister() -> None:
    # check=False everywhere: stop after a crash that skipped registration
    # must still clean up without erroring. Keybindings are SERVER-global
    # while follower state is per-window, so only the LAST stop calls this
    # (cmd_stop scans for other live followers first).
    saved_path = _saved_bindings_path()
    saved = _read_saved(saved_path)
    for key, _ in _KEYBINDINGS:
        previous = saved.get(key)
        if previous and "claude-follow" not in previous:
            if _restore_binding(key, previous):
                continue
            # Never leave ours bound in its place: a key that runs a stopped
            # follower's command is worse than an unbound one.
            logger.warning(
                "stop: could not restore your tmux binding %r; prefix %s is unbound", previous, key
            )
        subprocess.run(["tmux", "unbind-key", "-T", "prefix", key], check=False)
    saved_path.unlink(missing_ok=True)
    _owner_path().unlink(missing_ok=True)
