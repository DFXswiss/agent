"""`get_whiteboard {level, task_id?}` (03-api.md section 8.1, ADR-024): the live entries of the org whiteboard
(organization level), of the session's workgroup or of a task of it (workgroup level), newest first, from the kept
verified whiteboard events (`projection.entries`). `task_id` is required exactly for level `task` (the tool schema
cannot say so, contract change GST-07; the guest checks it too). A task outside the workgroup is `not_found`.
"""

from __future__ import annotations

from agent_runner.queries import common, projection
from agent_runner.queries.rights import read_scope
from agent_runner.subagents.extensions import QueryCall, QueryOutcome

LEVELS = frozenset({projection.ORG_LEVEL, projection.WG_LEVEL, projection.TASK_LEVEL})


async def get_whiteboard(call: QueryCall) -> QueryOutcome:
    async def read() -> QueryOutcome:
        found_args = common.arguments(call, frozenset({"level", "task_id"}), frozenset({"level"}))
        level = found_args["level"]
        if (
            not isinstance(level, str)
            or level not in LEVELS
            or (level == projection.TASK_LEVEL) != ("task_id" in found_args)
        ):
            raise common.PARAMS_INVALID
        task_id = common.uuid_argument(found_args, "task_id") if level == projection.TASK_LEVEL else None
        scope = read_scope(call.context, call.session)
        if level == projection.ORG_LEVEL:
            scope_id = scope.org_id
        else:
            scope.require_workgroup()
            scope_id = scope.wg_id
        if task_id is not None:
            await common.known_task(call.context, scope, task_id)
        async with common.store(call.context) as (conn, key):
            found = await projection.entries(conn, key, level=level, scope_id=scope_id, task_id=task_id)
        target = task_id or scope_id
        return common.ok({"entries": found}, len(found), target_kind=level, target_id=target)

    return await common.answered(read)


TOOL = get_whiteboard
