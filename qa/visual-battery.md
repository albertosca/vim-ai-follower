# vim-ai-follower — Visual Test Battery

The master, versioned runbook for Claude-driven visual QA: Claude sets each
check up and narrates it; Alberto watches and gives the verdict; Claude tears
down and moves on. This absorbs and supersedes `qa/smoke-runbook.md` (kept as
a stub pointing here).

**How this gets driven:** the `/qa-visual` slash command (added in a later
task) walks all 10 checks below in order against `qa/results/<date>.md`. You
can also run any single check by hand using the Setup command in its section.

**Shared harness:** `scripts/qa-lib.sh` provides `qa_run_id`,
`qa_snapshot_cache`, `qa_cache_created`, `qa_teardown`, `qa_verify_clean` —
isolated `/tmp/vaf-qa-<id>/` per run, cache-diff tracking, and a verified
final teardown. Each check's **Cleanup** below is the *per-check* portion
only (killing that check's own tmux session/pane, removing its scratch
files). The final, total teardown — `qa_teardown` followed by
`qa_verify_clean` — is run once, after the last check, by the driving slash
command; it is not repeated per check here.

- **Binary:** commands below call `claude-follow` directly. It is on `PATH`
  when the plugin is enabled; from a clone, use `./bin/claude-follow` or
  `PYTHONPATH=src .venv/bin/python -m vim_ai_follower.cli`.
- **Cache dir:** `~/.cache/claude-vim-follower/` (state `<window_id>.pane`,
  signals, logs).
- **Config file:** `~/.config/claude-vim-follower/config.json` (`backend`,
  `nvim_window`, `open_policy`, `on_failure`, `speed`, `adopt_existing`,
  `max_tabs`).

## Features under test

| # | Feature | Status |
| --- | --- | --- |
| 1 | Per-writer color cue | shipped 2026-07-17 |
| 2 | Stop restores the border | shipped 2026-07-17 (fix `c1760c8`) |
| 3 | Escape+undo race fix | shipped 2026-07-17 |
| 4 | Window-scoped identity (isolation) | shipped 2026-07-17 |
| 5 | nvim backend: char-by-char animation + floating writer cue | shipped 2026-07-19 |
| 6 | nvim backend: pause/interrupt/des-interrupt | shipped 2026-07-19 |
| 7 | Parallel-hook serialization | fixed 2026-07-23 (`e51071e`) |
| 8 | Insert-exit under a remapped `<Esc>` | `b8f43e8` 2026-07-23, superseded by `a5f6660` 2026-07-27 |
| 9 | nvim standalone (no tmux) | shipped 2026-07-23 (`fe87f5b`) |
| 10 | vim-without-tmux error | shipped 2026-07-23 (`fe87f5b`) |

---

## Prep (once, checks 1–8)

Open a tmux window and, from the pane where `claude` normally runs:

```sh
claude-follow start        # opens a follower Vim pane beside you (dedicated split)
```

**PASS:** a second (follower) pane opens. **FAIL:** an error, or no pane.

> If nothing opens, `open_policy` is `manual` (the default) — that's
> expected; `start` is required.

---

## Check 1 — Per-writer color cue

**Purpose:** confirm the follower border stays neutral for an unattributed
writer and tints + labels for an attributed one (e.g. a subagent), in real
tmux, as Alberto's eye actually sees it (not just the underlying color math).

**Setup:** from the same origin pane where you ran `start`:

```sh
zsh scripts/smoke-color-cue.sh
```

It fires two edits with distinct identities (content from
`qa/fixtures/cue-writer1.py` then `cue-writer2.py`) and pauses between them
so Alberto can look.

**What Alberto looks at:** the follower pane's border color and title bar,
at two moments — right after writer 1's edit, and right after writer 2's.

**PASS:**

| Moment | PASS | FAIL |
| --- | --- | --- |
| After writer 1 (no `agent_id`) | Border **neutral** (default); the file animates | Border already colored |
| After writer 2 (`agent_type=code-reviewer`) | Border **tints** to a color **and** the title reads `code-reviewer` | Border stays neutral, or no title |

**Cleanup:** none beyond the follower state left running for Check 2, which
consumes it directly.

---

## Check 2 — Stop restores the border

**Purpose:** confirm `claude-follow stop` restores the origin pane's border
to default before killing the follower pane — not after, and not partially.

**Setup:** immediately after Check 1, in the same pane:

```sh
claude-follow stop
```

**What Alberto looks at:** the follower pane closing, and the origin pane's
border/title bar right after.

