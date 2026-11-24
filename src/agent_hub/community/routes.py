"""Routes of the community area (14-community.md section 4, ADR-069).

Every route of a user is a signed browser request (`auth="browser"`; KOR-24 agents use the same routes with the
user's rights); the moderator list is root-signed (operator CLI). Request bodies are closed (`extra="forbid"`, strict
types). Database work runs in a worker thread inside one transaction; live frames are published after the commit:
`community {change, seq}` to every browser (`all`) for new and deleted messages and for a new key request, to the one
user for a new wrap (`key`) and a changed mute (`mute`).
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from typing import Annotated, Any, cast
from uuid import UUID

from fastapi import Query, Request
from pydantic import Field, StrictBool, StrictInt

from agent_hub.audit.recorder import unchanged
from agent_hub.auth.fields import B64u32, B64u64, Empty, Strict, Uuid7, b64u_text
from agent_hub.auth.keys import Principal
from agent_hub.community import distribution, store
from agent_hub.community.limits import REQUEST_BURST, REQUEST_REFILL_SECONDS, PostLimiter
from agent_hub.db.pool import transaction
from agent_hub.errors import HubError
from agent_hub.events.routes import parse_count
from agent_hub.http.context import Context, CurrentPrincipal, HubContext
from agent_hub.http.routing import HubRouter
from agent_hub.stream.publish import ALL, Audience, publish
from agent_hub.workgroups.routes import path_id

router = HubRouter()
LOG = logging.getLogger("agent_hub.community")

DEFAULT_PAGE = 50
DEFAULT_REQUESTS = 20
MAX_PAGE = 100
REQUEST_FRAME_SECONDS = 5.0
"""At most one `community {change:"request"}` frame per this many seconds and hub process (ADR-069 decision 3)."""
MAX_WRAPS = 100
SEQ_PATTERN = r"^(0|[1-9][0-9]{0,15})$"
WRAP_CHARS = 128
"""b64u length bound of a wrap (`enc` 32 B + 32 B key + 16 B tag = 80 B = 107 characters)."""


class SelfWrap(Strict):
    epoch: StrictInt = Field(ge=1, le=2**31 - 1)
    wrap: Annotated[str, b64u_text(WRAP_CHARS)]
    sig: B64u64


class EpochCreate(SelfWrap):
    key_check: B64u32


class RequestPut(Strict):
    id: Uuid7
    kem_pub: B64u32
    sig: B64u64


class RequestWrap(Strict):
    request: Uuid7
    epoch: StrictInt = Field(ge=1, le=2**31 - 1)
    wrap: Annotated[str, b64u_text(WRAP_CHARS)]
    sig: B64u64


class RequestWrapsPost(Strict):
    wraps: list[RequestWrap] = Field(min_length=1, max_length=MAX_WRAPS)


class MessagePost(Strict):
    message: dict[str, Any]


class ReadPut(Strict):
    seq: StrictInt = Field(ge=0, le=2**53 - 1)


class ModeratorPost(Strict):
    user: Uuid7
    granted: StrictBool


def _user(principal: Principal) -> UUID:
    return cast(UUID, principal.principal_id)


async def in_transaction[T](context: HubContext, work: Callable[[Any], T]) -> T:
    """Run `work(connection)` in a worker thread inside one transaction."""

    def run() -> T:
        with transaction(context.pool) as connection:
            return work(connection)

    return await asyncio.to_thread(run)


def limiter_of(request: Request, context: HubContext) -> PostLimiter:
    """The post limiter of this hub app (created on first use)."""
    limiter: PostLimiter | None = getattr(request.app.state, "community_limiter", None)
    if limiter is None:
        limiter = PostLimiter(context.clock, burst=store.LIMITS["burst"], refill_seconds=store.LIMITS["refill_seconds"])
        request.app.state.community_limiter = limiter
    return limiter


def request_limiter_of(request: Request, context: HubContext) -> PostLimiter:
    """The key-request limiter of this hub app (created on first use)."""
    limiter: PostLimiter | None = getattr(request.app.state, "community_request_limiter", None)
    if limiter is None:
        limiter = PostLimiter(context.clock, burst=REQUEST_BURST, refill_seconds=REQUEST_REFILL_SECONDS)
        request.app.state.community_request_limiter = limiter
    return limiter


def announce(audience: Audience, change: str, seq: int) -> None:
    """Publish `community {change, seq}` after the commit; a failing fan-out never fails the stored change."""
    try:
        publish(audience, "community", {"change": change, "seq": seq})
    except Exception:
        LOG.error("community: the %s frame could not be published", change, exc_info=True)


def everyone() -> Audience:
    return Audience(kind="all", id=ALL)


def request_frame(request: Request, context: HubContext, seq: int) -> None:
    """Announce a new key request to every browser, at most once per `REQUEST_FRAME_SECONDS` in this process."""
    now = context.clock().timestamp()
    last: float | None = getattr(request.app.state, "community_request_frame", None)
    if last is not None and now - last < REQUEST_FRAME_SECONDS:
        return
    request.app.state.community_request_frame = now
    announce(everyone(), "request", seq)


@router.get("/v1/community", auth="browser", right="user", read_resource="community")
async def get_community(request: Request, context: Context, principal: CurrentPrincipal) -> dict[str, Any]:
    user, now = _user(principal), context.clock()

    def work(connection: Any) -> tuple[dict[str, Any], bool]:
        return distribution.state(connection, user), distribution.waiting_unserved(connection, user, now)

    body, unserved = await in_transaction(context, work)
    # a waiting client reads its state every minute: an announcement swallowed by the coalescing window (or lost
    # with a stream) is repeated then, so no request waits for the holders' 10-minute round
    if unserved:
        request_frame(request, context, body["head_seq"])
    return body


@router.post("/v1/community/join", auth="browser", right="user")
async def join(body: Empty, context: Context, principal: CurrentPrincipal) -> dict[str, Any]:
    user, now = _user(principal), context.clock()
    await in_transaction(context, lambda c: store.join(c, user, now))
    return {"joined": True}


@router.post("/v1/community/epochs", auth="browser", right="user")
async def create_epoch(body: EpochCreate, context: Context, principal: CurrentPrincipal) -> dict[str, Any]:
    user, now = _user(principal), context.clock()
    epoch = await in_transaction(
        context,
        lambda c: distribution.create_epoch(
            c, user, epoch=body.epoch, check_text=body.key_check, wrap_text=body.wrap, signature=body.sig, now=now
        ),
    )
    return {"epoch": epoch}


@router.put("/v1/community/request", auth="browser", right="user")
async def put_request(
    body: RequestPut, request: Request, context: Context, principal: CurrentPrincipal
) -> dict[str, Any]:
    user, now = _user(principal), context.clock()
    limiter = request_limiter_of(request, context)

    def work(connection: Any) -> tuple[bool, int]:
        # each new request is audited and announced: a client cannot flood the platform with them (a repeat of the
        # open request is free)
        if not distribution.is_open_request(connection, user, UUID(body.id)):
            wait = limiter.acquire(str(user))
            if wait is not None:
                raise HubError("rate_limited", headers={"Retry-After": str(wait)})
        created = distribution.put_request(
            connection, user, request_id=UUID(body.id), kem_text=body.kem_pub, signature=body.sig, now=now
        )
        return created, store.head_seq(connection)

    created, seq = await in_transaction(context, work)
    if created:
        request_frame(request, context, seq)
    return {"id": body.id}


@router.get("/v1/community/requests", auth="browser", right="user", read_resource="community.requests")
async def get_requests(
    context: Context, principal: CurrentPrincipal, limit: Annotated[str | None, Query()] = None
) -> dict[str, Any]:
    user, now = _user(principal), context.clock()
    count = parse_count(limit, DEFAULT_REQUESTS, 1, MAX_PAGE)
    return {"requests": await in_transaction(context, lambda c: distribution.open_requests(c, user, count, now))}


@router.post("/v1/community/requests/wraps", auth="browser", right="user")
async def post_request_wraps(body: RequestWrapsPost, context: Context, principal: CurrentPrincipal) -> dict[str, Any]:
    user, now = _user(principal), context.clock()
    items = [item.model_dump() for item in body.wraps]

    def work(connection: Any) -> tuple[list[UUID], int]:
        served = distribution.wrap_requests(connection, user, items, now)
        if not served:  # every entry skipped: nothing changed (ADR-063 section 1)
            unchanged()
        return served, store.head_seq(connection)

    served, seq = await in_transaction(context, work)
    for requester in served:
        announce(Audience(kind="user", id=requester), "key", seq)
    return {"stored": len(served), "skipped": len(items) - len(served)}


@router.post("/v1/community/request/reject", auth="browser", right="user")
async def reject_request(
    body: Empty, request: Request, context: Context, principal: CurrentPrincipal
) -> dict[str, Any]:
    user, now = _user(principal), context.clock()

    def work(connection: Any) -> int:
        distribution.reject(connection, user, now)
        return store.head_seq(connection)

    seq = await in_transaction(context, work)
    request_frame(request, context, seq)
    return {"rejected": True}


@router.post("/v1/community/wraps", auth="browser", right="user")
async def post_self_wrap(body: SelfWrap, context: Context, principal: CurrentPrincipal) -> dict[str, Any]:
    user, now = _user(principal), context.clock()
    epoch = await in_transaction(
        context,
        lambda c: distribution.store_self_wrap(
            c, user, epoch=body.epoch, wrap_text=body.wrap, signature=body.sig, now=now
        ),
    )
    return {"epoch": epoch}


@router.get("/v1/community/messages", auth="browser", right="user", read_resource="community.messages")
async def get_messages(
    context: Context,
    before: Annotated[str | None, Query(pattern=SEQ_PATTERN)] = None,
    after: Annotated[str | None, Query(pattern=SEQ_PATTERN)] = None,
    limit: Annotated[str | None, Query()] = None,
) -> dict[str, Any]:
    if before is not None and after is not None:
        raise HubError("request_invalid")
    count = parse_count(limit, DEFAULT_PAGE, 1, MAX_PAGE)
    page_before = None if before is None else int(before)
    page_after = None if after is None else int(after)
    return await in_transaction(context, lambda c: store.messages(c, before=page_before, after=page_after, limit=count))


@router.post("/v1/community/messages", auth="browser", right="user")
async def post_message(
    body: MessagePost, request: Request, context: Context, principal: CurrentPrincipal
) -> dict[str, Any]:
    user, now = _user(principal), context.clock()
    limiter = limiter_of(request, context)

    def work(connection: Any) -> store.Posted:
        _last_read, muted_at = store.require_member(connection, user)
        if muted_at is not None:
            raise HubError("community_muted")
        conflict: HubError | None = None
        try:
            repeat = store.existing(connection, user, body.message)
        except HubError as error:  # another message under a used ID: refused, but it costs a token like every attempt
            repeat, conflict = None, error
        if repeat is not None:  # an identical repeat (a retry) neither needs nor takes a token
            unchanged()
            return repeat
        # every other attempt takes a token, refused ones included (14-community.md section 4.3)
        wait = limiter.acquire(str(user))
        if wait is not None:
            raise HubError("rate_limited", headers={"Retry-After": str(wait)})
        if conflict is not None:
            raise conflict
        posted = store.post(connection, user, body.message, now)
        if posted.replayed:  # a concurrent identical post won the lock
            unchanged()
        return posted

    posted = await in_transaction(context, work)
    if posted.replayed:
        return {"seq": posted.seq, "replayed": True}
    announce(everyone(), "message", posted.seq)
    return {"seq": posted.seq}


@router.delete("/v1/community/messages/{id}", auth="browser", right=("owner", "community:moderator"))
async def delete_message(id: str, context: Context, principal: CurrentPrincipal) -> dict[str, Any]:
    user, now, message_id = _user(principal), context.clock(), path_id(id)
    role, seq = await in_transaction(context, lambda c: store.delete(c, user, message_id, now))
    announce(everyone(), "delete", seq)
    return {"deleted": role}


@router.put("/v1/community/read", auth="browser", right="user")
async def put_read(body: ReadPut, context: Context, principal: CurrentPrincipal) -> dict[str, Any]:
    user = _user(principal)
    return {"last_read_seq": await in_transaction(context, lambda c: store.mark_read(c, user, body.seq))}


@router.get("/v1/community/mutes", auth="browser", right="community:moderator", read_resource="community.mutes")
async def get_mutes(context: Context, principal: CurrentPrincipal) -> dict[str, Any]:
    user = _user(principal)

    def work(connection: Any) -> list[dict[str, str]]:
        store.require_moderator(connection, user)
        return store.mutes(connection)

    return {"mutes": await in_transaction(context, work)}


async def _mute(context: HubContext, principal: Principal, target_text: str, muted: bool) -> dict[str, Any]:
    user, now, target = _user(principal), context.clock(), path_id(target_text)

    def work(connection: Any) -> tuple[bool, int]:
        store.require_moderator(connection, user)
        return store.set_mute(connection, user, target, muted, now), store.head_seq(connection)

    changed, seq = await in_transaction(context, work)
    if changed:
        announce(Audience(kind="user", id=target), "mute", seq)
    return {"muted": muted}


@router.put("/v1/community/mutes/{user}", auth="browser", right="community:moderator")
async def put_mute(user: str, body: Empty, context: Context, principal: CurrentPrincipal) -> dict[str, Any]:
    return await _mute(context, principal, user, True)


@router.delete("/v1/community/mutes/{user}", auth="browser", right="community:moderator")
async def delete_mute(user: str, context: Context, principal: CurrentPrincipal) -> dict[str, Any]:
    return await _mute(context, principal, user, False)


@router.get("/v1/community/moderators", auth="root", right="root", read_resource="community.moderators")
async def get_moderators(context: Context) -> dict[str, Any]:
    return {"moderators": await in_transaction(context, store.moderators)}


@router.post("/v1/community/moderators", auth="root", right="root")
async def post_moderator(body: ModeratorPost, context: Context) -> dict[str, Any]:
    now, target = context.clock(), UUID(body.user)
    await in_transaction(context, lambda c: store.set_moderator(c, target, body.granted, now))
    return {"user": body.user, "moderator": body.granted}
