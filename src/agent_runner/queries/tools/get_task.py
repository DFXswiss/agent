"""`get_task {task_id}` (03-api.md section 8.1, ADR-089): one task of a workgroup the session may read - the hub's
task row names it - with its sessions and operations (`GET /v1/device/tasks/{task}`) and the title of its verified
`task.meta` event of that workgroup's scope. A task of any other workgroup is `not_found`, like an unknown one.
Workgroup level.
"""

from __future__ import annotations

from agent_runner.queries import common
from agent_runner.queries.tools.list_tasks import title
from agent_runner.subagents.extensions import QueryCall, QueryOutcome


async def get_task(call: QueryCall) -> QueryOutcome:
    async def read() -> QueryOutcome:
        found_args = common.arguments(call, frozenset({"task_id"}), frozenset({"task_id"}))
        task_id = common.uuid_argument(found_args, "task_id")
        scope = common.scope_of(call)
        scope.require_workgroup()
        detail = await common.known_task(call.context, scope, task_id)
        row = detail.task
        result = {
            "id": row.task_id,
            "title": await title(call, row.wg_id, row.task_id),
            "state": row.state,
            "owner_id": row.owner_id,
            "created_at": row.created_at,
            "sessions": detail.sessions,
            "operations": detail.operations,
        }
        return common.ok(result, 1, target_kind="task", target_id=task_id, read_wg_id=row.wg_id)

    return await common.answered(read)


TOOL = get_task
