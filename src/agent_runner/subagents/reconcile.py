"""Reconcile step of the subagent area (`<area>/reconcile.py:STEPS`, 05-runner.md section 2.2): the desired child
sessions (`depth` > 0, which RUN-04's step 50 leaves alone) against the local ones (`controller.Subagents.reconcile`).
Order number 55: after the sessions step, so its stops and starts of main sessions come first."""

from __future__ import annotations

from agent_runner.reconcile.engine import Run
from agent_runner.registry import ReconcileStep
from agent_runner.sessions.desired import desired_sessions
from agent_runner.subagents.controller import controller_for


async def subagents_step(run: Run) -> None:
    if run.desired is None:
        return
    controller = controller_for(run.context)
    if controller is None:
        return
    await controller.reconcile(desired_sessions(run.desired), run.book)


STEPS = [ReconcileStep(55, "subagents", subagents_step)]
