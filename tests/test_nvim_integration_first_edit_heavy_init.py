"""The first Edit right after `start` into a DEDICATED nvim whose init is
slow (BACKLOG sweep-4 "Unseen first Edit": an Edit once never showed, and
hook.log said nothing). Two inits: one that blocks for 1.5 s, and a
plugin-manager stand-in that `vim.wait`s 1 s, during which nvim answers RPC
while it has not started. Each run asserts the file reached a tab.

VAF_HEAVY_INIT_RUNS repeats each run (the measurement ran 20)."""

from __future__ import annotations

import json
import os
from collections.abc import Iterator

import pytest
from e2e_harness import E2EFollower, e2e_world, payload

pytestmark = pytest.mark.integration

RUNS = int(os.environ.get("VAF_HEAVY_INIT_RUNS", "1"))

INITS = {
    "blocking-sleep": "vim.uv.sleep(1500)\n",
    "plugin-manager-wait": (
        "vim.wait(1000, function() return false end)\n"
        "vim.api.nvim_create_autocmd('VimEnter', {callback = function()\n"
        "  vim.wait(300, function() return false end)\n"
        "end})\n"
    ),
}


@pytest.fixture
def world() -> Iterator[E2EFollower]:
    with e2e_world() as live:
        yield live


@pytest.mark.parametrize("init", list(INITS))
def test_the_first_edit_into_a_dedicated_nvim_with_a_slow_init_is_shown(
    world: E2EFollower, init: str
) -> None:
    config = world.home / ".config" / "nvim"
    config.mkdir(parents=True, exist_ok=True)
    (config / "init.lua").write_text(INITS[init])
    log_path = world.cache_dir / "hook.log"
    for run in range(RUNS):
        target = world.workdir / f"first_{run}.py"
        world.start("nvim", speed="instant")
        edit = payload("Write", target)
        world.cli("hook", "pre", stdin=edit)
        target.write_text("one\ntwo\n")
        world.cli("hook", "post", stdin=edit)
        state = json.loads((world.cache_dir / f"{world.window_id}.pane").read_text())
        log = log_path.read_text() if log_path.exists() else ""
        assert os.path.realpath(target) in state["open_files"], f"run {run}: {state}\n{log}"
        assert world.nvim_buffer_lines(state["target"], target) == ["one", "two"], log
        # The next run's nvim listens on the same socket: drop this one's client.
        client = world._nvim_clients.pop(state["target"])
        world.cli("stop")
        client.close()
