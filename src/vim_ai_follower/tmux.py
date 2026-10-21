"""Thin subprocess wrappers over the tmux CLI: TmuxPane (send-keys, pane
queries, zoom) and TmuxWindow (resolve the window from the environment)."""

from __future__ import annotations

import re
import shlex
import subprocess
from dataclasses import dataclass
from pathlib import Path

# A run of ordinary characters, or one control character insert mode would
# act on (every C0 byte but the tab, and DEL); a line never holds a LF.
_CONTROL_CHARACTER = re.compile(r"([^\x00-\x08\x0b-\x1f\x7f]+)|([\x00-\x08\x0b-\x1f\x7f])")


def literal_argument(text: str) -> str:
    """`text` as one tmux argv argument that reaches the command whole. tmux
    reads any argument that ends in `;` as the end of its command and drops
    that `;` (`int a = 1;` typed as `int a = 1`, a lone `;` as nothing, a
    pane title `vim x;` set as `vim x`). Where the `;` is dropped, a
    backslash left at the end becomes the `;`, so a final `;` sent as `\\;`
    comes out as exactly one `;`, whatever comes before it (measured on
    tmux 3.8, 2026-10-17). In a `bind-key` argv the same `\\;` keeps a
    chain's separator inside the bound command."""
    if text.endswith(";"):
        return text[:-1] + "\\;"
    return text


# A `#` that is not part of a run of `#`s ending at `[` (a style, `#[fg=red]`).
_STYLE_FREE_HASH = re.compile(r"#(?!#*\[)")


def _without_stored_backslash_pairs(stored: str) -> str:
    """A pane title as tmux stores it, with each backslash pair the store
    made of one backslash (`a\\\\b` reads `a\\b`), left to right. Any
    other escape (`\\001`, a control byte tmux encoded) stays as stored:
    tmux refuses to set the byte itself, which would leave the cue up."""
    return stored.replace("\\\\", "\\")


