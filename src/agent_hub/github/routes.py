"""Routes of the GitHub identities area (03-api.md sections 4.6, 4.8 and 6.1; ADR-078).

| Route | Right |
|---|---|
| `GET /v1/me/github-identities` | `user`: own and company identities, defaults (`agent_hub.github.identities`) |
| `GET /v1/github-identities/{id}` | `user`: a visible identity record |
| `PUT /v1/github-identities/{id}` | `user`: an own record, or a company record by an active admin of the org |
| `DELETE /v1/github-identities/{id}` | `user`: an own record, or a company record by an active admin of the org |
| `PUT /v1/github-identities/{id}/primary` | `user`: an own record becomes the primary one |
| `PUT /v1/github-defaults/{scope}/{scope_id}` | `user`: org admins; for a workgroup also its writers |
| `PUT /v1/tasks/{task}/github-identity` | `wg:write` on the task's workgroup |
| `GET /v1/device/github-identities?owner_id=` | `device_eligible`: the metadata of every record of the owner |
| `GET /v1/device/github-identities/{id}` | `device_eligible`: the device holds the owner's credential key |
| `PUT /v1/device/github-identities/{id}` | `device_eligible` and answering an active `github.connect` of the owner |

Writes of a record follow `agent_hub.credentials.records.put_identity`: a personal record (`owner_id` = the caller)
is signed by the caller's identity and sealed under the caller's current `CK` epoch; a company record (`owner_id` = an
organization the caller is an active admin of; `403 forbidden` for anybody else) is signed by the admin and sealed
under a fresh epoch of the org credential key that the request creates (`key`). A device writes a user's record only
(a company record or an organization owner is `403 forbidden`) while answering an active `github.connect` of that user
that names no organization (`agent_hub.requests.store.authorizes_write`). Reads, deletes and the primary mark answer
`404 not_found` for an identity the caller cannot see; a visible company record that only an admin may change is
`403 forbidden` for a member, the primary mark of a company record as well. A device read of a user's record needs
the device to be eligible for the user's `CK`, of a company record to be a reader device of the org credential key;
the device list of an owner's records (personal and primary alike, newest change first) has the same eligibility;
anything else, a malformed or a missing `owner_id` included, is `404 not_found`. Only the list clears the device's
change notice of `(owner, github)` (up to the time it read): it shows every record of the owner, so the device sees
each changed or deleted one; a read of one record leaves the notice, which may stand for other records too.

The task choice (`PUT /v1/tasks/{task}/github-identity`, also `github_identity` of `POST /v1/tasks`) must be usable
by the caller (`403 identity_forbidden`) and passes the feature gate unless it is cleared; it appends
`task.github_identity`, sends the `task` frame and the hint `reconcile {}` to the devices of the task's live
sessions. A changed default sends `reconcile {}` to the reader devices of the organization. After a committed record
change the noticed devices get `credentials {owner_id}`, the recipients of a new org credential key epoch `wraps {}`.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from typing import Annotated, Any, cast
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from agent_hub.agents.notify import account_change
from agent_hub.audit.recorder import unchanged
from agent_hub.credentials import notices, records
from agent_hub.credentials.eligibility import holds, is_eligible, org_device_ids
from agent_hub.device.keys import ActiveDevice, device_id
from agent_hub.errors import HubError
from agent_hub.github import identities
from agent_hub.http.context import Context, CurrentPrincipal, HubContext
from agent_hub.http.routing import EVENT_BATCH_BYTES, HubRouter
from agent_hub.notifications.rules import Notification, publish_rows
from agent_hub.orgs import rights
from agent_hub.orgs.routes import parse_id, user_id
from agent_hub.requests.store import authorizes_write, request_org
from agent_hub.sessions import rows
from agent_hub.sessions.effects import Effects
from agent_hub.sessions.taskstate import Actor, audit
from agent_hub.stream.publish import Audience, hint, publish
from agent_hub.tasks.service import task_for_write

router = HubRouter()
MAX_SAFE_INTEGER = 2**53 - 1
UUID7 = r"^[0-9a-f]{8}-[0-9a-f]{4}-7[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$"
Uuid = Annotated[str, Field(pattern=UUID7)]
Version = Annotated[int, Field(strict=True, ge=0, le=MAX_SAFE_INTEGER)]
GITHUB = records.GITHUB


class KeyBody(BaseModel):
    """`key` of a company record write: the new epoch of the org credential key and its wraps."""

    model_config = ConfigDict(extra="forbid")
    epoch: Annotated[int, Field(strict=True, ge=1, le=2**31 - 1)]
    wraps: list[dict[str, Any]] = Field(min_length=1, max_length=1000)


class PutBody(BaseModel):
    """Body of `PUT /v1/github-identities/{id}` (the record form is checked by `records`)."""

    model_config = ConfigDict(extra="forbid")
    owner_id: Uuid
    record: dict[str, Any]
    expected_version: Version
    key: KeyBody | None = None


class DevicePutBody(BaseModel):
    """Body of `PUT /v1/device/github-identities/{id}`."""

    model_config = ConfigDict(extra="forbid")
    owner_id: Uuid
    record: dict[str, Any]
    expected_version: Version
    request_id: Uuid


class EmptyBody(BaseModel):
    """An empty body."""

    model_config = ConfigDict(extra="forbid")


class ChoiceBody(BaseModel):
    """Body of `PUT /v1/github-defaults/{scope}/{scope_id}` and `PUT /v1/tasks/{task}/github-identity`."""

    model_config = ConfigDict(extra="forbid")
    identity_id: Uuid | None


async def _in_transaction[T](context: HubContext, work: Callable[[Any], T]) -> T:
    def run() -> T:
        with context.pool.connection() as connection, connection.transaction():
            return work(connection)

    return await asyncio.to_thread(run)


def _path(text: str, *, write: bool = False) -> UUID:
    found = parse_id(text)
    if found is None:
        raise HubError("forbidden" if write else "not_found")
    return found


def _author(connection: Any, user: UUID) -> records.Author:
    key = records.identity_key(connection, user)
    if key is None:
        raise HubError("forbidden")
    return records.Author(id=user, kind="user", sig_public=key)


def _owner(connection: Any, owner_id: UUID, user: UUID) -> records.Owner:
    """The owner of a browser write: the caller itself, or an organization the caller is an active admin of."""
    if owner_id == user:
        return records.Owner(user, records.USER)
    if not rights.can(connection, user, rights.ORG_ADMIN, org_id=owner_id):
        raise HubError("forbidden")
    return records.Owner(owner_id, records.ORG)


def _after_change(change: records.Change, owner: UUID, account: list[Notification]) -> None:
    notices.hint_devices(change.devices, owner)
    for recipient in sorted(change.recipients):
        publish(Audience("device", UUID(recipient)), "wraps", {})
    publish_rows(account)


@router.get("/v1/me/github-identities", auth="browser", right="user", read_resource="me.github_identities")
async def get_identities(context: Context, principal: CurrentPrincipal) -> dict[str, Any]:
    user = user_id(principal)
    return await _in_transaction(context, lambda c: identities.list_for(c, user))


@router.get("/v1/github-identities/{id}", auth="browser", right="user", read_resource="github_identity")
async def get_identity(context: Context, principal: CurrentPrincipal, id: str) -> dict[str, Any]:
    user, record_id = user_id(principal), _path(id)
    return await _in_transaction(context, lambda c: identities.find_visible(c, record_id, user).answer())


@router.put("/v1/github-identities/{id}", auth="browser", right="user", max_body=EVENT_BATCH_BYTES)
async def put_identity(context: Context, principal: CurrentPrincipal, id: str, body: PutBody) -> dict[str, Any]:
    user, record_id, now = user_id(principal), _path(id), context.clock()
    key = None if body.key is None else records.OrgKey(body.key.epoch, body.key.wraps)

    def work(connection: Any) -> tuple[records.Change, records.Owner, list[Notification]]:
        owner = _owner(connection, UUID(body.owner_id), user)
        change = records.put_identity(
            connection,
            owner=owner,
            record_id=record_id,
            record=body.record,
            expected_version=body.expected_version,
            author=_author(connection, user),
            now=now,
            key=key,
            root_key=context.config.platform_root_key,
        )
        return change, owner, account_change(connection, user, "credential_changed", now, target=GITHUB)

    change, owner, account = await _in_transaction(context, work)
    _after_change(change, owner.id, account)
    return {"version": change.version}


def _changeable(connection: Any, record_id: UUID, user: UUID) -> records.Identity:
    """A visible identity the caller may change: an own record or, by an active admin, a company record."""
    identity = identities.find_visible(connection, record_id, user)
    if identity.owner.kind == records.ORG and not rights.can(
        connection, user, rights.ORG_ADMIN, org_id=identity.owner.id
    ):
        raise HubError("forbidden")
    return identity


@router.delete("/v1/github-identities/{id}", auth="browser", right="user")
async def delete_identity(context: Context, principal: CurrentPrincipal, id: str) -> dict[str, Any]:
    user, record_id, now = user_id(principal), _path(id), context.clock()

    def work(connection: Any) -> tuple[records.Change, records.Identity, list[Notification]]:
        identity = _changeable(connection, record_id, user)
        actor = records.Author(id=user, kind="user", sig_public=b"")
        change = records.delete_identity(
            connection, identity, actor=actor, now=now, root_key=context.config.platform_root_key
        )
        return change, identity, account_change(connection, user, "credential_changed", now, target=GITHUB)

    change, identity, account = await _in_transaction(context, work)
    _after_change(change, identity.owner.id, account)
    return {}


@router.put("/v1/github-identities/{id}/primary", auth="browser", right="user")
async def put_primary(context: Context, principal: CurrentPrincipal, id: str, body: EmptyBody) -> dict[str, Any]:
    user, record_id, now = user_id(principal), _path(id), context.clock()

    def work(connection: Any) -> tuple[records.Change, list[Notification]]:
        identity = identities.find_visible(connection, record_id, user)
        if identity.owner.kind != records.USER:
            raise HubError("forbidden")
        actor = records.Author(id=user, kind="user", sig_public=b"")
        change = records.make_primary(
            connection, identity, actor=actor, now=now, root_key=context.config.platform_root_key
        )
        return change, account_change(connection, user, "credential_changed", now, target=GITHUB)

    change, account = await _in_transaction(context, work)
    _after_change(change, user, account)
    return {}


@router.put("/v1/github-defaults/{scope}/{scope_id}", auth="browser", right="user")
async def put_default(
    context: Context, principal: CurrentPrincipal, scope: str, scope_id: str, body: ChoiceBody
) -> dict[str, Any]:
    user, now, root_key = user_id(principal), context.clock(), context.config.platform_root_key
    if scope not in identities.SCOPES:
        raise HubError("not_found")
    found_scope = _path(scope_id)
    identity = None if body.identity_id is None else UUID(body.identity_id)

    def work(connection: Any) -> list[UUID]:
        org = identities.set_default(
            connection,
            scope=scope,
            scope_id=found_scope,
            identity_id=identity,
            user=user,
            now=now,
            root_key=root_key,
        )
        return [] if org is None else sorted(org_device_ids(connection, org, root_key), key=str)

    for device in await _in_transaction(context, work):
        hint(device, "reconcile", {})
    return {}


def set_task_identity(
    connection: Any, user: UUID, task_id: UUID, identity: UUID | None, *, now: Any, root_key: bytes
) -> Effects:
    """`PUT /v1/tasks/{task}/github-identity` in the caller's transaction (module docstring); the effects."""
    task = task_for_write(connection, user, task_id)
    effects = Effects()
    if identity is not None:
        identities.require_usable(connection, identity, user)
        identities.require_feature(connection, task.org_id, root_key)
    current = connection.execute("SELECT github_identity FROM hub.task WHERE id = %s", (task.id,)).fetchone()
    if current[0] == identity:
        unchanged()
        return effects
    connection.execute("UPDATE hub.task SET github_identity = %s WHERE id = %s", (identity, task.id))
    audit(
        connection,
        Actor("user", user),
        "task.github_identity",
        task,
        now,
        detail={"identity_id": None if identity is None else str(identity)},
    )
    effects.org_frame(task.org_id, "task", cast(dict[str, Any], rows.task_json(connection, task.id)))
    for (device,) in connection.execute(
        "SELECT DISTINCT device_id FROM hub.session WHERE task_id = %s AND device_id IS NOT NULL"
        " AND state NOT IN ('stopped', 'failed') ORDER BY device_id",
        (task.id,),
    ):
        effects.hint(device, "reconcile", {})
    return effects


