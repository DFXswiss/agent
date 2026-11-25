"""Reconcile step `orchestrator` (order 55: after the sessions step, so a session the same run started is known;
05-runner.md section 17.1): the rows of ended sessions go (`work.sweep`), then every item of the desired state's
`orchestrators` list in order (`work.run_item`)."""

from __future__ import annotations

from agent_runner.orchestrator.env import orchestrator_for
from agent_runner.orchestrator.work import run_item, sweep, work_items
from agent_runner.reconcile.engine import Run
from agent_runner.registry import ReconcileStep


async def step(run: Run) -> None:
    if run.desired is None:
        return
    env = orchestrator_for(run.context)
    if env is None:
        return
    await sweep(env)
    for item in work_items(run.desired):
        await run_item(env, item)


STEPS = [ReconcileStep(55, "orchestrator", step)]
