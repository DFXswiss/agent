"""Parity matrix (docs/platform/agent-parity.md, 11-agent-access.md section 8.4): every row in state `done` names
commands that exist in the argparse tree of `agent-platform` (with the options it names), and `api` exists for the
rows that are reachable only through the generic call. That every screen and settings section of the web client has a
row is checked in `agent-core` (`tests/platform/test_agent_parity.py`), which has the web source."""

from __future__ import annotations

import argparse
import re
from pathlib import Path

import pytest

from agent_platform.cli import build_parser

MATRIX = Path(__file__).resolve().parents[3] / "docs" / "platform" / "agent-parity.md"
STATES = {"done", "api", "stage b", "stage c"}
# A bar inside a cell (command alternatives `a\|b`) is escaped; only unescaped bars separate cells.
CELL_BORDER = re.compile(r"(?<!\\)\|")
ESCAPED_BAR = "\\|"


def rows() -> list[dict[str, str]]:
    found = []
    for line in MATRIX.read_text(encoding="utf-8").splitlines():
        if not line.startswith("|"):
            continue
        body = line.strip().removeprefix("|").removesuffix("|")
        cells = [cell.strip().replace(ESCAPED_BAR, "|") for cell in CELL_BORDER.split(body)]
        if cells[0] in ("Action", "---"):
            continue
        assert len(cells) == 4, line
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
    assert len(found) > 40
    assert {row["state"] for row in found} <= STATES
    assert len({row["action"] for row in found}) == len(found)


@pytest.mark.parametrize("row", [row for row in rows() if row["state"] == "done"], ids=lambda row: row["action"])
def test_done_rows_name_existing_commands(row: dict[str, str]) -> None:
    for command in commands(row["command"]):
        assert command_exists(command), command


def test_generic_call_exists() -> None:
    assert command_exists("api GET /v1/me --body -")


def test_the_walker_refuses_unknown_commands() -> None:
    assert not command_exists("nosuch")
    assert not command_exists("org frobnicate")
    assert not command_exists("org list --nosuch")
    assert command_exists("claude service install|remove")
    assert not command_exists("--hub")
