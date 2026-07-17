"""Pure helpers for the per-writer color cue: compose a writer's identity and
label from a hook payload, and map an identity to a stable border color by its
position in the window's append-only writers list."""

from __future__ import annotations

from typing import Any

# Distinct 256-color border colors, legible on light and dark themes. The
# color is chosen by the writer's position in the window's writers list, so
# these need only be visually distinguishable, not semantically ordered.
PALETTE: tuple[str, ...] = (
    "colour203",  # salmon
    "colour78",  # green
    "colour220",  # yellow
    "colour75",  # blue
    "colour170",  # magenta
    "colour44",  # cyan
)


def writer_identity(payload: dict[str, Any]) -> str | None:
    """The writer's stable identity: agent_id (a Task subagent) when present,
    else session_id (the foreground or a background agent). None when neither
    is a non-empty string — the cue simply does not fire for that hook."""
    for key in ("agent_id", "session_id"):
        value = payload.get(key)
        if isinstance(value, str) and value:
            return value
    return None


def writer_label(payload: dict[str, Any]) -> str:
    """Human-readable label: the subagent's agent_type when present, else a
    short slice of the session_id so two distinct top-level sessions (the
    foreground plus a background agent) read differently, not both 'main'."""
    agent_type = payload.get("agent_type")
    if isinstance(agent_type, str) and agent_type:
        return agent_type
    session_id = payload.get("session_id")
    if isinstance(session_id, str) and session_id:
        return f"session:{session_id[-6:]}"
    return "unknown"


def color_for(writers: tuple[str, ...], identity: str) -> str:
    """The border color for identity, fixed by its position in writers (which
    is append-only), so a living writer's color never shifts."""
    return PALETTE[writers.index(identity) % len(PALETTE)]
