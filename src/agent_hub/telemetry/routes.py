"""Routes of the telemetry area (03-api.md section 4.11, ADR-086 section 5).

- `POST /v1/telemetry/ui` (anon, body at most `body_bytes`): `{version, views: {<view type>: {count, blocked_ms,
  max_bucket}}}` with at most `views_per_report` views → `{}`. The web client sends it at most once an hour and only
  when it counted a main-thread block over 200 ms; it carries no cookie, no signature and no ID. A malformed version,
  view name or counter is `400 request_invalid`; the counters are added to the day's rows (`agent_hub.telemetry.store`).
  The anonymous rate limit per source address of the pipeline applies; the address is never stored.
- `GET /v1/platform/ui-health?days=` (browser, `platform:inviter`; `days` 1-90, default 7) → `{from, rows[,
  truncated]}` (`store.rows`); read log `read.platform.ui_health`.
"""

from __future__ import annotations

import asyncio
from typing import Annotated, Any, cast
from uuid import UUID

from fastapi import Query
from pydantic import BaseModel, ConfigDict, Field, StrictInt, StrictStr, StringConstraints

from agent_hub.db.pool import transaction
from agent_hub.http.context import Context, CurrentPrincipal, HubContext
from agent_hub.http.routing import HubRouter
from agent_hub.platform.invites import require_inviter
from agent_hub.telemetry import store
from agent_hub.telemetry.views import LIMITS, VERSION_PATTERN, VIEW_PATTERN

router = HubRouter()
MAX_SAFE = 2**53 - 1
DAYS_DEFAULT = 7
DAYS_PATTERN = r"^([1-9]|[1-8][0-9]|90)$"
ViewName = Annotated[str, StringConstraints(strict=True, pattern=VIEW_PATTERN, max_length=64)]


class Closed(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class ViewCounts(Closed):
    count: StrictInt = Field(ge=0, le=MAX_SAFE)
    blocked_ms: StrictInt = Field(ge=0, le=MAX_SAFE)
    max_bucket: StrictInt = Field(ge=0, le=MAX_SAFE)


class UiReport(Closed):
    version: StrictStr = Field(pattern=VERSION_PATTERN)
    views: dict[ViewName, ViewCounts] = Field(max_length=LIMITS["views_per_report"])


def _record(context: HubContext, body: UiReport) -> None:
    views = {name: store.Counts(item.count, item.blocked_ms, item.max_bucket) for name, item in body.views.items()}
    with transaction(context.pool) as connection:
        store.record(connection, context.clock().date(), body.version, views)


@router.post("/v1/telemetry/ui", auth="anon", right="anon", max_body=LIMITS["body_bytes"])
async def post_ui(body: UiReport, context: Context) -> dict[str, Any]:
    await asyncio.to_thread(_record, context, body)
    return {}


def _rows(context: HubContext, user: UUID, days: int) -> dict[str, Any]:
    with transaction(context.pool) as connection:
        require_inviter(connection, user)
        return store.rows(connection, context.clock().date(), days)


@router.get("/v1/platform/ui-health", auth="browser", right="platform:inviter", read_resource="platform.ui_health")
async def get_ui_health(
    context: Context, principal: CurrentPrincipal, days: Annotated[str | None, Query(pattern=DAYS_PATTERN)] = None
) -> dict[str, Any]:
    user = cast(UUID, principal.principal_id)
    count = DAYS_DEFAULT if days is None else int(days)
    return await asyncio.to_thread(_rows, context, user, count)
