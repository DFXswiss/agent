"""Routes of the attachments of prompts (03-api.md sections 4.6 and 6.1, ADR-102 section 3).

| Route | Right |
|---|---|
| `PUT /v1/workgroups/{wg}/attachments/{object}/chunks/{index}`, `POST …/attachments/{object}/commit` | `task:write` |
| `GET /v1/workgroups/{wg}/attachments/{object}/chunks/{index}` | `wg:read` |
| `GET /v1/device/attachments/{object}/chunks/{index}` | `device:wg` |

The rules live in `agent_hub.attachments.store`. Uploading needs the right to write prompts of the workgroup's tasks
(`task:write`, which a support person of a released workgroup holds as well: support writes prompts, ADR-096). A
malformed path ID or an index at or above `MAX_CHUNKS` names nothing (`404 not_found`). A chunk upload carries up to
1 MiB of ciphertext as b64u, so it may use the batch body limit (4 MiB). Browser routes are open to the user's agent
like every browser route (ADR-056, `agent_hub.agents.scope`).
"""

from __future__ import annotations

import asyncio
import re
from collections.abc import Callable
from typing import Annotated, Any, cast
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, StringConstraints

from agent_hub.attachments import store
from agent_hub.crypto import attachment
from agent_hub.crypto.encoding import b64u_decode
from agent_hub.crypto.errors import CryptoError
from agent_hub.db.pool import transaction
from agent_hub.device.access import device_view
from agent_hub.device.keys import ActiveDevice, device_id
from agent_hub.errors import HubError
from agent_hub.http.context import Context, CurrentPrincipal, HubContext
from agent_hub.http.routing import EVENT_BATCH_BYTES, HubRouter
from agent_hub.orgs import rights
from agent_hub.workgroups.routes import path_id

router = HubRouter()
INDEX_RE = re.compile(r"0|[1-9][0-9]?")
Digest = Annotated[str, StringConstraints(pattern=r"^[A-Za-z0-9_-]{42}[AEIMQUYcgkosw048]$")]


class ChunkRequest(BaseModel):
    """Body of `PUT /v1/workgroups/{wg}/attachments/{object}/chunks/{index}`."""

    model_config = ConfigDict(extra="forbid", strict=True)
    ct: str


class CommitRequest(BaseModel):
    """Body of `POST /v1/workgroups/{wg}/attachments/{object}/commit`."""

    model_config = ConfigDict(extra="forbid", strict=True)
    chunks: int = Field(ge=1, le=attachment.MAX_CHUNKS)
    size: int = Field(ge=attachment.MIN_CT_BYTES, le=store.MAX_OBJECT_CT_BYTES)
    digest: Digest


def _index(text: str) -> int:
    """A chunk index below `MAX_CHUNKS`; anything else names nothing (`404 not_found`)."""
    if INDEX_RE.fullmatch(text) is None or int(text) >= attachment.MAX_CHUNKS:
        raise HubError("not_found")
    return int(text)


def _user_work[T](context: HubContext, user: UUID, wg: UUID, right: str, work: Callable[[Any], T]) -> T:
    """`work(connection)` in one transaction after the right on the workgroup (`task:write` or `wg:read`)."""
    with transaction(context.pool) as connection:
        if right == rights.TASK_WRITE:
            rights.require(connection, user, right, wg_id=wg, write=True, support_write=True)
        else:
            rights.require(connection, user, right, wg_id=wg)
        return work(connection)


@router.put(
    "/v1/workgroups/{wg}/attachments/{object}/chunks/{index}",
    auth="browser",
    right="task:write",
    max_body=EVENT_BATCH_BYTES,
)
async def put_chunk(
    context: Context, principal: CurrentPrincipal, *, wg: str, object: str, index: str, body: ChunkRequest
) -> dict[str, Any]:
    user, wg_id, object_id, number = cast(UUID, principal.principal_id), path_id(wg), path_id(object), _index(index)
    try:
        ct = b64u_decode(body.ct)
    except CryptoError:
        raise HubError("request_invalid", detail={"pointers": ["/ct"]}) from None
    now, limit = context.clock(), context.config.attachment_org_bytes
    replayed = await asyncio.to_thread(
        _user_work,
        context,
        user,
        wg_id,
        rights.TASK_WRITE,
        lambda connection: store.put_chunk(
            connection, user, wg_id, object_id=object_id, index=number, ct=ct, now=now, org_limit=limit
        ),
    )
    return {"replayed": replayed}


@router.post("/v1/workgroups/{wg}/attachments/{object}/commit", auth="browser", right="task:write")
async def post_commit(
    context: Context, principal: CurrentPrincipal, wg: str, object: str, body: CommitRequest
) -> dict[str, Any]:
    user, wg_id, object_id = cast(UUID, principal.principal_id), path_id(wg), path_id(object)
    now, values = context.clock(), body.model_dump()
    return await asyncio.to_thread(
        _user_work,
        context,
        user,
        wg_id,
        rights.TASK_WRITE,
        lambda connection: store.commit(connection, user, wg_id, object_id=object_id, values=values, now=now),
    )


@router.get(
    "/v1/workgroups/{wg}/attachments/{object}/chunks/{index}",
    auth="browser",
    right="wg:read",
    read_resource="workgroup.attachment.chunk",
)
async def get_chunk(context: Context, principal: CurrentPrincipal, wg: str, object: str, index: str) -> dict[str, Any]:
    user, wg_id, object_id, number = cast(UUID, principal.principal_id), path_id(wg), path_id(object), _index(index)
    return await asyncio.to_thread(
        _user_work,
        context,
        user,
        wg_id,
        rights.WG_READ,
        lambda connection: store.chunk(connection, wg_id, object_id, number),
    )


def _device_chunk(context: HubContext, device: UUID, object_id: UUID, index: int) -> dict[str, Any]:
    with context.pool.connection() as connection, connection.transaction():
        connection.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ")
        view = device_view(connection, device, context.config.platform_root_key)
        return store.device_chunk(connection, view, object_id, index)


@router.get(
    "/v1/device/attachments/{object}/chunks/{index}",
    auth="device",
    right="device:wg",
    read_resource="device.attachment.chunk",
)
async def get_device_chunk(context: Context, principal: ActiveDevice, object: str, index: str) -> dict[str, Any]:
    object_id, number = path_id(object), _index(index)
    return await asyncio.to_thread(_device_chunk, context, device_id(principal), object_id, number)
