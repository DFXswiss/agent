"""Reconcile step 7, device requests (05-runner.md sections 2.2 and 11.1, 03-api.md section 6.1): the active requests
listed in `requests` of the desired state are offered to the service, which also abandons flows whose root the hub
stopped listing (`RequestService.listed`)."""

from __future__ import annotations

from agent_runner.reconcile import wire
from agent_runner.reconcile.engine import Run
from agent_runner.registry import ReconcileStep
from agent_runner.requests.service import service_for


async def step(run: Run) -> None:
    desired = run.desired or {}
    items = wire.array(desired.get("requests", []), "requests")
    ids = [wire.uuid_text(wire.obj(item, "requests").get("request_id"), "requests") for item in items]
    service = service_for(run.context)
    if service is not None:
        service.listed(ids)


STEPS = [ReconcileStep(70, "requests", step)]
