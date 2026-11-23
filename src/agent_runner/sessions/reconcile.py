"""Reconcile step 5 of 05-runner.md section 2.2: desired sessions against the local ones (`SessionService.reconcile`).

Order number 50 (`reconcile.engine`). The step runs only with the desired state of an active device (step 1) after the
logs (step 2) and wraps (step 3) were verified, so the assignment check uses the current verified log. The platform
budget of the desired state is kept for the credential provisioning of the next session starts.
"""

from __future__ import annotations

from agent_runner.reconcile.engine import Run
from agent_runner.registry import ReconcileStep
from agent_runner.sessions.desired import desired_sessions
from agent_runner.sessions.service import service_for


async def sessions_step(run: Run) -> None:
    if run.desired is None:
        return
    service = service_for(run.context)
    if service is None:
        return
    wanted = desired_sessions(run.desired)
    service.env.desired = run.desired
    await service.reconcile(wanted, run.book)


STEPS = [ReconcileStep(50, "sessions", sessions_step)]
