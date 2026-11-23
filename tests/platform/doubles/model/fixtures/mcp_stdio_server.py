"""A minimal stdio MCP server for the real Claude Code run against the model double (copied from Spike C).

Speaks newline-delimited JSON-RPC 2.0 on stdin/stdout (MCP stdio transport), standard library only. Tools of the
server ``spike`` that the scenario ``e2e-tools`` calls: ``add`` (sum of ``a`` and ``b``), ``task_context`` (a fixed
organization/workgroup/task context) and ``fail`` (always ``isError: true``). Every received method is appended to
the file named by ``MCP_SPIKE_LOG`` when set.
"""

from __future__ import annotations

import json
import os
import sys
from typing import Any

SERVER_INFO = {"name": "spike", "version": "0.1.0"}
DEFAULT_PROTOCOL = "2025-06-18"
TOOLS = [
    {
        "name": "add",
        "description": "Add two numbers and return the sum.",
        "inputSchema": {
            "type": "object",
            "properties": {"a": {"type": "number"}, "b": {"type": "number"}},
            "required": ["a", "b"],
        },
    },
    {
        "name": "task_context",
        "description": "Return the organization, workgroup and task of this session.",
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "fail",
        "description": "Always fails. Only for testing error handling.",
        "inputSchema": {"type": "object", "properties": {"reason": {"type": "string"}}},
    },
]
CONTEXT = {"organization": "org-spike", "workgroup": "wg-default", "task": "task-0001"}


def _text(text: str, *, error: bool = False) -> dict[str, Any]:
    return {"content": [{"type": "text", "text": text}], "isError": error}


def call_tool(name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    """The MCP result of one tool call."""
    if name == "add":
        try:
            total = float(arguments["a"]) + float(arguments["b"])
        except (KeyError, TypeError, ValueError):
            return _text("add needs numbers a and b", error=True)
        return _text(f"sum={total:g}")
    if name == "task_context":
        return _text(json.dumps(CONTEXT, sort_keys=True))
    if name == "fail":
        return _text(f"refused: {arguments.get('reason', 'always fails')}", error=True)
    return _text(f"unknown tool {name}", error=True)


def handle(message: dict[str, Any]) -> dict[str, Any] | None:
    """The response to one JSON-RPC message; None for notifications."""
    if "id" not in message:
        return None
    method = message.get("method")
    params = message.get("params") or {}
    result: Any
    if method == "initialize":
        result = {
            "protocolVersion": params.get("protocolVersion", DEFAULT_PROTOCOL),
            "capabilities": {"tools": {"listChanged": False}},
            "serverInfo": SERVER_INFO,
        }
    elif method == "ping":
        result = {}
    elif method == "tools/list":
        result = {"tools": TOOLS}
    elif method == "tools/call":
        result = call_tool(str(params.get("name")), params.get("arguments") or {})
    else:
        error = {"code": -32601, "message": f"method not found: {method}"}
        return {"jsonrpc": "2.0", "id": message["id"], "error": error}
    return {"jsonrpc": "2.0", "id": message["id"], "result": result}


def main() -> None:
    log_path = os.environ.get("MCP_SPIKE_LOG")
    for raw in sys.stdin:
        line = raw.strip()
        if not line:
            continue
        message = json.loads(line)
        if log_path:
            with open(log_path, "a", encoding="utf-8") as handle_log:
                handle_log.write(json.dumps({"method": message.get("method"), "params": message.get("params")}) + "\n")
        response = handle(message)
        if response is not None:
            sys.stdout.write(json.dumps(response) + "\n")
            sys.stdout.flush()


if __name__ == "__main__":
    main()