@dataclass(frozen=True)
class TmuxPane:
    pane_id: str

    def running_command(self) -> str | None:
        result = subprocess.run(
            ["tmux", "list-panes", "-a", "-F", "#{pane_id} #{pane_current_command}"],
            capture_output=True,
            text=True,
            check=False,
        )
        for line in result.stdout.splitlines():
            pane_id, _, command = line.partition(" ")
            if pane_id == self.pane_id:
                return command
        return None

    def send_text(self, text: str) -> None:
        """Type `text` byte-exact, through literal_argument: a final `;`
        typed as nothing made an interrupted Write's `:w` save the damaged
        lines (measured in Vim on tmux 3.8, 2026-10-17: `-H` hex was exact
        too, but costs one argument per byte). A control character (a CR
        inside a mixed-endings line, a form feed, CTRL-K, ESC, DEL; never a
        tab, which insert mode takes as it is) goes as raw bytes after a
        CTRL-V, so insert mode inserts it instead of acting on it (a CR broke
        the line, a CTRL-K started a digraph). Still one tmux process: the
        pieces are commands chained by a lone `;` argument."""
        argv = ["tmux"]
        for literal, control in _CONTROL_CHARACTER.findall(text) or [("", "")]:
            if argv[-1] != "tmux":
                argv.append(";")
            if control:
                argv += ["send-keys", "-t", self.pane_id, "-H", "16", f"{ord(control):02x}"]
            else:
                argv += ["send-keys", "-t", self.pane_id, "-l", "--", literal_argument(literal)]
        subprocess.run(argv, check=True)

    def send_hex(self, *byte_values: str) -> None:
        """Send raw bytes, each a two-digit hex string (`send-keys -H`): no
        key-name parsing in between."""
        subprocess.run(["tmux", "send-keys", "-t", self.pane_id, "-H", *byte_values], check=True)

    def send_key(self, key_name: str) -> None:
        subprocess.run(["tmux", "send-keys", "-t", self.pane_id, key_name], check=True)

    def send_line(self, text: str) -> None:
        """Type `text` and its Enter in ONE `send-keys`, so the editor reads
        the line and the CR together: with the Enter in a second subprocess
        Vim drew the typed line on its command line and showed it until the
        Enter came (3-64 ms per call, measured 2026-10-17). The CR goes
        inside the literal text, never as a separate `;`-joined command: tmux
        ends a command at any argument that ends in `;`."""
        subprocess.run(
            ["tmux", "send-keys", "-t", self.pane_id, "-l", "--", text + "\r"],
            check=True,
        )

    def pane_pid(self) -> int | None:
        result = subprocess.run(
            ["tmux", "display-message", "-p", "-t", self.pane_id, "#{pane_pid}"],
            capture_output=True,
            text=True,
            check=False,
        )
        try:
            return int(result.stdout.strip())
        except ValueError:
            return None

    def title(self) -> str:
        result = subprocess.run(
            ["tmux", "display-message", "-p", "-t", self.pane_id, "#{pane_title}"],
            capture_output=True,
            text=True,
            check=False,
        )
        return _without_stored_backslash_pairs(result.stdout.rstrip("\n"))

    def set_title(self, title: str) -> None:
        """Set the pane title to exactly `title`, which `title()` reads back.
        `select-pane -T` format-expands its argument (`#{pane_id}` is set as
        `%0`, `#S` as the session, a final `#` dropped), so every `#` goes as
        `##` except a run of `#`s that ends at `[`: tmux keeps such a run whole
        (`##[x` stays `##[x`), and doubling it grew a style title by a `#` per
        `#` on every cue (T24b review I1). tmux then stores the title
        vis-encoded (a backslash as two), the stored form `title()` decodes
        (measured on tmux 3.8, 2026-10-17/18)."""
        # The user's own title comes back through here (StatusSurface.clear).
        escaped = literal_argument(_STYLE_FREE_HASH.sub("##", title))
        subprocess.run(
            ["tmux", "select-pane", "-t", self.pane_id, "-T", escaped],
            check=False,
        )

    def window_option(self, name: str) -> str | None:
        """The window-local option's value, or None when unset (shown empty)."""
        result = subprocess.run(
            ["tmux", "show-options", "-wv", "-t", self.pane_id, name],
            capture_output=True,
            text=True,
            check=False,
        )
        value = result.stdout.strip()
        return value or None

    def set_window_option(self, name: str, value: str | None) -> None:
        """Set the window-local option, or unset it when value is None."""
        if value is None:
            subprocess.run(["tmux", "set-option", "-wu", "-t", self.pane_id, name], check=False)
        else:
            subprocess.run(
                ["tmux", "set-option", "-w", "-t", self.pane_id, name, value], check=False
            )

    def set_border_color(self, color: str | None) -> None:
        """Tint this pane's border (both active and inactive styles), or
        restore the window default when color is None. Per-pane border
        styles need tmux 3.4+; best-effort like the other tmux helpers."""
        for name in ("pane-border-style", "pane-active-border-style"):
            if color is None:
                subprocess.run(["tmux", "set-option", "-pu", "-t", self.pane_id, name], check=False)
            else:
                subprocess.run(
                    ["tmux", "set-option", "-p", "-t", self.pane_id, name, f"fg={color}"],
                    check=False,
                )

    def kill(self) -> None:
        subprocess.run(["tmux", "kill-pane", "-t", self.pane_id], check=False)

    def window_panes(self) -> list[tuple[str, str]]:
        result = subprocess.run(
            [
                "tmux",
                "list-panes",
                "-t",
                self.pane_id,
                "-F",
                "#{pane_id} #{pane_current_command}",
            ],
            capture_output=True,
            text=True,
            check=False,
        )
        panes = []
        for line in result.stdout.splitlines():
            pane_id, _, command = line.partition(" ")
            panes.append((pane_id, command))
        return panes

    def zoomed(self) -> bool:
        result = subprocess.run(
            ["tmux", "display-message", "-p", "-t", self.pane_id, "#{window_zoomed_flag}"],
            capture_output=True,
            text=True,
            check=False,
        )
        return result.stdout.strip() == "1"

    def set_zoomed(self, zoomed: bool) -> None:
        # resize-pane -Z is a blind toggle; assert the desired state instead.
        if self.zoomed() != zoomed:
            subprocess.run(["tmux", "resize-pane", "-Z", "-t", self.pane_id], check=False)

    @classmethod
    def split_from(pane_class, target_pane: str, command: str) -> TmuxPane:
        result = subprocess.run(
            ["tmux", "split-window", "-h", "-t", target_pane, "-P", "-F", "#{pane_id}", command],
            capture_output=True,
            text=True,
            check=True,
        )
        return pane_class(pane_id=result.stdout.strip())


