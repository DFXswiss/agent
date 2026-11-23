"""U-DOC-konzept (KOR-38, KONZEPT.md part D): the central concept file holds, and everything that cites it hits.

`docs/platform/KONZEPT.md` is the first source for everyone working on agent.dfx.swiss: the concept with 47 ideas
(part A), the client's rules E-NN (part B), the interpretations (C) and its maintenance (D). This test checks that
(a) every rule ID is unique, numbered without gaps, anchored and dated; (b) every link from `spec/01-requirements.md`
and from the ADRs into the file names an existing anchor, every requirement row cites the file, the bare section
numbers of the ADRs 001-056 (short concept version) resolve through the numbering map of part A, and newer ADRs cite
the concept by link only; (c) part A carries every section and idea. Internal names are kept out by the repository
scan of `test_hygiene.py`, which covers `docs/platform/`.
"""

from __future__ import annotations

import datetime
import hashlib
import importlib.util
import re
from pathlib import Path
from types import ModuleType

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
DOCS = REPO_ROOT / "docs" / "platform"
KONZEPT = DOCS / "KONZEPT.md"
REQUIREMENTS = DOCS / "spec" / "01-requirements.md"
ADRS = DOCS / "adr"
# ADRs up to this number were written against the short concept version and cite bare section numbers.
LAST_SHORT_VERSION_ADR = 56
# SHA-256 of part A without its anchors (KONZEPT.md part D rule 4: part A stays the client's wording). A change of
# part A is deliberate only: the new digest is set here in the same commit, and the commit says why.
PART_A_SHA256 = "df479f1ddc30cc2139b48df49259ecdbd15d2476e4f0e009ddd81a2b32d8a774"


def _load_docs_module() -> ModuleType:
    """`test_docs.py` (same directory) for its GitHub anchor and link rules; the suite runs in importlib mode."""
    spec = importlib.util.spec_from_file_location("platform_test_docs", Path(__file__).with_name("test_docs.py"))
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


DOCS_RULES = _load_docs_module()

DASH = "\N{EN DASH}"
RULE_HEADING_RE = re.compile(r"^### (E-(\d{2})) " + DASH + r" \S", re.MULTILINE)
DATE_RE = re.compile(r"^- \*\*Datum:\*\* (\d{4}-\d{2}-\d{2})\b", re.MULTILINE)
KONZEPT_LINK_RE = re.compile(r"(?:^|/)KONZEPT\.md(?:#(.*))?$")
ROW_RE = re.compile(r"^\| (R-\d+\.\d+|K-[\d.]+|E-\d{2}) \| ([^|]*) \|", re.MULTILINE)
# "KONZEPT §3", "KONZEPT §14.6", "KONZEPT Grundprinzip"; further numbers of the same list ("§3, §7, §14.6").
SHORT_REF_RE = re.compile(r"KONZEPT (Grundprinzip|§\d+(?:\.\d+)?(?:, §\d+(?:\.\d+)?)*)")
BARE_KONZEPT_RE = re.compile(r"\bKONZEPT\b(?!\.md)")
ANCHOR_SPAN_RE = re.compile(r'<a\s+(?:name|id)="[^"]+"></a>')
ADR_NUMBER_RE = re.compile(r"^(\d{3})-")


def rules(text: str) -> list[tuple[str, str]]:
    """(rule ID, its section text up to the next heading) in order of appearance."""
    found = []
    matches = list(RULE_HEADING_RE.finditer(text))
    for index, match in enumerate(matches):
        end = matches[index + 1].start() if index + 1 < len(matches) else len(text)
        body = text[match.end() : end].split("\n## ", 1)[0]
        found.append((match.group(1), body))
    return found


def rule_problems(text: str) -> list[str]:
    """Rule IDs unique and gapless from E-01, each with its anchor and a valid date."""
    problems = []
    offered = DOCS_RULES.anchors(text)
    found = rules(text)
    ids = [rule_id for rule_id, _ in found]
    expected = [f"E-{n:02d}" for n in range(1, len(ids) + 1)]
    if ids != expected:
        problems.append(f"rule IDs {ids} are not unique and gapless from E-01")
    for rule_id, body in found:
        if rule_id.lower() not in offered:
            problems.append(f"{rule_id}: no anchor #{rule_id.lower()}")
        dates = DATE_RE.findall(body)
        if len(dates) != 1:
            problems.append(f"{rule_id}: needs exactly one date line, found {len(dates)}")
            continue
        try:
            datetime.date.fromisoformat(dates[0])
        except ValueError:
            problems.append(f"{rule_id}: invalid date {dates[0]}")
    return problems


def duplicate_anchors(text: str) -> list[str]:
    ids = DOCS_RULES.HTML_ANCHOR_RE.findall(text)
    return sorted({anchor for anchor in ids if ids.count(anchor) > 1})


