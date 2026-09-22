import os
from unittest.mock import patch

import pytest
from helpers import make_mock_tmux_run
from helpers import register_fake_follower as _register_fake_follower

from vim_ai_follower import cache
from vim_ai_follower import session as session_mod
from vim_ai_follower.tmux import TmuxWindow


# The synthetic-identity tests below reach _standalone_id only because the
# pid-ancestry fallback is off: conftest's autouse `no_pid_ancestry_walk`
# neutralises it for every test that does not carry @pytest.mark.pid_walk.
# Without it these resolve to whatever tmux window the runner is sitting in —
# measured 2026-09-22, three of them came back "@6". The fallback's own tests
# live in test_session_pid_fallback.py.
def test_standalone_identity_prefers_term_session_id() -> None:
    s = session_mod.resolve_session({"TERM_SESSION_ID": "w0t0p0"})
    assert s == session_mod.Session(window_id="term-w0t0p0", origin=None, in_tmux=False)


def test_standalone_identity_falls_back_to_iterm_then_tty(monkeypatch: pytest.MonkeyPatch) -> None:
    # session.py calls the stdlib `os.ttyname` directly, so patching the real
    # `os` module (not the name re-exported through session_mod) is what
    # actually intercepts the call.
    monkeypatch.setattr(os, "ttyname", lambda _fd: "/dev/ttys004")
    s = session_mod.resolve_session({"ITERM_SESSION_ID": "abc"})
    assert s is not None
    assert s.window_id == "iterm-abc"
    s2 = session_mod.resolve_session({})  # no env signal -> tty
    assert s2 is not None
    assert s2.window_id == "tty-ttys004"


def test_standalone_identity_defaults_when_no_signal(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(os, "ttyname", lambda _fd: (_ for _ in ()).throw(OSError))
    s = session_mod.resolve_session({})
    assert s is not None
    assert s.window_id == "standalone-default"


def test_in_tmux_session_uses_window_id(monkeypatch: pytest.MonkeyPatch) -> None:
    # Patch the TmuxWindow class object itself (imported above), the same
    # object session.py's `from vim_ai_follower.tmux import TmuxWindow`
    # binds to its own module namespace.
    monkeypatch.setattr(
        TmuxWindow,
        "from_env",
        classmethod(lambda cls, env: TmuxWindow(window_id="@3")),
    )
    s = session_mod.resolve_session({"TMUX_PANE": "%1"})
    assert s == session_mod.Session(window_id="@3", origin="%1", in_tmux=True)


def test_dead_pane_returns_none(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        TmuxWindow,
        "from_env",
        classmethod(lambda cls, env: None),
    )
    assert session_mod.resolve_session({"TMUX_PANE": "%1"}) is None


def test_other_live_followers_skips_own_identity_and_dead_ones() -> None:
    # $TMUX was measured (2026-09-22) NOT to survive the bg-spare
    # re-parenting, so no window can be recovered from it and none is. The
    # pid-ancestry fallback added later recovers a DIFFERENT case (TMUX_PANE
    # stripped, process still a pane's descendant) and does not reach the
    # bg-pty-host chain either, so this diagnostic scan — which makes the
    # give-up visible — is still the whole answer for that one.
    _register_fake_follower("@18", "%23", current_file="/tmp/a.py")
    _register_fake_follower("@19", "%24")
    _register_fake_follower("term-me", "%25")
    # %23 and %25 are both live vim panes; %24 is not. term-me is therefore
    # live AND is the caller's own identity — it has to be excluded for being
    # the caller's, not for being dead. An earlier version of this test gave
    # term-me a dead pane, which made the own-key skip untestable: deleting
    # the skip changed nothing and the canary came back blind.
    with patch(
        "vim_ai_follower.tmux.subprocess.run",
        side_effect=make_mock_tmux_run(pane_id="%23", vim_panes=("%25",)),
    ):
        others = session_mod.other_live_followers("term-me")
    assert others == [
        session_mod.OtherFollower(
            window_id="@18", backend="tmux", target="%23", current_file="/tmp/a.py"
        )
    ]


def test_other_live_followers_never_collects_dead_state() -> None:
    """cmd_stop's sibling scan deletes the dead state it walks past. This one
    must not: it runs from a hook, and a hook that failed to find its own
    follower has no business deleting another window's."""
    _register_fake_follower("@19", "%24")
    with patch(
        "vim_ai_follower.tmux.subprocess.run", side_effect=make_mock_tmux_run(pane_id="%23")
    ):
        assert session_mod.other_live_followers("term-me") == []
    assert (cache.CACHE_DIR / "@19.pane").exists()


def test_other_live_followers_is_empty_when_nothing_is_registered() -> None:
    assert session_mod.other_live_followers("term-me") == []
