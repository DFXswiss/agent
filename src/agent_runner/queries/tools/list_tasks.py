"""`list_tasks {state?, limit? <= 100, before?, workgroup_id?}` (03-api.md section 8.1, ADR-087): the tasks of the
session's workgroup or of another workgroup the session may read (`workgroup_id`, `ReadScope.readable`), newest
first, one page from `GET /v1/device/workgroups/{wg}/tasks` (metadata) with the titles of the verified `task.meta`
events of that workgroup's scope (empty while the device holds none). Workgroup level.
"""

from __future__ import annotations

import re

from agent_runner.queries import common, device_api
from agent_runner.sessions import profiles
from agent_runner.subagents.extensions import QueryCall, QueryOutcome

DEFAULT_LIMIT, MAX_LIMIT = 50, 100
TITLE_MAX = 256
_BEFORE_RE = re.compile(r"^[A-Za-z0-9_-]{1,256}$")


async def title(call: QueryCall, wg_id: str, task_id: str) -> str:
    async with common.store(call.context) as (conn, key):
        found = await profiles.name(conn, key, "task.meta", call.session.org_id, task_id, scope_id=wg_id)
    return "" if found is None else found[:TITLE_MAX]


async def list_tasks(call: QueryCall) -> QueryOutcome:
    async def read() -> QueryOutcome:
        found_args = common.arguments(call, frozenset({"state", "limit", "before", common.WORKGROUP_ID}))
        state, before = found_args.get("state"), found_args.get("before")
        limit = common.integer(found_args, "limit", 1, MAX_LIMIT, DEFAULT_LIMIT)
        if state is not None and (not isinstance(state, str) or state not in device_api.TASK_STATES):
            raise common.PARAMS_INVALID
        if before is not None and (not isinstance(before, str) or not _BEFORE_RE.fullmatch(before)):
            raise common.PARAMS_INVALID
        scope = common.scope_of(call)
        scope.require_workgroup()
        wg_id = await common.workgroup(call, scope, found_args)
        rows, following = await device_api.tasks(call.context, scope, wg_id, state=state, before=before, limit=limit)
        listed = [
            {
                "id": row.task_id,
                "title": await title(call, wg_id, row.task_id),
                "state": row.state,
                "owner_id": row.owner_id,
                "created_at": row.created_at,
                "last_activity_at": row.last_activity_at,
            }
            for row in rows[:limit]
        ]
        result = {"tasks": listed, "next_before": following}
        return common.ok(result, len(listed), target_kind="workgroup", target_id=wg_id, read_wg_id=wg_id)

    return await common.answered(read)


TOOL = list_tasks