**PASS:** the follower pane closes **and** the border returns to default —
no lingering color, no leftover border-title bar on the origin pane. Verify
programmatically at any time:

```sh
tmux show-options -wv pane-border-status    # expect: empty after stop
```

**FAIL:** the origin pane keeps a border-status bar / tinted border.

**Cleanup:** none — `stop` already returned the pane to its pre-check state.

---

## Check 3 — Escape+undo race fix

**Purpose:** confirm a pause landing mid-animation, while a real completion
popup (CoC/Copilot/vim-ai-autocomplete) is up, never leaves a stray
undo-triggered character in the buffer.

**Setup:** needs a pause landing mid-animation while a popup is visible.
Restart the follower slow so there's time to react:

```sh
claude-follow stop 2>/dev/null; claude-follow start --speed lento
```

Then drive an edit that triggers completions, either via Claude Code:

```
Cria o arquivo /tmp/vaf-qa-pause.py com a Write tool, com exatamente o
conteúdo de qa/fixtures/pause-trigger.py deste repositório (import os; uma
função collect_paths que usa os.listdir, os.path.join, .strip().lower(),
.append() e sorted(set(...))).
```

or by hand from the origin pane:

```sh
cp qa/fixtures/pause-trigger.py /tmp/vaf-qa-pause.py
P='{"tool_name":"Write","tool_input":{"file_path":"/tmp/vaf-qa-pause.py"},"session_id":"me"}'
echo "$P" | claude-follow hook pre
echo "$P" | claude-follow hook post   # animates slowly — press `prefix P` mid-line
```

Press **`prefix P`** (pause) at any moment while it types, then
**`prefix P`** again to resume (or let it finish). Do not try to aim
"mid-line": the tmux/vim backend types **line by line** by design (only the
nvim backend is char-by-char), so visually a pause always lands at a line
boundary. The race this check guards lives one level down — each line is
four key sends (opener, text, Escape, Escape) and the pause can land between
any two of them, e.g. after the `o` opened a line but before its text — and
that is exactly the case the rollback must undo cleanly.

**What Alberto looks at:** the final buffer content, line by line, focused
on the lines that were mid-type when the popup was up.

**PASS:** the final buffer is clean — no stray `u`, no `banu`-style smear;
content matches `pause-trigger.py` exactly. **FAIL:** a literal `u` (or
other junk) left in a line.

> Hermetic proof of the same fix (deterministic, no popup luck):
> `zsh scripts/repro-stray-u.sh` → prints `PASS`.

**Cleanup:**

```sh
rm -f /tmp/vaf-qa-pause.py
```

---

## Check 4 — Window-scoped identity (isolation)

**Purpose:** confirm two tmux windows, each with their own follower, never
bleed content into each other (the 2026-07-16 incident).

**Setup:** a dedicated session, driven end to end by one script:

```sh
zsh scripts/smoke-window-scoping.sh
```

⚠️ It creates the `vaf-smoke` tmux session (and **only** touches that
session — never your real server), starts a follower in **two** windows with
your real config (may prompt the macOS keychain once per follower — click
"Always Allow"), and animates a distinct file into each. It prints a
programmatic isolation check (different `window_id`s, different follower
target panes, one state file each).

Then eyeball it:

```sh
tmux attach -t vaf-smoke
```

- `prefix 0` → window 0's follower shows **only** `WINDOW ZERO` (alpha).
- `prefix 1` → window 1's follower shows **only** `WINDOW ONE` (beta).

**What Alberto looks at:** switching between windows 0 and 1 inside the
`vaf-smoke` session, checking each follower's content in turn.

**PASS:** no bleed — neither window's edits appear in the other's follower.
**FAIL:** one window's content shows up in the other's follower.

**Cleanup:**

```sh
tmux kill-session -t vaf-smoke
```

---

## Check 5 — nvim backend: char-by-char animation + floating writer cue

**Purpose:** confirm the nvim backend (real Neovim over RPC, no
`tmux send-keys`) types char-by-char with a highlighted current line and
cursor-follow, and shows a floating writer-label window for an attributed
writer (the nvim-native equivalent of Check 1's border tint — there's no
tmux border to read here).

**Setup:** `--backend nvim` overrides config for this invocation; with
`adopt_existing` at its default (`false`) this **launches a dedicated
nvim** in a split — it does **not** touch your
`~/.config/claude-vim-follower/config.json`. From the origin pane where
`claude` runs:

```sh
claude-follow start --backend nvim
```

