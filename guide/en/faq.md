🇺🇸 [English](https://albertosca.github.io/vim-ai-follower/faq/) · 🇧🇷 [Português](https://albertosca.github.io/vim-ai-follower/pt/faq/)

# FAQ

Straight answers to the questions people ask before letting a tool type into their editor, then the current limitations. Back to the [README](https://github.com/albertosca/vim-ai-follower#readme).

## Questions

### What is vim-ai-follower?

A Claude Code plugin that replays every file edit Claude makes into a real Vim or Neovim, line by line, at a pace you can read. `PreToolUse` and `PostToolUse` hooks snapshot the file, diff it, and animate the change into a tmux pane running Vim, or into a Neovim over RPC. You can pause it, take the keyboard mid-edit, and hand it back. See [how it works](engineering.md#how-vim-ai-follower-works).

### Does it slow Claude down?

Yes, while it animates. Claude's edit is already on disk when the animation starts, but the hook holds Claude's turn until the animation finishes, so a long edit at a slow pace takes real time. For speed, `prefix` `+` steps the pace up, one notch at a time, up to `instant` (no pacing), and `prefix` `F` mutes the follower: while muted, edits are not animated at all. `prefix` `S` is not a speed control: it stops the animation and hands you the buffer, and Claude's turn then waits until you save your version (or press `S` again to discard your edits and let the animation resume). See [Pause, take over, speed and mute](guides/controls.md).

### Does it touch my files?

No. The edit reaches your file through Claude Code's own `Edit`/`Write` tool; the follower only replays it. Follower buffers are never saved, and between animations they are locked read-only, so a stray keystroke can't change what you are watching (an adopted Neovim is the exception: it is your own editor, so it is never locked). The one time a file is written from the follower is when you take over with `prefix` `S` and save your own version yourself.

### Can it break a Claude Code tool call?

No. The hooks are built to exit `0` and log problems to `~/.cache/claude-vim-follower/hook.log` instead of failing the tool call. The trade-off is that a failure is silent in Claude: if the follower does nothing, read the log.

### Does anything leave my machine?

No. The package imports no network module; tmux is driven through its own CLI, and Neovim is reached over a local socket. There is no server and no telemetry.

### Does it work with Neovim?

Yes. The `nvim` backend drives Neovim ≥ 0.10 over msgpack-RPC, and can adopt the nvim you already have open. See [Neovim backend](guides/nvim-backend.md).

### Can it type into the Vim I already have open?

Yes, opt-in, with `adopt_existing: true`. Pause before you navigate during an animation: an adopted animation drives your live cursor. See [Adopting an editor](guides/adopting-an-editor.md).

### Do I need tmux?

For the default `tmux` backend, yes: `claude` has to run inside a tmux session, and the controls are tmux prefix keys. See [Install](getting-started/install.md).

## Limitations

- One follower per tmux window (state is keyed by tmux window id).
- Two `claude` processes in the same tmux window share that window's follower.
- Binary files are navigated to, not animated.
- On the tmux backend, a single edit hunk whose own keystrokes would take more than 60 seconds to pace is sent unpaced from that point on.
