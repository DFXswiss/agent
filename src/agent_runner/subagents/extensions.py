"""Extension points of the subagent area (ADR-027 section 4 style; ADR-038).

Discovered with `registry.discover` (areas in sorted order), so later packages add files and never edit this area:

- `<area>/query_tools.py:TOOLS`: mapping MCP read tool -> `QueryTool`. The guest forwards every read of a platform tool
  as a journal record `platform.query` (03-api.md section 7.2, GST-07); this area's journal handler
  (`journal.py`) answers it with the registered tool. This area registers `get_subagent_result`; RUN-08 registers the
  other read tools here instead of a second `platform.query` handler. A tool name is registered at most once.
- `<area>/subagent_finished.py:NOTIFY`: `notify(context, ping) -> Awaitable[None]` with a `FinishedPing`, called once
  when a child session ended (05-runner.md section 13: content-free ping `subagent.finished` to the parent session).
  At most one provider (RUN-14, `send_ping`); without one the end is only logged.

A `QueryTool` is `tool(call: QueryCall) -> Awaitable[QueryOutcome]`. The handler records every answered read as the
device event `platform.read {tool, arguments, result_count}` and the audit `read.session.<tool>` (03-api.md sections 8
and 10) and then answers the pending call; a tool only computes the outcome.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from typing import Any

from agent_runner import registry
from agent_runner.errors import RunnerError
from agent_runner.sessions.records import LocalSession

TOOL_ERRORS = frozenset({"params_invalid", "not_found", "forbidden", "unavailable", "timeout"})
"""The `contracts/mcp/objects.json#/$defs/tool_error` codes a runner query tool may answer (the guest's own read limit
`rate_limited` is not one of them)."""
DENIED = frozenset({"not_found", "forbidden"})


@dataclass(frozen=True)
class QueryCall:
    """A read of `session` (the session of the bearer token) with the tool's checked `arguments` and the instructing
    human of the read's turn, captured when the read arrived (`actor_id`, ADR-089 section 1; None: unknown)."""

    context: Any
    session: LocalSession
    tool: str
    arguments: Mapping[str, Any]
    actor_id: str | None = None


@dataclass(frozen=True)
class QueryOutcome:
    """The tool's result (`mcp/tools/<tool>.output.json`) or error code and English sentence, the number of objects
    returned (`result_count`), the audited target and the workgroup read (`read_wg_id`, None on the organization
    level, ADR-089 section 4)."""

    result: Any = None
    error: str | None = None
    message: str | None = None
    result_count: int = 0
    target_kind: str = "session"
    target_id: str | None = None
    read_wg_id: str | None = None

    def answer(self) -> dict[str, Any]:
        """The body of `POST /v1/pending/{id}/answer`."""
        if self.error is None:
            return {"status": "ok", "result": self.result}
        return {"status": "error", "error": {"error": self.error, "message": self.message or "The read failed."}}

    @property
    def audit_result(self) -> str:
        if self.error is None:
            return "ok"
        return "denied" if self.error in DENIED else "failed"


def failure(error: str, message: str, *, target_id: str | None = None, read_wg_id: str | None = None) -> QueryOutcome:
    if error not in TOOL_ERRORS:
        raise ValueError(error)
    return QueryOutcome(error=error, message=message, target_id=target_id, read_wg_id=read_wg_id)


@dataclass(frozen=True)
class FinishedPing:
    """The content-free `subagent.finished` ping for the parent session: kind, IDs, a stable ping ID."""

    parent_session_id: str
    child_session_id: str
    ping_id: str
    kind: str = "subagent.finished"


QueryTool = Callable[[QueryCall], Awaitable[QueryOutcome]]
Notify = Callable[[Any, FinishedPing], Awaitable[None]]


def query_tools(package: str = registry.ROOT_PACKAGE) -> dict[str, QueryTool]:
    found: dict[str, QueryTool] = {}
    for area, value in registry.discover("query_tools", "TOOLS", package):
        if not isinstance(value, Mapping):
            raise RunnerError("registry_invalid", f"{area}.query_tools")
        for name, tool in value.items():
            if not isinstance(name, str) or not callable(tool):
                raise RunnerError("registry_invalid", f"{area}.query_tools")
            if name in found:
                raise RunnerError("registry_duplicate", name)
            found[name] = tool
    return found


def finished_notifier(package: str = registry.ROOT_PACKAGE) -> Notify | None:
    found: list[Notify] = []
    for area, value in registry.discover("subagent_finished", "NOTIFY", package):
        if not callable(value):
            raise RunnerError("registry_invalid", f"{area}.subagent_finished")
        found.append(value)
    if len(found) > 1:
        raise RunnerError("registry_duplicate", "NOTIFY")
    return found[0] if found else None
