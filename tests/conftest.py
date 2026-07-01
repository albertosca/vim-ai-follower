from __future__ import annotations

import subprocess
import time
import uuid
from collections.abc import Callable, Iterator

import pytest


@pytest.fixture
def tmux_session() -> Iterator[str]:
    session_name = f"pytest-{uuid.uuid4().hex[:8]}"
    subprocess.run(
        ["tmux", "new-session", "-d", "-s", session_name, "-x", "100", "-y", "30"],
        check=True,
    )
    try:
        yield session_name
    finally:
        subprocess.run(["tmux", "kill-session", "-t", session_name], check=False)


@pytest.fixture
def wait_until() -> Callable[..., bool]:
    def _wait(predicate: Callable[[], bool], timeout: float = 3.0, interval: float = 0.1) -> bool:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if predicate():
                return True
            time.sleep(interval)
        return False

    return _wait