**PASS (pane open):** a dedicated **nvim** pane opens beside you. **FAIL:**
an error, or a Vim (not nvim) pane. Then, from that same origin pane:

```sh
zsh scripts/smoke-nvim.sh
```

It fires two edits with distinct identities and pauses between them.

**What Alberto looks at:** the nvim pane while each edit types, at two
moments — during writer 1's edit, and during writer 2's.

**PASS:**

| Moment | PASS | FAIL |
| --- | --- | --- |
| Writer 1 (no `agent_id`) | Content **types in char-by-char** — current line highlighted, cursor following. No floating window yet. | Content flashes in whole, or no highlight/cursor motion |
| Writer 2 (`agent_type=code-reviewer`) | A small **floating window** appears with the label **`code-reviewer`** in a color | No floating window, or no label/color |

**Cleanup:** `claude-follow stop` — a **launched** nvim pane is owned by the
follower and stop kills it (verified live, 2026-08-25). Only an **adopted**
nvim (`adopt_existing`) is never killed; close that one yourself with `:q`.

---

## Check 6 — nvim backend: pause / interrupt / des-interrupt

**Purpose:** confirm the nvim backend's control parity with the tmux
backend — pause/resume, interrupt (hand buffer to the human), and
des-interrupt (discard human edits, replay the remaining animation) — all
driven over RPC, no `send-keys`.

**Setup:** restart the nvim follower slow so you can catch it mid-animation:

```sh
claude-follow stop 2>/dev/null; claude-follow start --backend nvim --speed lento
```

> `stop` kills a **launched** nvim pane (the follower owns it), so this also
> cleans up Check 5's leftover. Only an **adopted** nvim (your own editor,
> `adopt_existing`) is spared — that one you close with `:q` yourself.

Drive a slow edit from the origin pane:

```sh
cp qa/fixtures/pause-trigger.py /tmp/vaf-qa-nvim-pause.py
P='{"tool_name":"Write","tool_input":{"file_path":"/tmp/vaf-qa-nvim-pause.py"},"session_id":"me"}'
echo "$P" | claude-follow hook pre
echo "$P" | claude-follow hook post   # animates slowly
```

While it types, exercise the controls (same prefix keys as the tmux
backend):

**What Alberto looks at:** the nvim buffer's state and modifiability at
each of the three control points below.

**PASS:**

| Action | Key | PASS | FAIL |
| --- | --- | --- | --- |
| **Pause / resume** | `prefix P`, then `prefix P` again | Typing halts at a clean line boundary, then resumes to the exact full content | Mid-char stop, garbled resume, or lost lines |
| **Interrupt (hand-over)** | `prefix S` mid-animation | Typing stops and the buffer becomes **modifiable** — you can edit it. Edit + `:w` → your version releases Claude's turn (a notification is printed) | Buffer stays locked, or the turn never releases |
| **Des-interrupt** | after an interrupt, `prefix S` again | Your unsaved typing is discarded and the **remaining** animation replays to the exact final content — no dropped lines, even across consecutive blank lines | A dropped line, a stray blank, or a flash of the finished file |

> The consecutive-blank-line des-interrupt fidelity is the fix in `27aa0e4`.
> Automated proof (real nvim, no manual timing luck) is in
> `tests/test_nvim_integration.py` (`*des_interrupt*` / `*pace0_consume*`
> tests) — this manual check confirms it under your real nvim config.

**Note — adopt path (optional).** Checks 5/6 use the **launch** path
(turnkey, deterministic). The **adopt** path (drive an nvim you already have
open) needs `adopt_existing: true` in config and an nvim running in the
origin pane; its socket discovery + adopted-stop cleanup (fix `6c565f9`) are
covered by the automated suite. Stage it manually only if you want to
eyeball adoption.

**Cleanup:**

```sh
rm -f /tmp/vaf-qa-nvim-pause.py
```

Close the leftover nvim pane/split yourself with `:q` (not killed by `stop`).

---

## Check 7 — Parallel-hook serialization

**Purpose:** confirm six PostToolUse hooks firing near-simultaneously
(e.g. six Write tool calls in one turn) no longer interleave keystrokes into
one follower pane — before the fix (commit range starting `e51071e`,
"Serialize parallel hooks with an atomic animation-slot claim"), a
check-then-act guard on the `.animating` marker let all six hooks pass the
check and animate concurrently, producing garbled lines mixing tokens from
multiple files and literal `o`/`O` openers leaking as text.

