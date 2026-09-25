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

- The buffer is left exactly as it is, unsaved typing included, and Claude's edit is not animated. The same holds for an animation that was interrupted or paused and never finished: its leftover is dropped rather than replayed over what you typed since.
- The status cue (the pane border on tmux, the floating box on Neovim) reads `buffer differs — :e! shows Claude's edit`, and the event is logged to `~/.cache/claude-vim-follower/hook.log`. The cue stays until the follower animates its next edit in that window.
- Claude's edit is already on disk. `:e!` in that buffer loads it and discards your unsaved typing there.
- Saving (`:w`) does the opposite: it writes your buffer, the old text plus your typing, over the file Claude just wrote, and Claude's edit is lost. To keep both, copy your typing somewhere else (another buffer, a register), run `:e!`, then apply your typing again.
- Once the buffer matches the file again, the next edit to it animates normally.

If the follower cannot check the buffer at all (your editor did not answer its probe, for example a Vim on the tmux backend that cannot write into `~/.cache/claude-vim-follower/`), it plays safe the same way: the edit is not shown and the cue reads `can't check buffer — edit not shown`. `:e!` does not clear that one. When the probe goes unanswered, it repeats on every edit for as long as that lasts, and `hook.log` records why. The same cue also appears once, without repeating, when a leftover from an interrupted or paused animation whose hook was stopped recorded nothing to check your buffer against: the leftover is dropped rather than replayed, your buffer is left alone, and `:e!` shows Claude's edit.

This also covers a file you opened yourself before Claude first edited it. A dedicated follower (not adopted) owns its buffers and retypes the whole file instead.

## Risks while it animates

- **Discipline:** pause (`P`) before you navigate around during an animation. An adopted animation drives your live cursor, and typing or switching tabs mid-animation can interleave with the injected keystrokes.
- Residual risk: the follower cannot tell your keystrokes from its own at the tty level, so a poorly timed edit during an unpaused animation can still land in the wrong place. The read-only lock protects the buffer between animations, not during one you interrupt by typing. That lock applies to an adopted Vim on the tmux backend; an adopted Neovim is never locked read-only, because it is your own editor.
