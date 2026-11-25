"""`get_whiteboard {level, task_id?, workgroup_id?}` (03-api.md section 8.1, ADR-087): the live entries of the org
whiteboard (organization level), of the session's workgroup or of another workgroup the session may read
(`workgroup_id`, level `workgroup`), or of a task of a readable workgroup (level `task`; the hub's task row names its
workgroup), newest first, from the kept verified whiteboard events sealed in that scope (`projection.entries`).
`task_id` is required exactly for level `task` and `workgroup_id` is allowed only for level `workgroup` (the tool
schema cannot say so, contract change GST-07; the guest checks it too). A task of any other workgroup is `not_found`.
"""

from __future__ import annotations

from agent_runner.queries import common, projection
from agent_runner.subagents.extensions import QueryCall, QueryOutcome

LEVELS = frozenset({projection.ORG_LEVEL, projection.WG_LEVEL, projection.TASK_LEVEL})


async def get_whiteboard(call: QueryCall) -> QueryOutcome:
    async def read() -> QueryOutcome:
        allowed = frozenset({"level", "task_id", common.WORKGROUP_ID})
        found_args = common.arguments(call, allowed, frozenset({"level"}))
        level = found_args["level"]
        if (
            not isinstance(level, str)
            or level not in LEVELS
            or (level == projection.TASK_LEVEL) != ("task_id" in found_args)
            or (common.WORKGROUP_ID in found_args and level != projection.WG_LEVEL)
        ):
            raise common.PARAMS_INVALID
        task_id = common.uuid_argument(found_args, "task_id") if level == projection.TASK_LEVEL else None
        scope = common.scope_of(call)
        read_wg_id: str | None = None
        if level == projection.ORG_LEVEL:
            scope_id = scope.org_id
        else:
            scope.require_workgroup()
            if task_id is not None:
                read_wg_id = (await common.known_task(call.context, scope, task_id)).task.wg_id
            else:
                read_wg_id = await common.workgroup(call, scope, found_args)
            scope_id = read_wg_id
        async with common.store(call.context) as (conn, key):
            found = await projection.entries(conn, key, level=level, scope_id=scope_id, task_id=task_id)
        target = task_id or scope_id
        return common.ok({"entries": found}, len(found), target_kind=level, target_id=target, read_wg_id=read_wg_id)

    return await common.answered(read)


TOOL = get_whiteboard
