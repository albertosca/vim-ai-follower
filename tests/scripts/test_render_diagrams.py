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
    render_diagrams = _rd()
    out = render_diagrams.render(
        '<line stroke="{{fg}}"/><path fill="{{accent}}"/>', render_diagrams.THEMES["dark"]
    )
    assert (
        out == f'<line stroke="{render_diagrams.THEMES["dark"]["fg"]}"/>'
        f'<path fill="{render_diagrams.THEMES["dark"]["accent"]}"/>'
    )


def test_gruvbox_yellow_is_the_same_in_both_themes() -> None:
    render_diagrams = _rd()
    assert (
        render_diagrams.THEMES["light"]["accent"]
        == render_diagrams.THEMES["dark"]["accent"]
        == "#FABD2F"
    )


def test_themes_use_the_gruvbox_grounds_and_inks() -> None:
    render_diagrams = _rd()
    assert render_diagrams.THEMES["light"] == {
        "fg": "#3C3836",
        "muted": "#928374",
        "accent": "#FABD2F",
        "ground": "#FBF1C7",
    }
    assert render_diagrams.THEMES["dark"] == {
        "fg": "#EBDBB2",
        "muted": "#928374",
        "accent": "#FABD2F",
        "ground": "#282828",
    }


def test_an_unknown_token_fails_loudly() -> None:
    with pytest.raises(KeyError, match="colour"):
        _rd().render("{{colour}}", _rd().THEMES["light"])


def test_every_shipped_source_renders_in_both_themes() -> None:
    render_diagrams = _rd()
    sources = sorted((_SCRIPTS_DIR.parent / "assets/diagrams/src").glob("*.svg"))
    assert sources, "no diagram sources found"
    for source in sources:
        for theme in render_diagrams.THEMES.values():
            out = render_diagrams.render(source.read_text(), theme)
            assert (
                "{{" not in out
                and "<script" not in out
                and "<style" not in out
                and "foreignObject" not in out
            )


def test_committed_svgs_match_the_current_render() -> None:
    """Guards drift: a source edited without rerunning render_diagrams.py
    would leave a *-light.svg/*-dark.svg that no longer matches its source —
    exactly the staleness scripts/check_readme_facts.py guards for the proof
    numbers, applied here to the diagrams."""
    render_diagrams = _rd()
    root = _SCRIPTS_DIR.parent / "assets/diagrams"
    sources = sorted((root / "src").glob("*.svg"))
    assert sources, "no diagram sources found"
    for source in sources:
        for name, theme in render_diagrams.THEMES.items():
            committed = root / f"{source.stem}-{name}.svg"
            assert committed.exists(), f"{committed} is missing — run scripts/render_diagrams.py"
            assert committed.read_text() == render_diagrams.render(source.read_text(), theme), (
                f"{committed} is stale against {source} — rerun scripts/render_diagrams.py"
            )


def test_a_mutated_source_would_fail_the_staleness_check() -> None:
    """Canary for test_committed_svgs_match_the_current_render: proves the
    comparison actually catches drift, without mutating the repo's own
    source file — mutate a COPY of the text in memory and render that."""
    render_diagrams = _rd()
    root = _SCRIPTS_DIR.parent / "assets/diagrams"
    source = sorted((root / "src").glob("*.svg"))[0]
    original = source.read_text()
    assert "{{fg}}" in original, "fixture assumption: the source uses {{fg}}"
    mutated = original.replace("{{fg}}", "{{fg}}<!-- drift -->", 1)
    committed = (root / f"{source.stem}-light.svg").read_text()
    assert render_diagrams.render(mutated, render_diagrams.THEMES["light"]) != committed
