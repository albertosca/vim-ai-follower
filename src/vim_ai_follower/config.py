"""User configuration loading and validation, the speed/pace tables, and the
code-file policy check."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

CONFIG_PATH = Path.home() / ".config" / "claude-vim-follower" / "config.json"

DEFAULT_ON_FAILURE = "silent"
DEFAULT_SPEED = "rapido"
DEFAULT_OPEN_POLICY = "manual"
DEFAULT_ADOPT_EXISTING = False
DEFAULT_MAX_TABS = 5
DEFAULT_BACKEND = "tmux"
DEFAULT_NVIM_WINDOW = "auto"

_VALID_ON_FAILURE = ("silent", "reopen")
_VALID_OPEN_POLICY = ("always", "code", "manual")
_VALID_BACKEND = ("tmux", "nvim")
_VALID_NVIM_WINDOW = ("auto", "always", "never")

SPEED_PACE_SECONDS: dict[str, float] = {
    "instant": 0.0,
    "muito_rapido": 0.01,
    "rapido": 0.03,
    "normal": 0.08,
    "lento": 0.15,
}

SPEED_ORDER: tuple[str, ...] = ("lento", "normal", "rapido", "muito_rapido", "instant")

CODE_EXTENSIONS: frozenset[str] = frozenset(
    {
        "c",
        "cc",
        "cpp",
        "css",
        "ex",
        "exs",
        "go",
        "h",
        "hpp",
        "html",
        "java",
        "js",
        "jsx",
        "lua",
        "mjs",
        "php",
        "py",
        "rb",
        "rs",
        "scss",
        "sh",
        "sql",
        "swift",
        "ts",
        "tsx",
        "vim",
        "vue",
        "zsh",
    }
)


@dataclass(frozen=True)
class Config:
    on_failure: str
    speed: str
    open_policy: str
    adopt_existing: bool
    max_tabs: int
    backend: str
    nvim_window: str


def load(config_path: Path | None = None) -> Config:
    path = config_path if config_path is not None else CONFIG_PATH
    data: dict[str, Any] = json.loads(path.read_text()) if path.exists() else {}
    on_failure = data.get("on_failure", DEFAULT_ON_FAILURE)
    speed = data.get("speed", DEFAULT_SPEED)
    open_policy = data.get("open_policy", DEFAULT_OPEN_POLICY)
    adopt_existing = data.get("adopt_existing", DEFAULT_ADOPT_EXISTING)
    max_tabs = data.get("max_tabs", DEFAULT_MAX_TABS)
    backend = data.get("backend", DEFAULT_BACKEND)
    nvim_window = data.get("nvim_window", DEFAULT_NVIM_WINDOW)
    if on_failure not in _VALID_ON_FAILURE:
        on_failure = DEFAULT_ON_FAILURE
    if speed not in SPEED_PACE_SECONDS:
        speed = DEFAULT_SPEED
    if open_policy not in _VALID_OPEN_POLICY:
        open_policy = DEFAULT_OPEN_POLICY
    if not isinstance(adopt_existing, bool):
        adopt_existing = DEFAULT_ADOPT_EXISTING
    if not isinstance(max_tabs, int) or isinstance(max_tabs, bool) or max_tabs < 1:
        max_tabs = DEFAULT_MAX_TABS
    if backend not in _VALID_BACKEND:
        backend = DEFAULT_BACKEND
    if nvim_window not in _VALID_NVIM_WINDOW:
        nvim_window = DEFAULT_NVIM_WINDOW
    return Config(on_failure, speed, open_policy, adopt_existing, max_tabs, backend, nvim_window)


def next_speed(speed: str, direction: Literal["up", "down"]) -> str:
    """One notch along SPEED_ORDER, saturating at both ends — wrapping
    around surprised in live use ("lento" jumping to "instant")."""
    if speed not in SPEED_ORDER:
        return DEFAULT_SPEED
    step = 1 if direction == "up" else -1
    index = max(0, min(len(SPEED_ORDER) - 1, SPEED_ORDER.index(speed) + step))
    return SPEED_ORDER[index]


def is_code_file(file_path: str) -> bool:
    suffix = Path(file_path).suffix
    return bool(suffix) and suffix[1:].lower() in CODE_EXTENSIONS


def pace_seconds_for(speed: str) -> float:
    return SPEED_PACE_SECONDS.get(speed, SPEED_PACE_SECONDS[DEFAULT_SPEED])
