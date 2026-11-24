"""U-DOC-key-model (KOR-54): the key model is closed over the concept and every rule names evidence that exists.

`docs/platform/key-model.md` describes who holds which key, who receives which scope when, revocation and rotation,
where plaintext lies and what the hub sees. This test keeps it honest (rule E-19 of the client):

- every rule `KM-NN` of section 9 is numbered without gaps and names evidence: a shared vector of `contracts/vectors/`
  by file and name (run by the hub reference, the runner copy and the web core), a test file, or a gap with the
  package that closes it;
- every gap package has its requirement section in section 11;
- section 10 (concordance) names every section, idea, rule and interpretation of `KONZEPT.md` and gives each row a
  status from the closed list and a target in this document.

Links and anchors themselves are checked by `test_docs.py`; internal names by `test_hygiene.py`.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
DOCS = REPO_ROOT / "docs" / "platform"
KEY_MODEL = DOCS / "key-model.md"
KONZEPT = DOCS / "KONZEPT.md"
VECTORS = REPO_ROOT / "contracts" / "vectors"

RULE_ROW_RE = re.compile(r"^\| (KM-(\d{2})) \| (.+) \| (.+) \| (.+) \|$", re.MULTILINE)
VECTOR_RE = re.compile(r"`([a-z0-9-]+\.json)#([A-Za-z0-9_]+)`")
TEST_RE = re.compile(r"test:`([^`]+?)(?:::([^`]+))?`")
GAP_RE = re.compile(r"\bgap (KOR-\d+)\b")
GAP_HEADING_RE = re.compile(r"^### (KOR-\d+) ", re.MULTILINE)
KM_RE = re.compile(r"\bKM-\d{2}\b")
CONCEPT_ANCHOR_RE = re.compile(r'<a id="((?:k-(?:gp|\d+|idee-\d+))|e-\d{2}|a-\d)"></a>')
CONCEPT_LINK_RE = re.compile(r"\]\(KONZEPT\.md#([a-z0-9-]+)\)")
# The points of the concept that bear on keys and therefore need a row of their own (whole sections are found above).
KEY_POINTS = (
    "k-1-b4",
    "k-3-b3",
    "k-5-b1",
    "k-5-b2",
    "k-5-b3",
    "k-5-b4",
    "k-5-b5",
    "k-8-b1",
    "k-8-b2",
    "k-8-b3",
    "k-8-b4",
    "k-8-b5",
    "k-8-b6",
    "k-13-1",
    "k-16-b7",
)
STATUS_RE = re.compile(r"^(carried|none|decision needed|carried \(pending KOR-\d+\)|gap → (KOR-\d+)(, KOR-\d+)*)$")
DASH = "\N{EN DASH}"
TARGET_RE = re.compile(r"\]\(#[a-z0-9-]+\)|\bKM-\d{2}\b|^this (document|section)$")
# The vectors KOR-54 added for rules that had none (KM-06, KM-10, KM-15), with their positive controls.
FOUND_VECTORS = (
    "`log-wraps.json#principal_outside_log_is_no_recipient`",
    "`log-wraps.json#org_key_for_device_without_workgroup`",
    "`log-wraps.json#W2_org_key_for_device_with_workgroup`",
    "`log.json#cred_device_of_org_without_consent`",
    "`log-wraps.json#cred_device_of_org_with_consent`",
)


def section(text: str, number: int) -> str:
    """The text of `## <number>.` up to the next `## ` heading."""
    match = re.search(rf"^## {number}\. .*?(?=^## |\Z)", text, re.MULTILINE | re.DOTALL)
    assert match is not None, f"section {number} missing"
    return match.group(0)


def vector_names(document: Any) -> set[str]:
    """Names a vector file offers: top-level keys and the `name` of every object in a top-level list."""
    names = set(document)
    for value in document.values():
        if isinstance(value, list):
            names |= {item["name"] for item in value if isinstance(item, dict) and "name" in item}
    return names


def rules(text: str) -> dict[str, str]:
    """Rule ID -> evidence cell of the rule table of section 9."""
    return {rule: evidence for rule, _, _, _, evidence in RULE_ROW_RE.findall(section(text, 9))}


def only_gap(evidence: str) -> bool:
    """The rule is not built: its evidence is a gap and nothing else."""
    return bool(GAP_RE.search(evidence)) and not VECTOR_RE.search(evidence) and not TEST_RE.search(evidence)


def evidence_problems(rule: str, evidence: str, root: Path, gap_packages: set[str]) -> list[str]:
    """Problems of one rule's evidence cell (empty: every item resolves and there is at least one)."""
    vectors = VECTOR_RE.findall(evidence)
    tests = TEST_RE.findall(evidence)
    gaps = GAP_RE.findall(evidence)
    problems = [] if vectors or tests or gaps else [f"{rule}: no evidence"]
    for file, name in vectors:
        path = root / "contracts" / "vectors" / file
        if not path.exists():
            problems.append(f"{rule}: no vector file {file}")
        elif name not in vector_names(json.loads(path.read_text("utf-8"))):
            problems.append(f"{rule}: no vector {file}#{name}")
    for test, name in tests:
        path = root / test
        if not path.is_file():
            problems.append(f"{rule}: no test file {test}")
        elif not name:
            problems.append(f"{rule}: no test name for {test}")
        elif name not in path.read_text("utf-8"):
            problems.append(f"{rule}: no test {name} in {test}")
    problems += [f"{rule}: gap {package} has no section in 11" for package in gaps if package not in gap_packages]
    return problems


