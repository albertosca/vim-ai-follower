---
description: Drive the full 19-check visual QA battery (qa/visual-battery.md) end to end, recording verdicts into qa/results/<date>.md.
---

Drive the entire visual test battery documented in `qa/visual-battery.md`,
end to end, against all 19 checks in order. Full detail for each check
(purpose, setup, what to look at, PASS/FAIL criteria, cleanup) lives in that
runbook — treat it as the source of truth if anything here seems to
disagree with it.

## Ground rules

- **Claude drives, Alberto watches.** You run every script and command;
  Alberto only observes the terminal/panes and gives a verdict. Never ask
  him to type commands himself unless a check's own setup explicitly calls
  for it (e.g. Check 9's real Claude Code prompt, or Check 3's manual
  fallback).
- **One heavy process at a time.** Do not run two checks' scripts
  concurrently, and do not background a check's driving script unless the
  check explicitly says to watch it stream live (e.g. Check 7) — in which
  case still wait for it to finish before starting the next check.
- **Kill any background process you started.** If you use `run_in_background`
  for any step, confirm it exited (or kill it) before moving to the next
  check.
- **Never touch Alberto's real tmux sessions or config**, except where a
  specific check's script explicitly says it will — Checks 9 and 10 write to
  `~/.config/claude-vim-follower/config.json`, and their own scripts
  (`scripts/qa-check-9-nvim-standalone.sh`, `scripts/qa-check-10-vim-without-tmux.sh`)
  already handle backup/restore of that file (auto-restoring on any crash).
  **Checks 11–19 never write the config** — each takes its backend from a
  `--backend` flag on `start` instead. Any future check that does need to
  write it uses `qa_protect_config` + `qa_write_test_config` +
  `qa_config_handoff`, exactly like Check 9. Checks 4 and 7 create their own
  isolated `vaf-smoke` / private tmux sessions and never touch Alberto's
  real server.
- **Redirect, never pipe, any check that leaves an animation running.**
  Checks 3, 6, 12, 13, 14 and 16 background a `hook post` so the controls can
  be exercised. Running them as `zsh scripts/... | tee log` makes the shell
  wait for the pipe, which closes only when the backgrounded hook exits — so
  the command does not return and no `prefix P`/`prefix S` lands until the
  animation is already over. Measured 2026-09-21: a whole verification round
  read as a pass that way with the interrupt never landing. Always
  `zsh scripts/... > /tmp/vaf-qa-run.log 2>&1` and then read the file.
- **Settle content verdicts from the buffer, not the screen.** `capture-pane`
  shows the rendered pane. Every check whose verdict is about content prints
  a ready-to-paste `qa_dump_nvim_buffer <socket> <file>` or
  `qa_dump_vim_buffer <pane>` diff line — use it.
