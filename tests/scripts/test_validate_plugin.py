"""scripts/validate_plugin.sh: `claude plugin validate --strict` as a local
gate that tolerates exactly one known warning (CLAUDE.md at the plugin
root, which is the repo's own notes for Claude Code, not plugin context)."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
SCRIPT = REPO / "scripts" / "validate_plugin.sh"

_BULLET = "\u276f"  # the validator's item marker, HEAVY RIGHT-POINTING ANGLE ORNAMENT
_TOLERATED = (
    f"  {_BULLET} root: CLAUDE.md at the plugin root is not loaded as project context. "
    "To ship context with your plugin, use a skill (skills/<name>/SKILL.md) instead."
)
_OTHER = f"  {_BULLET} hooks: something else is wrong."


def _fake_claude(
    bin_directory: Path, strict_output: str, strict_status: int, plain_status: int
) -> None:
    """A `claude` that answers `plugin validate [--strict] .` like the real one."""
    bin_directory.mkdir()
    strict_file = bin_directory / "strict.txt"
    strict_file.write_text(strict_output)
    fake = bin_directory / "claude"
    fake.write_text(
        "#!/bin/bash\n"
        'if [ "$3" = "--strict" ]; then\n'
        f'  cat "{strict_file}"; exit {strict_status}\n'
        "fi\n"
        f'echo "Validation passed"; exit {plain_status}\n'
    )
    fake.chmod(0o755)


def _run(path: str) -> subprocess.CompletedProcess[str]:
    environment = {**os.environ, "PATH": path}
    return subprocess.run(
        ["/bin/bash", str(SCRIPT)],
        cwd=REPO,
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )


def _warnings(*lines: str) -> str:
    count = len(lines)
    plural = "" if count == 1 else "s"
    body = "\n".join(lines)
    verdict = "✘ Validation failed (--strict treats warnings as errors)"
    return f"⚠ Found {count} warning{plural}:\n\n{body}\n\n{verdict}\n"


def test_without_claude_on_path_it_skips_with_a_message(tmp_path: Path) -> None:
    empty = tmp_path / "bin"
    empty.mkdir()
    result = _run(f"{empty}:/usr/bin:/bin")
    assert result.returncode == 0
    assert "claude not on PATH" in result.stderr


def test_a_clean_strict_pass_passes(tmp_path: Path) -> None:
    _fake_claude(tmp_path / "bin", "✔ Validation passed\n", 0, 0)
    assert _run(f"{tmp_path / 'bin'}:/usr/bin:/bin").returncode == 0


def test_only_the_root_claude_md_warning_is_tolerated(tmp_path: Path) -> None:
    _fake_claude(tmp_path / "bin", _warnings(_TOLERATED), 1, 0)
    result = _run(f"{tmp_path / 'bin'}:/usr/bin:/bin")
    assert result.returncode == 0, result.stderr
    assert "tolerated" in result.stderr


@pytest.mark.parametrize(
    "warnings", [(_OTHER,), (_TOLERATED, _OTHER)], ids=["other", "tolerated-plus-other"]
)
def test_any_other_warning_fails(tmp_path: Path, warnings: tuple[str, ...]) -> None:
    _fake_claude(tmp_path / "bin", _warnings(*warnings), 1, 0)
    result = _run(f"{tmp_path / 'bin'}:/usr/bin:/bin")
    assert result.returncode == 1
    assert "something else is wrong" in result.stdout + result.stderr


def test_an_error_fails_even_with_only_the_tolerated_warning(tmp_path: Path) -> None:
    _fake_claude(tmp_path / "bin", _warnings(_TOLERATED), 1, 1)
    assert _run(f"{tmp_path / 'bin'}:/usr/bin:/bin").returncode == 1


def test_the_full_suite_runs_it_before_pytest() -> None:
    full_suite = (REPO / "scripts" / "full_suite.sh").read_text()
    assert full_suite.index("validate_plugin.sh") < full_suite.index("pytest")
