"""Traceability of the requirements (R-P.4, docs/platform/spec/01-requirements.md "Coverage check").

`traceability.json` maps every requirement ID to the tests that prove it and assigns every test the milestone in
which it is first written (`10-work-packages.md` sections 5 and 7). E2E, PERF and LIVE tests exist when a spec of the
suite carries their tag (`@E2E-06`) in a test title (a string literal of a spec file); gate tests (unit suites, ATK,
CON, coverage and acceptance gates) exist when their evidence paths exist. Requirement tags (`@R-3.10`) in a title
must name a known requirement that lists the test IDs of the same title, and every E2E, PERF or LIVE test a
requirement lists must carry that requirement's tag in at least one of its titles (the report follows the markers).

Checks: every requirement names at least one known test, every referenced test has a milestone and a kind, the
matrix mirrors `01-requirements.md` row by row, no spec names an unknown test or requirement; with a milestone N,
every test of milestone <= N exists and every requirement of milestone <= N has an existing test of milestone <= N
(`Report.due` lists them).
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

MAX_MILESTONE = 3
SPEC_KINDS = frozenset({"e2e", "perf", "live"})
KINDS = SPEC_KINDS | {"gate"}
REPOS = ("agent-core", "agent")
REQUIREMENTS_DOC = Path("docs/platform/spec/01-requirements.md")
SPEC_DIRS = ("specs", "perf", "live")

_ROW_RE = re.compile(r"^\|\s*(R-(?:\d+|P)\.\d+)\s*\|")
_RANGE_RE = re.compile(r"\b(E2E|PERF|ATK|LIVE|CON)-(\d{2})\.\.(?:(?:E2E|PERF|ATK|LIVE|CON)-)?(\d{2})\b")
_TEST_RE = re.compile(r"\b(?:E2E|PERF|ATK|LIVE|CON)-\d{2}\b")
_UNIT_RE = re.compile(r"\bU-(?:HUB|RUN|GST|DBL|WEB)-[a-z]+(?:-[a-z]+)*\b")
_PHRASES = (
    ("this matrix", "GATE-MATRIX"),
    ("coverage gates", "GATE-COVERAGE"),
    ("traceability test", "GATE-TRACEABILITY"),
    ("branch protection", "GATE-BRANCH-PROTECTION"),
)
_LITERAL_RE = re.compile(r"'(?:[^'\\\n]|\\.)*'|\"(?:[^\"\\\n]|\\.)*\"|`(?:[^`\\]|\\.)*`")
_TAG_RE = re.compile(r"@((?:E2E|PERF|LIVE|ATK|CON)-\d{2}|R-(?:\d+|P)\.\d+)(?!\w)")


class TraceabilityError(Exception):
    """The matrix file cannot be read or has the wrong shape."""


@dataclass(frozen=True)
class Test:
    milestone: int
    kind: str
    evidence: tuple[str, ...] = ()


@dataclass(frozen=True)
class Matrix:
    requirements: dict[str, tuple[int, tuple[str, ...]]]
    tests: dict[str, Test]


@dataclass
class Report:
    problems: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    written: set[str] = field(default_factory=set)
    # requirement due up to the checked milestone -> its tests that exist (empty: none, the requirement is red)
    due: dict[str, tuple[str, ...]] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return not self.problems


def expand(cell: str) -> list[str]:
    """The test IDs of one "Tests" cell of `01-requirements.md` in order of appearance, ranges expanded."""
    found: list[tuple[int, str]] = []
    for match in _RANGE_RE.finditer(cell):
        prefix, first, last = match.group(1), int(match.group(2)), int(match.group(3))
        found.extend((match.start(), f"{prefix}-{n:02d}") for n in range(first, last + 1))
    spans = [match.span() for match in _RANGE_RE.finditer(cell)]
    for match in _TEST_RE.finditer(cell):
        if not any(start <= match.start() < end for start, end in spans):
            found.append((match.start(), match.group(0)))
    found.extend((match.start(), match.group(0)) for match in _UNIT_RE.finditer(cell))
    lowered = cell.lower()
    found.extend((lowered.index(phrase), gate) for phrase, gate in _PHRASES if phrase in lowered)
    ordered: list[str] = []
    for _, test_id in sorted(found):
        if test_id not in ordered:
            ordered.append(test_id)
    return ordered


def parse_requirements(text: str) -> dict[str, list[str]]:
    """Requirement ID → test IDs from the tables of `01-requirements.md` (last column of every `| R-…` row)."""
    rows: dict[str, list[str]] = {}
    for line in text.splitlines():
        match = _ROW_RE.match(line)
        if match:
            cells = [cell.strip() for cell in line.strip().strip("|").split("|")]
            rows[match.group(1)] = expand(cells[-1])
    return rows


def _milestone(value: Any, where: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= MAX_MILESTONE:
        raise TraceabilityError(f"{where}: milestone must be an integer 0..{MAX_MILESTONE}")
    return value


def load(path: Path) -> Matrix:
    """Read and shape-check `traceability.json`."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise TraceabilityError(f"{path}: {exc}") from exc
    if not isinstance(data, dict) or not isinstance(data.get("requirements"), dict):
        raise TraceabilityError(f"{path}: expected an object with 'requirements' and 'tests'")
    if not isinstance(data.get("tests"), dict):
        raise TraceabilityError(f"{path}: expected an object with 'requirements' and 'tests'")
    tests: dict[str, Test] = {}
    for test_id, entry in data["tests"].items():
        if not isinstance(entry, dict) or entry.get("kind") not in KINDS:
            raise TraceabilityError(f"{test_id}: kind must be one of {sorted(KINDS)}")
        evidence = entry.get("evidence", [])
        if not isinstance(evidence, list) or not all(_evidence_ok(item) for item in evidence):
            raise TraceabilityError(f"{test_id}: evidence must be a list of '<repo>:<path>'")
        tests[test_id] = Test(_milestone(entry.get("milestone"), test_id), entry["kind"], tuple(evidence))
    requirements: dict[str, tuple[int, tuple[str, ...]]] = {}
    for req_id, entry in data["requirements"].items():
        if not isinstance(entry, dict) or not isinstance(entry.get("tests"), list):
            raise TraceabilityError(f"{req_id}: expected an object with 'milestone' and 'tests'")
        if not all(isinstance(test_id, str) for test_id in entry["tests"]):
            raise TraceabilityError(f"{req_id}: tests must be strings")
        requirements[req_id] = (_milestone(entry.get("milestone"), req_id), tuple(entry["tests"]))
    return Matrix(requirements, tests)


