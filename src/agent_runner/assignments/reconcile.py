"""Reconcile step 4: assignment changes, and the gate of a revoked device (05-runner.md sections 2.2 step 4 and
3.5; rules in `assignments.teardown`).

Order number 40: after the logs (step 2) and wraps (step 3) were verified and before the sessions (step 5), so a
workgroup the verified log took away is ended (sessions `stopping` with reason `unassigned`, VM deleted) before step 5
would look at it, and keys are deleted only once the log shows the rotation (or at once after a removal).
"""

from __future__ import annotations

from agent_runner.assignments.teardown import apply_assignments, revoked
from agent_runner.reconcile.engine import Run
from agent_runner.registry import ReconcileStep


async def gate(run: Run) -> None:
    """A revoked device (`sync_state.device_revoked`, task `revocation`) acts no more: every run halts first."""
    if await revoked(run.context) is not None:
        run.halt("revoked")


async def assignments_step(run: Run) -> None:
    if run.desired is None:
        return
    await apply_assignments(run.context, run.book)


STEPS = [ReconcileStep(5, "revoked", gate), ReconcileStep(40, "assignments", assignments_step)]
