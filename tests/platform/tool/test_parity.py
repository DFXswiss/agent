"""Parity matrix (docs/platform/agent-parity.md, 11-agent-access.md section 8.4): every row of the Actions table is
`done`, `pending` or `n/a`; every `done` row names commands that exist in the argparse tree of `agent-platform` (with
the options it names); a `pending` row names the package that delivers it; an `n/a` row gives its reason. Every route
of the Routes table names action rows that exist. That every screen, settings section and hub route of the web client
has a row is checked in `agent-core` (`tests/platform/test_agent_parity.py`), which has the web source."""

from __future__ import annotations

import argparse
import re
from pathlib import Path

import pytest

from agent_platform.cli import build_parser

MATRIX = Path(__file__).resolve().parents[3] / "docs" / "platform" / "agent-parity.md"
STATES = {"done", "pending", "n/a"}
PENDING = re.compile(r"package KOR-\d+ delivers\b")
ROUTE = re.compile(r"`(GET|POST|PUT|PATCH|DELETE) (/v1/[a-z0-9/{}_-]*)`")
# A bar inside a cell (command alternatives `a\|b`) is escaped; only unescaped bars separate cells.
CELL_BORDER = re.compile(r"(?<!\\)\|")
ESCAPED_BAR = "\\|"


def table(heading: str) -> list[list[str]]:
    """The body rows of the table under `## <heading>` as cells."""
    found: list[list[str]] = []
    inside = False
    for line in MATRIX.read_text(encoding="utf-8").splitlines():
        if line.startswith("## "):
            inside = line == f"## {heading}"
            continue
        if not inside or not line.startswith("|"):
            continue
        body = line.strip().removeprefix("|").removesuffix("|")
        cells = [cell.strip().replace(ESCAPED_BAR, "|") for cell in CELL_BORDER.split(body)]
        if cells[0] in ("Action", "Route") or set(cells[0]) == {"-"}:
            continue
        found.append(cells)
    return found


def rows() -> list[dict[str, str]]:
    found = []
    for cells in table("Actions"):
        assert len(cells) == 4, cells
        found.append({"action": cells[0], "screen": cells[1], "command": cells[2], "state": cells[3]})
    return found


def _subcommands(parser: argparse.ArgumentParser) -> dict[str, argparse.ArgumentParser]:
    for action in parser._actions:
        if isinstance(action, argparse._SubParsersAction):
            return dict(action.choices)
    return {}


def _options(parser: argparse.ArgumentParser) -> set[str]:
    return {option for action in parser._actions for option in action.option_strings}


def command_exists(text: str) -> bool:
    """The words of `text` walk down the parser tree (`a|b` alternatives each); options must exist at the end."""
    node: argparse.ArgumentParser = build_parser()
    words = text.split()
    walked = 0
    for word in words:
        if word.startswith("-"):
            break
        choices = _subcommands(node)
        alternatives = word.split("|")
        if not choices:
            break
        if not all(choice in choices for choice in alternatives):
            return False
        node = choices[alternatives[0]]
        walked += 1
    if walked == 0:
        return False
    options = {word.split("=")[0] for word in words[walked:] if word.startswith("--")}
    return options <= _options(node)


def commands(cell: str) -> list[str]:
    return re.findall(r"`([^`]+)`", cell)


def test_matrix_has_rows_and_known_states() -> None:
    found = rows()
    assert len(found) > 60
    assert {row["state"] for row in found} <= STATES
    assert len({row["action"] for row in found}) == len(found)


@pytest.mark.parametrize("row", [row for row in rows() if row["state"] == "done"], ids=lambda row: row["action"])
def test_done_rows_name_existing_commands(row: dict[str, str]) -> None:
    assert commands(row["command"]), row
    for command in commands(row["command"]):
        assert command_exists(command), command


def test_pending_rows_name_a_package_and_na_rows_a_reason() -> None:
    for row in rows():
        if row["state"] == "pending":
            assert PENDING.search(row["command"]), row
        if row["state"] == "n/a":
            assert re.fullmatch(r"\(.{20,}\)", row["command"]), row


def test_routes_name_existing_actions() -> None:
    actions = {row["action"] for row in rows()}
    routes = table("Routes")
    assert len(routes) > 50
    seen = set()
    for cells in routes:
        assert len(cells) == 2, cells
        match = ROUTE.fullmatch(cells[0])
        assert match is not None, cells
        assert match.groups() not in seen, cells
        seen.add(match.groups())
        named = [name.strip() for name in cells[1].split(",")]
        assert named, cells
        assert set(named) <= actions, cells


def test_generic_call_exists() -> None:
    assert command_exists("api GET /v1/me --body -")


def test_the_walker_refuses_unknown_commands() -> None:
    assert not command_exists("nosuch")
    assert not command_exists("org frobnicate")
    assert not command_exists("org list --nosuch")
    assert command_exists("claude service install|remove")
    assert not command_exists("--hub")


GUIDE = Path(__file__).resolve().parents[3] / "docs" / "platform" / "agents.md"
GUIDE_COMMAND = re.compile(r"^(?:printf [^|]*\| )?agent-platform ([a-z][^#<\n]*)", re.MULTILINE)


def guide_commands() -> list[str]:
    """The commands of the guide's examples (`agent-platform …` at the start of a line, up to a comment or `<`)."""
    found = []
    for match in GUIDE_COMMAND.finditer(GUIDE.read_text(encoding="utf-8")):
        words = [word for word in match.group(1).split() if not word.startswith(('"', "'", "$", "["))]
        found.append(" ".join(words))
    return found


def test_every_example_of_the_guide_names_an_existing_command() -> None:
    found = guide_commands()
    assert len(found) > 60
    assert [command for command in found if not command_exists(command)] == []
