"""The ticket fields of the agent tool without a hub (E-41, ADR-100, KOR-75): the checks of the sealed and plaintext
fields, the newest field of each kind from opened events, the first prompt of a captured task and the filters."""

from __future__ import annotations

from typing import Any

import pytest

from agent_platform import tickets
from agent_platform.errors import PlatformError


def _reason(error: pytest.ExceptionInfo[PlatformError]) -> object:
    return error.value.fields.get("reason")


def test_checked_meta() -> None:
    assert tickets.checked_meta({"title": "  A title  "}) == {"title": "A title"}
    assert tickets.checked_meta({"github_repo": "Org/repo"}) == {"github_repo": "Org/repo", "github_org": "Org"}
    assert tickets.checked_meta({"github_org": "org", "github_repo": "ORG/repo"}) == {
        "github_org": "org",
        "github_repo": "ORG/repo",
    }
    assert tickets.checked_meta({"github_org": "", "github_repo": "", "description": ""}) == {
        "github_org": "",
        "github_repo": "",
        "description": "",
    }
    for fields, reason in (
        ({"title": "two\nlines"}, "title_invalid"),
        ({"title": "x" * 121}, "title_invalid"),
        ({"description": "x" * 4097}, "description_invalid"),
        ({"github_org": "-bad"}, "github_org_invalid"),
        ({"github_repo": "no-slash"}, "github_repo_invalid"),
        ({"github_org": "a", "github_repo": "b/c"}, "github_repo_invalid"),
    ):
        with pytest.raises(PlatformError) as error:
            tickets.checked_meta(fields)
        assert _reason(error) == reason, fields


def test_checked_code_and_status() -> None:
    assert tickets.checked_code("high", tickets.URGENCIES, "urgency_invalid") == "high"
    with pytest.raises(PlatformError) as error:
        tickets.checked_code("critical", tickets.URGENCIES, "urgency_invalid")
    assert _reason(error) == "urgency_invalid"
    assert tickets.status_of({"status": "todo", "state": "idle"}) == "todo"
    # a hub before ADR-100 sends no status: derived from the state
    assert tickets.status_of({"state": "closed"}) == "completed"
    assert tickets.status_of({"state": "working", "status": "done"}) == "inProgress"


def test_row_fields() -> None:
    row = {"status": "todo", "urgency": "high", "assignee_id": "u", "topic_id": "t"}
    assert tickets.row_fields(row, {"t": "Payments"}) == {**row, "topic": "Payments"}
    older: dict[str, Any] = {"state": "idle"}
    assert tickets.row_fields(older, {}) == {
        "status": "inProgress",
        "urgency": "normal",
        "assignee_id": None,
        "topic_id": None,
        "topic": None,
    }


def test_newest_topics_and_meta_take_the_last_of_each_field() -> None:
    topics = [
        {"ref": "a", "body": {"name": "First"}},
        None,  # did not verify
        {"ref": "a", "locked": True},
        {"ref": "b", "body": {"name": 7}},
        {"ref": "a", "body": {"name": "Renamed"}},
    ]
    assert tickets.newest_topics(topics) == {"a": "Renamed"}
    metas = [
        {"task": "t", "body": {"title": "One", "description": "Text", "github_repo": "o/r"}},
        None,
        {"task": "t", "locked": True},
        {"task": "t", "body": {"title": "Two"}},  # a device's task.set_title: the rest stays
        {"task": "u", "body": {"other": "x"}},
        {"task": "t", "body": {"github_repo": ""}},
    ]
    assert tickets.newest_meta(metas) == {"t": {"title": "Two", "description": "Text", "github_repo": ""}}


def test_captured_and_merged_meta() -> None:
    assert tickets.captured_meta(tickets.Ticket(title="T", description="", github_repo="o/r")) == {
        "title": "T",
        "github_repo": "o/r",
        "github_org": "o",
    }
    for ticket in (tickets.Ticket(), tickets.Ticket(title="T", status="todo")):
        with pytest.raises(PlatformError):
            tickets.captured_meta(ticket)
    assert tickets.merged_meta({"title": "T", "description": "D"}, {"description": ""}) == {
        "title": "T",
        "description": "",
    }
    with pytest.raises(PlatformError) as error:
        tickets.merged_meta({}, {"description": "D"})
    assert _reason(error) == "title_missing"


def test_plain_changes() -> None:
    assert tickets.plain_changes(tickets.Ticket()) == {}
    assert tickets.plain_changes(tickets.Ticket(status="completed", urgency="low", assignee="none", topic="none")) == {
        "status": "completed",
        "urgency": "low",
        "assignee_id": None,
        "topic_id": None,
    }
    assert tickets.plain_changes(tickets.Ticket(assignee="me", topic="x")) == {}
    with pytest.raises(PlatformError):
        tickets.plain_changes(tickets.Ticket(status="done"))


def test_answered_task() -> None:
    assert tickets.answered_task({"task": {"task_id": "t"}}, "t") == {"task_id": "t"}
    answers: list[dict[str, Any]] = [{}, {"task": []}, {"task": {"task_id": "other"}}]
    for answer in answers:
        with pytest.raises(PlatformError) as error:
            tickets.answered_task(answer, "t")
        assert (error.value.code, _reason(error)) == ("hub_refused", "response_invalid")


def test_the_first_prompt() -> None:
    assert tickets.start_text({"title": "T", "description": "D"}) == "T\n\nD"
    assert tickets.start_text({"title": "T"}) == "T"
    assert tickets.start_prompt({"title": "T"}, None) == "T"
    assert tickets.start_prompt({}, "Do it") == "Do it"
    for meta, text in (({}, None), ({"title": "T"}, "  ")):
        with pytest.raises(PlatformError) as error:
            tickets.start_prompt(meta, text)
        assert _reason(error) == "prompt_missing"


def test_a_topic_is_named_once() -> None:
    with pytest.raises(PlatformError) as error:
        tickets._topic_choice(None, "org", "a-topic", "New")  # type: ignore[arg-type]
    assert _reason(error) == "topic_twice"
    assert tickets._topic_choice(None, "org", None, None) is None  # type: ignore[arg-type]


def test_filters() -> None:
    none = tickets.Filters()
    assert not none.sealed()
    assert none.matches({})
    assert tickets.list_query(None, "org", None) == ""  # type: ignore[arg-type]
    assert tickets.list_query(None, "org", none) == ""  # type: ignore[arg-type]
    given = tickets.Filters(status="todo", urgency="high", assignee="none", topic="none")
    assert given.query(None, "org") == {  # type: ignore[arg-type]
        "status": "todo",
        "urgency": "high",
        "assignee": "none",
        "topic": "none",
    }
    assert tickets.list_query(None, "org", given) == "&status=todo&urgency=high&assignee=none&topic=none"  # type: ignore[arg-type]
    for wrong in (tickets.Filters(status="done"), tickets.Filters(urgency="x"), tickets.Filters(topic="x")):
        with pytest.raises(PlatformError):
            wrong.query(None, "org")  # type: ignore[arg-type]
    sealed = tickets.Filters(github_org="Org", github_repo="org/Repo")
    assert sealed.sealed()
    assert sealed.matches({"github_org": "org", "github_repo": "Org/repo"})
    assert not sealed.matches({"github_org": "other", "github_repo": "org/repo"})
    assert not sealed.matches({"github_org": "org", "github_repo": "org/other"})
    assert tickets.Filters(github_repo="o/r").matches({"github_repo": "O/R"})
