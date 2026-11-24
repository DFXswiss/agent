"""Routes of the platform area (03-api.md sections 4.1 to 4.3).

- `POST /v1/platform/log` (root): append root-signed entries (`agent_hub.platform.log`); after commit the hint
  `decertified` to every newly decertified device and the signal `device.decertified`;
- `GET /v1/platform/log` (browser, `user`): `?after=&limit=` gives entries ascending, `head` and `next_after`;
- `GET /v1/platform/enrollments` (root): verified pending enrollments and the platform log;
- `GET /v1/platform/waiting` (root): workgroups whose tasks wait for a device the operator must provide
  (`agent_hub.scheduler.waiting`, ADR-060 section 7);
- `POST /v1/platform/invites`, `GET /v1/platform/invites`, `POST /v1/platform/invites/{id}/revoke` (browser,
  `platform:inviter`): platform invitations (`agent_hub.platform.invites`);
- `POST /v1/invites/lookup` (anon): lookup of platform and org invitations (`agent_hub.invites.lookup`).

The request bodies are closed (`extra="forbid"`, strict types): an unknown field or a wrong type is
`400 request_invalid` with the pointers of the fields. Database work runs in a worker thread inside one transaction.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from typing import Annotated, Any, Literal
from uuid import UUID

from fastapi import Query
from pydantic import BaseModel, ConfigDict, Field, StrictInt, StrictStr

from agent_hub import signals
from agent_hub.agents.notify import account_change
from agent_hub.db.pool import transaction
from agent_hub.errors import HubError
from agent_hub.http.context import Context, CurrentPrincipal, HubContext
from agent_hub.http.routing import HubRouter
from agent_hub.invites import lookup as invite_lookup
from agent_hub.notifications.rules import Notification, publish_rows
from agent_hub.platform import enrollments, invites
from agent_hub.platform import log as platform_log
from agent_hub.scheduler import waiting as scheduler_waiting
from agent_hub.stream.publish import Audience, publish

router = HubRouter()
LOG = logging.getLogger("agent_hub.platform")

INVITE_ID_PATTERN = r"^[A-Za-z0-9_-]{21}[AQgw]$"
KEY_PATTERN = r"^[A-Za-z0-9_-]{42}[AEIMQUYcgkosw048]$"
TIME_PATTERN = (
    r"^[0-9]{4}-(0[1-9]|1[0-2])-(0[1-9]|[12][0-9]|3[01])T([01][0-9]|2[0-3]):[0-5][0-9]:[0-5][0-9]\.[0-9]{3}Z$"
)
Q_SEQ_PATTERN = r"^(0|[1-9][0-9]{0,15})$"
Q_LIMIT_LOG_PATTERN = r"^([1-9][0-9]{0,2}|1000)$"
DEFAULT_LOG_LIMIT = 200
MAX_LOG_ENTRIES = 1000


class Closed(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class AppendRequest(Closed):
    expected_seq: StrictInt = Field(ge=0, le=2**53 - 1)
    entries: list[Any] = Field(max_length=MAX_LOG_ENTRIES)


class CreateInviteRequest(Closed):
    invite_id: StrictStr = Field(pattern=INVITE_ID_PATTERN)
    pk_inv: StrictStr = Field(pattern=KEY_PATTERN)
    kind: Literal["user"]
    expires_at: StrictStr = Field(pattern=TIME_PATTERN)


class EmptyRequest(Closed):
    pass


class LookupRequest(Closed):
    invite_id: StrictStr = Field(pattern=INVITE_ID_PATTERN)


async def in_transaction[T](context: HubContext, work: Callable[[Any], T]) -> T:
    """Run `work(connection)` in a worker thread inside one transaction."""

    def run() -> T:
        with transaction(context.pool) as connection:
            return work(connection)

    return await asyncio.to_thread(run)


def user_of(principal: Any) -> UUID:
    """The user ID of a browser session (the pipeline guarantees the kind)."""
    user: UUID = principal.principal_id
    return user


@router.post("/v1/platform/log", auth="root", right="root")
async def append_log(body: AppendRequest, context: Context) -> dict[str, Any]:
    now = context.clock()
    root = context.config.platform_root_key
    result = await in_transaction(
        context, lambda connection: platform_log.append(connection, root, body.expected_seq, body.entries, now)
    )
    for device in result.decertified:
        try:  # after commit: a failing fan-out must not turn a stored append into an error (the CLI would retry)
            publish(Audience(kind="device", id=UUID(device)), "decertified", {})
        except Exception:
            LOG.error("platform log: the decertified hint could not be published", exc_info=True)
        signals.emit("device.decertified", device=UUID(device))
    return {"head": result.head.to_json()}


@router.get("/v1/platform/log", auth="browser", right="user", read_resource="platform.log")
async def get_log(
    context: Context,
    after: Annotated[str | None, Query(pattern=Q_SEQ_PATTERN)] = None,
    limit: Annotated[str | None, Query(pattern=Q_LIMIT_LOG_PATTERN)] = None,
) -> dict[str, Any]:
    start = -1 if after is None else int(after)
    count = DEFAULT_LOG_LIMIT if limit is None else int(limit)

    def read(connection: Any) -> dict[str, Any]:
        # head and entries from one snapshot: an append committed between the two reads must not show up beyond the
        # served head (the browser would refuse the page as a broken chain)
        connection.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ")
        head = platform_log.stored_head(connection)
        if head is None:
            raise HubError("not_found")
        rows = platform_log.stored_entries(connection, start, count)
        last = rows[-1][0] if rows else None
        more = last is not None and last < head.seq
        return {
            "entries": [{"seq": seq, "entry": entry} for seq, entry in rows],
            "head": head.to_json(),
            "next_after": last if more else None,
        }

    return await in_transaction(context, read)


@router.get("/v1/platform/enrollments", auth="root", right="root", read_resource="platform.enrollments")
async def get_enrollments(context: Context) -> dict[str, Any]:
    root = context.config.platform_root_key

    def read(connection: Any) -> dict[str, Any]:
        state = platform_log.state(connection, root)
        log = [entry for _seq, entry in platform_log.stored_entries(connection)]
        return {"enrollments": enrollments.pending(connection, state), "log": log}

    return await in_transaction(context, read)


@router.get("/v1/platform/waiting", auth="root", right="root", read_resource="platform.waiting")
async def get_waiting(context: Context) -> dict[str, Any]:
    now, online = context.clock(), context.config.device_online_seconds
    return await in_transaction(
        context, lambda connection: {"waiting": scheduler_waiting.waiting(connection, now, online)}
    )


@router.post("/v1/platform/invites", auth="browser", right="platform:inviter")
async def create_invite(body: CreateInviteRequest, context: Context, principal: CurrentPrincipal) -> dict[str, Any]:
    now = context.clock()
    user = user_of(principal)

    def work(connection: Any) -> list[Notification]:
        if not invites.create(
            connection, user, body.invite_id, pk_inv=body.pk_inv, expires_text=body.expires_at, now=now
        ):
            return []
        # an agent created a platform invitation: the user is told (KOR-24)
        return account_change(connection, user, "invite_created", now, target="platform")

    publish_rows(await in_transaction(context, work))
    return {}


@router.get("/v1/platform/invites", auth="browser", right="platform:inviter", read_resource="platform.invites")
async def list_invites(context: Context, principal: CurrentPrincipal) -> dict[str, Any]:
    now = context.clock()
    user = user_of(principal)
    return {"invites": await in_transaction(context, lambda c: invites.listing(c, user, now))}


@router.post("/v1/platform/invites/{id}/revoke", auth="browser", right="platform:inviter")
async def revoke_invite(id: str, body: EmptyRequest, context: Context, principal: CurrentPrincipal) -> dict[str, Any]:
    now = context.clock()
    user = user_of(principal)
    await in_transaction(context, lambda c: invites.revoke(c, user, id, now))
    return {}


@router.post("/v1/invites/lookup", auth="anon", right="anon")
async def lookup_invite(body: LookupRequest, context: Context) -> dict[str, Any]:
    now = context.clock()
    return await in_transaction(context, lambda c: invite_lookup.lookup(c, body.invite_id, now))
