from __future__ import annotations

import sys
from pathlib import Path
from types import ModuleType

import pytest

_SCRIPTS_DIR = Path(__file__).resolve().parents[2] / "scripts"


def _crf() -> ModuleType:
    if str(_SCRIPTS_DIR) not in sys.path:
        sys.path.insert(0, str(_SCRIPTS_DIR))
    import check_readme_facts

    return check_readme_facts


def _facts(tests: int = 700, coverage: int = 100, version: str = "0.2.8") -> object:
    return _crf().Facts(tests=tests, coverage=coverage, version=version)


def _block(text: str) -> str:
    return f"intro\n<!-- facts -->{text}<!-- /facts -->\n"


def test_true_facts_pass() -> None:
    text = _block("700+ tests · 100% branch coverage (CI-gated) · v0.2.8 · mypy strict")
    assert _crf().fact_problems("README.md", text, _facts()) == []


def test_portuguese_thousands_separator_parses() -> None:
    text = _block("1.700+ testes · 100% de cobertura de branches · v0.2.8")
    assert _crf().fact_problems("README.md", text, _facts(tests=1700)) == []


def test_mandated_proof_line_wording_parses() -> None:
    text = _block(
        "700+ unit tests in CI · 100% branch coverage on the full suite · "
        "Vim + Neovim backends · v0.2.8"
    )
    assert _crf().fact_problems("README.md", text, _facts()) == []


def test_mandated_proof_line_wording_parses_pt() -> None:
    text = _block(
        "700+ testes de unidade no CI · 100% de cobertura de branches na suíte completa · "
        "backends Vim + Neovim · v0.2.8"
    )
    assert _crf().fact_problems("README.pt.md", text, _facts()) == []


def test_a_unit_test_count_above_reality_is_a_false_claim() -> None:
    text = _block("800+ unit tests in CI · 100% · v0.2.8")
    [problem] = _crf().fact_problems("README.md", text, _facts(tests=757))
    assert "false" in problem and "800" in problem and "757" in problem


def test_a_test_count_above_reality_is_a_false_claim() -> None:
    [problem] = _crf().fact_problems("README.md", _block("800+ tests · 100% · v0.2.8"), _facts())
    assert "false" in problem and "800" in problem and "700" in problem


def test_a_test_count_trailing_by_more_than_the_band_is_stale() -> None:
    [problem] = _crf().fact_problems("README.md", _block("550+ tests · 100% · v0.2.8"), _facts())
    assert "stale" in problem


def test_exactly_the_band_is_still_fine() -> None:
    assert _crf().fact_problems("README.md", _block("600+ tests · 100% · v0.2.8"), _facts()) == []


def test_coverage_and_version_mismatches_are_reported() -> None:
    problems = _crf().fact_problems("README.md", _block("700+ tests · 95% · v0.2.7"), _facts())
    assert len(problems) == 2


def test_missing_or_duplicated_block_fails_instead_of_passing_vacuously() -> None:
    assert _crf().fact_problems("README.md", "no block here", _facts()) == [
        "README.md: expected exactly one facts block, found 0"
    ]
    two = _block("700+ tests · 100% · v0.2.8") * 2
    assert _crf().fact_problems("README.md", two, _facts()) == [
        "README.md: expected exactly one facts block, found 2"
    ]


def test_a_block_missing_one_fact_is_reported() -> None:
    [problem] = _crf().fact_problems("README.md", _block("100% · v0.2.8"), _facts())
    assert "test count" in problem


def test_missing_version_in_block_fails() -> None:
    [problem] = _crf().fact_problems("README.md", _block("700+ tests · 100%"), _facts())
    assert "version" in problem


def test_version_must_match_plugin_json() -> None:
    [problem] = _crf().fact_problems(
        "README.md", _block("700+ tests · 100% · v0.2.7"), _facts(version="0.2.8")
    )
    assert "v0.2.8" in problem


def test_junit_sums_every_suite_and_tolerates_missing_skipped(tmp_path: Path) -> None:
    report = tmp_path / "r.xml"
    report.write_text(
        '<testsuites><testsuite tests="10" skipped="2"/><testsuite tests="5"/></testsuites>'
    )
    assert _crf().junit_test_count(report) == 13
    report.write_text('<testsuite tests="7" skipped="1"/>')
    assert _crf().junit_test_count(report) == 6


