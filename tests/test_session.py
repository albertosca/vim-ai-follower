from vim_ai_follower import session as session_mod
from vim_ai_follower.tmux import TmuxWindow


def test_standalone_identity_prefers_term_session_id() -> None:
    s = session_mod.resolve_session({"TERM_SESSION_ID": "w0t0p0"})
    assert s == session_mod.Session(window_id="term-w0t0p0", origin=None, in_tmux=False)


def test_standalone_identity_falls_back_to_iterm_then_tty(monkeypatch) -> None:
    monkeypatch.setattr(session_mod.os, "ttyname", lambda _fd: "/dev/ttys004")
    s = session_mod.resolve_session({"ITERM_SESSION_ID": "abc"})
    assert s.window_id == "iterm-abc"
    s2 = session_mod.resolve_session({})  # no env signal -> tty
    assert s2.window_id == "tty-ttys004"


def test_standalone_identity_defaults_when_no_signal(monkeypatch) -> None:
    monkeypatch.setattr(session_mod.os, "ttyname", lambda _fd: (_ for _ in ()).throw(OSError))
    assert session_mod.resolve_session({}).window_id == "standalone-default"


def test_in_tmux_session_uses_window_id(monkeypatch) -> None:
    monkeypatch.setattr(
        session_mod.TmuxWindow,
        "from_env",
        classmethod(lambda cls, env: TmuxWindow(window_id="@3")),
    )
    s = session_mod.resolve_session({"TMUX_PANE": "%1"})
    assert s == session_mod.Session(window_id="@3", origin="%1", in_tmux=True)


def test_dead_pane_returns_none(monkeypatch) -> None:
    monkeypatch.setattr(
        session_mod.TmuxWindow,
        "from_env",
        classmethod(lambda cls, env: None),
    )
    assert session_mod.resolve_session({"TMUX_PANE": "%1"}) is None
