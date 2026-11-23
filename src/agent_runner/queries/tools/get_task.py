"""`get_task {task_id}` (03-api.md section 8.1, ADR-024): one task of the session's workgroup with its sessions and
operations (`GET /v1/device/tasks/{task}`) and the title of its verified `task.meta` event. A task of another
workgroup - also one this device serves - is `not_found`, like an unknown one. Workgroup level.
"""

from __future__ import annotations

from agent_runner.queries import common, device_api
from agent_runner.queries.rights import read_scope
from agent_runner.queries.tools.list_tasks import title
from agent_runner.subagents.extensions import QueryCall, QueryOutcome


async def get_task(call: QueryCall) -> QueryOutcome:
    async def read() -> QueryOutcome:
        found_args = common.arguments(call, frozenset({"task_id"}), frozenset({"task_id"}))
        task_id = common.uuid_argument(found_args, "task_id")
        scope = read_scope(call.context, call.session)
        scope.require_workgroup()
        detail = await device_api.task(call.context, scope, task_id)
        if detail is None:
            raise common.TASK_NOT_FOUND
        row = detail.task
        result = {
            "id": row.task_id,
            "title": await title(call, scope.wg_id, row.task_id),
            "state": row.state,
            "owner_id": row.owner_id,
            "created_at": row.created_at,
            "sessions": detail.sessions,
            "operations": detail.operations,
        }
        return common.ok(result, 1, target_kind="task", target_id=task_id)

    return await common.answered(read)


TOOL = get_task
