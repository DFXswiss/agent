"""Routes of the subscriptions area (03-api.md sections 4.8 and 6.1; ADR-090).

| Route | Right |
|---|---|
| `GET /v1/me/subscriptions` | `user`: every own subscription with its newest reading (`store.rows_for`) |
| `GET /v1/subscriptions/{id}` | `user`: an own subscription record |
| `PUT /v1/subscriptions/{id}` | `user`: an own further subscription (never the primary record) |
| `DELETE /v1/subscriptions/{id}` | `user`: an own subscription record (also the primary one) |
| `GET /v1/device/subscriptions?owner_id=` | `device_eligible`: the metadata of every subscription of the owner |
| `GET /v1/device/subscriptions/{id}` | `device_eligible`: the device holds the owner's `CK` |
| `PUT /v1/device/subscriptions/{id}` | `device_eligible`: answering a `claude.login` of the owner, or a re-seal |

A browser write is signed by the caller's identity or the caller's own-identity agent (ADR-082 section 9.5) and sealed
under the caller's current `CK` epoch (`agent_hub.credentials.records.put_identity` with kind `claude_subscription`): a
new record is never primary, and a write of the primary record is `403 forbidden` (it is written through `PUT
/v1/me/credentials/claude_subscription`, whose body form the deployed web client knows). `use` (optional) replaces the
plaintext mirror of the record's sealed use (`store.set_use`; ADR-090 section 3); without it the mirror stays. Reads and
deletes of a record that is not the caller's are `404 not_found`.

A device writes a user's further subscription while answering an active `claude.login` of that user
(`request_id`, `agent_hub.requests.store.authorizes_write`), or - without `request_id` - **re-seals** one: the stored
record is a non-primary subscription of the owner under an older epoch than the owner's current `CK` epoch, and the
new version is under the current epoch (checked by `put_identity`); anything else is `403 forbidden`, as is a write
of a device that is not eligible for the owner's `CK`. A device read needs the same eligibility (`404 not_found`
otherwise, also for a malformed or missing `owner_id`); only the list clears the device's change notice of `(owner,
claude_subscription)` up to the time it read (it shows every record of the owner).

After a committed change the noticed devices get `credentials {owner_id}`; a write or delete by the caller's agent
writes the account notification `credential_changed` (target `claude_subscription`).
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
from agent_hub.credentials.routes import user_author
from agent_hub.device.keys import ActiveDevice, device_id
from agent_hub.errors import HubError
from agent_hub.http.context import Context, CurrentPrincipal, HubContext
from agent_hub.http.routing import EVENT_BATCH_BYTES, HubRouter
from agent_hub.notifications.rules import Notification, publish_rows
from agent_hub.orgs.routes import parse_id, user_id
from agent_hub.requests.store import authorizes_write
from agent_hub.subscriptions import store
from agent_hub.wraps.rules import cred_epoch

router = HubRouter()
MAX_SAFE_INTEGER = 2**53 - 1
UUID7 = r"^[0-9a-f]{8}-[0-9a-f]{4}-7[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$"
Uuid = Annotated[str, Field(pattern=UUID7)]
Version = Annotated[int, Field(strict=True, ge=0, le=MAX_SAFE_INTEGER)]
KIND = records.SUBSCRIPTION


class UseBody(BaseModel):
    """`use` of `PUT /v1/subscriptions/{id}`: the mirror of the sealed use (ADR-090 section 3)."""

    model_config = ConfigDict(extra="forbid")
    pool: Annotated[bool, Field(strict=True)]
    orgs: list[Uuid] = Field(max_length=50)


class PutBody(BaseModel):
    """Body of `PUT /v1/subscriptions/{id}` (the record form is checked by `records`)."""

    model_config = ConfigDict(extra="forbid")
    record: dict[str, Any]
    expected_version: Version
    use: UseBody | None = None


class DevicePutBody(BaseModel):
    """Body of `PUT /v1/device/subscriptions/{id}`."""

    model_config = ConfigDict(extra="forbid")
    owner_id: Uuid
    record: dict[str, Any]
    expected_version: Version
    request_id: Uuid | None = None


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


def _after(change: records.Change, owner: UUID, account: list[Notification]) -> None:
    notices.hint_devices(change.devices, owner)
    publish_rows(account)


def _primary(connection: Any, record_id: UUID) -> bool:
    found = records.load_identity(connection, record_id, KIND)
    return found is not None and found.primary


@router.get("/v1/me/subscriptions", auth="browser", right="user", read_resource="me.subscriptions")
async def get_subscriptions(context: Context, principal: CurrentPrincipal) -> dict[str, Any]:
    user = user_id(principal)
    return {"subscriptions": await _in_transaction(context, lambda c: store.rows_for(c, user))}


@router.get("/v1/subscriptions/{id}", auth="browser", right="user", read_resource="subscription")
async def get_subscription(context: Context, principal: CurrentPrincipal, id: str) -> dict[str, Any]:
    user, record_id = user_id(principal), _path(id)
    return await _in_transaction(context, lambda c: store.answer(store.own(c, record_id, user)))


@router.put("/v1/subscriptions/{id}", auth="browser", right="user", max_body=EVENT_BATCH_BYTES)
async def put_subscription(context: Context, principal: CurrentPrincipal, id: str, body: PutBody) -> dict[str, Any]:
    user, record_id, now = user_id(principal), _path(id, write=True), context.clock()

    def work(connection: Any) -> tuple[records.Change, list[Notification]]:
        if _primary(connection, record_id):
            raise HubError("forbidden")
        change = records.put_identity(
            connection,
            owner=records.Owner(user, records.USER),
            record_id=record_id,
            record=body.record,
            expected_version=body.expected_version,
            author=user_author(connection, principal),
            now=now,
            root_key=context.config.platform_root_key,
            kind=KIND,
        )
        if body.use is not None:
            store.set_use(connection, record_id, user, pool=body.use.pool, orgs=list(body.use.orgs), now=now)
        return change, account_change(connection, user, "credential_changed", now, target=KIND)

    change, account = await _in_transaction(context, work)
    _after(change, user, account)
    return {"version": change.version}


@router.delete("/v1/subscriptions/{id}", auth="browser", right="user")
async def delete_subscription(context: Context, principal: CurrentPrincipal, id: str) -> dict[str, Any]:
    user, record_id, now = user_id(principal), _path(id), context.clock()

    def work(connection: Any) -> tuple[records.Change, list[Notification]]:
        identity = store.own(connection, record_id, user)
        actor = records.Author(id=user, kind="user", sig_public=b"")
        change = records.delete_identity(
            connection, identity, actor=actor, now=now, root_key=context.config.platform_root_key
        )
        return change, account_change(connection, user, "credential_changed", now, target=KIND)

    change, account = await _in_transaction(context, work)
    _after(change, user, account)
    return {}


@router.get("/v1/device/subscriptions", auth="device", right="device_eligible", read_resource="device.subscriptions")
async def list_device_subscriptions(
    context: Context, principal: ActiveDevice, *, owner_id: str | None = None
) -> dict[str, Any]:
    device, owner, now = device_id(principal), _path(owner_id or ""), context.clock()

    def work(connection: Any) -> dict[str, Any]:
        if not holds(connection, device, owner, context.config.platform_root_key):
            raise HubError("not_found")
        notices.clear(connection, device, owner, KIND, now)
        return {"subscriptions": [store.subscription_meta(item) for item in store.owner_records(connection, owner)]}

    return await _in_transaction(context, work)


@router.get(
    "/v1/device/subscriptions/{id}", auth="device", right="device_eligible", read_resource="device.subscription"
)
async def get_device_subscription(context: Context, principal: ActiveDevice, id: str) -> dict[str, Any]:
    device, record_id = device_id(principal), _path(id)

    def work(connection: Any) -> dict[str, Any]:
        identity = records.load_identity(connection, record_id, KIND)
        if identity is None or not holds(connection, device, identity.owner.id, context.config.platform_root_key):
            raise HubError("not_found")
        return store.answer(identity)

    return await _in_transaction(context, work)


def _reseal_allowed(connection: Any, record_id: UUID, owner: UUID) -> bool:
    """The stored record is a further subscription of the owner under an older epoch than the owner's current one."""
    found = records.load_identity(connection, record_id, KIND)
    if found is None or found.owner.id != owner or found.primary:
        return False
    current = cred_epoch(connection, str(owner))
    return current is not None and found.epoch < current


@router.put("/v1/device/subscriptions/{id}", auth="device", right="device_eligible")
async def put_device_subscription(
    context: Context, principal: ActiveDevice, id: str, body: DevicePutBody
) -> dict[str, Any]:
    device, record_id, now = device_id(principal), _path(id, write=True), context.clock()
    owner_id = UUID(body.owner_id)

    def work(connection: Any) -> records.Change:
        if body.request_id is None:
            allowed = _reseal_allowed(connection, record_id, owner_id)
        else:
            allowed = authorizes_write(
                connection, device, owner_id, request_id=UUID(body.request_id), kind=KIND, now=now
            ) and not _primary(connection, record_id)
        if not allowed or not is_eligible(connection, device, owner_id):
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
            kind=KIND,
        )

    change = await _in_transaction(context, work)
    notices.hint_devices(change.devices, owner_id)
    return {"version": change.version}
