"""U-DOC-links (07-testing.md section 7, DOC-01): the platform documentation is complete and its links resolve.

Every relative Markdown link under `docs/platform/` must name an existing file inside the repository and, with a
fragment, an existing heading of the target document (GitHub anchor rules). External links (`https:`, `mailto:`) are
not fetched: the suite is hermetic. The indexes must list every ADR, runbook and manual check, and the specification
must be complete. The same file runs in `agent` and `agent-core`; both repositories carry the same `docs/platform/spec/`
and `docs/platform/adr/` (DOC-01 decision 1).
"""

from __future__ import annotations

import filecmp
import os
import re
from collections.abc import Iterator
from pathlib import Path
from urllib.parse import unquote

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
DOCS = REPO_ROOT / "docs" / "platform"

SPEC_FILES = (
    "00-overview.md",
    "01-requirements.md",
    "02-data-model.md",
    "03-api.md",
    "04-crypto.md",
    "05-runner.md",
    "06-web.md",
    "07-testing.md",
    "08-monitoring.md",
    "09-milestones.md",
    "10-work-packages.md",
    "11-agent-access.md",
    "13-work-vm.md",
)
# Runbooks the work packages require (09-milestones.md WP-M3-07, DOC-01 scope).
RUNBOOKS = (
    "platform-root-key.md",
    "device-certification.md",
    "golden-image-release.md",
    "claude-code-upgrade-gate.md",
    "runner-installation.md",
    "hub-operation.md",
    "monitoring-patch-handover.md",
    "export-restore.md",
    "local-development-and-tests.md",
)

# Parts of docs/platform that are the same in both repositories (DOC-01 decision 1); their links stay inside
# docs/platform so that they resolve in both checkouts.
# CUTOVER.md is authored in agent-core and kept as an identical copy in agent (DOC-02).
SHARED = (
    "README.md",
    "PLATFORM-DESIGN.md",
    "CUTOVER.md",
    "spec-deviations.md",
    "spec",
    "adr",
    "runbooks",
    "manual-checks",
)
SIBLINGS = {"agent": "agent-core", "agent-core": "agent"}

FENCE_RE = re.compile(r"^(```|~~~).*?^\1[^\n]*$", re.MULTILINE | re.DOTALL)
INLINE_CODE_RE = re.compile(r"(`+)(?:(?!\1).)+?\1", re.DOTALL)
LINK_RE = re.compile(r"!?\[(?:[^\[\]]|\[[^\[\]]*\])*\]\(\s*<?([^)\s>]+)>?(?:\s+\"[^\"]*\")?\s*\)")
HEADING_RE = re.compile(r"^(#{1,6})[ \t]+(.+?)[ \t#]*$", re.MULTILINE)
HTML_ANCHOR_RE = re.compile(r"<a\s+(?:name|id)=\"([^\"]+)\"", re.IGNORECASE)
EXTERNAL_RE = re.compile(r"^[a-z][a-z0-9+.-]*:", re.IGNORECASE)
ADR_RE = re.compile(r"^(\d{3})-[a-z0-9-]+\.md$")
# A register row that waits for a correction package: "open defect until KOR-04".
OPEN_UNTIL_RE = re.compile(r"open defect until ([A-Z]+-\d+)")
# The closed list of the register's "Handling" column (its status paragraph).
HANDLING = ("spec updated", "as built", "deviation", "open defect")
# A concrete path into the documentation named in a code span (`docs/platform/x.md`, `…/x.md#part`, `…/x.md:12`);
# patterns with placeholders or globs (`<id>`, `**`) are not paths.
DOC_PATH_RE = re.compile(r"`docs/platform/([^`\s<>*#:]*)[^`\s<>*]*`")


def strip_code(text: str) -> str:
    """Text without fenced blocks and inline code spans (links inside code are not links)."""
    return INLINE_CODE_RE.sub("", FENCE_RE.sub("", text))


def slug(heading: str) -> str:
    """GitHub anchor of a heading: link text kept, code marks dropped, lower case, punctuation removed."""
    text = re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", heading)
    text = re.sub(r"[`*]", "", text).strip().lower()
    text = re.sub(r"[^\w\- ]", "", text)
    return text.replace(" ", "-")