- **Checks 9 and 10 must run OUTSIDE tmux — their scripts refuse to run
  inside one.** Your own Bash tool almost certainly runs inside the same
  tmux session Alberto's `claude` is running in (his normal Vim+tmux setup),
  so you likely CANNOT run these two yourself. Before reaching Check 9,
  check with `echo "$TMUX"` (or just try the script — it self-detects and
  exits 1 with a clear error if it can't run). If you're inside tmux, tell
  Alberto Checks 9 and 10 need to be run by him from a plain terminal window
  (not a tmux pane), give him the two commands
  (`zsh scripts/qa-check-9-nvim-standalone.sh`, `zsh scripts/qa-check-10-vim-without-tmux.sh`)
  and the LOOK AT text from `qa/visual-battery.md`, and record his reported
  verdict + notes the same way as any other check. This is the one
  documented exception to "Claude drives, Alberto watches."

## Setup — before Check 1

1. Confirm the current commit: `git rev-parse HEAD`.
2. Copy the results template to today's ledger:
   `cp qa/results/TEMPLATE.md qa/results/<YYYY-MM-DD>.md` (use today's actual
   date). If a file for today already exists, insert a new dated run block
   ABOVE the existing content instead of overwriting it — newest run at the
   TOP of the file (per the template's own instructions and the design spec).
3. Fill in the ledger's header line (`# Visual Battery Run — <DATE> — commit
   <SHA>`) with today's date and the commit SHA from step 1.
4. Run the Prep step from `qa/visual-battery.md` ("Prep (once, checks
   1-8)"): `claude-follow start` from the pane where `claude` runs, and
   confirm a follower pane opened before proceeding.

## For each of the 19 checks, in order

Run this five-step loop for each check. The scripts are:

| # | Script | Name |
| --- | --- | --- |
| 1 | `scripts/qa-check-1-color-cue.sh` | Per-writer color cue |
| 2 | `scripts/qa-check-2-stop-restores-border.sh` | Stop restores the border |
| 3 | `scripts/qa-check-3-escape-undo-race.sh` | Escape+undo race fix |
| 4 | `scripts/qa-check-4-window-scoping.sh` | Window-scoped identity (isolation) |
| 5 | `scripts/qa-check-5-nvim-animation-cue.sh` | nvim backend: char-by-char animation + floating writer cue |
| 6 | `scripts/qa-check-6-nvim-controls.sh` | nvim backend: pause/interrupt/des-interrupt |
| 7 | `scripts/qa-check-7-concurrent.sh` | Parallel-hook serialization |
| 8 | `scripts/qa-check-8-remapped-esc.sh` | Remapped-`<Esc>` insert-exit |
| 9 | `scripts/qa-check-9-nvim-standalone.sh` | nvim standalone (no tmux) |
| 10 | `scripts/qa-check-10-vim-without-tmux.sh` | vim-without-tmux error |
| 11 | `scripts/qa-check-11-nvim-multi-tabs.sh` | nvim backend: real tabs for multiple files |
| 12 | `scripts/qa-check-12-writing-cue-colors.sh` | "Writing…" cue on every animation + static colorscheme |
| 13 | `scripts/qa-check-13-nvim-interrupt-fidelity.sh line` then `... op` | nvim: what an interrupt leaves behind (two tables) |
| 14 | `scripts/qa-check-14-handoff-loop.sh nvim` then `... tmux` | Interrupt / des-interrupt hand-off cycles |
| 15 | `scripts/qa-check-15-crash-catchup.sh nvim` then `... tmux` | Crash-fallback catch-up rebuilds from the persisted partial |
| 16 | `scripts/qa-check-16-tmux-dirty-nav.sh` | tmux: no E37 hit-enter prompt on a dirty target |
| 17 | `scripts/qa-check-17-tmux-swap.sh live` then `... stale` | tmux: swap-file ATTENTION answered "(E)dit anyway" |
| 18 | `scripts/qa-check-18-nvim-swap.sh` | nvim: a swap-held file opens instead of crashing the hook |
| 19 | `scripts/qa-check-19-nvim-read-nav.sh` | nvim: Read navigation shows disk content, `goto_line` clamps |

Checks 13, 14, 15 and 17 are run TWICE each, with the argument shown —
`line`/`op`, `nvim`/`tmux`, `live`/`stale`. Announce, relay, wait for a
verdict and record each invocation separately; the results template has a
row per half (`13a`/`13b`, `14a`/`14b`, `15a`/`15b`, `17a`/`17b`). Let the
first half's animation finish before starting the second.

1. **Announce** the check to Alberto: its number, name, and one-line
   purpose (copy the "Purpose" sentence from the matching section of
   `qa/visual-battery.md`).
2. **Run** that check's script with the Bash tool (e.g.
   `zsh scripts/qa-check-1-color-cue.sh`). Some checks (5, 9) also require an
   extra `claude-follow start ...` invocation or a manual/Claude-Code-driven
   edit before or alongside the script — follow that check's own section in
   `qa/visual-battery.md` for any such extra step. For any check that leaves
   an animation running (3, 6, 12, 13, 14, 16), redirect instead of piping:
   `zsh scripts/... > /tmp/vaf-qa-run.log 2>&1`, then read the file (see the
   ground rule above).
3. **Relay** the script's `LOOK AT: ...` output to Alberto (paraphrase is
   fine, but don't drop detail — it names the exact moments/lines/output
   blocks to watch).
4. **Wait** for Alberto's verdict (PASS or FAIL) and any notes. Do not run
   the next check, and do not run that check's cleanup, until he responds.
   If he reports FAIL, still record it and continue to the next check unless
   he asks you to stop.
5. **Record** the verdict and notes into the matching row of
   `qa/results/<date>.md` (the file created in Setup step 2).
6. **Clean up** per that check's own script output / the runbook's
   "Cleanup" section:
   - Checks 1, 2, 3, 4, 5, 6, 9, 10 print or document cleanup steps for you
     to run yourself (e.g. `rm -f /tmp/vaf-qa-*.py`, `tmux kill-session -t
     vaf-smoke`, closing a leftover nvim pane with `:q`, restoring a backed-up
     config file). Run exactly what that check's script prints under
     "Cleanup when done:" (or, for Checks 1/2 which chain into each other,
     what the runbook's Cleanup section says).
   - Checks 7 and 8's hermetic half self-clean (their own `trap cleanup
     EXIT` tears down their isolated `$HOME`/tmux socket) — no action needed
     from you for those halves. Check 8 also has a real-world variant that
     leaves `/tmp/vaf-qa-esc.py` for you to remove, same as the other manual
     checks.
   - Checks 11–19 all print a "Cleanup when done:" block; run it verbatim.
     Three of them leave more than a `/tmp` file behind and are easy to skip:
     Check 16 and Check 17 print a `tmux resize-pane` that restores the
     follower pane's original width, Check 17 (`live`) and Check 18 print a
     `tmux kill-pane` for the second editor they opened, and Check 18 prints
     a `find ~/.local/state/nvim/swap -name '*vaf-qa-nvim-swap*' -delete`.

## Extra setup notes for checks 11–19

- **No config writes.** Every one of them picks its backend with
  `--backend` on `start`, so `~/.config/claude-vim-follower/config.json` is
  never touched. Nothing to back up or restore.
- **Geometry, for Checks 16 and 17.** Both only mean anything with the
  follower pane narrower than 51 columns — that is the width at which Vim's
  E37 / ATTENTION text wraps and escalates to a blocking prompt. The scripts
  resize to 49 and print the width; if the warning about ≥51 columns appears,
  tell Alberto and record the check as inconclusive rather than PASS, because
  a clean pane at that width proves nothing.
- **A second editor, for Checks 17 and 18.** The script opens it itself
  (`tmux split-window`), in the same window, and prints its pane id — a Vim
  for Check 17, an nvim for Check 18. Do not substitute one for the other:
  the two editors keep their swap files in different places.
- **Hooks in the foreground, for Checks 18 and 19.** Those two print an exit
  code and stderr instead of leaving an animation running, because their
  failure mode is a dead hook rather than anything on screen. Relay both
  numbers to Alberto; a non-zero code is a FAIL even if the pane looks fine.
- **The adopt note at the end of Check 19** is optional and not scripted.
  Offer it, do not stage it unless Alberto asks.

## After all 19 checks

1. Source the shared harness and run final teardown:
   `source scripts/qa-lib.sh && qa_teardown`.
2. Report the result of `qa_verify_clean` to Alberto explicitly — either
   `CLEAN: ...` or the exact `REMAINING: ...` lines it prints, verbatim.
3. Write a short entry to your own cross-session memory summarizing this
   run: which checks passed/failed, the commit SHA from Setup, and any
   notable observations Alberto made. (This is a note-to-self step — no
   special tooling is required beyond however you normally persist memory
   between sessions.)
