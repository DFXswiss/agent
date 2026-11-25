"""Pre-checks of this area's operation types (`<area>/op_prechecks.py:PRECHECKS`; ADR-091 sections 4 and 6).

- `review.verdict`: with `separate_duties` set in the workgroup, a session that made or pushed the head itself may not
  give a verdict on it (`rights_denied`). Whether it wrote other commits of the pull request is decided at the merge,
  which reads the pull request's commits: such a verdict never counts there.
- `security.report`: a `run_id` names a `test.run` of this task on this device (`not_found` otherwise).

Neither type reaches GitHub: a verdict names its head, and only a verdict on the head a merge finds counts there.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from agent_runner.operations.extensions import CheckFailed, PrecheckRequest, PrecheckResult
from agent_runner.orgs.db import session as store_session
from agent_runner.reviews import criteria, store


async def verdict_check(request: PrecheckRequest) -> PrecheckResult:
    if request.settings.separate_duties:
        async with store_session(request.context.pool) as conn:
            made = await store.session_commits(conn, request.session.id)
        if str(request.params["head_sha"]).lower() in made:
            raise CheckFailed("rights_denied", message=criteria.SEPARATE)
    return PrecheckResult()


async def report_check(request: PrecheckRequest) -> PrecheckResult:
    run_id = request.params.get("run_id")
    if run_id is None:
        return PrecheckResult()
    async with store_session(request.context.pool) as conn:
        cursor = await conn.execute(
            "SELECT 1 FROM runner.operation WHERE type = 'test.run' AND task_id = %s AND result->>'run_id' = %s",
            (request.operation.task_id, run_id),
        )
        found = await cursor.fetchone()
    if found is None:
        raise CheckFailed("not_found", message="There is no test run with this run_id in this task.")
    return PrecheckResult()


PRECHECKS: dict[str, Callable[..., Any]] = {"review.verdict": verdict_check, "security.report": report_check}
