"""Routes of the credentials area (03-api.md sections 4.8 and 6.1).

| Route | Right |
|---|---|
| `GET /v1/me/credentials` | `user`: metadata of the caller's records, newest change first |
| `GET/PUT/DELETE /v1/me/credentials/{kind}` | `user`: the caller's record of `kind` (author = the caller) |
| `POST /v1/me/credentials/rotate` | `user`: `CK` rotation with every record re-encrypted, atomically |
| `GET /v1/device/credentials/{owner}/{kind}` | `device_eligible`: the device is eligible for the owner's `CK` |
| `PUT /v1/device/credentials/{owner}/{kind}` | `device_eligible` and answering an active request (`request_id`) |

Of kind `github` these endpoints address the user's primary record only, and only user-owned records at all
(`agent_hub.credentials.records`, ADR-076 section 1); further and company records live behind the routes of
`agent_hub.github`. A device write needs an active device request of the owner addressed to the device whose kind
produces the credential kind (`agent_hub.requests.store.CREDENTIAL_KINDS`).

The rules live in `agent_hub.credentials.records`. A device read clears the device's change notice of the owner and
kind, also when the answer is `404` (the record was deleted; also for an organization whose credential key the
device holds: these endpoints address user records only, ADR-076, so an older runner that follows the notice of a
company record gets `404` and the notice is cleared). After a committed change the eligible devices get the hint
`credentials {owner_id}` and, after a rotation, the recipients of the new wraps the `wraps` frame/hint. Unknown kinds
and foreign owners are `404 not_found` for reads and `403 forbidden` for device writes.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from typing import Annotated, Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from agent_hub.agents.notify import account_change
from agent_hub.credentials import notices, records
from agent_hub.credentials.eligibility import holds, is_eligible
from agent_hub.device.keys import ActiveDevice, device_id
from agent_hub.errors import HubError
from agent_hub.http.context import Context, CurrentPrincipal, HubContext
from agent_hub.http.routing import EVENT_BATCH_BYTES, HubRouter
from agent_hub.notifications.rules import Notification, publish_rows
from agent_hub.orgs.routes import parse_id, user_id
from agent_hub.requests.store import authorizes_write
from agent_hub.stream.publish import Audience, AudienceKind, publish

router = HubRouter()
MAX_SAFE_INTEGER = 2**53 - 1
Version = Annotated[int, Field(strict=True, ge=0, le=MAX_SAFE_INTEGER)]


class PutBody(BaseModel):
    """Body of `PUT /v1/me/credentials/{kind}` (the record form is checked by `records`)."""

    model_config = ConfigDict(extra="forbid")
    record: dict[str, Any]
    expected_version: Version


class DevicePutBody(PutBody):
    """Body of `PUT /v1/device/credentials/{owner}/{kind}`."""

    request_id: Annotated[str, Field(pattern=r"^[0-9a-f]{8}-[0-9a-f]{4}-7[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$")]


class RotateBody(BaseModel):
    """Body of `POST /v1/me/credentials/rotate`."""

    model_config = ConfigDict(extra="forbid")
    epoch: Annotated[int, Field(strict=True, ge=1, le=2**31 - 1)]
    wraps: list[dict[str, Any]] = Field(min_length=1, max_length=1000)
    records: list[dict[str, Any]] = Field(max_length=4)


async def _in_transaction[T](context: HubContext, work: Callable[[Any], T]) -> T:
    def run() -> T:
        with context.pool.connection() as connection, connection.transaction():
            return work(connection)

    return await asyncio.to_thread(run)


def _kind(kind: str) -> str:
    if kind not in records.KINDS:
        raise HubError("not_found")
    return kind


def _user_author(connection: Any, user: UUID) -> records.Author:
    key = records.identity_key(connection, user)
    if key is None:
        raise HubError("forbidden")
    return records.Author(id=user, kind="user", sig_public=key)


@router.get("/v1/me/credentials", auth="browser", right="user", read_resource="me.credentials")
async def get_credentials(context: Context, principal: CurrentPrincipal) -> dict[str, Any]:
    user = user_id(principal)
    return {"credentials": await _in_transaction(context, lambda c: records.list_records(c, user))}


@router.get("/v1/me/credentials/{kind}", auth="browser", right="user", read_resource="me.credential")
async def get_credential(context: Context, principal: CurrentPrincipal, kind: str) -> dict[str, Any]:
    user, found_kind = user_id(principal), _kind(kind)
    record = await _in_transaction(context, lambda c: records.get_record(c, user, found_kind))
    if record is None:
        raise HubError("not_found")
    return {"record": record}


@router.put("/v1/me/credentials/{kind}", auth="browser", right="user")
async def put_credential(context: Context, principal: CurrentPrincipal, kind: str, body: PutBody) -> dict[str, Any]:
    user, found_kind, now = user_id(principal), _kind(kind), context.clock()

    def work(connection: Any) -> tuple[records.Change, list[Notification]]:
        change = records.put_record(
            connection,
            owner=user,
            kind=found_kind,
            record=body.record,
            expected_version=body.expected_version,
            author=_user_author(connection, user),
            now=now,
            root_key=context.config.platform_root_key,
        )
        return change, account_change(connection, user, "credential_changed", now, target=found_kind)

    change, account = await _in_transaction(context, work)
    notices.hint_devices(change.devices, user)
    publish_rows(account)
    return {"version": change.version}


@router.delete("/v1/me/credentials/{kind}", auth="browser", right="user")
async def delete_credential(context: Context, principal: CurrentPrincipal, kind: str) -> dict[str, Any]:
    user, found_kind, now = user_id(principal), _kind(kind), context.clock()
    owner = records.Author(id=user, kind="user", sig_public=b"")

    def work(connection: Any) -> tuple[records.Change, list[Notification]]:
        change = records.delete_record(
            connection, owner=owner, kind=found_kind, now=now, root_key=context.config.platform_root_key
        )
        return change, account_change(connection, user, "credential_changed", now, target=found_kind)

    change, account = await _in_transaction(context, work)
    notices.hint_devices(change.devices, user)
    publish_rows(account)
    return {}


@router.post("/v1/me/credentials/rotate", auth="browser", right="user", max_body=EVENT_BATCH_BYTES)
async def rotate_credentials(context: Context, principal: CurrentPrincipal, body: RotateBody) -> dict[str, Any]:
    user, now = user_id(principal), context.clock()

    def work(connection: Any) -> tuple[records.Rotation, dict[str, str]]:
        rotation = records.rotate(
            connection,
            owner=_user_author(connection, user),
            epoch=body.epoch,
            wraps=body.wraps,
            records=body.records,
            now=now,
            root_key=context.config.platform_root_key,
        )
        kinds = dict(
            connection.execute(
                "SELECT id::text, kind FROM hub.principal WHERE id = ANY(%s::uuid[])", (sorted(rotation.recipients),)
            ).fetchall()
        )
        return rotation, kinds

    rotation, kinds = await _in_transaction(context, work)
    notices.hint_devices(rotation.devices, user)
    for recipient in sorted(rotation.recipients):
        audience: AudienceKind = "device" if kinds[recipient] == "device" else "user"
        publish(Audience(audience, UUID(recipient)), "wraps", {})
    return {}


def _owner(text: str, *, write: bool) -> UUID:
    found = parse_id(text)
    if found is None:
        raise HubError("forbidden" if write else "not_found")
    return found


@router.get(
    "/v1/device/credentials/{owner}/{kind}", auth="device", right="device_eligible", read_resource="device.credential"
)
async def get_device_credential(context: Context, principal: ActiveDevice, owner: str, kind: str) -> dict[str, Any]:
    device, owner_id, now = device_id(principal), _owner(owner, write=False), context.clock()
    found_kind = _kind(kind)

    def work(connection: Any) -> dict[str, Any] | None:
        if not holds(connection, device, owner_id, context.config.platform_root_key):
            raise HubError("not_found")
        notices.clear(connection, device, owner_id, found_kind, now)
        return records.get_record(connection, owner_id, found_kind)

    record = await _in_transaction(context, work)
    if record is None:
        raise HubError("not_found")
    return {"record": record}


@router.put("/v1/device/credentials/{owner}/{kind}", auth="device", right="device_eligible")
async def put_device_credential(
    context: Context, principal: ActiveDevice, owner: str, kind: str, body: DevicePutBody
) -> dict[str, Any]:
    device, owner_id, now = device_id(principal), _owner(owner, write=True), context.clock()
    if kind not in records.KINDS:
        raise HubError("forbidden")

    def work(connection: Any) -> records.Change:
        if not is_eligible(connection, device, owner_id) or not authorizes_write(
            connection, device, owner_id, request_id=UUID(body.request_id), kind=kind, now=now
        ):
            raise HubError("forbidden")
        author = records.Author(id=device, kind="device", sig_public=records.device_key(connection, device))
        return records.put_record(
            connection,
            owner=owner_id,
            kind=kind,
            record=body.record,
            expected_version=body.expected_version,
            author=author,
            now=now,
            root_key=context.config.platform_root_key,
        )

    change = await _in_transaction(context, work)
    notices.hint_devices(change.devices, owner_id)
    return {"version": change.version}
