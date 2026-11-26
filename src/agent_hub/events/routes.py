"""Routes of the events area (03-api.md section 4.4).

- `POST /v1/events` (browser; the right depends on each event's kind, 04-crypto.md section 4.3): at most 100 events
  `{event, notify?}`, all or nothing through `agent_hub.events.store.append_events`; answer `{results:[{id, scope_id,
  scope_seq, replayed}]}` in request order. After the commit the new events are announced
  (`agent_hub.events.notify.announce`: live frames, device hints, the signal `event.appended`).
- `GET /v1/scopes/{scope_id}/events` (browser; `org:member` on an org scope, `wg:read` on a workgroup scope; an
  unknown or foreign scope is `404 not_found`): `?after=&limit=&kinds=a,b&task=&session=&ref=` through
  `agent_hub.events.read.read_events`, or newest first with `before=` instead of `after=` (both: `400
  request_invalid`; the answer adds `next_before`, KOR-44); read log `read.scope.events`.
"""

from __future__ import annotations

import asyncio
import re
from typing import Any, cast
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, StrictBool

from agent_hub.agents.identity import actor
from agent_hub.errors import HubError
from agent_hub.events import notify, read
from agent_hub.events.store import StoredEvent, append_events
from agent_hub.http.context import Context, CurrentPrincipal, HubContext
from agent_hub.http.routing import EVENT_BATCH_BYTES, HubRouter
from agent_hub.log.events import KIND_RULES
from agent_hub.orgs import rights

router = HubRouter()
MAX_EVENTS = 100
MAX_SEQ = 10**16 - 1
"""The largest `after` of the contract (`q_seq`: up to 16 digits)."""
KINDS = frozenset(rule.kind for rule in KIND_RULES)
_KINDS_RE = re.compile(r"[a-z_]+(\.[a-z_]+)?(,[a-z_]+(\.[a-z_]+)?){0,31}")


class EventItem(BaseModel):
    """One item of `POST /v1/events`; the event itself is checked by the crypto layer."""

    model_config = ConfigDict(extra="forbid")
    event: dict[str, Any]
    notify: StrictBool = False


class EventsRequest(BaseModel):
    """Body of `POST /v1/events`."""

    model_config = ConfigDict(extra="forbid")
    events: list[EventItem] = Field(min_length=1, max_length=MAX_EVENTS)


def _append(context: HubContext, author: str, body: EventsRequest) -> list[StoredEvent]:
    """Store in one transaction, then announce after the commit (in the worker thread: subscribers may do I/O)."""
    with context.pool.connection() as connection, connection.transaction():
        stored = append_events(
            connection,
            [item.event for item in body.events],
            author,
            now=context.clock(),
            root_key=context.config.platform_root_key,
            notify=[item.notify for item in body.events],
        )
    notify.announce(stored)
    return stored


@router.post("/v1/events", auth="browser", right="per_event_kind", max_body=EVENT_BATCH_BYTES)
async def post_events(context: Context, principal: CurrentPrincipal, body: EventsRequest) -> dict[str, Any]:
    # the author is the cryptographic actor: the user, or the user's own-identity agent (ADR-082)
    stored = await asyncio.to_thread(_append, context, actor(principal).id, body)
    return {"results": [item.result() for item in stored]}


def parse_uuid(text: str | None) -> UUID | None:
    """A query or path ID in canonical UUID form (None if absent); `ValueError` otherwise."""
    if text is None:
        return None
    value = UUID(text)
    if str(value) != text:
        raise ValueError("not a canonical UUID")
    return value


def parse_count(text: str | None, default: int, minimum: int, maximum: int) -> int:
    """A decimal query count without sign or leading zeros within the bounds (`request_invalid` otherwise)."""
    if text is None:
        return default
    if not (text.isascii() and text.isdigit()) or text != str(int(text)) or not minimum <= int(text) <= maximum:
        raise HubError("request_invalid")
    return int(text)


def parse_kinds(text: str | None) -> frozenset[str] | None:
    """The `kinds` filter: comma-separated known event kinds (`request_invalid` otherwise)."""
    if text is None:
        return None
    if _KINDS_RE.fullmatch(text) is None:
        raise HubError("request_invalid")
    kinds = frozenset(text.split(","))
    if not kinds <= KINDS:
        raise HubError("request_invalid")
    return kinds


def scope_page(
    context: HubContext,
    user: UUID,
    scope_id: UUID,
    *,
    after: int,
    before: int | None = None,
    limit: int,
    kinds: frozenset[str] | None,
    filters: dict[str, UUID | None],
) -> dict[str, Any]:
    """The body of `GET /v1/scopes/{scope_id}/events` after the right check: events and head from one snapshot (an
    event committed between the two reads would otherwise raise the head beyond the page, and the browser would move
    its cursor past an event it never received)."""
    with context.pool.connection() as connection, connection.transaction():
        connection.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ")
        scope = read.scope_of(connection, scope_id)
        if scope is None:
            raise HubError("not_found")
        if scope.scope == "org":
            rights.require(connection, user, rights.ORG_MEMBER, org_id=scope.org_id)
        else:
            rights.require(connection, user, rights.WG_READ, wg_id=scope.scope_id)
        ref = filters["ref"]
        page = read.read_events(
            connection,
            scope_id,
            after=after,
            limit=limit,
            kinds=kinds,
            task=filters["task"],
            session=filters["session"],
            ref=None if ref is None else str(ref),
            before=before,
        )
    return page.to_json()


@router.get(
    "/v1/scopes/{scope_id}/events", auth="browser", right=("org:member", "wg:read"), read_resource="scope.events"
)
async def get_scope_events(
    context: Context,
    principal: CurrentPrincipal,
    scope_id: str,
    *,
    after: str | None = None,
    before: str | None = None,
    limit: str | None = None,
    kinds: str | None = None,
    task: str | None = None,
    session: str | None = None,
    ref: str | None = None,
) -> dict[str, Any]:
    if after is not None and before is not None:
        raise HubError("request_invalid")
    try:
        scope = parse_uuid(scope_id)
    except ValueError:
        raise HubError("not_found") from None
    try:
        filters = {"task": parse_uuid(task), "session": parse_uuid(session), "ref": parse_uuid(ref)}
    except ValueError:
        raise HubError("request_invalid") from None
    return await asyncio.to_thread(
        scope_page,
        context,
        cast(UUID, principal.principal_id),
        cast(UUID, scope),
        after=parse_count(after, 0, 0, MAX_SEQ),
        before=None if before is None else parse_count(before, 0, 0, MAX_SEQ),
        limit=parse_count(limit, read.PAGE_DEFAULT, 1, read.PAGE_MAX),
        kinds=parse_kinds(kinds),
        filters=filters,
    )
