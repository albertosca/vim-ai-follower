"""Register and unregister the tmux prefix keybindings that drive follower control commands."""

from __future__ import annotations

import json
import os
import shlex
import shutil
import subprocess
import sys
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


def _claude_follow_executable() -> str:
    """Absolute path to the claude-follow entry point. tmux run-shell commands
    execute with the tmux SERVER's environment, whose PATH never includes this
    project's virtualenv nor a plugin's PATH — a bare name exits 127 there, so
    the keybinding must embed the resolved path (never a bare name).

    Resolution order: the plugin install (CLAUDE_PLUGIN_ROOT) when set, else the
    bundled wrapper resolved from this package's own location (covers a plugin
    started outside a hook, and an editable clone), else this venv's installed
    script, else PATH."""
    plugin_root = os.environ.get("CLAUDE_PLUGIN_ROOT")
    if plugin_root:
        return str(Path(plugin_root) / "bin" / "claude-follow")
    bundled = _bundled_wrapper()
    if bundled.exists():
        return str(bundled)
    candidate = Path(sys.executable).parent / "claude-follow"
    if candidate.exists():
        return str(candidate)
    located = shutil.which("claude-follow")
    return located if located is not None else "claude-follow"


def _saved_bindings_path() -> Path:
    return cache.CACHE_DIR / "saved-keybindings.json"


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
    executable = shlex.quote(_claude_follow_executable())
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
