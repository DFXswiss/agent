"""`list_workgroups {}` (03-api.md section 8.1, ADR-087): the names of all workgroups of the organization - also of
workgroups the session cannot read (names are organization metadata every member sees) - with `my_access`, the
**session's** access: the task owner's explicit `read`/`write` for the session's own workgroup, `read` for another
workgroup the session may read (`ReadScope.readable`: owner and instructing human, served by this device), null
otherwise (no new field, so older guests keep validating the result).

Organization level: the owner must be an active member. Archived workgroups are left out (they take no tasks; one is
still readable by its ID). The default workgroup comes first, the others by name. Names come from the verified
`wg.profile` events.
"""

from __future__ import annotations

from agent_runner.queries import common
from agent_runner.queries.rights import ReadRefused, ReadScope
from agent_runner.sessions import profiles
from agent_runner.subagents.extensions import QueryCall, QueryOutcome


def my_access(scope: ReadScope, wg_id: str) -> str | None:
    """The session's access to the workgroup, None when it may not read it."""
    try:
        return scope.readable(wg_id)
    except ReadRefused:
        return None


async def list_workgroups(call: QueryCall) -> QueryOutcome:
    async def read() -> QueryOutcome:
        common.arguments(call, frozenset())
        scope = common.scope_of(call)
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
                        "my_access": my_access(scope, wg.id),
                    }
                )
        found.sort(key=lambda item: (not item["is_default"], item["name"], item["id"]))
        return common.ok({"workgroups": found}, len(found), target_kind="org", target_id=scope.org_id)

    return await common.answered(read)


TOOL = list_workgroups