def _evidence_ok(item: Any) -> bool:
    return isinstance(item, str) and item.partition(":")[0] in REPOS and item.partition(":")[2] != ""


def title_tags(source: str) -> list[set[str]]:
    """The tag sets of every string literal of a spec source that carries at least one tag."""
    out: list[set[str]] = []
    for literal in _LITERAL_RE.finditer(source):
        tags = set(_TAG_RE.findall(literal.group(0)))
        if tags:
            out.append(tags)
    return out


def spec_titles(suite: Path) -> list[tuple[str, set[str]]]:
    """(spec file relative to the suite, tags of one title) for every tagged title of every spec file."""
    titles: list[tuple[str, set[str]]] = []
    for sub in SPEC_DIRS:
        for path in sorted((suite / sub).rglob("*.spec.ts")):
            rel = path.relative_to(suite).as_posix()
            titles.extend((rel, tags) for tags in title_tags(path.read_text(encoding="utf-8")))
    return titles


def _check_matrix(matrix: Matrix, report: Report) -> None:
    for req_id, (milestone, tests) in matrix.requirements.items():
        if not tests:
            report.problems.append(f"{req_id}: no test")
            continue
        unknown = [test_id for test_id in tests if test_id not in matrix.tests]
        if unknown:
            report.problems.append(f"{req_id}: unknown test IDs {', '.join(unknown)}")
            continue
        earliest = min(matrix.tests[test_id].milestone for test_id in tests)
        if milestone < earliest:
            report.problems.append(f"{req_id}: milestone {milestone} before its first test (milestone {earliest})")


def _check_document(matrix: Matrix, document: Mapping[str, list[str]], report: Report) -> None:
    for req_id, tests in document.items():
        if req_id not in matrix.requirements:
            report.problems.append(f"{req_id}: in {REQUIREMENTS_DOC} but missing in traceability.json")
        elif list(matrix.requirements[req_id][1]) != tests:
            report.problems.append(f"{req_id}: tests differ from {REQUIREMENTS_DOC} ({', '.join(tests)})")
    for req_id in matrix.requirements:
        if req_id not in document:
            report.problems.append(f"{req_id}: unknown requirement (not in {REQUIREMENTS_DOC})")


