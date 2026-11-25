"""Pre-check of `subagent.request` (`<area>/op_prechecks.py:PRECHECKS`, ADR-041; the target checks of
`subagent.instruct` and `subagent.stop` are `target_checks.py`; 03-api.md sections 3.1 and 9,
05-runner.md section 13, ADR-088 section 2).

Before the registration the runner checks the subagent limits of the platform (`limits.current`, the section `limits`
of the last desired state; 0 = no limit) with what it knows: the depth of the requesting session, and the children of
the task on this device - sessions with `depth` > 0 plus accepted requests whose child has not started yet - running
(not `stopped`, `failed` or `suspended`) and in total. Without the section (an older hub) these checks are left to the
hub. On a VM whose guest is older than ADR-088 (`guest_features`) a child deeper than 2 is refused as well (that
guest's contract). The parent's VM must have room for one more session (`limits.vm_room_refusal`,
`AGENT_RUNNER_SESSIONS_PER_VM`). A request over a limit is refused locally with `limit_exceeded` and a sentence that
names the limit, its value and the setting (registered as `rejected`, never executed). The hub decides the limits
bindingly (check H4, `denied` `limit_exceeded`), so a request that passes here may still be denied.

A child inherits the parent's access kind (05 section 13): for a parent with platform access the request is refused
with `budget_exceeded` while the organization's daily platform budget is spent (`model.gateways`, RUN-12).
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from agent_runner.model.gateways import platform_budget_spent
from agent_runner.operations.extensions import CheckFailed, PrecheckRequest, PrecheckResult
from agent_runner.orgs.db import session as store_session
from agent_runner.subagents import limits as subagent_limits
from agent_runner.subagents import store, target_checks
from agent_runner.subagents.guest_features import current_guest


def _refuse(message: str | None) -> None:
    if message is not None:
        raise CheckFailed("limit_exceeded", message=message)


async def limits(request: PrecheckRequest) -> PrecheckResult:
    session = request.session
    found = subagent_limits.current(request.context)
    _refuse(subagent_limits.depth_refusal(found, session.depth))
    if session.model_access == "platform" and platform_budget_spent(request.context, session.org_id):
        raise CheckFailed("budget_exceeded", message="The daily platform model budget of the organization is used up.")
    async with store_session(request.context.pool) as conn:
        if not await current_guest(conn, session.vm_id):
            _refuse(subagent_limits.old_guest_depth_refusal(session.depth))
        _refuse(await subagent_limits.vm_room_refusal(request.context, conn, session.vm_id))
        running, total = await store.counts(conn, request.operation.task_id)
    _refuse(subagent_limits.running_refusal(found, running))
    _refuse(subagent_limits.total_refusal(found, total))
    return PrecheckResult()


PRECHECKS: dict[str, Callable[..., Any]] = {
    "subagent.request": limits,
    # the target of a follow-up or stop (ADR-088 section 1)
    "subagent.instruct": target_checks.instruct_check,
    "subagent.stop": target_checks.stop_check,
}
