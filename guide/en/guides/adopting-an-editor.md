🇺🇸 [English](https://albertosca.github.io/vim-ai-follower/guides/adopting-an-editor/) · 🇧🇷 [Português](https://albertosca.github.io/vim-ai-follower/pt/guides/adopting-an-editor/)

# Adopting an existing Vim (opt-in)

With `adopt_existing: true`, instead of splitting a new pane the follower drives a Vim you already have open in the same tmux window. Back to the [README](https://github.com/albertosca/vim-ai-follower#readme).

## How to make Claude Code type into the Vim you already have open

1. Set `"adopt_existing": true` in `~/.config/claude-vim-follower/config.json` (see [Configuration](../reference/config.md)).
2. Open Vim (or Neovim, for the [nvim backend](nvim-backend.md)) in the tmux window where `claude` runs.
3. Run `/vim-ai-follower:start`, or let `open_policy` auto-open it on the first edit.

## What adoption guarantees

Because it is **your** editor:

- Adoption is strictly opt-in.
- The tab you were on is never renamed over — a new file always opens in its own tab.
- On `stop`, adoption never kills your Vim; it only closes the tabs it opened.

## When your buffer is not what Claude edited

The follower animates Claude's edit as a diff onto the buffer that holds the file. In your own editor that buffer may hold something else: lines you typed and have not saved, or an older copy of a file that a formatter or `git checkout` rewrote. Retyping the file would first wipe that buffer, and a diff would land on the wrong text, so an adopted editor gets neither:

- The buffer is left exactly as it is, unsaved typing included, and Claude's edit is not animated.
- The status cue (the pane border on tmux, the floating box on Neovim) reads `buffer differs — :e! shows Claude's edit`, and the event is logged to `~/.cache/claude-vim-follower/hook.log`. The cue stays until the follower animates its next edit in that window.
- Claude's edit is already on disk. Run `:e!` in that buffer to load it (that discards your unsaved typing there), or save your own version first.
- Once the buffer matches the file again, the next edit to it animates normally.

This also covers a file you opened yourself before Claude first edited it. A dedicated follower (not adopted) owns its buffers and retypes the whole file instead.

## Risks while it animates

- **Discipline:** pause (`P`) before you navigate around during an animation. An adopted animation drives your live cursor, and typing or switching tabs mid-animation can interleave with the injected keystrokes.
- Residual risk: the follower cannot tell your keystrokes from its own at the tty level, so a poorly timed edit during an unpaused animation can still land in the wrong place. The read-only lock protects the buffer between animations, not during one you interrupt by typing. That lock applies to an adopted Vim on the tmux backend; an adopted Neovim is never locked read-only, because it is your own editor.