def anchors(text: str) -> set[str]:
    """Every anchor a document offers: heading slugs (duplicates numbered like GitHub) and explicit HTML anchors."""
    found: set[str] = set(HTML_ANCHOR_RE.findall(text))
    seen: dict[str, int] = {}
    for match in HEADING_RE.finditer(FENCE_RE.sub("", text)):
        base = slug(match.group(2))
        count = seen.get(base, 0)
        seen[base] = count + 1
        found.add(base if count == 0 else f"{base}-{count}")
    return found


def links(text: str) -> list[str]:
    return LINK_RE.findall(strip_code(text))


def link_problems(source: Path, target: str, root: Path) -> list[str]:
    """Problems of one link target written in `source` (empty list: the link resolves)."""
    if EXTERNAL_RE.match(target):
        return []
    path_part, _, fragment = target.partition("#")
    resolved = (source.parent / unquote(path_part)).resolve() if path_part else source.resolve()
    if not resolved.is_relative_to(root.resolve()):
        return [f"{target}: leaves the repository"]
    if not resolved.exists():
        return [f"{target}: no such file"]
    if fragment and resolved.suffix == ".md" and unquote(fragment) not in anchors(resolved.read_text("utf-8")):
        return [f"{target}: no heading #{fragment}"]
    return []


def markdown_files(docs: Path) -> Iterator[Path]:
    yield from sorted(docs.rglob("*.md"))


def check_links(root: Path) -> list[str]:
    problems = []
    for path in markdown_files(root / "docs" / "platform"):
        rel = path.relative_to(root)
        for target in links(path.read_text("utf-8")):
            problems += [f"{rel}: {item}" for item in link_problems(path, target, root)]
    return problems


def shared_files(docs: Path) -> list[Path]:
    """The shared documentation files, relative to `docs`."""
    found: list[Path] = []
    for name in SHARED:
        top = docs / name
        found += [top] if top.is_file() else sorted(path for path in top.rglob("*") if path.is_file())
    return sorted(path.relative_to(docs) for path in found)


def leaving_links(docs: Path) -> list[str]:
    """Links of the shared files whose target lies outside `docs` (it would not exist in the other repository)."""
    problems = []
    for rel in shared_files(docs):
        if rel.suffix != ".md":
            continue
        source = docs / rel
        for target in links(source.read_text("utf-8")):
            path_part = target.partition("#")[0]
            if EXTERNAL_RE.match(target) or not path_part:
                continue
            if not (source.parent / unquote(path_part)).resolve().is_relative_to(docs.resolve()):
                problems.append(f"{rel}: {target}")
    return problems


def sibling_docs(root: Path, env: dict[str, str]) -> Path | None:
    """docs/platform of the other repository: `PLATFORM_SIBLING_CHECKOUT`, else `../<other>` or, in a work-package
    worktree `wt/<repo>/<id>`, `../../<other>/<id>`."""
    if env.get("PLATFORM_SIBLING_CHECKOUT"):
        return Path(env["PLATFORM_SIBLING_CHECKOUT"]) / "docs" / "platform"
    for repo, other in SIBLINGS.items():
        for candidate in (root.parent / other, root.parent.parent / other / root.name):
            if repo in (root.name, root.parent.name) and (candidate / "docs" / "platform" / "spec").is_dir():
                return candidate / "docs" / "platform"
    return None


def differences(docs: Path, other: Path) -> list[str]:
    """Shared files that differ between the two trees or exist in one only."""
    mine, theirs = set(shared_files(docs)), set(shared_files(other))
    changed = [rel for rel in mine & theirs if not filecmp.cmp(docs / rel, other / rel, shallow=False)]
    return sorted(str(rel) for rel in (mine ^ theirs) | set(changed))


