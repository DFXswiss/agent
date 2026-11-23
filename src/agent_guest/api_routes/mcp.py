"""``/mcp`` on port 7700: the platform MCP server (03-api.md §8); the JSON-RPC logic is in ``mcp.py``.

* ``POST /mcp`` - needs ``Authorization: Bearer <session token>`` of an active session owned by the connecting UID
  (else ``401 unauthorized``, before the body is looked at; after too many failed checks of that UID ``429
  rate_limited`` with ``Retry-After``, ``authguard.py``); a body over ``MAX_BODY`` is ``413 size_exceeded`` after the
  authentication, unread, and journaled for the session (``platform.refused {code:"size_exceeded", tool:null}``,
  KOR-10: 03-api.md section 8 journals every call); answers the JSON-RPC message as ``application/json`` or ``202``
  for notifications
* ``GET /mcp``, ``DELETE /mcp`` - ``405`` with ``Allow: POST``: the server offers no SSE stream and no MCP session
  (Streamable HTTP transport, 2025-06-18)

The handler may run ``TIMEOUT`` seconds, above the longest tool wait of 03-api.md §12 (``get_operation`` ≤ 30 s).
The tool registry is loaded once per API process (``mcp_tools/*:TOOL``).
"""

from __future__ import annotations

import threading
import weakref

from agent_guest.apisock import ApiClient, ApiSockError, AuthLimited
from agent_guest.gateway import authenticate
from agent_guest.mcp import McpServer, load_tools
from agent_guest.routing import HttpError, Request, Response, Route, json_response, rate_limited
from agent_guest.server import ApiContext

MAX_BODY = 1024 * 1024
ROUTE_MAX_BODY = 10**16 - 1
"""The router lets every length through to the handler, which checks ``MAX_BODY`` once the session is known; the
body of a refused request is never read (the router closes the connection)."""
TIMEOUT = 35.0

_SERVERS: weakref.WeakKeyDictionary[ApiClient, McpServer] = weakref.WeakKeyDictionary()
_SERVERS_LOCK = threading.Lock()


def server_for(context: ApiContext) -> McpServer:
    with _SERVERS_LOCK:
        found = _SERVERS.get(context.control)
        if found is None:
            found = _SERVERS[context.control] = McpServer(context.schemas, context.control, load_tools())
        return found


def rpc(context: ApiContext, request: Request) -> Response:
    try:
        session_id = authenticate(context.control, request, api_key=False, table=context.config.peer_table)
    except ApiSockError:
        raise HttpError("unavailable") from None
    except AuthLimited as error:
        return rate_limited(error.retry_after)
    if session_id is None:
        raise HttpError("unauthorized")
    assert request.stream is not None
    server = server_for(context)
    if request.stream.remaining > MAX_BODY:
        server.refused(session_id, "size_exceeded")
        raise HttpError("size_exceeded")
    status, answer = server.handle(session_id, request.stream.read())
    if answer is None:
        return Response(status=status)
    return json_response(answer, status)


def not_allowed(context: ApiContext, request: Request) -> Response:
    response = json_response({"error": "not_found"}, 405)
    response.headers["Allow"] = "POST"
    return response


ROUTES = [
    Route("POST", "/mcp", rpc, body="raw", max_body=ROUTE_MAX_BODY, timeout=TIMEOUT),
    Route("GET", "/mcp", not_allowed, body="none"),
    Route("DELETE", "/mcp", not_allowed, body="none"),
]
