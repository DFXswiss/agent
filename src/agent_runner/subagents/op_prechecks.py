"""Pre-check of `subagent.request` (`<area>/op_prechecks.py:PRECHECKS`, ADR-041; 03-api.md sections 3.1 and 9,
05-runner.md section 13, ADR-017).

Before the registration the runner checks the subagent limits with what it knows: the depth of the requesting session
(a child of it would be deeper than 2) and the children of the task on this device - sessions with `depth` > 0 plus
accepted requests whose child has not started yet - at most 3 running and 10 in total. A request over a limit is
refused locally with `limit_exceeded` (registered as `rejected`, never executed). The hub decides the limits bindingly
(check H4, `denied` `limit_exceeded`), so a request that passes here may still be denied.

A child inherits the parent's access kind (05 section 13): for a parent with platform access the request is refused
with `budget_exceeded` while the organization's daily platform budget is spent (`model.gateways`, RUN-12).
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from agent_runner.model.gateways import platform_budget_spent
from agent_runner.operations.extensions import CheckFailed, PrecheckRequest, PrecheckResult
from agent_runner.orgs.db import session as store_session
from agent_runner.subagents import store
from agent_runner.subagents.rules import MAX_DEPTH, MAX_RUNNING, MAX_TOTAL


async def limits(request: PrecheckRequest) -> PrecheckResult:
    if request.session.depth + 1 > MAX_DEPTH:
        raise CheckFailed("limit_exceeded", message="Subagents may be nested at most two levels deep.")
    session = request.session
    if session.model_access == "platform" and platform_budget_spent(request.context, session.org_id):
        raise CheckFailed("budget_exceeded", message="The daily platform model budget of the organization is used up.")
    async with store_session(request.context.pool) as conn:
        running, total = await store.counts(conn, request.operation.task_id)
    if running >= MAX_RUNNING:
        raise CheckFailed("limit_exceeded", message="At most three subagents of a task may run at a time.")
    if total >= MAX_TOTAL:
        raise CheckFailed("limit_exceeded", message="A task may start at most ten subagents.")
    return PrecheckResult()


PRECHECKS: dict[str, Callable[..., Any]] = {"subagent.request": limits}
