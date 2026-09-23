🇺🇸 [English](https://albertosca.github.io/vim-ai-follower/getting-started/first-follow/) · 🇧🇷 [Português](https://albertosca.github.io/vim-ai-follower/pt/getting-started/first-follow/)

# Follow your first edit

A dedicated tmux pane (or a running Neovim) mirrors every file Claude Code reads or writes, animating each change line by line so it looks like Claude is typing into your own editor. This page gets you from an installed plugin to a first animated edit. Back to the [README](https://github.com/albertosca/vim-ai-follower#readme).

## How to watch Claude Code edit a file in Vim

1. [Install the plugin](install.md), and run `claude` inside a tmux session.
2. From Claude Code, in the tmux session where `claude` runs, start the follower: `/vim-ai-follower:start`. It opens a follower pane (or adopts an existing Vim, with [`adopt_existing`](../guides/adopting-an-editor.md)).
3. Ask Claude to edit a file. On each `Edit`/`MultiEdit`/`Write`, a `PreToolUse` hook snapshots the file's old content and a `PostToolUse` hook diffs it against the new content and replays the change into the follower. On each `Read`, the follower navigates to that file (and line).
4. Check what it is showing with `/vim-ai-follower:status`, and tear it down with `/vim-ai-follower:stop`.

`/vim-ai-follower:start` passes its arguments straight through to `claude-follow start` (flags in the [CLI reference](../reference/cli.md)), e.g. `/vim-ai-follower:start --backend nvim --speed lento`. `/vim-ai-follower:toggle` mutes and unmutes it.

The follower buffer is kept read-only between animations (an adopted Neovim is the exception: it is your own editor, so it is never locked), so a stray keystroke can never corrupt what you are watching; the animation unlocks around itself and relocks (with a silent disk resync) when it finishes.

## How to open the follower automatically

With `open_policy` set to `always` or `code` (see [Configuration](../reference/config.md)), you don't even need `start`: the first matching edit opens the follower automatically.

## If you used the manual / development install

`pip install -e .` already put `claude-follow` on your shell's `PATH`, so run it directly from a pane inside the tmux session where `claude` runs:

```sh
claude-follow start      # open a follower pane (or adopt an existing Vim)
claude-follow status     # show the active follower and the file it is showing
claude-follow stop       # tear the follower down and remove the keybindings
```

Next: [pause, take over and change the speed](../guides/controls.md).
