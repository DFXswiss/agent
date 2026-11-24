"""Parity matrix web client ↔ agent tool (docs/platform/agent-parity.md, 11-agent-access.md section 8.4, ADR-056 §4).

Every screen directory under `web/src/ui/screens/` must appear as `screens/<directory>` and every settings section
under `web/src/ui/settings/sections/` as `settings/<directory>` in the Screen column of the matrix, so a new screen or
section of the web client fails here until the matrix has a row for it. The screen `settings` only hosts the sections
and is covered by them. The commands of the rows are checked by the parity test of the `agent` repository
(`tests/platform/tool/test_parity.py`), which has the tool but not the web source.
"""

from __future__ import annotations

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
MATRIX = REPO_ROOT / "docs" / "platform" / "agent-parity.md"
SCREENS = REPO_ROOT / "web" / "src" / "ui" / "screens"
SECTIONS = REPO_ROOT / "web" / "src" / "ui" / "settings" / "sections"
HOST_SCREEN = "settings"
STATES = {"done", "api", "stage b", "stage c"}
# A bar inside a cell (command alternatives `a\|b`) is escaped; only unescaped bars separate cells.
CELL_BORDER = re.compile(r"(?<!\\)\|")
ESCAPED_BAR = "\\|"


def matrix_rows() -> list[list[str]]:
    """The body rows of the matrix table as cells (action, screen, command, state)."""
    found = []
    for line in MATRIX.read_text(encoding="utf-8").splitlines():
        if not line.startswith("|"):
            continue
        body = line.strip().removeprefix("|").removesuffix("|")
        cells = [cell.strip().replace(ESCAPED_BAR, "|") for cell in CELL_BORDER.split(body)]
        if cells[0] in ("Action", "---"):
            continue
        found.append(cells)
    return found


def named(prefix: str) -> set[str]:
    """The directories named as `<prefix>/<directory>` in the Screen column."""
    pattern = re.compile(rf"\b{prefix}/([a-z0-9-]+)")
    return {name for row in matrix_rows() for name in pattern.findall(row[1])}


def directories(root: Path) -> set[str]:
    return {path.name for path in root.iterdir() if path.is_dir()}


def test_rows_are_well_formed() -> None:
    rows = matrix_rows()
    assert len(rows) > 40
    assert all(len(row) == 4 for row in rows)
    assert {row[3] for row in rows} <= STATES
    assert len({row[0] for row in rows}) == len(rows)


def test_every_screen_has_a_row() -> None:
    screens = directories(SCREENS)
    assert HOST_SCREEN in screens
    assert screens - {HOST_SCREEN} - named("screens") == set()


def test_every_settings_section_has_a_row() -> None:
    sections = directories(SECTIONS)
    assert sections
    assert sections - named("settings") == set()


def test_the_matrix_names_no_missing_directory() -> None:
    assert named("screens") - directories(SCREENS) == set()
    assert named("settings") - directories(SECTIONS) == set()
