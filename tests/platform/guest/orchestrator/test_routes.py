"""U-GST-orchestrator: the control routes, the ``orchestrator.mcp`` op, ``orch.sock`` and the second MCP path of the
main orchestrator (05-runner.md sections 17.2-17.5), through the real sockets of the guest fixtures.

Attack tests: a session that is not the claimed orchestrator gets nothing from ``/mcp-orchestrator`` (``404``) and
cannot reach the tool; the API process cannot create relay calls; a runner answer to anything but a relay is refused.
"""

from __future__ import annotations

import contextlib
import http.client
import json
import os
import socketserver
import stat
import threading
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler
from pathlib import Path
from typing import Any

import pytest

from agent_guest import orchestrator
from agent_guest.api_routes import orchestrator_mcp
from agent_guest.apisock import ApiClient, ApiSockError, AuthLimited
from agent_guest.config import GuestConfig
from agent_guest.control import ControlContext, ControlService
from agent_guest.control_routes import orchestrator as routes
from agent_guest.orchestrator import Orchestrator
from agent_guest.routing import HttpError
from agent_guest.server import ApiHttpServer
from tests.platform.guest.conftest import SESSION_ID, SESSION_TOKEN, ControlClient
from tests.platform.guest.orchestrator.conftest import (
    HANDOVER,
    KEYS,
    KEYS_INPUT,
    OTHER_ID,
    OTHER_TOKEN,
    FakeTool,
    add_session,
    set_state,
)
from tests.platform.guest.orchestrator.test_orchestrator import ANSWER, RELAY, answer_next

KEYS_PATH = f"/v1/sessions/{SESSION_ID}/orchestrator/keys"
HANDOVER_PATH = f"/v1/sessions/{SESSION_ID}/orchestrator/handover"


def claim(control_client: ControlClient, context: ControlContext) -> None:
    set_state(context, "idle")
    assert control_client.request("POST", KEYS_PATH, KEYS_INPUT).status == 200
    assert control_client.request("POST", HANDOVER_PATH, HANDOVER).status == 200


# -- control routes ----------------------------------------------------------------------------------------------


@pytest.mark.usefixtures("orch", "session")
def test_keys_and_handover_over_the_control_socket(control_client: ControlClient, tool: FakeTool) -> None:
    reply = control_client.request("POST", KEYS_PATH, KEYS_INPUT)
    assert (reply.status, reply.json()) == (200, KEYS)
    reply = control_client.request("POST", HANDOVER_PATH, HANDOVER)
    assert (reply.status, reply.json()) == (200, {})
    assert tool.serving


@pytest.mark.usefixtures("orch", "session")
def test_route_refusals(control_client: ControlClient, tool: FakeTool) -> None:
    assert control_client.request("POST", HANDOVER_PATH, HANDOVER).json() == {"error": "state_conflict"}
    assert control_client.request("POST", KEYS_PATH, {**KEYS_INPUT, "x": 1}).status == 400
    reply = control_client.request("POST", f"/v1/sessions/{OTHER_ID}/orchestrator/keys", KEYS_INPUT)
    assert (reply.status, reply.json()) == (404, {"error": "not_found"})
    tool.fail["pair --session"] = "internal"
    assert control_client.request("POST", KEYS_PATH, KEYS_INPUT).status == 503


@pytest.mark.usefixtures("orch", "session")
def test_the_runner_answers_relay_calls_only(
    control_client: ControlClient, context: ControlContext, api_client: ApiClient
) -> None:
    pending_id = api_client.pending_create(SESSION_ID, "query", {"tool": "get_context", "arguments": {}})
    reply = control_client.request("POST", f"/v1/relay/{pending_id}/answer", ANSWER)
    assert (reply.status, reply.json()) == (404, {"error": "not_found"})
    reply = control_client.request("POST", f"/v1/relay/{OTHER_ID}/answer", ANSWER)
    assert reply.status == 404
    claim(control_client, context)
    found: dict[str, Any] = {}
    thread = threading.Thread(target=lambda: found.update(result=routes.relay(context, dict(RELAY))), daemon=True)
    thread.start()
    pending = wait_relay(context)
    assert control_client.request("POST", f"/v1/relay/{pending}/answer", ANSWER).status == 200
    thread.join(5)
    assert found["result"] == ANSWER
    assert control_client.request("POST", f"/v1/relay/{pending}/answer", ANSWER).status == 200  # identical repeat
    reply = control_client.request("POST", f"/v1/relay/{pending}/answer", {"error": "refused"})
    assert (reply.status, reply.json()) == (409, {"error": "state_conflict"})
    assert control_client.request("POST", f"/v1/relay/{pending}/answer", {"error": "other"}).status == 400


