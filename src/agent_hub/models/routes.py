"""Routes of the models area (ADR-081 sections 5 and 6, KOR-50).

- `POST /v1/tasks/{task}/model-consent` (`task:write`): a person allows DFX model access for the task while its own
  access is used up. The task keeps the consent (`task.model_consent`); the hub writes the audit row
  `task.model_consent` (`changed`: false when the task had it already), derives the task's attention record again (a
  `quota_choice` becomes `quota_wait`) and hints `reconcile {}` to the devices of the task's live sessions, which
  receive the consent as `model_consent` with the sessions and resume them. Answer `{task_id, model_consent: true}`.
- `GET /v1/orgs/{org}/quotas` (`org:member`, read resource `org.quotas`): `agent_hub.models.quotas.org_quotas` with
  the verified platform log (ADR-079) and the hub's online window.

Unknown, malformed and foreign IDs answer `404 not_found` alike (a write on a known task without the right: `403`).
"""

from __future__ import annotations

import asyncio
from typing import Any, cast
from uuid import UUID

from agent_hub.attention import store as attention
from agent_hub.auth.fields import Empty
from agent_hub.db.pool import transaction
from agent_hub.http.context import Context, CurrentPrincipal, HubContext
from agent_hub.http.routing import HubRouter
from agent_hub.models.quotas import org_quotas
from agent_hub.orgs import rights
from agent_hub.orgs.logs import platform_state
from agent_hub.sessions.effects import Effects
from agent_hub.sessions.taskstate import Actor, audit
from agent_hub.tasks.service import task_for_write
from agent_hub.workgroups.routes import path_id

router = HubRouter()


def consent(context: HubContext, user: UUID, task_id: UUID) -> tuple[dict[str, Any], Effects]:
    """`POST /v1/tasks/{task}/model-consent` (module docstring)."""
    now = context.clock()
    effects = Effects()
    with transaction(context.pool) as connection:
        task = task_for_write(connection, user, task_id)
        changed = connection.execute(
            "UPDATE hub.task SET model_consent = true WHERE id = %s AND NOT model_consent RETURNING id", (task.id,)
        ).fetchone()
        audit(connection, Actor("user", user), "task.model_consent", task, now, detail={"changed": changed is not None})
        if changed is not None:
            attention.refresh(connection, task.id, now, effects)
            for (device,) in connection.execute(
                "SELECT DISTINCT device_id FROM hub.session WHERE task_id = %s AND device_id IS NOT NULL"
                " AND state NOT IN ('stopped', 'failed') ORDER BY device_id",
                (task.id,),
            ):
                effects.hint(device, "reconcile", {})
    return {"task_id": str(task.id), "model_consent": True}, effects


def quotas(context: HubContext, user: UUID, org: UUID) -> dict[str, Any]:
    """`GET /v1/orgs/{org}/quotas` (module docstring)."""
    with context.pool.connection() as connection:
        rights.require(connection, user, rights.ORG_MEMBER, org_id=org)
        platform = platform_state(connection, context.config.platform_root_key)
        window = context.config.device_online_seconds
        return org_quotas(connection, org, user, now=context.clock(), window=window, platform=platform)


@router.post("/v1/tasks/{task}/model-consent", auth="browser", right="task:write")
async def post_model_consent(context: Context, principal: CurrentPrincipal, task: str, body: Empty) -> dict[str, Any]:
    user = cast(UUID, principal.principal_id)
    answer, effects = await asyncio.to_thread(consent, context, user, path_id(task))
    effects.flush()
    return answer


@router.get("/v1/orgs/{org}/quotas", auth="browser", right="org:member", read_resource="org.quotas")
async def get_org_quotas(context: Context, principal: CurrentPrincipal, org: str) -> dict[str, Any]:
    user = cast(UUID, principal.principal_id)
    return await asyncio.to_thread(quotas, context, user, path_id(org))
