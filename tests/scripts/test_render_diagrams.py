from __future__ import annotations

import sys
from pathlib import Path
from types import ModuleType

import pytest

_SCRIPTS_DIR = Path(__file__).resolve().parents[2] / "scripts"


def _rd() -> ModuleType:
    if str(_SCRIPTS_DIR) not in sys.path:
        sys.path.insert(0, str(_SCRIPTS_DIR))
    import render_diagrams

    return render_diagrams


def test_tokens_are_replaced_per_theme() -> None:
    rd = _rd()
    out = rd.render('<line stroke="{{fg}}"/><path fill="{{accent}}"/>', rd.THEMES["dark"])
    assert (
        out
        == f'<line stroke="{rd.THEMES["dark"]["fg"]}"/><path fill="{rd.THEMES["dark"]["accent"]}"/>'
    )


def test_gruvbox_yellow_is_the_same_in_both_themes() -> None:
    rd = _rd()
    assert rd.THEMES["light"]["accent"] == rd.THEMES["dark"]["accent"] == "#FABD2F"


def test_themes_use_the_gruvbox_grounds_and_inks() -> None:
    rd = _rd()
    assert rd.THEMES["light"] == {
        "fg": "#3C3836",
        "muted": "#928374",
        "accent": "#FABD2F",
        "ground": "#FBF1C7",
    }
    assert rd.THEMES["dark"] == {
        "fg": "#EBDBB2",
        "muted": "#928374",
        "accent": "#FABD2F",
        "ground": "#282828",
    }


def test_an_unknown_token_fails_loudly() -> None:
    with pytest.raises(KeyError, match="colour"):
        _rd().render("{{colour}}", _rd().THEMES["light"])


def test_every_shipped_source_renders_in_both_themes() -> None:
    rd = _rd()
    sources = sorted((_SCRIPTS_DIR.parent / "assets/diagrams/src").glob("*.svg"))
    assert sources, "no diagram sources found"
    for source in sources:
        for theme in rd.THEMES.values():
            out = rd.render(source.read_text(), theme)
            assert (
                "{{" not in out
                and "<script" not in out
                and "<style" not in out
                and "foreignObject" not in out
            )
