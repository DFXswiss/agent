"""Device routes of the process analysis reads (KOR-53, ADR-097 section 4; 03-api.md section 6.1).

Routes (right `device:wg`, read resource `device.workgroup.<name>`), query parameters in brackets:

- `GET /v1/device/workgroups/{wg}/usage` (`since`, `until`, `by`, `before`, `limit`)
- `GET /v1/device/workgroups/{wg}/audit` (`since`, `until`, `action`, `task`, `session`, `group_by`, `before`, `limit`)
- `GET /v1/device/workgroups/{wg}/operations` (`since`, `until`, `type`, `state`, `group_by`, `before`, `limit`)
- `GET /v1/device/workgroups/{wg}/metrics` (`entity_kind`, `entity_id`, `metric`, `since`, `until`, `step`)

Query values follow `contracts/api/device-workgroup-*.request.json`; anything else is `400 request_invalid` with the
pointer of the parameter. Without `since` audit and operations read the `MAX_DAYS` before `until` (usage and metrics
require it). Each read runs in one repeatable-read transaction (`agent_hub.device.routes`); every one is
read-logged by the pipeline. The semantics are those of `agent_hub.analysis.reads`.
"""

from __future__ import annotations

import asyncio
import re
from collections.abc import Callable
from typing import Any
from uuid import UUID

from agent_hub.analysis import reads
from agent_hub.audit import query as audit_query
from agent_hub.device.access import device_view
from agent_hub.device.keys import ActiveDevice, device_id
from agent_hub.device.routes import BEFORE_RE, LIST_LIMIT_RE, UUID7_RE
from agent_hub.errors import HubError
from agent_hub.http.context import Context, HubContext
from agent_hub.http.routing import HubRouter

router = HubRouter()
OPERATION_TYPE_RE = re.compile(r"(?=.{1,64}$)[a-z][a-z_]*(\.[a-z][a-z_]*)+")


def _invalid(name: str) -> HubError:
    return HubError("request_invalid", detail={"pointers": [f"/{name}"]})


def _text(name: str, value: str | None, pattern: re.Pattern[str]) -> str | None:
    if value is not None and (len(value) > 1024 or pattern.fullmatch(value) is None):
        raise _invalid(name)
    return value


def _limit(value: str | None) -> int:
    found = _text("limit", value, LIST_LIMIT_RE)
    return reads.LIST_DEFAULT if found is None else int(found)


def _group_by(value: str | None, allowed: str) -> bool:
    if value is not None and value != allowed:
        raise _invalid("group_by")
    return value is not None


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


@router.get(
    "/v1/device/workgroups/{wg}/usage", auth="device", right="device:wg", read_resource="device.workgroup.usage"
)
async def get_usage(
    context: Context,
    principal: ActiveDevice,
    *,
    wg: str,
    since: str | None = None,
    until: str | None = None,
    by: str | None = None,
    before: str | None = None,
    limit: str | None = None,
) -> dict[str, Any]:
    wg_id = _path_id(wg)
    now = context.clock()
    when = reads.period({"since": since, "until": until}, now, since_required=True)
    if by is not None and by not in reads.BY:
        raise _invalid("by")
    cursor = _text("before", before, BEFORE_RE)
    size = _limit(limit)
    device, root = device_id(principal), context.config.platform_root_key

    def work(connection: Any) -> dict[str, Any]:
        view = device_view(connection, device, root)
        return reads.usage(connection, view, wg_id, when, by=by or "task", before=cursor, limit=size, now=now)

    return await _read(context, work)


@router.get(
    "/v1/device/workgroups/{wg}/audit", auth="device", right="device:wg", read_resource="device.workgroup.audit"
)
async def get_audit(
    context: Context,
    principal: ActiveDevice,
    *,
    wg: str,
    since: str | None = None,
    until: str | None = None,
    action: str | None = None,
    task: str | None = None,
    session: str | None = None,
    group_by: str | None = None,
    before: str | None = None,
    limit: str | None = None,
) -> dict[str, Any]:
    wg_id = _path_id(wg)
    grouped = _group_by(group_by, "action")
    when = reads.period({"since": since, "until": until}, context.clock(), since_required=False)
    filters = audit_query.parse_filters({"action": action, "task": task, "session": session})
    cursor = _text("before", before, BEFORE_RE)
    size = _limit(limit)
    device, root = device_id(principal), context.config.platform_root_key

    def work(connection: Any) -> dict[str, Any]:
        view = device_view(connection, device, root)
        return reads.audit(connection, view, wg_id, when, filters=filters, before=cursor, limit=size, grouped=grouped)

    return await _read(context, work)


@router.get(
    "/v1/device/workgroups/{wg}/operations",
    auth="device",
    right="device:wg",
    read_resource="device.workgroup.operations",
)
async def get_operations(
    context: Context,
    principal: ActiveDevice,
    *,
    wg: str,
    since: str | None = None,
    until: str | None = None,
    type: str | None = None,
    state: str | None = None,
    group_by: str | None = None,
    before: str | None = None,
    limit: str | None = None,
) -> dict[str, Any]:
    wg_id = _path_id(wg)
    grouped = _group_by(group_by, "type")
    when = reads.period({"since": since, "until": until}, context.clock(), since_required=False)
    op_type = _text("type", type, OPERATION_TYPE_RE)
    if state is not None and state not in reads.OPERATION_STATES:
        raise _invalid("state")
    cursor = _text("before", before, BEFORE_RE)
    size = _limit(limit)
    device, root = device_id(principal), context.config.platform_root_key

    def work(connection: Any) -> dict[str, Any]:
        view = device_view(connection, device, root)
        return reads.operations(
            connection, view, wg_id, when, op_type=op_type, state=state, before=cursor, limit=size, grouped=grouped
        )

    return await _read(context, work)


@router.get(
    "/v1/device/workgroups/{wg}/metrics", auth="device", right="device:wg", read_resource="device.workgroup.metrics"
)
async def get_metrics(
    context: Context,
    principal: ActiveDevice,
    *,
    wg: str,
    entity_kind: str | None = None,
    entity_id: str | None = None,
    metric: str | None = None,
    since: str | None = None,
    until: str | None = None,
    step: str | None = None,
) -> dict[str, Any]:
    wg_id = _path_id(wg)
    now = context.clock()
    params = {
        "entity_kind": entity_kind,
        "entity_id": entity_id,
        "metric": metric,
        "since": since,
        "until": until,
        "step": step,
    }
    device, root = device_id(principal), context.config.platform_root_key

    def work(connection: Any) -> dict[str, Any]:
        return reads.metrics(connection, device_view(connection, device, root), wg_id, params, now)

    return await _read(context, work)
