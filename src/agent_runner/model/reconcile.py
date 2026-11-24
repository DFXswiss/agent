"""Reconcile steps of the model area:

- `model` (order 75, after the credential changes of step 70; 05-runner.md sections 2.2 step 7, 2.6, 10.2): the
  gateways of running sessions get renewed credentials, the owner's latest platform budget, or lose a credential that
  is gone (`model.gateways`). The first run registers the purge listener of the credential cache.
- `model_fallback` (order 57, after `billing` 55 and before `prompts` 60; KOR-50, ADR-081 section 4): sessions on a
  used-up access are moved, paused and continued (`model.fallback`), on every desired state.
"""

from __future__ import annotations

from agent_runner.model import fallback
from agent_runner.model.gateways import refresh_all, subscribe
from agent_runner.prompts.deliver import deliverer_for
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


async def fallback_step(run: Run) -> None:
    if run.desired is None:
        return
    deliverer = deliverer_for(run.context)
    if deliverer is None:
        return
    await fallback.run_once(deliverer)


STEPS = [ReconcileStep(75, "model", step), ReconcileStep(57, "model_fallback", fallback_step)]
