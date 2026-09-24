"""Keeps the proof numbers on the READMEs, the site home and llms.txt true, and the
READMEs' relative links alive.

Facts live between `<!-- facts -->` and `<!-- /facts -->`. Rules:
- tests: the written "N+" must not exceed the real count (a false claim) and must not
  trail it by more than STALE_BAND (stale). Real count = JUnit `tests` minus `skipped`,
  read from the UNIT report the CI job already writes (--junitxml=unit-report.xml in the
  same command that runs the unit suite) — never the full suite's report, and never a
  second --collect-only pass.
- coverage: the written percentage must equal --cov-fail-under in scripts/full_suite.sh
  (the one command that actually runs the full suite at 100%), not pyproject.toml.
- version: the block must contain "v<version>" exactly, matching .claude-plugin/plugin.json's
  "version" — the file every release bumps.
Every file in FACT_FILES must hold exactly one block — a missing block must not pass.

Usage:
    python scripts/check_readme_facts.py --junit unit-report.xml [--repo PATH]
"""

from __future__ import annotations

import json
import re
import sys
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from pathlib import Path

FACT_FILES = (
    "README.md",
    "README.pt.md",
    "guide/en/index.md",
    "guide/pt/index.md",
    "guide/en/llms.txt",
)
LINK_FILES = ("README.md", "README.pt.md")
STALE_BAND = 100

_BLOCK = re.compile(r"<!-- facts -->(.*?)<!-- /facts -->", re.DOTALL)
_TESTS = re.compile(r"(\d[\d.,]*)\+\s*(?:unit\s+)?(?:tests|testes)")
_COVERAGE = re.compile(r"(\d+)%")
_VERSION = re.compile(r"\bv(\d+\.\d+\.\d+)\b")
_COV_GATE = re.compile(r"--cov-fail-under=(\d+)")
_FENCE = re.compile(r"^\s*(```|~~~)")
_HEADING = re.compile(r"^#{1,6}\s+(.*?)\s*#*\s*$")
_MD_LINK = re.compile(r"\]\(([^)\s]+)")
_HTML_REF = re.compile(r"(?:src|srcset|href)=\"([^\"\s]+)")


@dataclass(frozen=True)
class Facts:
    tests: int
    coverage: int
    version: str


def junit_test_count(junit: Path) -> int:
    root = ET.parse(junit).getroot()
    suites = [root] if root.tag == "testsuite" else root.findall("testsuite")
    return sum(int(s.get("tests", "0")) - int(s.get("skipped", "0")) for s in suites)


def real_facts(repo: Path, junit: Path) -> Facts:
    full_suite = (repo / "scripts" / "full_suite.sh").read_text()
    gate = _COV_GATE.search(full_suite)
    if gate is None:
        raise ValueError("scripts/full_suite.sh has no --cov-fail-under")
    plugin = json.loads((repo / ".claude-plugin" / "plugin.json").read_text())
    return Facts(
        tests=junit_test_count(junit),
        coverage=int(gate.group(1)),
        version=plugin["version"],
    )


def _number(raw: str) -> int:
    return int(re.sub(r"[.,]", "", raw))


def fact_problems(name: str, text: str, real: Facts) -> list[str]:
    blocks = _BLOCK.findall(text)
    if len(blocks) != 1:
        return [f"{name}: expected exactly one facts block, found {len(blocks)}"]
    block = blocks[0]
    problems: list[str] = []
    tests = _TESTS.search(block)
    if tests is None:
        problems.append(f'{name}: facts block has no test count ("N+ tests")')
    else:
        claimed = _number(tests.group(1))
        if claimed > real.tests:
            problems.append(
                f"{name}: claims {claimed}+ tests but there are {real.tests} — a false claim"
            )
        elif real.tests - claimed > STALE_BAND:
            problems.append(
                f"{name}: claims {claimed}+ tests, real {real.tests}"
                f" — stale by more than {STALE_BAND}"
            )
    coverage = _COVERAGE.search(block)
    if coverage is None or int(coverage.group(1)) != real.coverage:
        problems.append(f"{name}: coverage must read {real.coverage}% (the CI gate)")
    version = _VERSION.search(block)
    if version is None:
        problems.append(f'{name}: facts block has no version ("vX.Y.Z")')
    elif version.group(1) != real.version:
        problems.append(f"{name}: version must read v{real.version}")
    return problems


def slug(heading: str) -> str:
    text = re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", heading)
    text = text.strip().lower()
    text = re.sub(r"[^\w\- ]", "", text)
    return text.replace(" ", "-")


def anchors(text: str) -> set[str]:
    seen: dict[str, int] = {}
    result: set[str] = set()
    in_fence = False
    for line in text.splitlines():
        if _FENCE.match(line):
            in_fence = not in_fence
            continue
        match = None if in_fence else _HEADING.match(line)
        if match is None:
            continue
        base = slug(match.group(1))
        count = seen.get(base, 0)
        seen[base] = count + 1
        result.add(base if count == 0 else f"{base}-{count}")
    return result


def link_problems(repo: Path, rel_file: str) -> list[str]:
    source = repo / rel_file
    text = source.read_text()
    problems: list[str] = []
    for target in [*_MD_LINK.findall(text), *_HTML_REF.findall(text)]:
        if re.match(r"^[a-z]+:", target):
            continue
        path_part, _, anchor = target.partition("#")
        dest = source if path_part == "" else (source.parent / path_part)
        if not dest.exists():
            problems.append(f"{rel_file}: {target} does not exist")
        elif anchor and dest.suffix == ".md" and anchor not in anchors(dest.read_text()):
            problems.append(f"{rel_file}: {target} — no such anchor")
    return problems


def main(argv: list[str]) -> int:
    args = dict(zip(argv[1::2], argv[2::2], strict=False))
    repo = Path(args.get("--repo", Path(__file__).resolve().parents[1]))
    try:
        real = real_facts(repo, Path(args["--junit"]))
    except ValueError as error:
        print(error, file=sys.stderr)
        return 1
    problems: list[str] = []
    for name in FACT_FILES:
        problems += fact_problems(name, (repo / name).read_text(), real)
    for name in LINK_FILES:
        problems += link_problems(repo, name)
    for problem in problems:
        print(problem, file=sys.stderr)
    if problems:
        return 1
    print(
        f"README facts and links OK ({real.tests} tests, {real.coverage}% gate, v{real.version})."
    )
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