def rule_problems(text: str, root: Path) -> list[str]:
    """Problems of the rule table of section 9."""
    rows = RULE_ROW_RE.findall(section(text, 9))
    gap_packages = set(GAP_HEADING_RE.findall(section(text, 11)))
    numbers = [int(number) for _, number, _, _, _ in rows]
    problems = [] if numbers == list(range(1, len(numbers) + 1)) and numbers else ["rules not numbered KM-01 …"]
    for rule, _, _, _, evidence in rows:
        problems += evidence_problems(rule, evidence, root, gap_packages)
    return problems


def row_problems(cells: list[str], known: dict[str, str], gap_packages: set[str]) -> list[str]:
    """Problems of one concordance row `[concept, bearing, status, target]`."""
    status = STATUS_RE.match(cells[2]) if len(cells) == 4 else None
    if status is None:
        return [f"bad row: {cells[0]}"]
    name, value, target = cells[0], cells[2], cells[3]
    if value == "none":
        return [] if target == DASH or TARGET_RE.search(target) else [f"no target: {name}"]
    problems = [] if TARGET_RE.search(target) else [f"no target: {name}"]
    cited = KM_RE.findall(target)
    problems += [f"unknown rule {rule}: {name}" for rule in cited if rule not in known]
    if value.startswith("carried"):
        problems += [f"carried but {rule} is a gap: {name}" for rule in cited if only_gap(known.get(rule, ""))]
    if value.startswith("gap"):
        packages = re.findall(r"KOR-\d+", value)
        problems += [f"gap {package} has no section in 11" for package in packages if package not in gap_packages]
        named = {package for rule in cited for package in GAP_RE.findall(known.get(rule, ""))}
        if cited and not named & set(packages):
            problems.append(f"gap row cites no rule with that gap: {name}")
    return problems


def concordance_problems(text: str, konzept: str) -> list[str]:
    """Problems of the concordance (section 10) against the anchors of the concept file and the rules of section 9."""
    part = section(text, 10)
    gap_packages = set(GAP_HEADING_RE.findall(section(text, 11)))
    known = rules(text)
    named = set(CONCEPT_LINK_RE.findall(part))
    required = [*CONCEPT_ANCHOR_RE.findall(konzept), *(point for point in KEY_POINTS if f'id="{point}"' in konzept)]
    problems = [f"not in the concordance: {anchor}" for anchor in required if anchor not in named]
    for line in part.splitlines():
        if line.startswith("| ["):
            problems += row_problems([cell.strip() for cell in line.strip().strip("|").split("|")], known, gap_packages)
    return problems


def test_rules_name_existing_evidence() -> None:
    assert rule_problems(KEY_MODEL.read_text("utf-8"), REPO_ROOT) == []


def test_concordance_covers_the_whole_concept() -> None:
    assert concordance_problems(KEY_MODEL.read_text("utf-8"), KONZEPT.read_text("utf-8")) == []


def test_every_key_point_exists_in_the_concept() -> None:
    konzept = KONZEPT.read_text("utf-8")
    assert [point for point in KEY_POINTS if f'<a id="{point}"></a>' not in konzept] == []


def test_the_vectors_found_by_the_comparison_stay() -> None:
    """The vectors KOR-54 added are cited by their rules (and therefore exist, `test_rules_name_existing_evidence`)."""
    evidence = " ".join(rules(KEY_MODEL.read_text("utf-8")).values())
    assert [cited for cited in FOUND_VECTORS if cited not in evidence] == []


