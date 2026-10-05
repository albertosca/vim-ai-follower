🇺🇸 [English](https://albertosca.github.io/vim-ai-follower/guides/multi-file-tabs/) · 🇧🇷 [Português](https://albertosca.github.io/vim-ai-follower/pt/guides/multi-file-tabs/)

# Multi-file tabs

Each file Claude touches gets its own Vim tab. Back to the [README](https://github.com/albertosca/vim-ai-follower#readme).

## How the follower handles edits across several files

Editing a file already open navigates back to its tab first (by name, so it survives you closing or reordering tabs), animates there, and returns — it never types into whatever tab happens to be active.

## How many tabs it keeps

The tab list is recency-ordered; the least-recently-used tab is closed once you exceed `max_tabs` (default `5`, see [Configuration](../reference/config.md)). The file being animated or handed over is never the one evicted. In an [adopted editor](adopting-an-editor.md) eviction never closes a buffer with unsaved changes, or one you had open yourself.

Neovim opens real tabs too — the same tab-cycling experience as tmux, kept in sync via its RPC API; per-file eviction still applies (see [Neovim backend](nvim-backend.md)).
