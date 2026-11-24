"""The gateway's turn gate (ADR-064 section 1): a ``/v1/messages`` request that can make the agent act is forwarded
only while the session's turn is confirmed by the ``UserPromptSubmit`` hook; everything else passes as before."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import replace
from pathlib import Path
from typing import Any, cast

import pytest

from agent_guest.api_routes import model as model_routes
from agent_guest.apisock import ApiClient, ApiSockError
from agent_guest.config import GuestConfig
from agent_guest.control import ControlContext
from agent_guest.gateway import Gateway, can_act
from agent_guest.server import ApiContext
from agent_guest.state import Session
from tests.platform.guest.conftest import SESSION_ID
from tests.platform.guest.gateway.conftest import DEEPSEEK, Api, Started, journal_bodies, messages, text_step

MESSAGES = "/model/v1/messages?beta=true"
COUNT = "/model/v1/messages/count_tokens?beta=true"
TOOLS = [{"name": "Read", "description": "read a file", "input_schema": {"type": "object"}}]
Provision = Callable[..., Any]
StartDouble = Callable[..., Started]


def acting(**extra: Any) -> dict[str, Any]:
    return {**messages(DEEPSEEK, stream=False), **extra}


def idle(context: ControlContext, session: Session) -> None:
    context.state.save_session(replace(session, state="idle"))


def errors(context: ControlContext) -> list[dict[str, Any]]:
    return [body for session_id, body in journal_bodies(context, "guest.error") if session_id == SESSION_ID]


@pytest.mark.parametrize(
    "extra",
    [{"tools": TOOLS}, {"tool_choice": {"type": "auto"}}, {"mcp_servers": [{"type": "url", "url": "https://x"}]}],
    ids=["tools", "tool_choice", "mcp_servers"],
)
def test_an_agent_loop_request_outside_a_confirmed_turn_is_refused(
    *,
    start_double: StartDouble,
    provision: Provision,
    api: Api,
    context: ControlContext,
    session: Session,
    extra: dict[str, Any],
) -> None:
    started = start_double("platform", text_step("never"))
    provision("platform", started.url, models={DEEPSEEK: 200_000})
    idle(context, session)
    answer = api.request("POST", MESSAGES, acting(**extra))
    assert answer.status == 403
    assert answer.json()["error"]["type"] == "permission_error"
    assert errors(context) == [{"code": "turn_unconfirmed"}]
    usage = [body for session_id, body in journal_bodies(context, "usage") if session_id == SESSION_ID]
    assert [record["status"] for record in usage] == [403]


def test_the_same_request_in_a_confirmed_turn_is_forwarded(
    start_double: StartDouble, provision: Provision, api: Api, context: ControlContext, session: Session
) -> None:
    started = start_double("platform", text_step("Hello."))
    provision("platform", started.url, models={DEEPSEEK: 200_000})
    answer = api.request("POST", MESSAGES, acting(tools=TOOLS))
    assert answer.status == 200
    assert errors(context) == []


@pytest.mark.parametrize(("path", "body"), [(MESSAGES, messages(DEEPSEEK, stream=False)), (COUNT, acting(tools=TOOLS))])
def test_side_requests_and_token_counts_pass_outside_a_turn(
    *,
    start_double: StartDouble,
    provision: Provision,
    api: Api,
    context: ControlContext,
    session: Session,
    path: str,
    body: dict[str, Any],
) -> None:
    started = start_double("platform", text_step("T"))
    provision("platform", started.url, models={DEEPSEEK: 200_000})
    idle(context, session)
    assert api.request("POST", path, body).status == 200
    assert errors(context) == []


class Control:
    def __init__(self, answer: dict[str, Any] | Exception) -> None:
        self.answer = answer
        self.calls: list[str] = []

    def call(self, op: str, args: dict[str, Any] | None = None, timeout: float | None = None) -> dict[str, Any]:
        self.calls.append(op)
        if isinstance(self.answer, Exception):
            raise self.answer
        return self.answer


@pytest.mark.parametrize(
    ("answer", "confirmed"),
    [({"confirmed": True}, True), ({"confirmed": False}, False), ({}, False), (ApiSockError("unavailable"), False)],
)
def test_the_turn_check_fails_closed(answer: dict[str, Any] | Exception, confirmed: bool) -> None:
    gateway = Gateway(cast(ApiClient, Control(answer)))
    assert gateway._confirmed(SESSION_ID) is confirmed


def test_the_switch_open_skips_the_check() -> None:
    control = Control(ApiSockError("unavailable"))
    assert Gateway(cast(ApiClient, control), fail_open=True)._confirmed(SESSION_ID) is True
    assert control.calls == []


def test_what_can_make_the_agent_act() -> None:
    assert not can_act({"model": "m"})
    assert not can_act({"tools": [], "mcp_servers": [], "tool_choice": None})
    assert can_act({"tools": TOOLS})
    assert can_act({"tool_choice": {"type": "none"}})
    assert can_act({"mcp_servers": [{}]})


class Link:
    """A stand-in control link (weakly referenced by `gateway_for`)."""


def test_the_route_builds_its_gateway_with_the_switch(guest_config: GuestConfig, tmp_path: Path) -> None:
    install = tmp_path / "install.json"
    install.write_text('{"test_mode": true}', encoding="utf-8")
    config = replace(guest_config, fault_hooks="open", golden_install_file=str(install))
    context = ApiContext(config=config, schemas=cast(Any, None), control=cast(ApiClient, Link()))
    assert model_routes._gateway(context).fail_open is True
    other = ApiContext(config=guest_config, schemas=cast(Any, None), control=cast(ApiClient, Link()))
    assert model_routes._gateway(other).fail_open is False