def _check_titles(matrix: Matrix, titles: Iterable[tuple[str, set[str]]], report: Report) -> None:
    carried: dict[str, set[str]] = {}
    for spec, tags in titles:
        test_ids = sorted(tag for tag in tags if not tag.startswith("R-"))
        req_ids = sorted(tag for tag in tags if tag.startswith("R-"))
        for test_id in test_ids:
            carried.setdefault(test_id, set()).update(req_ids)
        for test_id in test_ids:
            entry = matrix.tests.get(test_id)
            if entry is None or entry.kind not in SPEC_KINDS:
                report.problems.append(f"{spec}: @{test_id} is not an E2E/PERF/LIVE test of traceability.json")
            else:
                report.written.add(test_id)
        for req_id in req_ids:
            if req_id not in matrix.requirements:
                report.problems.append(f"{spec}: @{req_id} is an unknown requirement")
                continue
            missing = [test_id for test_id in test_ids if test_id not in matrix.requirements[req_id][1]]
            if missing or not test_ids:
                report.problems.append(f"{spec}: @{req_id} does not list {', '.join(missing) or 'a test of the title'}")
    for req_id, (_, tests) in matrix.requirements.items():
        for test_id in tests:
            if test_id in carried and req_id not in carried[test_id]:
                report.problems.append(f"{req_id}: no title tagged @{test_id} carries @{req_id}")


def _exists(test: Test, roots: Mapping[str, Path | None], report: Report, test_id: str) -> bool:
    for item in test.evidence:
        repo, _, rel = item.partition(":")
        root = roots.get(repo)
        if root is None:
            report.notes.append(f"{test_id}: {item} not checked (no {repo} checkout given)")
        elif not (root / rel).exists():
            return False
    return bool(test.evidence)


def _check_milestone(matrix: Matrix, milestone: int, roots: Mapping[str, Path | None], report: Report) -> None:
    present: set[str] = set(report.written)
    for test_id, test in matrix.tests.items():
        if test.kind == "gate" and _exists(test, roots, report, test_id):
            present.add(test_id)
        if test.milestone <= milestone and test_id not in present:
            what = "spec tag" if test.kind in SPEC_KINDS else "evidence"
            report.problems.append(f"{test_id}: due in milestone {test.milestone}, no {what} found")
    for req_id, (req_milestone, tests) in matrix.requirements.items():
        if req_milestone > milestone:
            continue
        report.due[req_id] = tuple(
            test_id for test_id in tests if test_id in present and matrix.tests[test_id].milestone <= milestone
        )
        if not report.due[req_id]:
            report.problems.append(f"{req_id}: due in milestone {req_milestone}, none of its tests exists")


def check(
    matrix: Matrix,
    titles: Iterable[tuple[str, set[str]]],
    *,
    document: Mapping[str, list[str]] | None = None,
    milestone: int | None = None,
    roots: Mapping[str, Path | None] | None = None,
) -> Report:
    """All checks of the module docstring; `roots` maps repository names to checkouts (agent may be None)."""
    report = Report()
    _check_matrix(matrix, report)
    if document is not None:
        _check_document(matrix, document, report)
    _check_titles(matrix, titles, report)
    if milestone is not None:
        _check_milestone(matrix, milestone, roots or {}, report)
    return report


def check_suite(suite: Path, *, milestone: int | None = None, agent: Path | None = None) -> Report:
    """`check` for the suite directory `e2e/platform` of a checkout.

    Evidence of both former repositories (`agent-core:` and `agent:` in the matrix) lies in this checkout since they
    were unified (INF-06); `agent` names another checkout for the `agent:` evidence only on request."""
    repo = suite.parents[1]
    try:
        matrix = load(suite / "traceability.json")
    except TraceabilityError as exc:
        return Report(problems=[str(exc)])
    document = parse_requirements((repo / REQUIREMENTS_DOC).read_text(encoding="utf-8"))
    roots: dict[str, Path | None] = {"agent-core": repo, "agent": agent or repo}
    return check(matrix, spec_titles(suite), document=document, milestone=milestone, roots=roots)
