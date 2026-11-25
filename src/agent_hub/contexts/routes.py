"""Routes of the context checkpoints and pull-request links (03-api.md sections 4.6 and 6.1, ADR-072).

| Route | Right |
|---|---|
| `PUT /v1/device/contexts/{object}/chunks/{index}`, `POST /v1/device/contexts/{object}/commit` | `device:session` |
| `GET /v1/device/contexts/{object}`, `GET /v1/device/contexts/{object}/chunks/{index}` | `device:wg` |
| `POST /v1/device/pr-links` | `device:session` |
| `GET /v1/tasks/{task}/contexts` | `task:read` |
| `GET /v1/tasks/{task}/logs/{object}`, `GET /v1/tasks/{task}/logs/{object}/chunks/{index}` (KOR-43) | `task:read` |
| `GET /v1/workgroups/{wg}/pr-links/{epoch}/{tag}`, `GET /v1/workgroups/{wg}/pr-links` | `wg:read` |
| `POST /v1/workgroups/{wg}/pr-links`, `DELETE /v1/workgroups/{wg}/pr-links/{epoch}/{tag}` | `wg:write` |

The rules live in `agent_hub.contexts.store`; a chunk refused for a quota is audited (`session.context_refused`) in a
transaction of its own after the refused one was rolled back. A malformed path ID, index, epoch or tag names nothing
(`404 not_found`); a malformed query parameter of the list is `400 request_invalid` with its pointer. A new link of a
person answers `201`, one that exists already `200`. Browser routes are open to the user's agent like every browser
route (ADR-056, `agent_hub.agents.scope`).
A chunk upload carries up to 2 MiB of ciphertext as b64u, so it may use the batch body limit (4 MiB). Device routes
read the device's view from the verified logs (`agent_hub.device.access`) in their transaction.
"""

from __future__ import annotations

import asyncio
import functools
import re
from collections.abc import Callable
from datetime import datetime
from typing import Annotated, Any, Literal, cast
from uuid import UUID

from fastapi import Response
from pydantic import BaseModel, ConfigDict, Field, StringConstraints

from agent_hub.contexts import store
from agent_hub.crypto.encoding import b64u_decode
from agent_hub.crypto.errors import CryptoError
from agent_hub.db.pool import transaction
from agent_hub.device.access import DeviceView, device_view
from agent_hub.device.keys import ActiveDevice, device_id
from agent_hub.errors import HubError
from agent_hub.events.routes import parse_count
from agent_hub.http.context import Context, CurrentPrincipal, HubContext
from agent_hub.http.routing import EVENT_BATCH_BYTES, HubRouter
from agent_hub.orgs import rights
from agent_hub.workgroups.routes import path_id

router = HubRouter()
INDEX_RE = re.compile(r"0|[1-9][0-9]{0,5}")
EPOCH_RE = re.compile(r"[1-9][0-9]{0,9}")
TAG_RE = re.compile(r"[A-Za-z0-9_-]{42}[AEIMQUYcgkosw048]")
MAX_EPOCH = 2**31 - 1
LIST_MAX = 1000
UUID7_RE = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-7[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}")
CURSOR_RE = re.compile(r"[A-Za-z0-9_-]{1,256}")
Uuid7 = Annotated[str, StringConstraints(pattern=f"^{UUID7_RE.pattern}$")]
Tag = Annotated[str, StringConstraints(pattern=r"^[A-Za-z0-9_-]{42}[AEIMQUYcgkosw048]$")]
Sealed = Annotated[str, StringConstraints(pattern=r"^[A-Za-z0-9_-]{378}[AEIMQUYcgkosw048]$")]
"""b64u of the 284 bytes of a sealed link body (04-crypto.md section 4.11a)."""


class ChunkRequest(BaseModel):
    """Body of `PUT /v1/device/contexts/{object}/chunks/{index}`."""

    model_config = ConfigDict(extra="forbid", strict=True)
    session_id: Uuid7
    ct: str
    kind: Literal["context", "log"] = "context"


