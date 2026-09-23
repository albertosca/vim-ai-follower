🇺🇸 [English](https://albertosca.github.io/vim-ai-follower/engineering/) · 🇧🇷 [Português](https://albertosca.github.io/vim-ai-follower/pt/engineering/)

# Engineering

vim-ai-follower types into an editor you are looking at, from hooks that run inside every Claude Code tool call. So the bar is: never corrupt what you see, never get in Claude's way, and never type into the wrong window. This page records the decisions behind that bar, what each one cost, and where to check it in the code. Back to the [README](https://github.com/albertosca/vim-ai-follower#readme).

## How vim-ai-follower works

<picture>
  <source media="(prefers-color-scheme: dark)" srcset="https://albertosca.github.io/vim-ai-follower/assets/diagrams/how-it-works-dark.svg">
  <img alt="The edit lands on disk first; the follower only replays it; you control it — pause, or take the keyboard." src="https://albertosca.github.io/vim-ai-follower/assets/diagrams/how-it-works-light.svg" width="520">
</picture>

*The edit lands on disk first; the follower only replays it; you control it — pause, or take the keyboard.*

`claude-follow` is a single Python CLI that is both the control surface (`start`/`stop`/`status`, pause/interrupt, speed, toggle) and the Claude Code hook handler. On each `Edit`/`MultiEdit`/`Write`, a `PreToolUse` hook snapshots the file's old content and a `PostToolUse` hook diffs it against the new content and replays the change into the follower with `tmux send-keys` (or over RPC, on the [nvim backend](guides/nvim-backend.md)). On each `Read`, the follower navigates to that file (and line).

The follower buffer is kept read-only between animations (an adopted Neovim is the exception: it is your own editor, so it is never locked), so a stray keystroke can never corrupt what you are watching; the animation unlocks around itself and relocks (with a silent disk resync) when it finishes.

## Decisions and the trade-offs accepted

| Decision | Trade-off accepted |
|---|---|
| Two backends: tmux `send-keys` for any Vim, RPC for Neovim | Two implementations of one follower protocol, and RPC columns are byte offsets |
| Hooks never fail a tool call | A failure is a line in `hook.log`, not an error you see in Claude |
| Window identity is recalled, never inferred | When nothing proves the window, the edit is not animated at all |
| The global tmux keys have an owner | A second live installation gets no keys until you pass `--take-keys` |
| 100% branch coverage as a gate, visual QA turned into e2e tests | The full suite needs real tmux, Vim and Neovim, so CI runs only the unit suite |

### Two backends: tmux send-keys for any Vim, RPC for Neovim

The `tmux` backend drives an unmodified Vim by typing into its pane with `send-keys`, so it works with any Vim and any config. The `nvim` backend drives Neovim entirely over msgpack-RPC, so the keystroke-corruption bug class the tmux backend has to fight does not exist there. The cost is a second implementation of the same `Follower` protocol, and RPC has its own traps: `nvim_buf_set_text` and `nvim_win_set_cursor` take **byte** columns. The animation walked lines by character, which matches only while a line stays ASCII; an em dash turned `alpha — beta` into `b'alpha \xe2 beta\x80\x94'`, and Portuguese accents hit it on nearly every line. Fixed in [`ffaed50`](https://github.com/albertosca/vim-ai-follower/commit/ffaed50), pinned against a real headless Neovim by `test_show_fresh_types_multi_byte_content_byte_identically` in [`tests/test_nvim_integration_utf8.py`](https://github.com/albertosca/vim-ai-follower/blob/main/tests/test_nvim_integration_utf8.py).

### Hooks never fail a tool call

The hooks are built to exit `0` and log problems to `~/.cache/claude-vim-follower/hook.log` instead of failing the tool call. An animation can still die mid-way — a hook timeout kills the process while you have it paused. The remainder and the partly typed text are persisted to disk, so the next hook rebuilds the buffer from that partial and catches up instead of reinterpreting a half-typed line ([`d1620ff`](https://github.com/albertosca/vim-ai-follower/commit/d1620ff); end to end through the real CLI in `test_crash_fallback_catches_up_after_the_hook_is_killed`, [`tests/test_e2e_cli_controls.py`](https://github.com/albertosca/vim-ai-follower/blob/main/tests/test_e2e_cli_controls.py)). The trade-off: when something goes wrong, the tool call still succeeds and the only trace is the log.

### Window identity is recalled, never inferred

A follower belongs to one tmux window, and a hook has to know which. Normally `TMUX_PANE` says so, but a session can lose it mid-run. The obvious fallback, `tmux display-message -p '#{window_id}'`, was measured and rejected: it answers for the window the attached client is looking at, not the one the hook came from ([`4cda388`](https://github.com/albertosca/vim-ai-follower/commit/4cda388)). Instead the hook walks its process ancestry to a pane, and remembers `session_id` → window while it can still prove it, re-proving the tmux server before trusting a stored window ([`b1a61f0`](https://github.com/albertosca/vim-ai-follower/commit/b1a61f0), [`8c46e02`](https://github.com/albertosca/vim-ai-follower/commit/8c46e02)). `test_a_binding_is_never_reused_across_a_tmux_server_restart` in [`tests/test_integration_session_binding.py`](https://github.com/albertosca/vim-ai-follower/blob/main/tests/test_integration_session_binding.py) pins the guard against a real tmux server. The trade-off: when nothing proves the window, the edit is not animated and the log says the session lost its tmux window identity — a wrong guess would type one project's file into another project's editor.

### The global tmux keys have an owner

The prefix keys are server-global and embed an absolute path to the `claude-follow` that bound them, so a plugin update, a dev checkout and a pip install can fight over them. Each installation now records itself as the keys' owner; a hook re-points the keys after a plugin update, a dead owner's keys are taken over, and a live foreign owner keeps them unless you pass `--take-keys` ([`59e22d1`](https://github.com/albertosca/vim-ai-follower/commit/59e22d1), [`a9368cb`](https://github.com/albertosca/vim-ai-follower/commit/a9368cb)). The final whole-branch review then found two ownership holes — among them, the `/start` command and the hooks recorded the same plugin as two different installations, so the keys were never re-pointed after an update ([`de74e9e`](https://github.com/albertosca/vim-ai-follower/commit/de74e9e); `test_hook_post_repoints_keys_after_a_plugin_update` in [`tests/test_keybindings_ownership.py`](https://github.com/albertosca/vim-ai-follower/blob/main/tests/test_keybindings_ownership.py)).

### 100% branch coverage as a gate, visual QA turned into e2e tests

The full suite fails below 100% branch coverage. The cost is a test for every new branch, and a suite that needs real tmux, Vim and Neovim: it takes about seven minutes and runs locally, while CI runs the unit suite. Coverage cannot see what a user actually runs — the `bin/claude-follow` wrapper as a subprocess, hook payloads as JSON on stdin — so the manual visual QA battery was converted into e2e tests through the real CLI, each in an isolated world with its own `HOME` and tmux server ([`431526f`](https://github.com/albertosca/vim-ai-follower/commit/431526f); every test in that first tranche was canaried by reverting the fix it guards). The conversion found a bug the manual battery never showed, because its check fired a single edit: on Neovim the "Writing..." cue appeared on the first animation only. A malformed `:bwipeout!` caused it, and the unit test had hidden it by modelling the buffer as a bare integer ([`4e73cb1`](https://github.com/albertosca/vim-ai-follower/commit/4e73cb1); `test_first_animation_shows_writing_and_keeps_the_typing_highlight` in [`tests/test_e2e_battery_tranche2.py`](https://github.com/albertosca/vim-ai-follower/blob/main/tests/test_e2e_battery_tranche2.py) now drives a second animation).

## Where the code lives

| Module | Purpose |
|---|---|
| `cli.py`, `commands.py`, `keybindings.py` | Subcommands, their implementation, the tmux prefix keys |
| `hooks.py` | Hook orchestration: animate, interrupt hand-off, crash-fallback catch-up |
| `control.py` | Signals and persisted pending animations (with their partial) |
| `binding.py` | The `session_id` → window store |
| `state.py` | Per-window follower state |
| `snapshot.py`, `diff.py` | Before/after snapshots and the edit script |
| `backends/tmux_vim.py`, `backends/nvim.py` | The two implementations of the `Follower` protocol |

All under [`src/vim_ai_follower/`](https://github.com/albertosca/vim-ai-follower/tree/main/src/vim_ai_follower). Objections and limits are in the [FAQ](faq.md).
