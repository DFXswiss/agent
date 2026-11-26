"""Routes of the mailboxes area (03-api.md sections 4.8 and 6.1; ADR-099).

| Route | Right |
|---|---|
| `GET /v1/me/mailboxes` | `user`: every own mailbox with its release mirror (`store.rows_for`) |
| `GET /v1/mailboxes/{id}` | `user`: an own mailbox record |
| `PUT /v1/mailboxes/{id}` | `user`: an own mailbox record (also the primary one) |
| `DELETE /v1/mailboxes/{id}` | `user`: an own mailbox record (also the primary one) |
| `GET /v1/device/mailboxes?wg_id=` | `device_eligible`: the records released to the workgroup the device can open |

A browser write is signed by the caller's identity or the caller's own-identity agent (ADR-082 section 9.5) and sealed
under the caller's current `CK` epoch (`agent_hub.credentials.records.put_identity` with kind `mailbox`): a user's first
mailbox becomes primary (the mailbox of the automatic login), further ones never do. `use` (optional) replaces the
plaintext mirror of the record's sealed release (`store.set_use`, `403 forbidden` for a workgroup the caller has no
access to); without it the mirror stays. Reads and deletes of a record that is not the caller's are `404 not_found`.

A device reads only through the workgroup list (`store.released_to`): no device writes a mailbox. After a committed
change the noticed devices get `credentials {owner_id}`; a write or delete writes the account notification
`credential_changed` (target `mailbox`).
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from typing import Annotated, Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from agent_hub.agents.notify import account_change
from agent_hub.credentials import notices, records
from agent_hub.credentials.routes import user_author
from agent_hub.device.keys import ActiveDevice, device_id
from agent_hub.errors import HubError
from agent_hub.http.context import Context, CurrentPrincipal, HubContext
from agent_hub.http.routing import EVENT_BATCH_BYTES, HubRouter
from agent_hub.mailboxes import store
from agent_hub.notifications.rules import Notification, publish_rows
from agent_hub.orgs.routes import parse_id, user_id

router = HubRouter()
MAX_SAFE_INTEGER = 2**53 - 1
UUID7 = r"^[0-9a-f]{8}-[0-9a-f]{4}-7[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$"
Uuid = Annotated[str, Field(pattern=UUID7)]
Version = Annotated[int, Field(strict=True, ge=0, le=MAX_SAFE_INTEGER)]
KIND = records.MAILBOX


class UseBody(BaseModel):
    """`use` of `PUT /v1/mailboxes/{id}`: the mirror of the sealed release (ADR-099 section 3)."""

    model_config = ConfigDict(extra="forbid")
    wgs: list[Uuid] = Field(max_length=50)


class PutBody(BaseModel):
    """Body of `PUT /v1/mailboxes/{id}` (the record form is checked by `records`)."""

    model_config = ConfigDict(extra="forbid")
    record: dict[str, Any]
    expected_version: Version
    use: UseBody | None = None


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


@router.get("/v1/me/mailboxes", auth="browser", right="user", read_resource="me.mailboxes")
async def get_mailboxes(context: Context, principal: CurrentPrincipal) -> dict[str, Any]:
    user = user_id(principal)
    return {"mailboxes": await _in_transaction(context, lambda c: store.rows_for(c, user))}


@router.get("/v1/mailboxes/{id}", auth="browser", right="user", read_resource="mailbox")
async def get_mailbox(context: Context, principal: CurrentPrincipal, id: str) -> dict[str, Any]:
    user, record_id = user_id(principal), _path(id)
    return await _in_transaction(context, lambda c: store.answer(store.own(c, record_id, user)))


@router.put("/v1/mailboxes/{id}", auth="browser", right="user", max_body=EVENT_BATCH_BYTES)
async def put_mailbox(context: Context, principal: CurrentPrincipal, id: str, body: PutBody) -> dict[str, Any]:
    user, record_id, now = user_id(principal), _path(id, write=True), context.clock()

    def work(connection: Any) -> tuple[records.Change, list[Notification]]:
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
            store.set_use(connection, record_id, user, wgs=list(body.use.wgs), now=now)
        return change, account_change(connection, user, "credential_changed", now, target=KIND)

    change, account = await _in_transaction(context, work)
    _after(change, user, account)
    return {"version": change.version}


@router.delete("/v1/mailboxes/{id}", auth="browser", right="user")
async def delete_mailbox(context: Context, principal: CurrentPrincipal, id: str) -> dict[str, Any]:
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


@router.get("/v1/device/mailboxes", auth="device", right="device_eligible", read_resource="device.mailboxes")
async def list_device_mailboxes(
    context: Context, principal: ActiveDevice, *, wg_id: str | None = None
) -> dict[str, Any]:
    device, wg = device_id(principal), _path(wg_id or "")
    return {"mailboxes": await _in_transaction(context, lambda c: store.released_to(c, device, wg))}
