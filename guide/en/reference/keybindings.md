🇺🇸 [English](https://albertosca.github.io/vim-ai-follower/reference/keybindings/) · 🇧🇷 [Português](https://albertosca.github.io/vim-ai-follower/pt/reference/keybindings/)

# Keybindings

`start` (and auto-open) register tmux **prefix** keybindings, restored on `stop`. Back to the [README](https://github.com/albertosca/vim-ai-follower#readme).

## tmux prefix keys

| Key           | Command      | Effect                                                                    |
| ------------- | ------------ | ------------------------------------------------------------------------- |
| `prefix` `P`  | pause        | Pause a running animation; press again to resume.                         |
| `prefix` `S`  | interrupt    | Interrupt: hand the buffer over so you can take over and save your own version. Press again during hand-off to discard your edits and resume Claude's. |
| `prefix` `+`  | speed-up     | Step the animation one notch faster (saturates at `instant`).                 |
| `prefix` `_`  | speed-down   | Step the animation one notch slower (saturates at `lento`).                 |
| `prefix` `F`  | toggle       | Mute/unmute the follower.                                                 |

How each one behaves is in the [controls guide](../guides/controls.md).

## Who owns the keys

The keybindings are tmux **server-global**, shared by every follower; each press acts only on the follower of the window it was pressed in, and the last `stop` restores your original bindings.

When another live installation (dev checkout, pip install, plugin) owns the keys, `start` leaves them there and says so; `start --take-keys` moves them to this installation (see the [CLI reference](cli.md)).
