"""CLAUDE.md keeps each gotcha's rule and points to its full record in the
private docs/superpowers/gotchas.md: every anchor there has a pointer here
and every pointer lands (checked only where the private file exists, which
CI and other clones lack), every test file CLAUDE.md names exists, and the
file stays small enough to load in every session."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
CLAUDE_MD = ROOT / "CLAUDE.md"
GOTCHAS = ROOT / "docs" / "superpowers" / "gotchas.md"
MAXIMUM_BYTES = 24_000


def _anchors() -> set[str]:
    return set(re.findall(r'<a id="([a-z0-9-]+)"></a>', GOTCHAS.read_text()))


def _pointers() -> set[str]:
    return set(re.findall(r"docs/superpowers/gotchas\.md#([a-z0-9-]+)", CLAUDE_MD.read_text()))


@pytest.mark.skipif(not GOTCHAS.exists(), reason="the gotchas record is private")
def test_every_gotcha_has_a_pointer_and_every_pointer_lands() -> None:
    assert _anchors() - _pointers() == set()
    assert _pointers() - _anchors() == set()


def test_every_test_file_claude_md_names_exists() -> None:
    named = set(re.findall(r"`(tests/[\w/]+\.py)", CLAUDE_MD.read_text()))
    assert {path for path in named if not (ROOT / path).exists()} == set()


def test_claude_md_fits_its_budget() -> None:
    assert len(CLAUDE_MD.read_bytes()) <= MAXIMUM_BYTES
