"""Pre-check of `task.mode` (`<area>/op_prechecks.py:PRECHECKS`, ADR-041; ADR-098 section 3).

Switching a mode on needs the quote of the person's instruction: `quote.instructed` with the operation's acting user
(`Operation.actor_id`, the verified author of the turn, which also travels to the hub as `triggered_by`); anything
else is refused locally with `mode_not_instructed` and the sentence of ADR-098 (registered `rejected`, never
executed). Switching off needs no quote: every mode fails safe when it stops. The hub decides the rest bindingly at the
device route (the mode is offered, the chooser holds `wg:write`, the needs are on).
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any, cast

from agent_runner.modes import quote
from agent_runner.operations.extensions import CheckFailed, PrecheckRequest, PrecheckResult
from agent_runner.orgs.db import session as store_session
from agent_runner.sync.runtime import SyncRuntime, runtime

NOT_INSTRUCTED = "Only a person's instruction can switch on a mode; ask with human.question."


async def mode_check(request: PrecheckRequest) -> PrecheckResult:
    params = request.params
    if not params["on"]:
        return PrecheckResult()
    storage_key = cast(SyncRuntime, runtime(request.context)).storage_key
    async with store_session(request.context.pool) as conn:
        found = await quote.instructed(
            conn, storage_key, request.session, request.operation.actor_id, str(params.get("quote", ""))
        )
    if not found:
        raise CheckFailed("mode_not_instructed", message=NOT_INSTRUCTED)
    return PrecheckResult()


PRECHECKS: dict[str, Callable[..., Any]] = {"task.mode": mode_check}
