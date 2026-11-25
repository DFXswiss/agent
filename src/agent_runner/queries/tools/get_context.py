"""`get_context {}` (03-api.md section 8.1, ADR-024): organization, workgroup, task, session, the task owner's
workgroup access and the subagent limits of the platform's settings (`subagents.limits`, ADR-088 section 2: 0 = no
limit, null = unknown while the hub sends none; for a guest before ADR-088 clamped into its former contract).

Names and the title come from the verified profile and `task.meta` events (an object without one is shown by its
ID, the title is then empty), the task state from `GET /v1/device/tasks/{task}`, the session from the local row.
Workgroup level: an owner without access to the session's workgroup is `forbidden`.
"""

from __future__ import annotations

from agent_runner.queries import common, device_api
from agent_runner.queries.device_api import HUB_INVALID
from agent_runner.queries.tools.list_tasks import title
from agent_runner.sessions import profiles
from agent_runner.subagents import limits as subagent_limits
from agent_runner.subagents.extensions import QueryCall, QueryOutcome
from agent_runner.subagents.guest_features import current_guest

API_VERSION = 1


async def get_context(call: QueryCall) -> QueryOutcome:
    async def read() -> QueryOutcome:
        common.arguments(call, frozenset())
        session = call.session
        scope = common.scope_of(call)
        access = scope.require_workgroup()
        detail = await device_api.task(call.context, scope, session.task_id)
        if detail is None:
            raise HUB_INVALID
        async with common.store(call.context) as (conn, key):
            org_name = await profiles.name(conn, key, "org.profile", scope.org_id, scope.org_id)
            wg_name = await profiles.name(conn, key, "wg.profile", scope.org_id, scope.wg_id)
            owner_name = await profiles.name(conn, key, "member.profile", scope.org_id, scope.owner_id)
            current = await current_guest(conn, session.vm_id)
        limits = subagent_limits.context_limits(subagent_limits.current(call.context), current)
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
            "limits": limits,
            "api_version": API_VERSION,
        }
        return common.ok(result, 1, read_wg_id=scope.wg_id)

    return await common.answered(read)


TOOL = get_context