class CommitRequest(BaseModel):
    """Body of `POST /v1/device/contexts/{object}/commit` (the manifest is checked by the crypto layer)."""

    model_config = ConfigDict(extra="forbid", strict=True)
    manifest: dict[str, Any]
    sig: str = Field(max_length=128)


class PrLinkRequest(BaseModel):
    """Body of `POST /v1/device/pr-links`."""

    model_config = ConfigDict(extra="forbid", strict=True)
    session_id: Uuid7
    epoch: int = Field(ge=1, le=MAX_EPOCH)
    tag: Tag
    sealed: Sealed | None = None


class LinkRequest(BaseModel):
    """Body of `POST /v1/workgroups/{wg}/pr-links`."""

    model_config = ConfigDict(extra="forbid", strict=True)
    epoch: int = Field(ge=1, le=MAX_EPOCH)
    tag: Tag
    sealed: Sealed
    task_id: Uuid7


def _number(text: str, pattern: re.Pattern[str]) -> int:
    """A path number; anything else names nothing (`404 not_found`)."""
    if pattern.fullmatch(text) is None:
        raise HubError("not_found")
    return int(text)


def _limits(context: HubContext) -> store.Limits:
    config = context.config
    return store.Limits(
        max_object_bytes=config.context_max_object_bytes,
        org_bytes=config.context_org_bytes,
        keep=config.context_keep,
        log_org_bytes=config.log_org_bytes,
    )


async def _device_work[T](
    context: HubContext, principal: Any, work: Callable[[Any, DeviceView, UUID], T], *, read: bool = False
) -> T:
    """`work(connection, view, device)` in one transaction of a worker thread (repeatable read for reads)."""
    device = device_id(principal)
    root = context.config.platform_root_key

    def run() -> T:
        with context.pool.connection() as connection, connection.transaction():
            if read:
                connection.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ")
            return work(connection, device_view(connection, device, root), device)

    return await asyncio.to_thread(run)


@router.put(
    "/v1/device/contexts/{object}/chunks/{index}", auth="device", right="device:session", max_body=EVENT_BATCH_BYTES
)
async def put_chunk(
    context: Context, principal: ActiveDevice, object: str, index: str, body: ChunkRequest
) -> dict[str, Any]:
    object_id, number = path_id(object), _number(index, INDEX_RE)
    try:
        ct = b64u_decode(body.ct)
    except CryptoError:
        raise HubError("request_invalid", detail={"pointers": ["/ct"]}) from None
    now, limits = context.clock(), _limits(context)
    try:
        replayed = await _device_work(
            context,
            principal,
            lambda connection, view, device: store.put_chunk(
                connection,
                view,
                device,
                object_id=object_id,
                index=number,
                session_id=body.session_id,
                ct=ct,
                now=now,
                limits=limits,
                kind=body.kind,
            ),
        )
    except store.QuotaRefused as refused:
        await asyncio.to_thread(_record_refusal, context, device_id(principal), refused, now)
        raise
    return {"replayed": replayed}


def _record_refusal(context: HubContext, device: UUID, refused: store.QuotaRefused, now: datetime) -> None:
    with transaction(context.pool) as connection:
        store.record_refusal(connection, device, refused, now)


@router.post("/v1/device/contexts/{object}/commit", auth="device", right="device:session")
async def post_commit(context: Context, principal: ActiveDevice, object: str, body: CommitRequest) -> dict[str, Any]:
    object_id = path_id(object)
    now, keep, log_keep = context.clock(), context.config.context_keep, context.config.log_keep
    return await _device_work(
        context,
        principal,
        lambda connection, view, device: store.commit(
            connection,
            view,
            device,
            object_id=object_id,
            manifest_value=body.manifest,
            sig=body.sig,
            now=now,
            keep=keep,
            log_keep=log_keep,
        ),
    )


