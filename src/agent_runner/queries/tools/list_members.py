"""`list_members {scope: "org" | "workgroup"}` (03-api.md section 8.1, ADR-024).

- `org` (organization level): every active user (`role` admin/member) and device (`role` device) of the org;
- `workgroup` (workgroup level, the owner needs access): the users with explicit access to the session's workgroup
  (`access` read/write; default workgroup: every user writes; an admin's inherited read is not listed, sessions do
  not inherit, ADR-058 section 1) and the devices assigned to it (`access` write: they author the session events of
  the workgroup).

Platform devices whose certification has ended are left out (no readers, no authors), and so are agents (ADR-082: an
agent acts as its user and is no member of its own). Names come from the verified
`member.profile` (users) and `device.profile` (devices) events; sorted by kind (users first), then name, and cut
to the members that fit into one answer (`projection.BUDGET`; the tool has no paging).
"""

from __future__ import annotations

from typing import Any

from agent_runner.crypto.log.state import AGENT, USER
from agent_runner.queries import common, projection
from agent_runner.queries.rights import read_scope
from agent_runner.sessions import profiles
from agent_runner.subagents.extensions import QueryCall, QueryOutcome


def within_budget(members: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """The leading members whose JSON fits into the answer budget (`projection.BUDGET`)."""
    used = 0
    for index, member in enumerate(members):
        used += projection.json_size(member)
        if used > projection.BUDGET:
            return members[:index]
    return members


SCOPES = frozenset({"org", "workgroup"})
WRITE = "write"


async def list_members(call: QueryCall) -> QueryOutcome:
    async def read() -> QueryOutcome:
        found_args = common.arguments(call, frozenset({"scope"}), frozenset({"scope"}))
        level = found_args["scope"]
        if not isinstance(level, str) or level not in SCOPES:
            raise common.PARAMS_INVALID
        scope = read_scope(call.context, call.session)
        if level == "workgroup":
            scope.require_workgroup()
        state = scope.state
        members: list[dict[str, Any]] = []
        async with common.store(call.context) as (conn, key):
            for principal in state.principals.values():
                if not principal.active or principal.kind == AGENT or principal.id in scope.excluded:
                    continue
                user = principal.kind == USER
                if level == "org":
                    extra = {"role": principal.role}
                elif user:
                    access = state.explicit_access(principal.id, scope.wg_id)
                    if access is None:
                        continue
                    extra = {"access": access}
                elif state.assigned(principal.id, scope.wg_id):
                    extra = {"access": WRITE}
                else:
                    continue
                if user:
                    name = await profiles.name(conn, key, "member.profile", scope.org_id, principal.id)
                else:
                    name = await projection.device_label(conn, key, scope.org_id, principal.id)
                members.append(
                    {"id": principal.id, "kind": principal.kind, "display_name": common.display(name, principal.id)}
                    | extra
                )
        members.sort(key=lambda item: (item["kind"] != USER, item["display_name"], item["id"]))
        members = within_budget(members)
        target = scope.org_id if level == "org" else scope.wg_id
        return common.ok({"members": members}, len(members), target_kind=level, target_id=target)

    return await common.answered(read)


TOOL = list_members
