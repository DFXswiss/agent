"""The session platform API: MCP server ``platform`` on ``/mcp`` (03-api.md §8, contracts ``mcp/``).

JSON-RPC 2.0 over HTTP POST with JSON responses (no SSE, no MCP session IDs):

* ``initialize`` - echoes ``protocolVersion`` (``DEFAULT_PROTOCOL`` when absent), capabilities
  ``{tools:{listChanged:false}}``, ``serverInfo {name:"agent-dfx-swiss-platform", version}``
* notifications (no ``id``, e.g. ``notifications/initialized``) - HTTP 202 without a body
* ``tools/list`` - the registered tools with the dereferenced ``mcp/tools/<tool>.input.json`` as ``inputSchema``
* ``tools/call`` - arguments checked against the tool's input schema with the guest validator (integers never accept
  booleans, every argument object is closed); a mismatch is the tool error ``params_invalid``; an unknown tool is the
  JSON-RPC error ``-32602``
* ``ping`` - ``{}``; the lists of what the server does not offer answer empty (HOT-09: every method a client may ask
  while it connects gets an answer): ``resources/list`` ``{resources:[]}``, ``resources/templates/list``
  ``{resourceTemplates:[]}``, ``prompts/list`` ``{prompts:[]}``, ``logging/setLevel`` ``{}``; every other method
  ``-32601`` - among them ``server/discover``, the probe of a client that speaks a newer protocol generation first:
  ``-32601`` is the answer the protocol foresees for a server of the 2025 generation, the client then connects with
  ``initialize``; a body that is not JSON ``-32700``, not a JSON-RPC request ``-32600`` (HTTP 400 for both, 200 for
  the others)

Tools at the agent (HOT-09): a successful ``tools/list`` is the proof that the session's agent has the platform tools
(Claude Code connects its MCP servers in the background, ADR-029 addendum); the server reports it to the control
process (op ``session.tools_listed {session_id, tools}``, best effort), which holds the first prompt until then
(``delivery.py``).

Tool results are ``{content:[{type:"text",text:<JSON>}], isError}``; errors carry ``{"error":<code>,"message":...}``
with the codes of ``mcp/objects.json#/$defs/tool_error``.

Tools (ADR-027 §4): ``mcp_tools/<tool>.py:TOOL`` is a ``Tool`` whose name equals the module name; schemas are
``mcp/tools/<name>.input.json`` and ``.output.json``. A read tool uses ``query_tool``: the call becomes a pending
call of kind ``query`` (journal ``platform.query`` with the tool and its arguments, written by the control process),
and the tool waits up to ``QUERY_WAIT_SECONDS`` for the runner's answer (RUN-08). No answer in time is the tool error
``timeout``; an unreachable control process ``unavailable``; an answer that does not match the tool's output schema
``unavailable``; an error answer of the runner is passed on unchanged.

Journal (03-api.md §8 "every call is journaled"; the handshake ``initialize``/``tools/list``/``ping`` and
notifications carry no platform data and are not journaled when they succeed): a forwarded call by its pending
record (``platform.query``/``operation.request``; the runner writes the read audit ``read.session.<tool>`` when it
answers), ``get_notifications`` by ``platform.read`` (written by the control process with the take). A request
refused before it is forwarded or answered is journaled as ``platform.refused {code, tool}``: ``parse_error``,
``invalid_request``, ``method_not_found``, ``unknown_tool`` or ``params_invalid`` (of ``initialize``/``tools/list``
with ``tool`` null, of a tool call with the tool's name); ``tool`` names one of the registered tools or is null -
never a foreign name or the arguments, so nothing the session sends reaches the runner as content. A
``method_not_found`` also carries ``method``: the method's name when it is one of the protocol's (``METHODS``), else
``other`` (HOT-09: the log says which method was refused).
The runner audits a refused read tool as ``read.session.<tool>`` with result ``rejected`` (KOR-07). Journaling is
best effort: an unreachable control process does not change the answer.

Read limit (KOR-10, AUD-02 NB-07): every read is journaled and becomes an audit row and an event at the hub, so a
session calling in a loop would grow both without bound. ``ReadLimiter`` gives each session a token bucket for the
read tools (every tool but ``request_operation``, which the operation budget limits): ``READ_BURST`` calls at once,
``READ_RATE`` per second after that. A read over the limit is the tool error ``rate_limited``; only the first refused
read of a stretch is journaled (``platform.refused {code:"rate_limited", tool}``). Every journaled refusal (protocol
errors, ``params_invalid``, an oversized body, ``rate_limited``) passes a second bucket of the same size per session:
the first refusal of a stretch over it is journaled, the rest is not, so the records of one session stay bounded by
the two rates whatever it sends. A tool's own refusal that names a code (``ToolRefused``, e.g. ``get_subagent_result``
asked for the session itself: ``not_found``) is journaled as well (KOR-10, NB-08).
"""

