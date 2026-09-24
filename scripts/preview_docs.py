"""Serves the built docs site locally, the way GitHub Pages serves it.

The guide pages link assets and the other language by absolute production URL, so they
render on GitHub as well as on Pages. A plain `http.server` on site/ breaks both: the
assets come from production (or 404 before the first deploy) and the language switcher
points at /vim-ai-follower/. This copies site/ to .docs-preview/vim-ai-follower/,
rewrites the production URL to that path, and serves .docs-preview/.

Usage (after `bash scripts/build_docs.sh`):
    python scripts/preview_docs.py [PORT]    # then open http://localhost:PORT/vim-ai-follower/
"""

import functools
import http.server
import shutil
import sys
from pathlib import Path

PROD_URL = "https://albertosca.github.io/vim-ai-follower/"
PAGES_PATH = "vim-ai-follower"
_TEXT_SUFFIXES = {".html", ".xml", ".txt", ".css", ".js"}


def prepare(site: Path, preview: Path) -> Path:
    root = preview / PAGES_PATH
    if preview.exists():
        shutil.rmtree(preview)
    shutil.copytree(site, root)
    for path in root.rglob("*"):
        if path.is_file() and path.suffix in _TEXT_SUFFIXES:
            text = path.read_text()
            if PROD_URL in text:
                path.write_text(text.replace(PROD_URL, f"/{PAGES_PATH}/"))
    return root


def main(argv: list[str]) -> int:  # pragma: no cover - serves forever
    port = int(argv[1]) if len(argv) > 1 else 8000
    repo = Path(__file__).resolve().parents[1]
    prepare(repo / "site", repo / ".docs-preview")
    handler = functools.partial(
        http.server.SimpleHTTPRequestHandler, directory=str(repo / ".docs-preview")
    )
    print(f"http://localhost:{port}/{PAGES_PATH}/")
    http.server.ThreadingHTTPServer(("127.0.0.1", port), handler).serve_forever()
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main(sys.argv))
