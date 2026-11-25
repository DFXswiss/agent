"""``/mcp-orchestrator`` on port 7700: the second MCP server of the main orchestrator's session (05-runner.md section
17.5, ADR-082 sections 6.5 and 9.7).

* ``POST /mcp-orchestrator`` - authenticated exactly like ``/mcp`` (the session token, bound to the UID of the
  connecting process; ``401``/``429`` the same); answered only for the session that is the claimed orchestrator (op
  ``orchestrator.mcp`` of the control process), every other session ``404 not_found``. The body (at most
  ``MAX_BODY``, else ``413 size_exceeded``) goes unchanged to the agent tool's MCP server on its unix socket
  (``POST /mcp``, HTTP/1.1); its status and JSON answer come back. A tool that cannot be reached or answers out of
  bounds is ``503 unavailable``.
* ``GET``/``DELETE /mcp-orchestrator`` - ``405`` with ``Allow: POST`` (no SSE stream, no MCP session)

The API process reaches the tool's socket through ``/run/agent-orch`` (``agent-orch:platform 0710``); the session users
cannot. The tool runs under its own account and holds the agent key; Claude Code never starts it.
"""

from __future__ import annotations

import http.client
import socket

from agent_guest.apisock import ApiSockError, AuthLimited
from agent_guest.gateway import authenticate
from agent_guest.routing import HttpError, Request, Response, Route, json_response, rate_limited
from agent_guest.server import ApiContext

PATH = "/mcp-orchestrator"
MAX_BODY = 1024 * 1024
MAX_ANSWER = 8 * 1024 * 1024
ROUTE_MAX_BODY = 10**16 - 1
"""As for ``/mcp``: the handler checks ``MAX_BODY`` once the session is known; a refused body is never read."""
TIMEOUT = 120.0
"""A tool command runs signed hub requests through the relay; above the relay's wait of 40 s per request."""
TOOL_TIMEOUT = 110.0


class _UnixConnection(http.client.HTTPConnection):
    def __init__(self, path: str, timeout: float) -> None:
        super().__init__("localhost", timeout=timeout)
        self.unix_path = path

    def connect(self) -> None:
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.settimeout(self.timeout)
        self.sock.connect(self.unix_path)


def forward(path: str, body: bytes, timeout: float = TOOL_TIMEOUT) -> tuple[int, bytes]:
    """``POST /mcp`` to the tool's socket: ``(status, body)``; ``HttpError("unavailable")`` when it fails."""
    connection = _UnixConnection(path, timeout)
    try:
        connection.request("POST", "/mcp", body=body, headers={"Content-Type": "application/json"})
        response = connection.getresponse()
        answer = response.read(MAX_ANSWER + 1)
    except (OSError, http.client.HTTPException):
        raise HttpError("unavailable") from None
    finally:
        connection.close()
    if len(answer) > MAX_ANSWER or not 200 <= response.status < 500:
        raise HttpError("unavailable")
    return response.status, answer


def rpc(context: ApiContext, request: Request) -> Response:
    try:
        session_id = authenticate(context.control, request, api_key=False, table=context.config.peer_table)
    except ApiSockError:
        raise HttpError("unavailable") from None
    except AuthLimited as error:
        return rate_limited(error.retry_after)
    if session_id is None:
        raise HttpError("unauthorized")
    try:
        found = context.control.call("orchestrator.mcp", {"session_id": session_id})
    except ApiSockError as error:
        raise HttpError("not_found" if error.code == "not_found" else "unavailable") from None
    assert request.stream is not None
    if request.stream.remaining > MAX_BODY:
        raise HttpError("size_exceeded")
    status, answer = forward(str(found["socket"]), request.stream.read())
    if status == 202 or not answer:
        return Response(status=status)
    return Response(status=status, body=answer)


def not_allowed(context: ApiContext, request: Request) -> Response:
    response = json_response({"error": "not_found"}, 405)
    response.headers["Allow"] = "POST"
    return response


ROUTES = [
    Route("POST", PATH, rpc, body="raw", max_body=ROUTE_MAX_BODY, timeout=TIMEOUT),
    Route("GET", PATH, not_allowed, body="none"),
    Route("DELETE", PATH, not_allowed, body="none"),
]