def unknown_doc_paths(docs: Path, other: Path | None) -> list[str]:
    """Paths `docs/platform/...` named in code spans of the shared files that exist in neither repository.

    A path into a shared part must exist here (the other repository has the same file). A path into a part that each
    repository keeps for itself (`decisions/`, `acceptance/` ...) must exist here or in `other`; without the other
    checkout such a path is only checked when it exists here."""
    problems = []
    for rel in shared_files(docs):
        if rel.suffix != ".md":
            continue
        for named in DOC_PATH_RE.findall((docs / rel).read_text("utf-8")):
            here = (docs / named).exists()
            parts = Path(named).parts
            shared = bool(parts) and parts[0] in SHARED
            if here or (not shared and (other is None or (other / named).exists())):
                continue
            problems.append(f"{rel}: docs/platform/{named}")
    return problems


def stale_open_defects(docs: Path, other: Path | None) -> list[str]:
    """Packages named by "open defect until <id>" in the register that are done: their decisions file exists in this
    repository or in `other` (a package writes `decisions/<id>.md` with its merge)."""
    named = OPEN_UNTIL_RE.findall((docs / "spec-deviations.md").read_text("utf-8"))
    return [
        ident for ident in named if any((root / "decisions" / f"{ident}.md").exists() for root in (docs, other) if root)
    ]


def unknown_handling(register: str) -> list[str]:
    """Register rows whose last cell is not one of `HANDLING` (an open defect may name its package: "… until <id>")."""
    problems = []
    for line in register.splitlines():
        cells = [cell.strip() for cell in line.strip().strip("|").split("|")] if line.startswith("|") else []
        if len(cells) < 2 or cells[-1] in ("Handling", "") or set(cells[-1]) <= set("-:"):
            continue
        if cells[-1] not in HANDLING and not OPEN_UNTIL_RE.fullmatch(cells[-1]):
            problems.append(cells[0])
    return problems


def unindexed(directory: Path, names: list[str]) -> list[str]:
    """Names of `directory` that its README.md does not link to."""
    readme = directory / "README.md"
    listed = {target.partition("#")[0] for target in links(readme.read_text("utf-8"))} if readme.exists() else set()
    return [name for name in names if name not in listed]


def test_links_resolve() -> None:
    assert check_links(REPO_ROOT) == []


def test_specification_is_complete() -> None:
    assert sorted(path.name for path in (DOCS / "spec").glob("*.md")) == list(SPEC_FILES)


def test_adrs_are_numbered_without_gaps_and_indexed() -> None:
    names = sorted(path.name for path in (DOCS / "adr").glob("*.md") if path.name != "README.md")
    numbers = [int(match.group(1)) for name in names if (match := ADR_RE.match(name))]
    assert len(numbers) == len(names)
    assert numbers == list(range(1, len(names) + 1))
    assert unindexed(DOCS / "adr", names) == []


def test_runbooks_exist_and_are_indexed() -> None:
    names = sorted(path.name for path in (DOCS / "runbooks").glob("*.md") if path.name != "README.md")
    assert set(RUNBOOKS) <= set(names)
    assert unindexed(DOCS / "runbooks", names) == []


def test_manual_checks_are_indexed() -> None:
    names = sorted(path.name for path in (DOCS / "manual-checks").glob("*.md") if path.name != "README.md")
    assert names
    assert unindexed(DOCS / "manual-checks", names) == []


def test_entry_page_links_every_area() -> None:
    targets = {target.partition("#")[0] for target in links((DOCS / "README.md").read_text("utf-8"))}
    expected = {"PLATFORM-DESIGN.md", "spec/00-overview.md", "adr/README.md", "decisions/README.md"}
    expected |= {"runbooks/README.md", "manual-checks/README.md", "spec-deviations.md", "CUTOVER.md"}
    assert expected <= targets


def test_shared_links_stay_inside_the_documentation() -> None:
    assert leaving_links(DOCS) == []


def test_shared_documentation_equals_the_other_repository() -> None:
    other = sibling_docs(REPO_ROOT, dict(os.environ))
    if other is None:
        pytest.skip("no checkout of the other repository next to this one (set PLATFORM_SIBLING_CHECKOUT)")
    assert differences(DOCS, other) == []


def test_named_documentation_paths_exist() -> None:
    assert unknown_doc_paths(DOCS, sibling_docs(REPO_ROOT, dict(os.environ))) == []


