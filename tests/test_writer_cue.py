from __future__ import annotations

from vim_ai_follower import writer_cue


def test_identity_prefers_agent_id() -> None:
    assert writer_cue.writer_identity({"session_id": "$1", "agent_id": "a9"}) == "a9"


def test_identity_falls_back_to_session_id_when_agent_id_absent_or_empty() -> None:
    assert writer_cue.writer_identity({"session_id": "$1"}) == "$1"
    payload = {"session_id": "$1", "agent_id": ""}
    assert writer_cue.writer_identity(payload) == "$1"


def test_identity_is_none_when_nothing_identifies() -> None:
    assert writer_cue.writer_identity({}) is None
    assert writer_cue.writer_identity({"session_id": "", "agent_id": ""}) is None


def test_label_prefers_agent_type() -> None:
    payload = {"session_id": "$1", "agent_type": "code-reviewer"}
    assert writer_cue.writer_label(payload) == "code-reviewer"


def test_label_falls_back_to_short_session_for_top_level() -> None:
    assert writer_cue.writer_label({"session_id": "sess-abcdef123456"}) == "session:123456"


def test_label_unknown_when_nothing_present() -> None:
    assert writer_cue.writer_label({}) == "unknown"


def test_color_is_stable_by_position() -> None:
    writers = ("a", "b", "c")
    assert writer_cue.color_for(writers, "a") == writer_cue.PALETTE[0]
    assert writer_cue.color_for(writers, "b") == writer_cue.PALETTE[1]
    # position, not hash: the same identity always maps to the same color
    assert writer_cue.color_for(writers, "a") == writer_cue.color_for(("a",), "a")


def test_color_wraps_past_palette_length() -> None:
    n = len(writer_cue.PALETTE)
    writers = tuple(str(i) for i in range(n + 1))
    assert writer_cue.color_for(writers, str(n)) == writer_cue.PALETTE[0]


def test_palette_has_several_distinct_colors() -> None:
    assert len(writer_cue.PALETTE) >= 4
    assert len(set(writer_cue.PALETTE)) == len(writer_cue.PALETTE)


def test_identity_ignores_non_string_values() -> None:
    # a malformed (non-string) agent_id must be skipped, not returned
    payload = {"agent_id": 42, "session_id": "$1"}
    assert writer_cue.writer_identity(payload) == "$1"
    payload = {"agent_id": 42, "session_id": 7}
    assert writer_cue.writer_identity(payload) is None


def test_label_ignores_non_string_values() -> None:
    payload = {"agent_type": 42, "session_id": "sess-xyz123"}
    assert writer_cue.writer_label(payload) == "session:xyz123"
    payload = {"session_id": 7}
    assert writer_cue.writer_label(payload) == "unknown"