from __future__ import annotations

import contextlib
import json
import threading
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any

from agent_guest import __version__, registry
from agent_guest.apisock import ApiClient, ApiSockError
from agent_guest.routing import parse_json
from agent_guest.schema import SchemaError, SchemaSet

DEFAULT_PROTOCOL = "2025-06-18"
SERVER_NAME = "agent-dfx-swiss-platform"
QUERY_WAIT_SECONDS = 20.0
MAX_DEREF_DEPTH = 32
PARSE_ERROR, INVALID_REQUEST, METHOD_NOT_FOUND, INVALID_PARAMS = -32700, -32600, -32601, -32602
REFUSED = "platform.refused"
WRITE_TOOLS = frozenset({"request_operation"})
READ_BURST = 120
READ_RATE = 2.0
RATE_LIMITED = "rate_limited"
MAX_BUCKETS = 1024
TOOLS_LISTED = "session.tools_listed"
EMPTY_LISTS: dict[str, dict[str, Any]] = {
    "resources/list": {"resources": []},
    "resources/templates/list": {"resourceTemplates": []},
    "prompts/list": {"prompts": []},
    "logging/setLevel": {},
}
"""Methods of what the server does not offer, answered empty (module docstring)."""
METHODS = frozenset(
    {
        "server/discover",
        "resources/read",
        "resources/subscribe",
        "resources/unsubscribe",
        "prompts/get",
        "completion/complete",
        "sampling/createMessage",
        "elicitation/create",
        "roots/list",
        "tasks/get",
        "tasks/list",
        "tasks/result",
        "tasks/cancel",
    }
)
"""Request methods of the protocol that the server refuses ``-32601`` and names in the journal (others: ``other``)."""
_SKIPPED_KEYS = frozenset({"$ref", "$schema", "$id", "$defs", "$comment", "title"})


