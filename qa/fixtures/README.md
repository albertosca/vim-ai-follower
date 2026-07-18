# QA fixtures

Canonical test files used by the smoke runbook (`qa/smoke-runbook.md`) and the
`scripts/smoke-*.sh` helpers. Edit these to customize a check; the scripts copy
them into `/tmp` at run time, so the fixtures stay the single source of truth.

| File | Used by | Purpose |
| --- | --- | --- |
| `pause-trigger.py` | Check 3 (Escape+undo) | Python with `os.*`, `.strip()`, `.append()`, `sorted(` — triggers CoC/Copilot completion popups mid-animation. |
| `window-alpha.py` | Check 4 (window-scoping) | Distinct content animated into window 0's follower. |
| `window-beta.py` | Check 4 (window-scoping) | Distinct content animated into window 1's follower. |
| `cue-writer1.py` | Check 1 (color cue) | First writer's version (border stays neutral). |
| `cue-writer2.py` | Check 1 (color cue) | Second writer's version (border tints). |
