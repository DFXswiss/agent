"""Traceability logic (R-P.4): parsing 01-requirements.md, the matrix file, spec tags, milestone checks."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
import test_traceability as cli
from orchestrator import traceability as t
from support import CORE, SUITE


def test_expand_cells() -> None:
    assert t.expand("E2E-01 (incl. `register/begin`), E2E-02, U-HUB-passkeys") == ["E2E-01", "E2E-02", "U-HUB-passkeys"]
    assert t.expand("ATK-01..ATK-03, E2E-10") == ["ATK-01", "ATK-02", "ATK-03", "E2E-10"]
    assert t.expand("LIVE-01..04") == ["LIVE-01", "LIVE-02", "LIVE-03", "LIVE-04"]
    assert t.expand("E2E-04, `agent-runner doctor` assertion in E2E-04") == ["E2E-04"]
    assert t.expand("this matrix; M3 acceptance") == ["GATE-MATRIX"]
    assert t.expand("coverage gates in `make platform-ci` and CI") == ["GATE-COVERAGE"]
    assert t.expand("traceability test `e2e/platform/test_traceability.py`") == ["GATE-TRACEABILITY"]
    assert t.expand("M3 acceptance (branch protection check)") == ["GATE-BRANCH-PROTECTION"]


def test_the_document_has_153_requirements_and_the_matrix_mirrors_it() -> None:
    document = t.parse_requirements((CORE / t.REQUIREMENTS_DOC).read_text())
    assert len(document) == 153
    matrix = t.load(SUITE / "traceability.json")
    assert {req: list(tests) for req, (_, tests) in matrix.requirements.items()} == document
    report = t.check_suite(SUITE, milestone=0)
    assert report.ok, report.problems
    assert "E2E-00" in report.written
    assert any("not checked (no agent checkout given)" in note for note in report.notes)


def matrix_file(tmp_path: Path, data: Any) -> Path:
    path = tmp_path / "traceability.json"
    path.write_text(json.dumps(data) if not isinstance(data, str) else data)
    return path


@pytest.mark.parametrize(
    ("data", "message"),
    [
        ("{not json", "traceability.json"),
        ([], "expected an object with 'requirements' and 'tests'"),
        ({"requirements": {}, "tests": []}, "expected an object with 'requirements' and 'tests'"),
        ({"requirements": {}, "tests": {"X": {"kind": "other", "milestone": 0}}}, "X: kind must be one of"),
        ({"requirements": {}, "tests": {"X": {"kind": "gate", "milestone": 0, "evidence": ["web:x"]}}}, "evidence"),
        ({"requirements": {}, "tests": {"X": {"kind": "gate", "milestone": 0, "evidence": ["agent:"]}}}, "evidence"),
        ({"requirements": {}, "tests": {"X": {"kind": "e2e", "milestone": 4}}}, "X: milestone must be"),
        ({"requirements": {}, "tests": {"X": {"kind": "e2e", "milestone": True}}}, "X: milestone must be"),
        ({"requirements": {"R-1.1": []}, "tests": {}}, "R-1.1: expected an object"),
        ({"requirements": {"R-1.1": {"milestone": 0, "tests": [1]}}, "tests": {}}, "R-1.1: tests must be strings"),
    ],
)
def test_load_refuses_bad_files(tmp_path: Path, data: Any, message: str) -> None:
    with pytest.raises(t.TraceabilityError, match=message):
        t.load(matrix_file(tmp_path, data))


MATRIX = {
    "requirements": {
        "R-1.1": {"milestone": 1, "tests": ["E2E-01", "U-HUB-x"]},
        "R-1.2": {"milestone": 0, "tests": ["E2E-02"]},
        "R-1.3": {"milestone": 0, "tests": ["E2E-99"]},
        "R-1.4": {"milestone": 0, "tests": []},
        "R-P.1": {"milestone": 0, "tests": ["GATE-A", "GATE-B"]},
        "R-P.2": {"milestone": 0, "tests": ["ATK-01", "ATK-02"]},
    },
    "tests": {
        "E2E-01": {"milestone": 1, "kind": "e2e"},
        "E2E-02": {"milestone": 2, "kind": "e2e"},
        "E2E-03": {"milestone": 0, "kind": "perf"},
        "U-HUB-x": {"milestone": 0, "kind": "gate", "evidence": ["agent-core:present.txt"]},
        "GATE-A": {"milestone": 0, "kind": "gate", "evidence": ["agent:missing.txt"]},
        "GATE-B": {"milestone": 3, "kind": "gate"},
        "ATK-01": {"milestone": 0, "kind": "gate", "evidence": ["agent:atk"]},
        "ATK-02": {"milestone": 2, "kind": "gate", "evidence": ["agent:atk"]},
    },
}


def test_check_reports_every_problem(tmp_path: Path) -> None:
    (tmp_path / "present.txt").write_text("x")
    (tmp_path / "agent" / "atk").mkdir(parents=True)
    matrix = t.load(matrix_file(tmp_path, MATRIX))
    titles = [
        ("specs/a.spec.ts", {"E2E-01", "R-1.1"}),
        ("specs/b.spec.ts", {"ATK-01"}),
        ("specs/c.spec.ts", {"R-9.9"}),
        ("specs/d.spec.ts", {"E2E-03", "R-1.2"}),
        ("specs/e.spec.ts", {"R-1.2"}),
    ]
    document = {"R-1.1": ["E2E-01", "U-HUB-x"], "R-1.2": ["E2E-02", "E2E-05"], "R-1.5": ["E2E-05"]}
    report = t.check(
        matrix, titles, document=document, milestone=1, roots={"agent-core": tmp_path, "agent": tmp_path / "agent"}
    )
    problems = "\n".join(report.problems)
    assert "R-1.2: milestone 0 before its first test (milestone 2)" in problems
    assert "R-1.3: unknown test IDs E2E-99" in problems
    assert "R-1.4: no test" in problems
    assert "R-1.2: tests differ from" in problems
    assert "R-1.5: in docs/platform/spec/01-requirements.md but missing" in problems
    assert "R-1.3: unknown requirement (not in" in problems
    assert "specs/b.spec.ts: @ATK-01 is not an E2E/PERF/LIVE test" in problems
    assert "specs/c.spec.ts: @R-9.9 is an unknown requirement" in problems
    assert "specs/d.spec.ts: @R-1.2 does not list E2E-03" in problems
    assert "specs/e.spec.ts: @R-1.2 does not list a test of the title" in problems
    assert "GATE-A: due in milestone 0, no evidence found" in problems
    assert "R-P.1: due in milestone 0, none of its tests exists" in problems
    assert "ATK-01: due in milestone 0" not in problems
    assert "E2E-01: due" not in problems
    assert report.written == {"E2E-01", "E2E-03"}
    assert report.due["R-1.1"] == ("E2E-01", "U-HUB-x")
    assert report.due["R-P.2"] == ("ATK-01",)  # ATK-02 shares the evidence but is due only in milestone 2
    assert report.due["R-P.1"] == ()
    assert report.due["R-1.3"] == ()
    assert not report.ok
    quiet = t.check(matrix, [], milestone=0)
    assert any(note.startswith("U-HUB-x: agent-core:present.txt not checked") for note in quiet.notes)
    assert "E2E-03: due in milestone 0, no spec tag found" in quiet.problems
    assert "R-1.1" not in quiet.due  # due only in milestone 1


def test_title_tags_and_spec_scan(tmp_path: Path) -> None:
    source = (
        "// @E2E-77 in a comment is ignored\n"
        "test('@E2E-06 @R-3.10 first task', () => {});\n"
        'test("@PERF-01 timing @needs-vm", () => {});\n'
        "test(`@LIVE-01 live`, () => {});\n"
        "const x = 'no tags';\n"
        "test('@E2E-061 is not E2E-06', () => {});\n"
    )
    assert t.title_tags(source) == [{"E2E-06", "R-3.10"}, {"PERF-01"}, {"LIVE-01"}]
    suite = tmp_path / "e2e" / "platform"
    (suite / "perf").mkdir(parents=True)
    (suite / "perf" / "p.spec.ts").write_text(source)
    assert [rel for rel, _ in t.spec_titles(suite)] == ["perf/p.spec.ts"] * 3


def test_check_suite_reports_a_broken_matrix(tmp_path: Path) -> None:
    suite = tmp_path / "e2e" / "platform"
    suite.mkdir(parents=True)
    (suite / "traceability.json").write_text("{")
    report = t.check_suite(suite)
    assert not report.ok
    assert "traceability.json" in report.problems[0]


def test_report_lists_every_due_requirement(capsys: pytest.CaptureFixture[str]) -> None:
    report = t.Report(due={"R-1.1": ("E2E-01", "U-HUB-x"), "R-P.1": ()})
    report.problems.append("R-P.1: due in milestone 0, none of its tests exists")
    cli.print_report(report, 0)
    out = capsys.readouterr().out.splitlines()
    assert "requirement R-1.1: green (E2E-01, U-HUB-x)" in out
    assert "requirement R-P.1: RED (no test present)" in out
    assert out[-1] == "traceability (milestone 0): RED, 0 spec test IDs written, 1 of 2 due requirements green"
    cli.print_report(t.Report(), None)
    assert capsys.readouterr().out == "traceability (structure): green, 0 spec test IDs written\n"


def test_a_listed_spec_test_must_carry_the_requirement_tag(tmp_path: Path) -> None:
    matrix = t.load(
        matrix_file(
            tmp_path,
            {
                "requirements": {
                    "R-1.1": {"milestone": 1, "tests": ["E2E-01", "E2E-02"]},
                    "R-1.2": {"milestone": 1, "tests": ["E2E-01", "E2E-03"]},
                },
                "tests": {
                    "E2E-01": {"milestone": 1, "kind": "e2e"},
                    "E2E-02": {"milestone": 1, "kind": "e2e"},
                    "E2E-03": {"milestone": 1, "kind": "e2e"},
                },
            },
        )
    )
    titles = [
        ("specs/a.spec.ts", {"E2E-01", "R-1.1"}),
        ("specs/a.spec.ts", {"E2E-01", "R-1.2"}),
        ("specs/b.spec.ts", {"E2E-02"}),
    ]
    report = t.check(matrix, titles)
    assert report.problems == ["R-1.1: no title tagged @E2E-02 carries @R-1.1"]
    # a listed spec test that no title carries yet (E2E-03) is the milestone check's business
    assert t.check(matrix, [*titles, ("specs/b.spec.ts", {"E2E-02", "R-1.1"})]).ok
