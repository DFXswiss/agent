"""Routes of the orgs area (03-api.md section 4.3).

`POST /v1/orgs`, `GET /v1/orgs/{org}`, `GET /v1/orgs/{org}/log` and `POST /v1/orgs/{org}/log`.

After a committed change the route publishes, through `agent_hub.stream.publish` only (ADR-027): the `log` frame
`{org_id, head}` to the org's browsers and the `log` hint `{org_id, seq}` to every active device of the org, the
`wraps` frame/hint `{}` to every recipient of a new wrap, and the announcement of every stored event
(`agent_hub.events.notify.announce`: `event` frame, `events` hints, signal `event.appended`); and it emits the signal
`log.appended` (`org_id`, `seq`) for the other areas (e.g. the scheduler after a `device_assign`).
"""

from __future__ import annotations

import asyncio
from typing import Any, cast
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, StrictInt

from agent_hub import signals
from agent_hub.errors import HubError
from agent_hub.events.notify import announce
from agent_hub.http.context import Context, CurrentPrincipal, HubContext
from agent_hub.http.routing import EVENT_BATCH_BYTES, HubRouter
from agent_hub.log.state import DEVICE
from agent_hub.notifications.rules import publish_rows
from agent_hub.orgs import logs, rights
from agent_hub.orgs.append import Appender, AppendResult, append_batch
from agent_hub.orgs.genesis import Created, create_org
from agent_hub.stream.publish import Audience, AudienceKind, publish

router = HubRouter()
LOG_PAGE_DEFAULT = 200
LOG_PAGE_MAX = 1000
INVITE_TYPES = frozenset({"invite", "invite_revoke"})


class CreateOrgRequest(BaseModel):
    """Body of `POST /v1/orgs`; entries, wraps and events are checked by the crypto layer."""

    model_config = ConfigDict(extra="forbid")
    genesis: dict[str, Any]
    wraps: list[dict[str, Any]] = Field(min_length=1, max_length=1000)
    events: list[dict[str, Any]] = Field(min_length=3, max_length=100)
    platform_invite_id: str | None = None


class AppendRequest(BaseModel):
    """Body of `POST /v1/orgs/{org}/log`."""

    model_config = ConfigDict(extra="forbid")
    expected_seq: StrictInt = Field(ge=0)
    entries: list[dict[str, Any]] = Field(min_length=1, max_length=1000)
    wraps: list[dict[str, Any]] = Field(max_length=10000)
    events: list[dict[str, Any]] = Field(max_length=100)


def parse_id(text: str) -> UUID | None:
    """A path ID in canonical UUID form, None otherwise."""
    try:
        value = UUID(text)
    except ValueError:
        return None
    return value if str(value) == text else None


def user_id(principal: CurrentPrincipal) -> UUID:
    """The user of a browser request (the pipeline admits only browser sessions to these routes)."""
    return cast(UUID, principal.principal_id)


def user_of(principal: CurrentPrincipal) -> Appender:
    """The appending user of a browser request."""
    return Appender(str(user_id(principal)), "user")


def publish_append(result: AppendResult) -> None:
    """The live frames and the signal of a committed append (module docstring)."""
    head = result.head
    publish(Audience("org", result.org_id), "log", {"org_id": str(result.org_id), "head": head.to_json()})
    for device in sorted(result.devices):
        publish(Audience("device", UUID(device)), "log", {"org_id": str(result.org_id), "seq": head.seq})
    state = result.log.state
    for recipient in sorted({slot.recipient for slot in result.batch.wraps}):
        kind: AudienceKind = "device" if state.principals[recipient].kind == DEVICE else "user"
        publish(Audience(kind, UUID(recipient)), "wraps", {})
    announce(result.events)
    publish_rows(result.notices)
    signals.emit("log.appended", org_id=result.org_id, seq=head.seq)


def _create(context: HubContext, appender: Appender, body: CreateOrgRequest) -> Created:
    with context.pool.connection() as connection, connection.transaction():
        return create_org(
            connection,
            appender=appender,
            genesis=body.genesis,
            wraps=body.wraps,
            events=body.events,
            platform_invite_id=body.platform_invite_id,
            now=context.clock(),
            root_key=context.config.platform_root_key,
        )


@router.post("/v1/orgs", auth="browser", right="user", max_body=EVENT_BATCH_BYTES)
async def post_orgs(context: Context, principal: CurrentPrincipal, body: CreateOrgRequest) -> dict[str, Any]:
    created = await asyncio.to_thread(_create, context, user_of(principal), body)
    if created.result is not None:
        publish_append(created.result)
    answer: dict[str, Any] = {"org_id": str(created.org_id), "default_wg": str(created.default_wg)}
    if created.replayed:
        answer["replayed"] = True
    return answer


