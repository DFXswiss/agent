"""Reconcile step `model` (order 75, after the credential changes of step 70; 05-runner.md sections 2.2 step 7, 2.6,
10.2): the gateways of running sessions get renewed credentials, the owner's latest platform budget, or lose a
credential that is gone (`model.gateways`). The first run registers the purge listener of the credential cache."""

from __future__ import annotations

from agent_runner.model.gateways import refresh_all, subscribe
from agent_runner.reconcile.engine import Run
from agent_runner.registry import ReconcileStep
from agent_runner.sessions.service import service_for


async def step(run: Run) -> None:
    if run.desired is None:
        return
    service = service_for(run.context)
    if service is None:
        return
    subscribe(run.context)
    await refresh_all(service.env, set(service.tasks))


STEPS = [ReconcileStep(75, "model", step)]