def adopt_target(origin: str) -> str | None:
    """The single existing vim pane in origin's window to adopt as the
    follower target, so a fresh split isn't opened when one is already
    running. With two or more vim panes the choice is ambiguous — never
    guess which Vim the user meant: return None and let the caller fall
    back to a dedicated split (and say why)."""
    vim_panes = [
        pane_id
        for pane_id, command in TmuxPane(pane_id=origin).window_panes()
        if pane_id != origin and command == "vim"
    ]
    return vim_panes[0] if len(vim_panes) == 1 else None


@dataclass(frozen=True)
class TmuxWindow:
    window_id: str

    @classmethod
    def from_env(window_class, environment: dict[str, str]) -> TmuxWindow | None:
        pane_id = environment.get("TMUX_PANE")
        if not pane_id:
            return None
        result = subprocess.run(
            ["tmux", "display-message", "-p", "-t", pane_id, "#{window_id}"],
            capture_output=True,
            text=True,
            check=False,
        )
        if result.returncode != 0:
            return None
        window_id = result.stdout.strip()
        if not window_id:
            return None
        return window_class(window_id=window_id)


# How long a popup stays up.
POPUP_SECONDS = 1.5
_WITHDRAW_POLL_SECONDS = 0.05


def show_popup(
    pane_id: str,
    message: str,
    after_seconds: float = 0.0,
    *,
    withdraw_token: Path | None = None,
) -> None:
    """Brief, self-dismissing popup over a pane. Fire-and-forget via Popen:
    display-popup -E only exits when the popup closes, and callers must not
    stall (nor delay a resume replay) waiting for it.

    after_seconds > 0 opens it that much later, from a detached shell. tmux
    shows one popup per client and drops a second one silently, with exit 0
    (measured on tmux 3.7c): a popup that must follow another one still up —
    a correction of a "Paused" just shown — has to wait for it to close.

    `withdraw_token` (with a delay) makes the popup withdrawable, so a late
    correction never covers a newer press's own popup (sweep-5 T22): the
    shell opens it only if the token still exists, claiming it by renaming it
    to `<token>.showing`, the popup closes early once that file is removed,
    and `<token>.closed` says it is gone (control.withdraw_corrections)."""
    if withdraw_token is not None and after_seconds > 0:
        showing = shlex.quote(f"{withdraw_token}.showing")
        closed = shlex.quote(f"{withdraw_token}.closed")
        ticks = round(POPUP_SECONDS / _WITHDRAW_POLL_SECONDS)
        body = (
            f"echo {shlex.quote(message)}; tick=0; "
            f"while [ $tick -lt {ticks} ] && [ -e {showing} ]; "
            f"do sleep {_WITHDRAW_POLL_SECONDS}; tick=$((tick+1)); done"
        )
        popup = ["tmux", "display-popup", "-t", pane_id, "-E", "-w", "30", "-h", "3", body]
        script = (
            f"sleep {after_seconds}; "
            f"mv -- {shlex.quote(str(withdraw_token))} {showing} 2>/dev/null || exit 0; "
            f"{shlex.join(popup)}; rm -f -- {showing}; : > {closed}"
        )
        _spawn_detached(["sh", "-c", script])
        return
    command = [
        "tmux",
        "display-popup",
        "-t",
        pane_id,
        "-E",
        "-w",
        "30",
        "-h",
        "3",
        f"echo {shlex.quote(message)}; sleep {POPUP_SECONDS}",
    ]
    if after_seconds > 0:
        command = ["sh", "-c", f"sleep {after_seconds}; exec {shlex.join(command)}"]
    _spawn_detached(command)


def _spawn_detached(command: list[str]) -> None:
    # Detached: the child lives as long as the popup (plus any delay), and a
    # hook's caller waiting for end-of-output must not wait for it too.
    subprocess.Popen(
        command,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )


def show_status(pane_id: str, message: str) -> None:
    """Transient message on the tmux status line — unlike show_popup it
    never grabs the keyboard, so rapid repeated keybinding presses (the
    speed keys) are not blocked while feedback is visible."""
    subprocess.run(
        ["tmux", "display-message", "-t", pane_id, message],
        check=False,
    )