def wait_relay(context: ControlContext) -> str:
    for _ in range(500):
        with context.journal.transaction() as db:
            row = db.execute("SELECT id FROM pending WHERE kind = 'relay' AND state = 'waiting'").fetchone()
        if row is not None:
            return str(row[0])
        threading.Event().wait(0.01)
    raise AssertionError("no relay call")


@pytest.mark.usefixtures("orch", "session")
def test_the_mcp_op_of_the_api_process(
    control_client: ControlClient, context: ControlContext, api_client: ApiClient
) -> None:
    with pytest.raises(ApiSockError) as error:
        api_client.call("orchestrator.mcp", {"session_id": SESSION_ID})
    assert error.value.code == "not_found"
    for args in ({}, {"session_id": 1}, {"session_id": SESSION_ID, "x": 1}):
        with pytest.raises(ApiSockError) as error:
            api_client.call("orchestrator.mcp", args)
        assert error.value.code == "request_invalid"
    claim(control_client, context)
    assert api_client.call("orchestrator.mcp", {"session_id": SESSION_ID}) == {"socket": context.config.orch_mcp_socket}


@pytest.mark.usefixtures("session")
def test_the_session_event_ends_the_orchestrator(
    orch: Orchestrator, control_client: ControlClient, context: ControlContext, tool: FakeTool
) -> None:
    claim(control_client, context)
    routes.ended(context, SESSION_ID)  # a suspend: kept
    assert orch.record() is not None
    set_state(context, "stopping")
    routes.ended(context, SESSION_ID)
    assert orch.record() is None
    assert tool.stops == 1


@pytest.mark.usefixtures("session")
def test_the_runner_ends_the_orchestrator_over_the_control_socket(
    orch: Orchestrator, control_client: ControlClient, context: ControlContext, tool: FakeTool
) -> None:
    claim(control_client, context)
    path = f"/v1/sessions/{SESSION_ID}/orchestrator/end"
    assert control_client.request("POST", path, {"pairing_id": OTHER_ID}).json() == {}
    assert orch.record() is not None
    assert control_client.request("POST", path, {}).status == 400
    reply = control_client.request("POST", path, {"pairing_id": HANDOVER["pairing_id"]})
    assert (reply.status, reply.json()) == (200, {})
    assert orch.record() is None
    assert tool.stops == 1


# -- orch.sock ---------------------------------------------------------------------------------------------------


@pytest.mark.usefixtures("orch", "session")
def test_the_tool_relays_over_orch_sock(
    guest_config: GuestConfig, control_client: ControlClient, context: ControlContext
) -> None:
    mode = os.stat(guest_config.orch_socket).st_mode
    assert stat.S_ISSOCK(mode)
    assert stat.S_IMODE(mode) == 0o660  # root and the account's group only
    claim(control_client, context)
    thread = answer_next(context, ANSWER)
    client = ApiClient(guest_config.orch_socket, timeout=10.0)
    try:
        assert client.call("relay", dict(RELAY)) == ANSWER
        with pytest.raises(ApiSockError) as error:
            client.call("orchestrator.mcp", {"session_id": SESSION_ID})  # only the relay op there
        assert error.value.code == "not_found"
    finally:
        client.close()
    thread.join(5)


def test_the_socket_goes_with_the_control_process(guest_config: GuestConfig) -> None:
    service = ControlService(guest_config, startup=[("orchestrator", routes.startup)])
    service.start()
    assert Path(guest_config.orch_socket).exists()
    service.stop()
    assert not Path(guest_config.orch_socket).exists()
    service = ControlService(guest_config, startup=[("orchestrator", routes.startup)])
    service.start()
    os.unlink(guest_config.orch_socket)  # gone already: the close does not fail
    service.stop()


def test_a_started_control_process_ends_a_stale_orchestrator(
    guest_config: GuestConfig, tool: FakeTool, monkeypatch: pytest.MonkeyPatch
) -> None:
    service = ControlService(guest_config, startup=[])
    context = service.start()
    created = orchestrator.install(context, Orchestrator(context, tool, depth=lambda sid: 0))
    created._save(orchestrator.Record(SESSION_ID, "claimed", KEYS, "x"))
    routes.startup(context)
    assert created.record() is None
    assert tool.stops == 1
    service.stop()


# -- /mcp-orchestrator ----------------------------------------------------------------------------------------------


class ToolServer(socketserver.ThreadingUnixStreamServer):
    daemon_threads = True