@router.put("/v1/tasks/{task}/github-identity", auth="browser", right="wg:write")
async def put_task_identity(
    context: Context, principal: CurrentPrincipal, task: str, body: ChoiceBody
) -> dict[str, Any]:
    user, task_id, now = user_id(principal), _path(task), context.clock()
    root_key = context.config.platform_root_key
    identity = None if body.identity_id is None else UUID(body.identity_id)
    effects = await _in_transaction(
        context, lambda c: set_task_identity(c, user, task_id, identity, now=now, root_key=root_key)
    )
    effects.flush()
    return {}


@router.get(
    "/v1/device/github-identities",
    auth="device",
    right="device_eligible",
    read_resource="device.github_identities",
)
async def list_device_identities(
    context: Context, principal: ActiveDevice, *, owner_id: str | None = None
) -> dict[str, Any]:
    device, owner, now = device_id(principal), _path(owner_id or ""), context.clock()

    def work(connection: Any) -> dict[str, Any]:
        if not holds(connection, device, owner, context.config.platform_root_key):
            raise HubError("not_found")
        notices.clear(connection, device, owner, GITHUB, now)
        return {"identities": records.owner_identities(connection, owner)}

    return await _in_transaction(context, work)


@router.get(
    "/v1/device/github-identities/{id}",
    auth="device",
    right="device_eligible",
    read_resource="device.github_identity",
)
async def get_device_identity(context: Context, principal: ActiveDevice, id: str) -> dict[str, Any]:
    device, record_id = device_id(principal), _path(id)

    def work(connection: Any) -> dict[str, Any]:
        identity = records.load_identity(connection, record_id)
        if identity is None or not holds(connection, device, identity.owner.id, context.config.platform_root_key):
            raise HubError("not_found")
        return identity.answer()

    return await _in_transaction(context, work)


