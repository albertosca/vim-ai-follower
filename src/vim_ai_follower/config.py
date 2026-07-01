from __future__ import annotations

import json
from pathlib import Path

CONFIG_PATH = Path.home() / ".config" / "claude-vim-follower" / "config.json"

DEFAULT_ON_FAILURE = "silent"
DEFAULT_SPEED = "rapido"

_VALID_ON_FAILURE = ("silent", "reopen")

SPEED_PACE_SECONDS: dict[str, float] = {
    "instant": 0.0,
    "muito_rapido": 0.01,
    "rapido": 0.03,
    "normal": 0.08,
    "lento": 0.15,
}


def load_defaults(config_path: Path | None = None) -> tuple[str, str]:
    path = config_path if config_path is not None else CONFIG_PATH
    if not path.exists():
        return DEFAULT_ON_FAILURE, DEFAULT_SPEED
    data = json.loads(path.read_text())
    on_failure = data.get("on_failure", DEFAULT_ON_FAILURE)
    speed = data.get("speed", DEFAULT_SPEED)
    if on_failure not in _VALID_ON_FAILURE:
        on_failure = DEFAULT_ON_FAILURE
    if speed not in SPEED_PACE_SECONDS:
        speed = DEFAULT_SPEED
    return on_failure, speed


def pace_seconds_for(speed: str) -> float:
    return SPEED_PACE_SECONDS.get(speed, SPEED_PACE_SECONDS[DEFAULT_SPEED])
