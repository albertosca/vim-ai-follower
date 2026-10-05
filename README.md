🇺🇸 [English](README.md) · 🇧🇷 [Português](README.pt.md)

# Watch Claude Code type into your own Vim.

Every edit replayed line by line, at a pace you can actually read — pause it, take the keyboard, hand it back.

Built by Alberto Cavalcanti · [Connect on LinkedIn](https://www.linkedin.com/in/albertosca/) · [Read the docs](https://albertosca.github.io/vim-ai-follower/) · [Install](#install)

<!-- facts -->1100+ unit tests in CI · 100% branch coverage on the full suite · Vim + Neovim backends · v0.3.0<!-- /facts -->

[![CI](https://github.com/albertosca/vim-ai-follower/actions/workflows/ci.yml/badge.svg)](https://github.com/albertosca/vim-ai-follower/actions/workflows/ci.yml) [![Lint](https://github.com/albertosca/vim-ai-follower/actions/workflows/lint.yml/badge.svg)](https://github.com/albertosca/vim-ai-follower/actions/workflows/lint.yml) [![Coverage: 99% unit · 100% full](https://img.shields.io/badge/coverage-99%25%20unit%20%C2%B7%20100%25%20full-brightgreen.svg)](https://github.com/albertosca/vim-ai-follower/actions/workflows/ci.yml) [![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE) [![Python 3.11+](https://img.shields.io/badge/python-3.11%2B-blue.svg)](pyproject.toml) [![Claude Code plugin](https://img.shields.io/badge/Claude%20Code-plugin-8A2BE2.svg)](https://code.claude.com/docs/en/plugins)

## See it

<img src="assets/demo/follow.gif" alt="Claude writes fib.py into the follower Vim line by line; it is paused and resumed; an Edit then adds a function, animated as a diff; during the next file the keyboard is taken, a line is added and saved, which hands the turn back." width="100%">

*Scripted hook payloads, real follower — recorded with vhs from [`assets/demo/follow.tape`](assets/demo/follow.tape).*

## How it works

<picture>
  <source media="(prefers-color-scheme: dark)" srcset="assets/diagrams/how-it-works-dark.svg">
  <img alt="The edit lands on disk first; the follower only replays it; you control it — pause, or take the keyboard." src="assets/diagrams/how-it-works-light.svg" width="520">
</picture>

- Claude Code's `Edit` or `Write` lands on disk first; a `PreToolUse` hook has already snapshotted the old content.
- A `PostToolUse` hook diffs old against new and replays the change into your Vim (tmux `send-keys`) or Neovim (RPC).
- On a `Read`, the follower navigates to that file and line.
- You stay in control: `prefix` `P` pauses, `prefix` `S` takes the keyboard, and saving your version hands it back (use `:w!` if Vim reports E13).

## What it won't do

- **It never writes your files.** Follower buffers are never saved, and are locked read-only between animations (an adopted Neovim is the exception: it is your own editor, so it is never locked).
- **It never fails a Claude Code tool call.** The hooks are built to exit `0` and log problems to `hook.log` instead of failing the tool call.
- **Nothing leaves your machine.** The package imports no network module, and Neovim is reached over a local socket.

The animation does take time — that's what the speed keys and mute are for.

## Engineering decisions

vim-ai-follower types into an editor you are looking at, from hooks that run inside every Claude Code tool call. So the bar is: never corrupt what you see, never get in Claude's way, and never type into the wrong window. Each decision below bought one of those at a price; the [Engineering page](https://albertosca.github.io/vim-ai-follower/engineering/) records what each one cost and where to check it in the code.

| Decision | Trade-off accepted, and the evidence |
|---|---|
| [Two backends: tmux `send-keys` for any Vim, RPC for Neovim](https://albertosca.github.io/vim-ai-follower/engineering/#two-backends-tmux-send-keys-for-any-vim-rpc-for-neovim) | Two implementations of one follower protocol. RPC columns are byte offsets: an em dash corrupted the typed text until [`ffaed50`](https://github.com/albertosca/vim-ai-follower/commit/ffaed50), now pinned against a real Neovim |
| [Hooks never fail a tool call](https://albertosca.github.io/vim-ai-follower/engineering/#hooks-never-fail-a-tool-call) (the animation does hold the hook while it runs) | A failure is a line in `hook.log`, not an error in Claude. A dead animation is resumed from its persisted partial ([`d1620ff`](https://github.com/albertosca/vim-ai-follower/commit/d1620ff)) |
| [Window identity is recalled from `session_id` → window, never inferred](https://albertosca.github.io/vim-ai-follower/engineering/#window-identity-is-recalled-never-inferred) | When nothing proves the window, the edit is not animated. `display-message` was rejected: it answers for the window you are looking at ([`4cda388`](https://github.com/albertosca/vim-ai-follower/commit/4cda388)) |
| [The global tmux keys have an owner, so they survive a plugin update](https://albertosca.github.io/vim-ai-follower/engineering/#the-global-tmux-keys-have-an-owner) | A second live installation gets no keys until you pass `--take-keys`. The final whole-branch review found two ownership holes ([`de74e9e`](https://github.com/albertosca/vim-ai-follower/commit/de74e9e)) |
| [100% branch coverage is a gate; the visual QA battery became e2e tests](https://albertosca.github.io/vim-ai-follower/engineering/#100-branch-coverage-as-a-gate-visual-qa-turned-into-e2e-tests) | The full suite needs real tmux, Vim and Neovim, so CI runs only the unit suite. The first tranche was canaried test by test ([`431526f`](https://github.com/albertosca/vim-ai-follower/commit/431526f)), and the conversion found a bug the manual battery never showed ([`4e73cb1`](https://github.com/albertosca/vim-ai-follower/commit/4e73cb1)) |

## Install

You need tmux with `claude` running inside it, Vim or Neovim ≥ 0.10, and Python 3.11+. From Claude Code:

```
/plugin marketplace add albertosca/vim-ai-follower
/plugin install vim-ai-follower
```

Then run `/vim-ai-follower:start` inside the tmux session where `claude` runs. The default Vim backend needs no `pip` install; the Neovim extra, requirements and the manual install are in the [install guide →](https://albertosca.github.io/vim-ai-follower/getting-started/install/)

## Map

- [Follow your first edit](https://albertosca.github.io/vim-ai-follower/getting-started/first-follow/) — start the follower and watch an edit land
- [Pause, take over, speed and mute](https://albertosca.github.io/vim-ai-follower/guides/controls/) — the five tmux prefix keys that control a running animation
- [Multi-file tabs](https://albertosca.github.io/vim-ai-follower/guides/multi-file-tabs/) — one Vim tab per file, capped at `max_tabs`
- [Adopting an editor](https://albertosca.github.io/vim-ai-follower/guides/adopting-an-editor/) — let Claude type into the Vim you already have open
- [Neovim backend](https://albertosca.github.io/vim-ai-follower/guides/nvim-backend/) — drive Neovim over RPC instead of keystrokes
- [Command line](https://albertosca.github.io/vim-ai-follower/reference/cli/), [Configuration](https://albertosca.github.io/vim-ai-follower/reference/config/), [Keybindings](https://albertosca.github.io/vim-ai-follower/reference/keybindings/) — the reference
- [Engineering](https://albertosca.github.io/vim-ai-follower/engineering/) — the decisions, their costs, and where the code lives
- [FAQ](https://albertosca.github.io/vim-ai-follower/faq/) — does it slow Claude down, does it touch my files, current limitations

---

Built by Alberto Cavalcanti — [Connect on LinkedIn](https://www.linkedin.com/in/albertosca/) · MIT — see [LICENSE](LICENSE)
