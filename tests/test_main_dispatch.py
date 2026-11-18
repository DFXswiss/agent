"""The command dispatcher: the usage line, an unknown command and the errors it turns into an exit."""

from __future__ import annotations

from collections.abc import Callable

import pytest

from agent_cli import main as agent_main
from agent_cli.hub import HubError
from agent_cli.model_slots import ModelSlotsError, PinnedModelSlotsError, UnrecordedSlotsError
from agent_cli.store import StoreError

pytestmark = pytest.mark.no_pg


def exit_text(argv: list[str] | None) -> str:
    with pytest.raises(SystemExit) as caught:
        agent_main.main(argv)
    return str(caught.value)


@pytest.mark.parametrize("argv", [[], ["-h"], ["--help"], ["-h", "key"]])
def test_no_command_or_a_help_flag_prints_the_usage_line(argv: list[str]) -> None:
    text = exit_text(argv)
    assert text.startswith("agent: Usage: agent <init|session|")
    assert text.endswith("|conflicts|order|runner> …")


def test_the_usage_line_names_every_command_once() -> None:
    text = exit_text([])
    names = text[text.index("<") + 1 : text.index(">")].split("|")
    assert sorted(names) == sorted(agent_main.COMMANDS)
    assert len(names) == len(set(names))


def test_the_arguments_default_to_the_command_line(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("sys.argv", ["agent", "no-such-command", "x"])
    assert exit_text(None) == "agent: unknown command: no-such-command"


@pytest.mark.parametrize("command", ["no-such-command", "KEY", "", " key"])
def test_an_unknown_command_is_refused_by_name(command: str) -> None:
    assert exit_text([command]) == f"agent: unknown command: {command}"


def test_a_known_command_gets_the_remaining_arguments(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[list[str]] = []
    monkeypatch.setitem(agent_main.COMMANDS, "probe", seen.append)
    agent_main.main(["probe", "one", "--two"])
    assert seen == [["one", "--two"]]


def failing(error: BaseException) -> Callable[[list[str]], None]:
    def command(args: list[str]) -> None:
        raise error

    return command


@pytest.mark.parametrize(
    ("error", "text"),
    [
        (StoreError("the store is not there"), "agent: the store is not there"),
        (HubError("hub request failed: down"), "agent: hub request failed: down"),
        (UnrecordedSlotsError("no recorded slots"), "agent: no recorded slots"),
        (PinnedModelSlotsError("first is empty"), "agent: invalid models pinned on the task: first is empty"),
        (ModelSlotsError("models must name first"), "agent: models must name first"),
    ],
)
def test_an_error_of_a_command_ends_in_one_exit_line(
    monkeypatch: pytest.MonkeyPatch, error: BaseException, text: str
) -> None:
    monkeypatch.setitem(agent_main.COMMANDS, "probe", failing(error))
    assert exit_text(["probe"]) == text


def test_another_error_of_a_command_is_not_swallowed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(agent_main.COMMANDS, "probe", failing(KeyError("unexpected")))
    with pytest.raises(KeyError):
        agent_main.main(["probe"])
