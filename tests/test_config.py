from __future__ import annotations

from pathlib import Path

from vim_ai_follower import config


def test_load_defaults_returns_builtin_defaults_when_no_file(tmp_path: Path) -> None:
    on_failure, speed = config.load_defaults(tmp_path / "config.json")
    assert on_failure == "silent"
    assert speed == "rapido"


def test_load_defaults_reads_persisted_values(tmp_path: Path) -> None:
    path = tmp_path / "config.json"
    path.write_text('{"on_failure": "reopen", "speed": "lento"}')
    on_failure, speed = config.load_defaults(path)
    assert on_failure == "reopen"
    assert speed == "lento"


def test_load_defaults_falls_back_on_invalid_values(tmp_path: Path) -> None:
    path = tmp_path / "config.json"
    path.write_text('{"on_failure": "explode", "speed": "ludicrous"}')
    on_failure, speed = config.load_defaults(path)
    assert on_failure == "silent"
    assert speed == "rapido"


def test_load_defaults_falls_back_on_missing_keys(tmp_path: Path) -> None:
    path = tmp_path / "config.json"
    path.write_text("{}")
    on_failure, speed = config.load_defaults(path)
    assert on_failure == "silent"
    assert speed == "rapido"


def test_pace_seconds_for_known_speeds() -> None:
    assert config.pace_seconds_for("instant") == 0.0
    assert config.pace_seconds_for("muito_rapido") == 0.01
    assert config.pace_seconds_for("rapido") == 0.03
    assert config.pace_seconds_for("normal") == 0.08
    assert config.pace_seconds_for("lento") == 0.15


def test_pace_seconds_for_unknown_speed_falls_back_to_default() -> None:
    assert config.pace_seconds_for("ludicrous") == config.pace_seconds_for("rapido")
