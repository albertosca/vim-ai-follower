"""Single source for the on-disk cache directory shared by state files
(state.py), signal/pending files (control.py) and the hook log (cli.py) —
they must all live together, or a relocation would split one session's
state and signals across two directories."""

from __future__ import annotations

from pathlib import Path

CACHE_DIR = Path.home() / ".cache" / "claude-vim-follower"
