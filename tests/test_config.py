from __future__ import annotations

from pathlib import Path

from vim_ai_follower import config


def test_load_returns_defaults_when_no_file(tmp_path: Path) -> None:
    cfg = config.load(tmp_path / "missing.json")
    assert cfg == config.Config(
        on_failure="silent",
        speed="rapido",
        open_policy="manual",
        adopt_existing=False,
        max_tabs=5,
    )


def test_load_reads_new_keys(tmp_path: Path) -> None:
    path = tmp_path / "config.json"
    path.write_text('{"open_policy": "code", "adopt_existing": true, "max_tabs": 3}')
    cfg = config.load(path)
    assert (cfg.open_policy, cfg.adopt_existing, cfg.max_tabs) == ("code", True, 3)


def test_load_falls_back_on_invalid_values(tmp_path: Path) -> None:
    path = tmp_path / "config.json"
    path.write_text('{"open_policy": "sometimes", "adopt_existing": "yes", "max_tabs": 0}')
    cfg = config.load(path)
    assert (cfg.open_policy, cfg.adopt_existing, cfg.max_tabs) == ("manual", False, 5)


def test_load_falls_back_on_invalid_on_failure_and_speed(tmp_path: Path) -> None:
    path = tmp_path / "config.json"
    path.write_text('{"on_failure": "bogus", "speed": "ludicrous"}')
    cfg = config.load(path)
    assert cfg.on_failure == config.DEFAULT_ON_FAILURE
    assert cfg.speed == config.DEFAULT_SPEED


def test_load_rejects_bool_max_tabs(tmp_path: Path) -> None:
    # JSON true is a Python bool, and bool is an int subclass with True == 1,
    # so without the explicit isinstance(bool) guard `"max_tabs": true` would
    # sneak through as a 1-tab limit instead of falling back to the default.
    path = tmp_path / "config.json"
    path.write_text('{"max_tabs": true}')
    assert config.load(path).max_tabs == config.DEFAULT_MAX_TABS


def test_next_speed_steps_and_clamps() -> None:
    assert config.next_speed("rapido", "up") == "muito_rapido"
    assert config.next_speed("rapido", "down") == "normal"
    # Clamped at both ends: wrapping surprised in live use ("lento" jumping
    # to "instant" mid-animation), so the scale saturates instead.
    assert config.next_speed("instant", "up") == "instant"
    assert config.next_speed("lento", "down") == "lento"
    assert config.next_speed("bogus", "up") == config.DEFAULT_SPEED


def test_is_code_file() -> None:
    assert config.is_code_file("/a/b/main.py")
    assert config.is_code_file("/a/b/app.TSX")  # case-insensitive
    assert not config.is_code_file("/a/b/notes.md")
    assert not config.is_code_file("/a/b/Makefile")  # no extension


def test_pace_seconds_for_known_speeds() -> None:
    assert config.pace_seconds_for("instant") == 0.0
    assert config.pace_seconds_for("muito_rapido") == 0.01
    assert config.pace_seconds_for("rapido") == 0.03
    assert config.pace_seconds_for("normal") == 0.08
    assert config.pace_seconds_for("lento") == 0.15


def test_pace_seconds_for_unknown_speed_falls_back_to_default() -> None:
    assert config.pace_seconds_for("ludicrous") == config.pace_seconds_for("rapido")