**Setup:** reuse the existing hermetic repro script, but watch it live
instead of only reading its final verdict — it drives a real follower over a
real (isolated) tmux session and captures the pane while firing:

```sh
zsh scripts/repro-concurrent-hooks.sh
```

It builds an isolated `HOME`/tmux socket, starts the follower, then fires
six `hook post` OS processes at once against six distinct files
(`alpha.txt` … `foxtrot.txt`), snapshotting the follower pane every 0.3s
while they race, then asserts on the snapshots.

**What Alberto looks at:** the real-time animation in the terminal output
the script streams — one clean tab typing one file's content — then the
script's printed result block.

**PASS:** the follower pane ends up with exactly **one clean tab** (one
file fully typed), zero interleaved/garbled lines, zero literal `o`/`O`
openers. The script itself already asserts this and prints
`PASS: exactly one hook animated; no concurrent-animation garble` — so this
check is largely "watch the live animation, then confirm the script printed
PASS" (also visible as `TABS: 1`, `interleave: 0`, `literal_open: 0` in its
result block). **FAIL:** script prints `FAIL: concurrent-animation garble
present` and dumps a busy snapshot.

**Cleanup:** none — the script's own `trap cleanup EXIT` removes its
isolated `$HOME`/`$TMUX_TMPDIR` and kills its private tmux server.

---

## Check 8 — Insert-exit under a remapped `<Esc>`

**Purpose:** confirm the animation reliably leaves insert mode on every line,
in a config whose insert-mode `<Esc>` is remapped to something that can stay
in insert (vim-ai-autocomplete's `EscHandler`, CoC's popup close). When an
exit fails, the next line's opener (`o`/`i`) or an ex-command (`:Nd`) is typed
as LITERAL text and garbles the buffer.

