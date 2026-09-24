from __future__ import annotations

import sys
from pathlib import Path
from types import ModuleType

_SCRIPTS_DIR = Path(__file__).resolve().parents[2] / "scripts"
PROD = "https://albertosca.github.io/vim-ai-follower/"


def _pd() -> ModuleType:
    if str(_SCRIPTS_DIR) not in sys.path:
        sys.path.insert(0, str(_SCRIPTS_DIR))
    import preview_docs

    return preview_docs


def test_prepare_serves_the_working_copy_under_the_pages_path(tmp_path: Path) -> None:
    site, preview = tmp_path / "site", tmp_path / "preview"
    (site / "pt").mkdir(parents=True)
    (site / "index.html").write_text(f'<img src="{PROD}assets/d.svg#gh-dark-mode-only">')
    (site / "pt/index.html").write_text(f'<a href="{PROD}pt/">pt</a>')
    (site / "d.svg").write_bytes(b"<svg/>")

    root = _pd().prepare(site, preview)

    assert root == preview / "vim-ai-follower"
    assert (root / "index.html").read_text() == (
        '<img src="/vim-ai-follower/assets/d.svg#gh-dark-mode-only">'
    )
    assert (root / "pt/index.html").read_text() == '<a href="/vim-ai-follower/pt/">pt</a>'
    assert (root / "d.svg").read_bytes() == b"<svg/>"


def test_prepare_replaces_a_stale_preview(tmp_path: Path) -> None:
    site, preview = tmp_path / "site", tmp_path / "preview"
    site.mkdir()
    (site / "index.html").write_text("new")
    stale = preview / "vim-ai-follower" / "gone.html"
    stale.parent.mkdir(parents=True)
    stale.write_text("old")

    root = _pd().prepare(site, preview)

    assert not stale.exists()
    assert (root / "index.html").read_text() == "new"
