"""Pre-checks of `subagent.instruct` and `subagent.stop` (registered in `op_prechecks.py:PRECHECKS`, ADR-041; 03-api.md
section 9, ADR-088 section 1).

The hub cannot read the parameters, so the runner checks the target locally before the registration: `session_id`
must name a child of the requesting session in the same task that has not ended (not `stopping`, `stopped` or
`failed`); anything else is refused with `not_found`. An instruction for a `suspended` child resumes it, so it is
running again: the child's VM must have the free resources for one more session (`limits.vm_room_refusal`, measured;
no fixed number of running subagents, ADR-075 addendum 2); else `limit_exceeded` with the sentence why. The check
before the registration is not atomic, so the executor repeats it (`resume_refusal`) right before it resumes the
child, serialised per task, and a child suspended while its instruction waited is checked there too.
"""

from __future__ import annotations

from typing import Any

from psycopg import AsyncConnection

from agent_runner.operations.extensions import CheckFailed, PrecheckRequest, PrecheckResult
from agent_runner.orgs.db import session as store_session
from agent_runner.sessions import records
from agent_runner.sessions.records import FINAL, LocalSession
from agent_runner.subagents import limits as subagent_limits

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


async def resume_refusal(context: Any, conn: AsyncConnection[Any], child: LocalSession) -> str | None:
    """The sentence why the suspended `child` may not run again now (its VM has no free resources for it), None when
    it may. The executor asks again right before it resumes the child, under one lock per task (module docstring)."""
    return await subagent_limits.vm_room_refusal(context, conn, child.vm_id)


async def instruct_check(request: PrecheckRequest) -> PrecheckResult:
    async with store_session(request.context.pool) as conn:
        child = await _target(conn, request)
        if child.state != "suspended":
            return PrecheckResult()
        refused = await resume_refusal(request.context, conn, child)
    if refused is not None:
        raise CheckFailed("limit_exceeded", message=refused)
    return PrecheckResult()


async def stop_check(request: PrecheckRequest) -> PrecheckResult:
    async with store_session(request.context.pool) as conn:
        await _target(conn, request)
    return PrecheckResult()
