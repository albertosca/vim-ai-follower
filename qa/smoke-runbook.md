# vim-ai-follower — Smoke Test Runbook

Manual, live QA for the features that automated tests can't fully validate
(real tmux, real Vim config with CoC/Copilot, real keystroke timing). Run this
after any change that touches the follower's tmux/animation/hook paths, and
before merging a batch.

Everything here is self-contained: the helper scripts live in `scripts/`, the
test files in `qa/fixtures/` (already made — the scripts copy them into `/tmp`
at run time). Nothing here ever touches your own tmux sessions except the
dedicated `vaf-smoke` session that Check 4 creates and you kill.

- **Binary:** `claude-follow` is **not** on `PATH` — every command below uses
  the full path `/Users/albertosca/Programming/vim-ai-follower/.venv/bin/claude-follow`
  so you can copy-paste each line as-is, no alias to set first.
- **Cache dir:** `~/.cache/claude-vim-follower/` (state `<window_id>.pane`, signals, logs).
- **Time budget:** ~10–12 min for all four checks.

## Features under test

| Check | Feature | Shipped | What only a live test catches |
| --- | --- | --- | --- |
| 1 | Per-writer color cue | 2026-07-17 | Border actually tints + labels in real tmux |
| 2 | Stop restores the border | 2026-07-17 (fix `c1760c8`) | `pane-border-status` cleared on a real window after the pane is killed |
| 3 | Escape+undo race fix | 2026-07-17 | No stray `u` under real CoC/Copilot completion popups |
| 4 | Window-scoped identity | 2026-07-17 | Two real windows stay isolated, no cross-bleed |

---

## Prep (once)

Open a tmux window and, from the pane where `claude` normally runs:

```sh
/Users/albertosca/Programming/vim-ai-follower/.venv/bin/claude-follow start        # opens a follower Vim pane beside you (dedicated split)
```

**PASS:** a second (follower) pane opens. **FAIL:** an error, or no pane.

> If nothing opens, `open_policy` is `manual` (the default since the 2026-07-16
> bleed fix) — that's expected; `start` is required.

---

## Check 1 — Per-writer color cue 🎯

Run from the **same** origin pane where you did `start`:

```sh
zsh scripts/smoke-color-cue.sh
```

It fires two edits with distinct identities (content from
`qa/fixtures/cue-writer1.py` then `cue-writer2.py`) and pauses between them so
you can look.

| Moment | PASS | FAIL |
| --- | --- | --- |
| After writer 1 (you, no `agent_id`) | Border **neutral** (default); the file animates | Border already colored |
| After writer 2 (`agent_type=code-reviewer`) | Border **tints** to a color **and** the border **title** reads `code-reviewer` | Border stays neutral, or no title |

---

## Check 2 — Stop restores the border

Immediately after Check 1, in the same pane:

```sh
/Users/albertosca/Programming/vim-ai-follower/.venv/bin/claude-follow stop
```

**PASS:** the follower pane closes **and** the border returns to default — no
lingering color, and no leftover border-title bar on your origin pane.
**FAIL:** the origin pane keeps a border-status bar / tinted border.

> Regression `c1760c8`: the restore now runs *before* the follower pane is
> killed. Verify programmatically at any time:
> ```sh
> tmux show-options -wv pane-border-status    # expect: empty after stop
> ```

---

## Check 3 — Escape+undo race fix ⌨️

Needs a pause landing **mid-animation while a completion popup is up** (your
real CoC/Copilot). Restart the follower slow so you have time:

```sh
/Users/albertosca/Programming/vim-ai-follower/.venv/bin/claude-follow stop 2>/dev/null; /Users/albertosca/Programming/vim-ai-follower/.venv/bin/claude-follow start --speed lento
```

Then paste this prompt into your Claude Code (it drives the follower):

```
Cria o arquivo /tmp/vaf-smoke-pause.py com a Write tool, com exatamente o
conteúdo de qa/fixtures/pause-trigger.py deste repositório (import os; uma
função collect_paths que usa os.listdir, os.path.join, .strip().lower(),
.append() e sorted(set(...))).
```

(Or, without Claude, drive it by hand from the origin pane:)

```sh
cp qa/fixtures/pause-trigger.py /tmp/vaf-smoke-pause.py
P='{"tool_name":"Write","tool_input":{"file_path":"/tmp/vaf-smoke-pause.py"},"session_id":"me"}'
echo "$P" | /Users/albertosca/Programming/vim-ai-follower/.venv/bin/claude-follow hook pre
echo "$P" | /Users/albertosca/Programming/vim-ai-follower/.venv/bin/claude-follow hook post   # animates slowly — press `prefix P` mid-line
```

While a line with `os.` / `.strip()` / `sorted(` is animating and a completion
popup is visible, press **`prefix P`** (pause), then **`prefix P`** again to
resume (or let it finish).

**PASS:** the final buffer is clean — no stray `u`, no `banu`-style smear; the
content matches the file. **FAIL:** a literal `u` (or other junk) is left in a
line.

> Hermetic proof of the same fix (deterministic, no popup luck):
> `zsh scripts/repro-stray-u.sh` → prints `PASS`.

---

## Check 4 — Window-scoped identity (isolation)

A dedicated session, driven end to end by one script:

```sh
zsh scripts/smoke-window-scoping.sh
```

⚠️ It creates the `vaf-smoke` tmux session (and **only** touches that session —
never your server), starts a follower in **two** windows with your real config
(may prompt the macOS keychain once per follower — click "Always Allow"), and
animates a distinct file into each. It prints a programmatic isolation check
(different `window_id`s, different follower target panes, one state file each).

Then eyeball it:

```sh
tmux attach -t vaf-smoke
```

- `prefix 0` → window 0's follower shows **only** `WINDOW ZERO` (alpha).
- `prefix 1` → window 1's follower shows **only** `WINDOW ONE` (beta).

**PASS:** no bleed — neither window's edits appear in the other's follower
(the 2026-07-16 incident is gone). **FAIL:** one window's content shows up in
the other's follower.

**Cleanup:**

```sh
tmux kill-session -t vaf-smoke
```

---

## Teardown / reset

```sh
/Users/albertosca/Programming/vim-ai-follower/.venv/bin/claude-follow stop 2>/dev/null                      # stop any follower in the current window
tmux kill-session -t vaf-smoke 2>/dev/null  # if Check 4 left it
rm -f /tmp/vaf-smoke-*.py                  # scratch copies
```

If a check misbehaves, the hook log is the first place to look:

```sh
tail -n 40 ~/.cache/claude-vim-follower/hook.log
```

---

## Result log

Record each run so regressions are obvious over time.

| Date | 1 cue | 2 stop | 3 esc+undo | 4 window | Notes |
| --- | --- | --- | --- | --- | --- |
| 2026-07-17 | | ✗→fixed | | | Check 2 failed live (border stuck); fixed in `c1760c8`, re-run pending |
