# vim-ai-follower — Visual Test Battery

The master, versioned runbook for Claude-driven visual QA: Claude sets each
check up and narrates it; Alberto watches and gives the verdict; Claude tears
down and moves on. This absorbs and supersedes `qa/smoke-runbook.md` (kept as
a stub pointing here).

**How this gets driven:** the `/qa-visual` slash command walks all 19 checks
below in order against `qa/results/<date>.md`. You can also run any single
check by hand using the Setup command in its section.

**Shared harness:** `scripts/qa-lib.sh` provides `qa_run_id`,
`qa_snapshot_cache`, `qa_cache_created`, `qa_teardown`, `qa_verify_clean`,
`qa_protect_config`/`qa_write_test_config`/`qa_config_handoff`, and — for the
checks added after run 2 — `qa_window_id`, `qa_follower_target`,
`qa_dump_nvim_buffer`, `qa_dump_vim_buffer`. Isolated `/tmp/vaf-qa-<id>/` per
run, cache-diff tracking, and a verified final teardown. Each check's
**Cleanup** below is the *per-check* portion only (killing that check's own
tmux session/pane, removing its scratch files). The final, total teardown —
`qa_teardown` followed by `qa_verify_clean` — is run once, after the last
check, by the driving slash command; it is not repeated per check here.

> **Read the buffer, never the screen.** `tmux capture-pane` shows the
> RENDERED pane: wrapped lines, truncation, CoC/inlay virtual text. It has
> twice been mistaken for buffer corruption in this project. Every verdict
> below that is about CONTENT is settled with `qa_dump_nvim_buffer <socket>
> <file>` (nvim, over RPC) or `qa_dump_vim_buffer <pane>` (tmux, via a `:w!`
> to a scratch path), both of which each check prints ready to paste.

> **Never pipe a check script that backgrounds `hook post`.** Checks 3, 9,
> 12, 13, 14, 15 and 16 do, most of them to leave an animation running so the
> controls can be exercised. If you run them as `zsh scripts/... | tee log`,
> the shell waits
> for the pipe to close, which only happens when the backgrounded hook exits
> — so the prompt does not come back and no `prefix P`/`prefix S` you type
> lands until the animation is already over (measured 2026-09-21: an entire
> verification round read as a pass that way, with the interrupt never
> landing). Redirect instead: `zsh scripts/... > /tmp/vaf-qa-run.log 2>&1`,
> then read the file.

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
| 2 | Stop restores the border | shipped 2026-07-17 (fix `c1e1490`) |
| 3 | Escape+undo race fix | shipped 2026-07-17 |
| 4 | Window-scoped identity (isolation) | shipped 2026-07-17 |
| 5 | nvim backend: char-by-char animation + floating writer cue | shipped 2026-07-19 |
| 6 | nvim backend: pause/interrupt/des-interrupt | shipped 2026-07-19 |
| 7 | Parallel-hook serialization | fixed 2026-07-23 (`e51071e`) |
| 8 | Insert-exit under a remapped `<Esc>` | `b8f43e8` 2026-07-23, superseded by `a5f6660` 2026-07-27 |
| 9 | nvim standalone (no tmux) | shipped 2026-07-23 (`fe87f5b`), iTerm2 split-pane fallback added 2026-09-04 |
| 10 | vim-without-tmux error | shipped 2026-07-23 (`fe87f5b`) |
| 11 | nvim backend: real tabs for multiple files | shipped 2026-09-15 (`7abf023`…`142b76d`) |
| 12 | "Writing…" cue on every animation + static gruvbox | shipped 2026-09-15 (`28937f0`, `0998397`, `c42b77a`) |
| 13 | nvim: interrupt leaves the half-typed line; an interrupted op rolls back | shipped 2026-09-16 (`1d2b5a9`, `6bef225`) |
| 14 | Interrupt / des-interrupt hand-off CYCLES (both backends) | shipped 2026-09-16 (`5b87c34`) |
| 15 | Crash-fallback catch-up rebuilds from the persisted partial (both backends) | shipped 2026-09-16 (`d1620ff`, `7a470c1`, `4362526`) |
| 16 | tmux: no E37 hit-enter prompt navigating to a dirty buffer | shipped 2026-09-21 (`511ceb6`) |
| 17 | tmux: swap-file ATTENTION answered "(E)dit anyway" | shipped 2026-09-21 (`e0bb5be`) |
| 18 | nvim: a swap-held file opens instead of crashing the hook | shipped 2026-09-21 (`d817f08`) |
| 19 | nvim: Read navigation shows disk content, `goto_line` clamps (+ adopt note) | shipped 2026-09-16 (`98b6201`), adopt rule `779284b` 2026-09-21 |

**Machine-verified (2026-09-29).** Checks 2, 3, 4, 6, 7 and 10 have no manual step left: each is an e2e test in `tests/test_e2e_battery_tranche3.py`, and a battery run records them from that test instead of running anything. Checks 1, 5, 8 and 9 keep only the part a test cannot judge (the colour in your theme, the smoothness, your own plugin stack, a real iTerm2 split).

---

## Prep (once, before Check 1)

Open a tmux window and, from the pane where `claude` normally runs:

```sh
claude-follow start        # opens a follower Vim pane beside you (dedicated split)
```

**PASS:** a second (follower) pane opens. **FAIL:** an error, or no pane.

> If nothing opens, `open_policy` is `manual` (the default) — that's
> expected; `start` is required.

## Prep (checks 11–19)

No shared prep: every one of these checks restarts the follower itself, on
the backend it needs, as its first step. What they DO need is on the table
below — read it before dispatching them, because two of them are vacuous in
the wrong geometry and two need a second editor on screen.

