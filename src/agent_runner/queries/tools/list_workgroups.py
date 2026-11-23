"""`list_workgroups {}` (03-api.md section 8.1, ADR-024): the names of all workgroups of the organization - also of
workgroups the session cannot read otherwise - with `my_access` of the task owner (null without access). `my_access`
is the explicit access: the inherited read of an admin does not count, sessions do not inherit (ADR-058 section 1).

Organization level: the owner must be an active member. Archived workgroups are left out (they take no tasks). The
default workgroup comes first, the others by name. Names come from the verified `wg.profile` events.
"""

from __future__ import annotations

from agent_runner.queries import common
from agent_runner.queries.rights import read_scope
from agent_runner.sessions import profiles
from agent_runner.subagents.extensions import QueryCall, QueryOutcome


async def list_workgroups(call: QueryCall) -> QueryOutcome:
    async def read() -> QueryOutcome:
        common.arguments(call, frozenset())
        scope = read_scope(call.context, call.session)
        state = scope.state
        found = []
        async with common.store(call.context) as (conn, key):
            for wg in state.workgroups.values():
                if wg.archived_seq is not None:
                    continue
                name = await profiles.name(conn, key, "wg.profile", scope.org_id, wg.id)
                found.append(
                    {
                        "id": wg.id,
                        "name": common.display(name, wg.id),
                        "is_default": wg.id == state.default_wg,
                        "my_access": state.explicit_access(scope.owner_id, wg.id),
                    }
                )
        found.sort(key=lambda item: (not item["is_default"], item["name"], item["id"]))
        return common.ok({"workgroups": found}, len(found), target_kind="org", target_id=scope.org_id)

    return await common.answered(read)


TOOL = list_workgroups
