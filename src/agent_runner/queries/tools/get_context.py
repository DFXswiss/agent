"""`get_context {}` (03-api.md section 8.1, ADR-024): organization, workgroup, task, session, the task owner's
workgroup access and the subagent limits of the platform's settings (`subagents.limits`, ADR-088 section 2: 0 = no
limit, null = unknown while the hub sends none; for a guest before ADR-088 clamped into its former contract).

Names and the title come from the verified profile and `task.meta` events (an object without one is shown by its
ID, the title is then empty), the task state from `GET /v1/device/tasks/{task}`, the session from the local row.
Workgroup level: an owner without access to the session's workgroup is `forbidden`.

`agent` and `children` (KOR-52, ADR-095 section 3): the handles come from the task's `number` and the sessions'
`agent_path` of the same hub answer, the name from the newest verified `task.agent_name` event this device holds
(`profiles.given_name`) or else automatic from the title (`agent_name.automatic_name`); a subagent carries its main
agent's name, its parent's handle is its own path without the last ordinal. `children` are the session's own child
sessions with a path, oldest first (their ordinals are given in creation order), at most 100. A hub of an older
release sends no number, and a subagent whose row has no path cannot be named: both fields are then left out.
"""

from __future__ import annotations

from typing import Any

from agent_runner.queries import common, device_api
from agent_runner.queries.device_api import HUB_INVALID, TaskDetail
from agent_runner.queries.tools.list_tasks import title
from agent_runner.sessions import profiles
from agent_runner.sessions.agent_name import automatic_name
from agent_runner.sessions.records import LocalSession
from agent_runner.subagents import limits as subagent_limits
from agent_runner.subagents.extensions import QueryCall, QueryOutcome
from agent_runner.subagents.guest_features import current_guest

API_VERSION = 1
MAX_CHILDREN = 100


def _ordinal(path: str) -> int:
    return int(path.rsplit(".", 1)[-1])


def agent_fields(session: LocalSession, detail: TaskDetail, title: str, given: str | None) -> dict[str, Any]:
    """`{agent, children}` of `session` (module docstring), empty when the hub's answer cannot name it."""
    number = detail.task.number
    own_path = detail.agent_paths.get(session.id)
    if number is None or (session.depth > 0 and own_path is None):
        return {}
    main_handle = f"#{number}"
    name = given if given is not None else automatic_name(title)
    parent = None
    if own_path is not None:
        above = own_path.rpartition(".")[0]
        parent = {"session_id": session.parent_id, "handle": f"{main_handle}.{above}" if above else main_handle}
    children = sorted(
        (
            (_ordinal(detail.agent_paths[row["id"]]), row)
            for row in detail.sessions
            if row["parent_session_id"] == session.id and row["id"] in detail.agent_paths
        ),
        key=lambda pair: pair[0],
    )[:MAX_CHILDREN]
    return {
        "agent": {
            "kind": "main" if own_path is None else "subagent",
            "handle": main_handle if own_path is None else f"{main_handle}.{own_path}",
            "name": name,
            "name_source": "automatic" if given is None else "given",
            "main": {"handle": main_handle, "name": name},
            "parent": parent,
        },
        "children": [
            {"session_id": row["id"], "handle": f"{main_handle}.{detail.agent_paths[row['id']]}", "state": row["state"]}
            for _ordinal_of, row in children
        ],
    }


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
            given = await profiles.given_name(conn, key, scope.org_id, session.task_id, wg_id=scope.wg_id)
        limits = subagent_limits.context_limits(subagent_limits.current(call.context), current)
        task_title = await title(call, scope.wg_id, session.task_id)
        result = {
            "org": {"id": scope.org_id, "name": common.display(org_name, scope.org_id)},
            "workgroup": {
                "id": scope.wg_id,
                "name": common.display(wg_name, scope.wg_id),
                "is_default": scope.wg_id == scope.state.default_wg,
            },
            "task": {
                "id": session.task_id,
                "title": task_title,
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
            **agent_fields(session, detail, task_title, given),
        }
        return common.ok(result, 1, read_wg_id=scope.wg_id)

    return await common.answered(read)


TOOL = get_context
