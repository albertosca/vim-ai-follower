"""The real-HOME guard blames the suite, never the developer's live follower.

N-A (2026-10-20): `assert_real_home_untouched` failed tests because the
developer's own follower, running in their real tmux, wrote `@35.animating`
into the real cache mid-suite. The guard still has to catch a test that
writes there. These tests point the guard at a temporary directory and
never touch the real cache.
"""

from __future__ import annotations

import contextlib
import json
import os
import signal
import subprocess
from collections.abc import Iterator
from pathlib import Path

import e2e_harness
import pytest


@pytest.fixture
def fake_real_cache(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    cache_directory = tmp_path / "real-cache"
    cache_directory.mkdir()
    monkeypatch.setattr(e2e_harness, "REAL_CACHE_DIRECTORY", cache_directory)
    monkeypatch.setattr(e2e_harness, "REAL_CONFIG_PATH", tmp_path / "config.json")
    monkeypatch.setattr(e2e_harness, "_default_socket_sessions", frozenset)
    return cache_directory


@pytest.fixture
def outside_pid() -> Iterator[int]:
    """A live process whose parent chain does not reach pytest: the shell
    that started it exits at once, so it is reparented to init."""
    result = subprocess.run(
        ["sh", "-c", "sleep 30 >/dev/null 2>&1 & echo $!"],
        capture_output=True,
        text=True,
        check=True,
    )
    pid = int(result.stdout.strip())
    yield pid
    with contextlib.suppress(ProcessLookupError):
        os.kill(pid, signal.SIGKILL)


@pytest.fixture
def child_pid() -> Iterator[int]:
    child = subprocess.Popen(["sleep", "30"])
    yield child.pid
    child.kill()
    child.wait()


def _world(window_id: str = "@1") -> e2e_harness.E2EFollower:
    return e2e_harness.E2EFollower(
        token="tok",
        home=Path("/nonexistent-home"),
        workdir=Path("/nonexistent-workdir"),
        tmux_tmpdir="/nonexistent-tmux",
        session="vaf-e2e-guard",
        origin_pane="%0",
        window_id=window_id,
        env={},
        _before=e2e_harness._fingerprint_real_home(),
    )


def _dead_pid() -> int:
    process = subprocess.Popen(["true"])
    process.wait()
    return process.pid


def test_a_live_follower_outside_the_suite_is_not_blamed(
    fake_real_cache: Path, outside_pid: int
) -> None:
    world = _world()
    (fake_real_cache / "@35.animating").write_text(f"{outside_pid} running")
    (fake_real_cache / "@35.pending_animation.json").write_text("{}")
    (fake_real_cache / "@36.pane").write_text(json.dumps({"server_pid": outside_pid}))
    world.assert_real_home_untouched()


def test_a_test_that_writes_the_real_cache_is_caught(fake_real_cache: Path, child_pid: int) -> None:
    world = _world()
    (fake_real_cache / "@2.animating").write_text(f"{child_pid} running")
    (fake_real_cache / "@3.pane").write_text(json.dumps({"backend": "tmux"}))
    (fake_real_cache / "@4.animating").write_text(f"{_dead_pid()} running")
    with pytest.raises(AssertionError) as raised:
        world.assert_real_home_untouched()
    message = str(raised.value)
    for name in ("@2.animating", "@3.pane", "@4.animating"):
        assert name in message


def test_an_entry_in_one_of_the_tests_own_windows_is_blamed_whatever_its_pid(
    fake_real_cache: Path, outside_pid: int
) -> None:
    world = _world("@7")
    (fake_real_cache / "@7.animating").write_text(f"{outside_pid} running")
    with pytest.raises(AssertionError, match=r"@7\.animating"):
        world.assert_real_home_untouched()


def test_an_entry_present_before_the_test_is_not_new(fake_real_cache: Path, child_pid: int) -> None:
    (fake_real_cache / "@9.animating").write_text(f"{child_pid} running")
    world = _world()
    world.assert_real_home_untouched()