def org_view(context: HubContext, org_id: UUID, user: UUID) -> dict[str, Any]:
    """The body of `GET /v1/orgs/{org}` from the projections."""
    with context.pool.connection() as connection:
        rights.require(connection, user, rights.ORG_MEMBER, org_id=org_id)
        org = cast(logs.OrgRow, logs.org_row(connection, org_id))  # an active member implies the org
        devices: dict[UUID, list[str]] = {}
        for wg_id, device_id in connection.execute(
            "SELECT d.wg_id, d.device_id FROM hub.wg_device d JOIN hub.wg w ON w.id = d.wg_id"
            " JOIN hub.org_member m ON m.org_id = w.org_id AND m.principal_id = d.device_id AND m.removed_seq IS NULL"
            " WHERE w.org_id = %s AND d.unassigned_seq IS NULL ORDER BY d.assigned_seq DESC, d.device_id DESC",
            (org_id,),
        ):
            devices.setdefault(wg_id, []).append(str(device_id))
        workgroups = [
            {
                "wg_id": str(row[0]),
                "is_default": row[1],
                "archived": row[2],
                "my_access": row[3],
                "devices": devices.get(row[0], []),
                "epoch": row[4],
            }
            for row in connection.execute(
                "SELECT w.id, w.is_default, w.archived_seq IS NOT NULL, a.access,"
                " (SELECT max(e.epoch) FROM hub.scope_epoch e WHERE e.scope = 'wg' AND e.scope_id = w.id)"
                " FROM hub.wg w LEFT JOIN hub.wg_access a ON a.wg_id = w.id AND a.user_id = %s"
                " AND a.revoked_seq IS NULL WHERE w.org_id = %s ORDER BY w.created_seq DESC, w.id DESC",
                (user, org_id),
            )
        ]
        members = [
            {"principal_id": str(row[0]), "kind": row[1], "role": row[2], "joined_seq": row[3]}
            for row in connection.execute(
                "SELECT principal_id, kind, role, joined_seq FROM hub.org_member WHERE org_id = %s"
                " AND removed_seq IS NULL ORDER BY joined_seq DESC, principal_id DESC",
                (org_id,),
            )
        ]
    return {
        "org_id": str(org.id),
        "private": org.private,
        "platform_access": org.platform_access,
        "default_wg": str(org.default_wg),
        "log_head": {"seq": org.head_seq, "hash": org.head_hash},
        "workgroups": workgroups,
        "members": members,
        "rotation_pending": [{"scope": scope, "scope_id": scope_id} for scope, scope_id in org.rotation_pending],
    }


def _org_id(org: str) -> UUID:
    found = parse_id(org)
    if found is None:
        raise HubError("not_found")
    return found


@router.get("/v1/orgs/{org}", auth="browser", right="org:member", read_resource="org")
async def get_org(context: Context, principal: CurrentPrincipal, org: str) -> dict[str, Any]:
    org_id = _org_id(org)
    return await asyncio.to_thread(org_view, context, org_id, user_id(principal))


def _page(text: str | None, default: int, minimum: int, maximum: int) -> int:
    if text is None:
        return default
    if not (text.isascii() and text.isdigit()) or text != str(int(text)) or not minimum <= int(text) <= maximum:
        raise HubError("request_invalid")
    return int(text)


def log_page(context: HubContext, org_id: UUID, user: UUID, after: int, limit: int) -> dict[str, Any]:
    """The body of `GET /v1/orgs/{org}/log`: head and entries from one snapshot (an append committed between the two
    reads would otherwise serve entries beyond the head, which the browser refuses as a broken chain)."""
    with context.pool.connection() as connection, connection.transaction():
        connection.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ")
        rights.require(connection, user, rights.ORG_MEMBER, org_id=org_id)
        org = cast(logs.OrgRow, logs.org_row(connection, org_id))  # an active member implies the org
        found = logs.entries(connection, org_id, after, limit + 1)
    page = found[:limit]
    items = [{"seq": entry["body"]["seq"], "entry": entry} for entry in page]
    more = len(found) > limit
    return {
        "entries": items,
        "head": {"seq": org.head_seq, "hash": org.head_hash},
        "next_after": items[-1]["seq"] if more else None,
    }


@router.get("/v1/orgs/{org}/log", auth="browser", right="org:member", read_resource="org.log")
async def get_log(
    context: Context, principal: CurrentPrincipal, org: str, after: str | None = None, limit: str | None = None
) -> dict[str, Any]:
    org_id = _org_id(org)
    after_seq = _page(after, -1, 0, 2**53 - 1)
    size = _page(limit, LOG_PAGE_DEFAULT, 1, LOG_PAGE_MAX)
    return await asyncio.to_thread(log_page, context, org_id, user_id(principal), after_seq, size)


def browser_refuses(body: object) -> bool:
    """Entries a browser never appends here: `invite`/`invite_revoke` (only with their invitation row, HUB-11) and a
    device `accept` (only through the enrollment of the device, HUB-08)."""
    if not isinstance(body, dict):
        return False
    return body.get("type") in INVITE_TYPES or (body.get("type") == "accept" and body.get("kind") != "user")


def _append(context: HubContext, org_id: UUID, appender: Appender, body: AppendRequest) -> AppendResult:
    with context.pool.connection() as connection, connection.transaction():
        return append_batch(
            connection,
            org_id=org_id,
            appender=appender,
            expected_seq=body.expected_seq,
            entries=body.entries,
            wraps=body.wraps,
            events=body.events,
            now=context.clock(),
            root_key=context.config.platform_root_key,
        )


@router.post("/v1/orgs/{org}/log", auth="browser", right="per_entry_type", max_body=EVENT_BATCH_BYTES)
async def post_log(context: Context, principal: CurrentPrincipal, org: str, body: AppendRequest) -> dict[str, Any]:
    org_id = _org_id(org)
    for index, entry in enumerate(body.entries):
        if browser_refuses(entry.get("body")):
            raise HubError("request_invalid", detail={"pointers": [f"/entries/{index}"]})
    result = await asyncio.to_thread(_append, context, org_id, user_of(principal), body)
    publish_append(result)
    return {"head": result.head.to_json()}
