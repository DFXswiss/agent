"""Routes of the device API (03-api.md section 6.1, without outbox, operations, credentials and requests).

| Route | Right | Module |
|---|---|---|
| `POST /v1/device/enroll` | proof of possession + invitation | `enroll` |
| `GET /v1/device/state` | `device:any` | `state` |
| `GET /v1/device/logs?org&after&limit`, `GET /v1/device/platform-log?after&limit` | `device` | `logs` |
| `GET/POST /v1/device/wraps` | `device` | `wraps` |
| `GET /v1/device/pull?cursor&kinds&limit` | `device:wg` / `device:org` | `pull` |
| `GET /v1/device/workgroups/{wg}/tasks?state&before&limit`, `GET /v1/device/tasks/{task}` | `device:wg` | `tasks` |

Query values follow `contracts/api/device-*.request.json`; anything else is `400 request_invalid` with the pointer of
the parameter. Reads run in one repeatable-read transaction in a worker thread (a consistent snapshot of log heads,
assignments and rows). After a committed change the route publishes through `agent_hub.stream.publish` only: an org
enrollment the frames of its log append (`agent_hub.orgs.routes.publish_append`, which also emits `log.appended` and
thereby the `reconcile` hints of `agent_hub.device.signals`), stored wraps the `wraps` frame/hint. Every GET is read-
logged by the pipeline (`read.device.*`).
"""

from __future__ import annotations

import asyncio
import re
from collections.abc import Callable
from typing import Annotated, Any
from uuid import UUID

from fastapi import Query, Request
from pydantic import BaseModel, ConfigDict, Field

from agent_hub.auth.gate import verify_enrollment
from agent_hub.auth.keys import Principal, ed25519_verifier
from agent_hub.device import enroll, logs, pull, state, tasks, wraps
from agent_hub.device.access import device_view
from agent_hub.device.keys import ACTIVE, PENDING, ActiveDevice, AnyDevice, device_id, key_id_of
from agent_hub.errors import HubError
from agent_hub.http.context import Context, HubContext
from agent_hub.http.routing import HubRouter
from agent_hub.orgs.routes import publish_append

router = HubRouter()
SEQ_RE = re.compile(r"0|[1-9][0-9]{0,15}")
LOG_LIMIT_RE = re.compile(r"[1-9][0-9]{0,2}|1000")
LIST_LIMIT_RE = re.compile(r"[1-9][0-9]?|1[0-9]{2}|200")
EPOCH_RE = re.compile(r"[1-9][0-9]{0,9}")
UUID7_RE = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-7[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}")
CURSOR_RE = re.compile(rf"({UUID7_RE.pattern}):({SEQ_RE.pattern})")
KINDS_RE = re.compile(r"[a-z_]+(\.[a-z_]+)?(,[a-z_]+(\.[a-z_]+)?){0,31}")
BEFORE_RE = re.compile(r"[A-Za-z0-9_-]{1,256}")
KEY_SCOPES = frozenset({"org", "wg", "cred"})
MAX_CURSORS = 10000
MAX_EPOCH = 2**31 - 1


def _invalid(name: str) -> HubError:
    return HubError("request_invalid", detail={"pointers": [f"/{name}"]})


def _text(name: str, value: str | None, pattern: re.Pattern[str]) -> str | None:
    if value is not None and (len(value) > 1024 or pattern.fullmatch(value) is None):
        raise _invalid(name)
    return value


def _number(name: str, value: str | None, pattern: re.Pattern[str], default: int) -> int:
    found = _text(name, value, pattern)
    return default if found is None else int(found)


def _uuid(name: str, value: str | None) -> UUID | None:
    found = _text(name, value, UUID7_RE)
    return None if found is None else UUID(found)


def _path_id(value: str) -> UUID:
    """A path ID; anything but a UUIDv7 names nothing the device may read (`404 not_found`)."""
    if UUID7_RE.fullmatch(value) is None:
        raise HubError("not_found")
    return UUID(value)


async def _read[T](context: HubContext, work: Callable[[Any], T]) -> T:
    """`work(connection)` in a worker thread inside one repeatable-read transaction."""

    def run() -> T:
        with context.pool.connection() as connection, connection.transaction():
            connection.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ")
            return work(connection)

    return await asyncio.to_thread(run)


async def _write[T](context: HubContext, work: Callable[[Any], T]) -> T:
    """`work(connection)` in a worker thread inside one transaction."""

    def run() -> T:
        with context.pool.connection() as connection, connection.transaction():
            return work(connection)

    return await asyncio.to_thread(run)


@router.post("/v1/device/enroll", auth="device_enroll", right="invite_proof")
async def post_enroll(request: Request, context: Context, body: enroll.EnrollRequest) -> dict[str, Any]:
    body.check_form()
    principal = Principal(
        kind="device",
        key_id=key_id_of(body.device_id),
        principal_id=UUID(body.device_id),
        actor_kind="device",
        attributes={"state": ACTIVE if body.kind == "org" else PENDING},
    )
    await verify_enrollment(request, context, ed25519_verifier(body.sig_public()), principal)
    now, root = context.clock(), context.config.platform_root_key
    handler = enroll.enroll_org if body.kind == "org" else enroll.enroll_platform
    enrolled = await _write(context, lambda connection: handler(connection, body, now=now, root_key=root))
    if enrolled.append is not None:
        publish_append(enrolled.append)
    return enrolled.answer


@router.get("/v1/device/state", auth="device", right="device:any", read_resource="device.state")
async def get_state(context: Context, principal: AnyDevice) -> dict[str, Any]:
    now = context.clock()
    if principal.attributes.get("state") != ACTIVE:
        return state.pending_state(now)
    device = device_id(principal)
    return await _read(context, lambda connection: state.active_state(connection, device, context.config, now))


