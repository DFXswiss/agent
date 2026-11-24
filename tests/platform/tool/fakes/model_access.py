"""Model access routes of the hub fake as the hub runs them (ADR-081 sections 5 and 6, 03-api.md):

- `GET /v1/orgs/{org}/quotas` (`org:member`, else `404 not_found`): the answer stored for the org in
  `ModelAccess.quotas`, by default nothing readable, no operator subscriptions and no DFX access;
- `POST /v1/tasks/{task}/model-consent` (writers of the task's workgroup, unknown or not writable `404`): records the
  consent in `ModelAccess.consents` and answers `{task_id, model_consent: true}`.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, cast

from ..hubfake import HubFake, Json, Request

EMPTY: Json = {
    "own": {"windows": [], "unreadable": None, "limited": None},
    "operator": {"granted": False, "subscriptions": []},
    "platform": {"available": False},
    "summary": {"usable": 0, "exhausted": 0, "next_release": None},
}


@dataclass
class ModelAccess:
    quotas: dict[str, Any] = field(default_factory=dict)
    consents: list[str] = field(default_factory=list)


def model_access(hub: HubFake) -> ModelAccess:
    found = getattr(hub, "model_access", None)
    if found is None:
        found = ModelAccess()
        setattr(hub, "model_access", found)  # noqa: B010
    return cast(ModelAccess, found)


def install(hub: HubFake) -> None:
    found = model_access(hub)

    def org_quotas(request: Request, body: Any, org: str) -> tuple[int, Any]:
        stored = hub.orgs.get(org)
        if stored is None or stored.log.state.member(cast(str, request.user)) is None:
            return 404, {"error": "not_found"}
        return 200, found.quotas.get(org, EMPTY)

    def consent(request: Request, body: Any, task: str) -> tuple[int, Any]:
        stored = hub.tasks.get(task)
        wg = None if stored is None else stored["task"]["wg_id"]
        org = None if wg is None else hub.org_of_wg(wg)
        state = None if org is None else hub.orgs[org].log.state
        if state is None or not state.can_write_wg(cast(str, request.user), cast(str, wg)):
            return 404, {"error": "not_found"}
        found.consents.append(task)
        return 200, {"task_id": task, "model_consent": True}

    hub.add_route("GET", r"/v1/orgs/(?P<org>[0-9a-f-]+)/quotas", org_quotas)
    hub.add_route("POST", r"/v1/tasks/(?P<task>[0-9a-f-]+)/model-consent", consent)
