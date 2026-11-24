"""Journal handlers `platform.read` and `platform.refused` (`<area>/journal.py:HANDLERS`, ADR-027 section 4;
03-api.md sections 7.2, 8 and 10; contracts/changes/KOR-07.md).

03-api.md section 8: every MCP call is journaled and every read is an audit row. Forwarded reads are recorded by the
controller that answers them (`subagents`, RUN-08). Two kinds cover the rest:

- `platform.read {tool:"get_notifications", result_count}` - the read the guest answers itself (its pings). The
  handler records what the controller records for a forwarded read: the device event `platform.read {tool,
  arguments:{}, result_count}` (its `ref` the session) and the outbox audit `read.session.get_notifications`
  (`{result_count}`, result `ok`, target the session).
- `platform.refused {code, tool, method?}` - an MCP request the guest refused before forwarding or answering it. A
  refused read tool (`params_invalid`, `rate_limited` or - for `get_subagent_result` asked for the session itself -
  `not_found`, with the name of a read tool) is a read attempt: audit `read.session.<tool>` with result `rejected` and
  `result_count` 0, no event (nothing was read). A body over the limit (`size_exceeded`) names no tool. Every refusal
  gets one fact line `runner.session_api refused session_id=... code=... tool=... method=...` (KOR-10: the guest
  journals only the first refused read of a rate-limited stretch); `method` names the method of a `method_not_found`
  (a request method of the protocol by name, else `other`; `-` for every other refusal and for an older guest that
  does not send it, HOT-09).

Both run in the journal page's transaction for a session that runs on this VM; a body outside the contract is refused
(`JournalRecordInvalid`) and the journal moves on. The guest is not trusted: the bodies are closed enums and a count,
so a session can neither put content into the event, the audit or the log line nor name another session.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Any

from psycopg import AsyncConnection

from agent_runner.guestlink.client import JournalRecord
from agent_runner.sessions.events import session_event
from agent_runner.sessions.journal import JournalRecordInvalid
from agent_runner.subagents.journal import READ_TOOLS

READ = "platform.read"
REFUSED = "platform.refused"
GUEST_READS = frozenset({"get_notifications"})
"""Read tools the guest answers itself (journal `platform.read`)."""
PROTOCOL_CODES = frozenset({"parse_error", "invalid_request", "method_not_found", "unknown_tool", "size_exceeded"})
PARAMS_INVALID = "params_invalid"
RATE_LIMITED = "rate_limited"
OWN_SESSION = ("not_found", "get_subagent_result")
METHOD_NOT_FOUND = "method_not_found"
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
        "other",
    }
)
"""The names a `method_not_found` may carry (`contracts/guest/journal/record.json#/$defs/body_platform_refused`)."""
TOOLS = READ_TOOLS | {"request_operation"}
MAX_COUNT = 2**53 - 1


def parse_read(body: Mapping[str, Any]) -> tuple[str, int]:
    """`(tool, result_count)` of a `platform.read` body (`JournalRecordInvalid` otherwise)."""
    tool, count = body.get("tool"), body.get("result_count")
    if set(body) != {"tool", "result_count"} or tool not in GUEST_READS:
        raise JournalRecordInvalid(READ)
    if type(count) is not int or not 0 <= count <= MAX_COUNT:
        raise JournalRecordInvalid(READ)
    return str(tool), count


def parse_refused(body: Mapping[str, Any]) -> tuple[str, str | None, str | None]:
    """`(code, tool, method)` of a `platform.refused` body (`JournalRecordInvalid` otherwise)."""
    code, tool, method = body.get("code"), body.get("tool"), body.get("method")
    if set(body) == {"code", "tool", "method"}:
        if code == METHOD_NOT_FOUND and tool is None and method in METHODS:
            return code, None, str(method)
        raise JournalRecordInvalid(REFUSED)
    if set(body) != {"code", "tool"}:
        raise JournalRecordInvalid(REFUSED)
    if code in PROTOCOL_CODES and tool is None:
        return str(code), None, None
    if code == PARAMS_INVALID and (tool is None or tool in TOOLS):
        return code, None if tool is None else str(tool), None
    if (code == RATE_LIMITED and tool in READ_TOOLS) or (code, tool) == OWN_SESSION:
        return str(code), str(tool), None
    raise JournalRecordInvalid(REFUSED)


def _audit(session: Any, tool: str, result: str, count: int) -> dict[str, Any]:
    return {
        "action": f"read.session.{tool}",
        "result": result,
        "task_id": session.task_id,
        "session_id": session.id,
        "target_kind": "session",
        "target_id": session.id,
        "detail": {"result_count": count},
    }


async def platform_read(journal: Any, conn: AsyncConnection[Any], record: JournalRecord) -> None:
    session = await journal.session(conn, record)
    if session is None:
        return
    tool, count = parse_read(record.body)
    read = {"tool": tool, "arguments": {}, "result_count": count}
    at = journal.env.at()  # the device time, as the controller stamps a forwarded read
    await session_event(conn, journal.env.outbox, session, kind=READ, body=read, ts=at, ref=session.id)
    await journal.env.outbox.enqueue(conn, "audit", _audit(session, tool, "ok", count))


async def platform_refused(journal: Any, conn: AsyncConnection[Any], record: JournalRecord) -> None:
    session = await journal.session(conn, record)
    if session is None:
        return
    code, tool, method = parse_refused(record.body)
    journal.env.log(
        f"runner.session_api refused session_id={session.id} code={code} tool={tool or '-'} method={method or '-'}"
    )
    if tool in READ_TOOLS:
        await journal.env.outbox.enqueue(conn, "audit", _audit(session, str(tool), "rejected", 0))


HANDLERS: dict[str, Callable[..., Any]] = {READ: platform_read, REFUSED: platform_refused}