@router.get("/v1/device/logs", auth="device", right="device", read_resource="device.logs")
async def get_logs(
    context: Context,
    principal: ActiveDevice,
    org: str | None = None,
    after: str | None = None,
    limit: str | None = None,
) -> dict[str, Any]:
    org_id = _uuid("org", org)
    if org_id is None:
        raise _invalid("org")
    start = _number("after", after, SEQ_RE, -1)
    size = _number("limit", limit, LOG_LIMIT_RE, logs.PAGE_DEFAULT)
    device = device_id(principal)
    root = context.config.platform_root_key

    def work(connection: Any) -> dict[str, Any]:
        return logs.org_log_page(connection, device_view(connection, device, root), org_id, start, size)

    return await _read(context, work)


@router.get("/v1/device/platform-log", auth="device", right="device", read_resource="device.platform_log")
async def get_platform_log(
    context: Context, principal: ActiveDevice, after: str | None = None, limit: str | None = None
) -> dict[str, Any]:
    start = _number("after", after, SEQ_RE, -1)
    size = _number("limit", limit, LOG_LIMIT_RE, logs.PAGE_DEFAULT)
    return await _read(context, lambda connection: logs.platform_log_page(connection, start, size))


@router.get("/v1/device/wraps", auth="device", right="device", read_resource="device.wraps")
async def get_wraps(
    context: Context,
    principal: ActiveDevice,
    scope: str | None = None,
    scope_id: str | None = None,
    epoch: str | None = None,
) -> dict[str, Any]:
    if scope is not None and scope not in KEY_SCOPES:
        raise _invalid("scope")
    scope_uuid = _uuid("scope_id", scope_id)
    number = None if epoch is None else _number("epoch", epoch, EPOCH_RE, 0)
    if number is not None and number > MAX_EPOCH:
        raise _invalid("epoch")
    device = device_id(principal)
    found = await _read(
        context,
        lambda connection: wraps.list_wraps(
            connection,
            device,
            scope=scope,
            scope_id=None if scope_uuid is None else str(scope_uuid),
            epoch=number,
        ),
    )
    return {"wraps": found}


class WrapsRequest(BaseModel):
    """Body of `POST /v1/device/wraps`; every wrap is checked by the wrap rules (W3)."""

    model_config = ConfigDict(extra="forbid")
    wraps: list[dict[str, Any]] = Field(min_length=1, max_length=1000)


@router.post("/v1/device/wraps", auth="device", right="device")
async def post_wraps(context: Context, principal: ActiveDevice, body: WrapsRequest) -> dict[str, Any]:
    device = device_id(principal)
    now, root = context.clock(), context.config.platform_root_key
    stored, kinds = await _write(
        context, lambda connection: wraps.store(connection, device, body.wraps, now=now, root_key=root)
    )
    wraps.publish_wraps(stored, kinds)
    return {"stored": stored.stored, "ignored": stored.ignored}


def _cursors(values: list[str]) -> list[pull.Cursor]:
    if len(values) > MAX_CURSORS:
        raise _invalid("cursor")
    found: list[pull.Cursor] = []
    seen: set[str] = set()
    for value in values:
        match = CURSOR_RE.fullmatch(value)
        if match is None or match.group(1) in seen:
            raise _invalid("cursor")
        seen.add(match.group(1))
        found.append(pull.Cursor(UUID(match.group(1)), int(match.group(2))))
    return found


@router.get("/v1/device/pull", auth="device", right=("device:wg", "device:org"), read_resource="device.pull")
async def get_pull(
    context: Context,
    principal: ActiveDevice,
    cursor: Annotated[list[str] | None, Query()] = None,
    kinds: str | None = None,
    limit: str | None = None,
) -> dict[str, Any]:
    cursors = _cursors(cursor or [])
    selected = _text("kinds", kinds, KINDS_RE)
    size = _number("limit", limit, LOG_LIMIT_RE, pull.PAGE_DEFAULT)
    device = device_id(principal)
    root = context.config.platform_root_key
    names = None if selected is None else selected.split(",")

    def work(connection: Any) -> dict[str, Any]:
        view = device_view(connection, device, root)
        return pull.pull(connection, view, cursors, kinds=names, limit=size)

    return await _read(context, work)


@router.get(
    "/v1/device/workgroups/{wg}/tasks", auth="device", right="device:wg", read_resource="device.workgroup.tasks"
)
async def get_workgroup_tasks(
    context: Context,
    principal: ActiveDevice,
    *,
    wg: str,
    state: str | None = None,
    before: str | None = None,
    limit: str | None = None,
) -> dict[str, Any]:
    wg_id = _path_id(wg)
    if state is not None and state not in tasks.TASK_STATES:
        raise _invalid("state")
    cursor = _text("before", before, BEFORE_RE)
    size = _number("limit", limit, LIST_LIMIT_RE, tasks.LIST_DEFAULT)
    device = device_id(principal)
    root = context.config.platform_root_key

    def work(connection: Any) -> dict[str, Any]:
        view = device_view(connection, device, root)
        return tasks.list_tasks(connection, view, wg_id, state=state, before=cursor, limit=size)

    return await _read(context, work)


@router.get("/v1/device/tasks/{task}", auth="device", right="device:wg", read_resource="device.task")
async def get_task(context: Context, principal: ActiveDevice, task: str) -> dict[str, Any]:
    task_id = _path_id(task)
    device = device_id(principal)
    root = context.config.platform_root_key

    def work(connection: Any) -> dict[str, Any]:
        return tasks.get_task(connection, device_view(connection, device, root), task_id)

    return await _read(context, work)
