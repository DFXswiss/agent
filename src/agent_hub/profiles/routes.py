"""Routes of the rights profiles (ADR-094 sections 1, 2 and 7; 03-api.md section 4.10).

| Route | Right |
|---|---|
| `GET /v1/workgroups/{wg}/agent-profiles` | `wg:read`: every profile usable in the workgroup, the type classes |
| `PUT /v1/workgroups/{wg}/agent-profiles/{name}` | `wg:write`: create or change an own profile of the workgroup |
| `DELETE /v1/workgroups/{wg}/agent-profiles/{name}` | `wg:write` |
| `PUT /v1/orgs/{org}/agent-profiles/{name}` | `org:admin`: create or change an own profile of the organization |
| `DELETE /v1/orgs/{org}/agent-profiles/{name}` | `org:admin` |
| `PUT /v1/tasks/{task}/agent-profile` | `wg:write` on the task's workgroup: assign (`profile` name) or clear (`null`) |

A definition names known operation types only (`400 request_invalid` with the pointers of the others) and never a
built-in name (`400 request_invalid` `/name`). Assigning resolves the name in the task's workgroup (unknown: `404
not_found`) and stores the snapshot; the task row, its `task` frame and the device state carry it, the devices of the
task's live sessions get `reconcile {}`. A changed definition reaches the organization's devices with `reconcile {}`
(the runner resolves the profile of a child from the device state; the hub decides bindingly anyway).
Assigning a profile (never clearing one) passes the feature gate `agent.profiles` of the organization's devices
(`409 peer_outdated`): only the runner can check reads and the work VM and browser tools.
Every change is audited (`agent_profile.set`, `agent_profile.delete`, `task.agent_profile`); a repetition without
change is not. The profile of an analysis task (ADR-095 section 2) is never widened or cleared: only a profile within
its snapshot is assigned, anything else is `403 forbidden` with detail `{"reason": "analysis"}`. The narrower snapshot
keeps the name `analysis` (its types and reach are the chosen profile's), so the marker of the analysis holds.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from datetime import datetime
from typing import Annotated, Any, Literal, cast
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from agent_hub.audit.chain import AuditRow, append_audit
from agent_hub.audit.recorder import unchanged
from agent_hub.credentials.eligibility import org_device_ids
from agent_hub.device.features import AGENT_PROFILES, outdated_devices
from agent_hub.errors import HubError
from agent_hub.github.identities import MAX_DETAIL_DEVICES
from agent_hub.http.context import Context, CurrentPrincipal, HubContext
from agent_hub.http.routing import HubRouter
from agent_hub.orgs import rights
from agent_hub.orgs.routes import user_id
from agent_hub.profiles import model, store
from agent_hub.profiles.model import CLASSES, SCOPE_ORG, SCOPE_WG, Profile, invalid_operations
from agent_hub.sessions import rows
from agent_hub.sessions.effects import Effects
from agent_hub.sessions.taskstate import Actor, audit
from agent_hub.stream.publish import hint
from agent_hub.tasks.service import task_for_write
from agent_hub.workgroups.routes import path_id

router = HubRouter()
OperationType = Annotated[str, Field(pattern=r"^[a-z][a-z_]*(\.[a-z][a-z_]*)+$", max_length=64)]
ProfileName = Annotated[str, Field(pattern=r"^[a-z][a-z0-9_-]{0,39}$")]


class DefinitionBody(BaseModel):
    """Body of `PUT …/agent-profiles/{name}`."""

    model_config = ConfigDict(extra="forbid")
    operations: list[OperationType] = Field(max_length=256)
    reads: Literal["all", "workgroup", "task"]


class AssignBody(BaseModel):
    """Body of `PUT /v1/tasks/{task}/agent-profile`."""

    model_config = ConfigDict(extra="forbid")
    profile: ProfileName | None


async def _in_transaction[T](context: HubContext, work: Callable[[Any], T]) -> T:
    def run() -> T:
        with context.pool.connection() as connection, connection.transaction():
            return work(connection)

    return await asyncio.to_thread(run)


def _wg_org(connection: Any, wg: UUID) -> UUID:
    return cast(tuple[UUID], connection.execute("SELECT org_id FROM hub.wg WHERE id = %s", (wg,)).fetchone())[0]


def _profile(name: str, body: DefinitionBody) -> Profile:
    store.check_name(name)
    wrong = invalid_operations(body.operations)
    if wrong:
        raise HubError("request_invalid", detail={"pointers": [f"/operations/{index}" for index in wrong]})
    return Profile(name, frozenset(body.operations), body.reads)


def _audit_definition(
    connection: Any,
    *,
    action: str,
    scope: str,
    scope_id: UUID,
    org: UUID,
    user: UUID,
    now: datetime,
    detail: dict[str, Any],
) -> None:
    append_audit(
        connection,
        AuditRow(
            at=now,
            actor_kind="user",
            actor_id=user,
            action=action,
            result="ok",
            org_id=org,
            wg_id=scope_id if scope == SCOPE_WG else None,
            target_kind="org" if scope == SCOPE_ORG else "workgroup",
            target_id=str(scope_id),
            detail={"scope": scope, **detail},
        ),
    )


def _put_definition(
    connection: Any, *, scope: str, scope_id: UUID, org: UUID, profile: Profile, user: UUID, now: datetime
) -> bool:
    changed = store.put_definition(
        connection, scope=scope, scope_id=scope_id, org_id=org, profile=profile, user=user, now=now
    )
    if not changed:
        unchanged()
        return False
    detail = {"profile": profile.name, "reads": profile.reads, "operations": len(profile.operations)}
    _audit_definition(
        connection,
        action="agent_profile.set",
        scope=scope,
        scope_id=scope_id,
        org=org,
        user=user,
        now=now,
        detail=detail,
    )
    return True


def _delete_definition(
    connection: Any, *, scope: str, scope_id: UUID, org: UUID, name: str, user: UUID, now: datetime
) -> None:
    store.check_name(name)
    if not store.delete_definition(connection, scope=scope, scope_id=scope_id, name=name):
        raise HubError("not_found")
    _audit_definition(
        connection,
        action="agent_profile.delete",
        scope=scope,
        scope_id=scope_id,
        org=org,
        user=user,
        now=now,
        detail={"profile": name},
    )


def _reconcile_org(context: HubContext, connection: Any, org: UUID) -> list[UUID]:
    return sorted(org_device_ids(connection, org, context.config.platform_root_key), key=str)


@router.get(
    "/v1/workgroups/{wg}/agent-profiles", auth="browser", right="wg:read", read_resource="workgroup.agent_profiles"
)
async def list_profiles(context: Context, principal: CurrentPrincipal, wg: str) -> dict[str, Any]:
    user, wg_id = user_id(principal), path_id(wg)

    def work(connection: Any) -> dict[str, Any]:
        rights.require(connection, user, rights.WG_READ, wg_id=wg_id)
        return {"profiles": store.listing(connection, _wg_org(connection, wg_id), wg_id), "classes": dict(CLASSES)}

    return await _in_transaction(context, work)


@router.put("/v1/workgroups/{wg}/agent-profiles/{name}", auth="browser", right="wg:write")
async def put_wg_profile(
    context: Context, principal: CurrentPrincipal, wg: str, name: str, body: DefinitionBody
) -> dict[str, Any]:
    user, wg_id, now = user_id(principal), path_id(wg), context.clock()

    def work(connection: Any) -> list[UUID]:
        rights.require(connection, user, rights.WG_WRITE, wg_id=wg_id, write=True)
        org = _wg_org(connection, wg_id)
        changed = _put_definition(
            connection, scope=SCOPE_WG, scope_id=wg_id, org=org, profile=_profile(name, body), user=user, now=now
        )
        return _reconcile_org(context, connection, org) if changed else []

    for device in await _in_transaction(context, work):
        hint(device, "reconcile", {})
    return {}


@router.delete("/v1/workgroups/{wg}/agent-profiles/{name}", auth="browser", right="wg:write")
async def delete_wg_profile(context: Context, principal: CurrentPrincipal, wg: str, name: str) -> dict[str, Any]:
    user, wg_id, now = user_id(principal), path_id(wg), context.clock()

    def work(connection: Any) -> list[UUID]:
        rights.require(connection, user, rights.WG_WRITE, wg_id=wg_id, write=True)
        org = _wg_org(connection, wg_id)
        _delete_definition(connection, scope=SCOPE_WG, scope_id=wg_id, org=org, name=name, user=user, now=now)
        return _reconcile_org(context, connection, org)

    for device in await _in_transaction(context, work):
        hint(device, "reconcile", {})
    return {}


@router.put("/v1/orgs/{org}/agent-profiles/{name}", auth="browser", right="org:admin")
async def put_org_profile(
    context: Context, principal: CurrentPrincipal, org: str, name: str, body: DefinitionBody
) -> dict[str, Any]:
    user, org_id, now = user_id(principal), path_id(org), context.clock()

    def work(connection: Any) -> list[UUID]:
        rights.require(connection, user, rights.ORG_ADMIN, org_id=org_id, write=True)
        changed = _put_definition(
            connection, scope=SCOPE_ORG, scope_id=org_id, org=org_id, profile=_profile(name, body), user=user, now=now
        )
        return _reconcile_org(context, connection, org_id) if changed else []

    for device in await _in_transaction(context, work):
        hint(device, "reconcile", {})
    return {}


@router.delete("/v1/orgs/{org}/agent-profiles/{name}", auth="browser", right="org:admin")
async def delete_org_profile(context: Context, principal: CurrentPrincipal, org: str, name: str) -> dict[str, Any]:
    user, org_id, now = user_id(principal), path_id(org), context.clock()

    def work(connection: Any) -> list[UUID]:
        rights.require(connection, user, rights.ORG_ADMIN, org_id=org_id, write=True)
        _delete_definition(connection, scope=SCOPE_ORG, scope_id=org_id, org=org_id, name=name, user=user, now=now)
        return _reconcile_org(context, connection, org_id)

    for device in await _in_transaction(context, work):
        hint(device, "reconcile", {})
    return {}


def require_feature(connection: Any, org_id: UUID, root_key: bytes) -> None:
    """The feature gate (module docstring): `409 peer_outdated` with the outdated devices."""
    devices = [str(device) for device in org_device_ids(connection, org_id, root_key)]
    outdated = outdated_devices(connection, devices, AGENT_PROFILES)
    if outdated:
        raise HubError("peer_outdated", detail={"devices": outdated[:MAX_DETAIL_DEVICES]})


def set_task_profile(
    connection: Any, user: UUID, task_id: UUID, name: str | None, *, now: datetime, root_key: bytes
) -> tuple[Effects, Any]:
    """`PUT /v1/tasks/{task}/agent-profile` in the caller's transaction (module docstring); effects and snapshot."""
    task = task_for_write(connection, user, task_id)
    effects = Effects()
    profile = None
    if name is not None:
        profile = store.resolve(connection, task.org_id, task.wg_id, name)
        if profile is None:
            raise HubError("not_found")
        require_feature(connection, task.org_id, root_key)
    current = connection.execute("SELECT agent_profile, analysis FROM hub.task WHERE id = %s", (task.id,)).fetchone()
    if current[1] is not None:
        if profile is None or not model.within(profile, model.from_json(current[0])):
            # an analysis task always reads only: only a narrower profile (ADR-095 section 2)
            raise HubError("forbidden", detail={"reason": "analysis"})
        profile = model.Profile(model.ANALYSIS, profile.operations, profile.reads)  # it stays marked as one
    wanted = None if profile is None else profile.as_json()
    if current[0] == wanted:
        unchanged()
        return effects, wanted
    connection.execute("UPDATE hub.task SET agent_profile = %s WHERE id = %s", (store.snapshot(profile), task.id))
    audit(
        connection,
        Actor("user", user),
        "task.agent_profile",
        task,
        now,
        detail={"profile": name, "reads": None if profile is None else profile.reads},
    )
    effects.org_frame(task.org_id, "task", cast(dict[str, Any], rows.task_json(connection, task.id)))
    for (device,) in connection.execute(
        "SELECT DISTINCT device_id FROM hub.session WHERE task_id = %s AND device_id IS NOT NULL"
        " AND state NOT IN ('stopped', 'failed') ORDER BY device_id",
        (task.id,),
    ):
        effects.hint(device, "reconcile", {})
    return effects, wanted


@router.put("/v1/tasks/{task}/agent-profile", auth="browser", right="wg:write")
async def put_task_profile(
    context: Context, principal: CurrentPrincipal, task: str, body: AssignBody
) -> dict[str, Any]:
    user, task_id, now = user_id(principal), path_id(task), context.clock()
    root_key = context.config.platform_root_key
    effects, snapshot = await _in_transaction(
        context, lambda c: set_task_profile(c, user, task_id, body.profile, now=now, root_key=root_key)
    )
    effects.flush()
    return {"profile": snapshot}
