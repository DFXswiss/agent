"""The runner's checks of rights profiles (ADR-094 section 3).

- `operation(db, op, session)`: before every other check of an operation, the type must be allowed by the session's
  effective profile (`rights_denied` with a sentence that names the profile, the type and who can change it). For
  `subagent.request` with the parameter `profile`, that profile must be known in the session's workgroup and within
  the session's effective profile (`rights_denied`); the checked name is returned for the registration
  (`child_profile`). Without a profile (no section from the hub, or none for the session) nothing is refused.
- `tool(conn, session, tool)`: a work VM or browser tool (`model.tool_type`) needs the type it stands for;
  the sentence of a refusal, None when allowed.
- `reach(db, session_id, parent_id)`: the reach of the session's reads (`all` without a profile); the read tools apply
  it to every workgroup and task they read (`queries.rights.ReadScope.outside_reach`); `read_profile` adds the
  profile's name (`analysis` reads other workgroups only as figures, `queries.rights.FiguresOnly`). `read_refusal(reach,
  tool, arguments)` refuses at once what reads beyond the reach as a whole: the members of the whole organization for
  reach `workgroup` and `task`, `list_tasks`, the workgroup-wide reads of ADR-097 (`WORKGROUP_WIDE`: usage, audit,
  operations and metrics of a workgroup) and the organization and workgroup whiteboards for reach `task`. The names of
  the organization's workgroups (`list_workgroups`) stay readable: they are organization metadata every member sees.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from psycopg import AsyncConnection

from agent_runner.operations.extensions import CheckFailed
from agent_runner.orgs.db import session as store_session
from agent_runner.profiles import model, state
from agent_runner.queries.rights import REACH_SENTENCE
from agent_runner.subagents.extensions import QueryOutcome, failure

CHILD_PROFILE = "child_profile"
WORKGROUP_WIDE = frozenset({"list_tasks", "get_usage", "list_audit", "list_workgroup_operations", "get_metrics"})
"""Reads over every task of a workgroup: beyond reach `task`."""


def refusal(profile: model.Profile, what: str, depth: int) -> str:
    """The sentence of a refusal by the profile."""
    who = "Ask the session that started you." if depth > 0 else "A person can change the task's profile."
    return f"This session works with the profile {profile.name}, which does not allow {what}. {who}"


async def operation(db: Any, op: Any, session: Any) -> dict[str, Any]:
    """The check of an operation (module docstring); `{child_profile}` for a checked child profile, else empty."""
    async with store_session(db) as conn:
        profile = await state.effective(conn, session.id, session.parent_id)
        if profile is not None and not model.allows(profile, op.type):
            raise CheckFailed("rights_denied", message=refusal(profile, op.type, session.depth))
        name = op.params.get("profile") if op.type == "subagent.request" else None
        if not isinstance(name, str):
            return {}
        child = await state.named(conn, session.wg_id, name)
    if child is None or not model.within(child, profile):
        raise CheckFailed(
            "rights_denied",
            message=f"The profile {name} is not known in this workgroup or allows more than this session may do.",
        )
    return {CHILD_PROFILE: name}


async def tool(conn: AsyncConnection[Any], session: Any, name: str) -> str | None:
    """The refusal of a work VM or browser tool (module docstring), None when allowed."""
    needed = model.tool_type(name)
    if needed is None:
        return None
    profile = await state.effective(conn, session.id, session.parent_id)
    if profile is None or model.allows(profile, needed):
        return None
    return refusal(profile, f"{name} (it needs {needed})", session.depth)


async def reach(db: Any, session_id: str, parent_id: str | None) -> str:
    return (await read_profile(db, session_id, parent_id))[0]


async def read_profile(db: Any, session_id: str, parent_id: str | None) -> tuple[str, str | None]:
    """The reach of the session's reads and the name of its effective profile (None without one)."""
    async with store_session(db) as conn:
        profile = await state.effective(conn, session_id, parent_id)
    return model.reach(profile), None if profile is None else profile.name


def read_refusal(reach: str, tool: str, arguments: Mapping[str, Any]) -> QueryOutcome | None:
    """The refusal of a read beyond the own task as a whole (module docstring), None otherwise."""
    if reach == "all":
        return None
    if tool == "list_members" and arguments.get("scope") == "org":
        return failure("forbidden", REACH_SENTENCE.format(reach=reach))
    if reach != "task":
        return None
    if tool in WORKGROUP_WIDE or (tool == "get_whiteboard" and arguments.get("level") != "task"):
        return failure("forbidden", REACH_SENTENCE.format(reach="task"))
    return None
