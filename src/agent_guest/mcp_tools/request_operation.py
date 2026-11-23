"""MCP tool ``request_operation`` (03-api.md §8.1, §12; 05-runner.md §12 steps 1 and 7; ADR-012).

The guest decides and executes nothing. It gives the call a new ``operation_id`` (UUIDv7) and journals it as
``operation.request {pending_id, operation_id, type, params, idempotency_key?, requested_at, local_check,
local_error?}`` (pending call of kind
``operation``) for the runner, then:

* local check (05 §12 step 1): a ``type`` that is not in ``ops/INDEX.json`` is ``unknown_type``; ``params`` that do not
  match ``ops/<type>.params.json`` (closed objects, integers never booleans) are ``params_invalid`` with the JSON
  pointers below ``/params``. Such a request is still forwarded with ``local_check:"rejected"`` and the error as
  ``local_error`` (binding for the runner: it records the operation at the hub as rejected and never executes it,
  contract change GST-07), and the tool returns ``{operation_id, status:"rejected", error}`` at once; any other
  request carries ``local_check:"passed"`` (a hint: the runner repeats the formal checks)
* otherwise the tool waits up to ``WAIT_SECONDS`` (20 s, 03 §12) for the runner's answer to the pending call (sent when
  the operation is final, 05 §12 step 7) and returns it; no answer in time is ``{operation_id, status:"pending"}``
  (the runner pings ``operation.finished`` later; ``get_operation`` reads the result)

Tool arguments that do not match ``mcp/tools/request_operation.input.json`` are refused by the MCP server
(``params_invalid``) before this handler runs: without a valid ``type`` and ``params`` object there is no
``operation.request`` body to record (decisions GST-07).
"""

from __future__ import annotations

import json
import threading
from typing import Any

from agent_guest.clock import Clock, SystemClock, iso_ms, uuid7
from agent_guest.mcp import Tool, ToolCall
from agent_guest.mcp_tools import _forward
from agent_guest.schema import SchemaSet, pointers

WAIT_SECONDS = 20.0
MAX_POINTER_LENGTH = 256
clock: Clock = SystemClock()

_types: dict[str, frozenset[str]] = {}
_types_lock = threading.Lock()


def known_types(schemas: SchemaSet) -> frozenset[str]:
    """The operation types of ``ops/INDEX.json`` (read once per contract directory)."""
    key = str(schemas.root)
    with _types_lock:
        found = _types.get(key)
        if found is None:
            index = json.loads((schemas.root / "ops" / "INDEX.json").read_text(encoding="utf-8"))
            found = _types[key] = frozenset(str(entry["type"]) for entry in index["types"])
        return found


def local_check(schemas: SchemaSet, kind: str, params: dict[str, Any]) -> dict[str, Any] | None:
    """The operation error of a request the guest already knows to be invalid, else ``None``."""
    if kind not in known_types(schemas):
        return {"code": "unknown_type", "message": "The operation type is not known."}
    violations = schemas.errors(f"ops/{kind}.params.json", params)
    if not violations:
        return None
    error: dict[str, Any] = {"code": "params_invalid", "message": "The parameters do not match the operation type."}
    found = [f"/params{pointer}" for pointer in pointers(violations)]
    found = [pointer for pointer in found if len(pointer) <= MAX_POINTER_LENGTH]
    if found:
        error["pointers"] = found
    return error


def request_operation(call: ToolCall) -> Any:
    now = clock.now()
    operation_id = uuid7(now)
    body: dict[str, Any] = {
        "operation_id": operation_id,
        "type": call.arguments["type"],
        "params": call.arguments["params"],
        "requested_at": iso_ms(now),
    }
    if "idempotency_key" in call.arguments:
        body["idempotency_key"] = call.arguments["idempotency_key"]
    error = local_check(call.schemas, body["type"], body["params"])
    if error is not None:
        _forward.forward(call, "operation", {**body, "local_check": "rejected", "local_error": error})
        return {"operation_id": operation_id, "status": "rejected", "error": error}
    answer = _forward.ask(call, "operation", {**body, "local_check": "passed"}, WAIT_SECONDS)
    if answer is None:
        return {"operation_id": operation_id, "status": "pending"}
    return _forward.outcome(call, answer, operation_id=operation_id)


TOOL = Tool(
    name="request_operation",
    description="Request a platform operation (for example github.checkout, github.commit, github.create_pull_request, "
    "subagent.request) with its parameters; returns the result, or status pending after 20 s (then use "
    "get_operation).",
    handler=request_operation,
)
