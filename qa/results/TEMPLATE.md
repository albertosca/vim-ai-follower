# Visual Battery Run — <DATE> — commit <SHA>

**Copy this file to `qa/results/<YYYY-MM-DD>.md` at the start of a run; newest runs go at the TOP of a growing history if multiple runs share a date, otherwise one file per date.**

| # | Check | Verdict | Notes |
| --- | --- | --- | --- |
| 1 | Per-writer color cue | | |
| 2 | Stop restores the border | | |
| 3 | Escape+undo race fix | | |
| 4 | Window-scoped identity (isolation) | | |
| 5 | nvim backend: char-by-char animation + floating writer cue | | |
| 6 | nvim backend: pause/interrupt/des-interrupt | | |
| 7 | Parallel-hook serialization | | |
| 8 | Remapped-`<Esc>` insert-exit | | |
| 9 | nvim standalone (no tmux) | | |
| 10 | vim-without-tmux error | | |
| 11 | nvim backend: real tabs for multiple files | | |
| 12 | "Writing…" cue on every animation + static colorscheme | | |
| 13a | nvim interrupt leaves the half-typed line | | |
| 13b | nvim: an interrupted op rolls its delete back | | |
| 14a | Hand-off loop cycles (nvim) | | |
| 14b | Hand-off loop cycles (tmux) | | |
| 15a | Crash-fallback catch-up (nvim) | | |
| 15b | Crash-fallback catch-up (tmux) | | |
| 16 | tmux: no E37 hit-enter prompt on a dirty target | | |
| 17a | tmux: swap-file ATTENTION answered — live owner | | |
| 17b | tmux: swap-file ATTENTION answered — stale swap | | |
| 18 | nvim: a swap-held file opens instead of crashing the hook | | |
| 19 | nvim: Read navigation shows disk content, `goto_line` clamps | | |
