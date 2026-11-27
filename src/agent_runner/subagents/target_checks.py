"""Pre-checks of `subagent.instruct` and `subagent.stop` (registered in `op_prechecks.py:PRECHECKS`, ADR-041; 03-api.md
section 9, ADR-088 section 1).

The hub cannot read the parameters, so the runner checks the target locally before the registration: `session_id`
must name a child of the requesting session in the same task that has not ended (not `stopping`, `stopped` or
`failed`); anything else is refused with `not_found`. An instruction for a `suspended` child resumes it; that is never
refused because the VM of its task is full (HOT-32: a child belongs to running work; the placement of new tasks keeps
the reserve for it, `vm.room`).
"""

from __future__ import annotations

from typing import Any

from psycopg import AsyncConnection

from agent_runner.operations.extensions import CheckFailed, PrecheckRequest, PrecheckResult
from agent_runner.orgs.db import session as store_session
from agent_runner.sessions import records
from agent_runner.sessions.records import FINAL, LocalSession

NOT_CHILD = "The session is no running subagent of this session."


async def _target(conn: AsyncConnection[Any], request: PrecheckRequest) -> LocalSession:
    parent = request.session
    child = await records.get(conn, str(request.params["session_id"]))
    if (
        child is None
        or child.parent_id != parent.id
        or child.task_id != parent.task_id
        or child.state in FINAL
        or child.state == "stopping"
    ):
        raise CheckFailed("not_found", message=NOT_CHILD)
    return child


async def instruct_check(request: PrecheckRequest) -> PrecheckResult:
    async with store_session(request.context.pool) as conn:
        await _target(conn, request)
    return PrecheckResult()


async def stop_check(request: PrecheckRequest) -> PrecheckResult:
    async with store_session(request.context.pool) as conn:
        await _target(conn, request)
    return PrecheckResult()
