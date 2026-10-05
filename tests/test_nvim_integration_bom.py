"""Real-nvim twin of tests/test_integration_bom.py (final review of
backlog-sweep-3, I2). At acce2b2 an adopted nvim left every Edit of a BOM
file alone with the "buffer differs" cue, and a dedicated one typed the BOM
character into line 1 of a buffer that also had 'bomb' set, so a `:w` would
have written two BOMs."""

from __future__ import annotations

from pathlib import Path

import pytest

pynvim = pytest.importorskip("pynvim")

from test_nvim_integration_adopted_base_mismatch import (  # noqa: E402
    _adopted_nvim,
    _buffer,
    _edit,
    _status_text,
)
from test_nvim_integration_edit_base_mismatch import _ENV, _WINDOW, _payload  # noqa: E402

from vim_ai_follower import cache, hooks  # noqa: E402
from vim_ai_follower.hooks import BASE_DIFFERS_CUE  # noqa: E402
from vim_ai_follower.state import FollowerState  # noqa: E402

pytestmark = pytest.mark.integration

BOM = "﻿"
ONE = BOM + "x = 1\ny = 2\n"
TWO = BOM + "x = 1\ny = 2\nz = 3\n"


def test_an_adopted_nvim_animates_an_edit_of_a_bom_file(
    headless_nvim: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, nvim = _adopted_nvim(headless_nvim, tmp_path, monkeypatch)
    target = (tmp_path / "a.py").resolve()
    target.write_text(ONE)
    nvim.command(f"edit {target}")
    assert nvim.current.buffer[:] == ["x = 1", "y = 2"]

    _edit(target, TWO)

    assert _buffer(nvim, "a.py")[:] == ["x = 1", "y = 2", "z = 3"]
    assert BASE_DIFFERS_CUE not in _status_text(nvim)


def test_a_dedicated_nvim_never_types_the_bom(
    headless_nvim: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(cache, "CACHE_DIR", tmp_path / "cache")
    FollowerState.set(_WINDOW, "nvim", headless_nvim, origin="", shown_any=False)
    nvim = pynvim.attach("socket", path=headless_nvim)
    target = (tmp_path / "a.py").resolve()
    assert hooks.cmd_hook_pre(_ENV, _payload("Write", target)) == 0
    target.write_text(ONE)
    assert hooks.cmd_hook_post(_ENV, _payload("Write", target)) == 0
    assert _buffer(nvim, "a.py")[:] == ["x = 1", "y = 2"]
    handle = _buffer(nvim, "a.py").number

    _edit(target, TWO)

    buffer = _buffer(nvim, "a.py")
    assert buffer[:] == ["x = 1", "y = 2", "z = 3"]
    assert buffer.number == handle  # a diff, not a retype
    assert buffer.options["bomb"]
