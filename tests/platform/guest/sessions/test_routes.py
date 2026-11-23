"""U-GST-sessions: ``POST /v1/sessions`` and ``POST /v1/sessions/{id}/control`` over the real control socket."""

from __future__ import annotations

import types

import pytest

from agent_guest import registry, sessions
from agent_guest.control import ControlContext
from agent_guest.schema import SchemaSet
from agent_guest.state import StateError
from tests.platform.guest.conftest import ControlClient
from tests.platform.guest.sessions.conftest import SESSION, FakeSystem, request

CONTROL = f"/v1/sessions/{SESSION}/control"


def check_error(schemas: SchemaSet, reply_json: object, code: str) -> None:
    assert schemas.errors("guest/error.response.json", reply_json) == []
    assert isinstance(reply_json, dict)
    assert reply_json["error"] == code


def test_create_answers_sandbox_ready(
    manager: sessions.SessionManager, control_client: ControlClient, schemas: SchemaSet, context: ControlContext
) -> None:
    body = request()
    assert schemas.errors("guest/sessions-create.request.json", body) == []
    reply = control_client.request("POST", "/v1/sessions", body)
    assert (reply.status, reply.json()) == (200, {"state": "sandbox_ready"})
    assert schemas.errors("guest/sessions-create.response.json", reply.json()) == []
    again = control_client.request("POST", "/v1/sessions", body)
    assert (again.status, again.json()) == (200, {"state": "sandbox_ready"})
    session = context.state.session(SESSION)
    assert session is not None
    assert session.state == "sandbox_ready"


@pytest.mark.parametrize(
    ("body", "status", "code"),
    [
        ({**request(), "extra": 1}, 400, "request_invalid"),
        (request(depth=1), 400, "request_invalid"),
        ({**request(), "limits": {"memory_max": 1}}, 400, "request_invalid"),
    ],
)
def test_create_errors(
    *,
    manager: sessions.SessionManager,
    control_client: ControlClient,
    schemas: SchemaSet,
    body: dict[str, object],
    status: int,
    code: str,
) -> None:
    reply = control_client.request("POST", "/v1/sessions", body)
    assert reply.status == status
    check_error(schemas, reply.json(), code)


def test_create_conflicts_and_sandbox_failure(
    manager: sessions.SessionManager, control_client: ControlClient, schemas: SchemaSet, fake_system: FakeSystem
) -> None:
    control_client.request("POST", "/v1/sessions", request())
    conflict = control_client.request("POST", "/v1/sessions", request(depth=0, task_id=SESSION))
    assert conflict.status == 409
    check_error(schemas, conflict.json(), "id_conflict")
    other = request(session_id="01a1ee56-5e02-7003-8f1b-340691d99f03")
    fake_system.fail_add = True
    failed = control_client.request("POST", "/v1/sessions", other)
    assert failed.status == 500
    check_error(schemas, failed.json(), "sandbox_failed")
    repeat = control_client.request("POST", "/v1/sessions", other)
    assert repeat.status == 409
    check_error(schemas, repeat.json(), "state_conflict")


def test_a_large_initial_prompt_fits_the_body_limit(
    manager: sessions.SessionManager, control_client: ControlClient
) -> None:
    body = request(initial_prompt={"prompt_id": "01a1ee56-5e00-7007-8f1b-340691d99f07", "text": "ä" * 262144})
    reply = control_client.request("POST", "/v1/sessions", body)
    assert reply.status == 200


def test_stop_through_the_control_route(
    manager: sessions.SessionManager, control_client: ControlClient, schemas: SchemaSet, fake_system: FakeSystem
) -> None:
    missing = control_client.request("POST", CONTROL, {"action": "stop"})
    assert missing.status == 404
    check_error(schemas, missing.json(), "not_found")
    control_client.request("POST", "/v1/sessions", request())
    manager.start(SESSION)
    for _ in range(2):
        reply = control_client.request("POST", CONTROL, {"action": "stop"})
        assert (reply.status, reply.json()) == (200, {})
        assert schemas.errors("guest/session-control.response.json", reply.json()) == []
    assert fake_system.stopped == [f"agent-session-{SESSION}"] * 2
    bad = control_client.request("POST", CONTROL, {"action": "reboot"})
    assert bad.status == 400
    assert control_client.request("POST", "/v1/sessions/not-a-uuid/control", {"action": "stop"}).status == 404


def test_other_actions_need_a_registered_handler(
    manager: sessions.SessionManager,
    control_client: ControlClient,
    schemas: SchemaSet,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    control_client.request("POST", "/v1/sessions", request())
    # Without the actions of control_routes/control.py (GST-06) nobody handles them.
    discovered = registry.modules
    monkeypatch.setattr(
        registry,
        "modules",
        lambda package: [
            found for found in discovered(package) if found.__name__ != "agent_guest.control_routes.control"
        ],
    )
    unregistered = control_client.request("POST", CONTROL, {"action": "interrupt"})
    assert unregistered.status == 400
    check_error(schemas, unregistered.json(), "request_invalid")
    seen: list[tuple[str, str]] = []

    def interrupt(context: ControlContext, session_id: str) -> None:
        seen.append(("interrupt", session_id))

    def suspend(context: ControlContext, session_id: str) -> None:
        raise StateError("state_conflict")

    module = types.ModuleType("agent_guest.control_routes.control")
    module.ROUTES = []  # type: ignore[attr-defined]
    module.CONTROL_ACTIONS = {"interrupt": interrupt, "suspend": suspend}  # type: ignore[attr-defined]
    original = registry.modules
    monkeypatch.setattr(registry, "modules", lambda package: [*original(package), module])
    reply = control_client.request("POST", CONTROL, {"action": "interrupt"})
    assert (reply.status, reply.json(), seen) == (200, {}, [("interrupt", SESSION)])
    refused = control_client.request("POST", CONTROL, {"action": "suspend"})
    assert refused.status == 409
    check_error(schemas, refused.json(), "state_conflict")
