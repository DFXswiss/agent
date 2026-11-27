"""Helpers of the HOT-28 suites: a continuation the platform wrote is accepted by the guest, then the continued turn
asks for a `github.*` operation - through the real session and operation journal handlers and the acting-user
observer, in the guest's order (`idle` of the interrupted turn, `prompt_accepted` of the continuation,
`operation.request`)."""

from __future__ import annotations

from typing import Any

from agent_runner.credentials import session_state as acting_state
from agent_runner.crypto.ids import uuid7
from agent_runner.guestlink.client import JournalRecord
from agent_runner.operations import records as operation_records
from agent_runner.operations.journal import HANDLERS as OPERATION_HANDLERS
from agent_runner.sessions.consume import VmJournal
from agent_runner.sessions.journal import HANDLERS as SESSION_HANDLERS
from agent_runner.vm.records import VmRow

AT = "2026-11-21T12:00:01.000Z"


def _record(seq: int, sid: str, kind: str, body: dict[str, Any]) -> JournalRecord:
    return JournalRecord(seq=seq, ts=AT, session_id=sid, kind=kind, body=body)


async def continued_request(
    session_env: Any, row: VmRow, sid: str, prompt_id: str, *, seq: int = 9000, op_type: str = "github.comment"
) -> operation_records.Operation:
    """The guest accepts the continuation `prompt_id` of `sid` and the turn requests `op_type`; its stored row."""
    op_id = uuid7()
    request = {
        "pending_id": uuid7(),
        "operation_id": op_id,
        "type": op_type,
        "params": {"repo": "octo-org/hello", "number": 1, "body": "Continued."},
        "requested_at": AT,
        "local_check": "passed",
    }
    page = [
        _record(seq, sid, "session.state", {"state": "idle", "at": AT}),
        _record(seq + 1, sid, "session.state", {"state": "prompt_accepted", "prompt_id": prompt_id, "at": AT}),
        _record(seq + 2, sid, "operation.request", request),
    ]
    reader = VmJournal(
        session_env, row, handlers={**SESSION_HANDLERS, **OPERATION_HANDLERS}, observers=acting_state.OBSERVERS
    )
    await reader.handle(page)
    async with session_env.transaction() as conn:
        found = await operation_records.get(conn, op_id)
    assert found is not None
    return found


async def set_turn(pool: Any, sid: str, actor: str | None) -> None:
    """`actor` gave the instruction of the session's running turn (`prompt_accepted` of their prompt)."""
    async with pool.connection() as conn:
        await conn.execute("UPDATE runner.session SET turn_actor = %s, turn_open = true WHERE id = %s", (actor, sid))


def continued_audits(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """The details of the audit rows `session.continued` among outbox audit bodies."""
    return [item["detail"] for item in items if item.get("action") == "session.continued"]
