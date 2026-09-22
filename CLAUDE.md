# vim-ai-follower — notes for Claude Code

Live "follower" editor for Claude Code: `PreToolUse`/`PostToolUse` hooks (`claude-follow hook pre|post`) snapshot a file, diff it, and animate the change into a real Vim (tmux backend, `send-keys`) or a Neovim (nvim backend, msgpack-RPC over pynvim). Ships as a Claude Code plugin (`.claude-plugin/`, `hooks/hooks.json`, slash commands in `commands/`).

## Commands

```sh
uv sync --extra dev --extra nvim            # a bare `uv sync` leaves pytest/mypy/pynvim missing
uv run ruff check src tests && uv run ruff format --check src tests
uv run mypy                                  # bare — pyproject's files = src + tests; CI runs the same
uv run pytest -q --cov=vim_ai_follower --cov-branch --cov-fail-under=100   # full suite, ~3 min, real tmux+vim+nvim
uv run pytest -q -m "not integration"        # what CI runs (~15 s); coverage floor there is 98 (unit-only is ~99%)
```

The project's bar is 100% branch coverage on the full suite; the coverage gate lives in the command line, not in pyproject. Integration tests (`@pytest.mark.integration`) need `tmux`, `vim` and `nvim` on PATH.

## Layout

- `src/vim_ai_follower/cli.py` — subcommands `start stop status hook pause interrupt speed-up speed-down toggle`; `commands.py` implements them, `keybindings.py` the tmux prefix keys.
- `hooks.py` — the hook orchestration (animate, interrupt hand-off loop, crash-fallback catch-up); `control.py` — signals + persisted pending animations (with `partial`); `state.py` — per-window follower state in `~/.cache/claude-vim-follower/`; `snapshot.py`/`diff.py` — before/after and the edit script; `animate.py` — tmux keystroke drivers.
- `backends/tmux_vim.py` (Vim via send-keys) and `backends/nvim.py` (Neovim via RPC) implement the `Follower` protocol in `backends/__init__.py`.
- Config: `~/.config/claude-vim-follower/config.json`; hook log: `~/.cache/claude-vim-follower/hook.log` (hooks never fail the tool call — problems go there).
- `qa/visual-battery.md` + `scripts/qa-check-N-*.sh` + `/qa-visual` — the manual visual QA battery (19 checks). `docs/superpowers/` is gitignored (private planning docs).

## Gotchas (each one cost a real bug)

- **nvim API columns are BYTE offsets** (`buf_set_text`, `win_set_cursor`). Anything typing per character must carry `col += len(ch.encode())`. In tests compare bytes: pynvim decodes with `surrogateescape`, so corruption looks like an ordinary string mismatch.
- **nvim follower buffers are never written to disk.** Navigation must use pure API (`list_tabpages`/`win_get_buf`/`set_current_win`), never `:edit`/`:drop`/`:buffer` — those hit Vim's abandon check (E37) or silently reload from disk. `ensure_showing` is the only disk-reading entry point; `goto_file` never reads disk.
- **An adopted nvim (`adopt_existing`) is never locked `nomodifiable`** — animation or navigation. Guard every lock with `if not self._is_adopted()`. The tmux backend locks adopted Vim like a dedicated one; that asymmetry is intentional.
- **tmux `:tab drop` ends with `:rewind`, whose abandon check runs on the buffer it just landed on** → E37 on a dirty target; wrapped in `try/catch`. Never `:tab drop!` (discards unsaved content). Swap-file dialogs are auto-answered "edit anyway" via a scoped `SwapExists` autocmd (tmux) / buffer-local `swapfile=false` before `bufload` (nvim) — `SwapExists` does not fire under `bufload`.
- **Interrupt on nvim leaves the half-typed line and rolls back the interrupted op**; `completed_count` counts whole lines/ops only. Persisted `partial` strings are always newline-terminated (`diff.apply_ops` terminates).
- **`tmux capture-pane` shows the rendered screen, not the buffer.** For content assertions dump the real buffer (`:w!` to a scratch file on Vim, `buf_get_lines` over RPC on nvim). E37/ATTENTION only become blocking prompts under 51 columns — probe at the follower pane's real width (49).
- **`tests/conftest.py`'s `headless_nvim` runs nvim with `-n` (no swap)** — anything swap-related needs the no-`-n` fixture in `tests/test_nvim_integration_swap.py`. `tmux_session` isolates from your real tmux server; never point a test at `$TMUX`.
- **`$TMUX` OVERRIDES `TMUX_TMPDIR`.** `TMUX_TMPDIR=/tmp/mine tmux kill-server` run from inside a tmux pane kills the REAL server, silently and with no output — measured 2026-09-22, when it took down a live 20-window server mid-session. Isolation needs `TMUX` unset (`env -u TMUX`, or `monkeypatch.delenv` as `tmux_session` does) — setting `TMUX_TMPDIR` alone is not isolation. This bites hand-typed teardown even when the scripts get it right, so target throwaway servers by socket AND by pid.
- **A hook resolving to a `term-*`/`iterm-*` identity means the session lost `$TMUX_PANE`** (the `bg-spare` harness bug), not that the hook never ran. Before concluding anything from a quiet log, grep it for `lost its tmux window identity`, and remember `claude-follow status` answers about the identity the CALLER resolves to — from a session without `TMUX_PANE` it will say "no follower" while the real window's follower is alive.
- **`compute_edit_script` emits ops bottom-up** (later lines first), so line numbers stay valid as ops apply; a whole-buffer delete is always the last op.
- Test-side: assert on the specific call, not `assert_called_once` — exact call counts break whenever a neighbor adds an API read.
- **Run the full suite alone.** Integration tests spawn real tmux servers and nvims; two suites in parallel (or a parallel agent's) push load past 10 and produce false timeouts — rerun the failing test alone before calling it a regression.
- **QA scripts (`scripts/qa-check-*.sh`) must run from inside a tmux pane** (`$TMUX_PANE` guard) and background a hook with `> log 2>&1 &`, never `| tee` — a pipe makes the shell wait and the interrupt/pause you're testing never lands. Finish with `source scripts/qa-lib.sh && qa_teardown`.
- **Worktrees:** tool-created worktrees may start from a stale `origin/main` — check `git rev-parse HEAD` against local `main` first. `docs/superpowers/` is gitignored, so edits made there inside a worktree never come back via merge: copy them to the main checkout before removing the worktree.

## Release

`claude plugin update` is gated on `.claude-plugin/plugin.json`'s `version`, not on git. Every user-visible fix: bump the version, commit, then `claude plugin tag --push` (creates `vim-ai-follower--vX.Y.Z`; refuses a dirty tree — never `--force`). CI is `ci.yml` (unit suite + coverage floor) and `lint.yml` (ruff + mypy); the README coverage badge is static — update it in the same commit when the measured number moves.