@pytest.fixture
def tool_server(guest_config: GuestConfig) -> Iterator[dict[str, Any]]:
    """A stand-in of the tool's MCP server on the configured socket: records bodies, answers from ``reply``."""
    seen: dict[str, Any] = {"bodies": [], "reply": (200, b'{"jsonrpc":"2.0","id":1,"result":{}}')}

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, format: str, *args: Any) -> None:
            pass

        def do_POST(self) -> None:
            seen["bodies"].append((self.path, self.rfile.read(int(self.headers["Content-Length"]))))
            status, body = seen["reply"]
            self.send_response(status)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    server = ToolServer(guest_config.orch_mcp_socket, Handler)
    thread = threading.Thread(target=server.serve_forever, args=(0.05,), daemon=True)
    thread.start()
    try:
        yield seen
    finally:
        server.shutdown()
        server.server_close()
        Path(guest_config.orch_mcp_socket).unlink(missing_ok=True)


def post(port: int, body: bytes, token: str | None = SESSION_TOKEN, method: str = "POST") -> tuple[int, bytes, Any]:
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=30)
    headers = {"Content-Type": "application/json"}
    if token is not None:
        headers["Authorization"] = f"Bearer {token}"
    try:
        # a server that refuses before it reads the body (413) may close while the body is still being sent
        with contextlib.suppress(BrokenPipeError, ConnectionResetError):
            connection.request(method, "/mcp-orchestrator", body=body, headers=headers)
        response = connection.getresponse()
        return response.status, response.read(), response.headers
    finally:
        connection.close()


MESSAGE = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "tools/list"}).encode()


@pytest.mark.usefixtures("orch", "session")
def test_only_the_claimed_orchestrator_reaches_the_tool(
    api_port: int, control_client: ControlClient, context: ControlContext, tool_server: dict[str, Any]
) -> None:
    add_session(context)
    assert post(api_port, MESSAGE)[0] == 404  # not yet the orchestrator
    claim(control_client, context)
    status, body, _ = post(api_port, MESSAGE)
    assert (status, body) == (200, b'{"jsonrpc":"2.0","id":1,"result":{}}')
    assert tool_server["bodies"] == [("/mcp", MESSAGE)]
    status, body, _ = post(api_port, MESSAGE, token=OTHER_TOKEN)  # attack: another session of the VM
    assert (status, json.loads(body)) == (404, {"error": "not_found"})
    assert post(api_port, MESSAGE, token=None)[0] == 401
    assert post(api_port, MESSAGE, token="x" * 43)[0] == 401
    assert len(tool_server["bodies"]) == 1
    tool_server["reply"] = (202, b"")
    assert post(api_port, b'{"jsonrpc":"2.0","method":"notifications/initialized"}')[:2] == (202, b"")
    assert post(api_port, b"x" * (orchestrator_mcp.MAX_BODY + 1))[0] == 413
    status, _, headers = post(api_port, b"", method="GET")
    assert (status, headers["Allow"]) == (405, "POST")
    assert post(api_port, b"", method="DELETE")[0] == 405


@pytest.mark.usefixtures("orch", "session")
def test_a_tool_that_fails_is_unavailable(
    api_port: int, control_client: ControlClient, context: ControlContext, tool_server: dict[str, Any]
) -> None:
    claim(control_client, context)
    tool_server["reply"] = (500, b"{}")
    assert post(api_port, MESSAGE)[0] == 503
    tool_server["reply"] = (200, b"x" * (orchestrator_mcp.MAX_ANSWER + 1))
    assert post(api_port, MESSAGE)[0] == 503


@pytest.mark.usefixtures("orch", "session")
def test_a_tool_that_is_not_there_is_unavailable(
    api_port: int, control_client: ControlClient, context: ControlContext
) -> None:
    claim(control_client, context)
    assert post(api_port, MESSAGE)[0] == 503


def test_forward_refuses_a_missing_socket(tmp_path: Path) -> None:
    with pytest.raises(HttpError) as error:
        orchestrator_mcp.forward(str(tmp_path / "none.sock"), b"{}", timeout=1)
    assert error.value.code == "unavailable"


@pytest.mark.usefixtures("orch", "session")
def test_the_api_route_when_the_control_process_fails(
    api_port: int, api_server: ApiHttpServer, monkeypatch: pytest.MonkeyPatch
) -> None:
    control = api_server.context.control

    def down(*args: Any, **kwargs: Any) -> Any:
        raise ApiSockError("unavailable")

    monkeypatch.setattr(control, "call", down)
    assert post(api_port, MESSAGE)[0] == 503  # the token check itself
    original = ApiClient.call
    monkeypatch.setattr(
        control,
        "call",
        lambda op, args=None, timeout=None: (
            down() if op == "orchestrator.mcp" else original(control, op, args, timeout)
        ),
    )
    assert post(api_port, MESSAGE)[0] == 503  # the orchestrator lookup

    def limited(*args: Any, **kwargs: Any) -> Any:
        raise AuthLimited(7)

    monkeypatch.setattr(control, "resolve_session", limited)
    status, _, headers = post(api_port, MESSAGE)
    assert (status, headers["Retry-After"]) == (429, "7")