| Check | Backend | Needs |
| --- | --- | --- |
| 11 | nvim (`--backend nvim`) | nothing beyond a tmux window |
| 12 | nvim, `--speed lento` | a FRESH nvim (the script restarts one — the colorscheme is forced once per nvim process, so re-running against an nvim that already animated measures nothing) |
| 13 | nvim, `--speed lento` | two invocations: `line` then `op`; let `line` finish before `op` |
| 14 | either (`nvim` \| `tmux` argument) | run twice, once per backend; on tmux the whole animation is much shorter (line-granular sends, not char-by-char), so press the cycles briskly |
| 15 | either (`nvim` \| `tmux` argument) | run twice, once per backend; fully script-driven, no keys to press |
| 16 | tmux | a window wide enough for the follower split to be narrowed to **49 columns** — E37 is 51 characters and only escalates to a hit-enter prompt when it WRAPS. The script resizes and prints the width; at ≥51 it warns, and a clean pane then proves nothing |
| 17 | tmux | same 49-column geometry, plus a **second real Vim** which the script opens in its own pane; two invocations, `live` then `stale` |
| 18 | nvim | a **second real nvim** which the script opens in its own pane (a Vim will not do — nvim's default swap dir is `~/.local/state/nvim/swap//`, Vim's starts with `.`) |
| 19 | nvim | nothing; the optional adopt note at the end needs `adopt_existing: true` and is not scripted |

None of checks 11–19 writes `~/.config/claude-vim-follower/config.json`;
every one of them takes the backend from a `--backend` flag on `start`
instead. Only Check 9 touches the real config file (Check 10 is machine-verified).

---

## Check 1 — Per-writer color cue

**Purpose:** confirm the per-writer tint is VISIBLE in your own terminal theme — the one part of this cue no test can judge.