@router.put("/v1/device/github-identities/{id}", auth="device", right="device_eligible")
async def put_device_identity(
    context: Context, principal: ActiveDevice, id: str, body: DevicePutBody
) -> dict[str, Any]:
    device, record_id, now = device_id(principal), _path(id, write=True), context.clock()
    owner_id = UUID(body.owner_id)

    def work(connection: Any) -> records.Change:
        request_id = UUID(body.request_id)
        if (
            not is_eligible(connection, device, owner_id)
            or not authorizes_write(connection, device, owner_id, request_id=request_id, kind=GITHUB, now=now)
            or request_org(connection, request_id) is not None
            or _company(connection, record_id)
        ):
            raise HubError("forbidden")
        author = records.Author(id=device, kind="device", sig_public=records.device_key(connection, device))
        return records.put_identity(
            connection,
            owner=records.Owner(owner_id, records.USER),
            record_id=record_id,
            record=body.record,
            expected_version=body.expected_version,
            author=author,
            now=now,
            root_key=context.config.platform_root_key,
        )

    change = await _in_transaction(context, work)
    notices.hint_devices(change.devices, owner_id)
    return {"version": change.version}


def _company(connection: Any, record_id: UUID) -> bool:
    identity = records.load_identity(connection, record_id)
    return identity is not None and identity.owner.kind == records.ORG
