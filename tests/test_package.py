import tomllib
from pathlib import Path

import vim_ai_follower


def test_package_is_importable() -> None:
    assert vim_ai_follower is not None


def test_core_has_no_runtime_deps_and_pynvim_is_an_extra() -> None:
    data = tomllib.loads(Path("pyproject.toml").read_text())
    assert data["project"].get("dependencies", []) == []
    assert "pynvim" in " ".join(data["project"]["optional-dependencies"]["nvim"])
