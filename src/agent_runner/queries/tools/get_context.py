"""`get_context {}` (03-api.md section 8.1, ADR-024): organization, workgroup, task, session, the task owner's
workgroup access and the subagent limits of this session.

Names and the title come from the verified profile and `task.meta` events (an object without one is shown by its
ID, the title is then empty), the task state from `GET /v1/device/tasks/{task}`, the session from the local row.
Workgroup level: an owner without access to the session's workgroup is `forbidden`.
"""

from __future__ import annotations

from agent_runner.queries import common, device_api
from agent_runner.queries.device_api import HUB_INVALID
from agent_runner.queries.rights import read_scope
from agent_runner.queries.tools.list_tasks import title
from agent_runner.sessions import profiles
from agent_runner.sessions.harness import SUBAGENT_LIMITS
from agent_runner.subagents.extensions import QueryCall, QueryOutcome

API_VERSION = 1


async def get_context(call: QueryCall) -> QueryOutcome:
    async def read() -> QueryOutcome:
        common.arguments(call, frozenset())
        session = call.session
        scope = read_scope(call.context, session)
        access = scope.require_workgroup()
        detail = await device_api.task(call.context, scope, session.task_id)
        if detail is None:
            raise HUB_INVALID
        async with common.store(call.context) as (conn, key):
            org_name = await profiles.name(conn, key, "org.profile", scope.org_id, scope.org_id)
            wg_name = await profiles.name(conn, key, "wg.profile", scope.org_id, scope.wg_id)
            owner_name = await profiles.name(conn, key, "member.profile", scope.org_id, scope.owner_id)
        depth, parallel, total = SUBAGENT_LIMITS
        result = {
            "org": {"id": scope.org_id, "name": common.display(org_name, scope.org_id)},
            "workgroup": {
                "id": scope.wg_id,
                "name": common.display(wg_name, scope.wg_id),
                "is_default": scope.wg_id == scope.state.default_wg,
            },
            "task": {
                "id": session.task_id,
                "title": await title(call, scope.wg_id, session.task_id),
                "state": detail.task.state,
                "owner": {"id": scope.owner_id, "display_name": common.display(owner_name, scope.owner_id)},
            },
            "session": {
                "id": session.id,
                "parent_session_id": session.parent_id,
                "depth": session.depth,
                "model_id": session.model_id,
            },
            "rights": {"workgroup": access},
            "limits": {"subagent_max_depth": depth, "subagent_max_parallel": parallel, "subagent_max_total": total},
            "api_version": API_VERSION,
        }
        return common.ok(result, 1)

    return await common.answered(read)


TOOL = get_context
