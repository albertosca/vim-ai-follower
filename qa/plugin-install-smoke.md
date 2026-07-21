# Plugin-install Smoke Runbook

Manual QA that the Claude Code plugin installs and wires itself correctly —
the part `pytest` can't cover (real `claude` plugin loading, real tmux). Run
after any change to `.claude-plugin/`, `hooks/hooks.json`, `bin/claude-follow`,
`commands/`, or `keybindings._claude_follow_executable`.

- **Repo:** `the repo root`
- **No pip install needed** for the tmux backend — the plugin bundles the CLI.

## Fast structural check (no tmux)

```sh
cd the repo root
claude plugin validate .                                   # expect: ✔ Validation passed
CLAUDE_PLUGIN_ROOT="$PWD" PYTHONPATH="$PWD/src" python3 -m vim_ai_follower.cli status </dev/null
#   expect: "claude-follow: not running inside tmux" (the bundled CLI runs, no install)
./bin/claude-follow status </dev/null                      # same output via the wrapper
```

## Live install check (dedicated tmux session)

⚠️ Uses a dedicated `vaf-plugin` tmux session — never touches your own server.

```sh
tmux new-session -d -s vaf-plugin -x 200 -y 50
tmux attach -t vaf-plugin
```

Inside that session, start Claude with the plugin loaded from the working tree:

```sh
claude --plugin-dir the repo root
```

| Check | PASS |
| --- | --- |
| `/help` lists `/vim-ai-follower:start` `:stop` `:status` `:toggle` | the four commands appear namespaced |
| Ask Claude to Write a small file | a follower pane opens and the content animates (hooks fired automatically — no settings.json) |
| `prefix P` while it animates | the follower shows the paused state (keybinding resolved via `${CLAUDE_PLUGIN_ROOT}/bin/claude-follow`) |
| `~/.cache/claude-vim-follower/hook.log` | shows `python3 -m vim_ai_follower.cli` invocations, no tracebacks |

**Teardown:**

```sh
./bin/claude-follow stop 2>/dev/null
tmux kill-session -t vaf-plugin
```

## From-marketplace check (after the repo is pushed)

```
/plugin marketplace add albertosca/vim-ai-follower
/plugin install vim-ai-follower
```

Then repeat the live table above. This is the real end-user path; run it once
the GitHub repo exists (private is fine — Claude Code installs from a private
repo with your git credentials).

## Result log

| Date | validate | bundled CLI | live install | prefix P | Notes |
| --- | --- | --- | --- | --- | --- |
| 2026-07-21 | ✓ | ✓ | | | Structural checks green during build; live/marketplace run pending. |
