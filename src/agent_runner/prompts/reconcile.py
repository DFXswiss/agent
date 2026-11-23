"""Reconcile step 6 of 05-runner.md section 2.2 (order number 60): the prompts of the desired state.

Runs after the sessions step (50), so new sessions are known locally. The hub's `delivered_prompt_event_ids` and
`pending_prompts` are applied by `deliver.Deliverer.apply_desired` (checked routings, rows, a wake-up of the delivery
task); a malformed list fails the step like any malformed answer (`wire.ResponseInvalid`).
"""

from __future__ import annotations

from agent_runner.prompts.deliver import deliverer_for
from agent_runner.reconcile.engine import Run
from agent_runner.registry import ReconcileStep


async def prompts_step(run: Run) -> None:
    if run.desired is None:
        return
    deliverer = deliverer_for(run.context)
    if deliverer is not None:
        await deliverer.apply_desired(run.desired)


STEPS = [ReconcileStep(60, "prompts", prompts_step)]
