"""U-HUB-modes: who switches a mode on through a person route (`agent_hub.modes.choice.person_choice`; ADR-098 section
3) - the passing on of a session's agent, on a recorded connection (the route suite covers the database paths)."""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from typing import Any

import pytest

from agent_hub.auth.keys import Principal
from agent_hub.errors import HubError
from agent_hub.modes import catalog, choice

USER, AGENT, SESSION, ORIGIN, TASK, CHOOSER = (uuid.UUID(int=index, version=7) for index in range(1, 7))
TEST = catalog.find("test", True)


class Rows:
    """A connection whose answers are chosen by the start of the query."""

    def __init__(self, answers: dict[str, Sequence[Any] | None]) -> None:
        self.answers = answers

    def execute(self, query: str, _params: Any = None) -> Rows:
        self.found = next((value for key, value in self.answers.items() if query.startswith(key)), None)
        return self

    def fetchone(self) -> Any:
        return self.found


def _agent() -> Principal:
    return Principal(kind="agent", key_id="k", principal_id=USER, actor_kind="user", attributes={"agent_id": AGENT})


def _rows(*, record: bool = True, author: uuid.UUID = AGENT, session: bool = True) -> Rows:
    return Rows(
        {
            "SELECT session_id FROM hub.agent": (SESSION if session else None,),
            "SELECT task_id FROM hub.session": (ORIGIN,),
            "SELECT chosen_by FROM hub.task_mode": (CHOOSER,) if record else None,
            "SELECT author_id FROM hub.event": (author,),
        }
    )


def test_a_sessions_agent_passes_on_a_mode_of_its_own_task_to_a_task_it_created() -> None:
    assert TEST is not None
    found, actor = choice.person_choice(_rows(), _agent(), TASK, TEST, USER)
    assert (found.way, found.chosen_by, found.origin_task, found.operation_id) == ("inherited", CHOOSER, ORIGIN, None)
    assert (actor.kind, actor.id) == ("user", USER)


@pytest.mark.parametrize(
    "rows",
    [_rows(record=False), _rows(author=USER)],
    ids=["not-on-its-own-task", "not-created-by-it"],
)
def test_anything_else_needs_an_instruction(rows: Rows) -> None:
    assert TEST is not None
    with pytest.raises(HubError) as raised:
        choice.person_choice(rows, _agent(), TASK, TEST, USER)
    assert raised.value.code == "mode_needs_instruction"


def test_a_mode_without_passes_on_is_never_passed_on() -> None:
    assert TEST is not None
    closed = catalog.Mode(TEST.name, TEST.version, TEST.needs, False, TEST.entry)
    with pytest.raises(HubError):
        choice.person_choice(_rows(), _agent(), TASK, closed, USER)


def test_a_persons_agent_and_a_browser() -> None:
    assert TEST is not None
    tool, _actor = choice.person_choice(_rows(session=False), _agent(), TASK, TEST, USER)
    assert (tool.way, tool.chosen_by) == ("tool", USER)
    browser = Principal(kind="session", key_id="k", principal_id=USER, actor_kind="user", attributes={})
    web, _actor = choice.person_choice(_rows(), browser, TASK, TEST, USER)
    assert web.way == "web"
    odd = Principal(kind="agent", key_id="k", principal_id=USER, actor_kind="user", attributes={})
    assert choice.session_of_agent(_rows(), odd) is None
