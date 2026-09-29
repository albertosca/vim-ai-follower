"""After a hook's LAST animation returns it still holds the window's slot
(for its bookkeeping: cue refresh, current-file pointer), so the marker must
not read "running" there: a P or S pressed in that window used to be
answered "pause requested" / "interrupt requested", addressed to the hook
and never consumed. The hook marks "finishing", which cmd_pause and
cmd_interrupt treat as nothing to act on."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import pytest
from test_hooks_base_mismatch import _follower, _run_hook, _setup

from vim_ai_follower import control, hooks
from vim_ai_follower.animate import AnimationResult


def _state_at_release(monkeypatch: pytest.MonkeyPatch) -> list[str | None]:
    """What the marker says at the moment the hook releases its slot."""
    seen: list[str | None] = []
    real: Callable[..., None] = control.clear_animating

    def release(window_id: str, base_dir: Path | None = None) -> None:
        seen.append(control.animating_state(window_id, base_dir))
        real(window_id, base_dir)

    monkeypatch.setattr(control, "clear_animating", release)
    return seen


@pytest.mark.parametrize("outcome", ["diff", "retype", "interrupted then released"])
def test_the_hook_marks_finishing_once_its_last_animation_returns(
    outcome: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target, _resolved = _setup(tmp_path)
    follower = _follower("absent" if outcome == "retype" else "holds")
    if outcome == "interrupted then released":
        follower.apply_edit.return_value = AnimationResult("interrupted", 0)
        monkeypatch.setattr(hooks, "_await_user_handoff", lambda *a, **k: None)
    seen = _state_at_release(monkeypatch)
    _run_hook(target, follower, monkeypatch)
    assert seen == ["finishing"]
    assert control.is_animating("@1") is False
