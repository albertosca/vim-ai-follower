# QA fixtures

Canonical test files used by the smoke runbook (`qa/smoke-runbook.md`) and the
`scripts/smoke-*.sh` helpers. Edit these to customize a check; the scripts copy
them into `/tmp` at run time, so the fixtures stay the single source of truth.

| File | Used by | Purpose |
| --- | --- | --- |
| `pause-trigger.py` | Check 3 (Escape+undo), Check 8 (remapped-Esc) | Python with `os.*`, `.strip()`, `.append()`, `sorted(` — triggers CoC/Copilot completion popups mid-animation. |
| `window-alpha.py` | Check 4 (window-scoping) | Distinct content animated into window 0's follower. |
| `window-beta.py` | Check 4 (window-scoping) | Distinct content animated into window 1's follower. |
| `cue-writer1.py` | Check 1 (color cue) | First writer's version (border stays neutral). |
| `cue-writer2.py` | Check 1 (color cue) | Second writer's version (border tints). |
| `standalone-demo.py` | Check 9 (nvim standalone), Check 17 (tmux swap), Check 18 (nvim swap), Check 19 (nvim Read nav) | Breadth-first search graph algorithm — recognizable animation when typed line-by-line outside tmux, and a recognizable 46-line file to navigate to. |
| `nvim-blanks.py` | Check 6 (nvim des-interrupt), Check 14 (hand-off loop) | PEP 8 double blank lines between top-level defs — a des-interrupt replay across one of those gaps is the consecutive-blank-line fidelity case. |
| `multi-alpha.py` | Check 11 (nvim multi-file tabs) | Tab 1 of 3; every marker says `TAB ALPHA`. |
| `multi-beta.py` | Check 11 (nvim multi-file tabs) | Tab 2 of 3; every marker says `TAB BETA`. |
| `multi-gamma.py` | Check 11 (nvim multi-file tabs) | Tab 3 of 3; every marker says `TAB GAMMA`. |
| `rollback-before.py` | Check 13 (nvim interrupt fidelity) | The BEFORE half of the pair. |
| `rollback-after.py` | Check 13 (nvim interrupt fidelity) | The AFTER half: byte-identical to `rollback-before.py` except `total_of`'s body, so the edit script is exactly one `replace` op of 1 line → 3 lines — the shape an interrupted op has to roll back. |
| `catchup-blanks.py` | Check 15 (crash-fallback catch-up) | The FIRST edit, paused mid-animation before its hook is killed. Long enough that a pause at `lento` always leaves real content untyped, with consecutive blank lines (two between defs, one deliberate triple gap) around the rows the rebuild has to get right. |
| `catchup-blanks-next.py` | Check 15 (crash-fallback catch-up) | The SECOND edit: identical except `report()`'s dict, which grows a third key holding the marker `CATCHUP OK`. |

All fixtures are pure ASCII on purpose. Typing a multi-byte character
through the nvim backend corrupts it (measured 2026-09-21: `alpha — beta`
lands as `alpha \xe2 beta\x80\x94`), so a fixture with an em dash in it fails
every content diff for a reason that has nothing to do with the check.