# The checker itself, on samples.

SAMPLE = f"""# Key model
## 9. Rules and their evidence
| Rule | Statement | Enforced by | Evidence |
|---|---|---|---|
| KM-01 | One. | all | `v.json#a`; `v.json#b`; test:`t.py::test_one` |
| KM-02 | Two. | {DASH} | gap KOR-90 |
## 10. Concordance with the concept
| Concept | Bearing | Status | Section |
|---|---|---|---|
| [§1](KONZEPT.md#k-1) | x | carried | [§9](#9-rules-and-their-evidence), KM-01 |
| [§2](KONZEPT.md#k-2) | x | none | {DASH} |
| [E-01](KONZEPT.md#e-01) | x | gap → KOR-90 | KM-02 |
## 11. Gaps
### KOR-90 {DASH} Something
"""
CONCEPT_SAMPLE = '<a id="k-1"></a> <a id="k-2"></a> <a id="e-01"></a>'


def _tree(tmp_path: Path) -> Path:
    (tmp_path / "contracts" / "vectors").mkdir(parents=True)
    (tmp_path / "contracts" / "vectors" / "v.json").write_text(
        json.dumps({"a": 1, "invalid": [{"name": "b"}, "x"]}), "utf-8"
    )
    (tmp_path / "t.py").write_text("def test_one() -> None: ...\n", "utf-8")
    return tmp_path


def test_checker_accepts_a_complete_sample(tmp_path: Path) -> None:
    root = _tree(tmp_path)
    assert rule_problems(SAMPLE, root) == []
    assert concordance_problems(SAMPLE, CONCEPT_SAMPLE) == []


@pytest.mark.parametrize(
    ("change", "expected"),
    [
        (("`v.json#b`", "`v.json#c`"), "KM-01: no vector v.json#c"),
        (("`v.json#a`", "`w.json#a`"), "KM-01: no vector file w.json"),
        (("test:`t.py::test_one`", "test:`u.py::test_one`"), "KM-01: no test file u.py"),
        (("test:`t.py::test_one`", "test:`t.py`"), "KM-01: no test name for t.py"),
        (("test:`t.py::test_one`", "test:`t.py::test_two`"), "KM-01: no test test_two in t.py"),
        (("gap KOR-90 |", "gap KOR-91 |"), "KM-02: gap KOR-91 has no section in 11"),
        (("gap KOR-90 |", "nothing |"), "KM-02: no evidence"),
        (("| KM-02 |", "| KM-03 |"), "rules not numbered KM-01 …"),
    ],
)
def test_checker_finds_bad_rules(tmp_path: Path, change: tuple[str, str], expected: str) -> None:
    assert expected in rule_problems(SAMPLE.replace(*change), _tree(tmp_path))


@pytest.mark.parametrize(
    ("change", "konzept", "expected"),
    [
        (("", ""), CONCEPT_SAMPLE + '<a id="k-idee-30"></a>', "not in the concordance: k-idee-30"),
        (("", ""), CONCEPT_SAMPLE + '<a id="k-5-b1"></a>', "not in the concordance: k-5-b1"),
        (("| carried |", "| maybe |"), "", "bad row: [§1](KONZEPT.md#k-1)"),
        (("| [§9](#9-rules-and-their-evidence), KM-01 |", "| somewhere |"), "", "no target: [§1](KONZEPT.md#k-1)"),
        (("| none |", "| carried |"), "", "no target: [§2](KONZEPT.md#k-2)"),
        ((f"| none | {DASH} |", "| none | later |"), "", "no target: [§2](KONZEPT.md#k-2)"),
        (("gap → KOR-90", "gap → KOR-92"), "", "gap KOR-92 has no section in 11"),
        (("gap → KOR-90", "gap → KOR-92"), "", "gap row cites no rule with that gap: [E-01](KONZEPT.md#e-01)"),
        (("gap → KOR-90 |", "carried |"), "", "carried but KM-02 is a gap: [E-01](KONZEPT.md#e-01)"),
        ((", KM-01 |", ", KM-09 |"), "", "unknown rule KM-09: [§1](KONZEPT.md#k-1)"),
    ],
)
def test_checker_finds_bad_concordance(change: tuple[str, str], konzept: str, expected: str) -> None:
    text = SAMPLE.replace(*change) if change[0] else SAMPLE
    assert expected in concordance_problems(text, konzept)


def test_missing_section_is_reported() -> None:
    with pytest.raises(AssertionError, match="section 9 missing"):
        section("# nothing\n", 9)