def _write_repo(
    tmp_path: Path, full_suite_gate: int, pyproject_gate: int | None, version: str
) -> None:
    if pyproject_gate is not None:
        (tmp_path / "pyproject.toml").write_text(
            f'[tool.pytest.ini_options]\naddopts = "--cov --cov-fail-under={pyproject_gate}"\n'
        )
    scripts_dir = tmp_path / "scripts"
    scripts_dir.mkdir()
    scripts_dir.joinpath("full_suite.sh").write_text(
        f'#!/usr/bin/env bash\nexec uv run pytest --cov-fail-under={full_suite_gate} "$@"\n'
    )
    plugin_dir = tmp_path / ".claude-plugin"
    plugin_dir.mkdir()
    plugin_dir.joinpath("plugin.json").write_text(f'{{"version": "{version}"}}')


def test_real_facts_read_the_repo(tmp_path: Path) -> None:
    _write_repo(tmp_path, full_suite_gate=100, pyproject_gate=None, version="0.2.8")
    report = tmp_path / "r.xml"
    report.write_text('<testsuite tests="3"/>')
    assert _crf().real_facts(tmp_path, report) == _facts(tests=3, coverage=100, version="0.2.8")


def test_coverage_gate_is_read_from_full_suite_script(tmp_path: Path) -> None:
    # pyproject.toml carries a DIFFERENT gate on purpose: if the code fell back to reading
    # it, this would read 42 instead of the full_suite.sh's 100.
    _write_repo(tmp_path, full_suite_gate=100, pyproject_gate=42, version="0.2.8")
    report = tmp_path / "r.xml"
    report.write_text('<testsuite tests="3"/>')
    assert _crf().real_facts(tmp_path, report).coverage == 100


def test_slug_follows_github_rules() -> None:
    slug = _crf().slug
    assert slug("Extensions (adding a new backend)") == "extensions-adding-a-new-backend"
    assert slug("Instalação") == "instalação"
    assert slug("`claude-follow` status") == "claude-follow-status"
    assert slug("Sixty seconds of it") == "sixty-seconds-of-it"


def test_duplicate_headings_get_numbered_slugs() -> None:
    assert _crf().anchors("# Setup\n\n## Setup\n\n## Setup\n") == {"setup", "setup-1", "setup-2"}


def test_anchor_ignores_headings_in_code_fences() -> None:
    assert _crf().anchors("## Real\n\n```sh\n# not-a-heading\n```\n") == {"real"}


def test_link_problems_find_dead_files_and_anchors(tmp_path: Path) -> None:
    (tmp_path / "guide").mkdir()
    (tmp_path / "guide/page.md").write_text("## Target\n")
    (tmp_path / "README.md").write_text(
        "[ok](guide/page.md#target) [bad-anchor](guide/page.md#nope) [gone](missing.md) "
        '[self](#local) [web](https://example.com) <img src="assets/x.svg">\n\n## Local\n'
    )
    problems = _crf().link_problems(tmp_path, "README.md")
    assert sorted(problems) == [
        "README.md: assets/x.svg does not exist",
        "README.md: guide/page.md#nope — no such anchor",
        "README.md: missing.md does not exist",
    ]


def test_main_exit_codes(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    _write_repo(tmp_path, full_suite_gate=100, pyproject_gate=None, version="0.2.8")
    report = tmp_path / "r.xml"
    report.write_text('<testsuite tests="1"/>')
    good = _block("1+ tests · 100% branch coverage · v0.2.8")
    for name in _crf().FACT_FILES:
        path = tmp_path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(good)
    assert _crf().main(["x", "--junit", str(report), "--repo", str(tmp_path)]) == 0
    (tmp_path / "README.md").write_text(_block("1+ tests · 95% · v0.2.8"))
    assert _crf().main(["x", "--junit", str(report), "--repo", str(tmp_path)]) == 1
    assert "coverage must read" in capsys.readouterr().err