class ToolError(Exception):
    """A tool result with ``isError: true``; ``code`` from ``mcp/objects.json#/$defs/tool_error``."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(code)
        self.code = code
        self.message = message


class ToolRefused(ToolError):
    """A tool's own check refused the call before anything was forwarded (by default ``params_invalid``), journaled
    as ``platform.refused`` like a call refused by the input schema."""

    def __init__(self, code: str = "params_invalid", message: str = "The arguments do not match the tool.") -> None:
        super().__init__(code, message)


class ReadLimiter:
    """A token bucket per session for the read tools (module docstring); thread-safe."""

    def __init__(
        self, *, burst: int = READ_BURST, rate: float = READ_RATE, clock: Callable[[], float] = time.monotonic
    ) -> None:
        self.burst = burst
        self.rate = rate
        self.clock = clock
        self._lock = threading.Lock()
        # session -> (tokens, time of the last update, a read of the current stretch was refused already)
        self._buckets: dict[str, tuple[float, float, bool]] = {}

    def take(self, session_id: str) -> str:
        """``ok`` (one token taken), ``refused`` (the first refusal of a stretch) or ``refused_again``."""
        with self._lock:
            now = self.clock()
            tokens, at, refused = self._buckets.get(session_id, (float(self.burst), now, False))
            tokens = min(float(self.burst), tokens + (now - at) * self.rate)
            if tokens >= 1.0:
                self._buckets[session_id] = (tokens - 1.0, now, False)
                result = "ok"
            else:
                self._buckets[session_id] = (tokens, now, True)
                result = "refused_again" if refused else "refused"
            if len(self._buckets) > MAX_BUCKETS:
                self._forget_full(now)
            return result

    def _forget_full(self, now: float) -> None:
        """Drop the buckets that refilled completely (they equal a fresh one)."""
        for session_id, (tokens, at, _) in list(self._buckets.items()):
            if tokens + (now - at) * self.rate >= self.burst:
                del self._buckets[session_id]


@dataclass(frozen=True)
class ToolCall:
    """What a tool handler gets: the session, the checked arguments and the way to the control process."""

    tool: str
    session_id: str
    arguments: dict[str, Any]
    control: ApiClient
    schemas: SchemaSet


@dataclass(frozen=True)
class Tool:
    name: str
    description: str
    handler: Callable[[ToolCall], Any]

    @property
    def input_schema(self) -> str:
        return f"mcp/tools/{self.name}.input.json"

    @property
    def output_schema(self) -> str:
        return f"mcp/tools/{self.name}.output.json"


def forward_query(call: ToolCall, wait: float | None = None) -> Any:
    """Hand a read call to the runner as ``platform.query`` and wait for the answer (generic forwarding)."""
    limit = QUERY_WAIT_SECONDS if wait is None else wait
    try:
        pending_id = call.control.pending_create(
            call.session_id, "query", {"tool": call.tool, "arguments": call.arguments}
        )
        answer = call.control.pending_wait(pending_id, call.session_id, limit)
    except ApiSockError:
        raise ToolError("unavailable", "The platform is not reachable right now. Try again later.") from None
    if answer is None:
        raise ToolError("timeout", "The platform did not answer in time. Try again later.")
    if answer["status"] == "error":
        raise ToolError(str(answer["error"]["error"]), str(answer["error"]["message"]))
    result = answer["result"]
    if call.schemas.errors(f"mcp/tools/{call.tool}.output.json", result):
        raise ToolError("unavailable", "The platform answered with an invalid result.")
    return result


def query_tool(name: str, description: str) -> Tool:
    """A read tool answered by the runner through ``platform.query``."""
    return Tool(name=name, description=description, handler=forward_query)


def load_tools(package: str = registry.MCP_TOOLS) -> dict[str, Tool]:
    """The discovered tools; a module whose ``TOOL`` is no ``Tool`` or has another name refuses the start."""
    tools: dict[str, Tool] = {}
    for module, tool in registry.mcp_tools(package).items():
        if not isinstance(tool, Tool) or tool.name != module:
            raise registry.RegistryError("registry_invalid", f"{package}.{module}.TOOL")
        tools[module] = tool
    return tools


def dereference(schemas: SchemaSet, rel: str) -> dict[str, Any]:
    """The schema ``rel`` with every ``$ref`` inlined, without ``$schema $id $defs $comment title``."""
    return _inline(schemas, rel, schemas.load(rel), 0)


def _inline(schemas: SchemaSet, base: str, node: Mapping[str, Any], depth: int) -> dict[str, Any]:
    if depth > MAX_DEREF_DEPTH:
        raise SchemaError("schema_ref_invalid", base)
    out: dict[str, Any] = {}
    if "$ref" in node:
        target, schema = schemas.resolve(base, str(node["$ref"]))
        out.update(_inline(schemas, target, schema, depth + 1))
    for key, value in node.items():
        if key in _SKIPPED_KEYS:
            continue
        if key == "properties":
            out[key] = {name: _inline(schemas, base, item, depth + 1) for name, item in value.items()}
        elif key in ("items", "additionalProperties") and isinstance(value, dict):
            out[key] = _inline(schemas, base, value, depth + 1)
        elif key in ("oneOf", "anyOf"):
            out[key] = [_inline(schemas, base, item, depth + 1) for item in value]
        else:
            out[key] = value
    return out


def _text(value: object, *, error: bool) -> dict[str, Any]:
    text = json.dumps(value, separators=(",", ":"), ensure_ascii=False, allow_nan=False)
    return {"content": [{"type": "text", "text": text}], "isError": error}


def _error(ident: object, code: int, message: str) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": ident, "error": {"code": code, "message": message}}


class _RpcError(Exception):
    def __init__(self, code: int, message: str, refusal: str, method: str | None = None) -> None:
        super().__init__(code)
        self.code = code
        self.message = message
        self.refusal = refusal
        self.method = method


class McpServer:
    """Answers JSON-RPC messages of one authenticated session."""

    def __init__(
        self,
        schemas: SchemaSet,
        control: ApiClient,
        tools: Mapping[str, Tool],
        limiter: ReadLimiter | None = None,
        refusals: ReadLimiter | None = None,
    ) -> None:
        self.schemas = schemas
        self.control = control
        self.tools = dict(tools)
        self.limiter = limiter or ReadLimiter()
        self.refusals = refusals or ReadLimiter()
        self._listing: list[dict[str, Any]] | None = None

    def handle(self, session_id: str, body: bytes) -> tuple[int, dict[str, Any] | None]:
        """HTTP status and JSON-RPC response (``None``: notification, answered 202 without a body)."""
        try:
            message = parse_json(body)
        except ValueError:
            self.refused(session_id, "parse_error")
            return 400, _error(None, PARSE_ERROR, "Parse error.")
        if not isinstance(message, dict) or self.schemas.errors("mcp/jsonrpc.request.json", message):
            ident = message.get("id") if isinstance(message, dict) else None
            valid_id = ident is not None and self.schemas.is_valid("mcp/objects.json#/$defs/rpc_id", ident)
            self.refused(session_id, "invalid_request")
            return 400, _error(ident if valid_id else None, INVALID_REQUEST, "Invalid request.")
        if "id" not in message:
            return 202, None
        ident = message["id"]
        try:
            result = self._dispatch(session_id, message["method"], message.get("params", {}))
        except _RpcError as error:
            self.refused(session_id, error.refusal, method=error.method)
            return 200, _error(ident, error.code, error.message)
        return 200, {"jsonrpc": "2.0", "id": ident, "result": result}

    def refused(self, session_id: str, code: str, tool: str | None = None, *, method: str | None = None) -> None:
        """Journal a refused request unless the session's refusals are over their limit (module docstring); a control
        process that cannot be reached is ignored."""
        if self.refusals.take(session_id) == "refused_again":
            return
        body: dict[str, Any] = {"code": code, "tool": tool}
        if method is not None:
            body["method"] = method
        with contextlib.suppress(ApiSockError):
            self.control.append(REFUSED, body, session_id)

    def tools_listed(self, session_id: str) -> None:
        """Tell the control process that the session's agent has the tools (best effort: without the report the
        first prompt waits for the next listing or the control process's deadline)."""
        with contextlib.suppress(ApiSockError):
            self.control.call(TOOLS_LISTED, {"session_id": session_id, "tools": len(self.tools)})

    def _dispatch(self, session_id: str, method: str, params: dict[str, Any]) -> dict[str, Any]:
        if method == "initialize":
            self._check_params("mcp/initialize.params.json", params)
            return {
                "protocolVersion": params.get("protocolVersion", DEFAULT_PROTOCOL),
                "capabilities": {"tools": {"listChanged": False}},
                "serverInfo": {"name": SERVER_NAME, "version": __version__},
            }
        if method == "ping":
            return {}
        if method == "tools/list":
            self._check_params("mcp/tools-list.params.json", params)
            listing = self.listing()
            self.tools_listed(session_id)
            return {"tools": listing}
        if method in EMPTY_LISTS:
            return dict(EMPTY_LISTS[method])
        if method == "tools/call":
            name = params.get("name")
            tool = self.tools.get(name) if isinstance(name, str) else None
            if tool is None or set(params) - {"name", "arguments", "_meta"}:
                raise _RpcError(INVALID_PARAMS, "Unknown tool.", "unknown_tool")
            return self.call(tool, session_id, params.get("arguments", {}))
        named = method if method in METHODS else "other"
        raise _RpcError(METHOD_NOT_FOUND, "Method not found.", "method_not_found", named)

    def _check_params(self, rel: str, params: dict[str, Any]) -> None:
        if self.schemas.errors(rel, params):
            raise _RpcError(INVALID_PARAMS, "Invalid params.", "params_invalid")

    def listing(self) -> list[dict[str, Any]]:
        if self._listing is None:
            self._listing = [
                {
                    "name": tool.name,
                    "description": tool.description,
                    "inputSchema": dereference(self.schemas, tool.input_schema),
                }
                for tool in self.tools.values()
            ]
        return self._listing

    def call(self, tool: Tool, session_id: str, arguments: object) -> dict[str, Any]:
        if tool.name not in WRITE_TOOLS:
            taken = self.limiter.take(session_id)
            if taken != "ok":
                if taken == "refused":
                    self.refused(session_id, RATE_LIMITED, tool.name)
                message = "Too many platform reads of this session; try again in a few seconds."
                return _text({"error": RATE_LIMITED, "message": message}, error=True)
        if not isinstance(arguments, dict) or self.schemas.errors(tool.input_schema, arguments):
            self.refused(session_id, "params_invalid", tool.name)
            return _text({"error": "params_invalid", "message": "The arguments do not match the tool."}, error=True)
        try:
            value = tool.handler(ToolCall(tool.name, session_id, arguments, self.control, self.schemas))
        except ToolError as error:
            if isinstance(error, ToolRefused):
                self.refused(session_id, error.code, tool.name)
            return _text({"error": error.code, "message": error.message}, error=True)
        return _text(value, error=False)