@router.get("/v1/device/contexts/{object}", auth="device", right="device:wg", read_resource="device.context")
async def get_object(context: Context, principal: ActiveDevice, object: str) -> dict[str, Any]:
    object_id = path_id(object)
    return await _device_work(
        context, principal, lambda connection, view, _device: store.get_object(connection, view, object_id), read=True
    )


@router.get(
    "/v1/device/contexts/{object}/chunks/{index}",
    auth="device",
    right="device:wg",
    read_resource="device.context.chunk",
)
async def get_chunk(context: Context, principal: ActiveDevice, object: str, index: str) -> dict[str, Any]:
    object_id, number = path_id(object), _number(index, INDEX_RE)
    return await _device_work(
        context,
        principal,
        lambda connection, view, _device: store.get_chunk(connection, view, object_id, number),
        read=True,
    )


@router.post("/v1/device/pr-links", auth="device", right="device:session")
async def post_pr_link(context: Context, principal: ActiveDevice, body: PrLinkRequest) -> dict[str, Any]:
    now = context.clock()
    replayed = await _device_work(
        context,
        principal,
        lambda connection, view, device: store.link_pr(
            connection,
            view,
            device,
            session_id=body.session_id,
            epoch=body.epoch,
            tag=body.tag,
            sealed=body.sealed,
            now=now,
        ),
    )
    return {"replayed": replayed}


def _task_contexts(context: HubContext, user: UUID, task: UUID) -> dict[str, Any]:
    with context.pool.connection() as connection:
        found = connection.execute("SELECT wg_id FROM hub.task WHERE id = %s", (task,)).fetchone()
        if found is None:
            raise HubError("not_found")
        rights.require(connection, user, rights.TASK_READ, wg_id=found[0])
        return store.task_contexts(connection, task, LIST_MAX)


@router.get("/v1/tasks/{task}/contexts", auth="browser", right="task:read", read_resource="task.contexts")
async def get_task_contexts(context: Context, principal: CurrentPrincipal, task: str) -> dict[str, Any]:
    user = cast(UUID, principal.principal_id)
    return await asyncio.to_thread(_task_contexts, context, user, path_id(task))


def _task_log(context: HubContext, user: UUID, task: UUID, read: Callable[[Any], dict[str, Any]]) -> dict[str, Any]:
    with context.pool.connection() as connection:
        found = connection.execute("SELECT wg_id FROM hub.task WHERE id = %s", (task,)).fetchone()
        if found is None:
            raise HubError("not_found")
        rights.require(connection, user, rights.TASK_READ, wg_id=found[0])
        return read(connection)


@router.get("/v1/tasks/{task}/logs/{object}", auth="browser", right="task:read", read_resource="task.log")
async def get_task_log(context: Context, principal: CurrentPrincipal, task: str, object: str) -> dict[str, Any]:
    user = cast(UUID, principal.principal_id)
    task_id, object_id = path_id(task), path_id(object)
    return await asyncio.to_thread(
        _task_log, context, user, task_id, lambda connection: store.task_log(connection, task_id, object_id)
    )


@router.get(
    "/v1/tasks/{task}/logs/{object}/chunks/{index}", auth="browser", right="task:read", read_resource="task.log.chunk"
)
async def get_task_log_chunk(
    context: Context, principal: CurrentPrincipal, task: str, object: str, index: str
) -> dict[str, Any]:
    user = cast(UUID, principal.principal_id)
    task_id, object_id, number = path_id(task), path_id(object), _number(index, INDEX_RE)
    return await asyncio.to_thread(
        _task_log,
        context,
        user,
        task_id,
        lambda connection: store.task_log_chunk(connection, task_id, object_id, number),
    )


def _pr_link(context: HubContext, user: UUID, wg: UUID, epoch: int, tag: str) -> dict[str, Any]:
    with context.pool.connection() as connection:
        rights.require(connection, user, rights.WG_READ, wg_id=wg)
        return store.pr_link(connection, wg, epoch, tag)


