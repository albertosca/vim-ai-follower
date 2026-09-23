🇺🇸 [English](https://albertosca.github.io/vim-ai-follower/) · 🇧🇷 [Português](https://albertosca.github.io/vim-ai-follower/pt/)

# vim-ai-follower

**Watch Claude Code type into your own Vim.**

Every edit replayed line by line, at a pace you can actually read — pause it, take the keyboard, hand it back.

<!-- facts -->700+ unit tests in CI · 100% branch coverage on the full suite · Vim + Neovim backends · v0.2.8<!-- /facts -->

**[Get started →](getting-started/install.md)**

## See it

<video src="https://albertosca.github.io/vim-ai-follower/assets/demo/follow.mp4" autoplay loop muted playsinline controls width="100%" aria-label="Claude writes fib.py into the follower Vim line by line; it is paused and resumed; during the next file the keyboard is taken, a line is added and saved, which hands the turn back."></video>

*Scripted hook payloads, real follower — recorded with vhs from [`assets/demo/follow.tape`](https://github.com/albertosca/vim-ai-follower/blob/main/assets/demo/follow.tape).*

## How it works

<picture>
  <source media="(prefers-color-scheme: dark)" srcset="https://albertosca.github.io/vim-ai-follower/assets/diagrams/how-it-works-dark.svg">
  <img alt="The edit lands on disk first; the follower only replays it; you control it — pause, or take the keyboard." src="https://albertosca.github.io/vim-ai-follower/assets/diagrams/how-it-works-light.svg" width="520">
</picture>

- Claude Code's `Edit` or `Write` lands on disk first; a `PreToolUse` hook has already snapshotted the old content.
- A `PostToolUse` hook diffs old against new and replays the change into your Vim (tmux `send-keys`) or Neovim (RPC).
- On a `Read`, the follower navigates to that file and line.
- You stay in control: `prefix` `P` pauses, `prefix` `S` takes the keyboard, and saving your version hands it back.

## What it won't do

- **It never writes your files.** Follower buffers are never saved, and are locked read-only between animations (an adopted Neovim is the exception: it is your own editor, so it is never locked).
- **It never fails a Claude Code tool call.** The hooks are built to exit `0` and log problems to `hook.log` instead of failing the tool call. The animation does take time — that's what the speed keys and mute are for.
- **Nothing leaves your machine.** The package imports no network module, and Neovim is reached over a local socket.

## Built with care

Two backends, hooks that never fail a tool call, a window identity that is recalled instead of guessed, and a 100% branch-coverage gate backed by end-to-end tests through the real CLI. The decisions, and what each one cost, are on the [Engineering](engineering.md) page.

## Install

```
/plugin marketplace add albertosca/vim-ai-follower
/plugin install vim-ai-follower
```

Then run `/vim-ai-follower:start` inside the tmux session where `claude` runs. Requirements, the Neovim extra and the manual install are in the [install guide](getting-started/install.md).

## Where to go next

- [Install](getting-started/install.md) — requirements, the plugin, the manual install
- [Follow your first edit](getting-started/first-follow.md) — start the follower and watch an edit land
- [Pause, take over, speed and mute](guides/controls.md) — the five tmux keys that control a running animation
- [Multi-file tabs](guides/multi-file-tabs.md) — one Vim tab per file, capped at `max_tabs`
- [Adopting an editor](guides/adopting-an-editor.md) — let Claude type into the Vim you already have open
- [Neovim backend](guides/nvim-backend.md) — drive Neovim over RPC instead of keystrokes
- [Command line](reference/cli.md), [Configuration](reference/config.md), [Keybindings](reference/keybindings.md) — the reference
- [FAQ](faq.md) — does it slow Claude down, does it touch my files, current limitations

---

Built by Alberto Cavalcanti — [Connect on LinkedIn](https://www.linkedin.com/in/albertosca/) · [Source on GitHub](https://github.com/albertosca/vim-ai-follower) · MIT license