def link_problems(source: Path, text: str, offered: set[str]) -> list[str]:
    """Links of `source` into KONZEPT.md that name no anchor, or a missing one."""
    problems = []
    for target in DOCS_RULES.links(text):
        match = KONZEPT_LINK_RE.search(target)
        if match is None:
            continue
        fragment = match.group(1)
        if not fragment:
            continue
        if fragment not in offered:
            problems.append(f"{source.name}: {target} names no anchor of KONZEPT.md")
    return problems


def row_problems(text: str) -> list[str]:
    """Every requirement row (R-, K-, E-; not the R-P guardrails) cites KONZEPT.md in its Ref column."""
    return [
        f"{row}: Ref column does not link KONZEPT.md"
        for row, ref in ROW_RE.findall(text)
        if not any(KONZEPT_LINK_RE.search(target) for target in DOCS_RULES.links(ref))
    ]


def short_version_anchor(ref: str) -> str:
    """Anchor of a section number of the short concept version (numbering map of KONZEPT.md part A)."""
    if ref == "Grundprinzip":
        return "k-gp"
    section, _, item = ref.lstrip("§").partition(".")
    number = int(section)
    if number == 13:
        return "k-idee-26"
    if number == 14:
        return f"k-13-{item}" if item else "k-13"
    if number == 15:
        return "k-14"
    if number > 15:
        return f"k-kurzfassung-{number}"  # the short version ended with section 15
    return f"k-{number}"


def adr_problems(adrs: Path, offered: set[str]) -> list[str]:
    """Bare KONZEPT references: resolvable in the ADRs of the short version, absent from newer ADRs."""
    problems = []
    for path in sorted(adrs.glob("[0-9][0-9][0-9]-*.md")):
        text = path.read_text("utf-8")
        problems += link_problems(path, text, offered)
        numbered = ADR_NUMBER_RE.match(path.name)
        assert numbered is not None
        number = int(numbered.group(1))
        prose = DOCS_RULES.strip_code(re.sub(r"\[[^\]]*\]\([^)]*\)", "", text))
        if number > LAST_SHORT_VERSION_ADR:
            if BARE_KONZEPT_RE.search(prose):
                problems.append(f"{path.name}: cites KONZEPT without a link to an anchor of KONZEPT.md")
            continue
        for match in SHORT_REF_RE.finditer(prose):
            for ref in match.group(1).split(", "):
                anchor = short_version_anchor(ref)
                if anchor not in offered:
                    problems.append(f"{path.name}: KONZEPT {ref} (#{anchor}) is no anchor of KONZEPT.md")
    return problems


def part_a_digest(text: str) -> str:
    """Digest of part A (from its heading up to part B) with the anchors removed and line ends normalised."""
    part = text.split("\n## Teil A", 1)[1].split("\n## Teil B", 1)[0]
    part = ANCHOR_SPAN_RE.sub("", part)
    lines = [line.rstrip() for line in part.splitlines()]
    return hashlib.sha256("\n".join(lines).encode("utf-8")).hexdigest()


def part_a_problems(offered: set[str]) -> list[str]:
    wanted = {"k-gp", *(f"k-{n}" for n in range(1, 17)), *(f"k-idee-{n}" for n in range(26, 48))}
    wanted |= {f"k-13-{n}" for n in range(1, 8)}
    return [f"part A: no anchor #{anchor}" for anchor in sorted(wanted - offered)]


# --- the repository --------------------------------------------------------------------------------------------


def konzept_text() -> str:
    return KONZEPT.read_text("utf-8")


def test_rules_are_unique_anchored_and_dated() -> None:
    text = konzept_text()
    assert rules(text), "no rule E-NN in KONZEPT.md"
    assert rule_problems(text) == []
    assert duplicate_anchors(text) == []


def test_part_a_has_every_section_and_idea() -> None:
    assert part_a_problems(DOCS_RULES.anchors(konzept_text())) == []


def test_requirements_cite_existing_anchors() -> None:
    text = REQUIREMENTS.read_text("utf-8")
    offered = DOCS_RULES.anchors(konzept_text())
    assert link_problems(REQUIREMENTS, text, offered) == []
    assert row_problems(text) == []
    listed = set(re.findall(r"^\| (E-\d{2}) \|", text, re.MULTILINE))
    assert {rule_id for rule_id, _ in rules(konzept_text())} <= listed


def test_adrs_cite_existing_anchors() -> None:
    assert adr_problems(ADRS, DOCS_RULES.anchors(konzept_text())) == []


def test_internal_links_of_the_file_resolve() -> None:
    assert link_problems(KONZEPT, konzept_text(), DOCS_RULES.anchors(konzept_text())) == []


# --- the checks on samples --------------------------------------------------------------------------------------

RULE = '<a id="e-{n}"></a>\n\n### E-{n} ' + DASH + " Rule\n\n- **Datum:** {date}\n- **Regel:** x\n"


