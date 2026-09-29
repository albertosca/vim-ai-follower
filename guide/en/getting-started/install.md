🇺🇸 [English](https://albertosca.github.io/vim-ai-follower/getting-started/install/) · 🇧🇷 [Português](https://albertosca.github.io/vim-ai-follower/pt/getting-started/install/)

# Install vim-ai-follower

vim-ai-follower installs as a Claude Code plugin that wires its own hooks, and works from any project where `claude` runs inside tmux. Back to the [README](https://github.com/albertosca/vim-ai-follower#readme).

## Requirements

- tmux, with `claude` running inside a tmux session
- Vim (the default `tmux` backend) or Neovim ≥ 0.10 (the first-class `nvim` backend)
- Python 3.11+

## How to install the Claude Code plugin

The plugin declares the hooks itself, so there is no `settings.json` to edit. This is the recommended install.

1. From Claude Code, add the marketplace and install the plugin:

    ```
    /plugin marketplace add albertosca/vim-ai-follower
    /plugin install vim-ai-follower
    ```

2. For the default `tmux` backend, you are done: it needs **no `pip` install** — the plugin bundles the stdlib-only CLI and runs it in place.
3. For the `nvim` backend, additionally install pynvim into the `python3` on your `PATH`:

    ```sh
    pip install pynvim
    ```

Hooks never fail a tool call: they are built to exit `0` and log problems to `~/.cache/claude-vim-follower/hook.log` instead of reporting them to Claude.

Next: [follow your first edit](first-follow.md).

## Manual / development install

From a clone — for development, or if you'd rather not use the plugin:

```sh
pip install -e .          # installs the `claude-follow` script
pip install -e '.[nvim]'  # add the pynvim extra for the nvim backend
```

Then wire the hooks by hand: add these to `~/.claude/settings.json` (use the absolute path to the installed `claude-follow`):

```json
{
  "hooks": {
    "PreToolUse": [
      { "matcher": "Edit|MultiEdit|Write",
        "hooks": [{ "type": "command", "command": "claude-follow hook pre" }] }
    ],
    "PostToolUse": [
      { "matcher": "Edit|MultiEdit|Write",
        "hooks": [{ "type": "command", "command": "claude-follow hook post" }] },
      { "matcher": "Read",
        "hooks": [{ "type": "command", "command": "claude-follow hook post" }] }
    ],
    "PostToolUseFailure": [
      { "matcher": "Edit|MultiEdit|Write",
        "hooks": [{ "type": "command", "command": "claude-follow hook failure" }] }
    ],
    "PermissionDenied": [
      { "matcher": "Edit|MultiEdit|Write",
        "hooks": [{ "type": "command", "command": "claude-follow hook failure" }] }
    ]
  }
}
```
