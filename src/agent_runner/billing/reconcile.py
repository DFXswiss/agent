"""Reconcile step `billing` (order 55: after the sessions step 50, which keeps the desired state for the session
area, and before the prompts step 60): the controlled stop and the continuation of `agent_runner.billing.pause`
(KOR-31, ADR-062 section 6). The hub sends `reconcile {}` when an organization's credit runs out or is added, so the
step runs within seconds."""

from __future__ import annotations

from agent_runner.billing.pause import enforce
from agent_runner.prompts.deliver import deliverer_for
from agent_runner.reconcile.engine import Run
from agent_runner.registry import ReconcileStep


async def step(run: Run) -> None:
    if run.desired is None:
        return
    deliverer = deliverer_for(run.context)
    if deliverer is None:
        return
    await enforce(deliverer, run.desired)


STEPS = [ReconcileStep(55, "billing", step)]