**Machine-verified** by `tests/test_e2e_battery_tranche3.py::test_second_writer_tints_and_titles_the_follower_border_and_stop_restores_it` (through the real wrapper: a lone writer never tints the follower border, sampled while it animates; a second writer, a subagent, sets `fg=colour78` on both border styles of the follower pane, turns the window's `pane-border-status` to `top` and titles the pane `code-reviewer`). `show-options` proves the value, not the pixels — so the manual pass is a quick look.

**Setup:** from the same origin pane where you ran `start`:

```sh
zsh scripts/smoke-color-cue.sh
```

It fires two edits with distinct identities (content from `qa/fixtures/cue-writer1.py` then `cue-writer2.py`) and pauses between them.

**What Alberto looks at:** the follower pane's border right after writer 2's edit.

**PASS:** in your theme, the tinted border and its `code-reviewer` title are clearly distinguishable from the neutral border. **FAIL:** the tint is hard to tell from neutral, or the title bar does not render.

**Cleanup:** `claude-follow stop` (Check 2 no longer consumes this state).

---

## Check 2 — Stop restores the border

**Purpose:** `claude-follow stop` clears the writer cue BEFORE killing the follower pane, so the window's `pane-border-status` is not left stuck on `top` (fix `c1e1490`). The tint lives on the **follower** pane — the one the cue colours — and `pane-border-status` is a window option, which is why the clear has to run while the pane still exists.

**Machine-verified, no manual step** by the tail of Check 1's test (`tests/test_e2e_battery_tranche3.py::test_second_writer_tints_and_titles_the_follower_border_and_stop_restores_it`): `stop` through the wrapper leaves exactly one pane (the origin) and the window's `pane-border-status` unset, after asserting it read `top` before the stop — that window option is the discriminating assertion. The test also reads the origin pane's border styles as unset, but the cue only ever tints the follower pane, so those reads would pass without the fix; they only show that `stop` did not tint the origin. Nothing to run by hand.

---

## Check 3 — Escape+undo race fix

**Purpose:** a pause or interrupt landing mid-line, while a completion popup (CoC/Copilot/vim-ai-autocomplete) is up, never leaves a stray undo-triggered character in the buffer (`banu`). The rollback sends two Escapes then `u`; a popup-close map (`inoremap <expr> <Esc> pumvisible() ? "\<C-e>" : "\<Esc>"`) used to eat the first Escape and let the `u` land as text.

**Machine-verified, no manual step** by `tests/test_e2e_battery_tranche3.py::test_paste_is_on_for_the_whole_animation_and_off_after_the_relock`: a key-free timer in the follower's own Vim samples `'paste'` mid-animation and finds it on from the first typed line through every later state up to the finished file, then off once the relock has run. `'paste'` disables every insert-mode mapping, so the popup-close map cannot fire while the follower types; a canary that drops `paste` from the unlock turns the test red. The Escape half stays proven with a real built-in completion popup by `zsh scripts/repro-stray-u.sh` (prints `PASS`), kept as the mechanism proof. Nothing to run by hand.

---

## Check 4 — Window-scoped identity (isolation)

**Purpose:** two tmux windows, each with its own follower, never bleed content into each other (the 2026-07-16 incident).

**Machine-verified, no manual step** by `tests/test_e2e_battery_tranche3.py::test_two_windows_never_bleed_content_into_each_others_follower` (a follower in each of two windows of one tmux server, a distinct file written through each window's own hooks; each follower's Vim lists exactly its own file and holds exactly its bytes, read from the buffers rather than the screen). `scripts/smoke-window-scoping.sh` stays available for a live look with your real config, but is no longer part of a battery run.

---

## Check 5 — nvim backend: char-by-char animation + floating writer cue

**Purpose:** a 10-second look at what a test cannot judge on the nvim backend: that the typing READS as smooth and the writer tint is visible in your colourscheme.

**Machine-verified** by `tests/test_e2e_battery_tranche3.py::test_nvim_types_char_by_char_with_the_cursor_and_shows_the_writer_float` (atomic RPC snapshots at `lento`: a line is caught part-way typed — the same run at `instant` fails that — the cursor sits on the `VafTypingLine` row, a lone writer's float carries no writer label or tint, and a subagent's float shows `code-reviewer` in its title with `FloatBorder:VafWriterCue`, whose gui colour is colour78's `#5fd787`).

**Setup:** from the origin pane where `claude` runs (`--backend nvim` overrides the config for this invocation and launches a dedicated nvim):

```sh
claude-follow start --backend nvim
zsh scripts/smoke-nvim.sh
```

It fires two edits with distinct identities and pauses between them.

**What Alberto looks at:** the nvim pane for about ten seconds of each edit.

**PASS:**

| Moment | PASS | FAIL |
| --- | --- | --- |
| Writer 1 (no `agent_id`) | The typing reads as smooth character-by-character motion. The float shows the default title with a `Writing...` body — no writer label, no tint | Jerky or whole-line jumps |
| Writer 2 (`agent_type=code-reviewer`) | The float's `code-reviewer` title and its border tint are visible against your colourscheme | The tint is invisible or unreadable |

**Cleanup:** `claude-follow stop` — a **launched** nvim pane is owned by the follower and stop kills it. Only an **adopted** nvim (`adopt_existing`) is never killed; close that one yourself with `:q`.

---

## Check 6 — nvim backend: pause / interrupt / des-interrupt

**Purpose:** control parity with the tmux backend on nvim — pause/resume, interrupt (hand the buffer to the human), and des-interrupt (discard their unsaved typing, replay the rest) — all over RPC, no `send-keys`.

**Machine-verified, no manual step** by three tests in `tests/test_e2e_battery_tranche3.py`, all through the CLI on a launched nvim:

- `::test_nvim_pause_then_resume_through_the_cli_ends_on_the_exact_bytes` — the pause halts (two reads 0.6 s apart agree, short of the content) and the resume ends on the exact bytes. On nvim a pause is checked per character, so it can land mid-line;
- `::test_nvim_interrupt_then_save_releases_the_hook_with_its_notification` — after an interrupt the current buffer is the file and modifiable, and the user's `:w` releases the hook with the "saved their own version" notification, leaving no remainder;
- `::test_nvim_des_interrupt_discards_the_users_unsaved_typing` — a second interrupt throws unsaved typing away and replays onto the exact content, consecutive blank lines included (`27aa0e4`).

Your real nvim config (plugins, mappings) is not exercised here; that is the nvim step of `qa/plugin-install-smoke.md`. The adopt path's socket discovery and adopted-stop cleanup (`6c565f9`) are covered by the automated suite.

---

## Check 7 — Parallel-hook serialization

**Purpose:** six PostToolUse hooks firing near-simultaneously (six Write tool calls in one turn) animate exactly one file, instead of interleaving keystrokes into one follower pane (fixed by the atomic animation-slot claim, `e51071e`).

**Machine-verified, no manual step** by `tests/test_e2e_battery_tranche3.py::test_six_parallel_hooks_animate_exactly_one_file_cleanly` (six `hook post` processes fired back to back into a Vim carrying a key-free observer timer: every loser exits while the winner's marker reads `running` — proof they raced a live holder — one file reaches the follower, no observed line is garble, a leaked opener or another file's token, one tab, and `open_files` names only the winner). `scripts/repro-concurrent-hooks.sh` stays as a live demo, but is no longer part of a battery run.

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

**Machine-verified** by `tests/test_e2e_battery_tranche3.py::test_insert_exit_stays_clean_under_a_remapped_esc_and_a_ctrl_backslash_map` (a hermetic vimrc stands in for the real config: an EscHandler-style insert `<Esc>` map that swallows the first Escape, a Normal-mode `<C-\>` map proved live before the animation, an InsertLeave counter and a key-free observer. The animation leaves Insert once per line, never fires `<C-\>` — re-adding `<C-\><C-n>` to the exit fires it on every line — and no observed line is stray. The `<Esc>` stand-in is asserted inert: the animation runs under `'paste'`, which disables insert-mode mappings).

**When to run by hand:** after changing an insert-mode or navigation plugin in your real config — only your own plugin stack can say whether it stays clean, and the hermetic test cannot see it:

```sh
zsh scripts/repro-exit-insert-matrix.sh
```

It runs a full animation against your REAL config and dumps the buffer **before** the relock (the relock's `:silent! e!` reloads the correct file from disk and would hide any corruption), comparing the shipped two-Escape exit against the old four-key one.

**What Alberto looks at:** the script's verdict line.

**PASS:** `PASS: the two-Escape exit is clean; re-adding <C-\><C-n> corrupts the buffer`. **FAIL:** `FAIL` (the shipped exit dirtied the buffer). `INCONCLUSIVE` means your config lacks a Normal-mode `<C-\>` mapping, so the old variant cannot fail there — check with `:verbose map <C-Bslash>`.

**Cleanup:** none — the script tears down its own isolated tmux server.

---

## Check 9 — nvim standalone (no tmux)

**Purpose:** confirm the no-tmux path (`fe87f5b`, extended 2026-09-04 with the iTerm2 split-pane fallback) opens a VISIBLE nvim surface beside your shell — the part only a real GUI session can show. Run it after changing `backends/nvim_connect.py`.

**Machine-verified** by `tests/test_e2e_battery_tranche3.py::test_standalone_nvim_starts_animates_stays_open_and_stops_without_tmux` (outside tmux with `backend: nvim`, `nvim_window: auto`, through the wrapper: an `nvim-qt` shim first on `PATH` hosts a headless nvim, while `osascript`/`open` shims fail loudly so no real window can open; `start` returns promptly on the GUI tier and keys the follower by the terminal (`term-e2e`), the hook types char by char, the nvim outlives the edit, and `stop` quits it). What stays here is the iTerm2 tier, which only a real iTerm2 can show.

**Setup:** from **iTerm2**, outside any tmux session (from Terminal.app the script warns that it exercises the older Terminal.app branch instead):

```sh
zsh scripts/qa-check-9-nvim-standalone.sh
```

> ⚠️ It writes your **real** `~/.config/claude-vim-follower/config.json` (`{"backend": "nvim", "nvim_window": "auto"}`), backing it up first and restoring it automatically on any crash; its printed cleanup restores it once you are done watching.

> ⚠️ On the first run from iTerm2, macOS may show an Automation permission prompt asking to let iTerm2 (or vim-ai-follower) control iTerm2 — approve it, or the split silently fails and nothing opens.

It animates `qa/fixtures/standalone-demo.py` into the new surface.

**What Alberto looks at:** where the nvim surface opens, and the origin pane while it animates.

**PASS:** a **split pane inside your current iTerm tab** opens beside your shell (not a disconnected window), the origin pane stays usable while it animates, and `claude-follow stop` from the origin quits it. **FAIL:** nothing opens, a separate window opens instead of a split, or the origin pane is blocked.

**Cleanup:** run the "Cleanup when done:" lines the script prints (they remove the demo file and restore or remove your config).

---

## Check 10 — vim-without-tmux error

**Purpose:** starting the **tmux** backend outside any tmux session fails loudly and actionably instead of silently no-opping or crashing.

**Machine-verified, no manual step** by `tests/test_e2e_battery_tranche3.py::test_tmux_backend_outside_tmux_fails_loudly_and_opens_nothing` (`backend: tmux` in the config file, the real wrapper run without `TMUX_PANE`: exit 1, stderr exactly `commands.VIM_NEEDS_TMUX` — `claude-follow: the vim backend requires tmux — run inside a tmux session, or set backend to nvim` — compared for exact equality, which is what rules out a traceback, since one would land on stderr beside the message; an empty stdout; and no pane, follower state or nvim socket left behind). Since 2026-09-29 the message goes to **stderr**, like its sibling "could not open a standalone nvim window". Nothing to run by hand.

---

## Check 11 — nvim backend: real tabs for multiple files

**Purpose:** confirm several files edited in one turn land as real *tabs* in
ONE nvim — not stacked windows, and not one tab whose content is silently
replaced by each new file (`7abf023`, fixed further in `5253e00`/`142b76d`).

**Machine-verified** by `tests/test_e2e_battery_tranche2.py::test_three_writes_land_as_three_real_tabs` (exactly three tabs, one window each, one tab per file, each buffer holding only its own marker, read over RPC) — the whole PASS table is covered; run this by hand only as a fallback or to eyeball the tab line.

**Setup:**

```sh
zsh scripts/qa-check-11-nvim-multi-tabs.sh
```

It restarts the follower with `--backend nvim`, edits
`multi-alpha.py`/`multi-beta.py`/`multi-gamma.py` into three `/tmp` files back
to back, and then prints nvim's own tabpage list over RPC.

> Serial on purpose. Parallel hooks are serialized by the atomic
> animation-slot claim (Check 7) and only ONE of them animates, so firing
> them at once would test that guard instead of tab parity. A turn whose tool
> calls are sequential — the ordinary case — fires them exactly like this.

**What Alberto looks at:** the nvim pane with its tab line, walking the tabs
with `gt`/`gT`, plus the `TABS:` line the script prints.

**PASS:**

| Moment | PASS | FAIL |
| --- | --- | --- |
| After all three edits | Three tabs in ONE nvim, and the printed list says `TABS: 3` naming the three files | One tab replaced twice, three splits inside one tab, or `TABS:` ≠ 3 |
| Walking `gt`/`gT` | Each tab shows its own marker — `TAB ALPHA`, `TAB BETA`, `TAB GAMMA` — complete, with no other file's content mixed in | A tab holds another file's lines, or an empty tab |

**Cleanup:**

```sh
rm -f /tmp/vaf-qa-multi-alpha.py /tmp/vaf-qa-multi-beta.py /tmp/vaf-qa-multi-gamma.py
```

Close a leftover nvim pane with `:q` if `stop` left one behind.

---

## Check 12 — "Writing…" cue on every animation + static colorscheme

**Purpose:** confirm two fixes that only show on the FIRST animation of a
fresh nvim. `28937f0`: the ordinary (non-paused, non-interrupted) path never
set the float body to `Writing...`, so a first-time edit animated with a
blank box. `0998397`+`c42b77a`: the colorscheme is now forced explicitly
(`set background=dark`, then `colorscheme gruvbox`) *before* the typing
highlight is defined, because `:colorscheme` runs `hi clear` first and was
silently wiping `VafTypingLine` (and transiently `VafWriterCue`) on every
animation.

**Machine-verified** by `tests/test_e2e_battery_tranche2.py::test_first_animation_shows_writing_and_keeps_the_typing_highlight` — the float body reads `Writing...` mid-animation on the FIRST and a SECOND animation and closes after each, `VafTypingLine` survives the colorscheme's `hi clear` (against a stand-in gruvbox, since the isolated HOME has none) AND is applied as a `line_hl_group` extmark on the line being typed. In a manual run, judge only what a test cannot: whether the real gruvbox colours READ well from the first line, and the optional tmux border-title variant.

**Setup:**

```sh
zsh scripts/qa-check-12-writing-cue-colors.sh
```

It restarts the nvim follower at `--speed lento` and fires ONE edit into a
fresh file with no writer identity.

> The restart is load-bearing: the colorscheme is forced once per nvim
> process (guarded on `g:colors_name`), so re-running this against an nvim
> that already animated measures nothing.

**What Alberto looks at:** the nvim pane for the whole of that one
animation, and the floating box top-right.

**PASS:**

| Moment | PASS | FAIL |
| --- | --- | --- |
| From the first typed characters | The float's BODY reads `Writing...` (no writer identity in this edit, so the title stays default — the body is the thing to read) | A blank box for the whole animation |
| Throughout | Colors are gruvbox-dark and readable from the very first line; no washed-out stretch that later "settles" as plugins finish loading | Unreadable/inconsistent colors early on |
| Throughout | The line being typed is highlighted as the cursor walks it (`VafTypingLine` survived the `hi clear`) | No per-line highlight |
| After it finishes | No stuck `Writing...`. With no writer identity the whole box CLOSES (measured live 2026-09-21); with one (Check 5's writer 2) it stays, titled, with a blank body | `Writing...` still showing after the edit completed |

> tmux variant (optional): the cue is backend-shared (`hooks._animate_edit`
> calls `set_state("Writing...")` above both backends); on tmux it renders as
> the pane BORDER TITLE rather than a float. `claude-follow stop;
> claude-follow start --speed lento`, fire the same two hooks, watch the
> border title. Not exercised in the 2026-09-21 verification run.

**Cleanup:**

```sh
rm -f /tmp/vaf-qa-writing-cue.py
```

---

## Check 13 — nvim: what an interrupt leaves behind

**Purpose:** two guarantees about the buffer the user takes over.
`1d2b5a9`: interrupting mid-line used to snap the line to its full text
first ("it finished writing the line", Alberto 2026-09-16); it must now stay
cut exactly where it was. `6bef225`: `_run_ops` deletes an op's old range
instantly, so an interrupt inside an op used to hand over a buffer missing
lines the interrupt notification claimed were on screen; the delete must now
be rolled back.
**Machine-verified** by
`tests/test_e2e_cli_controls.py::test_nvim_interrupt_leaves_the_half_typed_line_uncounted`
and `::test_nvim_interrupt_rolls_back_the_op_it_was_inside` — in a manual run,
judge only whether the typing and the replay LOOK right: the per-character
pace, the typing-line highlight, and the cut landing where your eye says you
pressed `S`.

**Setup — two steps, in order, against the same follower and file:**

```sh
zsh scripts/qa-check-13-nvim-interrupt-fidelity.sh line > /tmp/vaf-qa-13.log 2>&1
# verdict on table A, let the animation finish, then:
zsh scripts/qa-check-13-nvim-interrupt-fidelity.sh op   > /tmp/vaf-qa-13.log 2>&1
```

Step `line` restarts the nvim follower at `--speed lento` and retypes
`rollback-before.py` into a fresh file. Step `op` fires
`rollback-after.py` at that same file; the two fixtures are byte-identical
except for `total_of`'s body, so the edit script is exactly one `replace` op
of 1 line → 3 lines (verified with `diff.compute_edit_script`).

**What Alberto looks at:** the nvim buffer at each press of `prefix S`.

**PASS — table A (step `line`, interrupt mid-LINE):**

| Moment | PASS | FAIL |
| --- | --- | --- |
| 1st `S`, pressed mid-word | The line stays CUT exactly where the cursor was, the rest simply absent; the buffer becomes modifiable | The line silently completes itself to its full text before the hand-over (the old behaviour) |
| 2nd `S` (des-interrupt) | That same line is retyped FROM ITS START — not resumed mid-word — and the rest of the file follows | The line resumes mid-word, or a line is dropped/duplicated |
| When it settles | Buffer == `qa/fixtures/rollback-before.py` exactly | Any diff |

**PASS — table B (step `op`, interrupt mid-OP):**

| Moment | PASS | FAIL |
| --- | --- | --- |
| 1st `S`, pressed while the three new lines are typing | `total_of`'s ORIGINAL single `return sum(...)` line is BACK and the buffer is exactly the before-version again — no half-typed leftover, no blank rows in the gap | Lines the op deleted are missing, or a half-typed line sits in the gap |
| 2nd `S` (des-interrupt) | The op replays from its start and the buffer lands on `qa/fixtures/rollback-after.py` exactly | A duplicated or missing line around the replaced region |

> An `S` landing BEFORE the op's delete correctly touches nothing at all —
> that boundary check runs before the delete. Wait until at least the first
> new line is partly on screen.

Settle both tables from the real buffer, not the pane — the script prints the
exact `qa_dump_nvim_buffer … | diff -` line.

**Cleanup:**

```sh
rm -f /tmp/vaf-qa-rollback.py
```

---

## Check 14 — Interrupt / des-interrupt hand-off CYCLES (both backends)

**Purpose:** confirm the hand-off wait is a loop with no ceiling (`5b87c34`).
Before the fix it ended at the first replay stop: `S` (interrupt), `S`
(des-interrupt), `S` mid-replay (interrupt again) all worked and the FOURTH
press did nothing at all — nobody was listening any more, and the buffer
stayed partial and modifiable until some later animation's relock healed it
("nao desinterrompeu mais", Alberto 2026-09-16).
**Machine-verified** by
`tests/test_e2e_cli_controls.py::test_handoff_cycles_past_the_press_that_used_to_do_nothing`
— in a manual run, judge only that every press FEELS acknowledged at the
moment you make it (the popup, the cue switching between waiting and
"Writing…") and that each replay looks smooth rather than jumping.

**Setup — run it twice, once per backend:**

```sh
zsh scripts/qa-check-14-handoff-loop.sh nvim > /tmp/vaf-qa-14.log 2>&1
zsh scripts/qa-check-14-handoff-loop.sh tmux > /tmp/vaf-qa-14.log 2>&1
```

Each run restarts the follower on that backend at `--speed lento` and
retypes `nvim-blanks.py` into a fresh file.

**What Alberto looks at:** pressing `prefix S` over and over, alternating
interrupt and des-interrupt, AT LEAST FOUR TIMES.

**PASS:**

| Press | PASS | FAIL |
| --- | --- | --- |
| 1st `S` | Typing stops, buffer modifiable | No response |
| 2nd `S` | The remaining animation replays | No response |
| 3rd `S` | The replay stops, buffer modifiable again | No response |
| **4th `S`** | The replay RESUMES — this is the press the fix bought | Nothing happens at all (the old behaviour) |
| 5th, 6th, … | Every further press lands too; each des-interrupt discards what you typed and picks up from the same place, never from the top | A press that does nothing |
| After the last des-interrupt finishes | Buffer == `qa/fixtures/nvim-blanks.py` exactly, consecutive blank lines intact (none dropped, none doubled) | Any diff |

> On the tmux backend the whole animation is far shorter than on nvim — it
> sends whole lines, not characters — so press the cycles briskly or it will
> finish under you. A press after the animation ended prints
> `claude-follow: nothing to interrupt`, which is correct, not a failure.
> You can watch the state machine from another pane:
> `cat ~/.cache/claude-vim-follower/<window_id>.animating` prints
> `<pid> running` / `<pid> paused` / `<pid> handoff`.

**Cleanup:**

```sh
rm -f /tmp/vaf-qa-handoff-loop.py
```

---

## Check 15 — Crash-fallback catch-up rebuilds from the persisted partial

**Purpose:** confirm that when a hook is killed mid-pause, the NEXT edit
converges on the right content (`d1620ff`, `7a470c1`, `4362526`). The pace-0
catch-up used to replay straight onto the live buffer, whose tail is
routinely a half-typed line nothing had repaired — measured against real
nvim, `_resume_fresh` pushed the leftover down and `_run_ops` stranded every
extra row a partly-typed multi-line op had left. It now rebuilds from the
partial persisted alongside the remainder, on both backends.
**Machine-verified** by
`tests/test_e2e_cli_controls.py::test_crash_fallback_catches_up_after_the_hook_is_killed`
— in a manual run, judge only the SHAPE of the catch-up on screen: the buffer
jumping to the rest of the first version in one silent step, then the new edit
typing on top of it at normal pace.

**Setup — run it twice, once per backend. Fully script-driven; Alberto only
reads the result:**

```sh
zsh scripts/qa-check-15-crash-catchup.sh nvim
zsh scripts/qa-check-15-crash-catchup.sh tmux
```

What the script does, in order, because the mechanics are the check:

1. Animate `catchup-blanks.py` into a fresh file at `--speed lento`.
2. `claude-follow pause` — the same path `prefix P` runs.
   `animate._wait_while_paused` writes the remainder to
   `<window_id>.pending_animation.json` and blocks in its poll loop.
3. Wait for that pending file to APPEAR. It is the readiness signal; a blind
   sleep here would let the kill land before anything was persisted and the
   whole check would measure nothing. The script aborts loudly if it never
   shows up.
4. `kill -9` the hook. `-9` on purpose: the wait loop's `finally` discards
   the pending file on any orderly exit, so a catchable signal would clean up
   the very state a hook timeout leaves behind. The script re-checks that the
   pending file survived, and aborts if it did not.
5. Fire the NEXT edit (`catchup-blanks-next.py`) at the same file.

**What Alberto looks at:** the follower catching up and then animating the
new edit.

**PASS:**

| Moment | PASS | FAIL |
| --- | --- | --- |
| Right after the next edit fires | The buffer JUMPS to the rest of the first version (the pace-0 catch-up), then the new edit types on top | It types onto the half-typed tail, pushing a leftover line down |
| When it settles | Buffer == `qa/fixtures/catchup-blanks-next.py` exactly, ending in the marker `CATCHUP OK` | Any diff |
| At the seam | Nothing duplicated, nothing stranded; the blank-line gaps (two blanks between defs, one triple gap) are exactly as in the fixture | A repeated line where the kill happened, or a stray/missing blank |

> The `claude-follow: pause requested` line may be followed by
> `no current client` when the tmux session has no attached client — that is
> the pause POPUP failing to draw, not the pause failing.

Settle it from the real buffer, not the pane — the script prints the exact
`qa_dump_nvim_buffer`/`qa_dump_vim_buffer` diff line for the backend it ran.

**Cleanup:**

```sh
rm -f /tmp/vaf-qa-catchup.py
```

---

## Check 16 — tmux: no E37 hit-enter prompt on a dirty target

**Purpose:** confirm navigating to a MODIFIED buffer no longer stalls the
follower pane (`511ceb6`). `:tab drop {file}` ends with a `:rewind` whose
abandon check raises `E37: No write since last change (add ! to override)`
when the buffer it landed on is modified — the ordinary state after an
interrupt hand-off or a killed hook. `goto_file` now wraps the drop in a
`:try`/`:catch` that swallows exactly E37.
**Machine-verified** by
`tests/test_e2e_cli_controls.py::test_navigating_to_a_dirty_buffer_raises_no_e37_prompt`
— in a manual run, judge only what the pane LOOKS and FEELS like: no error
text or prompt anywhere on it, and it responding instantly to `j`/`G` rather
than to a prompt. (The test asserts on Vim's `:messages`, because the
follower's own trailing `:setlocal` dismisses a real hit-enter prompt
milliseconds after it appears — too fast for a script, but not for your eye.)

**Setup:**

```sh
zsh scripts/qa-check-16-tmux-dirty-nav.sh > /tmp/vaf-qa-16.log 2>&1
```

It restarts the tmux follower at `--speed lento`, animates
`pause-trigger.py`, interrupts it, `kill -9`s the waiting hook (so the window
is free for the next one while the buffer stays dirty), then fires a Read
navigation at that same dirty buffer.

> ⚠️ **Geometry is load-bearing and the script enforces it.** E37 is 51
> characters and only WRAPS — and a wrapped message is what makes Vim
> escalate to a real blocking hit-enter prompt — in a pane narrower than
> that. The script resizes the follower to 49 columns and prints the width;
> if it cannot get below 51 it says so, and a clean pane then proves
> NOTHING. Use a terminal window at least ~100 columns wide so the split can
> be narrowed.

**What Alberto looks at:** the follower pane immediately after the
navigation.

**PASS:**

| | PASS | FAIL |
| --- | --- | --- |
| The pane | No `E37: No write since last change`, no `Press ENTER or type command to continue`, no `-- More --` | Any of them on screen |
| The content | The file is shown and the half-typed content the interrupt left is still there | The buffer reloaded from disk (full content back), which means the unsaved work was discarded |
| Liveness | Press `j` or `G` — the cursor moves | The keystroke only dismisses a prompt |

> Use a NON-colon key for the liveness probe. `:` is a real key at a
> hit-enter prompt, so an Ex command can dismiss the very prompt it was sent
> to detect and report success.
>
> Negative control, if you want to see the failure this guards (measured
> 2026-09-21 at 49 columns): send a RAW `:tab drop <file>` into the dirty
> follower pane by hand — it prints `E37: No write since last change (add !
> to override)` followed by `Press ENTER or type command to continue`. Press
> Enter to clear it before continuing.

**Cleanup:**

```sh
rm -f /tmp/vaf-qa-dirty-nav.py
tmux resize-pane -t <follower pane> -x <original width>   # both printed by the script
```

---

## Check 17 — tmux: swap-file ATTENTION answered "(E)dit anyway"

**Purpose:** confirm `:tab drop` on a file with a `.swp` no longer blocks the
pane twice over (`e0bb5be`). At the follower's real width the ATTENTION block
reaches the `-- More --` pager BEFORE it reaches the `[O]pen Read-Only,
(E)dit anyway, …` question, so every keystroke the follower sends afterwards
answers a prompt instead of navigating. The fix is Vim's own
`SwapExists`/`v:swapchoice` hook, registered and torn down inside
`goto_file`'s single Ex line — scoped by TIME, so a user `:e` of a swapped
file afterwards still gets the normal dialog.

**Machine-verified** by `tests/test_e2e_battery_tranche2.py::test_a_live_owners_swap_is_edited_anyway_and_the_owner_still_writes` and `::test_a_stale_swap_is_edited_anyway_and_left_on_disk` (at 49 columns: no E325/ATTENTION in `:messages`, cursor not parked on the bottom row, real content in the buffer, the owner Vim's buffer is byte-identical before and after the navigation and it still `:w`s, the stale swap survives) — the whole PASS table is covered; run this by hand only as a fallback.

**Setup — two steps, in order:**

```sh
zsh scripts/qa-check-17-tmux-swap.sh live  > /tmp/vaf-qa-17.log 2>&1
# verdict on table A, then:
zsh scripts/qa-check-17-tmux-swap.sh stale > /tmp/vaf-qa-17.log 2>&1
```

`live` restarts the tmux follower, opens a **second real Vim** on the target
in its own pane (the everyday adopt-mode shape: the user's own editor on the
file Claude is writing), waits for the swap file to appear, narrows the
follower to 49 columns, and navigates. `stale` does the same but `kill -9`s
that Vim first, so what is left is a crash's stale swap. Both abort loudly if
no swap file appears — without one there is nothing to answer and the run
would measure nothing.

> ⚠️ Same 49-column geometry requirement as Check 16, same warning, same
> reason.

**What Alberto looks at:** the FOLLOWER pane after each navigation, plus the
other Vim (step `live`) and the swap file on disk (step `stale`).

**PASS — table A (`live`):**

| | PASS | FAIL |
| --- | --- | --- |
| The follower pane | Real content of the file, and no `E325`, no `ATTENTION`, no `Swap file … already exists`, no `[O]pen Read-Only, (E)dit anyway`, no `-- More --`, no `Press ENTER` | Any of them |
| Liveness | `j`/`G` moves the cursor (never a `:` command — see Check 16's note) | The key only dismisses a prompt |
| The other Vim | Type something there and `:w` — it saves normally | It cannot save, or its content was touched |

**PASS — table B (`stale`):**

| | PASS | FAIL |
| --- | --- | --- |
| The follower pane | Same as above: content shown, no dialog, live | Any dialog or stall |
| The swap file | `ls -l /tmp/.vaf-qa-swap-stale.py.swp` still lists it — the policy is (E)dit anyway, never (D)elete it, so no recovery data is destroyed | The swap file is gone |

> Negative control (measured 2026-09-21 at 49 columns): a RAW `:tab drop`
> at a swap-held file the follower has not opened puts
> `Swap file "…" already exists!` and `[O]pen Read-Only, (E)dit anyway,
> (R)ecover, (Q)uit, (A)bort:` on the pane, so the clean result above is a
> real suppression, not an absent dialog. Answer it with `q` and Enter if you
> stage it.

**Cleanup:**

```sh
tmux kill-pane -t <owner pane>          # printed by the script (step live)
rm -f /tmp/vaf-qa-swap-live.py /tmp/.vaf-qa-swap-live.py.swp
rm -f /tmp/vaf-qa-swap-stale.py /tmp/.vaf-qa-swap-stale.py.swp
tmux resize-pane -t <follower pane> -x <original width>
```

---

## Check 18 — nvim: a swap-held file opens instead of crashing the hook

**Purpose:** confirm `NvimFollower._open_from_disk` survives another live
editor holding the target's swap (`d817f08`). `bufload` raised `Vim:E325:
ATTENTION` straight out of the RPC call and nothing caught it, so the HOOK
PROCESS died instead of showing the file — on both callers (`ensure_showing`
for Read navigation, and `apply_edit`'s vanished-buffer branch). The buffer's
own `swapfile` is now turned off between `bufadd` and `bufload`, which is
safe because this backend's buffers are display-only and never written.

**Machine-verified** by `tests/test_e2e_battery_tranche2.py::test_a_swap_held_file_opens_in_nvim_and_the_hook_survives` (exit 0 through the real CLI, empty stderr, disk content in the buffer, no Traceback/E325 in `hook.log`, no `error`/`traceback`/`E325` in `hook.log` case-insensitively, the owner nvim still USABLE — it runs a `writefile` sent to its pane), for BOTH callers: the Read path and `apply_edit`'s vanished-buffer branch (`::test_a_swap_held_file_whose_follower_buffer_vanished_opens_on_edit`) — the whole PASS table is covered; run this by hand only as a fallback.

**Setup:**

```sh
zsh scripts/qa-check-18-nvim-swap.sh
```

It restarts the nvim follower, opens a **second real nvim** on the target in
its own pane, waits for the swap to appear under
`~/.local/state/nvim/swap/`, then Read-navigates with the hook in the
FOREGROUND and prints its exit code and stderr.

> The owner must be an **nvim**, not a Vim: nvim's default `directory` is
> `~/.local/state/nvim/swap//` while Vim's starts with `.`, so a Vim-made
> swap beside the file in `/tmp` is not where the follower's nvim looks and
> the check would be vacuous.
>
> This is also why the automated suite could not catch the bug: the shared
> `headless_nvim` fixture launches with `-n`, which suppresses the swap check
> outright. The follower's own launched nvim has no `-n`, so this check is
> real.

**What Alberto looks at:** the follower nvim pane and the two printed lines.

**PASS:**

| | PASS | FAIL |
| --- | --- | --- |
| The hook | `exit code: 0` and empty stderr | Non-zero with a `Vim:E325: ATTENTION` traceback |
| The follower | The file's real content shown in its own tab | An empty buffer, or nothing at all |
| The other nvim | Untouched and still usable | Disturbed |
| The log | `grep -i -e traceback -e error -e E325 ~/.cache/claude-vim-follower/hook.log` finds nothing | Anything there |

> There is no on-screen dialog to look for here, unlike Check 17: nvim's
> channel stays responsive and the failure is a dead hook, not a stalled
> pane. That is why the hook runs in the foreground with its exit code
> printed — otherwise a pass and a crash would look identical.
>
> Negative control (measured 2026-09-21): a raw `bufload` of an UNRELATED
> swap-held file in the same follower nvim still raises `Vim:E325:
> ATTENTION`, so the silence above is a buffer-local suppression, not a
> globally disarmed swap check. If you stage it, dismiss the resulting
> `Press ENTER` in the nvim pane before doing anything else — while it is up,
> every later RPC call blocks, including `claude-follow stop`.

**Cleanup:**

```sh
tmux kill-pane -t <owner pane>          # printed by the script
rm -f /tmp/vaf-qa-nvim-swap.py
rm -rf /tmp/vaf-qa-<run id>
find ~/.local/state/nvim/swap -name '*vaf-qa-nvim-swap*' -delete
```

---

## Check 19 — nvim: Read navigation shows disk content, `goto_line` clamps

**Purpose:** confirm a Read of a never-animated file opens a tab holding the
REAL on-disk text, and that an offset past EOF lands on the last line
(`98b6201`). `ensure_showing` used to delegate to `goto_file`, whose
no-buffer fallback creates an EMPTY named buffer and never reads disk — so a
Read opened a blank tab, and the `goto_line(offset)` that follows then raised
nvim's "Invalid cursor line: out of range" out of the hook process.

**Machine-verified** by `tests/test_e2e_battery_tranche2.py::test_read_navigation_shows_disk_and_clamps_the_offset` (disk content, cursor on line 5 then on the last line, exit 0 and empty stderr both times, the current buffer proved to BE the target before cursor and `nomodifiable` are read) — the launched half is covered; the adopted-nvim note below stays manual.

**Setup:**

```sh
zsh scripts/qa-check-19-nvim-read-nav.sh
```

It restarts the nvim follower (so the file has definitely never been animated
into it), then fires two Read hooks in the FOREGROUND — offset 5, then offset
9999 — printing each exit code, stderr, and the resulting cursor line.

**What Alberto looks at:** the follower nvim pane and the printed lines.

**PASS:**

| | PASS | FAIL |
| --- | --- | --- |
| The tab | Holds the real content of the file (the `bfs()` algorithm) | An empty buffer or a blank tab |
| Offset 5 | `exit code: 0`, empty stderr, `cursor is on line: 5` | Non-zero, a traceback, or the wrong line |
| Offset 9999 | `exit code: 0`, empty stderr, cursor on the LAST line (46) | A traceback saying `Invalid cursor line: out of range`, or the cursor on line 1 |
| The buffer | `nomodifiable` — this nvim was LAUNCHED, so the follower owns it | Modifiable |

> **Note — adopted nvim (`779284b`), optional and manual**, same shape as
> Check 6's adopt note. An adopted nvim is the user's OWN editor and
> navigation must never lock it, while a launched one (above) is always
> locked. Adoption needs the ORIGIN pane's own process to BE nvim
> (`nvim_connect.discover_adopt_socket` globs `$TMPDIR/nvim.$USER/*/nvim.<pane
> pid>.0`), so the realistic staging is: open nvim as a pane command, run the
> shell inside it (`:terminal`), set `adopt_existing: true`, and
> `claude-follow start --backend nvim` from there. Fire the same Read and
> check `:echo &modifiable` — 1 when adopted, 0 when launched.
> **NOT VERIFIED** by the script or by the 2026-09-21 verification run; only
> the launched half is exercised live. The adopted half is covered by
> `tests/test_nvim_integration_lock_parity.py`.

**Cleanup:**

```sh
rm -f /tmp/vaf-qa-read-nav.py
rm -rf /tmp/vaf-qa-<run id>
```

---

## Result log

Superseded by `qa/results/<date>.md` (added in a later task of this plan).
The prior manual log lives in `qa/smoke-runbook.md`'s git history for
checks 1–6 up to 2026-07-21.
