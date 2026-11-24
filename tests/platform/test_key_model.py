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
TEST_RE = re.compile(r"test:`([^`]+)`")
GAP_RE = re.compile(r"\bgap (KOR-\d+)\b")
GAP_HEADING_RE = re.compile(r"^### (KOR-\d+) ", re.MULTILINE)
CONCEPT_ANCHOR_RE = re.compile(r'<a id="((?:k-(?:gp|\d+|idee-\d+))|e-\d{2}|a-\d)"></a>')
CONCEPT_LINK_RE = re.compile(r"\]\(KONZEPT\.md#([a-z0-9-]+)\)")
STATUS_RE = re.compile(r"^(carried|none|carried \(pending KOR-\d+\)|gap → (KOR-\d+)(, KOR-\d+)*)$")
DASH = "\N{EN DASH}"
TARGET_RE = re.compile(r"\]\(#[a-z0-9-]+\)|\bKM-\d{2}\b|^this (document|section)$|^" + DASH + "$")


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
    problems += [f"{rule}: no test file {test}" for test in tests if not (root / test).is_file()]
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


def concordance_problems(text: str, konzept: str) -> list[str]:
    """Problems of the concordance (section 10) against the anchors of the concept file."""
    part = section(text, 10)
    gap_packages = set(GAP_HEADING_RE.findall(section(text, 11)))
    problems = [f"not in the concordance: {anchor}" for anchor in CONCEPT_ANCHOR_RE.findall(konzept)]
    named = set(CONCEPT_LINK_RE.findall(part))
    problems = [problem for problem in problems if problem.rpartition(" ")[2] not in named]
    for line in part.splitlines():
        cells = [cell.strip() for cell in line.strip().strip("|").split("|")] if line.startswith("| [") else []
        if not cells:
            continue
        status = STATUS_RE.match(cells[2]) if len(cells) == 4 else None
        if status is None:
            problems.append(f"bad row: {cells[0]}")
            continue
        if not TARGET_RE.search(cells[3]):
            problems.append(f"no target: {cells[0]}")
        problems += [
            f"gap {package} has no section in 11"
            for package in re.findall(r"KOR-\d+", cells[2])
            if cells[2].startswith("gap") and package not in gap_packages
        ]
    return problems


def test_rules_name_existing_evidence() -> None:
    assert rule_problems(KEY_MODEL.read_text("utf-8"), REPO_ROOT) == []


def test_concordance_covers_the_whole_concept() -> None:
    assert concordance_problems(KEY_MODEL.read_text("utf-8"), KONZEPT.read_text("utf-8")) == []


def test_the_vectors_found_by_the_comparison_stay() -> None:
    """The vectors KOR-54 added for rules that had none (KM-06, KM-10, KM-15) are cited and present."""
    text = KEY_MODEL.read_text("utf-8")
    for cited in (
        "`log-wraps.json#principal_outside_log_is_no_recipient`",
        "`log-wraps.json#org_key_for_device_without_workgroup`",
        "`log.json#cred_device_of_org_without_consent`",
    ):
        assert cited in text


# The checker itself, on samples.

SAMPLE = f"""# Key model
## 9. Rules and their evidence
| Rule | Statement | Enforced by | Evidence |
|---|---|---|---|
| KM-01 | One. | all | `v.json#a`; `v.json#b`; test:`t.py` |
| KM-02 | Two. | {DASH} | gap KOR-90 |
## 10. Concordance with the concept
| Concept | Bearing | Status | Section |
|---|---|---|---|
| [§1](KONZEPT.md#k-1) | x | carried | [§9](#9-rules-and-their-evidence) |
| [E-01](KONZEPT.md#e-01) | x | gap → KOR-90 | KM-02 |
## 11. Gaps
### KOR-90 {DASH} Something
"""


def _tree(tmp_path: Path) -> Path:
    (tmp_path / "contracts" / "vectors").mkdir(parents=True)
    (tmp_path / "contracts" / "vectors" / "v.json").write_text(
        json.dumps({"a": 1, "invalid": [{"name": "b"}, "x"]}), "utf-8"
    )
    (tmp_path / "t.py").write_text("", "utf-8")
    return tmp_path


def test_checker_accepts_a_complete_sample(tmp_path: Path) -> None:
    root = _tree(tmp_path)
    assert rule_problems(SAMPLE, root) == []
    assert concordance_problems(SAMPLE, '<a id="k-1"></a> <a id="e-01"></a>') == []


@pytest.mark.parametrize(
    ("change", "expected"),
    [
        (("`v.json#b`", "`v.json#c`"), "KM-01: no vector v.json#c"),
        (("`v.json#a`", "`w.json#a`"), "KM-01: no vector file w.json"),
        (("test:`t.py`", "test:`u.py`"), "KM-01: no test file u.py"),
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
        (("", ""), '<a id="k-1"></a><a id="k-idee-30"></a>', "not in the concordance: k-idee-30"),
        (("| carried |", "| maybe |"), '<a id="k-1"></a>', "bad row: [§1](KONZEPT.md#k-1)"),
        (("| [§9](#9-rules-and-their-evidence) |", "| somewhere |"), "", "no target: [§1](KONZEPT.md#k-1)"),
        (("gap → KOR-90", "gap → KOR-92"), "", "gap KOR-92 has no section in 11"),
    ],
)
def test_checker_finds_bad_concordance(change: tuple[str, str], konzept: str, expected: str) -> None:
    text = SAMPLE.replace(*change) if change[0] else SAMPLE
    assert expected in concordance_problems(text, konzept)


def test_missing_section_is_reported() -> None:
    with pytest.raises(AssertionError, match="section 9 missing"):
        section("# nothing\n", 9)
