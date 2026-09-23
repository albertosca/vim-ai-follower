🇺🇸 [English](https://albertosca.github.io/vim-ai-follower/guides/controls/) · 🇧🇷 [Português](https://albertosca.github.io/vim-ai-follower/pt/guides/controls/)

# Pause, take over, speed and mute

Four tmux prefix keys control a running follower: pause, interrupt (take the keyboard), speed and mute. Each press acts only on the follower of the window it was pressed in. The full key table is in [Keybindings](../reference/keybindings.md). Back to the [README](https://github.com/albertosca/vim-ai-follower#readme).

## How to pause Claude Code's edits in Vim

**Pause** (`P`) holds Claude's turn in place until you resume — the animation finishes visually before Claude continues.

1. While an edit is animating, press `prefix` `P`.
2. Read, scroll back, think. Claude's turn waits.
3. Press `prefix` `P` again to resume.

If the hook process is killed (e.g. hook timeout) while paused, a crash-fallback lets the keyboard finish the animation.

## How to take over an edit mid-animation

**Interrupt** (`S`) hands the buffer to you: edit it and save your own version, and Claude is told you took over (it re-reads your version from disk rather than restoring its own).

1. While an edit is animating, press `prefix` `S`. The follower hands you the buffer.
2. Edit it, then save your version to hand it back. Claude is told you took over, and re-reads your version from disk.
3. Changed your mind? Pressing `S` again during the hand-off discards your unsaved edits and resumes following the file Claude wrote.

## How to speed up or slow down the animation

`prefix` `+` / `prefix` `_` re-read the pace on the fly: a running animation speeds up or slows down at its **next line boundary**, not only on the next animation. The scale saturates at both ends; the popup labels the limits (`lento (slowest)`, `instant (fastest)`).

1. Press `prefix` `+` for one notch faster, or `prefix` `_` for one notch slower.
2. To choose the pace a follower starts with, pass `--speed` to [`start`](../reference/cli.md) or set `speed` in the [configuration file](../reference/config.md).

## How to mute the follower

`prefix` `F` mutes the follower: further edits are ignored and the origin pane is zoomed so the follower gets out of the way. Pressing `F` again unmutes, unzooms, and forces the next edit to **resync via a full retype** — the file changed on disk while muted, so animating a diff against the stale buffer would produce garbage. Existing tabs stay open for reading.

1. Press `prefix` `F` (or run `/vim-ai-follower:toggle`) to mute.
2. Press it again to unmute; the next edit retypes the whole file.