> **History — read before judging this check.** Until `a5f6660` the exit was
> two Escapes plus `<C-\><C-n>`, added as insurance against exactly that
> remapped `<Esc>`. The insurance was itself corrupting the buffer on EVERY
> animation, two ways: `send_paced` puts the pace between every key, splitting
> `<C-\><C-n>` (one atomic Vim command) so `<C-N>` ran as insert-mode keyword
> completion; and after the Escapes it landed in Normal mode, where `<C-\>`
> can be a real user mapping (vim-tmux-navigator's `:TmuxNavigatePrevious`).
> The exit is now two Escapes and nothing else. Do NOT "fix" a future
> remapped-`<Esc>` garble by re-adding `<C-\><C-n>`.

**Setup — hermetic (the hazard, not the product):**

```sh
zsh scripts/repro-remapped-esc.sh
```

It proves an ACTIVE insert-mode `<Esc>` mapping really can swallow both
Escapes. It deliberately does NOT claim that reaches the product.

**Setup — the regression guard (this is the one that judges the product):**

```sh
zsh scripts/repro-exit-insert-matrix.sh
```

Runs a full animation against your REAL config and dumps the buffer **before**
the relock — necessary because the relock's `:silent! e!` reloads the correct
file from disk and would hide any corruption. Compares the shipped two-Escape
exit against the old four-key one.

**Setup — real-world eyeball (optional, alongside Check 3):**

```sh
claude-follow stop 2>/dev/null; claude-follow start --speed lento
cp qa/fixtures/pause-trigger.py /tmp/vaf-qa-esc.py
P='{"tool_name":"Write","tool_input":{"file_path":"/tmp/vaf-qa-esc.py"},"session_id":"me"}'
echo "$P" | claude-follow hook pre
echo "$P" | claude-follow hook post
```

**What Alberto looks at:** the two scripts' verdict lines; for the eyeball
variant, every line boundary, looking for a leaked `o`/`i` opener or a `:Nd`
fragment landing as text.

**PASS:**

| | PASS | FAIL |
| --- | --- | --- |
| `repro-remapped-esc.sh` | `PASS: an active insert-<Esc> mapping swallowed both Escapes, ':2d' landed as text` | `INCONCLUSIVE` (Vim behavior changed — re-check by hand) |
| `repro-exit-insert-matrix.sh` | `PASS: the two-Escape exit is clean; re-adding <C-\><C-n> corrupts the buffer` | `FAIL` (the shipped exit dirtied the buffer) or `INCONCLUSIVE` (your config lacks a Normal-mode `<C-\>` mapping, so the old variant cannot fail here — check with `:verbose map <C-Bslash>`) |
| Eyeball variant | Clean insert on every line; content matches the fixture exactly | Leaked opener/command text in the buffer |

**Cleanup:**

```sh
rm -f /tmp/vaf-qa-esc.py
```

---

## Check 9 — nvim standalone (no tmux)

**Purpose:** confirm the no-tmux path (merge `fe87f5b`, "no-tmux support:
standalone nvim follower") opens a real, visible window from a plain
terminal — not a pane inside the origin terminal — and that it survives
independently of the edit that triggered it.

**Setup:** from a plain terminal window (Terminal.app / iTerm) **outside
any tmux session**, set config to the nvim backend with the default
`nvim_window: auto`:

```sh
mkdir -p ~/.config/claude-vim-follower
cat > ~/.config/claude-vim-follower/config.json <<'EOF'
{"backend": "nvim", "nvim_window": "auto"}
EOF
```

> ⚠️ This touches your **real** config file — back up
> `~/.config/claude-vim-follower/config.json` first if you have one, and
> restore it in Cleanup below.

Then, still outside tmux:

```sh
claude-follow start
```

Trigger an edit either via a real Claude Code hook (paste in your Claude Code
session, outside tmux):

```
Cria o arquivo /tmp/vaf-qa-standalone.py com a Write tool com um algoritmo
simples reconhecível (ex: uma função de busca ou ordenação).
```

or manually, from the same terminal:

```sh
printf 'def bubble_sort(items):\n    for i in range(len(items)):\n        for j in range(len(items) - i - 1):\n            if items[j] > items[j + 1]:\n                items[j], items[j + 1] = items[j + 1], items[j]\n    return items\n' > /tmp/vaf-qa-standalone.py
P='{"tool_name":"Write","tool_input":{"file_path":"/tmp/vaf-qa-standalone.py"},"session_id":"me"}'
echo "$P" | claude-follow hook pre
echo "$P" | claude-follow hook post
```

**What Alberto looks at:** whether a separate, visible window opens (GUI
nvim-qt or VimR if installed, else a fresh Terminal.app window) beside — not
inside — the origin terminal; whether the origin terminal keeps working
while it animates; whether the window is still open once the edit finishes.

**PASS:** a visible standalone window opens, animates the content
char-by-char, and stays open after the edit completes (does not
auto-close); the origin terminal remains fully usable throughout; running
`claude-follow stop` from the origin terminal quits the standalone window
cleanly.

```sh
claude-follow stop
```

**FAIL:** no window opens, a traceback appears, the window closes itself
when the edit finishes, or the origin terminal is blocked/frozen while the
window is open.

**Cleanup:**

```sh
rm -f /tmp/vaf-qa-standalone.py
```

Restore your real config file if you backed one up before Setup (or remove
`~/.config/claude-vim-follower/config.json` if it did not exist before this
check).

> Fixture note: a dedicated `qa/fixtures/standalone-demo.py` (a short,
> visually recognizable graph/sort algorithm) is planned in a later task of
> this plan (Task 4) to replace the inline `printf` above. Until it lands,
> use the inline content or the Claude Code prompt shown here.

---

## Check 10 — vim-without-tmux error

**Purpose:** confirm that starting the **tmux** backend outside any tmux
session fails loudly and actionably instead of silently no-opping or
crashing with a traceback.

**Setup:** outside tmux, set config to the tmux backend:

```sh
mkdir -p ~/.config/claude-vim-follower
cat > ~/.config/claude-vim-follower/config.json <<'EOF'
{"backend": "tmux"}
EOF
```

> ⚠️ Same config-file caveat as Check 9 — back up/restore
> `~/.config/claude-vim-follower/config.json` around this check.

```sh
claude-follow start
echo "exit code: $?"
```

**What Alberto looks at:** the terminal output and the exit code; whether
any pane/window opened anywhere.

**PASS:** the exact message

```
claude-follow: the vim backend requires tmux — run inside a tmux session, or set backend to nvim
```

is printed (verified against `commands.VIM_NEEDS_TMUX` — as observed while
writing this check, it is printed to **stdout**, not stderr), the process
exits non-zero (observed: `1`), and no follower pane/window opens anywhere.
**FAIL:** a silent no-op (exit 0, nothing printed), a Python traceback, or
any pane/window opening.

**Cleanup:**

Restore your real config file if you backed one up before Setup (or remove
`~/.config/claude-vim-follower/config.json` if it did not exist before this
check).

---

## Result log

Superseded by `qa/results/<date>.md` (added in a later task of this plan).
The prior manual log lives in `qa/smoke-runbook.md`'s git history for
checks 1–6 up to 2026-07-21.
