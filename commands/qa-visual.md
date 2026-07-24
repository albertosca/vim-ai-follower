---
description: Drive the full 10-check visual QA battery (qa/visual-battery.md) end to end, recording verdicts into qa/results/<date>.md.
---

Drive the entire visual test battery documented in `qa/visual-battery.md`,
end to end, against all 10 checks in order. Full detail for each check
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
  Checks 4 and 7 create their own isolated `vaf-smoke` / private tmux
  sessions and never touch Alberto's real server.
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

## For each of the 10 checks, in order

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

1. **Announce** the check to Alberto: its number, name, and one-line
   purpose (copy the "Purpose" sentence from the matching section of
   `qa/visual-battery.md`).
2. **Run** that check's script with the Bash tool (e.g.
   `zsh scripts/qa-check-1-color-cue.sh`). Some checks (5, 9) also require an
   extra `claude-follow start ...` invocation or a manual/Claude-Code-driven
   edit before or alongside the script — follow that check's own section in
   `qa/visual-battery.md` for any such extra step.
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

## After all 10 checks

1. Source the shared harness and run final teardown:
   `source scripts/qa-lib.sh && qa_teardown`.
2. Report the result of `qa_verify_clean` to Alberto explicitly — either
   `CLEAN: ...` or the exact `REMAINING: ...` lines it prints, verbatim.
3. Write a short entry to your own cross-session memory summarizing this
   run: which checks passed/failed, the commit SHA from Setup, and any
   notable observations Alberto made. (This is a note-to-self step — no
   special tooling is required beyond however you normally persist memory
   between sessions.)
