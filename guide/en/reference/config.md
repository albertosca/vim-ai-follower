🇺🇸 [English](https://albertosca.github.io/vim-ai-follower/reference/config/) · 🇧🇷 [Português](https://albertosca.github.io/vim-ai-follower/pt/reference/config/)

# Configuration

Optional JSON at `~/.config/claude-vim-follower/config.json`. Every key has a default, and an invalid value silently falls back to it. Back to the [README](https://github.com/albertosca/vim-ai-follower#readme).

## Configuration keys

| Key             | Values                                                    | Default    | Meaning                                                                                 |
| --------------- | --------------------------------------------------------- | ---------- | --------------------------------------------------------------------------------------- |
| `backend`       | `tmux`, `nvim`                                            | `tmux`     | `tmux`: drive an unmodified Vim in a tmux pane via `send-keys`. `nvim`: drive a real Neovim over msgpack-RPC (adopts a running nvim in the window, or launches a dedicated one — see [Neovim backend](../guides/nvim-backend.md)). Requires the `nvim` install extra. |
| `open_policy`   | `always`, `code`, `manual`                                | `manual`   | `manual`: only `start` opens a follower. `code`: auto-open on edits/reads of code files; non-code files are also ignored (never animated) even for a manually started follower. `always`: auto-open on any file. |
| `adopt_existing`| `true`, `false`                                           | `false`    | When opening automatically (or on `start`), reuse a Vim already running in the window instead of splitting a new pane. See [Adopting an editor](../guides/adopting-an-editor.md). |
| `max_tabs`      | integer ≥ 1                                                | `5`        | How many file tabs the follower keeps. The least-recently-touched tab is closed past this limit. See [Multi-file tabs](../guides/multi-file-tabs.md). |
| `on_failure`    | `silent`, `reopen`                                        | `silent`   | If the follower pane dies, `reopen` re-splits a fresh one from where it started; `silent` just stops following. |
| `speed`         | `instant`, `muito_rapido`, `rapido`, `normal`, `lento`    | `rapido`   | Animation pace (seconds per line boundary: `0`, `0.01`, `0.03`, `0.08`, `0.15`).         |

## Example

```json
{
  "backend": "nvim",
  "open_policy": "code",
  "max_tabs": 8,
  "speed": "normal"
}
```

Flags passed to [`claude-follow start`](cli.md) override `backend`, `on_failure` and `speed` for that session.