def test_register_names_no_finished_correction_as_pending() -> None:
    assert stale_open_defects(DOCS, sibling_docs(REPO_ROOT, dict(os.environ))) == []


def test_register_handling_comes_from_the_closed_list() -> None:
    assert unknown_handling((DOCS / "spec-deviations.md").read_text("utf-8")) == []


# The checker itself, on samples built in a temporary tree.


@pytest.mark.parametrize(
    ("heading", "expected"),
    [
        ("5. Repository layout", "5-repository-layout"),
        (
            "ADR-027: Work-package conventions (parallel implementation)",
            "adr-027-work-package-conventions-parallel-implementation",
        ),
        (
            "4.2 Managed settings (`/etc/claude-code/managed-settings.json`, root 0644)",
            "42-managed-settings-etcclaude-codemanaged-settingsjson-root-0644",
        ),
        ("Platform root \N{EN DASH} loss", "platform-root--loss"),
        ("See [the API](03-api.md) *now*", "see-the-api-now"),
        ("Prüfung", "prüfung"),
    ],
)
def test_slug_follows_github(heading: str, expected: str) -> None:
    assert slug(heading) == expected


def test_anchors_number_duplicates_and_skip_code() -> None:
    text = '# A\n## A\n```\n# Not a heading\n```\n<a name="x-y"></a>\n'
    assert anchors(text) == {"a", "a-1", "x-y"}


def test_links_ignore_code_and_read_titles() -> None:
    text = 'See [a](a.md "Title") and ![i](img/x.png), [b [c]](<b.md>) `[no](x.md)`\n```\n[no](y.md)\n```\n'
    assert links(text) == ["a.md", "img/x.png", "b.md"]


def test_check_links_reports_every_kind(tmp_path: Path) -> None:
    docs = tmp_path / "docs" / "platform"
    (docs / "spec").mkdir(parents=True)
    (docs / "spec" / "a.md").write_text("# Title\n## Part two\n", "utf-8")
    (tmp_path / "Makefile").write_text("all:\n", "utf-8")
    (docs / "README.md").write_text(
        "[ok](spec/a.md#part-two) [self](#top) [top](#index) [web](https://example.org/x)\n"
        "[mail](mailto:a@example.org) [repo](../../Makefile) [missing](spec/b.md)\n"
        "[anchor](spec/a.md#nope) [out](../../../x.md) [title](spec/a.md#title)\n"
        "# Index\n",
        "utf-8",
    )
    assert check_links(tmp_path) == [
        "docs/platform/README.md: #top: no heading #top",
        "docs/platform/README.md: spec/b.md: no such file",
        "docs/platform/README.md: spec/a.md#nope: no heading #nope",
        "docs/platform/README.md: ../../../x.md: leaves the repository",
    ]


def test_unindexed_names_missing_entries(tmp_path: Path) -> None:
    assert unindexed(tmp_path, ["a.md"]) == ["a.md"]
    (tmp_path / "README.md").write_text("[a](a.md#x) and `[b](b.md)`\n", "utf-8")
    assert unindexed(tmp_path, ["a.md", "b.md"]) == ["b.md"]


def test_non_markdown_targets_are_not_searched_for_anchors(tmp_path: Path) -> None:
    (tmp_path / "x.json").write_text("{}", "utf-8")
    assert link_problems(tmp_path / "a.md", "x.json#/defs", tmp_path) == []


def test_leaving_links_and_differences(tmp_path: Path) -> None:
    docs = tmp_path / "a" / "docs" / "platform"
    (docs / "spec").mkdir(parents=True)
    (docs / "README.md").write_text("[in](spec/x.md) [out](../../Makefile) [web](https://example.org) [top](#a)\n")
    (docs / "spec" / "x.md").write_text("# A\n", "utf-8")
    (docs / "decisions").mkdir()
    (docs / "decisions" / "y.md").write_text("[out](../../../Makefile)\n", "utf-8")
    assert leaving_links(docs) == ["README.md: ../../Makefile"]
    other = tmp_path / "b" / "docs" / "platform"
    (other / "spec").mkdir(parents=True)
    (other / "README.md").write_text((docs / "README.md").read_text())
    (other / "spec" / "x.md").write_text("# B\n", "utf-8")
    (other / "adr").mkdir()
    (other / "adr" / "z.md").write_text("", "utf-8")
    assert differences(docs, other) == ["adr/z.md", "spec/x.md"]
    assert differences(docs, docs) == []


