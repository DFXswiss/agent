"""Reconcile steps of the modes area (ADR-098 sections 3 and 4): `task_modes` (order 46: after the desired state was
read, before the sessions start) keeps the section `task_modes` of the desired state in the store (`state.store`);
`mode_drivers` (order 58: after the sessions and their pauses) runs the due driver steps (`drivers.run_once`). Without
a desired state (pending device, hub not reached) the stored modes stay and the drivers run on them."""

from __future__ import annotations

from agent_runner.modes import drivers, state
from agent_runner.orgs.db import session
from agent_runner.reconcile.engine import Run
from agent_runner.registry import ReconcileStep


async def modes_step(run: Run) -> None:
    if run.desired is None:
        return
    async with session(run.pool) as conn, conn.transaction():
        await state.store(conn, run.desired)


async def drivers_step(run: Run) -> None:
    await drivers.run_once(run.context)


STEPS = [ReconcileStep(46, "task_modes", modes_step), ReconcileStep(58, "mode_drivers", drivers_step)]
