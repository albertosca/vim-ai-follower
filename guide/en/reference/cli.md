🇺🇸 [English](https://albertosca.github.io/vim-ai-follower/reference/cli/) · 🇧🇷 [Português](https://albertosca.github.io/vim-ai-follower/pt/reference/cli/)

# Command line

`claude-follow` is a single Python CLI that is both the control surface (`start`/`stop`/`status`, pause/interrupt, speed, toggle) and the Claude Code hook handler. Back to the [README](https://github.com/albertosca/vim-ai-follower#readme).

## Slash commands (plugin install)

**If you installed the plugin**, drive it with the slash commands, from a pane inside the tmux session where `claude` runs: `/vim-ai-follower:start`, `/vim-ai-follower:status`, `/vim-ai-follower:stop`, `/vim-ai-follower:toggle`. `/vim-ai-follower:start` passes its arguments straight through to `claude-follow start` (flags below), e.g. `/vim-ai-follower:start --backend nvim --speed lento`.

## Subcommands

| Command | What it does |
|---|---|
| `claude-follow start` | Open a follower pane (or adopt an existing Vim). A launched nvim that does not answer within 10 s is reported, its pane left open with nvim's own error, and nothing is recorded (exit 1). |
| `claude-follow status` | Show the active follower and the file it is showing. |
| `claude-follow stop` | Tear the follower down and remove the keybindings. It stays stopped for this window until `start` (or toggle) — auto-open does not reopen it. It is bounded: an nvim that does not answer (busy or frozen) is sent nothing, and neither is an nvim of your own (adopted) waiting at a prompt (one line in `hook.log` says so); the follower's own nvim at a prompt has the prompt dismissed and is quit, an nvim that stops answering mid-teardown holds `stop` for about 13 s at most, and a follower nvim that does not quit has its own pane closed. A pane is closed or typed into only once it is proven to be the follower's: a state left by an earlier tmux server (pane ids start again after a restart), or naming a pane that no longer runs Vim, is cleared with one `hook.log` line and nothing sent. |
| `claude-follow pause` | Pause or resume a running animation (bound to `prefix` `P`). |
| `claude-follow interrupt` | Hand the buffer over to you, or discard your hand-off edits (bound to `prefix` `S`). |
| `claude-follow speed-up` / `speed-down` | Step the animation pace (bound to `prefix` `+` / `prefix` `_`). |
| `claude-follow toggle` | Mute/unmute the follower (bound to `prefix` `F`). After a `stop`, it turns auto-open back on for the window without opening anything. |
| `claude-follow hook pre` / `hook post` / `hook failure` | The hook handler Claude Code calls; reads the hook payload as JSON on stdin. `hook failure` runs when an edit fails or is denied, so a Read of that file is no longer held back. |

The keys are described in [Keybindings](keybindings.md).

## `start` flags

`start` accepts:

- `--backend {tmux,nvim}` — default `tmux`.
- `--on-failure {silent,reopen}` — what to do if the follower pane dies mid-session.
- `--speed {instant,muito_rapido,rapido,normal,lento}` — initial animation pace.
- `--take-keys` — move the tmux prefix keys to this installation even when another live installation (dev checkout, pip install, plugin) owns them; without it, `start` leaves them there and says so.

Without a flag, `start` falls back to the matching key in the [configuration file](config.md).

## How to run claude-follow from your own shell

The bundled `claude-follow` wrapper is deliberately not on your shell's `PATH` — it only runs from Claude Code's own Bash tool and from the tmux keybindings (see `bin/claude-follow`'s header comment). If you also want to run `claude-follow` directly from your own shell, symlink the bundled wrapper onto any directory on your `PATH` (e.g. `~/.local/bin`) — the version directory varies with whatever the plugin last downloaded, so list it first:

```sh
ls ~/.claude/plugins/cache/vim-ai-follower/vim-ai-follower/                       # find the installed <version>
ln -s ~/.claude/plugins/cache/vim-ai-follower/vim-ai-follower/<version>/bin/claude-follow ~/.local/bin/claude-follow
```

**If you used the manual/development install**, `pip install -e .` already put `claude-follow` on your shell's `PATH`.
