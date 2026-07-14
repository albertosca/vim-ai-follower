# vim-ai-follower

*[Versão em português](README.pt.md)*

Watch Claude Code edit files **live, inside a real Vim** — no editor migration,
no GUI. A dedicated tmux pane (or a running Neovim) mirrors every file Claude
Code reads or writes, animating each change line by line so it looks like Claude
is typing into your own editor.

It installs once, wires into Claude Code's global hooks, and works from any
project where `claude` runs inside tmux.

## How it works

`claude-follow` is a single Python CLI that is both the control surface
(`start`/`stop`/`status`, pause/interrupt, speed, toggle) and the Claude Code
hook handler. On each `Edit`/`MultiEdit`/`Write`, a `PreToolUse` hook snapshots
the file's old content and a `PostToolUse` hook diffs it against the new content
and replays the change into the follower with `tmux send-keys`. On each `Read`,
the follower navigates to that file (and line).

The follower buffer is kept read-only between animations, so a stray keystroke
can never corrupt what you are watching; the animation unlocks around itself and
relocks (with a silent disk resync) when it finishes.

## Requirements

- tmux, with `claude` running inside a tmux session
- Vim (the default backend) or Neovim with an RPC socket (the `nvim_rpc` backend)
- Python 3.11+

## Install

```sh
pip install -e .          # from a clone; installs the `claude-follow` script
```

### Wire the hooks

Add these entries to `~/.claude/settings.json` so Claude Code invokes the CLI
(use the absolute path to the `claude-follow` installed in your environment):

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
    ]
  }
}
```

Hooks never block or fail a tool call — every path exits `0`, and problems go to
`~/.cache/claude-vim-follower/hook.log`, not to Claude.

## Usage

From a pane inside the tmux session where `claude` runs:

```sh
claude-follow start      # open a follower pane (or adopt an existing Vim)
claude-follow status     # show the active follower and the file it is showing
claude-follow stop       # tear the follower down and remove the keybindings
```

`start` accepts:

- `--backend {tmux,nvim_rpc}` — default `tmux`.
- `--on-failure {silent,reopen}` — what to do if the follower pane dies mid-session.
- `--speed {instant,muito_rapido,rapido,normal,lento}` — initial animation pace.

With `open_policy` set to `always` or `code` (see below), you don't even need
`start`: the first matching edit opens the follower automatically.

## Configuration

Optional JSON at `~/.config/claude-vim-follower/config.json`. Every key has a
default, and an invalid value silently falls back to it.

| Key             | Values                                                    | Default    | Meaning                                                                                 |
| --------------- | --------------------------------------------------------- | ---------- | --------------------------------------------------------------------------------------- |
| `open_policy`   | `always`, `code`, `manual`                                | `manual`   | `manual`: only `start` opens a follower. `code`: auto-open on edits/reads of code files. `always`: auto-open on any file. |
| `adopt_existing`| `true`, `false`                                           | `false`    | When opening automatically (or on `start`), reuse a Vim already running in the window instead of splitting a new pane. |
| `max_tabs`      | integer ≥ 1                                                | `5`        | How many file tabs the follower keeps. The least-recently-touched tab is closed past this limit. |
| `on_failure`    | `silent`, `reopen`                                        | `silent`   | If the follower pane dies, `reopen` re-splits a fresh one from where it started; `silent` just stops following. |
| `speed`         | `instant`, `muito_rapido`, `rapido`, `normal`, `lento`    | `rapido`   | Animation pace (seconds per line boundary: `0`, `0.01`, `0.03`, `0.08`, `0.15`).         |

## Keybindings

`start` (and auto-open) register tmux **prefix** keybindings, restored on `stop`:

| Key           | Command      | Effect                                                                    |
| ------------- | ------------ | ------------------------------------------------------------------------- |
| `prefix` `P`  | pause        | Pause a running animation; press again to resume.                         |
| `prefix` `S`  | interrupt    | Interrupt: hand the buffer over so you can take over and save your own version. Press again during hand-off to discard your edits and resume Claude's. |
| `prefix` `+`  | speed-up     | Step the animation one notch faster (round-robin, wraps).                 |
| `prefix` `_`  | speed-down   | Step the animation one notch slower (round-robin, wraps).                 |
| `prefix` `F`  | toggle       | Mute/unmute the follower.                                                 |

The keybindings are tmux **server-global**: running two followers in two tmux
sessions at once is unsupported (the first `stop` takes the keys down for both).

## Behavior details

### Multi-file tabs

Each file Claude touches gets its own Vim tab. Editing a file already open
navigates back to its tab first (by name, so it survives you closing or
reordering tabs), animates there, and returns — it never types into whatever tab
happens to be active. The tab list is recency-ordered; the least-recently-used
tab is closed once you exceed `max_tabs`. The file being animated or handed over
is never the one evicted.

### Live speed

`prefix` `+` / `prefix` `_` re-read the pace on the fly: a running animation
speeds up or slows down at its **next line boundary**, not only on the next
animation. The five speeds form a round-robin that wraps at both ends.

### Pause and interrupt

- **Pause** (`P`) holds Claude's turn in place until you resume — the animation
  finishes visually before Claude continues. If the hook process is killed
  (e.g. hook timeout) while paused, a crash-fallback lets the keyboard finish
  the animation.
- **Interrupt** (`S`) hands the buffer to you: edit it and `:w` your own version
  and Claude is told you took over (it re-reads your version from disk rather
  than restoring its own). Pressing `S` again during the hand-off discards your
  unsaved edits and resumes following the file Claude wrote.

### Mute toggle

`prefix` `F` mutes the follower: further edits are ignored and the origin pane is
zoomed so the follower gets out of the way. Pressing `F` again unmutes, unzooms,
and forces the next edit to **resync via a full retype** — the file changed on
disk while muted, so animating a diff against the stale buffer would produce
garbage. Existing tabs stay open for reading.

### Adopting an existing Vim (opt-in)

With `adopt_existing: true`, instead of splitting a new pane the follower drives
a Vim you already have open in the same tmux window. Because it is **your**
editor:

- Adoption is strictly opt-in.
- The tab you were on is never renamed over — a new file always opens in its own
  tab.
- On `stop`, adoption never kills your Vim; it only closes the tabs it opened.
- **Discipline:** pause (`P`) before you navigate around during an animation. An
  adopted animation drives your live cursor, and typing or switching tabs mid-
  animation can interleave with the injected keystrokes.
- Residual risk: the follower cannot tell your keystrokes from its own at the tty
  level, so a poorly timed edit during an unpaused animation can still land in
  the wrong place. The read-only lock protects the buffer between animations, not
  during one you interrupt by typing.

## Backends

- **`tmux`** (default): drives an unmodified Vim in a tmux pane via `send-keys`.
  All tab-based multi-file behavior above is tmux-only.
- **`nvim_rpc`**: talks to a running Neovim over msgpack-RPC (the same buffer you
  are looking at). It has no tabs — it switches buffers instead — so per-file tab
  tracking is a no-op there. The Neovim side must expose a socket via
  `vim.fn.serverstart()` at the path `claude-follow` expects (printed by
  `start --backend nvim_rpc`).

## Limitations

- One follower per tmux session (state is keyed by tmux session id).
- Two `claude` processes in the same tmux session would share, and could race on,
  one follower.
- Binary files are navigated to, not animated.
- Very large files degrade to a block paste once an animation would run long,
  rather than pacing keystroke by keystroke.
