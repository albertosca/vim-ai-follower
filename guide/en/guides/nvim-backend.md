🇺🇸 [English](https://albertosca.github.io/vim-ai-follower/guides/nvim-backend/) · 🇧🇷 [Português](https://albertosca.github.io/vim-ai-follower/pt/guides/nvim-backend/)

# Neovim backend

vim-ai-follower has two backends. Back to the [README](https://github.com/albertosca/vim-ai-follower#readme).

- **`tmux`** (default): drives an unmodified Vim in a tmux pane via `send-keys`.
- **`nvim`** (needs pynvim, Neovim ≥ 0.10): drives a real Neovim entirely over msgpack-RPC.

## How to use Claude Code with Neovim

1. Install pynvim into the `python3` on your `PATH`: `pip install pynvim` (plugin install), or `pip install -e '.[nvim]'` from a clone (see [Install](../getting-started/install.md)).
2. Start the follower with the nvim backend: `/vim-ai-follower:start --backend nvim`, or set `"backend": "nvim"` in the [configuration file](../reference/config.md).

## What the nvim backend does differently

The `nvim` backend drives a real Neovim entirely over msgpack-RPC — no `send-keys`, so the keystroke-corruption bug class the tmux backend has to fight simply does not exist. Every animation, live-speed change, pause/interrupt, and the hand-back after an interrupt works the same as tmux.

**Adopt-or-launch:** with `adopt_existing: true` (or `start --backend nvim` in a window that already has a running nvim) it adopts that nvim — your own editor, never locked read-only; otherwise it launches a dedicated headless nvim for the window.

Neovim opens real tabs too — the same tab-cycling experience as tmux, kept in sync via its RPC API; per-file eviction still applies (`max_tabs`, see [Multi-file tabs](multi-file-tabs.md)).

Why two backends, and what the RPC route cost, is on the [Engineering](../engineering.md) page.