def _link_path(wg: str, epoch: str, tag: str) -> tuple[UUID, int, str]:
    wg_id, number = path_id(wg), _number(epoch, EPOCH_RE)
    if number > MAX_EPOCH or TAG_RE.fullmatch(tag) is None:
        raise HubError("not_found")
    return wg_id, number, tag


@router.get(
    "/v1/workgroups/{wg}/pr-links/{epoch}/{tag}", auth="browser", right="wg:read", read_resource="workgroup.pr_link"
)
async def get_pr_link(context: Context, principal: CurrentPrincipal, wg: str, epoch: str, tag: str) -> dict[str, Any]:
    user = cast(UUID, principal.principal_id)
    return await asyncio.to_thread(_pr_link, context, user, *_link_path(wg, epoch, tag))


def _query(name: str, value: str | None, pattern: re.Pattern[str]) -> str | None:
    if value is not None and pattern.fullmatch(value) is None:
        raise HubError("request_invalid", detail={"pointers": [f"/{name}"]})
    return value


def _pr_links(
    context: HubContext, user: UUID, wg: UUID, *, task: str | None, cursor: str | None, limit: int
) -> dict[str, Any]:
    with context.pool.connection() as connection:
        rights.require(connection, user, rights.WG_READ, wg_id=wg)
        task_id = None if task is None else UUID(task)
        return store.list_links(connection, wg, task_id=task_id, cursor=cursor, limit=limit)


@router.get("/v1/workgroups/{wg}/pr-links", auth="browser", right="wg:read", read_resource="workgroup.pr_links")
async def get_pr_links(
    context: Context,
    principal: CurrentPrincipal,
    wg: str,
    *,
    task: str | None = None,
    cursor: str | None = None,
    limit: str | None = None,
) -> dict[str, Any]:
    user, wg_id = cast(UUID, principal.principal_id), path_id(wg)
    found_task, found_cursor = _query("task", task, UUID7_RE), _query("cursor", cursor, CURSOR_RE)
    count = parse_count(limit, store.LIST_DEFAULT, 1, store.LIST_MAX)
    work = functools.partial(_pr_links, context, user, wg_id, task=found_task, cursor=found_cursor, limit=count)
    return await asyncio.to_thread(work)


def _link(context: HubContext, user: UUID, wg: UUID, body: LinkRequest, now: datetime) -> tuple[bool, dict[str, Any]]:
    with transaction(context.pool) as connection:
        rights.require(connection, user, rights.WG_WRITE, wg_id=wg, write=True)
        return store.link_by_user(
            connection,
            user,
            wg,
            epoch=body.epoch,
            tag=body.tag,
            sealed=body.sealed,
            task_id=body.task_id,
            now=now,
        )


@router.post("/v1/workgroups/{wg}/pr-links", auth="browser", right="wg:write")
async def post_pr_links(
    context: Context, principal: CurrentPrincipal, response: Response, wg: str, body: LinkRequest
) -> dict[str, Any]:
    user, wg_id = cast(UUID, principal.principal_id), path_id(wg)
    created, link = await asyncio.to_thread(_link, context, user, wg_id, body, context.clock())
    if created:
        response.status_code = 201
    return {"created": created, "link": link}


def _unlink(context: HubContext, user: UUID, link: tuple[UUID, int, str], now: datetime) -> None:
    wg, epoch, tag = link
    with transaction(context.pool) as connection:
        rights.require(connection, user, rights.WG_WRITE, wg_id=wg, write=True)
        store.unlink(connection, user, wg, epoch=epoch, tag=tag, now=now)


@router.delete("/v1/workgroups/{wg}/pr-links/{epoch}/{tag}", auth="browser", right="wg:write")
async def delete_pr_link(
    context: Context, principal: CurrentPrincipal, wg: str, epoch: str, tag: str
) -> dict[str, Any]:
    user = cast(UUID, principal.principal_id)
    await asyncio.to_thread(_unlink, context, user, _link_path(wg, epoch, tag), context.clock())
    return {"deleted": True}
