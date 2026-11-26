"""Journal handler `platform.query` (`<area>/journal.py:HANDLERS`, ADR-027 section 4, ADR-038; 03-api.md sections 7.2,
8.1 and 10; GST-07).

The guest journals every read of a forwarded platform tool as `platform.query {pending_id, tool, arguments}` and waits
for `POST /v1/pending/{id}/answer`. The handler runs inside the journal page's transaction (`sessions.consume`): for a
session that runs on this VM it captures the instructing human of the read's turn (`credentials.acting.stamp`, in
the guest's journal order, ADR-089 section 1) and hands both to the controller (`controller.Subagents.spawn_query`),
which answers it
outside the transaction with the tool registered at `<area>/query_tools.py:TOOLS` (`get_subagent_result` here, the
other read tools from RUN-08), after recording the `platform.read` event and the `read.session.<tool>` audit. A read
of a tool without a registered provider is not answered (the guest's timeout, as before), and a pending call is taken
once even when its journal page is read again.

A body that is not `{pending_id, tool, arguments}` with a UUID, a read tool of 03-api.md section 8.1 and an argument
object that can be stored is refused (`JournalRecordInvalid`); the journal moves on.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Mapping
from typing import Any

from psycopg import AsyncConnection

from agent_runner.credentials import acting
from agent_runner.guestlink.client import JournalRecord
from agent_runner.operations.journal import storable
from agent_runner.sessions.journal import JournalRecordInvalid
from agent_runner.subagents.controller import controller_for

KIND = "platform.query"
READ_TOOLS = frozenset(
    {
        "get_context",
        "list_workgroups",
        "list_members",
        "list_tasks",
        "get_task",
        "get_task_messages",
        "get_whiteboard",
        "get_notifications",
        "get_operation",
        "list_operations",
        "get_subagent_result",
        "get_usage",
        "list_audit",
        "list_workgroup_operations",
        "get_metrics",
    }
)
"""The `read.session.<tool>` tools of `contracts/enums/outbox-audit-actions.json` (a test compares them)."""
_UUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")


def parse(body: Mapping[str, Any]) -> tuple[str, str, dict[str, Any]]:
    """`(pending_id, tool, arguments)` of a `platform.query` body (`JournalRecordInvalid` otherwise)."""
    pending_id, tool, arguments = body.get("pending_id"), body.get("tool"), body.get("arguments")
    if (
        set(body) != {"pending_id", "tool", "arguments"}
        or not isinstance(pending_id, str)
        or not _UUID_RE.fullmatch(pending_id)
        or tool not in READ_TOOLS
        or not isinstance(arguments, dict)
        or not storable(arguments)
    ):
        raise JournalRecordInvalid(KIND)
    assert isinstance(tool, str)
    return pending_id, tool, arguments


async def platform_query(journal: Any, conn: AsyncConnection[Any], record: JournalRecord) -> None:
    session = await journal.session(conn, record)
    if session is None:
        return
    pending_id, tool, arguments = parse(record.body)
    controller = controller_for(journal.env.context)
    if controller is None:
        journal.env.log(f"runner.subagents query_unrouted tool={tool} session_id={session.id}")
        return
    # the instructing human at this point of the journal, before a later prompt of the guest's order (ADR-089)
    actor_id = await acting.stamp(conn, session)
    controller.spawn_query(journal.vm, session, pending_id, tool, arguments, actor_id=actor_id)


HANDLERS: dict[str, Callable[..., Any]] = {KIND: platform_query}
