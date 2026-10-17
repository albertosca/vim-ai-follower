import json
import tomllib
from pathlib import Path

import pytest

import vim_ai_follower


def test_package_is_importable() -> None:
    assert vim_ai_follower is not None


def test_core_has_no_runtime_deps_and_pynvim_is_an_extra() -> None:
    data = tomllib.loads(Path("pyproject.toml").read_text())
    assert data["project"].get("dependencies", []) == []
    assert "pynvim" in " ".join(data["project"]["optional-dependencies"]["nvim"])


# Seconds (Claude Code's per-hook `timeout`). The non-user hooks can only take
# seconds now that every RPC, connect, landing and probe is bounded; the
# Edit/Write post hook keeps 600 s for the waits that ARE the feature (a pause,
# the hand-off waiting for `:w`). A hook cut at its timeout leaves the crash
# fallback to finish the animation.
_HOOK_TIMEOUTS = {
    ("PreToolUse", "Edit|MultiEdit|Write"): 30,
    ("PostToolUse", "Edit|MultiEdit|Write"): 600,
    ("PostToolUse", "Read"): 60,
    ("PostToolUseFailure", "Edit|MultiEdit|Write"): 30,
    ("PermissionDenied", "Edit|MultiEdit|Write"): 30,
}


def _hook_timeouts(hooks_config: dict[str, object]) -> dict[tuple[str, str], list[object]]:
    found: dict[tuple[str, str], list[object]] = {}
    for event, entries in hooks_config["hooks"].items():  # type: ignore[attr-defined]
        for entry in entries:
            found[(event, entry["matcher"])] = [hook.get("timeout") for hook in entry["hooks"]]
    return found


def test_every_plugin_hook_has_its_explicit_timeout() -> None:
    hooks_config = json.loads(Path("hooks/hooks.json").read_text())
    assert _hook_timeouts(hooks_config) == {
        key: [seconds] for key, seconds in _HOOK_TIMEOUTS.items()
    }


@pytest.mark.parametrize("language", ["en", "pt"])
def test_the_manual_install_snippet_carries_the_same_timeouts(language: str) -> None:
    page = Path(f"guide/{language}/getting-started/install.md").read_text()
    snippet = page.split("```json\n", 1)[1].split("```", 1)[0]
    assert _hook_timeouts(json.loads(snippet)) == {
        key: [seconds] for key, seconds in _HOOK_TIMEOUTS.items()
    }
