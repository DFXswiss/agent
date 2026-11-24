"""HOT-09: the mark that a session's agent has the platform tools (``toolsready.py``) and the ``api.sock`` op that sets
it (``control_routes/tools.py``): one mark per run of the agent, journaled once, kept across a restart of the control
process, refused for unknown or ended sessions and for malformed reports."""

from __future__ import annotations

from dataclasses import replace
from typing import Any

import pytest

from agent_guest import toolsready
from agent_guest.apisock import ApiClient, ApiSockError
from agent_guest.control import ControlContext
from agent_guest.state import Session, State, StateError
from tests.platform.guest.conftest import SESSION_ID, make_session


def records(context: ControlContext) -> list[tuple[str | None, dict[str, Any]]]:
    found = context.journal.read(0, 100)[0]
    return [(record["session_id"], record["body"]) for record in found if record["kind"] == toolsready.KIND]


def test_the_first_listing_of_a_run_is_marked_and_journaled_once(context: ControlContext, session: Session) -> None:
    assert not toolsready.ready(context, SESSION_ID)
    assert toolsready.mark(context, SESSION_ID, 17) is True
    assert toolsready.ready(context, SESSION_ID)
    assert toolsready.mark(context, SESSION_ID, 17) is False
    [(sid, body)] = records(context)
    assert (sid, body["tools"]) == (SESSION_ID, 17)
    assert set(body) == {"tools", "at"}
    # a new run of the agent has a new token: its tools must be listed again
    context.state.save_session(replace(session, token_sha256="1" * 64))
    assert not toolsready.ready(context, SESSION_ID)
    assert toolsready.mark(context, SESSION_ID, 17) is True
    assert len(records(context)) == 2


def test_the_mark_survives_a_restart_of_the_control_process(context: ControlContext, session: Session) -> None:
    toolsready.mark(context, SESSION_ID, 3)
    restarted = replace(context, state=State(context.journal))  # what a restarted control process reads
    assert toolsready.ready(restarted, SESSION_ID)


def test_forget_drops_the_mark(context: ControlContext, session: Session) -> None:
    toolsready.mark(context, SESSION_ID, 3)
    toolsready.forget(context, SESSION_ID)
    assert not toolsready.ready(context, SESSION_ID)


@pytest.mark.parametrize("state", ["stopped", "failed", None])
def test_no_mark_for_an_unknown_or_ended_session(context: ControlContext, state: str | None) -> None:
    if state is not None:
        context.state.save_session(make_session(state=state))
    with pytest.raises(StateError) as raised:
        toolsready.mark(context, SESSION_ID, 3)
    assert raised.value.code == "not_found"
    assert not toolsready.ready(context, SESSION_ID)
    assert records(context) == []


def test_the_op_marks_the_session(api_client: ApiClient, context: ControlContext, session: Session) -> None:
    assert api_client.call("session.tools_listed", {"session_id": SESSION_ID, "tools": 17}) == {"first": True}
    assert api_client.call("session.tools_listed", {"session_id": SESSION_ID, "tools": 17}) == {"first": False}
    assert toolsready.ready(context, SESSION_ID)


@pytest.mark.parametrize(
    ("args", "code"),
    [
        ({"session_id": SESSION_ID}, "request_invalid"),
        ({"session_id": SESSION_ID, "tools": 1, "extra": 1}, "request_invalid"),
        ({"session_id": 7, "tools": 1}, "request_invalid"),
        ({"session_id": SESSION_ID, "tools": True}, "request_invalid"),
        ({"session_id": SESSION_ID, "tools": -1}, "request_invalid"),
        ({"session_id": SESSION_ID, "tools": 1001}, "request_invalid"),
        ({"session_id": SESSION_ID, "tools": "17"}, "request_invalid"),
        ({"session_id": "01a1ee56-5e00-7003-8f1b-340691d99fff", "tools": 1}, "not_found"),
    ],
)
def test_the_op_refuses_malformed_reports(
    api_client: ApiClient, context: ControlContext, session: Session, args: dict[str, Any], code: str
) -> None:
    with pytest.raises(ApiSockError) as raised:
        api_client.call("session.tools_listed", args)
    assert raised.value.code == code
    assert not toolsready.ready(context, SESSION_ID)