def test_unknown_doc_paths_by_part(tmp_path: Path) -> None:
    docs, other = tmp_path / "a" / "docs" / "platform", tmp_path / "b" / "docs" / "platform"
    for root in (docs, other):
        (root / "runbooks").mkdir(parents=True)
        (root / "decisions").mkdir()
    (docs / "decisions" / "A-1.md").write_text("", "utf-8")
    (other / "decisions" / "B-1.md").write_text("", "utf-8")
    (docs / "README.md").write_text("", "utf-8")
    (docs / "runbooks" / "x.json").write_text("`docs/platform/runbooks/gone.md`", "utf-8")
    (docs / "runbooks" / "r.md").write_text(
        "`docs/platform/README.md#part` `docs/platform/runbooks/r.md:3` `docs/platform/decisions/<id>.md`\n"
        "`docs/platform/**` `docs/platform/decisions/A-1.md` `docs/platform/decisions/B-1.md`\n"
        "`docs/platform/acceptance/C-1.md` `docs/platform/runbooks/gone.md` `docs/platform/`\n",
        "utf-8",
    )
    assert unknown_doc_paths(docs, other) == [
        "runbooks/r.md: docs/platform/acceptance/C-1.md",
        "runbooks/r.md: docs/platform/runbooks/gone.md",
    ]
    assert unknown_doc_paths(docs, None) == ["runbooks/r.md: docs/platform/runbooks/gone.md"]


def test_stale_open_defects_by_decisions_file(tmp_path: Path) -> None:
    docs, other = tmp_path / "a", tmp_path / "b"
    for root in (docs, other):
        (root / "decisions").mkdir(parents=True)
    (docs / "decisions" / "K-1.md").write_text("", "utf-8")
    (other / "decisions" / "K-2.md").write_text("", "utf-8")
    (docs / "spec-deviations.md").write_text(
        "| x | open defect until K-1 |\n| y | open defect until K-2 |\n| z | open defect until K-3 |\n", "utf-8"
    )
    assert stale_open_defects(docs, other) == ["K-1", "K-2"]
    assert stale_open_defects(docs, None) == ["K-1"]


def test_unknown_handling_reads_the_last_cell() -> None:
    register = (
        "| § | As built | Source | Handling |\n|---|---|---|---|\n| a | x | s | as built |\n"
        "| b | x | s | open defect until K-1 |\n| c | x | s | as built (closed) |\n| d | x | s | |\ntext | e |\n"
    )
    assert unknown_handling(register) == ["c"]


def test_sibling_docs_is_found_by_variable_or_layout(tmp_path: Path) -> None:
    assert sibling_docs(tmp_path / "agent", {"PLATFORM_SIBLING_CHECKOUT": "/x"}) == Path("/x/docs/platform")
    for layout in (tmp_path / "flat", tmp_path / "wt"):
        (layout / "agent").mkdir(parents=True)
    (tmp_path / "flat" / "agent-core" / "docs" / "platform" / "spec").mkdir(parents=True)
    expected = tmp_path / "flat" / "agent-core" / "docs" / "platform"
    assert sibling_docs(tmp_path / "flat" / "agent", {}) == expected
    (tmp_path / "wt" / "agent" / "p-1").mkdir()
    (tmp_path / "wt" / "agent-core" / "p-1" / "docs" / "platform" / "spec").mkdir(parents=True)
    expected = tmp_path / "wt" / "agent-core" / "p-1" / "docs" / "platform"
    assert sibling_docs(tmp_path / "wt" / "agent" / "p-1", {}) == expected
    assert sibling_docs(tmp_path / "wt" / "agent" / "p-2", {}) is None
    assert sibling_docs(tmp_path / "other", {}) is None
