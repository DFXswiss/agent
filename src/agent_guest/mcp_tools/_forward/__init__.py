"""Forwarding helpers of the platform tools (GST-07; 03-api.md §8.1, §12; 05-runner.md §12 step 1).

A sub-package, so the tool registry (``registry.modules`` lists plain modules only) does not take it for a tool.

* ``read_tool(name, description)`` - a read tool: ``mcp.forward_query`` (journal ``platform.query``) with the handler
  limit ``READ_WAIT_SECONDS`` (05 §5.1: 10 s); no answer is the tool error ``timeout``
* ``waiting_read(call)`` - a read whose arguments carry ``wait_seconds`` (``get_operation``, ``get_subagent_result``):
  the runner holds its answer up to ``wait_seconds``; the guest waits that long plus ``READ_WAIT_SECONDS``, at most
  ``MAX_WAIT_SECONDS`` (below the ``/mcp`` route limit of 35 s)
* ``NoAnswer`` - the ``timeout`` tool error of a read the runner did not answer in time
* ``checked_read(call, wait, **identity)`` - a read whose result must name the object asked for (``get_task``)
* ``forward(call, kind, body) -> pending_id`` - create the pending call (journal record) without waiting
* ``ask(call, kind, body, wait)`` - create the pending call and wait for the answer (``None``: none in time)
* ``outcome(call, answer, **identity)`` - the checked result of an answer: an error answer is passed on, a result
  that does not match ``mcp/tools/<tool>.output.json`` or names another object than asked for is ``unavailable``
* ``workvm_tool(name, description)`` - a work VM tool (13-work-vm.md §3, KOR-29): the call becomes the pending kind
  ``workvm`` (journal ``workvm.call {pending_id, tool, arguments}``, no read audit); the guest waits the call's
  ``wait_seconds`` (default ``WORKVM_WAIT_SECONDS``, 30) plus ``WORKVM_MARGIN_SECONDS`` (5 s), at most
  ``MAX_WAIT_SECONDS``; the tools without ``wait_seconds`` (``workvm_kill``, ``workvm_status``, ``workvm_sync``,
  ``workvm_browse``) so wait ``MAX_WAIT_SECONDS``. No answer in time is the tool error ``timeout``
  (``WorkvmNoAnswer``, which points the session to ``workvm_status``: the command or sync may still be running)
* ``storable(value)`` - no NUL character or lone surrogate in any string (keys included) and at most ``MAX_DEPTH``
  levels of nesting: the runner's rule for journal records (``agent_runner.operations.journal.storable``, PostgreSQL
  ``jsonb``), which skips any other record without an answer. ``read`` and ``forward`` (so ``ask`` and every read)
  refuse a call whose arguments break it with ``ToolRefused`` (``params_invalid``, journaled ``platform.refused``
  without the arguments) before anything is journaled for the runner (KOR-09, AUD-02 NA-11)

The session is always ``call.session_id``, which the MCP route resolved from the bearer token; no tool takes a session
from its arguments (a ``session_id`` argument of ``get_subagent_result`` names the child to read).
"""

from __future__ import annotations

import re
from typing import Any

from agent_guest.apisock import ApiSockError
from agent_guest.mcp import Tool, ToolCall, ToolError, ToolRefused, forward_query

READ_WAIT_SECONDS = 10.0
MAX_WAIT_SECONDS = 33.0
WORKVM_WAIT_SECONDS = 30
WORKVM_MARGIN_SECONDS = 5.0
MAX_DEPTH = 64
_UNSTORABLE_RE = re.compile("[\\x00\\ud800-\\udfff]")


def storable(value: object, depth: int = 0) -> bool:
    if depth > MAX_DEPTH:
        return False
    if isinstance(value, str):
        return _UNSTORABLE_RE.search(value) is None
    if isinstance(value, dict):
        return all(storable(k, depth + 1) and storable(v, depth + 1) for k, v in value.items())
    if isinstance(value, list):
        return all(storable(item, depth + 1) for item in value)
    return True


def _storable_arguments(call: ToolCall) -> None:
    if not storable(call.arguments):
        raise ToolRefused()


def read(call: ToolCall) -> Any:
    _storable_arguments(call)
    return forward_query(call, READ_WAIT_SECONDS)


def read_tool(name: str, description: str) -> Tool:
    return Tool(name=name, description=description, handler=read)


def forward(call: ToolCall, kind: str, body: dict[str, Any]) -> str:
    _storable_arguments(call)
    try:
        return call.control.pending_create(call.session_id, kind, body)
    except ApiSockError:
        raise _unreachable() from None


def ask(call: ToolCall, kind: str, body: dict[str, Any], wait: float) -> dict[str, Any] | None:
    pending_id = forward(call, kind, body)
    try:
        return call.control.pending_wait(pending_id, call.session_id, wait)
    except ApiSockError:
        raise _unreachable() from None


def _unreachable() -> ToolError:
    return ToolError("unavailable", "The platform is not reachable right now. Try again later.")


def outcome(call: ToolCall, answer: dict[str, Any], **identity: str) -> Any:
    if answer["status"] == "error":
        raise ToolError(str(answer["error"]["error"]), str(answer["error"]["message"]))
    result = answer["result"]
    if call.schemas.errors(f"mcp/tools/{call.tool}.output.json", result) or any(
        result[key] != value for key, value in identity.items()
    ):
        raise ToolError("unavailable", "The platform answered with an invalid result.")
    return result


class NoAnswer(ToolError):
    """The runner did not answer a forwarded read in time (tool error ``timeout``)."""

    def __init__(self) -> None:
        super().__init__("timeout", "The platform did not answer in time. Try again later.")


def checked_read(call: ToolCall, wait: float, **identity: str) -> Any:
    answer = ask(call, "query", {"tool": call.tool, "arguments": call.arguments}, wait)
    if answer is None:
        raise NoAnswer
    return outcome(call, answer, **identity)


def waiting_read(call: ToolCall, **identity: str) -> Any:
    wait = min(READ_WAIT_SECONDS + call.arguments.get("wait_seconds", 0), MAX_WAIT_SECONDS)
    return checked_read(call, wait, **identity)


class WorkvmNoAnswer(ToolError):
    """The runner did not answer a work VM call in time (tool error ``timeout``); the work may still go on."""

    def __init__(self) -> None:
        super().__init__(
            "timeout",
            "The work VM did not answer in time; the command or sync may still be running. Check workvm_status, "
            "then continue with workvm_output for a running job.",
        )


def workvm(call: ToolCall) -> Any:
    wait = min(WORKVM_MARGIN_SECONDS + call.arguments.get("wait_seconds", WORKVM_WAIT_SECONDS), MAX_WAIT_SECONDS)
    answer = ask(call, "workvm", {"tool": call.tool, "arguments": call.arguments}, wait)
    if answer is None:
        raise WorkvmNoAnswer
    return outcome(call, answer)


def workvm_tool(name: str, description: str) -> Tool:
    return Tool(name=name, description=description, handler=workvm)