def test_rule_problems_name_every_kind() -> None:
    good = RULE.format(n="01", date="2026-11-23") + RULE.format(n="02", date="2026-11-24") + "\n## Teil C\n"
    assert rule_problems(good) == []
    assert [rule_id for rule_id, _ in rules(good)] == ["E-01", "E-02"]
    duplicate = RULE.format(n="01", date="2026-11-23") * 2
    assert rule_problems(duplicate)[0].startswith("rule IDs ['E-01', 'E-01']")
    assert duplicate_anchors(duplicate) == ["e-01"]
    gap = RULE.format(n="02", date="2026-11-23")
    assert "not unique and gapless" in rule_problems(gap)[0]
    unanchored = "### E-01 " + DASH + " Rule\n\n- **Datum:** 2026-11-23\n"
    assert rule_problems(unanchored) == ["E-01: no anchor #e-01"]
    undated = '<a id="e-01"></a>\n### E-01 ' + DASH + " Rule\n- **Regel:** x\n"
    assert rule_problems(undated) == ["E-01: needs exactly one date line, found 0"]
    bad_date = RULE.format(n="01", date="2026-13-40")
    assert rule_problems(bad_date) == ["E-01: invalid date 2026-13-40"]


def test_link_problems_check_only_konzept_fragments(tmp_path: Path) -> None:
    source = tmp_path / "x.md"
    text = "[a](../KONZEPT.md#k-1) [b](../KONZEPT.md#k-99) [c](../KONZEPT.md) [d](other.md#k-99) [e](KONZEPT.md#e-01)"
    assert link_problems(source, text, {"k-1", "e-01"}) == [
        "x.md: ../KONZEPT.md#k-99 names no anchor of KONZEPT.md",
    ]


def test_row_problems() -> None:
    text = (
        "| R-1.1 | [§1 b1](../KONZEPT.md#k-1-b1) | x |\n"
        "| R-1.2 | §1 b2 | x |\n"
        "| K-3.5 | [§3 b5](../KONZEPT.md#k-3-b5) | x |\n"
        "| E-01 | [E-01](other.md#e-01) | x |\n"
        "| R-P.1 | not a concept row | x |\n"
        "| R-0 | 5 |\n"
    )
    assert row_problems(text) == [
        "R-1.2: Ref column does not link KONZEPT.md",
        "E-01: Ref column does not link KONZEPT.md",
    ]


@pytest.mark.parametrize(
    ("ref", "anchor"),
    [
        ("Grundprinzip", "k-gp"),
        ("§3", "k-3"),
        ("§12", "k-12"),
        ("§13", "k-idee-26"),
        ("§14", "k-13"),
        ("§14.6", "k-13-6"),
        ("§15", "k-14"),
        ("§16", "k-kurzfassung-16"),
    ],
)
def test_short_version_anchor(ref: str, anchor: str) -> None:
    assert short_version_anchor(ref) == anchor


def test_adr_problems_by_era(tmp_path: Path) -> None:
    offered = {"k-gp", "k-3", "k-7", "k-13-6", "k-idee-26", "e-01"}
    (tmp_path / "README.md").write_text("KONZEPT §99\n")
    (tmp_path / "013-old.md").write_text("# ADR (KONZEPT §3, §7, §14.6)\nKONZEPT Grundprinzip; `KONZEPT §99` here.\n")
    (tmp_path / "017-old.md").write_text("KONZEPT §13 and KONZEPT §16 and [x](../KONZEPT.md#k-99)\n")
    (tmp_path / "057-new.md").write_text("See [E-01](../KONZEPT.md#e-01) and [KONZEPT §3](../KONZEPT.md#k-3).\n")
    (tmp_path / "058-new.md").write_text("KONZEPT §3 says so.\n")
    assert adr_problems(tmp_path, offered) == [
        "017-old.md: ../KONZEPT.md#k-99 names no anchor of KONZEPT.md",
        "017-old.md: KONZEPT §16 (#k-kurzfassung-16) is no anchor of KONZEPT.md",
        "058-new.md: cites KONZEPT without a link to an anchor of KONZEPT.md",
    ]


def test_part_a_problems() -> None:
    complete = {"k-gp", *(f"k-{n}" for n in range(1, 17)), *(f"k-idee-{n}" for n in range(26, 48))}
    complete |= {f"k-13-{n}" for n in range(1, 8)}
    assert part_a_problems(complete) == []
    missing = complete - {"k-idee-47", "k-16"}
    assert part_a_problems(missing) == ["part A: no anchor #k-16", "part A: no anchor #k-idee-47"]


def test_part_a_keeps_its_wording() -> None:
    assert part_a_digest(konzept_text()) == PART_A_SHA256


def test_part_a_digest_ignores_anchors_only() -> None:
    text = 'x\n## Teil A\n- <a id="k-1-b1"></a>Satz.  \n## Teil B\n'
    plain = "y\n## Teil A\n- Satz.\n## Teil B\n"
    assert part_a_digest(text) == part_a_digest(plain)
    assert part_a_digest(text) != part_a_digest(plain.replace("Satz", "Satz geändert"))
